#!/usr/bin/env python3
"""Live queries under load (ADR 0033 §7, item 2): what a re-run costs in a write storm, how long a change takes to reach
1,000 streams, whether GET /q/ slows while re-runs happen, and how fast ingest takes Debezium's batches.

Run manually, never from CI, on the benchmark laptop's usual terms (AC power, `powerprofilesctl set performance`):

    python benchmarks/live.py --out /tmp/qag-live setup all teardown

It measures the checkout it runs from (its commit is recorded). Every run appends one JSON line to
<out>/results.jsonl and prints a summary. Each part runs on its own cores - gunicorn, the events server, PostgreSQL
and the load (oha, the subscribers, the writer) - and PostgreSQL is reached at its container's own address, as in
benchmarks/oltp.py.

Debezium isn't run: the harness posts each change to POST /ingest/cdc/<source> itself, signed as Debezium Server
signs, right after the write commits. So the lags here are QueryAPIGate's share - from the signal to the stream -
plus the write; Debezium's own (0.5 to 1.7 s, polling PostgreSQL's log) is in ADR 0033 §9.

Blocks:
  m1  re-run cost: a write storm (writes plus Debezium-sized posts) against 100 groups, and the same storm naming a
      table no stream reads - the difference is what the re-runs cost (events server and database CPU per re-run)
  m2  delivery lag: 1,000 streams across 100 groups; one UPDATE changes all 100 rows; commit to arrival, per stream
  m3  GET /q/ while re-runs happen: oha on /q/item, with the same storm re-running 100 groups or none, interleaved
  m4  ingest throughput: oha posting batches of 200 change events, 8 and 32 at once
  m5  re-run capacity: m1's storm against 1,000 groups - does one events server keep each to once a second?
"""
import argparse
import asyncio
import base64
import glob
import hashlib
import hmac
import http.client
import json
import os
import random
import resource
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OHA = 'ghcr.io/hatoo/oha@sha256:3ec3dbf549ea197793482d47a6324797411406bbf438c2fe8b91f244ec641a2f'  # oha 1.16.0
PG_IMAGE = 'postgres:16@sha256:63e83b5a3dc36a917dccb76fe1bd2ac51f1ec9c1deb47ff20d4f04233bf6eae9'
PG = 'qag-live-bench'
API_PORT, EVENTS_PORT = 5191, 5192
API, EVENTS = f'http://127.0.0.1:{API_PORT}', f'http://127.0.0.1:{EVENTS_PORT}'
ADMIN = 'bench-admin'
ITEMS = 10_000      # rows /q/ looks up
GROUPS = 100        # items 1..100 are the ones streams watch
TICK = os.sysconf('SC_CLK_TCK')

args = OUT = None
_procs = {}


def log(*a):
    print(time.strftime('%H:%M:%S'), *a, flush=True)


def sh(*cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def psql(sql, db='postgres'):
    return sh('docker', 'exec', PG, 'psql', '-U', 'postgres', '-d', db, '-Atc', sql).stdout.strip()


def pg_ip():
    return sh('docker', 'inspect', '-f', '{{.NetworkSettings.IPAddress}}', PG).stdout.strip()


def secs(n):
    return 3 if args.smoke else n


def rounds(n):
    return 1 if args.smoke else min(n, args.rounds or n)


def record(row):
    row = {**row, 'ts': time.strftime('%Y-%m-%dT%H:%M:%S')}
    with open(OUT / 'results.jsonl', 'a') as f:
        f.write(json.dumps(row) + '\n')


def pct(values, p):
    if not values:
        return None
    values = sorted(values)
    return values[min(len(values) - 1, int(round(p / 100 * (len(values) - 1))))]


# ---------------------------------------------------------------- the machine

def cpu_list(spec):
    out = []
    for part in spec.split(','):
        lo, _, hi = part.partition('-')
        out += range(int(lo), int(hi or lo) + 1)
    return out


def mhz(spec):
    vals = []
    for c in cpu_list(spec):
        try:
            vals.append(int(open(f'/sys/devices/system/cpu/cpu{c}/cpufreq/scaling_cur_freq').read()) / 1000)
        except OSError:
            pass
    return round(max(vals)) if vals else None


def pkg_temp():
    for zone in glob.glob('/sys/class/thermal/thermal_zone*'):
        try:
            if open(zone + '/type').read().strip() == 'x86_pkg_temp':
                return int(open(zone + '/temp').read()) / 1000
        except OSError:
            pass
    return None


def environment():
    def read(pattern):
        for p in glob.glob(pattern):
            try:
                return open(p).read().strip()
            except OSError:
                pass
        return None
    commit = sh('git', '-C', str(REPO), 'rev-parse', 'HEAD').stdout.strip()
    dirty = bool(sh('git', '-C', str(REPO), 'status', '--porcelain', '--', 'queryapigate').stdout.strip())
    return {'profile': sh('powerprofilesctl', 'get').stdout.strip() if shutil.which('powerprofilesctl') else None,
            'ac': read('/sys/class/power_supply/A*/online'),
            'epp': read('/sys/devices/system/cpu/cpufreq/policy0/energy_performance_preference'),
            'pkg_temp_c': pkg_temp(), 'load1': os.getloadavg()[0], 'commit': commit, 'dirty': dirty,
            'cpus': {'gateway': args.gateway_cpus, 'events': args.events_cpus, 'db': args.db_cpus,
                     'load': args.load_cpus}}


def proc_cpu_s(root):
    """utime+stime of a process and its descendants."""
    total, todo, seen = 0.0, [root], set()
    while todo:
        pid = todo.pop()
        if pid in seen:
            continue
        seen.add(pid)
        try:
            fields = open(f'/proc/{pid}/stat').read().rsplit(')', 1)[1].split()
            total += (int(fields[11]) + int(fields[12])) / TICK
            todo += [int(p) for p in open(f'/proc/{pid}/task/{pid}/children').read().split()]
        except (FileNotFoundError, ProcessLookupError):
            pass
    return total


def pg_cpu_s():
    full = sh('docker', 'inspect', '-f', '{{.Id}}', PG).stdout.strip()
    for path in glob.glob(f'/sys/fs/cgroup/**/docker-{full}.scope/cpu.stat', recursive=True):
        for line in open(path):
            if line.startswith('usage_usec'):
                return int(line.split()[1]) / 1e6
    return 0.0


def cpu_now():
    return {'gateway': proc_cpu_s(_procs['serve'].pid), 'events': proc_cpu_s(_procs['events'].pid),
            'db': pg_cpu_s()}


def cpu_since(before):
    now = cpu_now()
    return {k: round(now[k] - before[k], 3) for k in now}


def live_metrics():
    text = urllib.request.urlopen(EVENTS + '/metrics', timeout=5).read().decode()
    found = {}
    for line in text.splitlines():
        if line.startswith('queryapigate_live_'):
            name, _, value = line.rpartition(' ')
            found[name.removeprefix('queryapigate_live_')] = float(value)
    return found


def metrics_since(before):
    now = live_metrics()
    return {k: now[k] - before.get(k, 0) for k in now if k.endswith('_total')}


# ---------------------------------------------------------------- QueryAPIGate

def api(method, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(API + path, method=method, data=data,
                                     headers={'X-API-Key': ADMIN, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read() or b'null')


def wait_healthy(url, what):
    for _ in range(150):
        try:
            urllib.request.urlopen(url + '/health', timeout=1)
            return
        except Exception:
            time.sleep(0.2)
    raise RuntimeError(f'{what} did not start - see {OUT}')


def start():
    """gunicorn (as the image runs it) and `queryapigate events`, each on its own cores, one PostgreSQL store."""
    stop()
    home = OUT / 'home'
    home.mkdir(exist_ok=True)
    psql('TRUNCATE events', 'qagstore')  # every block starts with an empty event log
    env = {**os.environ, 'PYTHONPATH': str(REPO), 'QUERYAPIGATE_HOME': str(home), 'QUERYAPIGATE_API_KEY': ADMIN,
           'QUERYAPIGATE_DATABASE_URL': f'postgresql://postgres:pw@{pg_ip()}:5432/qagstore',
           'QUERYAPIGATE_LIVE_QUERIES': '1', 'QUERYAPIGATE_RATE_LIMIT': '', 'QUERYAPIGATE_ACCESS_LOG': 'off',
           'QUERYAPIGATE_EVENTS_MAX_CONNECTIONS': '5000', 'QUERYAPIGATE_METADATA_CACHE_TTL': '5'}
    bindir = Path(sys.executable).parent
    log_file = open(OUT / 'server.log', 'w')
    _procs['serve'] = subprocess.Popen(
        ['taskset', '-c', args.gateway_cpus, str(bindir / 'gunicorn'), '--bind', f'127.0.0.1:{API_PORT}',
         '--workers', '2', '--worker-class', 'gthread', '--threads', '8', '--timeout', '120', '--no-control-socket',
         '-c', 'python:queryapigate.gunicorn_conf', 'queryapigate.app:create_app()'],
        env=env, cwd=home, stdout=log_file, stderr=log_file, start_new_session=True)
    wait_healthy(API, 'gunicorn')
    events_log = open(OUT / 'events.log', 'w')
    _procs['events'] = subprocess.Popen(
        ['taskset', '-c', args.events_cpus, sys.executable, '-m', 'queryapigate', 'events', '--port', str(EVENTS_PORT)],
        env=env, cwd=home, stdout=events_log, stderr=events_log, start_new_session=True)
    wait_healthy(EVENTS, 'the events server')


def stop():
    for name in ('events', 'serve'):
        process = _procs.pop(name, None)
        if process is None:
            continue
        t = time.monotonic()
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=20)
            log(f'{name} stopped in {time.monotonic() - t:.1f} s')
        except Exception:
            log(f'{name} still running {time.monotonic() - t:.0f} s after SIGTERM: killed')
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except Exception:
                pass


_keys = {}


def seed():
    """The connection and the saved query (once per store), and the app's key and the CDC source's secret - made
    afresh for each run of this harness and kept in memory only, never on disk. The servers must be running."""
    if _keys:
        return _keys
    try:
        api('GET', '/api/v1/connections/shop')
    except urllib.error.HTTPError:
        api('POST', '/api/v1/connections', {'name': 'shop', 'db': 'postgres', 'host': pg_ip(), 'port': 5432,
                                            'user': 'postgres', 'password': 'pw', 'database': 'shop'})
        api('POST', '/api/v1/queries', {'name': 'item', 'description': 'One item', 'connection_name': 'shop',
                                        'sql': 'SELECT id, name, price, stock FROM item WHERE id = :id',
                                        'parameters': {'id': {'type': 'int'}}, 'publish': True})
    for path in ('/api/v1/api-keys/app', '/api/v1/cdc-sources/shop'):  # an earlier run's: their secrets are gone
        try:
            api('DELETE', path)
        except urllib.error.HTTPError:
            pass
    _keys['app'] = api('POST', '/api/v1/api-keys', {'name': 'app', 'connections': [], 'queries': ['item']})['secret']
    _keys['cdc'] = api('POST', '/api/v1/cdc-sources', {'name': 'shop', 'connection': 'shop',
                                                       'server_name': 'shop'})['secret']
    return _keys


# ---------------------------------------------------------------- Debezium, as the harness plays it

def change(table, row_id):
    return {'schema': {}, 'payload': {'before': None, 'after': {'id': row_id}, 'op': 'u', 'ts_ms': 1,
                                      'source': {'name': 'shop', 'connector': 'postgresql', 'db': 'shop',
                                                 'schema': 'public', 'table': table}}}


def signed(secret, body, msg_id=None):
    msg_id = msg_id or f'msg_{random.getrandbits(48):x}'
    stamp = str(int(time.time()))
    key = base64.b64decode(secret.removeprefix('whsec_'))
    signature = base64.b64encode(hmac.new(key, f'{msg_id}.{stamp}.'.encode() + body, hashlib.sha256).digest())
    return {'webhook-id': msg_id, 'webhook-timestamp': stamp, 'webhook-signature': f'v1,{signature.decode()}',
            'Content-Type': 'application/json'}


class Poster:
    """Posts to /ingest/cdc/shop on one kept-alive connection, as Debezium's HTTP sink does."""

    def __init__(self, secret):
        self.secret, self.conn = secret, http.client.HTTPConnection('127.0.0.1', API_PORT, timeout=30)

    def post(self, table, ids):
        body = json.dumps([change(table, i) for i in ids]).encode()
        self.conn.request('POST', '/ingest/cdc/shop', body=body, headers=signed(self.secret, body))
        response = self.conn.getresponse()
        response.read()
        if response.status != 204:
            raise RuntimeError(f'ingest answered {response.status}')

    def close(self):  # else gunicorn waits out its graceful timeout for this kept-alive connection when stopped
        self.conn.close()


class Storm:
    """A write storm, in a thread: `writes` random UPDATEs a second to items 1..100, and `posts` Debezium-sized posts
    a second (20 changes each) naming `table` - public.item, which every stream reads, or public.other, which none
    does. Debezium batches, so a storm of writes is a few posts a second, each naming many rows."""

    def __init__(self, conn, secret, table, writes=200, posts=20, ids=GROUPS):
        self.conn, self.poster, self.table, self.ids = conn, Poster(secret), table, ids
        self.writes, self.posts, self.done = writes, posts, threading.Event()
        self.counts = {'writes': 0, 'posts': 0}

    def run(self):
        cur = self.conn.cursor()
        interval, next_post, start = 1 / self.writes, time.monotonic(), time.monotonic()
        while not self.done.is_set():
            cur.execute('UPDATE item SET stock = stock + 1 WHERE id = %s', (random.randint(1, self.ids),))
            self.counts['writes'] += 1
            now = time.monotonic()
            if now >= next_post:
                self.poster.post(self.table, [random.randint(1, self.ids) for _ in range(20)])
                self.counts['posts'] += 1
                next_post += 1 / self.posts
            pause = start + self.counts['writes'] * interval - time.monotonic()
            if pause > 0:
                time.sleep(pause)

    def __enter__(self):
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.done.set()
        self.thread.join(30)
        self.poster.close()


# ---------------------------------------------------------------- subscribers

class Subscribers:
    """Many /live streams in this process's event loop (in a thread), each recording when each result arrived."""

    def __init__(self, key, ids, per_id):
        self.key, self.paths = key, [f'/live/item?id={i}' for i in ids for _ in range(per_id)]
        self.arrivals = [[] for _ in self.paths]   # (monotonic time, reason, rows) per stream
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.writers = []

    async def _one(self, n, path):
        reader, writer = await asyncio.open_connection('127.0.0.1', EVENTS_PORT, limit=1 << 20)
        self.writers.append(writer)
        writer.write(f'GET {path} HTTP/1.1\r\nHost: bench\r\nX-API-Key: {self.key}\r\n\r\n'.encode())
        status = await reader.readline()
        if b' 200 ' not in status:
            raise RuntimeError(f'{path}: {status!r}')
        while (await reader.readline()) not in (b'\r\n', b''):
            pass
        buffer = b''
        while True:
            size = int((await reader.readline()).strip() or b'0', 16)
            if size == 0:
                return
            buffer += (await reader.readexactly(size + 2))[:-2]
            while b'\n\n' in buffer:
                block, buffer = buffer.split(b'\n\n', 1)
                data = [line[6:] for line in block.split(b'\n') if line.startswith(b'data: ')]
                if data:
                    result = json.loads(b''.join(data))
                    self.arrivals[n].append((time.monotonic(), result.get('reason'), result.get('rows')))

    def __enter__(self):
        self.thread.start()
        futures = [asyncio.run_coroutine_threadsafe(self._one(n, p), self.loop) for n, p in enumerate(self.paths)]
        self.futures = futures
        deadline = time.monotonic() + 120
        while sum(1 for a in self.arrivals if a) < len(self.paths):
            failed = [f for f in futures if f.done() and f.exception()]
            if failed:
                raise failed[0].exception()
            if time.monotonic() > deadline:
                raise RuntimeError('not every stream got its first result')
            time.sleep(0.05)
        return self

    def __exit__(self, *exc):
        async def close():
            for writer in self.writers:
                writer.close()
        asyncio.run_coroutine_threadsafe(close(), self.loop).result(10)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(10)

    def changes_after(self, t0, timeout):
        """Per stream, the first `change` result that arrived after t0 (None if none within timeout)."""
        deadline = time.monotonic() + timeout
        while True:
            firsts = [next((t for t, reason, _ in a if t > t0 and reason == 'change'), None) for a in self.arrivals]
            if all(f is not None for f in firsts) or time.monotonic() > deadline:
                return firsts
            time.sleep(0.01)


def data_conn():
    import psycopg2
    conn = psycopg2.connect(host=pg_ip(), port=5432, user='postgres', password='pw', dbname='shop')
    conn.autocommit = True
    return conn


# ---------------------------------------------------------------- blocks

def b_setup():
    """PostgreSQL 16, pinned: `shop` (10,000 items, and a table no stream reads) and the store `qagstore`."""
    sh('docker', 'rm', '-fv', PG)
    _keys.clear()  # a new store: an earlier store's key and source are gone with it
    started = sh('docker', 'run', '-d', '--name', PG, '--cpuset-cpus', args.db_cpus, '--shm-size=256m',
                 '-e', 'POSTGRES_PASSWORD=pw', PG_IMAGE, '-c', 'shared_buffers=1GB', '-c', 'max_connections=300')
    if started.returncode:
        sys.exit(started.stderr)
    for _ in range(60):
        if sh('docker', 'exec', PG, 'pg_isready', '-U', 'postgres', '-h', '127.0.0.1').returncode == 0:
            break
        time.sleep(1)
    psql('CREATE DATABASE shop')
    psql('CREATE DATABASE qagstore')
    psql('CREATE TABLE item (id int PRIMARY KEY, name text NOT NULL, price numeric(10, 2) NOT NULL, '
         'stock int NOT NULL); '
         f"INSERT INTO item SELECT n, 'Item ' || n, 10 + n % 90, 100 FROM generate_series(1, {ITEMS}) AS n; "
         'CREATE TABLE other (id int PRIMARY KEY, n int NOT NULL); '
         'INSERT INTO other SELECT n, 0 FROM generate_series(1, 100) AS n', 'shop')
    start()
    seed()
    stop()
    log('setup done; database at', pg_ip())


def b_teardown():
    stop()
    sh('docker', 'rm', '-fv', PG)
    log('teardown done')


def m1():
    """Re-run cost: the same storm against 100 groups, naming the table they read (re-runs) or one they don't (none),
    interleaved. What the re-runs cost is the difference."""
    start()
    keys = seed()
    conn = data_conn()
    with Subscribers(keys['app'], range(1, GROUPS + 1), 1):
        for r in range(rounds(5)):
            for table in (('item', 'other') if r % 2 == 0 else ('other', 'item')):
                time.sleep(2)
                cpu, metrics, t = cpu_now(), live_metrics(), time.monotonic()
                with Storm(conn, keys['cdc'], table) as storm:
                    time.sleep(secs(30))
                elapsed, used, counted = time.monotonic() - t, cpu_since(cpu), metrics_since(metrics)
                row = {'block': 'm1', 'round': r, 'signals_name': table, 'seconds': round(elapsed, 2),
                       **storm.counts, 'cpu_s': used, 'live': counted, 'mhz_events': mhz(args.events_cpus),
                       'mhz_gateway': mhz(args.gateway_cpus), 'pkg_temp_c': pkg_temp()}
                record(row)
                log(f"m1 r{r} {table:5}: {counted.get('reruns_total', 0):.0f} re-runs "
                    f"({counted.get('reruns_total', 0) / elapsed:.0f}/s), {counted.get('pushed_total', 0):.0f} pushed, "
                    f"{counted.get('postponed_total', 0):.0f} postponed; cpu {used}")
    stop()


def m2():
    """Delivery lag: 1,000 streams on 100 groups; one UPDATE changes all 100 rows, then the post says so."""
    start()
    keys = seed()
    conn = data_conn()
    cur = conn.cursor()
    poster = Poster(keys['cdc'])
    per_id = 1 if args.smoke else 10
    with Subscribers(keys['app'], range(1, GROUPS + 1), per_id) as subs:
        for r in range(rounds(20)):
            time.sleep(1.5)  # past QUERYAPIGATE_LIVE_MIN_INTERVAL: the round isn't throttled by the one before
            cpu = cpu_now()
            cur.execute('UPDATE item SET price = price + 0.01 WHERE id <= %s', (GROUPS,))
            committed = time.monotonic()
            poster.post('item', range(1, GROUPS + 1))
            posted = time.monotonic()
            firsts = subs.changes_after(committed, timeout=30)
            got = [f - committed for f in firsts if f is not None]
            row = {'block': 'm2', 'round': r, 'streams': len(firsts), 'groups': GROUPS, 'delivered': len(got),
                   'post_ms': round((posted - committed) * 1000, 2),
                   **{f'p{p}_ms': round(pct(got, p) * 1000, 1) for p in (50, 90, 95, 99, 100)},
                   'cpu_s': cpu_since(cpu), 'mhz_events': mhz(args.events_cpus), 'pkg_temp_c': pkg_temp()}
            record(row)
            log(f"m2 r{r}: {len(got)}/{len(firsts)} delivered; commit to arrival p50 {row['p50_ms']} ms, "
                f"p99 {row['p99_ms']} ms, max {row['p100_ms']} ms (post {row['post_ms']} ms)")
    poster.close()
    stop()


def oha(url, key, c, seconds, method='GET', body=None, headers=None):
    cmd = ['docker', 'run', '--rm', '--network', 'host', '--cpuset-cpus', args.load_cpus]
    if body is not None:
        cmd += ['-v', f'{body}:/body.json:ro']
    cmd += [OHA, '--no-tui', '--output-format', 'json', '-z', f'{seconds}s', '-c', str(c)]
    if key:
        cmd += ['-H', f'X-API-Key: {key}']
    for name, value in (headers or {}).items():
        cmd += ['-H', f'{name}: {value}']
    if body is not None:
        cmd += ['-m', method, '-D', '/body.json']
        cmd += [url]
    else:
        cmd += ['--rand-regex-url', url]
    d = json.loads(sh(*cmd).stdout)
    s, p = d['summary'], d['latencyPercentiles']
    codes = d.get('statusCodeDistribution', {})
    return {'rps': s['requestsPerSec'], 'success': s['successRate'], 'codes': codes,
            **{k: (v * 1000 if v is not None else None) for k, v in p.items() if k in ('p50', 'p90', 'p99', 'p99.9')}}


def m3():
    """GET /q/ under the same storm, re-running 100 groups or none, interleaved: only the re-runs differ."""
    start()
    keys = seed()
    conn = data_conn()
    url = API + r'/q/item\?id=[1-9][0-9]{0,3}'
    with Subscribers(keys['app'], range(1, GROUPS + 1), 1):
        oha(url, keys['app'], 16, 5)  # warm the pools and caches
        for r in range(rounds(6)):
            for table in (('item', 'other') if r % 2 == 0 else ('other', 'item')):
                time.sleep(2)
                cpu, metrics = cpu_now(), live_metrics()
                with Storm(conn, keys['cdc'], table):
                    time.sleep(1)
                    result = oha(url, keys['app'], 16, secs(20))
                row = {'block': 'm3', 'round': r, 'signals_name': table, **result, 'cpu_s': cpu_since(cpu),
                       'live': metrics_since(metrics), 'mhz_gateway': mhz(args.gateway_cpus), 'pkg_temp_c': pkg_temp()}
                record(row)
                log(f"m3 r{r} {table:5}: {result['rps']:.0f} req/s, p50 {result['p50']:.2f} ms, "
                    f"p99 {result['p99']:.2f} ms; {row['live'].get('reruns_total', 0):.0f} re-runs")
    stop()


def m4():
    """Ingest: batches of 200 change events, posted by oha 8 and 32 at once, with no stream open."""
    start()
    keys = seed()
    body = json.dumps([change('item', random.randint(1, ITEMS)) for _ in range(200)]).encode()
    path = OUT / 'batch.json'
    path.write_bytes(body)
    for r in range(rounds(3)):
        for c in ((8, 32) if r % 2 == 0 else (32, 8)):
            time.sleep(2)
            psql('TRUNCATE events', 'qagstore')
            cpu = cpu_now()
            # one signature for the whole run: the timestamp is good for 5 minutes, and nothing deduplicates ids
            headers = signed(keys['cdc'], body, 'msg_bench')
            result = oha(API + '/ingest/cdc/shop', None, c, secs(20), method='POST', body=path, headers=headers)
            used = cpu_since(cpu)
            requests = sum(result['codes'].values())
            row = {'block': 'm4', 'round': r, 'c': c, 'batch': 200, **result,
                   'changes_per_s': round(result['rps'] * 200), 'cpu_s': used,
                   'gateway_ms_per_post': round(used['gateway'] / max(requests, 1) * 1000, 3),
                   'db_ms_per_post': round(used['db'] / max(requests, 1) * 1000, 3),
                   'signals_stored': int(psql('SELECT count(*) FROM events', 'qagstore') or 0),
                   'mhz_gateway': mhz(args.gateway_cpus), 'pkg_temp_c': pkg_temp()}
            record(row)
            log(f"m4 r{r} c={c}: {result['rps']:.0f} posts/s ({row['changes_per_s']} changes/s), "
                f"p50 {result['p50']:.2f} ms, p99 {result['p99']:.2f} ms, codes {result['codes']}; "
                f"gateway {row['gateway_ms_per_post']} ms/post")
    stop()


def m5():
    """Re-run capacity: m1's storm, on 1,000 groups (items 1..1,000). Each may re-run once a second: whether one events
    server keeps up shows in re-runs a second against 1,000, and its CPU."""
    start()
    keys = seed()
    conn = data_conn()
    groups = 100 if args.smoke else 1000
    with Subscribers(keys['app'], range(1, groups + 1), 1):
        for r in range(rounds(3)):
            time.sleep(2)
            cpu, metrics, t = cpu_now(), live_metrics(), time.monotonic()
            with Storm(conn, keys['cdc'], 'item', ids=groups) as storm:
                time.sleep(secs(30))
            elapsed, used, counted = time.monotonic() - t, cpu_since(cpu), metrics_since(metrics)
            row = {'block': 'm5', 'round': r, 'groups': groups, 'seconds': round(elapsed, 2), **storm.counts,
                   'cpu_s': used, 'live': counted, 'reruns_per_s': round(counted.get('reruns_total', 0) / elapsed, 1),
                   'events_cores': round(used['events'] / elapsed, 3), 'pkg_temp_c': pkg_temp()}
            record(row)
            log(f"m5 r{r}: {groups} groups, {row['reruns_per_s']} re-runs/s, "
                f"events server {row['events_cores']} cores, db {used['db'] / elapsed:.2f} cores, "
                f"{counted.get('postponed_total', 0):.0f} postponed")
    stop()


def b_env():
    record({'block': 'env', **environment()})
    log('env', environment())


BLOCKS = {'setup': b_setup, 'teardown': b_teardown, 'env': b_env, 'm1': m1, 'm2': m2, 'm3': m3, 'm4': m4,
          'm5': m5}
ALL = ['env', 'm4', 'm1', 'm3', 'm2', 'm5', 'env']


def main():
    global args, OUT
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('blocks', nargs='+', help=f"any of {', '.join(BLOCKS)}, or all ({' '.join(ALL)})")
    parser.add_argument('--out', default='/tmp/qag-live-bench', help='results and logs (default %(default)s)')
    parser.add_argument('--gateway-cpus', default='0-3', help='taskset list for gunicorn (default %(default)s)')
    parser.add_argument('--events-cpus', default='4-7', help='taskset list for the events server (default %(default)s)')
    parser.add_argument('--db-cpus', default='8-11', help='cpuset for PostgreSQL (default %(default)s)')
    parser.add_argument('--load-cpus', default='12-19',
                        help='this harness (subscribers, writer) and oha (default %(default)s)')
    parser.add_argument('--smoke', action='store_true', help='short runs, one round: checks the mechanics only')
    parser.add_argument('--rounds', type=int, help='at most this many rounds per block')
    args = parser.parse_args()
    OUT = Path(args.out) / ('smoke' if args.smoke else '')
    OUT.mkdir(parents=True, exist_ok=True)
    os.sched_setaffinity(0, cpu_list(args.load_cpus))  # the subscribers and the writer are load, on its cores
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE, (hard, hard))  # 1,000 streams are 1,000 sockets
    names = [n for b in args.blocks for n in (ALL if b == 'all' else [b])]
    try:
        for name in names:
            log('=== block', name)
            BLOCKS[name]()
    finally:
        stop()
    log('done; results in', OUT / 'results.jsonl')


if __name__ == '__main__':
    main()

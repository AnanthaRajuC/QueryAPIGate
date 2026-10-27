#!/usr/bin/env python3
"""OLTP benchmark on PostgreSQL: two git refs of QueryAPIGate, side by side, on pgbench's 10-million-row schema. See
documentation/PERFORMANCE_V2.md for what each block measures and how to read the results; run with --help for options.

Run manually, never from CI. Needs Docker, `taskset`, and gunicorn and psycopg2 in this Python's environment
(`pip install -e ".[server,postgres,flow]"`); py-spy for the profile block. Every run appends one JSON line to
<out>/results.jsonl; benchmarks/oltp_report.py turns that into tables.

    python benchmarks/oltp.py --out /tmp/oltp setup all teardown
    python benchmarks/oltp_report.py /tmp/oltp/results.jsonl

The method, in short: each part on its own cores (gateway, database, load generator), the gateway reaching
PostgreSQL at the container's own address (not through Docker's userland port proxy), every comparison interleaved
- builds in rotated order, round after round - and a /health floor recorded before and after every block, so a
machine that changed speed mid-run shows.
"""
import argparse
import collections
import glob
import json
import os
import shutil
import signal
import statistics
import subprocess
import sys
import tarfile
import threading
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OHA = 'ghcr.io/hatoo/oha@sha256:3ec3dbf549ea197793482d47a6324797411406bbf438c2fe8b91f244ec641a2f'  # oha 1.16.0
PG_IMAGE = 'postgres:16'
PG = 'qag-oltp-bench'
PORT = 5091
U = f'http://127.0.0.1:{PORT}'
INNER_PORT = PORT + 1  # gunicorn's, behind the data plane (a +dp build, ADR 0028)
DATAPLANE_SECRET = 'bench-dataplane-secret-0123456789'
URLS = {
    'lookup': U + r'/q/account\?aid=[1-9][0-9]{0,6}',
    'page': U + r'/q/account_page\?start=[1-9][0-9]{0,5}&page_size=100',
    'write': U + r'/q/deposit\?aid=[1-9][0-9]{0,6}&delta=[1-9]',
    'hotwrite': U + r'/q/deposit\?aid=[1-9][0-9]?&delta=[1-9]',  # 99 accounts: row-lock contention
}
SCENARIOS = {'lookup_plain': ('lookup', 'plain'), 'lookup_gov': ('lookup', 'governed'),
             'page_plain': ('page', 'plain'), 'write_writer': ('write', 'writer'),
             'hotwrite_writer': ('hotwrite', 'writer')}

args = None
OUT = BUILDS = None
DATAPLANE_BUILDS = {}  # the head with the Rust data plane in front: outside BUILDS, so the b-blocks are as they were
_server = None
_dataplane = None
_server_meta = {}
TICK = os.sysconf('SC_CLK_TCK')


def log(*a):
    print(time.strftime('%H:%M:%S'), *a, flush=True)


def sh(*cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def psql(sql, db='postgres'):
    return sh('docker', 'exec', PG, 'psql', '-U', 'postgres', '-d', db, '-Atc', sql).stdout.strip()


def pg_ip():
    return sh('docker', 'inspect', '-f', '{{.NetworkSettings.IPAddress}}', PG).stdout.strip()


def record(row):
    with open(OUT / 'results.jsonl', 'a') as f:
        f.write(json.dumps(row) + '\n')


def seconds(n):
    return 2 if args.smoke else n


def rounds(n):
    return 1 if args.smoke else min(n, args.rounds or n)


# ---------------------------------------------------------------- accounting

def pg_cpu_s():
    """CPU seconds the database container has used (its cgroup v2 cpu.stat)."""
    full = sh('docker', 'inspect', '-f', '{{.Id}}', PG).stdout.strip()
    for path in glob.glob(f'/sys/fs/cgroup/**/docker-{full}.scope/cpu.stat', recursive=True):
        for line in open(path):
            if line.startswith('usage_usec'):
                return int(line.split()[1]) / 1e6
    return 0.0


def tree_cpu_s(root):
    """utime+stime of a process and its descendants (the gunicorn master and workers)."""
    total, todo, seen = 0.0, [root], set()
    while todo:
        pid = todo.pop()
        if pid in seen:
            continue
        seen.add(pid)
        try:
            with open(f'/proc/{pid}/stat') as f:
                fields = f.read().rsplit(')', 1)[1].split()
            total += (int(fields[11]) + int(fields[12])) / TICK
            with open(f'/proc/{pid}/task/{pid}/children') as f:
                todo += [int(p) for p in f.read().split()]
        except (FileNotFoundError, ProcessLookupError):
            pass
    return total


def cpus_busy_s(spec):
    """CPU seconds the given CPUs spent busy (not idle or iowait), from /proc/stat - whoever ran there."""
    wanted = {f'cpu{c}' for c in cpu_list(spec)}
    total = 0
    with open('/proc/stat') as f:
        for line in f:
            name, *vals = line.split()
            if name in wanted:
                v = [int(x) for x in vals]
                total += sum(v) - v[3] - v[4]  # everything but idle and iowait
    return total / TICK


def store_tx():
    """Transactions so far on each database. PostgreSQL may hold an idle session's counts back for up to 10 s, so a
    short run under-counts - read per-request figures from 30 s runs, or count statements with log_statement."""
    rows = psql("select datname, xact_commit + xact_rollback from pg_stat_database "
                "where datname in ('bench', 'qagstore') order by 1")
    return {d: int(n) for d, n in (r.split('|') for r in rows.splitlines())}


def cpu_list(spec):
    out = []
    for part in spec.split(','):
        lo, _, hi = part.partition('-')
        out += range(int(lo), int(hi or lo) + 1)
    return out


def mhz():
    """The busiest gateway core's clock: idle cores clock down and would dilute an average."""
    vals = []
    for c in cpu_list(args.gateway_cpus):
        try:
            with open(f'/sys/devices/system/cpu/cpu{c}/cpufreq/scaling_cur_freq') as f:
                vals.append(int(f.read()) / 1000)
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
    return {'profile': sh('powerprofilesctl', 'get').stdout.strip() if shutil.which('powerprofilesctl') else None,
            'ac': read('/sys/class/power_supply/A*/online'),
            'epp': read('/sys/devices/system/cpu/cpufreq/policy0/energy_performance_preference'),
            'pkg_temp_c': pkg_temp(), 'load1': os.getloadavg()[0]}


# ---------------------------------------------------------------- setup

def b_setup():
    """A pinned PostgreSQL 16 with pgbench's schema at scale 100 (`bench`) and an empty store (`qagstore`), iproute2
    installed for the network-delay block, and the two builds exported from git."""
    sh('docker', 'rm', '-fv', PG)
    started = sh('docker', 'run', '-d', '--name', PG, '--cpuset-cpus', args.db_cpus, '--cap-add', 'NET_ADMIN',
                 '-e', 'POSTGRES_PASSWORD=pw', PG_IMAGE, '-c', 'shared_buffers=2GB', '-c', 'max_connections=300',
                 '-c', 'synchronous_commit=on')
    if started.returncode:
        sys.exit(started.stderr)
    for _ in range(60):
        if sh('docker', 'exec', PG, 'pg_isready', '-U', 'postgres').returncode == 0:
            break
        time.sleep(1)
    time.sleep(2)
    psql('CREATE DATABASE bench')
    psql('CREATE DATABASE qagstore')
    log('pgbench -i -s 100:', sh('docker', 'exec', PG, 'pgbench', '-U', 'postgres', '-i', '-s', '100', '-q',
                                 'bench').stderr.strip().splitlines()[-1])
    sh('docker', 'exec', PG, 'bash', '-c', 'apt-get update -qq && apt-get install -y -qq iproute2')
    for label, (ref, _) in {**BUILDS}.items():
        target = OUT / 'builds' / label.split('+')[0]
        if target.exists():
            continue
        target.mkdir(parents=True)
        archive = subprocess.run(['git', '-C', str(REPO), 'archive', '--format=tar', ref, 'queryapigate'],
                                 capture_output=True)
        if archive.returncode:
            sys.exit(f'git archive {ref}: {archive.stderr}')
        tar = OUT / 'build.tar'
        tar.write_bytes(archive.stdout)
        with tarfile.open(tar) as t:
            t.extractall(target, filter='data')
        tar.unlink()
    log('setup done; database at', pg_ip())


def b_teardown():
    stop()
    sh('docker', 'rm', '-fv', PG)  # -v: the image's data volume too, or every run leaves gigabytes behind
    log('database removed')


# ---------------------------------------------------------------- the gateway

def stop():
    global _server, _dataplane, _server_meta
    _server_meta = {}
    for process in (_dataplane, _server):
        if process is None:
            continue
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=20)
        except Exception:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except Exception:
                pass
    _server = _dataplane = None
    for port in (PORT, INNER_PORT):
        for _ in range(50):
            if sh('bash', '-c', f'ss -ltn | grep -q ":{port} "').returncode:
                break
            time.sleep(0.2)


def wait_healthy(url, what):
    for _ in range(150):
        try:
            urllib.request.urlopen(url + '/health', timeout=1)
            return
        except Exception:
            time.sleep(0.2)
    raise RuntimeError(f'{what} did not start - see {OUT / "server.log"}')


def start(build, workers=1, access_log=True, access_format=None, profile_to=None, threads=8):
    """gunicorn for ``build``, pinned to the gateway's cores, as the Docker image runs it (gthread)."""
    global _server, _server_meta
    stop()
    ref, extra = BUILDS[build] if build in BUILDS else DATAPLANE_BUILDS[build]
    extra = dict(extra)
    dataplane = extra.pop('dataplane', False)  # the Rust data plane in front (ADR 0028), on the same cores
    port = INNER_PORT if dataplane else PORT
    if dataplane:
        extra['QUERYAPIGATE_DATAPLANE_SECRET'] = DATAPLANE_SECRET
    home = OUT / 'home'
    home.mkdir(exist_ok=True)
    bindir = Path(sys.executable).parent
    env = {**os.environ, 'PYTHONPATH': str(OUT / 'builds' / build.split('+')[0]), 'QUERYAPIGATE_HOME': str(home),
           'QUERYAPIGATE_API_KEY': 'bench-admin', 'QUERYAPIGATE_ALLOW_WRITES': '1',
           'QUERYAPIGATE_DATABASE_URL': f'postgresql://postgres:pw@{pg_ip()}:5432/qagstore', **extra,
           # from 0.43 gunicorn_conf.py writes an access log to stdout unless told not to: say so either way
           'QUERYAPIGATE_ACCESS_LOG': 'on' if access_log else 'off'}
    cmd = [str(bindir / 'gunicorn'), '--bind', f'127.0.0.1:{port}', '--workers', str(workers), '--worker-class',
           'gthread', '--threads', str(threads), '--timeout', '120', '--no-control-socket',
           '-c', 'python:queryapigate.gunicorn_conf']
    if access_log:
        cmd += ['--access-logfile', str(OUT / 'access.log')]
    if access_format:
        cmd += ['--access-logformat', access_format]
    cmd += ['queryapigate.app:create_app()']
    if profile_to:
        cmd = [args.py_spy, 'record', '--subprocesses', '--rate', '200', '--format', 'raw', '-o', str(profile_to),
               '--'] + cmd
    cmd = ['taskset', '-c', args.gateway_cpus] + cmd
    out = open(OUT / 'server.log', 'a')
    _server = subprocess.Popen(cmd, env=env, cwd=home, stdout=out, stderr=out, start_new_session=True)
    wait_healthy(f'http://127.0.0.1:{port}', f'gateway {build}')
    threads_dp = None
    if dataplane:  # after gunicorn: it reads the store gunicorn's process keeps
        global _dataplane
        threads_dp = args.dataplane_threads or len(cpu_list(args.gateway_cpus))
        _dataplane = subprocess.Popen(
            ['taskset', '-c', args.gateway_cpus, args.dataplane_bin],
            env={**env, 'QUERYAPIGATE_DATAPLANE_LISTEN': f'127.0.0.1:{PORT}',
                 'QUERYAPIGATE_DATAPLANE_UPSTREAM': f'http://127.0.0.1:{INNER_PORT}',
                 'QUERYAPIGATE_DATAPLANE_THREADS': str(threads_dp)},
            cwd=home, stdout=out, stderr=out, start_new_session=True)
        wait_healthy(U, f'the data plane for {build}')
    _server_meta = {'build': build, 'workers': workers, 'access_log': access_log, 'threads': threads,
                    'dataplane_threads': threads_dp}


def gateway_cpu_s():
    """CPU seconds the gateway has used: gunicorn's master and workers, and any data plane in front of them."""
    return tree_cpu_s(gunicorn_master()) + (tree_cpu_s(_dataplane.pid) if _dataplane is not None else 0.0)


def gunicorn_master():
    port = INNER_PORT if _dataplane is not None else PORT
    pids = [int(p) for p in sh('pgrep', '-f', f'gunicorn --bind 127.0.0.1:{port}').stdout.split()]
    for p in pids:  # the master is the one whose parent isn't another match
        try:
            ppid = int(open(f'/proc/{p}/stat').read().rsplit(')', 1)[1].split()[1])
        except OSError:
            continue
        if ppid not in pids:
            return p
    return pids[0]


def api(method, path, body):
    req = urllib.request.Request(U + path, method=method, data=json.dumps(body).encode(),
                                 headers={'X-API-Key': 'bench-admin', 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read() or b'null')


def seed():
    """The connection, the three saved queries and the three keys - once per store. The keys are kept with the store
    they belong to: a --smoke run writes its results under <out>/smoke but seeds the same store, so both read
    <out>/keys.json (seeding a seeded store again would fail on the existing connection)."""
    keys_file = Path(args.out) / 'keys.json'
    if keys_file.exists():
        return
    start(args.head_label)
    api('POST', '/api/v1/connections', {'name': 'oltp', 'db': 'postgres', 'host': pg_ip(), 'port': 5432,
                                        'database': 'bench', 'user': 'postgres', 'password': 'pw', 'active': True})
    for name, sql, params, desc in [
        ('account', 'SELECT aid, bid, abalance FROM pgbench_accounts WHERE aid = :aid',
         {'aid': {'type': 'int', 'required': True}}, 'One account by id'),
        ('account_page', 'SELECT a.aid, a.abalance, b.bid, b.bbalance FROM pgbench_accounts a JOIN pgbench_branches b '
         'ON b.bid = a.bid WHERE a.aid BETWEEN :start AND :start + 99 ORDER BY a.aid',
         {'start': {'type': 'int', 'required': True}}, '100 accounts with their branch'),
        ('deposit', 'UPDATE pgbench_accounts SET abalance = abalance + :delta WHERE aid = :aid',
         {'aid': {'type': 'int', 'required': True}, 'delta': {'type': 'int', 'required': True}}, 'TPC-B style update'),
    ]:
        api('POST', '/api/v1/queries', {'name': name, 'connection_name': 'oltp', 'sql': sql, 'parameters': params,
                                        'description': desc, 'publish': True})
    keys = {}
    for kind, body in [
        ('plain', {'name': 'plain', 'connections': [], 'queries': ['account', 'account_page']}),
        ('governed', {'name': 'governed', 'connections': [], 'queries': ['account', 'account_page'],
                      'allowed_tables': ['pgbench_accounts', 'pgbench_branches'],
                      'mask': [{'column': '*.abalance', 'action': 'hash'}]}),
        ('writer', {'name': 'writer', 'connections': [], 'queries': ['deposit'], 'allow_writes': True,
                    'allowed_write_ops': ['update']}),
    ]:
        keys[kind] = api('POST', '/api/v1/api-keys', body)['secret']
    keys_file.write_text(json.dumps(keys))


# ---------------------------------------------------------------- load

def oha(url, key=None, c=32, secs=30, q=None, correction=False, regex=True):
    cmd = ['docker', 'run', '--rm', '--network', 'host', '--cpuset-cpus', args.load_cpus, OHA, '--no-tui',
           '--output-format', 'json', '-z', f'{seconds(secs)}s', '-c', str(c)]
    if key:
        cmd += ['-H', f'X-API-Key: {key}']
    if q:
        cmd += ['-q', str(q)]
    if correction:
        cmd += ['--latency-correction']
    cmd += ['--rand-regex-url', url] if regex else [url]
    d = json.loads(sh(*cmd).stdout)
    s, p = d['summary'], d['latencyPercentiles']
    codes = d.get('statusCodeDistribution', {})
    return {'rps': s['requestsPerSec'], 'success': s['successRate'], 'codes': codes, 'requests': sum(codes.values()),
            **{k: (v * 1000 if v is not None else None) for k, v in p.items()}}  # milliseconds


def measure(block, scenario, c=32, secs=30, q=None, correction=False, warm=True, extra=None, rnd=None):
    keys = json.loads((Path(args.out) / 'keys.json').read_text())  # where seed() keeps them, smoke or not
    kind, keyname = SCENARIOS[scenario]
    url, key = URLS[kind], keys[keyname]
    if warm:
        oha(url, key, c=8, secs=5)
    freqs, done = [], threading.Event()

    def sample():
        while not done.wait(2):
            freqs.append(mhz())
    sampler = threading.Thread(target=sample, daemon=True)
    tx0, g0, p0, l0, t0 = store_tx(), gateway_cpu_s(), pg_cpu_s(), cpus_busy_s(args.load_cpus), time.time()
    sampler.start()
    r = oha(url, key, c=c, secs=secs, q=q, correction=correction)
    done.set()
    wall = time.time() - t0
    g1, p1, l1 = gateway_cpu_s(), pg_cpu_s(), cpus_busy_s(args.load_cpus)
    time.sleep(1.5)
    tx1 = store_tx()
    n = max(r['requests'], 1)
    row = {'block': block, 'scenario': scenario, 'c': c, 'q': q, 'seconds': seconds(secs), 'round': rnd,
           **_server_meta, **r, 'gw_cores': (g1 - g0) / wall, 'pg_cores': (p1 - p0) / wall,
           'load_cores': (l1 - l0) / wall, 'load_cpus': len(cpu_list(args.load_cpus)),
           'store_tx_per_req': (tx1['qagstore'] - tx0['qagstore']) / n,
           'data_tx_per_req': (tx1['bench'] - tx0['bench']) / n,
           'mhz_gw': round(sum(f for f in freqs if f) / len(freqs)) if any(freqs) else None,
           'pkg_temp_c': pkg_temp(), 'ts': time.strftime('%Y-%m-%dT%H:%M:%S'), **(extra or {})}
    record(row)
    log(f'{block} {row["build"]} w{row["workers"]} t{row["threads"]} {scenario} c={c} q={q} r{rnd}: '
        f'{r["rps"]:.0f} req/s p50 {r["p50"]:.2f} p99 {r["p99"]:.2f} ok {r["success"]:.4f} '
        f'gw {row["gw_cores"]:.2f} pg {row["pg_cores"]:.2f} store_tx {row["store_tx_per_req"]:.2f}')
    return row


def floor(block, label):
    """/health on one worker of the head build: the same work every time, so a change here is the machine."""
    if _server_meta.get('build') != args.head_label or _server_meta.get('workers') != 1:
        start(args.head_label)
    r = oha(U + '/health', None, c=1, secs=10, regex=False)
    row = {'block': block, 'scenario': 'health_floor', 'label': label, 'rps': r['rps'], 'p50': r['p50'],
           **environment(), 'mhz_gw': mhz(), 'ts': time.strftime('%Y-%m-%dT%H:%M:%S')}
    record(row)
    log(f'{block} floor {label}: /health {r["rps"]:.0f} req/s, {row["pkg_temp_c"]} C')


def rotations(items, n):
    """Round r runs the items rotated by r - A B C / B C A / C A B - so none is always first or last."""
    return [(r, items[r % len(items):] + items[:r % len(items)]) for r in range(rounds(n))]


# ---------------------------------------------------------------- blocks

def b_env():
    py = str(Path(sys.executable))

    def version(module):
        return sh(py, '-c', f'import importlib.metadata as m; print(m.version("{module}"))').stdout.strip()
    record({'block': 'env', 'scenario': 'env', **environment(), 'builds': list(BUILDS),
            'refs': {label: sh('git', '-C', str(REPO), 'rev-parse', f'{ref}^{{commit}}').stdout.strip()
                     for label, (ref, _) in BUILDS.items()},
            'python': sh(py, '-V').stdout.strip(), 'gunicorn': version('gunicorn'),
            'psycopg2': version('psycopg2-binary') or version('psycopg2'), 'flask': version('flask'),
            'sqlglot': version('sqlglot'), 'postgres': psql('show server_version'), 'oha': OHA,
            'kernel': os.uname().release,
            'cpu': sh('bash', '-c', "lscpu | sed -n 's/^Model name: *//p'").stdout.strip(),
            'cpus': {'gateway': args.gateway_cpus, 'database': args.db_cpus, 'load': args.load_cpus},
            'dataplane': dataplane_identity(),
            'ts': time.strftime('%Y-%m-%dT%H:%M:%S')})
    log('environment recorded')


def b_pgbench():
    """The database on its own, from the load generator's cores."""
    for r in range(rounds(5)):
        for label, extra in [('pk_c1', ['-S', '-c', '1', '-j', '1']), ('pk_c32', ['-S', '-c', '32', '-j', '8']),
                             ('tpcb_c32', ['-c', '32', '-j', '8'])]:
            out = sh('docker', 'run', '--rm', '--cpuset-cpus', args.load_cpus, '-e', 'PGPASSWORD=pw', PG_IMAGE,
                     'pgbench', '-h', pg_ip(), '-U', 'postgres', *extra, '-T', str(seconds(10)), 'bench').stdout
            tps = float(next(line for line in out.splitlines() if line.startswith('tps')).split()[2])
            lat = float(next(line for line in out.splitlines() if 'latency average' in line).split()[3])
            record({'block': 'pgbench', 'scenario': label, 'round': r, 'tps': tps, 'latency_ms': lat,
                    'ts': time.strftime('%Y-%m-%dT%H:%M:%S')})
            log('pgbench', label, r, tps, lat)


def b1():
    """One process, 32 callers: every build and call, interleaved, 5 rounds of 30 s."""
    floor('b1', 'start')
    for scenario in ['lookup_plain', 'lookup_gov', 'page_plain', 'write_writer']:
        for r, order in rotations(list(BUILDS), 5):
            for build in order:
                start(build)
                measure('b1', scenario, c=32, secs=30, rnd=r)
    floor('b1', 'end')


def b2():
    """One caller at a time: the latency of a single call."""
    floor('b2', 'start')
    for scenario in ['lookup_plain', 'lookup_gov']:
        for r, order in rotations(list(BUILDS), 5):
            for build in order:
                start(build)
                measure('b2', scenario, c=1, secs=30, rnd=r)
    floor('b2', 'end')


def b3():
    """Open loop: fixed offered rates from 25% to 110% of each build's capacity, latency corrected for coordinated
    omission. Capacity is measured first, in the same block: the median of 5 closed-loop rounds at 32 callers."""
    floor('b3', 'start')
    builds = [args.head_label, args.head_label + '+cache']
    caps = collections.defaultdict(list)
    for r, order in rotations(builds, 5):
        for build in order:
            start(build)
            caps[build].append(measure('b3cap', 'lookup_plain', c=32, secs=30, rnd=r)['rps'])
    caps = {b: statistics.median(v) for b, v in caps.items()}
    fracs = (0.25, 0.5, 0.75, 0.9, 1.0, 1.1)
    for r, order in rotations(builds, 5):
        for build in order:
            start(build)
            for frac in fracs:
                measure('b3', 'lookup_plain', c=128, secs=20, q=int(caps[build] * frac), correction=True, rnd=r,
                        warm=(frac == fracs[0]), extra={'capacity': caps[build], 'offered_frac': frac})
    floor('b3', 'end')


def b4():
    """Scale-out: 1, 2, 4 and 8 workers, 64 callers, with CPU and latency."""
    floor('b4', 'start')
    for workers in (1, 2, 4, 8):
        for scenario in ['lookup_plain', 'write_writer']:
            for r, order in rotations([args.head_label, args.head_label + '+cache'], 5):
                for build in order:
                    start(build, workers=workers)
                    measure('b4', scenario, c=64, secs=30, rnd=r)
    floor('b4', 'end')


def b5():
    """Cold path: 150 s that cross the 60 s schema and masking-trace expiry, timed per request from the access log."""
    floor('b5', 'start')
    for scenario in ['lookup_gov', 'lookup_plain']:
        (OUT / 'access.log').unlink(missing_ok=True)
        start(args.head_label, access_format='%(t)s %(D)s %(s)s')
        measure('b5', scenario, c=8, secs=150)
        stop()
        (OUT / 'access.log').replace(OUT / f'access_{scenario}.log')
    floor('b5', 'end')


def b6():
    """What gunicorn's access log costs."""
    floor('b6', 'start')
    for r, order in rotations([True, False], 5):
        for on in order:
            start(args.head_label, access_log=on)
            measure('b6', 'lookup_plain', c=32, secs=30, rnd=r)
    floor('b6', 'end')


def b7(delay='0.5ms'):
    """A database across a simulated network: netem delays everything the database sends."""
    floor('b7', 'start')
    sh('docker', 'exec', PG, 'tc', 'qdisc', 'add', 'dev', 'eth0', 'root', 'netem', 'delay', delay)
    try:
        for c in (1, 32):
            for r, order in rotations(list(BUILDS), 5):
                for build in order:
                    start(build)
                    measure('b7', 'lookup_plain', c=c, secs=30, rnd=r, extra={'netem': delay})
    finally:
        sh('docker', 'exec', PG, 'tc', 'qdisc', 'del', 'dev', 'eth0', 'root')
    floor('b7', 'end')


def b8():
    """Reads and writes at once; writes on 99 hot rows."""
    floor('b8', 'start')
    keys = json.loads((Path(args.out) / 'keys.json').read_text())  # where seed() keeps them, smoke or not
    for r in range(rounds(5)):
        start(args.head_label)
        oha(URLS['lookup'], keys['plain'], c=8, secs=5)
        result = {}

        def run(name, url, key, c, result=result):
            result[name] = oha(url, key, c=c, secs=30)
        threads = [threading.Thread(target=run, args=('reads', URLS['lookup'], keys['plain'], 24)),
                   threading.Thread(target=run, args=('writes', URLS['write'], keys['writer'], 8))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        for name, res in result.items():
            record({'block': 'b8', 'scenario': f'mixed_{name}', 'round': r, **_server_meta, **res,
                    'ts': time.strftime('%Y-%m-%dT%H:%M:%S')})
            log('b8 mixed', name, f"{res['rps']:.0f} req/s p99 {res['p99']:.2f}")
        measure('b8', 'hotwrite_writer', c=32, secs=30, rnd=r)
        measure('b8', 'write_writer', c=32, secs=30, rnd=r)
    floor('b8', 'end')


def b9():
    """Where the time goes: py-spy across the workers, 200 samples a second, for the head build with and without the
    metadata cache."""
    if not args.py_spy:
        log('b9 skipped: py-spy not found (pip install py-spy, or --py-spy PATH)')
        return
    for build in [args.head_label, args.head_label + '+cache']:
        out = OUT / f'profile_{build}.txt'
        start(build, profile_to=out)
        measure('b9', 'lookup_plain', c=32, secs=30, extra={'profiled': True})
        os.kill(_server.pid, signal.SIGINT)  # py-spy (taskset exec'd into it): stop sampling and write the profile
        for _ in range(100):
            if out.exists() and out.stat().st_size and _server.poll() is not None:
                break
            time.sleep(0.3)
        stop()
        log('b9 profile', out)


def b10():
    """What threads buy one worker."""
    floor('b10', 'start')
    for r, order in rotations([1, 2, 4, 8], 5):
        for threads in order:
            start(args.head_label, threads=threads)
            measure('b10', 'lookup_plain', c=32, secs=30, rnd=r)
    floor('b10', 'end')


PROBE = r"""
import random, sys, time
import psycopg2
conn = psycopg2.connect(sys.argv[1])
conn.autocommit = True
cur = conn.cursor()
mode, secs = sys.argv[2], float(sys.argv[3])
n, start = 0, time.perf_counter()
while time.perf_counter() < start + secs:
    aid = random.randint(1, 9_999_999)
    if mode == 'four':  # what a saved query sends: see PERFORMANCE_V2.md, Round trips per call
        cur.execute('BEGIN READ ONLY')
        cur.execute('SET LOCAL statement_timeout = 30000')
    cur.execute('SELECT aid, bid, abalance FROM pgbench_accounts WHERE aid = %s', (aid,))
    cur.fetchall()
    if mode == 'four':
        cur.execute('ROLLBACK')
    n += 1
print((time.perf_counter() - start) / n * 1000)
"""


def probe(mode):
    """ms per iteration of psycopg2 alone, on the gateway's cores: one statement, or the four a saved query sends."""
    out = sh('taskset', '-c', args.gateway_cpus, sys.executable, '-c', PROBE,
             f'postgresql://postgres:pw@{pg_ip()}:5432/bench', mode, str(seconds(10)))
    return float(out.stdout.strip())


def netem(delay):
    sh('docker', 'exec', PG, 'tc', 'qdisc', 'del', 'dev', 'eth0', 'root')
    if delay != '0ms':
        sh('docker', 'exec', PG, 'tc', 'qdisc', 'add', 'dev', 'eth0', 'root', 'netem', 'delay', delay)


def b11():
    """Round trips per call, measured: single-call latency at 0, 0.5, 1 and 2 ms of netem delay, every build. The slope
    of latency against the delay is how many round trips a call waits for - next to psycopg2 alone sending one
    statement and the four a saved query sends, whose round trips are known."""
    floor('b11', 'start')
    builds = list(BUILDS)
    try:
        for r, order in rotations(['0ms', '0.5ms', '1ms', '2ms'], 3):
            for delay in order:
                netem(delay)
                for mode in ('one', 'four'):
                    record({'block': 'b11', 'scenario': f'probe_{mode}', 'netem': delay, 'round': r,
                            'ms_per_call': probe(mode), 'ts': time.strftime('%Y-%m-%dT%H:%M:%S')})
                for build in builds[r % 3:] + builds[:r % 3]:
                    start(build)
                    measure('b11', 'lookup_plain', c=1, secs=20, rnd=r, extra={'netem': delay})
    finally:
        netem('0ms')
    floor('b11', 'end')


# ---------------------------------------------------------------- the Rust data plane (ADR 0028)

def dataplane_builds(base=True):
    """The builds the d-blocks compare: Python as 0.43 ships it (base), the head with and without the metadata cache,
    and the head with the data plane in front - on the same gateway cores - with and without it."""
    head = args.head_label
    return ([args.base.split('/')[-1]] if base else []) + [head, head + '+cache', head + '+dp', head + '+dp+cache']


def dataplane_identity():
    """Which data plane the d-blocks ran: its binary's SHA-256, the commit it was built from, and the compiler."""
    binary = Path(args.dataplane_bin)
    if not binary.exists():
        return None
    import hashlib
    return {'sha256': hashlib.sha256(binary.read_bytes()).hexdigest(),
            'commit': sh('git', '-C', str(REPO), 'rev-parse', 'HEAD').stdout.strip(),
            'dirty': bool(sh('git', '-C', str(REPO), 'status', '--porcelain', 'dataplane').stdout.strip()),
            'rustc': sh('rustc', '--version').stdout.strip()}


def d1():
    """32 callers, one gunicorn worker (as b1) - and the data plane in front of one. Calls a second per gateway core is
    the decision's measure; the write is forwarded, so it shows what the extra hop costs."""
    floor('d1', 'start')
    for scenario in ['lookup_plain', 'lookup_gov', 'page_plain']:
        for r, order in rotations(dataplane_builds(), 5):
            for build in order:
                start(build)
                measure('d1', scenario, c=32, secs=30, rnd=r)
    for r, order in rotations([args.head_label + '+cache', args.head_label + '+dp+cache'], 3):
        for build in order:
            start(build)
            measure('d1', 'write_writer', c=32, secs=30, rnd=r)
    floor('d1', 'end')


def d2():
    """One caller at a time: the latency of a single call, with and without the data plane."""
    floor('d2', 'start')
    for scenario in ['lookup_plain', 'lookup_gov']:
        for r, order in rotations(dataplane_builds(base=False), 5):
            for build in order:
                start(build)
                measure('d2', scenario, c=1, secs=20, rnd=r)
    floor('d2', 'end')


def d3(delay='0.5ms'):
    """The database 0.5 ms away (netem): one caller, and 32 - the store is in the same container, so its reads wait
    too: where the metadata cache matters most."""
    floor('d3', 'start')
    netem(delay)
    try:
        for c in (1, 32):
            for r, order in rotations(dataplane_builds(base=False), 5):
                for build in order:
                    start(build)
                    measure('d3', 'lookup_plain', c=c, secs=20, rnd=r, extra={'netem': delay})
    finally:
        netem('0ms')
    floor('d3', 'end')


def d4():
    """Capacity on the whole gateway: Python's best (one worker per gateway CPU) against the data plane with one
    worker behind it, 64 callers - both with the metadata cache."""
    floor('d4', 'start')
    workers = len(cpu_list(args.gateway_cpus))
    builds = [(args.head_label + '+cache', workers), (args.head_label + '+dp+cache', 1)]
    for scenario in ['lookup_plain', 'lookup_gov']:
        for r, order in rotations(builds, 3):
            for build, n in order:
                start(build, workers=n)
                measure('d4', scenario, c=64, secs=30, rnd=r)
    floor('d4', 'end')


BLOCKS = {'setup': b_setup, 'teardown': b_teardown, 'env': b_env, 'pgbench': b_pgbench, 'b1': b1, 'b2': b2,
          'b3': b3, 'b4': b4, 'b5': b5, 'b6': b6, 'b7': b7, 'b8': b8, 'b9': b9, 'b10': b10, 'b11': b11,
          'd1': d1, 'd2': d2, 'd3': d3, 'd4': d4}
ALL = ['env', 'pgbench', 'b1', 'b2', 'b3', 'b4', 'b5', 'b6', 'b7', 'b8', 'b9', 'b10', 'b11', 'env']


def main():
    global args, OUT, BUILDS
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('blocks', nargs='+', help=f"any of {', '.join(BLOCKS)}, or all ({' '.join(ALL)})")
    parser.add_argument('--out', default='/tmp/qag-oltp-bench', help='results, builds and logs (default %(default)s)')
    parser.add_argument('--base', default='v0.41.0', help='the git ref to compare against (default %(default)s)')
    parser.add_argument('--head', default='origin/main', help='the git ref under test (default %(default)s)')
    parser.add_argument('--gateway-cpus', default='0-7', help='taskset list for gunicorn (default %(default)s)')
    parser.add_argument('--db-cpus', default='8-11', help='cpuset for PostgreSQL (default %(default)s)')
    parser.add_argument('--load-cpus', default='12-19', help='cpuset for oha and pgbench (default %(default)s)')
    parser.add_argument('--py-spy', default=shutil.which('py-spy'), help='py-spy binary, for b9')
    parser.add_argument('--smoke', action='store_true', help='2 s runs, one round: checks the mechanics only')
    parser.add_argument('--rounds', type=int, help='at most this many rounds per comparison (default: each block\'s)')
    parser.add_argument('--dataplane-bin', default=str(REPO / 'dataplane' / 'target' / 'release' /
                                                         'queryapigate-dataplane'),
                        help='the Rust data plane, for the d-blocks (default %(default)s)')
    parser.add_argument('--dataplane-threads', type=int, help='its Tokio threads (default: one per gateway CPU)')
    args = parser.parse_args()
    OUT = Path(args.out)
    OUT.mkdir(parents=True, exist_ok=True)
    if args.smoke:
        OUT = OUT / 'smoke'
        OUT.mkdir(exist_ok=True)
    base_label, args.head_label = args.base.split('/')[-1], args.head.split('/')[-1]
    BUILDS = {base_label: (args.base, {}), args.head_label: (args.head, {}),
              args.head_label + '+cache': (args.head, {'QUERYAPIGATE_METADATA_CACHE_TTL': '5'})}
    DATAPLANE_BUILDS.update({args.head_label + '+dp': (args.head, {'dataplane': True}),
                             args.head_label + '+dp+cache': (args.head, {'dataplane': True,
                                                                         'QUERYAPIGATE_METADATA_CACHE_TTL': '5'})})
    names = [n for b in args.blocks for n in (ALL if b == 'all' else [b])]
    try:
        for name in names:
            log('=== block', name)
            if name not in ('setup', 'teardown'):
                seed()
            BLOCKS[name]()
    finally:
        stop()
    log('done')


if __name__ == '__main__':
    main()

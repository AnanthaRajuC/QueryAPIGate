#!/usr/bin/env python3
"""A TPC-C-derived, read-only benchmark on MySQL: one server, six databases - one per microservice - and per-user
queries that join across them, run through QueryAPIGate and directly. See documentation/PERFORMANCE_MYSQL_V1.md for
what each block measures and how to read the results; run with --help for options.

Run manually, never from CI. Needs Docker, `taskset`, the go-tpc binary (github.com/pingcap/go-tpc, --go-tpc), and
gunicorn and mysql-connector-python in this Python's environment (`pip install -e ".[server,mysql,postgres]"`);
py-spy for the profile block. Every run appends one JSON line to <out>/results.jsonl; benchmarks/tpcc_mysql_report.py
turns that into tables.

    python benchmarks/tpcc_mysql.py --out /tmp/qag-tpcc --go-tpc ./go-tpc setup A B teardown

The method is benchmarks/oltp.py's: each part on its own cores, the gateway reaching MySQL at the container's own
address, every comparison interleaved in rotated order, 5 rounds per cell, and a /health floor around every block.
"Not TPC-C": the data and two of the queries are TPC-C's, but this is not the TPC-C benchmark, and its numbers are
not TPC-C results.
"""
import argparse
import bisect
import collections
import json
import os
import random
import re
import shutil
import signal
import statistics
import subprocess
import sys
import tarfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import oltp  # noqa: E402 - the shared plumbing: pinning, CPU accounting, clock and temperature, rotations

REPO = Path(__file__).resolve().parents[1]
MYSQL, MYSQL_IMAGE = 'qag-tpcc-mysql', 'mysql:8.4'
PG, PG_IMAGE = 'qag-tpcc-store', 'postgres:16'  # the PostgreSQL store, for m8 only
SYSBENCH_IMAGE = 'perconalab/sysbench:latest'
NETSHOOT_IMAGE = 'nicolaka/netshoot:latest'  # runs `tc` inside a container's network namespace
PORT = 5091
U = f'http://127.0.0.1:{PORT}'
DISTRICTS, CUSTOMERS = 10, 3000  # per warehouse, per district - TPC-C's own
SCHEMAS = {  # one database per "microservice"
    'org': ['warehouse', 'district'], 'crm': ['customer'], 'orders': ['orders', 'new_order', 'order_line'],
    'catalog': ['item'], 'inventory': ['stock'], 'billing': ['history']}

# The saved queries. `user` queries take one customer (w, d, c); `district` ones a district and a threshold.
QUERIES = {
    'customer': ('user', 'One customer by id', """
        SELECT c_id, c_first, c_middle, c_last, c_balance, c_credit, c_since
        FROM crm.customer WHERE c_w_id = :w AND c_d_id = :d AND c_id = :c"""),
    'order_status': ('user', "TPC-C Order-Status: the customer, their last order and its lines", """
        SELECT c.c_first, c.c_last, c.c_balance, o.o_id, o.o_entry_d, o.o_carrier_id,
               ol.ol_i_id, ol.ol_supply_w_id, ol.ol_quantity, ol.ol_amount, ol.ol_delivery_d
        FROM crm.customer c
        JOIN orders.orders o ON o.o_w_id = c.c_w_id AND o.o_d_id = c.c_d_id AND o.o_c_id = c.c_id
        JOIN orders.order_line ol ON ol.ol_w_id = o.o_w_id AND ol.ol_d_id = o.o_d_id AND ol.ol_o_id = o.o_id
        WHERE c.c_w_id = :w AND c.c_d_id = :d AND c.c_id = :c
          AND o.o_id = (SELECT MAX(o2.o_id) FROM orders.orders o2
                        WHERE o2.o_w_id = :w AND o2.o_d_id = :d AND o2.o_c_id = :c)
        ORDER BY ol.ol_number"""),
    'order_history': ('user', "The customer's last orders, with item names", """
        SELECT c.c_last, o.o_id, o.o_entry_d, ol.ol_number, i.i_name, ol.ol_quantity, ol.ol_amount
        FROM crm.customer c
        JOIN orders.orders o ON o.o_w_id = c.c_w_id AND o.o_d_id = c.c_d_id AND o.o_c_id = c.c_id
        JOIN orders.order_line ol ON ol.ol_w_id = o.o_w_id AND ol.ol_d_id = o.o_d_id AND ol.ol_o_id = o.o_id
        JOIN catalog.item i ON i.i_id = ol.ol_i_id
        WHERE c.c_w_id = :w AND c.c_d_id = :d AND c.c_id = :c
        ORDER BY o.o_id DESC, ol.ol_number LIMIT 50"""),
    'payments': ('user', "The customer's payments", """
        SELECT c.c_last, h.h_date, h.h_amount, h.h_data
        FROM crm.customer c
        JOIN billing.history h ON h.h_c_w_id = c.c_w_id AND h.h_c_d_id = c.c_d_id AND h.h_c_id = c.c_id
        WHERE c.c_w_id = :w AND c.c_d_id = :d AND c.c_id = :c
        ORDER BY h.h_date DESC LIMIT 20"""),
    'profile': ('user', 'The customer with their district and warehouse', """
        SELECT c.c_first, c.c_last, c.c_city, c.c_phone, d.d_name, d.d_city, w.w_name, w.w_city
        FROM crm.customer c
        JOIN org.district d ON d.d_w_id = c.c_w_id AND d.d_id = c.c_d_id
        JOIN org.warehouse w ON w.w_id = c.c_w_id
        WHERE c.c_w_id = :w AND c.c_d_id = :d AND c.c_id = :c"""),
    'stock_level': ('district', "TPC-C Stock-Level: items low on stock among the district's last 20 orders", """
        SELECT COUNT(DISTINCT s.s_i_id) AS low_stock
        FROM org.district d
        JOIN orders.order_line ol ON ol.ol_w_id = d.d_w_id AND ol.ol_d_id = d.d_id
             AND ol.ol_o_id < d.d_next_o_id AND ol.ol_o_id >= d.d_next_o_id - 20
        JOIN inventory.stock s ON s.s_w_id = ol.ol_w_id AND s.s_i_id = ol.ol_i_id
        WHERE d.d_w_id = :w AND d.d_id = :d AND s.s_quantity < :threshold"""),
}
CACHED = 'order_status_cached'  # order_status with a response cache_ttl, for m7

args = None
OUT = BUILDS = None
_server = None
_server_meta = {}


def log(*a):
    print(time.strftime('%H:%M:%S'), *a, flush=True)


def sh(*cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def mysql(sql, user='root'):
    """A statement through the container's own client, as root - setup and accounting only, never the load."""
    out = sh('docker', 'exec', MYSQL, 'mysql', f'-u{user}', '-ppw', '-N', '-B', '-e', sql)
    if out.returncode:
        raise RuntimeError(f'mysql: {out.stderr.strip()}')
    return out.stdout.strip()


def ip(container):
    return sh('docker', 'inspect', '-f', '{{.NetworkSettings.IPAddress}}', container).stdout.strip()


def record(row):
    with open(OUT / 'results.jsonl', 'a') as f:
        f.write(json.dumps({**row, 'ts': time.strftime('%Y-%m-%dT%H:%M:%S')}) + '\n')


def seconds(n):
    return 2 if args.smoke else n


def rounds(n):
    return 1 if args.smoke else n


def container_cpu_s(name):
    full = sh('docker', 'inspect', '-f', '{{.Id}}', name).stdout.strip()
    for path in Path('/sys/fs/cgroup').glob(f'**/docker-{full}.scope/cpu.stat'):
        for line in path.read_text().splitlines():
            if line.startswith('usage_usec'):
                return int(line.split()[1]) / 1e6
    return 0.0


def questions():
    """Statements MySQL has received from every client - exact and immediate, unlike pg_stat_database."""
    return int(mysql("SHOW GLOBAL STATUS LIKE 'Questions'").split()[1])


def netem(container, delay):
    """Delay everything ``container`` sends, from a sidecar in its network namespace - the database image needs no
    `tc` of its own. '0ms' removes it."""
    base = ['docker', 'run', '--rm', '--net', f'container:{container}', '--cap-add', 'NET_ADMIN', NETSHOOT_IMAGE, 'tc']
    sh(*base, 'qdisc', 'del', 'dev', 'eth0', 'root')
    if delay != '0ms':
        out = sh(*base, 'qdisc', 'add', 'dev', 'eth0', 'root', 'netem', 'delay', delay)
        if out.returncode:
            raise RuntimeError(f'netem: {out.stderr.strip()}')


# ---------------------------------------------------------------- setup

def wait_mysql():
    for _ in range(120):
        if sh('docker', 'exec', MYSQL, 'mysqladmin', '-uroot', '-ppw', 'ping').returncode == 0:
            break
        time.sleep(1)
    time.sleep(3)


def b_setup():
    """MySQL 8.4 pinned to its cores; TPC-C data from go-tpc, moved into six databases; a read-only user; sysbench's
    own tables for its baseline; and the builds exported from git."""
    if not args.go_tpc:
        sys.exit('setup needs --go-tpc PATH (github.com/pingcap/go-tpc releases)')
    sh('docker', 'rm', '-fv', MYSQL)
    started = sh('docker', 'run', '-d', '--name', MYSQL, '--cpuset-cpus', args.db_cpus, '-e', 'MYSQL_ROOT_PASSWORD=pw',
                 MYSQL_IMAGE, '--innodb-buffer-pool-size=4G', '--max-connections=500', '--skip-log-bin')
    if started.returncode:
        sys.exit(started.stderr)
    wait_mysql()
    mysql('CREATE DATABASE tpcc')
    t0 = time.time()
    load = sh('taskset', '-c', args.load_cpus, args.go_tpc, 'tpcc', '--warehouses', str(args.warehouses), '-H',
              ip(MYSQL), '-P', '3306', '-U', 'root', '-p', 'pw', '-D', 'tpcc', '-T', '8', 'prepare',
              '--no-check')  # a prepare flag: its consistency queries run for hours on 20 warehouses
    loaded = mysql("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'tpcc' "
                   "AND table_name = 'warehouse'") == '1' and mysql('SELECT COUNT(*) FROM tpcc.warehouse')
    if load.returncode or loaded != str(args.warehouses):  # go-tpc exits 0 even on a usage error
        sys.exit(f'go-tpc prepare: {load.stdout[-2000:]}{load.stderr[-2000:]}')
    log(f'go-tpc loaded {args.warehouses} warehouses in {time.time() - t0:.0f} s')
    move_into_schemas()
    mysql("CREATE USER 'qag'@'%' IDENTIFIED BY 'pw'")
    for schema in SCHEMAS:
        mysql(f"GRANT SELECT ON {schema}.* TO 'qag'@'%'")
    mysql('CREATE DATABASE sbtest')
    mysql("GRANT SELECT ON sbtest.* TO 'qag'@'%'")
    prep = sh('docker', 'run', '--rm', '--cpuset-cpus', args.load_cpus, SYSBENCH_IMAGE, 'sysbench', 'oltp_read_only',
              f'--mysql-host={ip(MYSQL)}', '--mysql-user=root', '--mysql-password=pw', '--mysql-ssl=REQUIRED',
              '--tables=4',
              '--table-size=500000', '--threads=4', 'prepare')
    if prep.returncode:
        sys.exit(f'sysbench prepare: {prep.stderr[-2000:]}')
    export_builds()
    log('setup done; MySQL at', ip(MYSQL), mysql("SELECT ROUND(SUM(data_length + index_length) / 1024 / 1024) "
                                                "FROM information_schema.tables WHERE table_schema IN "
                                                f"({','.join(repr(s) for s in SCHEMAS)})"), 'MB in the six databases')


def move_into_schemas():
    """go-tpc writes every table into one database; a microservice estate has one per service. RENAME TABLE moves a
    table between databases on the same server without copying it."""
    for schema, tables in SCHEMAS.items():
        mysql(f'CREATE DATABASE IF NOT EXISTS {schema}')
        for table in tables:
            mysql(f'RENAME TABLE tpcc.{table} TO {schema}.{table}')
    # The payments service looks a customer's payments up by customer: TPC-C itself defines no index for it.
    mysql('CREATE INDEX idx_history_customer ON billing.history (h_c_w_id, h_c_d_id, h_c_id)')
    for schema, tables in SCHEMAS.items():
        mysql('ANALYZE TABLE ' + ', '.join(f'{schema}.{t}' for t in tables))
    mysql('DROP DATABASE tpcc')


def export_builds():
    for label, (ref, _) in BUILDS.items():
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


def b_teardown():
    stop()
    for name in (MYSQL, PG):
        sh('docker', 'rm', '-fv', name)  # -v: the image's data volume too
    log('containers removed')


# ---------------------------------------------------------------- request files

def zipf_sampler(n, s, rng):
    """Draws ranks 0..n-1 with P(rank k) proportional to 1 / (k+1)^s."""
    cum, total = [], 0.0
    for k in range(n):
        total += 1.0 / (k + 1) ** s
        cum.append(total)
    return lambda: bisect.bisect_left(cum, rng.random() * total)


def url_file(query, dist):
    """A file of request URLs for oha's --urls-from-file: 200,000 draws of a customer (or district), uniform or
    Zipf-distributed. A hot customer is a random one, not warehouse 1's first: ranks map through a fixed shuffle."""
    path = OUT / 'urls' / f'{query}_{dist}.txt'
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    rng = random.Random(f'{query}-{dist}-{args.seed}')
    kind = QUERIES[query.replace('_cached', '')][0]
    if kind == 'district':
        lines = [f'{U}/q/{query}?w={rng.randint(1, args.warehouses)}&d={rng.randint(1, DISTRICTS)}'
                 f'&threshold={rng.randint(10, 20)}' for _ in range(200_000)]
    else:
        people = args.warehouses * DISTRICTS * CUSTOMERS
        order = list(range(people))
        random.Random(f'shuffle-{args.seed}').shuffle(order)
        draw = zipf_sampler(people, args.zipf_s, rng) if dist == 'zipf' else (lambda: rng.randrange(people))

        def url(i):
            w, rest = divmod(i, DISTRICTS * CUSTOMERS)
            d, c = divmod(rest, CUSTOMERS)
            return f'{U}/q/{query}?w={w + 1}&d={d + 1}&c={c + 1}'
        lines = [url(order[draw()]) for _ in range(200_000)]
    path.write_text('\n'.join(lines) + '\n')
    return path


# ---------------------------------------------------------------- the gateway

def stop():
    global _server, _server_meta
    _server_meta = {}
    if _server is not None:
        try:
            os.killpg(_server.pid, signal.SIGTERM)
            _server.wait(timeout=20)
        except Exception:
            try:
                os.killpg(_server.pid, signal.SIGKILL)
            except Exception:
                pass
        _server = None
    for _ in range(50):
        if sh('bash', '-c', f'ss -ltn | grep -q ":{PORT} "').returncode:
            return
        time.sleep(0.2)


def start(build, workers=1, threads=8, store='sqlite', profile_to=None, access_log=True):
    global _server, _server_meta
    stop()
    ref, extra = BUILDS[build]
    export_builds()  # setup does this; a later --out reusing the same database does it here
    home = OUT / f'home-{store}'
    home.mkdir(exist_ok=True)
    env = {**os.environ, 'PYTHONPATH': str(OUT / 'builds' / build.split('+')[0]), 'QUERYAPIGATE_HOME': str(home),
           'QUERYAPIGATE_API_KEY': 'bench-admin', **extra}
    env.pop('QUERYAPIGATE_DATABASE_URL', None)
    if store == 'postgres':
        env['QUERYAPIGATE_DATABASE_URL'] = f'postgresql://postgres:pw@{ip(PG)}:5432/postgres'
    cmd = [str(Path(sys.executable).parent / 'gunicorn'), '--bind', f'127.0.0.1:{PORT}', '--workers', str(workers),
           '--worker-class', 'gthread', '--threads', str(threads), '--timeout', '120', '--no-control-socket',
           '-c', 'python:queryapigate.gunicorn_conf']
    if access_log:
        cmd += ['--access-logfile', str(OUT / 'access.log')]
    cmd += ['queryapigate.app:create_app()']
    if profile_to:
        cmd = [args.py_spy, 'record', '--subprocesses', '--rate', '200', '--format', 'raw', '-o', str(profile_to),
               '--'] + cmd
    cmd = ['taskset', '-c', args.gateway_cpus] + cmd
    out = open(OUT / 'server.log', 'a')
    _server = subprocess.Popen(cmd, env=env, cwd=home, stdout=out, stderr=out, start_new_session=True)
    for _ in range(150):
        try:
            urllib.request.urlopen(U + '/health', timeout=1)
            break
        except Exception:
            time.sleep(0.2)
    else:
        raise RuntimeError(f'gateway {build} did not start - see {OUT / "server.log"}')
    _server_meta = {'build': build, 'workers': workers, 'threads': threads, 'store': store,
                    'access_log': access_log}
    seed(store)


def gunicorn_master():
    pids = [int(p) for p in sh('pgrep', '-f', f'gunicorn --bind 127.0.0.1:{PORT}').stdout.split()]
    for p in pids:
        try:
            ppid = int(open(f'/proc/{p}/stat').read().rsplit(')', 1)[1].split()[1])
        except OSError:
            continue
        if ppid not in pids:
            return p
    return pids[0]


def api(method, path, body=None, key='bench-admin'):
    req = urllib.request.Request(U + path, method=method, data=None if body is None else json.dumps(body).encode(),
                                 headers={'X-API-Key': key, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req) as r:
        raw = r.read()
        return json.loads(raw) if raw[:1] in (b'{', b'[') else raw.decode()


_keys = {}  # store -> this process's reader key: kept in memory only, never written to disk


def seed(store):
    """The connection and the saved queries, once per store, and a reader key once per process - through the
    Management API. The key's secret is held in memory only; a later run against the same store makes its own."""
    if store in _keys:
        return
    try:
        api('GET', '/api/v1/connections/tpcc')
        seeded = True
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        seeded = False
    if not seeded:
        _seed_estate()
    _keys[store] = api('POST', '/api/v1/api-keys', {'name': f'reader-{os.getpid()}-{int(time.time())}',
                                                    'connections': [], 'queries': [*QUERIES, CACHED]})['secret']


def _seed_estate():
    api('POST', '/api/v1/connections', {'name': 'tpcc', 'db': 'mysql', 'host': ip(MYSQL), 'port': 3306,
                                        'database': 'crm', 'user': 'qag', 'password': 'pw', 'active': True})
    for name, (kind, desc, sql) in QUERIES.items():
        params = ({'w': {'type': 'int', 'required': True}, 'd': {'type': 'int', 'required': True},
                   'c': {'type': 'int', 'required': True}} if kind == 'user' else
                  {'w': {'type': 'int', 'required': True}, 'd': {'type': 'int', 'required': True},
                   'threshold': {'type': 'int', 'required': True}})
        body = {'name': name, 'connection_name': 'tpcc', 'sql': ' '.join(sql.split()), 'parameters': params,
                'description': desc, 'publish': True}
        api('POST', '/api/v1/queries', body)
        if name == 'order_status':
            api('POST', '/api/v1/queries', {**body, 'name': CACHED, 'cache_ttl': 30,
                                            'description': desc + ', cached for 30 s'})


def reader_key():
    return _keys[_server_meta.get('store', 'sqlite')]


# ---------------------------------------------------------------- load

def oha(urls, key=None, c=32, secs=30, q=None, correction=False):
    """oha from the load cores. ``urls`` is a --urls-from-file path, or one plain URL."""
    cmd = ['docker', 'run', '--rm', '--network', 'host', '--cpuset-cpus', args.load_cpus]
    if isinstance(urls, Path):
        cmd += ['-v', f'{urls.parent}:/urls:ro']
    cmd += [oltp.OHA, '--no-tui', '--output-format', 'json', '-z', f'{seconds(secs)}s', '-c', str(c)]
    if key:
        cmd += ['-H', f'X-API-Key: {key}']
    if q:
        cmd += ['-q', str(q)]
    if correction:
        cmd += ['--latency-correction']
    cmd += ['--urls-from-file', f'/urls/{urls.name}'] if isinstance(urls, Path) else [urls]
    d = json.loads(sh(*cmd).stdout)
    s, p = d['summary'], d['latencyPercentiles']
    codes = d.get('statusCodeDistribution', {})
    return {'rps': s['requestsPerSec'], 'success': s['successRate'], 'codes': codes, 'requests': sum(codes.values()),
            **{k: (v * 1000 if v is not None else None) for k, v in p.items()}}


def cache_counts():
    text = api('GET', '/metrics')
    found = {}
    for name in ('hits', 'misses'):
        m = re.search(rf'^queryapigate_cache_{name}_total\s+(\S+)', text, re.M)
        found[name] = float(m.group(1)) if m else 0.0
    return found


def measure(block, query, c=32, secs=30, q=None, correction=False, dist='uniform', warm=True, rnd=None, extra=None):
    urls, key = url_file(query, dist), reader_key()
    if warm:
        oha(urls, key, c=8, secs=5)
    master = gunicorn_master()
    freqs, done = [], threading.Event()

    def sample():
        while not done.wait(2):
            freqs.append(oltp.mhz())
    sampler = threading.Thread(target=sample, daemon=True)
    cache0 = cache_counts() if query == CACHED else None
    q0, g0, m0, l0, t0 = (questions(), oltp.tree_cpu_s(master), container_cpu_s(MYSQL),
                          oltp.cpus_busy_s(args.load_cpus), time.time())
    sampler.start()
    r = oha(urls, key, c=c, secs=secs, q=q, correction=correction)
    done.set()
    wall = time.time() - t0
    g1, m1, l1, q1 = (oltp.tree_cpu_s(master), container_cpu_s(MYSQL), oltp.cpus_busy_s(args.load_cpus),
                      questions())
    n = max(r['requests'], 1)
    row = {'block': block, 'scenario': query, 'dist': dist, 'c': c, 'q': q, 'seconds': seconds(secs), 'round': rnd,
           **_server_meta, **r, 'gw_cores': (g1 - g0) / wall, 'db_cores': (m1 - m0) / wall,
           'load_cores': (l1 - l0) / wall, 'mysql_statements_per_req': (q1 - q0 - 1) / n,
           'mhz_gw': round(sum(freqs) / len(freqs)) if freqs and all(freqs) else None, 'pkg_temp_c': oltp.pkg_temp(),
           **(extra or {})}
    if cache0 is not None:
        cache1 = cache_counts()
        hits, misses = cache1['hits'] - cache0['hits'], cache1['misses'] - cache0['misses']
        row['cache_hit_rate'] = hits / (hits + misses) if hits + misses else None
    record(row)
    log(f"{block} {row['build']} w{row['workers']} t{row['threads']} {row['store']} {query}/{dist} c={c} q={q} "
        f"r{rnd}: {r['rps']:.0f} req/s p50 {r['p50']:.2f} p99 {r['p99']:.2f} ok {r['success']:.4f} "
        f"gw {row['gw_cores']:.2f} db {row['db_cores']:.2f} stmts {row['mysql_statements_per_req']:.2f}"
        + (f" hits {row['cache_hit_rate']:.2f}" if row.get('cache_hit_rate') is not None else ''))
    return row


def floor(block, label):
    if _server_meta.get('build') != args.head_label or _server_meta.get('workers') != 1 \
            or _server_meta.get('store') != 'sqlite':
        start(args.head_label)
    r = oha(U + '/health', None, c=1, secs=10)
    record({'block': block, 'scenario': 'health_floor', 'label': label, 'rps': r['rps'], 'p50': r['p50'],
            **oltp.environment(), 'mhz_gw': oltp.mhz()})
    log(f'{block} floor {label}: /health {r["rps"]:.0f} req/s, {oltp.pkg_temp()} C')


def rotations(items, n):
    return [(r, items[r % len(items):] + items[:r % len(items)]) for r in range(rounds(n))]


# ---------------------------------------------------------------- direct, with sysbench

LUA = r"""
-- Each saved query, sent straight to MySQL with the same uniform random parameters: what a service gets without a
-- gateway. One statement per call, autocommit - as a service would send it.
sysbench.cmdline.options = {
  query = {"Which query", "customer"}, warehouses = {"Warehouses", 20},
}
local Q = {
%s
}
function thread_init() drv = sysbench.sql.driver(); con = drv:connect() end
function thread_done() con:disconnect() end
function event()
  local w = sysbench.rand.uniform(1, sysbench.opt.warehouses)
  local d = sysbench.rand.uniform(1, 10)
  local third = sysbench.opt.query == "stock_level" and sysbench.rand.uniform(10, 20) or sysbench.rand.uniform(1, 3000)
  con:query(string.format(Q[sysbench.opt.query], w, d, third))
end
"""


def lua_script():
    path = OUT / 'lua' / 'tpcc_direct.lua'
    path.parent.mkdir(parents=True, exist_ok=True)
    entries = []
    for name, (kind, _, sql) in QUERIES.items():
        third = ':threshold' if kind == 'district' else ':c'
        text = ' '.join(sql.split()).replace(':w', '%d').replace(':d', '%d').replace(third, '%d')
        # every :w/:d/:third in order of appearance - string.format needs the arguments in that order
        order = re.findall(r':w|:d|' + third, ' '.join(sql.split()))
        entries.append((name, text, order))
    body = []
    for name, text, order in entries:
        # Rewrite to string.format with positional arguments repeated as the SQL repeats them.
        args_lua = ', '.join({'w': 'w', 'd': 'd'}.get(o[1:], 'third') for o in order)
        body.append(f'  {name} = function(w, d, third) return string.format([[{text}]], {args_lua}) end,')
    script = LUA.replace('local Q = {\n%s\n}', 'local Q = {\n' + '\n'.join(body) + '\n}').replace(
        'con:query(string.format(Q[sysbench.opt.query], w, d, third))', 'con:query(Q[sysbench.opt.query](w, d, third))')
    path.write_text(script)
    return path


def sysbench(test, threads, secs, extra=()):
    """sysbench from the load cores against MySQL as the read-only user; returns tps and p50/p99 in ms."""
    cmd = ['docker', 'run', '--rm', '--cpuset-cpus', args.load_cpus]
    if test.endswith('.lua'):
        cmd += ['-v', f'{Path(test).parent}:/lua:ro']
        test = f'/lua/{Path(test).name}'
    # TLS, as the gateway's mysql-connector negotiates by default - and MySQL 8.4's caching_sha2_password needs it
    cmd += [SYSBENCH_IMAGE, 'sysbench', test, f'--mysql-host={ip(MYSQL)}', '--mysql-user=qag', '--mysql-password=pw',
            '--mysql-ssl=REQUIRED',
            f'--threads={threads}', f'--time={seconds(secs)}', '--histogram=on', '--report-interval=0', *extra, 'run']
    out = sh(*cmd)
    text = out.stdout
    tps = re.search(r'transactions:\s+\d+\s+\((\S+) per sec', text) or re.search(r'queries:\s+\d+\s+\((\S+) per', text)
    hist = [(float(v), int(n)) for v, n in re.findall(r'^\s+([\d.]+)\s+\|[*\s]*(\d+)\s*$', text, re.M)]
    total = sum(n for _, n in hist)

    def pct(p):
        acc = 0
        for v, n in hist:
            acc += n
            if acc >= p * total:
                return v
        return None
    if not tps:
        raise RuntimeError(f'sysbench: {text[-1500:]}{out.stderr[-1500:]}')
    return {'tps': float(tps.group(1)), 'p50': pct(0.5), 'p99': pct(0.99)}


# ---------------------------------------------------------------- blocks

def b_env():
    py = sys.executable

    def version(module):
        return sh(py, '-c', f'import importlib.metadata as m; print(m.version("{module}"))').stdout.strip()
    record({'block': 'env', 'scenario': 'env', **oltp.environment(), 'builds': list(BUILDS),
            'refs': {label: sh('git', '-C', str(REPO), 'rev-parse', f'{ref}^{{commit}}').stdout.strip()
                     for label, (ref, _) in BUILDS.items()},
            'python': sh(py, '-V').stdout.strip(), 'gunicorn': version('gunicorn'),
            'mysql_connector': version('mysql-connector-python'), 'mysql': mysql('SELECT VERSION()'),
            'warehouses': args.warehouses, 'customers': args.warehouses * DISTRICTS * CUSTOMERS,
            'zipf_s': args.zipf_s, 'oha': oltp.OHA, 'kernel': os.uname().release,
            'cpu': sh('bash', '-c', "lscpu | sed -n 's/^Model name: *//p'").stdout.strip(),
            'cpus': {'gateway': args.gateway_cpus, 'database': args.db_cpus, 'load': args.load_cpus},
            'tables': mysql("SELECT CONCAT(table_schema, '.', table_name, '=', table_rows) "
                            'FROM information_schema.tables '
                            f"WHERE table_schema IN ({','.join(repr(s) for s in SCHEMAS)}) ORDER BY 1").split('\n')})
    log('environment recorded')


def m1():
    """MySQL on its own: sysbench's standard oltp_read_only, then each saved query sent directly - one caller, and
    32 - with no gateway in the path."""
    floor('m1', 'start')
    for r in range(rounds(5)):
        for threads in (1, 32):
            res = sysbench('oltp_read_only', threads, 20, ['--tables=4', '--table-size=500000'])
            record({'block': 'm1', 'scenario': 'sysbench_oltp_read_only', 'c': threads, 'round': r, **res})
            log('m1 sysbench oltp_read_only', threads, res)
    lua = lua_script()
    for r in range(rounds(5)):
        for query in QUERIES:
            for threads in (1, 32):
                res = sysbench(str(lua), threads, 20, [f'--query={query}', f'--warehouses={args.warehouses}'])
                record({'block': 'm1', 'scenario': f'direct_{query}', 'query': query, 'c': threads, 'round': r, **res})
                log('m1 direct', query, threads, res)
    floor('m1', 'end')


def m2():
    """One worker, 32 callers: every query on every build."""
    floor('m2', 'start')
    for query in QUERIES:
        for r, order in rotations(list(BUILDS), 5):
            for build in order:
                start(build)
                measure('m2', query, c=32, rnd=r)
    floor('m2', 'end')


def m3():
    """One caller at a time: the latency of a single call, three representative queries, every build."""
    floor('m3', 'start')
    for query in ('customer', 'order_status', 'stock_level'):
        for r, order in rotations(list(BUILDS), 5):
            for build in order:
                start(build)
                measure('m3', query, c=1, rnd=r)
    floor('m3', 'end')


def m4():
    """Open loop on order_status: capacity first (5 closed-loop rounds), then 25-110% of it, latency corrected."""
    floor('m4', 'start')
    builds = [args.head_label, args.head_label + '+cache']
    caps = collections.defaultdict(list)
    for r, order in rotations(builds, 5):
        for build in order:
            start(build)
            caps[build].append(measure('m4cap', 'order_status', c=32, rnd=r)['rps'])
    caps = {b: statistics.median(v) for b, v in caps.items()}
    fracs = (0.25, 0.5, 0.75, 0.9, 1.0, 1.1)
    for r, order in rotations(builds, 5):
        for build in order:
            start(build)
            for frac in fracs:
                measure('m4', 'order_status', c=128, secs=20, q=int(caps[build] * frac), correction=True, rnd=r,
                        warm=(frac == fracs[0]), extra={'capacity': caps[build], 'offered_frac': frac})
    floor('m4', 'end')


def m5():
    """Scale-out: 1, 2, 4 and 8 workers, 64 callers, on a light and a heavier query."""
    floor('m5', 'start')
    for workers in (1, 2, 4, 8):
        for query in ('order_status', 'stock_level'):
            for r, order in rotations([args.head_label, args.head_label + '+cache'], 5):
                for build in order:
                    start(build, workers=workers)
                    measure('m5', query, c=64, rnd=r)
    floor('m5', 'end')


def m6():
    """Threads per worker, 1 to 8, on a fast query and the heaviest."""
    floor('m6', 'start')
    for query in ('customer', 'stock_level'):
        for r, order in rotations([1, 2, 4, 8], 5):
            for threads in order:
                start(args.head_label, threads=threads)
                measure('m6', query, c=32, rnd=r)
    floor('m6', 'end')


def m7():
    """Skew: uniform against Zipf-distributed customers, with and without a 30 s response cache (cache_ttl)."""
    floor('m7', 'start')
    cells = [(dist, query) for dist in ('uniform', 'zipf') for query in ('order_status', CACHED)]
    for r, order in rotations(cells, 5):
        for dist, query in order:
            start(args.head_label)  # a fresh process: an empty response cache every run
            measure('m7', query, c=32, dist=dist, rnd=r)
    floor('m7', 'end')


def m8():
    """A database across a network: 0.5 ms netem on MySQL - every build, one caller and 32 - then the store on SQLite
    (local) against PostgreSQL (also 0.5 ms away), with and without the metadata cache."""
    floor('m8', 'start')
    netem(MYSQL, '0.5ms')
    try:
        for c in (1, 32):
            for r, order in rotations(list(BUILDS), 5):
                for build in order:
                    start(build)
                    measure('m8', 'order_status', c=c, rnd=r, extra={'netem': '0.5ms'})
        sh('docker', 'rm', '-fv', PG)
        _keys.pop('postgres', None)  # a new, empty store: seed it again
        sh('docker', 'run', '-d', '--name', PG, '--cpuset-cpus', args.db_cpus, '-e', 'POSTGRES_PASSWORD=pw', PG_IMAGE)
        for _ in range(60):
            if sh('docker', 'exec', PG, 'pg_isready', '-U', 'postgres').returncode == 0:
                break
            time.sleep(1)
        time.sleep(2)
        netem(PG, '0.5ms')
        cells = [(store, build) for store in ('sqlite', 'postgres') for build in (args.head_label,
                                                                                    args.head_label + '+cache')]
        for r, order in rotations(cells, 5):
            for store, build in order:
                start(build, store=store)
                measure('m8store', 'order_status', c=1, rnd=r, extra={'netem': '0.5ms'})
    finally:
        netem(MYSQL, '0ms')
        stop()
        sh('docker', 'rm', '-fv', PG)
    floor('m8', 'end')


PROBE = r"""
import random, sys, time
import mysql.connector
conn = mysql.connector.connect(host=sys.argv[1], user='qag', password='pw', database='crm')
mode, secs, warehouses = sys.argv[2], float(sys.argv[3]), int(sys.argv[4])
conn.autocommit = mode == 'one'
cur = conn.cursor()
n, start = 0, time.perf_counter()
while time.perf_counter() < start + secs:
    cur.execute('SELECT c_id, c_last FROM crm.customer WHERE c_w_id = %s AND c_d_id = %s AND c_id = %s',
                (random.randint(1, warehouses), random.randint(1, 10), random.randint(1, 3000)))
    cur.fetchall()
    if mode == 'two':  # the query and the ROLLBACK a pooled connection gets on its way back
        conn.rollback()
    n += 1
print((time.perf_counter() - start) / n * 1000)
"""


def m9():
    """Round trips per call: counted in MySQL's general log, and measured as the slope of single-call latency against
    netem delay - next to mysql-connector alone sending one statement, and the two a pooled query sends."""
    floor('m9', 'start')
    mysql("SET GLOBAL log_output = 'TABLE'")
    for build in BUILDS:
        start(build)
        key, urls = reader_key(), url_file('customer', 'uniform')
        lines = urls.read_text().splitlines()
        for line in lines[:30]:  # warm: the pool, the timeout already set
            urllib.request.urlopen(urllib.request.Request(line, headers={'X-API-Key': key})).read()
        mysql('TRUNCATE TABLE mysql.general_log')
        mysql("SET GLOBAL general_log = 'ON'")
        for line in lines[30:130]:
            urllib.request.urlopen(urllib.request.Request(line, headers={'X-API-Key': key})).read()
        mysql("SET GLOBAL general_log = 'OFF'")
        rows = mysql("SELECT LEFT(REGEXP_REPLACE(CONVERT(argument USING utf8mb4), '[0-9]+', 'N'), 70), COUNT(*) "
                     "FROM mysql.general_log WHERE command_type = 'Query' AND user_host LIKE 'qag%' "
                     "GROUP BY 1 ORDER BY 2 DESC")
        counted = [line.rsplit('\t', 1) for line in rows.splitlines() if line]
        record({'block': 'm9', 'scenario': 'statements', 'build': build, 'calls': 100,
                'statements': {stmt: int(n) for stmt, n in counted}, 'per_call': sum(int(n) for _, n in counted) / 100})
        log('m9 statements', build, sum(int(n) for _, n in counted) / 100, counted[:4])
    stop()
    builds = list(BUILDS)
    try:
        for r, order in rotations(['0ms', '0.5ms', '1ms', '2ms'], 3):
            for delay in order:
                netem(MYSQL, delay)
                for mode in ('one', 'two'):
                    out = sh('taskset', '-c', args.gateway_cpus, sys.executable, '-c', PROBE, ip(MYSQL), mode,
                             str(seconds(10)), str(args.warehouses))
                    record({'block': 'm9', 'scenario': f'probe_{mode}', 'netem': delay, 'round': r,
                            'ms_per_call': float(out.stdout.strip())})
                for build in builds[r % len(builds):] + builds[:r % len(builds)]:
                    start(build)
                    measure('m9', 'customer', c=1, secs=20, rnd=r, extra={'netem': delay})
    finally:
        netem(MYSQL, '0ms')
    floor('m9', 'end')


def m10():
    """Where the time goes: py-spy on one worker of the head build, order_status at 32 callers."""
    if not args.py_spy:
        log('m10 skipped: py-spy not found (pip install py-spy, or --py-spy PATH)')
        return
    for build in (args.head_label, args.head_label + '+cache'):
        out = OUT / f'profile_{build}.txt'
        start(build, profile_to=out)
        measure('m10', 'order_status', c=32, extra={'profiled': True})
        os.kill(_server.pid, signal.SIGINT)  # py-spy (taskset exec'd into it): stop sampling and write
        for _ in range(100):
            if out.exists() and out.stat().st_size and _server.poll() is not None:
                break
            time.sleep(0.3)
        stop()
        log('m10 profile', out)


def m11():
    """Not performance: whether a table allow-list tells two databases' same-named tables apart on MySQL. A key may
    read `customer`; `billing.customer` is a decoy table of that name in another service's database."""
    start(args.head_label)
    mysql('CREATE TABLE billing.customer LIKE crm.customer')
    try:
        secret = api('POST', '/api/v1/api-keys', {'name': f'scoped-{int(time.time())}', 'connections': ['tpcc'],
                                                  'allowed_tables': ['customer']})['secret']
        for sql in ('SELECT COUNT(*) AS n FROM crm.customer', 'SELECT COUNT(*) AS n FROM billing.customer',
                    'SELECT COUNT(*) AS n FROM billing.history'):
            req = urllib.request.Request(U + '/execute_sql', method='POST', data=json.dumps(
                {'sql': sql, 'connection_name': 'tpcc'}).encode(), headers={'X-API-Key': secret,
                                                                           'Content-Type': 'application/json'})
            try:
                with urllib.request.urlopen(req) as res:
                    status, code = res.status, None
            except urllib.error.HTTPError as error:
                status, code = error.code, json.loads(error.read() or b'{}').get('code')
            record({'block': 'm11', 'scenario': 'allow_list', 'build': args.head_label, 'sql': sql, 'status': status,
                    'code': code})
            log('m11', sql, status, code)
    finally:
        mysql('DROP TABLE billing.customer')


BLOCKS = {'setup': b_setup, 'teardown': b_teardown, 'env': b_env, 'm1': m1, 'm2': m2, 'm3': m3, 'm4': m4, 'm5': m5,
          'm6': m6, 'm7': m7, 'm8': m8, 'm9': m9, 'm10': m10, 'm11': m11}
SESSIONS = {'A': ['env', 'm1', 'm2', 'm3'], 'B': ['env', 'm4', 'm5', 'm6', 'm7', 'm8', 'm9', 'm10', 'm11', 'env']}


def main():
    global args, OUT, BUILDS
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('blocks', nargs='+', help=f"any of {', '.join(BLOCKS)}, or the sessions A and B")
    parser.add_argument('--out', default='/tmp/qag-tpcc-bench', help='results, builds and logs (default %(default)s)')
    parser.add_argument('--refs', default='v0.41.0,v0.42.0,origin/main',
                        help='git refs to compare, oldest first; the last is the head (default %(default)s)')
    parser.add_argument('--warehouses', type=int, default=20, help='TPC-C warehouses (default %(default)s)')
    parser.add_argument('--zipf-s', type=float, default=1.0, help='Zipf exponent for skewed customers (m7)')
    parser.add_argument('--seed', default='qag', help='seed for the request files')
    parser.add_argument('--go-tpc', default=shutil.which('go-tpc'), help='the go-tpc binary, for setup')
    parser.add_argument('--gateway-cpus', default='0-7')
    parser.add_argument('--db-cpus', default='8-11')
    parser.add_argument('--load-cpus', default='12-19')
    parser.add_argument('--py-spy', default=shutil.which('py-spy'))
    parser.add_argument('--smoke', action='store_true', help='2 s runs, one round: checks the mechanics only')
    args = parser.parse_args()
    OUT = Path(args.out)
    if args.smoke:
        OUT = OUT / 'smoke'
    OUT.mkdir(parents=True, exist_ok=True)
    refs = args.refs.split(',')
    labels = [r.split('/')[-1] for r in refs]
    args.head_label = labels[-1]
    BUILDS = {label: (ref, {}) for label, ref in zip(labels, refs, strict=True)}
    BUILDS[args.head_label + '+cache'] = (refs[-1], {'QUERYAPIGATE_METADATA_CACHE_TTL': '5'})
    oltp.args = args  # oltp's helpers read the CPU lists and --smoke from here
    names = [n for b in args.blocks for n in SESSIONS.get(b, [b])]
    try:
        for name in names:
            log('=== block', name)
            BLOCKS[name]()
    finally:
        stop()
    log('done')


if __name__ == '__main__':
    main()

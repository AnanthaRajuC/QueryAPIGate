"""Shared plumbing for every benchmarks/*.py script: a real queryapigate subprocess hit over real HTTP (not
the Flask test client - see benchmarks/README.md for why), native driver connections for fast seeding, and
the small pieces of orchestration (connection file, dialect env var) every benchmark needs regardless of
which question it answers. Split out of run.py once a second and third benchmark (BACKLOG #25) needed the
same plumbing rather than a copy of it.
"""
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SAMPLE_INTERVAL = 0.2  # seconds between RSS samples while a request is in flight


# --------------------------------------------------------------------------------------
# Native connections, for seeding only - the benchmark itself always goes through the
# real HTTP API. See run.py's own module docstring for why seeding bypasses it.
# --------------------------------------------------------------------------------------

def native_connect(dialect, details):
    if dialect == 'mysql':
        import mysql.connector
        return mysql.connector.connect(host=details['host'], port=details.get('port', 3306),
                                       user=details['user'], password=details.get('password', ''),
                                       database=details['database'])
    if dialect == 'postgres':
        import psycopg2
        return psycopg2.connect(host=details['host'], port=details.get('port', 5432), user=details['user'],
                                password=details.get('password', ''), dbname=details['database'])
    if dialect == 'clickhouse':
        from clickhouse_driver import Client
        return Client(host=details['host'], port=details.get('port', 9000), user=details.get('user', 'default'),
                     password=details.get('password', ''), database=details.get('database', 'default'))
    if dialect == 'h2':
        import jaydebeapi  # noqa: I001

        from queryapigate import config
        host = details['host']
        if details.get('port'):
            host = f"{host}:{details['port']}"
        url = f"jdbc:h2:tcp://{host}/~/{details['database']}"
        return jaydebeapi.connect('org.h2.Driver', url, [details.get('user', 'SA'), details.get('password', '')],
                                  [config.h2_jar()])
    raise ValueError(f'native_connect() does not handle {dialect!r} (file-based dialects are seeded directly)')


def native_close(dialect, conn):
    if dialect == 'clickhouse':
        conn.disconnect()
    else:
        conn.close()


# --------------------------------------------------------------------------------------
# The queryapigate server under test: a real subprocess, hit over real HTTP, same as a user
# would - not the Flask test client, which does not exercise a real WSGI response cycle
# or a real separate OS process to sample memory from.
# --------------------------------------------------------------------------------------

class Server:
    def __init__(self, home, port, max_page_size=None, pool_size=None):
        self.port = port
        env = {**os.environ, 'QUERYAPIGATE_HOME': str(home)}
        env.pop('QUERYAPIGATE_API_KEY', None)
        if max_page_size is not None:
            env['QUERYAPIGATE_MAX_PAGE_SIZE'] = str(max_page_size)
        if pool_size is not None:
            env['QUERYAPIGATE_POOL_SIZE'] = str(pool_size)
        self.proc = subprocess.Popen(
            [sys.executable, '-m', 'queryapigate', 'serve', '--port', str(port), '--host', '127.0.0.1'],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._wait_healthy()

    def _wait_healthy(self):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                urllib.request.urlopen(f'http://127.0.0.1:{self.port}/health', timeout=1)
                return
            except (urllib.error.URLError, ConnectionError):
                time.sleep(0.2)
        raise RuntimeError('queryapigate server did not become healthy in time')

    def rss_mb(self):
        with open(f'/proc/{self.proc.pid}/status') as f:
            for line in f:
                if line.startswith('VmRSS:'):
                    return int(line.split()[1]) / 1024
        return None

    def get(self, path):
        """A plain GET against this server, returning (status, headers, body_bytes)."""
        req = urllib.request.Request(f'http://127.0.0.1:{self.port}{path}')
        try:
            with urllib.request.urlopen(req, timeout=60) as res:
                return res.status, dict(res.headers), res.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def metrics_text(self):
        return self.get('/metrics')[2].decode()

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()


class Sampler:
    """Samples the server's RSS on a background thread while one request is in flight."""

    def __init__(self, server):
        self.server = server
        self.samples = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self._stop.is_set():
            rss = self.server.rss_mb()
            if rss is not None:
                self.samples.append(rss)
            self._stop.wait(SAMPLE_INTERVAL)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(timeout=2)


# --------------------------------------------------------------------------------------
# Orchestration shared by every benchmark: which dialect, which connection details, where
# the temp home/connections file live.
# --------------------------------------------------------------------------------------

def get_connection_details(dialect):
    if dialect in ('sqlite', 'duckdb'):
        return None
    raw = os.environ.get(f'QUERYAPIGATE_IT_{dialect.upper()}')
    if not raw:
        raise SystemExit(f'QUERYAPIGATE_IT_{dialect.upper()} is not set - see benchmarks/README.md')
    return json.loads(raw)


def build_connections_file(home, dialect, details, file_path=None, name='bench'):
    if dialect == 'sqlite':
        entry = {'db': 'sqlite', 'database': file_path, 'active': True}
    elif dialect == 'duckdb':
        entry = {'db': 'duckdb', 'database': file_path, 'active': True}
    else:
        entry = {'db': dialect, 'host': details['host'], 'port': details.get('port'), 'user': details.get('user'),
                 'password': details.get('password', ''), 'database': details['database'], 'active': True}
    conn_file = Path(home) / 'db_connections.json'
    existing = json.loads(conn_file.read_text()) if conn_file.exists() else {'connections': {}}
    existing['connections'][name] = entry
    conn_file.write_text(json.dumps(existing))


def save_query(server, filename, sql_query, connection_name='bench', cache_ttl=None, query_parameters=None):
    """Creates a saved query - or adds a version to one - published at once, over the real Management API
    (POST /api/v1/queries), not a direct store.py call, so this benchmark exercises the exact same code path a saved
    query normally goes through (parameter validation, versioning) rather than a shortcut."""
    body = {
        'description': 'benchmarks/README.md scratch query', 'author': 'benchmarks', 'sql': sql_query,
        'connection_name': connection_name, 'parameters': query_parameters or {}, 'publish': True,
    }
    if cache_ttl is not None:
        body['cache_ttl'] = cache_ttl

    def post(path, payload):
        req = urllib.request.Request(f'http://127.0.0.1:{server.port}{path}', data=json.dumps(payload).encode(),
                                     headers={'Content-Type': 'application/json'}, method='POST')
        with urllib.request.urlopen(req, timeout=30) as res:
            if res.status != 201:
                raise RuntimeError(f'save_query failed: HTTP {res.status}')

    try:
        post('/api/v1/queries', {'name': filename, **body})
    except urllib.error.HTTPError as error:
        if error.code != 409:  # 409: it exists already - add a version instead
            raise
        post(f'/api/v1/queries/{urllib.parse.quote(filename)}/versions', body)

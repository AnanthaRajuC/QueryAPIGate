"""Write an upgrade fixture: a store created by a *released* QueryAPIGate, through that release's own API (BACKLOG #65).

    python tests/fixtures/stores/generate.py /path/to/<venv of queryapigate==X.Y.Z>/bin/queryapigate
    python tests/fixtures/stores/generate.py --postgres postgresql://... /path/to/.../bin/queryapigate

Starts that version's server on a throwaway home, creates the same small, realistic set of things through its own
HTTP API - a connection, a saved query with two versions, a run of it, a scoped API key, a role - stops it, and packs
the home as tests/fixtures/stores/<version>.tar.gz with an expected.json naming what was created. test_upgrades.py
starts the current code on each fixture and checks it all reads back. Each release adds its own fixture; a version
whose API lacks a step (roles before they existed) records the step as skipped instead of failing. From 0.13 the
steps go through the Management API (/api/v1), the only management interface since.

Standard library only: it runs under the released version's own interpreter, or any Python 3.9+.

--postgres URL (0.12.0 and later, which have the PostgreSQL store): the release runs on schema qag_fixture of that
database instead - dropped and recreated first, so use a scratch database - and the fixture is
<version>.postgres.tar.gz: that schema as plain SQL (store.sql, from pg_dump on PATH) beside data.db. This mode
needs psycopg2 in the interpreter running it, and the release installed with its postgres extra.

--run-store URL (0.45.0 and later, which have the ClickHouse run store - ADR 0029): the release keeps its runs and
events in database qag_fixture of that ClickHouse (an http://user:password@host:port URL; dropped and recreated
first, so use a scratch server), with the metadata store in SQLite as usual, and the fixture is
<version>.runstore.tar.gz: the home, plus runstore/ - each table's DDL in creation order (tables.json, the views last)
and its rows in ClickHouse's Native format, the one every version reads back the same. Its estate adds an ad-hoc run
before the saved one, so the hourly rollups hold a statement (which 0.46 stopped keeping as text). Generate these on
the oldest ClickHouse CI tests (24.8), so the DDL loads on every newer one.
"""
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ADMIN = 'fixture-admin-key'


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def call(base, method, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={'X-API-Key': ADMIN, 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            return res.status, res.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def V1_STEPS(data):
    """The same estate through the Management API, for 0.13 and later."""
    rule = {'rating': {'type': 'str', 'required': True}}
    return [
        ('connection', 'POST', '/api/v1/connections', {'name': 'films', 'db': 'sqlite', 'database': data,
                                                       'active': True}),
        ('query v1', 'POST', '/api/v1/queries', {
            'name': 'films_by_rating', 'sql': 'SELECT title FROM film WHERE rating = :rating', 'parameters': rule,
            'connection_name': 'films', 'author': 'fixture', 'description': 'Films by rating', 'publish': True}),
        ('query v2', 'POST', '/api/v1/queries/films_by_rating/versions', {
            'sql': 'SELECT title FROM film WHERE rating = :rating ORDER BY title', 'parameters': rule,
            'connection_name': 'films', 'author': 'fixture', 'description': 'Films by rating, sorted',
            'publish': True}),
        ('run', 'GET', '/q/films_by_rating?rating=PG', None),
        ('key', 'POST', '/api/v1/api-keys', {'name': 'reporting', 'connections': ['films'], 'allow_writes': False}),
        ('role', 'POST', '/api/v1/roles', {'name': 'analyst', 'connections': ['films']}),
    ]


PG_SCHEMA = 'qag_fixture'


def _pg(url, statement):
    import psycopg2  # --postgres only
    conn = psycopg2.connect(url)
    conn.autocommit = True
    try:
        conn.cursor().execute(statement)
    finally:
        conn.close()


RUN_STORE_DB = 'qag_fixture'


def _ch(url, sql, body=None):
    """One statement to ClickHouse's HTTP interface (--run-store only), standard library only: its response's bytes.
    ``body``, when given, is the data of an INSERT named in ``sql``."""
    parts = urllib.parse.urlsplit(url)
    target = f'{parts.scheme}://{parts.hostname}:{parts.port or 8123}/?' + urllib.parse.urlencode(
        {'query': sql} if body is not None else {})
    req = urllib.request.Request(target, data=body if body is not None else sql.encode(), method='POST', headers={
        'X-ClickHouse-User': urllib.parse.unquote(parts.username or 'default'),
        'X-ClickHouse-Key': urllib.parse.unquote(parts.password or '')})
    with urllib.request.urlopen(req, timeout=60) as res:
        return res.read()


def dump_run_store(url, home):
    """The release's run store as runstore/ in the home: tables.json, each table's DDL in an order that creates
    them (tables, then the materialized views that write to them), and <table>.native, its rows."""
    out = os.path.join(home, 'runstore')
    os.makedirs(out)
    listed = _ch(url, f"SELECT name, engine FROM system.tables WHERE database = '{RUN_STORE_DB}' "
                      "ORDER BY engine = 'MaterializedView', name FORMAT JSONEachRow")
    tables, rows = [], {}
    for line in listed.splitlines():
        table = json.loads(line)
        table['ddl'] = _ch(url, f'SHOW CREATE TABLE {RUN_STORE_DB}.{table["name"]} FORMAT TSVRaw').decode().strip()
        if table['engine'] != 'MaterializedView':
            rows[table['name']] = int(_ch(url, f'SELECT count() FROM {RUN_STORE_DB}.{table["name"]}'))
            with open(os.path.join(out, f'{table["name"]}.native'), 'wb') as f:
                f.write(_ch(url, f'SELECT * FROM {RUN_STORE_DB}.{table["name"]} FORMAT Native'))
        tables.append(table)
    with open(os.path.join(out, 'tables.json'), 'w') as f:
        json.dump(tables, f, indent=2)
        f.write('\n')
    schema = _ch(url, f'SELECT max(version) FROM {RUN_STORE_DB}.run_store_schema').decode().strip()
    return {'schema_version': int(schema), 'rows': rows}


def main(binary, postgres=None, run_store=None):
    if os.sep in binary:  # a path: the server runs from the throwaway home, so make it absolute
        binary = os.path.abspath(binary)
    version = subprocess.run([binary, '--version'], capture_output=True, text=True).stdout.split()[-1]
    home = tempfile.mkdtemp(prefix=f'qag-fixture-{version}-')
    data = os.path.join(home, 'data.db')
    conn = sqlite3.connect(data)
    conn.execute('CREATE TABLE film (film_id INTEGER, title TEXT, rating TEXT)')
    conn.executemany('INSERT INTO film VALUES (?, ?, ?)', [(1, 'Alpha', 'PG'), (2, 'Beta', 'R'), (3, 'Gamma', 'PG')])
    conn.commit()
    conn.close()
    port = free_port()
    env = {**os.environ, 'QUERYAPIGATE_HOME': home, 'QUERYAPIGATE_API_KEY': ADMIN, 'QUERYAPIGATE_PORT': str(port)}
    env.pop('QUERYAPIGATE_DATABASE_URL', None)
    if postgres:
        _pg(postgres, f'DROP SCHEMA IF EXISTS {PG_SCHEMA} CASCADE; CREATE SCHEMA {PG_SCHEMA}')
        env['QUERYAPIGATE_DATABASE_URL'] = (postgres + ('&' if '?' in postgres else '?')
                                            + f'options=-csearch_path%3D{PG_SCHEMA}')
    if run_store:
        _ch(run_store, f'DROP DATABASE IF EXISTS {RUN_STORE_DB} SYNC')
        env['QUERYAPIGATE_RUN_STORE_URL'] = run_store.rstrip('/') + '/' + RUN_STORE_DB
    server = subprocess.Popen([binary, 'serve'], env=env, cwd=home, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    base = f'http://127.0.0.1:{port}'
    expected = {'version': version, 'created': {}, 'skipped': []}
    try:
        for _ in range(100):
            try:
                urllib.request.urlopen(base + '/health', timeout=1)
                break
            except OSError:
                time.sleep(0.2)
        v1 = call(base, 'GET', '/api/v1/connections')[0] == 200  # 0.13 removed the unversioned routes below
        steps = V1_STEPS(data) if v1 else [
            ('connection', 'PATCH', '/connections', {'connections': {'films': {
                'db': 'sqlite', 'database': data, 'active': True}}}),
            ('query v1', 'PATCH', '/save_sql_to_file', {
                'filename': 'films_by_rating', 'sql_query': 'SELECT title FROM film WHERE rating = :rating',
                'query_parameters': {'rating': 'str'}, 'connection_name': 'films', 'author': 'fixture',
                'description': 'Films by rating'}),
            ('query v2', 'PATCH', '/save_sql_to_file', {
                'filename': 'films_by_rating',
                'sql_query': 'SELECT title FROM film WHERE rating = :rating ORDER BY title',
                'query_parameters': {'rating': 'str'}, 'connection_name': 'films', 'author': 'fixture',
                'description': 'Films by rating, sorted'}),
            ('run', 'GET', '/q/films_by_rating?rating=PG', None),
            ('key', 'POST', '/api_keys', {'name': 'reporting', 'connections': ['films'], 'allow_writes': False}),
            ('role', 'POST', '/roles', {'name': 'analyst', 'connections': ['films']}),
        ]
        if run_store:  # an ad-hoc run before the saved one, which stays the newest event
            steps.insert(1, ('ad-hoc run', 'POST', '/execute_sql', {
                'sql': "SELECT title FROM film WHERE rating = 'R'", 'connection_name': 'films'}))
        for step, method, path, body in steps:
            status, payload = call(base, method, path, body)
            if 200 <= status < 300:
                expected['created'][step] = True
            else:
                expected['skipped'].append(f'{step}: HTTP {status} {payload[:120].decode(errors="replace")}')
        time.sleep(1.5)  # let a batched history write land
    finally:
        server.terminate()
        server.wait(timeout=30)
    if postgres:  # the store as plain SQL: readable, and restorable into any PostgreSQL version
        dump = subprocess.run(['pg_dump', '--format=plain', f'--schema={PG_SCHEMA}', '--no-owner', '--no-privileges',
                               '--inserts', postgres], check=True, capture_output=True).stdout
        with open(os.path.join(home, 'store.sql'), 'wb') as f:
            f.write(dump)
        _pg(postgres, f'DROP SCHEMA {PG_SCHEMA} CASCADE')
    if run_store:
        expected['run_store'] = dump_run_store(run_store, home)
        _ch(run_store, f'DROP DATABASE {RUN_STORE_DB} SYNC')
    store = os.path.join(home, 'queryapigate.db')
    if os.path.exists(store):  # fold the write-ahead log into the file itself, so the fixture is one file
        conn = sqlite3.connect(store)
        conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        conn.close()
    os.makedirs(HERE, exist_ok=True)
    fixture = f'{version}.postgres' if postgres else f'{version}.runstore' if run_store else version
    out = os.path.join(HERE, f'{fixture}.tar.gz')
    with tarfile.open(out, 'w:gz') as tar:
        for name in sorted(os.listdir(home)):
            if name.endswith(('-wal', '-shm')):
                continue
            tar.add(os.path.join(home, name), arcname=name)
    expected['data_db_path'] = data  # the connection's absolute path in the fixture's own home
    with open(os.path.join(HERE, f'{fixture}.expected.json'), 'w') as f:
        json.dump(expected, f, indent=2, sort_keys=True)
        f.write('\n')
    print(version, out, json.dumps(expected))


if __name__ == '__main__':
    args = sys.argv[1:]
    options = {}
    while args[:1] in (['--postgres'], ['--run-store']):
        options[args[0][2:].replace('-', '_')], args = args[1], args[2:]
    main(args[0], **options)

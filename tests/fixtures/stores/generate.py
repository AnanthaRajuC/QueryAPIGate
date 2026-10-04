"""Write an upgrade fixture: a store created by a *released* QueryAPIGate, through that release's own API (BACKLOG #65).

    python tests/fixtures/stores/generate.py /path/to/<venv of queryapigate==X.Y.Z>/bin/queryapigate

Starts that version's server on a throwaway home, creates the same small, realistic set of things through its own
HTTP API - a connection, a saved query with two versions, a run of it, a scoped API key, a role - stops it, and packs
the home as tests/fixtures/stores/<version>.tar.gz with an expected.json naming what was created. test_upgrades.py
starts the current code on each fixture and checks it all reads back. Each release adds its own fixture; a version
whose API lacks a step (roles before they existed) records the step as skipped instead of failing.

Standard library only: it runs under the released version's own interpreter, or any Python 3.9+.
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


def main(binary):
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
        steps = [
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
        for step, method, path, body in steps:
            status, payload = call(base, method, path, body)
            if status == 200:
                expected['created'][step] = True
            else:
                expected['skipped'].append(f'{step}: HTTP {status} {payload[:120].decode(errors="replace")}')
        time.sleep(1.5)  # let a batched history write land
    finally:
        server.terminate()
        server.wait(timeout=30)
    store = os.path.join(home, 'queryapigate.db')
    if os.path.exists(store):  # fold the write-ahead log into the file itself, so the fixture is one file
        conn = sqlite3.connect(store)
        conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        conn.close()
    os.makedirs(HERE, exist_ok=True)
    out = os.path.join(HERE, f'{version}.tar.gz')
    with tarfile.open(out, 'w:gz') as tar:
        for name in sorted(os.listdir(home)):
            if name.endswith(('-wal', '-shm')):
                continue
            tar.add(os.path.join(home, name), arcname=name)
    expected['data_db_path'] = data  # the connection's absolute path in the fixture's own home
    with open(os.path.join(HERE, f'{version}.expected.json'), 'w') as f:
        json.dump(expected, f, indent=2, sort_keys=True)
        f.write('\n')
    print(version, out, json.dumps(expected))


if __name__ == '__main__':
    main(sys.argv[1])

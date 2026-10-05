"""A rolling upgrade on a shared PostgreSQL store (BACKLOG #58): instances are replaced one at a time, so for a while
the previous release keeps serving against a store the new one has already upgraded.

The policy (DEPLOYMENT.md, Rolling upgrades) that makes this work: store changes within 1.x are additive - new tables,
new nullable columns - so the previous minor release's code keeps working on an upgraded store until it is replaced.
What it can't do is start: a release refuses a store a newer one has upgraded (db._refuse_newer), so a node that
restarts mid-upgrade must come back on the new version.

This runs the real previous release, installed from PyPI - QUERYAPIGATE_TEST_OLD_BIN is its `queryapigate` - beside
the current code, on one store. Runs when that and QUERYAPIGATE_TEST_DATABASE_URL are set (CI's PostgreSQL job)."""
import os
import socket
import subprocess
import sys
import tempfile
import unittest
import uuid

from tests.test_multinode import ADMIN, call, eventually

DATABASE_URL = os.environ.get('QUERYAPIGATE_TEST_DATABASE_URL')
OLD_BIN = os.environ.get('QUERYAPIGATE_TEST_OLD_BIN')


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


@unittest.skipUnless(DATABASE_URL and OLD_BIN, 'set QUERYAPIGATE_TEST_DATABASE_URL and QUERYAPIGATE_TEST_OLD_BIN')
class RollingUpgradeTests(unittest.TestCase):
    def setUp(self):
        import psycopg2
        self.schema = 'qag_rolling_' + uuid.uuid4().hex[:12]
        conn = psycopg2.connect(DATABASE_URL)
        conn.autocommit = True
        conn.cursor().execute(f'CREATE SCHEMA {self.schema}')
        conn.close()
        self.tmp = tempfile.TemporaryDirectory()
        separator = '&' if '?' in DATABASE_URL else '?'
        self.env = {**os.environ, 'QUERYAPIGATE_API_KEY': ADMIN, 'QUERYAPIGATE_HISTORY_FLUSH_INTERVAL': '0.1',
                    'QUERYAPIGATE_DATABASE_URL': f'{DATABASE_URL}{separator}options=-csearch_path%3D{self.schema}'}
        for name in ('QUERYAPIGATE_TEST_DATABASE_URL', 'QUERYAPIGATE_REDIS_URL'):
            self.env.pop(name, None)
        self.processes = []
        self.data = os.path.join(self.tmp.name, 'data.db')
        import sqlite3
        db = sqlite3.connect(self.data)
        db.execute('CREATE TABLE t (n INTEGER)')
        db.execute('INSERT INTO t VALUES (1), (2)')
        db.commit()
        db.close()

    def tearDown(self):
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
        import psycopg2
        conn = psycopg2.connect(DATABASE_URL)
        conn.autocommit = True
        conn.cursor().execute(f'DROP SCHEMA IF EXISTS {self.schema} CASCADE')
        conn.close()
        self.tmp.cleanup()

    def start(self, command, name):
        home = os.path.join(self.tmp.name, name)
        os.makedirs(home, exist_ok=True)
        port = free_port()
        log = open(os.path.join(self.tmp.name, f'{name}.log'), 'w')  # a file, not a pipe nobody drains
        self.addCleanup(log.close)
        process = subprocess.Popen([*command, 'serve', '--port', str(port)],
                                   env={**self.env, 'QUERYAPIGATE_HOME': home}, stdout=log, stderr=subprocess.STDOUT)
        process.log_path = log.name
        self.processes.append(process)
        base = f'http://127.0.0.1:{port}'

        def up():
            if process.poll() is not None:
                return True  # exited - the caller looks at why
            try:
                return call(base, 'GET', '/health', key=None)[0] == 200
            except OSError:
                return False
        eventually(up, timeout=30)
        return base, process

    def test_the_previous_release_keeps_serving_while_the_new_one_upgrades_the_store(self):
        old, _ = self.start([OLD_BIN], 'old')
        released = subprocess.check_output([OLD_BIN, '--version'], text=True).split()[-1]  # e.g. 0.15.0
        self.assertEqual(call(old, 'GET', '/health', key=None)[1]['version'], released)
        call(old, 'POST', '/api/v1/connections', {'name': 'data', 'db': 'sqlite', 'database': self.data,
                                                  'active': True})
        status, body, _ = call(old, 'POST', '/api/v1/queries', {'name': 'total', 'sql': 'SELECT SUM(n) AS s FROM t',
                                                                'connection_name': 'data', 'description': 'd',
                                                                'publish': True})
        self.assertEqual(status, 201, body)

        new, _ = self.start([sys.executable, '-m', 'queryapigate'], 'new')  # upgrades the store
        import psycopg2
        conn = psycopg2.connect(DATABASE_URL, options=f'-csearch_path={self.schema}')
        from queryapigate import db
        cursor = conn.cursor()
        cursor.execute('SELECT version FROM schema_version')
        self.assertEqual(cursor.fetchone()[0], db.SCHEMA_VERSION)
        conn.close()

        # The old node, still running, against the upgraded store: reads, writes, and its runs are recorded.
        self.assertEqual(call(old, 'GET', '/q/total')[1], [{'s': 3}])
        status, body, _ = call(old, 'POST', '/api/v1/api-keys', {'name': 'made-by-old', 'connections': ['data']})
        self.assertEqual(status, 201, body)
        secret = body['secret']
        status, body, _ = call(old, 'POST', '/api/v1/queries/total/versions',
                               {'sql': 'SELECT SUM(n) * 10 AS s FROM t', 'connection_name': 'data',
                                'description': 'd', 'publish': True})
        self.assertEqual(status, 201, body)
        # ... and the new node sees all of it, and the other way round.
        self.assertEqual(call(new, 'GET', '/q/total', key=secret)[1], [{'s': 30}])
        status, body, _ = call(new, 'POST', '/api/v1/api-keys', {'name': 'made-by-new', 'connections': ['data']})
        self.assertEqual(status, 201, body)
        self.assertEqual(call(old, 'GET', '/q/total', key=body['secret'])[1], [{'s': 30}])
        self.assertTrue(eventually(lambda: len(call(new, 'GET', '/api/v1/history?query=total')[1]['items']) >= 3))

        # A node that restarts mid-upgrade can't come back on the old release: it refuses, saying why.
        _, again = self.start([OLD_BIN], 'old-again')
        again.wait(timeout=30)
        self.assertNotEqual(again.returncode, 0)
        with open(again.log_path) as log:
            self.assertIn('from a newer release of QueryAPIGate', log.read())


if __name__ == '__main__':
    unittest.main()

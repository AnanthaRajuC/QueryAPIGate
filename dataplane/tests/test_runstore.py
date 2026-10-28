"""End to end, with a run store (ADR 0029): the runs the data plane serves are written to ClickHouse by the data plane
itself, with ids from the block Python leased it - and they read back through Python exactly as Python's own runs do.

    cargo build --release --manifest-path dataplane/Cargo.toml
    QUERYAPIGATE_DATAPLANE_TEST_POSTGRES=127.0.0.1:55435:postgres:pw:postgres \\
    QUERYAPIGATE_DATAPLANE_TEST_RUN_STORE_URL=http://default:pw@127.0.0.1:8123 \\
    .venv/bin/python dataplane/tests/test_runstore.py
"""
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from test_forwarding import Pair, binary, request  # noqa: E402
from test_queries import details  # noqa: E402

ADMIN = 'runstore-admin-key'
POSTGRES = details('QUERYAPIGATE_DATAPLANE_TEST_POSTGRES')
CLICKHOUSE = os.environ.get('QUERYAPIGATE_DATAPLANE_TEST_RUN_STORE_URL', '').strip().rstrip('/') or None
DATABASE = 'qag_dataplane_test'
VARYING = ('executed_at', 'request_id', 'duration_ms', 'serialization_ms')  # what differs between any two runs


def clickhouse(sql):
    """Rows of a read, as dicts, from the test's ClickHouse."""
    parts = urllib.parse.urlsplit(CLICKHOUSE)
    url = f'{parts.scheme}://{parts.hostname}:{parts.port or 8123}/?' + urllib.parse.urlencode(
        {'query': sql + ' FORMAT JSONEachRow', 'output_format_json_quote_64bit_integers': 0})
    req = urllib.request.Request(url, method='POST', headers={
        'X-ClickHouse-User': urllib.parse.unquote(parts.username or 'default'),
        'X-ClickHouse-Key': urllib.parse.unquote(parts.password or '')})
    with urllib.request.urlopen(req, timeout=10) as res:
        return [json.loads(line) for line in res.read().splitlines() if line]


@unittest.skipIf(binary() is None, 'build the data plane first')
@unittest.skipIf(POSTGRES is None, 'set QUERYAPIGATE_DATAPLANE_TEST_POSTGRES')
@unittest.skipIf(CLICKHOUSE is None, 'set QUERYAPIGATE_DATAPLANE_TEST_RUN_STORE_URL')
class RunStoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg2
        conn = psycopg2.connect(host=POSTGRES['host'], port=POSTGRES['port'], user=POSTGRES['user'],
                                password=POSTGRES['password'], dbname=POSTGRES['database'])
        conn.autocommit = True
        with conn.cursor() as cursor:
            cursor.execute('DROP TABLE IF EXISTS dp_runs')
            cursor.execute('CREATE TABLE dp_runs (id int PRIMARY KEY, name text)')
            cursor.executemany('INSERT INTO dp_runs VALUES (%s, %s)', [(i, f'Name {i}') for i in range(1, 6)])
        conn.close()
        clickhouse(f'DROP DATABASE IF EXISTS {DATABASE} SYNC')
        cls.tmp = tempfile.TemporaryDirectory()
        cls.pair = Pair(Path(cls.tmp.name), {'QUERYAPIGATE_API_KEY': ADMIN,
                                             'QUERYAPIGATE_RUN_STORE_URL': f'{CLICKHOUSE}/{DATABASE}'})
        cls.python = ('127.0.0.1', cls.pair.python_port)
        admin = {'X-API-Key': ADMIN}
        status, _h, body = request(*cls.python, 'POST', '/api/v1/connections', admin, {
            'name': 'shop', 'db': 'postgres', 'host': POSTGRES['host'], 'port': POSTGRES['port'],
            'user': POSTGRES['user'], 'password': POSTGRES['password'], 'database': POSTGRES['database'],
            'active': True})
        assert status == 201, body
        status, _h, body = request(*cls.python, 'POST', '/api/v1/queries', admin, {
            'name': 'by_id', 'connection_name': 'shop', 'description': 'by_id',
            'sql': 'SELECT id, name FROM dp_runs WHERE id = :id', 'parameters': {'id': 'int'}, 'publish': True})
        assert status == 201, body
        status, _h, body = request(*cls.python, 'POST', '/api/v1/api-keys', admin, {'name': 'app',
                                                                                   'connections': ['shop']})
        assert status == 201, body
        cls.key = json.loads(body)['secret']

    @classmethod
    def tearDownClass(cls):
        cls.pair.stop()
        cls.tmp.cleanup()
        clickhouse(f'DROP DATABASE IF EXISTS {DATABASE} SYNC')

    def served(self):
        self.pair.dataplane_log.flush()
        return (self.pair.home / 'dataplane.log').read_text().count('Executing on ')

    def call(self, request_id, host=None):
        host = host or ('127.0.0.1', self.pair.port)
        return request(*host, 'GET', '/q/by_id?id=2', {'X-API-Key': self.key, 'X-Request-Id': request_id})

    def stored(self, request_id, table='events', timeout=10.0):
        """The row of the run with ``request_id``, once it's there."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            rows = clickhouse(f"SELECT id, entry FROM {DATABASE}.{table} WHERE position(entry, '\"{request_id}\"') > 0")
            if rows:
                return rows[0]
            time.sleep(0.1)
        self.fail(f'no run {request_id} in {table}')

    def served_run(self, request_id):
        for _ in range(2):  # a plan comes back with these
            self.call('warm')
        before = self.served()
        self.assertEqual(self.call(request_id)[0], 200)
        self.assertEqual(self.served(), before + 1, 'the data plane did not serve it itself')
        return self.stored(request_id)

    def test_a_served_run_is_written_with_an_id_from_the_data_plane_s_own_block(self):
        row = self.served_run('dp-own')
        store = sqlite3.connect(self.pair.home / 'queryapigate.db')
        blocks = store.execute("SELECT block_start, size FROM run_writer_leases WHERE holder LIKE 'dataplane:%'"
                               ).fetchall()
        store.close()
        self.assertEqual(len(blocks), 1)
        start, size = blocks[0]
        self.assertTrue(start <= row['id'] % 4096 < start + size, (row['id'] % 4096, blocks))
        self.assertLess(row['id'], 2 ** 53)
        self.stored('dp-own', table='history')  # filled from events by ClickHouse, as for Python's runs
        time.sleep(0.5)  # a report Python got for it has been handled by now: it mustn't record the run a second time
        self.assertEqual(len(clickhouse(f"SELECT id FROM {DATABASE}.events WHERE position(entry, '\"dp-own\"') > 0")),
                         1)

    def test_its_entry_is_the_one_python_writes_for_the_same_call(self):
        served = json.loads(self.served_run('dp-same')['entry'])
        self.assertEqual(self.call('py-same', host=self.python)[0], 200)
        python = json.loads(self.stored('py-same')['entry'])
        for entry in (served, python):
            for name in VARYING:
                entry.pop(name, None)
        self.assertEqual(served, python)

    def test_it_reads_back_through_python(self):
        self.served_run('dp-read')
        status, _h, body = request(*self.python, 'GET', '/api/v1/history?key=app&limit=1000', {'X-API-Key': ADMIN})
        self.assertEqual(status, 200)
        self.assertIn('dp-read', [item.get('request_id') for item in json.loads(body)['items']])


if __name__ == '__main__':
    unittest.main()

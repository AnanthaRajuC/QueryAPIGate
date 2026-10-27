"""End to end: the data plane serves `/q/` itself, from Python's plans (ADR 0028, step 3e) - in front of a real
gunicorn, on a real PostgreSQL. A served call answers exactly as Python answers the same call (status, headers, body),
its run reaches Python's history, and anything that changes - a key revoked, a grant narrowed - is Python's to answer
at once.

    cargo build --release --manifest-path dataplane/Cargo.toml
    QUERYAPIGATE_DATAPLANE_TEST_POSTGRES=127.0.0.1:55435:postgres:pw:postgres \\
    .venv/bin/python dataplane/tests/test_serving.py
"""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from test_forwarding import IGNORED, Pair, binary, request  # noqa: E402
from test_queries import details  # noqa: E402

ADMIN = 'serving-admin-key'
POSTGRES = details('QUERYAPIGATE_DATAPLANE_TEST_POSTGRES')
QUERIES = {
    'by_id': ('SELECT id, email, name, balance, born FROM dp_customers WHERE id = :id', {'id': 'int'}),
    'everyone': ('SELECT id, email, name FROM dp_customers ORDER BY id', {}),
    'by_name': ('SELECT id, name FROM dp_customers WHERE name = :name ORDER BY id', {'name': 'str'}),
    'sleepy': ('SELECT 1 AS done FROM pg_sleep(:s)', {'s': 'float'}),
}


@unittest.skipIf(binary() is None, 'build the data plane first')
@unittest.skipIf(POSTGRES is None, 'set QUERYAPIGATE_DATAPLANE_TEST_POSTGRES')
class ServingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg2
        conn = psycopg2.connect(host=POSTGRES['host'], port=POSTGRES['port'], user=POSTGRES['user'],
                                password=POSTGRES['password'], dbname=POSTGRES['database'])
        conn.autocommit = True
        with conn.cursor() as cursor:
            cursor.execute('DROP TABLE IF EXISTS dp_customers')
            cursor.execute('CREATE TABLE dp_customers (id int PRIMARY KEY, email text, name text, balance numeric, '
                           'born date)')
            cursor.executemany('INSERT INTO dp_customers VALUES (%s, %s, %s, %s, %s)', [
                (i, f'user{i}@example.com', "O'Brien" if i == 3 else f'Name {i}', f'{i}.50', f'1990-01-{i:02d}')
                for i in range(1, 26)])
        conn.close()
        cls.tmp = tempfile.TemporaryDirectory()
        cls.pair = Pair(Path(cls.tmp.name), {'QUERYAPIGATE_API_KEY': ADMIN, 'QUERYAPIGATE_MASK_KEY': 'serving-mask'})
        cls.python = ('127.0.0.1', cls.pair.python_port)
        admin = {'X-API-Key': ADMIN}
        cls.admin = admin
        status, _h, body = request(*cls.python, 'POST', '/api/v1/connections', admin, {
            'name': 'shop', 'db': 'postgres', 'host': POSTGRES['host'], 'port': POSTGRES['port'],
            'user': POSTGRES['user'], 'password': POSTGRES['password'], 'database': POSTGRES['database'],
            'active': True})
        assert status == 201, body
        for name, (sql, parameters) in QUERIES.items():
            status, _h, body = request(*cls.python, 'POST', '/api/v1/queries', admin, {
                'name': name, 'connection_name': 'shop', 'description': name, 'sql': sql, 'parameters': parameters,
                'publish': True})
            assert status == 201, body
        cls.keys = {}
        for name, grants in (('plain', {}), ('masked', {'mask': [{'column': 'dp_customers.email', 'action': 'hash'},
                                                                 {'column': 'dp_customers.name', 'action': 'mask'}]}),
                             ('restricted', {'allowed_tables': ['dp_customers']}), ('doomed', {})):
            status, _h, body = request(*cls.python, 'POST', '/api/v1/api-keys', admin,
                                       {'name': name, 'connections': ['shop'], **grants})
            assert status == 201, body
            cls.keys[name] = json.loads(body)['secret']

    @classmethod
    def tearDownClass(cls):
        cls.pair.stop()
        cls.tmp.cleanup()

    def served(self):
        """How many calls the data plane has run itself so far."""
        self.pair.dataplane_log.flush()
        return (self.pair.home / 'dataplane.log').read_text().count('Executing on ')

    def call(self, path, key, request_id, host=None, method='GET', body=None):
        host = host or ('127.0.0.1', self.pair.port)
        return request(*host, method, path, {'X-API-Key': key, 'X-Request-Id': request_id}, body)

    def assertServedAsPython(self, path, key, method='GET', body=None, warm=None, request_id='same-id'):
        """Warm the plan (with `warm`, another call to the same query, when given), then: the data plane serves the
        call itself, and answers as Python answers it."""
        for _ in range(2):  # the first records the key's use with Python; either may bring the plan back
            self.call(warm or path, key, 'warm', method=method, body=body)
        before = self.served()
        status, headers, payload = self.call(path, key, request_id, method=method, body=body)
        self.assertEqual(self.served(), before + 1, 'the data plane did not serve it itself')
        python = self.call(path, key, request_id, host=self.python, method=method, body=body)
        self.assertEqual((status, payload), python[:3:2])
        strip = lambda found: {k: v for k, v in found.items() if k not in IGNORED | {'server'}}  # noqa: E731
        self.assertEqual(strip(headers), strip(python[1]))
        return status, payload

    def test_a_call_is_served_as_python_serves_it(self):
        for path in ('/q/by_id?id=3', '/q/by_id?id=999', '/q/everyone', '/q/everyone?page=2&page_size=7',
                     '/q/everyone?page=4&page_size=7', '/q/by_name?name=O%27Brien'):
            with self.subTest(path=path):
                self.assertServedAsPython(path, self.keys['plain'])

    def test_a_post_with_a_json_body(self):
        self.assertServedAsPython('/q/by_name', self.keys['plain'], method='POST', body={'params': {'name': 'Name 5'}})

    def test_masked_and_table_restricted_keys(self):
        _status, body = self.assertServedAsPython('/q/everyone?page_size=3', self.keys['masked'])
        rows = json.loads(body)
        self.assertNotIn('@example.com', body.decode())
        self.assertEqual(rows[0]['name'], 'N*****')
        self.assertServedAsPython('/q/by_id?id=4', self.keys['restricted'])

    def test_a_time_limit_ends_in_python_s_504(self):
        # A plan comes from a call that succeeds: a quick one first, then the same query, too slow
        status, _body = self.assertServedAsPython('/q/sleepy?s=2&timeout=0.3', self.keys['plain'], warm='/q/sleepy?s=0')
        self.assertEqual(status, 504)

    def test_a_served_run_is_in_python_s_history(self):
        self.assertServedAsPython('/q/by_id?id=7', self.keys['plain'], request_id='history-check')
        for _ in range(50):
            status, _h, body = request(*self.python, 'GET', '/api/v1/history?query=by_id&limit=200', self.admin)
            mine = [e for e in json.loads(body)['items'] if e.get('request_id') == 'history-check']
            if len(mine) >= 2:  # the data plane's, and the direct call compared with it
                break
            time.sleep(0.1)
        self.assertGreaterEqual(len(mine), 2)
        self.assertEqual({e['key_name'] for e in mine}, {'plain'})

    def test_a_revoked_key_is_refused_at_once(self):
        self.assertServedAsPython('/q/by_id?id=1', self.keys['doomed'])
        status, _h, _b = request(*self.python, 'PATCH', '/api/v1/api-keys/doomed', self.admin, {'active': False})
        self.assertEqual(status, 200)
        before = self.served()
        status, _h, _body = self.call('/q/by_id?id=1', self.keys['doomed'], 'after')
        self.assertEqual(status, 401)
        self.assertEqual(self.served(), before)

    def test_what_the_data_plane_leaves_to_python(self):
        before = self.served()
        for path, headers in (('/q/everyone?format=csv', {}), ('/q/everyone?stream=true&format=csv', {}),
                              ('/q/everyone?version=1', {}), ('/q/everyone', {'Origin': 'https://example.com'}),
                              ('/q/by_id?id=x', {}), ('/q/by_id', {})):
            with self.subTest(path=path, headers=headers):
                direct = request(*self.python, 'GET', path, {'X-API-Key': self.keys['plain'], 'X-Request-Id': 'r',
                                                              **headers})
                forwarded = request('127.0.0.1', self.pair.port, 'GET', path,
                                    {'X-API-Key': self.keys['plain'], 'X-Request-Id': 'r', **headers})
                self.assertEqual(forwarded[0], direct[0])
        self.assertEqual(self.served(), before)


if __name__ == '__main__':
    unittest.main()

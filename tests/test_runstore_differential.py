"""The run store's differential test (ADR 0029 §9, 6): the same random calls sent to a server keeping runs in its
metadata store and to one keeping them in ClickHouse leave the same history - page for page, filter for filter, the
Console's runs per version - and the same events, byte for byte. Only what differs between any two runs is left out:
when they ran, how long they took, their request ids. Runs when QUERYAPIGATE_TEST_RUN_STORE_URL is set."""
import contextlib
import json
import os
import random
import sqlite3
import tempfile
import unittest
from unittest import mock

from queryapigate import config, create_app, db, runstore, store
from tests import TEST_RUN_STORE_URL
from tests.helpers import save_query, write_connections

VARYING = ('executed_at', 'request_id', 'duration_ms', 'serialization_ms', 'trace_id')
SEED = int(os.environ.get('QUERYAPIGATE_DIFFERENTIAL_SEED', '29'))


def steady(entry):
    return {k: v for k, v in entry.items() if k not in VARYING}


@unittest.skipUnless(TEST_RUN_STORE_URL, 'set QUERYAPIGATE_TEST_RUN_STORE_URL to run')
class DifferentialTests(unittest.TestCase):
    def setUp(self):
        self.homes = {}
        for side in ('metadata', 'run_store'):
            tmp = tempfile.TemporaryDirectory()
            self.addCleanup(tmp.cleanup)
            self.homes[side] = tmp.name
        self.servers = {side: self.make(side) for side in self.homes}

    @contextlib.contextmanager
    def side(self, side):
        url = TEST_RUN_STORE_URL if side == 'run_store' else None
        with mock.patch.dict(os.environ, {
                'QUERYAPIGATE_HOME': self.homes[side], 'QUERYAPIGATE_API_KEY': 'admin', 'QUERYAPIGATE_RATE_LIMIT': '',
                'QUERYAPIGATE_HISTORY_FLUSH_INTERVAL': '0', 'QUERYAPIGATE_HISTORY_SAMPLE_RATE': '',
                'QUERYAPIGATE_HISTORY_LIMIT': '6', 'QUERYAPIGATE_HISTORY_RETENTION_DAYS': '30'}), \
                mock.patch.object(config, 'run_store_url', lambda: url):
            yield

    def make(self, side):
        with self.side(side):
            data = os.path.join(self.homes[side], 'data.db')
            conn = sqlite3.connect(data)
            conn.execute('CREATE TABLE t (id INTEGER, name TEXT)')
            conn.executemany('INSERT INTO t VALUES (?, ?)', [(i, f"name {i} é 'q'") for i in range(20)])
            conn.commit()
            conn.close()
            write_connections({'lite': {'db': 'sqlite', 'database': data, 'active': True}})
            client = create_app().test_client()
            admin = {'X-API-Key': 'admin'}
            for name, sql in (('q', 'SELECT id, name FROM t WHERE id >= :min'),
                              ('other', 'SELECT count(*) AS n FROM t WHERE id < :min'),
                              ('broken', 'SELECT nothing FROM missing WHERE 1 = :min')):
                self.assertEqual(save_query(client, headers=admin, body={
                    'author': 'a', 'description': 'd', 'filename': name, 'connection_name': 'lite', 'sql_query': sql,
                    'query_parameters': {'min': 'int'}}).status_code, 201)
            self.assertEqual(client.post('/api/v1/queries/other/versions', headers=admin, json={
                'description': 'v2', 'connection_name': 'lite', 'sql': 'SELECT count(*) AS n FROM t WHERE id <= :min',
                'parameters': {'min': {'type': 'integer'}}}).status_code, 201)
            keys = {}
            for name in ('alice', 'bob'):
                keys[name] = client.post('/api/v1/api-keys', headers=admin, json={
                    'name': name, 'connections': ['lite'], 'queries': ['q', 'other', 'broken']}).get_json()['secret']
            return client, keys

    def call(self, side, kind, key, value):
        client, keys = self.servers[side]
        with self.side(side):
            headers = {'X-API-Key': keys[key] if key != 'admin' else 'admin'}
            if kind == 'adhoc':
                return client.post('/execute_sql', headers={'X-API-Key': 'admin'}, json={
                    'connection_name': 'lite', 'sql_query': f'SELECT id FROM t WHERE id = {value}'}).status_code
            return client.get(f'/q/{kind}?min={value}', headers=headers).status_code

    def read(self, side, path, params=None):
        client, _ = self.servers[side]
        with self.side(side):
            res = client.get(path, headers={'X-API-Key': 'admin'}, query_string=params or {})
            self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
            return res.get_json()

    def pages(self, side, params):
        found, cursor = [], None
        while True:
            page = self.read(side, '/api/v1/history', {**params, 'limit': 7, **({'cursor': cursor} if cursor else {})})
            found += [steady(item) for item in page['items']]
            cursor = page['next_cursor']
            if cursor is None:
                return found

    def events(self, side):
        with self.side(side):
            if side == 'run_store':
                rows = runstore.query(f'SELECT {runstore.EVENT_COLUMNS} FROM events ORDER BY id')
                payloads = [runstore.event_payload(row) for row in rows]
            else:
                payloads = [row[0] for row in db.connection().execute('SELECT payload_json FROM events ORDER BY id')]
        return [{**json.loads(p), 'occurred_at': None, 'entry': steady(json.loads(p)['entry'])} for p in payloads]

    def test_the_same_calls_leave_the_same_history_and_events(self):
        rng = random.Random(SEED)
        calls = [(rng.choice(['q', 'q', 'other', 'broken', 'adhoc']), rng.choice(['alice', 'bob', 'admin']),
                  rng.choice([0, 3, 7, 19, 25])) for _ in range(120)]
        for call in calls:
            statuses = {side: self.call(side, *call) for side in self.servers}
            self.assertEqual(statuses['metadata'], statuses['run_store'], call)
        everything = self.pages('metadata', {})
        self.assertGreater(len(everything), 80)  # not two empty histories, equal
        self.assertEqual({entry['status'] for entry in everything}, {'success', 'error'})
        for params in ({}, {'key': 'alice'}, {'status': 'error'}, {'kind': 'adhoc'}, {'query': 'other'},
                       {'query': 'other', 'version': 2}):
            with self.subTest(filter=params):
                self.assertEqual(self.pages('run_store', params), self.pages('metadata', params))
        for name in ('q', 'other', 'broken'):
            with self.subTest(console=name):
                listed = {}
                for side in self.servers:
                    with self.side(side):
                        content = store.load_versions(name)
                    listed[side] = {v: [steady(e) for e in body['execution_history']]
                                    for v, body in content.items() if v.isdigit()}
                self.assertEqual(listed['run_store'], listed['metadata'])
                details = {side: self.read(side, f'/api/v1/queries/{name}') for side in self.servers}
                for detail in details.values():  # every time in it is when its side's call happened
                    for item in (detail, *detail['versions']):
                        for field in [f for f in item if f.endswith('_at') or f == 'uuid']:
                            item.pop(field)
                self.assertEqual(details['run_store'], details['metadata'])
        self.assertEqual(self.events('run_store'), self.events('metadata'))


if __name__ == '__main__':
    unittest.main()

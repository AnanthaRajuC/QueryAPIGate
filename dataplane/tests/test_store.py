"""The data plane reads the store as Python does (ADR 0028, step 3a): for the same key, saved query and connection, the
digests it computes are the ones Python puts in a plan - on a SQLite store, and on a PostgreSQL one when
QUERYAPIGATE_DATAPLANE_TEST_STORE_URL names a scratch database. A plan is only ever used when they match.

    cargo build --release --manifest-path dataplane/Cargo.toml
    .venv/bin/python dataplane/tests/test_store.py
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))

from test_forwarding import binary  # noqa: E402

from queryapigate import apikeys, create_app, dataplane, runners, schema  # noqa: E402
from tests.helpers import save_query, write_connections  # noqa: E402

SECRET = 'store-test-secret-0123456789'
# Not QUERYAPIGATE_TEST_DATABASE_URL: importing tests/ would map that to a schema per test home
STORE_URL = os.environ.get('QUERYAPIGATE_DATAPLANE_TEST_STORE_URL')


def fake_runner(details, sql, params, limit, offset, read_only, timeout=None, pool=None, **options):
    return ['id', 'name'], [(1, 'A')]


class DigestTests:
    """Mixed into a TestCase per store."""

    store_env: dict = {}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = {'QUERYAPIGATE_HOME': self.tmp.name, 'QUERYAPIGATE_API_KEY': 'admin-key',
                    'QUERYAPIGATE_DATAPLANE_SECRET': SECRET, 'QUERYAPIGATE_HISTORY_FLUSH_INTERVAL': '0',
                    **self.store_env}
        patcher = mock.patch.dict(os.environ, self.env)
        patcher.start()
        self.addCleanup(patcher.stop)
        write_connections({'pg': {'db': 'postgres', 'host': 'db.internal', 'database': 'shop', 'user': 'u',
                                  'password': "p'w é", 'active': True}})
        tables = {'tables': [{'name': 't', 'columns': [{'name': 'id'}, {'name': 'name'}]}]}
        for patch in (mock.patch.dict(runners.RUNNERS, {'postgres': fake_runner}),
                      mock.patch.object(schema, 'fetch_schema', return_value=tables)):
            patch.start()
            self.addCleanup(patch.stop)
        self.client = create_app().test_client()
        save_query(self.client, headers={'X-API-Key': 'admin-key'}, body={
            'filename': 'by_id', 'connection_name': 'pg', 'description': 'Ünïcode, "quotes"',
            'sql_query': 'SELECT id, name FROM t WHERE id = :id', 'query_parameters': {'id': 'int'}})

    def plan_and_probe(self, key):
        res = self.client.get('/q/by_id?id=1', headers={'X-API-Key': key, dataplane.SECRET_HEADER: SECRET})
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        plan = dataplane.decode(res.headers[dataplane.PLAN_HEADER])
        probe = subprocess.run([str(binary()), '--probe-digests', key, 'by_id'], env={**os.environ, **self.env},
                               capture_output=True, text=True, timeout=30)
        self.assertEqual(probe.returncode, 0, probe.stderr)
        return plan['rows'], json.loads(probe.stdout)

    def test_the_data_plane_digests_the_rows_python_planned_from(self):
        key = apikeys.create_key('reporting', connections=['pg'], allowed_tables=['t'],
                                 mask=[{'column': 't.name', 'action': 'hash'}])
        python, rust = self.plan_and_probe(key)
        self.assertEqual(rust, python)

    def test_and_sees_a_change_python_makes(self):
        key = apikeys.create_key('reporting', connections=['pg'])
        before = self.plan_and_probe(key)[1]
        apikeys.update_key('reporting', allowed_tables=['t'])
        python, rust = self.plan_and_probe(key)
        self.assertEqual(rust, python)
        self.assertNotEqual(rust['api_key'], before['api_key'])


@unittest.skipIf(binary() is None, 'build the data plane first')
class SqliteStoreTests(DigestTests, unittest.TestCase):
    store_env = {}


@unittest.skipIf(binary() is None, 'build the data plane first')
@unittest.skipUnless(STORE_URL, 'set QUERYAPIGATE_DATAPLANE_TEST_STORE_URL')
class PostgresStoreTests(DigestTests, unittest.TestCase):
    store_env = {'QUERYAPIGATE_DATABASE_URL': STORE_URL or ''}

    def setUp(self):
        import psycopg2
        conn = psycopg2.connect(self.store_env['QUERYAPIGATE_DATABASE_URL'])
        conn.autocommit = True
        with conn.cursor() as cursor:
            cursor.execute('DROP SCHEMA public CASCADE; CREATE SCHEMA public')
        conn.close()
        super().setUp()


if __name__ == '__main__':
    unittest.main()

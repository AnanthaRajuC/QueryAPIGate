"""Ad-hoc SQL runs in run history and live events (BACKLOG #62): /execute_sql, /execute_mongo and MCP's execute_sql
are recorded like a saved query's runs - caller, transport, connection, SQL (as QUERYAPIGATE_HISTORY_ADHOC_SQL says),
parameter names but never values, rows, duration, status - and the schema-5 upgrade that makes room for them."""
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from queryapigate import create_app, db, events, history, mcp_server
from tests import TEST_DATABASE_URL
from tests.helpers import write_connections

ADMIN = {'X-API-Key': 'admin-key'}


class AdhocTestCase(unittest.TestCase):
    env = {}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(history.flush)
        self.db_path = os.path.join(self.tmp.name, 'data.db')
        conn = sqlite3.connect(self.db_path)
        conn.execute('CREATE TABLE t (id INTEGER, secret TEXT)')
        conn.executemany('INSERT INTO t VALUES (?, ?)', [(i, f's{i}') for i in range(1, 6)])
        conn.commit()
        conn.close()
        patcher = mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': self.tmp.name, 'QUERYAPIGATE_API_KEY': 'admin-key',
                                               **self.env})
        patcher.start()
        self.addCleanup(patcher.stop)
        write_connections({'lite': {'db': 'sqlite', 'database': self.db_path, 'active': True}})
        self.app = create_app()
        self.client = self.app.test_client()

    def run_sql(self, sql, headers=ADMIN, **body):
        return self.client.post('/execute_sql', json={'sql': sql, 'connection_name': 'lite', **body}, headers=headers)

    def adhoc(self, **filters):
        res = self.client.get('/api/v1/history', query_string={'kind': 'adhoc', **filters}, headers=ADMIN)
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        return res.get_json()['items']

    def key(self, name, **grants):
        res = self.client.post('/api/v1/api-keys', json={'name': name, **grants}, headers=ADMIN)
        self.assertEqual(res.status_code, 201, res.get_data(as_text=True))
        return res.get_json()['secret']


class RecordingTests(AdhocTestCase):
    def test_a_rest_run_is_recorded_with_its_caller_sql_and_parameter_names_but_not_values(self):
        secret = self.key('analyst', connections=['lite'])
        res = self.run_sql('SELECT id FROM t WHERE secret = :code', headers={'X-API-Key': secret},
                           params={'code': 's3'})
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        [run] = self.adhoc()
        self.assertEqual((run['kind'], run['query'], run['version']), ('adhoc', None, None))
        self.assertEqual((run['key_name'], run['transport'], run['connection_name']), ('analyst', 'rest', 'lite'))
        self.assertEqual((run['status'], run['rows']), ('success', 1))
        self.assertEqual(run['sql'], 'SELECT id FROM t WHERE secret = :code')
        self.assertEqual(run['params'], ['code'])
        self.assertNotIn('s3', str(run))  # a parameter's value is never kept
        self.assertIsInstance(run['duration_ms'], int)
        self.assertTrue(run['request_id'])

    def test_a_failed_run_is_recorded_with_its_error(self):
        self.assertEqual(self.run_sql('SELECT nope FROM t').status_code, 500)
        [run] = self.adhoc(status='error')
        self.assertEqual(run['status'], 'error')
        self.assertTrue(run['error'])

    def test_a_streamed_run_is_recorded_once_drained(self):
        res = self.client.post('/execute_sql?stream=true&format=csv', json={'sql': 'SELECT id FROM t',
                                                                            'connection_name': 'lite'}, headers=ADMIN)
        self.assertEqual(len(res.get_data(as_text=True).splitlines()), 6)
        [run] = self.adhoc()
        self.assertEqual((run['status'], run['rows']), ('success', 5))

    def test_an_mcp_run_is_recorded_as_mcp(self):
        secret = self.key('agent', connections=['lite'])
        result = mcp_server.handle_call(self.app, secret, '', '10.0.0.1', 'execute_sql',
                                        {'connection_name': 'lite', 'sql': 'SELECT id FROM t WHERE id = :id',
                                         'params': {'id': 2}})
        self.assertFalse(result.get('isError'), result)
        [run] = self.adhoc()
        self.assertEqual((run['transport'], run['key_name'], run['rows'], run['params']), ('mcp', 'agent', 1, ['id']))

    def test_saved_runs_say_which_front_door_they_came_through_too(self):
        self.client.post('/api/v1/queries', headers=ADMIN, json={
            'name': 'q', 'description': 'd', 'sql': 'SELECT id FROM t', 'connection_name': 'lite', 'publish': True})
        self.client.get('/q/q', headers=ADMIN)
        mcp_server.handle_call(self.app, 'admin-key', '', '10.0.0.1', 'q', {})
        runs = self.client.get('/api/v1/history?kind=saved', headers=ADMIN).get_json()['items']
        self.assertEqual(sorted(r['transport'] for r in runs), ['mcp', 'rest'])

    def test_the_kind_filter_separates_them(self):
        self.client.post('/api/v1/queries', headers=ADMIN, json={
            'name': 'q', 'description': 'd', 'sql': 'SELECT id FROM t', 'connection_name': 'lite', 'publish': True})
        self.client.get('/q/q', headers=ADMIN)
        self.run_sql('SELECT 1')
        everything = self.client.get('/api/v1/history', headers=ADMIN).get_json()['items']
        self.assertEqual(sorted(r['kind'] for r in everything), ['adhoc', 'saved'])
        self.assertEqual(len(self.adhoc()), 1)
        res = self.client.get('/api/v1/history?kind=other', headers=ADMIN)
        self.assertEqual(res.status_code, 400)

    def test_a_scoped_keys_live_feed_carries_its_own_adhoc_runs_only(self):
        mine, theirs = self.key('mine', connections=['lite']), self.key('theirs', connections=['lite'])
        broadcaster = self.app.extensions['queryapigate_broadcaster']
        subscriber = broadcaster.subscribe(key_name='mine')
        self.addCleanup(broadcaster.unsubscribe, subscriber)
        self.run_sql('SELECT 1 AS a', headers={'X-API-Key': theirs})
        self.run_sql('SELECT 2 AS b', headers={'X-API-Key': mine})
        event = subscriber.get(timeout=2)
        self.assertEqual((event['type'], event['entry']['sql']), ('adhoc_execution', 'SELECT 2 AS b'))
        self.assertTrue(subscriber.empty())

    def test_the_events_server_formats_an_adhoc_run(self):
        payload = events.event_payload(None, None, {'connection_name': 'lite', 'sql': 'SELECT 1'})
        self.assertEqual(payload, {'type': 'adhoc_execution', 'connection_name': 'lite',
                                   'entry': {'connection_name': 'lite', 'sql': 'SELECT 1'}})


class SqlKeptTests(AdhocTestCase):
    def test_long_sql_is_capped(self):
        sql = 'SELECT 1 AS x' + ' ' * 5000
        self.run_sql(sql)
        [run] = self.adhoc()
        self.assertEqual(len(run['sql']), 4000)
        self.assertTrue(run['sql_truncated'])

    def test_hash_keeps_only_a_digest(self):
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_HISTORY_ADHOC_SQL': 'hash'}):
            self.run_sql("SELECT id FROM t WHERE secret = 's1'")
        [run] = self.adhoc()
        self.assertNotIn('sql', run)
        self.assertEqual(len(run['sql_sha256']), 64)

    def test_none_keeps_nothing_of_it(self):
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_HISTORY_ADHOC_SQL': 'none'}):
            self.run_sql('SELECT 1')
        [run] = self.adhoc()
        self.assertNotIn('sql', run)
        self.assertNotIn('sql_sha256', run)

    def test_a_bad_setting_stops_startup(self):
        from queryapigate import config
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_HISTORY_ADHOC_SQL': 'everything'}), \
                self.assertRaises(ValueError):
            config.check_settings()


class LimitTests(AdhocTestCase):
    env = {'QUERYAPIGATE_HISTORY_ADHOC_LIMIT': '3', 'QUERYAPIGATE_HISTORY_FLUSH_INTERVAL': '0'}

    def test_only_the_newest_adhoc_runs_are_kept(self):
        for i in range(5):
            self.run_sql(f'SELECT {i} AS n')
        self.assertEqual([r['sql'] for r in self.adhoc()], ['SELECT 4 AS n', 'SELECT 3 AS n', 'SELECT 2 AS n'])


@unittest.skipIf(TEST_DATABASE_URL, 'builds a SQLite store by hand')
class SchemaFiveUpgradeTests(unittest.TestCase):
    """A schema-4 store (query_name/version NOT NULL) is rebuilt on start, keeping every run and its rowid - the id
    its live event carries, so a client resuming with Last-Event-ID still finds its place."""

    def test_an_older_history_table_is_rebuilt_keeping_rowids(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': home}):
            db.init_schema()
            conn = db.connection()
            conn.execute('DROP TABLE execution_history')
            conn.execute(db._SQLITE_HISTORY_V5.replace('query_name TEXT,', 'query_name TEXT NOT NULL,')
                         .replace('version INTEGER,', 'version INTEGER NOT NULL,'))
            conn.execute("INSERT INTO saved_queries (name) VALUES ('q')")
            conn.execute("INSERT INTO saved_query_versions (query_name, version, uuid, created_at, last_modified_at, "
                         "status, fields_json) VALUES ('q', 1, 'u1', 'now', 'now', 'active', '{}')")
            for rowid in (7, 9):
                conn.execute('INSERT INTO execution_history (rowid, query_name, version, executed_at, entry_json, '
                             "status, key_name) VALUES (?, 'q', 1, '2026-01-01 00:00:00', '{}', 'success', 'admin')",
                             (rowid,))
            conn.execute('UPDATE schema_version SET version = 4')
            self.assertTrue(db._history_requires_a_query(conn, False))
            db.init_schema()
            self.assertFalse(db._history_requires_a_query(db.connection(), False))
            rows = db.connection().execute('SELECT rowid, query_name FROM execution_history ORDER BY rowid').fetchall()
            self.assertEqual([(r[0], r[1]) for r in rows], [(7, 'q'), (9, 'q')])
            version = db.connection().execute('SELECT version FROM schema_version').fetchone()[0]
            self.assertEqual(version, db.SCHEMA_VERSION)
            db.connection().execute("INSERT INTO execution_history (executed_at, entry_json) VALUES ('now', '{}')")
            db.close()


@unittest.skipUnless(TEST_DATABASE_URL, 'set QUERYAPIGATE_TEST_DATABASE_URL to run')
class SchemaFivePostgresUpgradeTests(AdhocTestCase):
    def test_an_older_history_table_accepts_adhoc_runs_after_start(self):
        conn = db.connection()
        conn.execute('ALTER TABLE execution_history ALTER COLUMN query_name SET NOT NULL')
        conn.execute('ALTER TABLE execution_history ALTER COLUMN version SET NOT NULL')
        self.assertTrue(db._history_requires_a_query(conn, True))
        db.init_schema()
        self.assertFalse(db._history_requires_a_query(db.connection(), True))
        self.run_sql('SELECT 1 AS one')
        self.assertEqual(len(self.adhoc()), 1)


if __name__ == '__main__':
    unittest.main()

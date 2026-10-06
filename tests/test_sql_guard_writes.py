"""Writes hidden behind a read-only first keyword (sqltools.write_operations): a writable CTE, the statement
EXPLAIN ANALYZE runs, SELECT ... INTO. PostgreSQL, MySQL, ClickHouse and SQLite refuse these in their own read-only
sessions; DuckDB, H2 and JDBC connections are opened read-write, so the guard is their only lock - and before this,
`EXPLAIN ANALYZE DELETE FROM t` emptied a DuckDB table with writes switched off."""
import os
import tempfile
import unittest
from unittest import mock

import duckdb

from queryapigate import create_app, sqltools
from tests.helpers import create_key, secret_of, write_connections

ADMIN = {'X-API-Key': 'admin-key'}


class RuleTests(unittest.TestCase):
    def test_hidden_writes_are_found(self):
        cases = {
            'WITH x AS (SELECT 2 AS n) INSERT INTO t SELECT n FROM x': {'insert'},
            'WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d': {'delete'},
            'with u as (update t set n = 1 returning n) select * from u': {'update'},
            'WITH s AS (SELECT 1) MERGE INTO t USING s ON true WHEN MATCHED THEN DELETE': {'merge', 'delete'},
            'EXPLAIN ANALYZE DELETE FROM t': {'delete'},
            'EXPLAIN (ANALYZE, BUFFERS) INSERT INTO t VALUES (1)': {'insert'},
            'explain analyze verbose update t set n = 2': {'update'},
            'EXPLAIN ANALYZE WITH d AS (DELETE FROM t RETURNING *) SELECT 1': {'delete'},
            'SELECT * INTO new_table FROM t': {'select into'},
            "SELECT n FROM t INTO OUTFILE '/tmp/x'": {'select into'},
            'DELETE FROM t': {'delete'},
            'EXPLAIN DROP TABLE t': {'drop'},
        }
        for sql, expected in cases.items():
            with self.subTest(sql=sql):
                self.assertEqual(sqltools.write_operations(sql, 'postgres'), expected)
                self.assertFalse(sqltools.is_read_only(sql, 'postgres'))

    def test_reads_stay_reads(self):
        for sql, dialect in (
                ('SELECT * FROM t', 'postgres'),
                ("SELECT * FROM t WHERE action = 'delete' OR note = 'insert into x'", 'postgres'),
                ('SELECT "update", "delete" FROM t', 'postgres'),
                ('SELECT `insert` FROM t', 'mysql'),
                ('SELECT updated_at, deleted, insert_count FROM t', 'postgres'),
                ('SELECT * FROM t -- delete everything\nWHERE n = 1', 'postgres'),
                ('SELECT * FROM t /* update */', 'postgres'),
                ('SELECT * FROM t FOR UPDATE', 'postgres'),
                ('SELECT * FROM t FOR NO KEY UPDATE SKIP LOCKED', 'postgres'),
                ("SELECT replace(name, 'a', 'b') FROM t", 'postgres'),
                ("SELECT * FROM merge('db', '^t')", 'clickhouse'),
                ('WITH x AS (SELECT 1 AS n) SELECT n FROM x', 'duckdb'),
                ('EXPLAIN SELECT * FROM t', 'postgres'),
                ('EXPLAIN ANALYZE SELECT * FROM t', 'duckdb'),
                ('EXPLAIN QUERY PLAN SELECT * FROM t', 'sqlite'),
                ('EXPLAIN FORMAT=JSON SELECT * FROM t', 'mysql'),
                ('EXPLAIN PIPELINE SELECT 1', 'clickhouse'),
                ('EXPLAIN customers', 'mysql'),
                ('SHOW TABLES', 'mysql'),
                ('DESCRIBE t', 'duckdb'),
                ('VALUES (1), (2)', 'postgres')):
            with self.subTest(sql=sql):
                self.assertEqual(sqltools.write_operations(sql, dialect), set())

    def test_allowed_write_ops_cover_hidden_writes_too(self):
        sql = 'WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d'
        with self.assertRaises(Exception) as caught:
            sqltools.validate_sql(sql, 'postgres', allow_writes=True, allowed_write_ops=['insert'])
        self.assertEqual(caught.exception.code, 'write_op_not_allowed')
        sqltools.validate_sql(sql, 'postgres', allow_writes=True, allowed_write_ops=['delete'])


class DuckDbTests(unittest.TestCase):
    """End to end on a DuckDB file - the engine that ran these."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': self.tmp.name, 'QUERYAPIGATE_API_KEY': 'admin-key'})
        patcher.start()
        self.addCleanup(patcher.stop)
        path = os.path.join(self.tmp.name, 'data.duckdb')
        conn = duckdb.connect(path)
        conn.execute('CREATE TABLE t (n INTEGER)')
        conn.execute('INSERT INTO t VALUES (1), (2)')
        conn.close()
        write_connections({'duck': {'db': 'duckdb', 'database': path, 'active': True}})
        self.client = create_app().test_client()

    def run_sql(self, sql, headers=ADMIN):
        return self.client.post('/execute_sql', headers=headers, json={'sql': sql, 'connection_name': 'duck'})

    def count(self):
        return self.run_sql('SELECT COUNT(*) AS n FROM t').get_json()[0]['n']

    def test_nothing_hidden_gets_through_with_writes_off(self):
        for sql in ('EXPLAIN ANALYZE DELETE FROM t', 'EXPLAIN ANALYZE INSERT INTO t VALUES (9)',
                    'WITH x AS (SELECT 3 AS n) INSERT INTO t SELECT n FROM x'):
            with self.subTest(sql=sql):
                res = self.run_sql(sql)
                self.assertEqual((res.status_code, res.get_json()['code']), (403, 'read_only'))
        self.assertEqual(self.count(), 2)
        self.assertEqual(self.run_sql('EXPLAIN ANALYZE SELECT * FROM t').status_code, 200)

    def test_a_key_limited_to_inserts_cannot_delete_through_explain(self):
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_ALLOW_WRITES': '1'}):
            key = {'X-API-Key': secret_of(create_key(self.client, headers=ADMIN, name='loader', connections=['duck'],
                                                     allow_writes=True, allowed_write_ops=['insert']))}
            res = self.run_sql('EXPLAIN ANALYZE DELETE FROM t', headers=key)
            self.assertEqual((res.status_code, res.get_json()['code']), (403, 'write_op_not_allowed'))
            self.assertEqual(self.run_sql('WITH x AS (SELECT 3 AS n) INSERT INTO t SELECT n FROM x',
                                          headers=key).status_code, 200)
        self.assertEqual(self.count(), 3)


if __name__ == '__main__':
    unittest.main()

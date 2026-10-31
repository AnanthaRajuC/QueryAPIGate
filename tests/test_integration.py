"""Integration tests against real database servers.

Each database is opt-in: set the matching variable to a JSON connection object, e.g.

    QUERYAPIGATE_IT_POSTGRES='{"host": "localhost", "user": "postgres", "password": "pw", "database": "postgres"}'
    QUERYAPIGATE_IT_MYSQL=...       QUERYAPIGATE_IT_CLICKHOUSE=...       QUERYAPIGATE_IT_H2=...

The tests create and drop a table called queryapigate_it, so point them at a scratch database.
CI provides these as service containers (see .github/workflows/ci.yml).
"""
import json
import os
import tempfile
import time
import unittest
from unittest import mock

from queryapigate import config, create_app, history, pool, runners, tableguard
from queryapigate.errors import ApiError
from tests.helpers import save_query, write_connections

CREATE = {
    'postgres': 'CREATE TABLE queryapigate_it (id INT, name VARCHAR(50), price NUMERIC(8, 2), added TIMESTAMP)',
    'mysql': 'CREATE TABLE queryapigate_it (id INT, name VARCHAR(50), price DECIMAL(8, 2), added DATETIME)',
    'h2': 'CREATE TABLE queryapigate_it (id INT, name VARCHAR(50), price DECIMAL(8, 2), added TIMESTAMP)',
    'clickhouse': 'CREATE TABLE queryapigate_it (id Int32, name String, price Decimal(8, 2), added DateTime) '
                  'ENGINE = MergeTree ORDER BY id',
}
CREATE['jdbc'] = CREATE['h2']  # JdbcViaH2Tests points the generic jdbc driver at the same real H2 server
CREATE['duckdb'] = CREATE['postgres']  # DuckDB's SQL dialect accepts the same CREATE TABLE as Postgres
CREATE['trino'] = 'CREATE TABLE queryapigate_it (id INTEGER, name VARCHAR, price DECIMAL(8, 2), added TIMESTAMP)'
INSERT = {
    'postgres': "INSERT INTO queryapigate_it VALUES ({i}, 'Item {i}', {i}.50, '2024-01-{i:02d} 10:00:00')",
    'mysql': "INSERT INTO queryapigate_it VALUES ({i}, 'Item {i}', {i}.50, '2024-01-{i:02d} 10:00:00')",
    'h2': "INSERT INTO queryapigate_it VALUES ({i}, 'Item {i}', {i}.50, TIMESTAMP '2024-01-{i:02d} 10:00:00')",
    'clickhouse': "INSERT INTO queryapigate_it VALUES ({i}, 'Item {i}', {i}.50, '2024-01-{i:02d} 10:00:00')",
}
INSERT['jdbc'] = INSERT['h2']
INSERT['duckdb'] = INSERT['postgres']
INSERT['trino'] = "INSERT INTO queryapigate_it VALUES ({i}, 'Item {i}', {i}.50, TIMESTAMP '2024-01-{i:02d} 10:00:00')"
COMMENTS = {  # a table comment and a column comment, as each database writes them
    'postgres': ["COMMENT ON TABLE queryapigate_it IS 'Items for sale'",
                 "COMMENT ON COLUMN queryapigate_it.price IS 'Price in EUR, VAT included'"],
    'mysql': ["ALTER TABLE queryapigate_it COMMENT = 'Items for sale'",
              "ALTER TABLE queryapigate_it MODIFY price DECIMAL(8, 2) COMMENT 'Price in EUR, VAT included'"],
    'clickhouse': ["ALTER TABLE queryapigate_it MODIFY COMMENT 'Items for sale'",
                   "ALTER TABLE queryapigate_it COMMENT COLUMN price 'Price in EUR, VAT included'"],
}
COMMENTS['duckdb'] = COMMENTS['postgres']
# Column masking (ADR 0005) traces SQL with each database's own grammar: statements that read queryapigate_it.name -
# which holds 'Item 1'..'Item 7' - in that database's own ways, on top of MASKED_SHAPES below.
MASKED_DIALECT = {
    'postgres': ['SELECT name::text AS c FROM queryapigate_it', "SELECT name || '!' AS c FROM queryapigate_it",
                 'SELECT CAST(name AS varchar(80)) AS c FROM queryapigate_it',
                 'SELECT "name" FROM "queryapigate_it"', 'SELECT coalesce(name, \'-\') AS c FROM queryapigate_it'],
    'mysql': ["SELECT CONCAT(name, '!') AS c FROM queryapigate_it", 'SELECT `name` FROM `queryapigate_it`',
              'SELECT CAST(name AS CHAR) AS c FROM queryapigate_it',
              'SELECT IFNULL(name, \'-\') AS c FROM queryapigate_it'],
    'clickhouse': ["SELECT concat(name, '!') AS c FROM queryapigate_it", 'SELECT lower(name) AS c FROM queryapigate_it',
                   'SELECT toString(name) AS c FROM queryapigate_it', 'SELECT `name` FROM queryapigate_it'],
}
MASKED_DIALECT['duckdb'] = MASKED_DIALECT['postgres']
MASKED_DIALECT['trino'] = ['SELECT CAST(name AS varchar) AS c FROM queryapigate_it',
                           "SELECT name || '!' AS c FROM queryapigate_it",
                           'SELECT "name" FROM "queryapigate_it"',
                           "SELECT coalesce(name, '-') AS c FROM queryapigate_it"]
MASKED_SHAPES = [  # every one of these must come back masked - not refused
    'SELECT name FROM queryapigate_it',
    'SELECT name AS n FROM queryapigate_it',
    'SELECT upper(name) AS u FROM queryapigate_it',
    'WITH x AS (SELECT name AS nm FROM queryapigate_it) SELECT nm FROM x',
    'SELECT name FROM queryapigate_it UNION ALL SELECT name FROM queryapigate_it',
    'SELECT a.name FROM queryapigate_it a JOIN queryapigate_it b ON a.id = b.id',
    'SELECT t.name FROM (SELECT id, name FROM queryapigate_it) t',
    'SELECT * FROM queryapigate_it',
]


class IntegrationBase:
    db = None  # set by subclasses
    env_var = None
    slow_sql = None  # a statement that would run for minutes
    finite_slow_sql = None  # a statement that takes about two seconds
    connection_id_sql = None  # returns an id that identifies the database connection serving it

    @classmethod
    def setUpClass(cls):
        raw = os.environ.get(cls.env_var)
        if not raw:
            raise unittest.SkipTest(f'{cls.env_var} not set')
        cls.details = {**json.loads(raw), 'db': cls.db, 'active': True}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._prepare_storage()
        patcher = mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop('QUERYAPIGATE_API_KEY', None)
        write_connections({'it': self.details})
        self.client = create_app().test_client()

        # Setting up and tearing down needs writes; the tests themselves run read-only.
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_ALLOW_WRITES': '1'}):
            self.write('DROP TABLE IF EXISTS queryapigate_it')
            self.write(CREATE[self.db])
            for i in range(1, 8):
                self.write(INSERT[self.db].format(i=i))
        self.addCleanup(self.drop)

    def _prepare_storage(self):
        """Hook for a file-based driver (DuckDB) to create its database file before any query touches it -
        a no-op for the network databases, which need no such file."""

    def write(self, sql):
        res = self.client.post('/execute_sql', json={'sql': sql, 'connection_name': 'it'})
        self.assertLess(res.status_code, 300, res.get_data(as_text=True))

    def drop(self):
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_ALLOW_WRITES': '1'}):
            self.client.post('/execute_sql', json={'sql': 'DROP TABLE IF EXISTS queryapigate_it',
                                                   'connection_name': 'it'})

    def query(self, sql, query='', **body):
        return self.client.post(f'/execute_sql{query}', json={'sql': sql, 'connection_name': 'it', **body})

    # ---- tests ----------------------------------------------------------------------

    def test_pagination_and_has_more(self):
        res = self.query('SELECT id FROM queryapigate_it ORDER BY id', '?page=2&page_size=3')
        self.assertEqual([r[list(r)[0]] for r in res.get_json()], [4, 5, 6])
        self.assertEqual(res.headers['X-Has-More'], 'true')
        res = self.query('SELECT id FROM queryapigate_it ORDER BY id LIMIT 100', '?page=3&page_size=3')
        self.assertEqual(len(res.get_json()), 1)
        self.assertEqual(res.headers['X-Has-More'], 'false')

    def test_types_serialise(self):
        row = self.query('SELECT * FROM queryapigate_it WHERE id = 2').get_json()[0]
        values = {k.lower(): v for k, v in row.items()}
        self.assertEqual(values['id'], 2)
        self.assertEqual(values['name'], 'Item 2')
        self.assertEqual(float(values['price']), 2.5)
        self.assertEqual(values['added'], '2024-01-02 10:00:00')

    def test_bound_parameters(self):
        nasty = "Item 3'; DROP TABLE queryapigate_it; --"
        res = self.query('SELECT id FROM queryapigate_it WHERE id = :id OR name = :name',
                         params={'id': 3, 'name': nasty})
        self.assertEqual([list(r.values())[0] for r in res.get_json()], [3])
        # a literal % in the SQL must survive drivers that use %-style parameters
        res = self.query("SELECT id FROM queryapigate_it WHERE name LIKE 'Item%' AND id = :id", params={'id': 1})
        self.assertEqual(len(res.get_json()), 1)
        self.assertEqual(len(self.query('SELECT * FROM queryapigate_it', '?page_size=100').get_json()), 7)

    def test_a_nul_in_a_bound_parameter_is_never_a_500(self):
        """A NUL in a string parameter: a value the database compares as it is, or - PostgreSQL's text can't hold
        one, and psycopg2 refuses to send it - the caller's 400 naming the parameter."""
        res = self.query('SELECT id FROM queryapigate_it WHERE name = :name', params={'name': 'Item\x003'})
        if self.db == 'postgres':
            self.assertEqual((res.status_code, res.get_json()['code']), (400, 'param_invalid'))
            self.assertIn("'name'", res.get_json()['error'])
        else:
            self.assertEqual(res.status_code, 200, res.get_data(as_text=True))  # and no row: none has a NUL
            self.assertNotIsInstance(res.get_json(), list)

    def test_all_formats(self):
        for fmt in ('json', 'ndjson', 'csv', 'tsv', 'xml', 'yaml', 'xlsx'):
            res = self.query('SELECT * FROM queryapigate_it ORDER BY id', f'?format={fmt}&page_size=2')
            self.assertEqual(res.status_code, 200, fmt)
            self.assertTrue(res.get_data(), fmt)

    def test_writes_are_blocked(self):
        res = self.query('DELETE FROM queryapigate_it')
        self.assertEqual(res.status_code, 403)
        self.assertEqual(len(self.query('SELECT * FROM queryapigate_it', '?page_size=100').get_json()), 7)

    def test_query_timeout_cancels_the_statement_and_keeps_the_service_usable(self):
        started = time.monotonic()
        res = self.query(self.slow_sql, '?timeout=1')
        elapsed = time.monotonic() - started
        self.assertEqual(res.status_code, 504, res.get_data(as_text=True))
        self.assertLess(elapsed, 10, 'the statement should be cancelled close to the 1 second limit')
        self.assertIn('time limit', res.get_json()['error'])
        self.assertEqual(len(self.query('SELECT * FROM queryapigate_it', '?page_size=100').get_json()), 7)

    # ---- connection pooling ----------------------------------------------------------

    def connection_ids(self, count):
        return [list(self.query(self.connection_id_sql).get_json()[0].values())[0] for _ in range(count)]

    def test_connections_are_reused_when_pooled(self):
        if not self.connection_id_sql:
            self.skipTest('no connection id query for this database')
        self.assertEqual(len(set(self.connection_ids(4))), 1)

    def test_every_request_gets_its_own_connection_when_pooling_is_disabled(self):
        if not self.connection_id_sql:
            self.skipTest('no connection id query for this database')
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_POOL_SIZE': '0'}):
            self.assertEqual(len(set(self.connection_ids(3))), 3)

    def test_pooled_connection_sees_data_committed_elsewhere(self):
        before = self.query('SELECT COUNT(*) AS n FROM queryapigate_it').get_json()[0]
        before = list(before.values())[0]
        # a separate, unpooled connection inserts and commits
        runners.RUNNERS[self.db](self.details, INSERT[self.db].format(i=8), None, 10, 0, False, None, None)
        after = list(self.query('SELECT COUNT(*) AS n FROM queryapigate_it').get_json()[0].values())[0]
        self.assertEqual((int(before), int(after)), (7, 8))

    def test_a_request_limit_does_not_leak_to_the_next_user_of_the_connection(self):
        if not self.finite_slow_sql:
            self.skipTest('no two-second statement for this database')
        self.assertEqual(self.query('SELECT 1 AS one', '?timeout=1').status_code, 200)  # leaves a 1s limit behind?
        started = time.monotonic()
        res = self.query(self.finite_slow_sql)  # server default limit (30s) must apply again
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        self.assertGreater(time.monotonic() - started, 1.5, 'the previous request\'s 1 second limit cut this one short')

    def test_read_only_mode_holds_on_a_reused_connection(self):
        shared = pool.get_pool()
        self.assertIsNotNone(shared)
        run = runners.RUNNERS[self.db]
        run(self.details, 'SELECT * FROM queryapigate_it', None, 10, 0, True, 5, shared)  # warms the pool
        if self.db != 'h2':  # H2 relies on the SQL guard instead (see H2Tests)
            with self.assertRaises(Exception):  # noqa: B017 - each driver raises its own error type
                run(self.details, 'DELETE FROM queryapigate_it', None, 10, 0, True, 5, shared)
        self.assertEqual(len(self.query('SELECT * FROM queryapigate_it', '?page_size=100').get_json()), 7)

    def test_concurrent_requests_all_succeed(self):
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
            results = list(executor.map(lambda i: self.query('SELECT id FROM queryapigate_it ORDER BY id',
                                                             '?page_size=3').status_code, range(30)))
        self.assertEqual(set(results), {200})

    def test_saved_query_end_to_end(self):
        save_query(self.client, {
            'author': 'it', 'description': 'd', 'filename': 'it_query', 'connection_name': 'it',
            'sql_query': 'SELECT name FROM queryapigate_it WHERE id = :id', 'query_parameters': {'id': 'int'}})
        res = self.client.get('/q/it_query?id=5')
        self.assertEqual([list(r.values())[0] for r in res.get_json()], ['Item 5'])

    def test_streaming_export_returns_every_row(self):
        res = self.query('SELECT id FROM queryapigate_it ORDER BY id', '?stream=true&format=csv')
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        lines = res.get_data(as_text=True).strip().splitlines()
        self.assertEqual(len(lines), 8)  # header + all 7 rows
        self.assertEqual([line.strip() for line in lines[1:]], [str(i) for i in range(1, 8)])
        # page/page_size make no sense for a full-result export and are rejected outright
        self.assertEqual(self.query('SELECT * FROM queryapigate_it', '?stream=true&format=csv&page=2').status_code, 400)

    def test_streaming_export_is_read_only_even_with_writes_allowed(self):
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_ALLOW_WRITES': '1'}):
            res = self.query('DELETE FROM queryapigate_it', '?stream=true&format=csv')
            self.assertEqual(res.status_code, 403)
        self.assertEqual(len(self.query('SELECT * FROM queryapigate_it', '?page_size=100').get_json()), 7)

    def test_schema_lists_the_table_and_its_columns(self):
        res = self.client.get('/connections/it/schema')
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        tables = {t['name'].lower(): t for t in res.get_json()['tables']}
        self.assertIn('queryapigate_it', tables)
        self.assertEqual(tables['queryapigate_it']['type'], 'table')
        columns = {c['name'].lower() for c in tables['queryapigate_it']['columns']}
        self.assertEqual(columns, {'id', 'name', 'price', 'added'})

    def key(self, **grant):
        return {'X-API-Key': self.client.post('/api/v1/api-keys', json={
            'name': 'scoped', 'connections': ['it'], **grant}).get_json()['secret']}

    def test_a_table_grant_is_this_connections_table_not_one_of_the_same_name_elsewhere(self):
        """A key allowed queryapigate_it reads the connection's own queryapigate_it, never one of the same name in
        another schema or database - which bare-name matching let through before 0.43 - and sees only its own in the
        schema browser."""
        if self.db not in tableguard._DIALECT_MAP:
            self.skipTest(f"allowed_tables refuses every query on {self.db}")
        headers = self.key(allowed_tables=['queryapigate_it'])
        own = self.client.post('/execute_sql?page_size=100', headers=headers,
                               json={'sql': 'SELECT id FROM queryapigate_it', 'connection_name': 'it'})
        self.assertEqual(own.status_code, 200, own.get_data(as_text=True))
        self.assertEqual(len(own.get_json()), 7)
        elsewhere = self.client.post('/execute_sql', headers=headers, json={
            'sql': 'SELECT id FROM queryapigate_it_other.queryapigate_it', 'connection_name': 'it'})
        self.assertEqual((elsewhere.status_code, elsewhere.get_json()['code']), (403, 'table_not_allowed'))
        browsed = self.client.get('/connections/it/schema', headers=headers)
        self.assertEqual(browsed.status_code, 200, browsed.get_data(as_text=True))
        self.assertEqual([t['name'].lower() for t in browsed.get_json()['tables']], ['queryapigate_it'])


    def test_comments_are_imported_as_descriptions(self):
        """The database's own comments become descriptions (BACKLOG #86) - where it has a way to read them."""
        if self.db not in COMMENTS:
            res = self.client.post('/api/v1/connections/it/descriptions/import')
            self.assertEqual((res.status_code, res.get_json()['code']), (400, 'unsupported_operation'))
            return
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_ALLOW_WRITES': '1'}):
            for sql in COMMENTS[self.db]:
                self.write(sql)
        res = self.client.post('/api/v1/connections/it/descriptions/import')
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        found = {(d['table'], d['column']): (d['description'], d['source']) for d in res.get_json()['items']}
        self.assertEqual(found[('queryapigate_it', None)], ('Items for sale', 'database'))
        self.assertEqual(found[('queryapigate_it', 'price')], ('Price in EUR, VAT included', 'database'))
        tables = {t['name'].lower(): t for t in self.client.get('/connections/it/schema').get_json()['tables']}
        self.assertEqual(tables['queryapigate_it']['description'], 'Items for sale')
        price = next(c for c in tables['queryapigate_it']['columns'] if c['name'].lower() == 'price')
        self.assertEqual(price['description'], 'Price in EUR, VAT included')


    def test_masking_holds_in_this_database_s_own_sql(self):
        """A masked column never leaves raw, however this database's SQL reads it (ADR 0005): masked, or refused."""
        if self.db not in MASKED_DIALECT:
            self.skipTest(f'masking refuses {self.db} connections')
        res = self.client.post('/api/v1/api-keys', json={
            'name': 'masked', 'connections': ['it'], 'mask': [{'column': 'queryapigate_it.name', 'action': 'hash'}]})
        self.assertEqual(res.status_code, 201, res.get_data(as_text=True))
        headers = {'X-API-Key': res.get_json()['secret']}

        def run(sql):
            return self.client.post('/execute_sql', headers=headers, json={'sql': sql, 'connection_name': 'it'})

        def leaked(rows):
            return [v for row in rows for v in row.values() if isinstance(v, str) and 'Item' in v]

        for sql in MASKED_SHAPES:
            with self.subTest(sql=sql):
                res = run(sql)
                self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
                self.assertEqual(leaked(res.get_json()), [])
                self.assertTrue(res.get_json())
        star = run('SELECT * FROM queryapigate_it ORDER BY id').get_json()
        # the other columns as they are
        self.assertEqual([{k.lower(): v for k, v in r.items()}['id'] for r in star], list(range(1, 8)))
        for sql in MASKED_DIALECT[self.db]:
            with self.subTest(sql=sql):
                res = run(sql)
                if res.status_code == 403:
                    self.assertEqual(res.get_json()['code'], 'mask_unresolvable')  # refused is fine; raw never
                else:
                    self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
                    self.assertEqual(leaked(res.get_json()), [])
        hashed = {r['name'] for r in run('SELECT name FROM queryapigate_it').get_json()}
        self.assertEqual(len(hashed), 7)  # equal values stay equal and distinct ones distinct - what hash is for


    def test_an_agent_write_is_approved_runs_once_and_stops_at_its_ceiling(self):
        """An AI agent's write (ADR 0008) on this database: in a transaction, counted, committed once approved - and
        rolled back, changing nothing, when it would change more rows than its query allows."""
        from queryapigate import definitions, mcp_server

        def save(name, sql, **fields):
            return self.client.post('/api/v1/queries', json={'name': name, 'description': 'd', 'sql': sql,
                                                             'connection_name': 'it', 'publish': True, **fields})

        def call(tool, **arguments):  # with no key set, as a local client: open access answers no other
            return mcp_server.handle_call(self.client.application, '', '', '127.0.0.1', tool,
                                          arguments)['structuredContent']

        def names():
            rows = self.query('SELECT id, name FROM queryapigate_it ORDER BY id', '?page_size=100').get_json()
            return [list(r.values())[1] for r in rows]

        res = save('it_rename', 'UPDATE queryapigate_it SET name = :name WHERE id = :id', agent_writes='approval')
        if self.db not in definitions.AGENT_WRITE_DIALECTS:
            self.assertEqual(res.status_code, 400)  # not where a write can't be counted and rolled back
            return
        self.assertEqual(res.status_code, 201, res.get_data(as_text=True))
        self.assertEqual(save('it_rename_all', 'UPDATE queryapigate_it SET name = :name', agent_writes='approval',
                              max_affected_rows=3).status_code, 201)
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_ALLOW_WRITES': '1'}):
            asked = call('it_rename', name='Renamed', id=2)
            self.assertEqual(asked['status'], 'approval_required')
            self.assertEqual(names()[1], 'Item 2')
            self.client.post(f"/api/v1/write-requests/{asked['request']}/approve")
            done = call('it_rename', name='Renamed', id=2, approval=asked['request'])
            self.assertEqual((done['status'], done['rows_affected']), ('done', 1), done)
            self.assertEqual(names()[1], 'Renamed')
            call('it_rename', name='Renamed', id=2, approval=asked['request'])  # a retry: the stored result
            everything = call('it_rename_all', name='All')
            self.client.post(f"/api/v1/write-requests/{everything['request']}/approve")
            stopped = call('it_rename_all', name='All', approval=everything['request'])
        self.assertEqual(stopped['code'], 'too_many_rows_affected', stopped)
        self.assertNotIn('All', names())  # rolled back: not one of the seven rows changed
        self.assertEqual(names()[1], 'Renamed')


class DriverReadOnlyMixin:
    """Databases whose driver enforces read-only sessions should refuse writes even if the SQL guard is bypassed."""

    def test_driver_level_readonly(self):
        with self.assertRaises(Exception):  # noqa: B017 - each driver raises its own error type
            runners.RUNNERS[self.db](self.details, 'DELETE FROM queryapigate_it', None, 10, 0, True)
        rows = self.query('SELECT * FROM queryapigate_it', '?page_size=100').get_json()
        self.assertEqual(len(rows), 7)


class PostgresTests(DriverReadOnlyMixin, IntegrationBase, unittest.TestCase):
    db, env_var = 'postgres', 'QUERYAPIGATE_IT_POSTGRES'
    slow_sql = 'SELECT pg_sleep(60)'
    finite_slow_sql = 'SELECT pg_sleep(2)'
    connection_id_sql = 'SELECT pg_backend_pid() AS id'

    # ADR 0027: one message per query, read-only per transaction - a query can't carry anything into the next call

    def run_ro(self, sql, the_pool, timeout=None):
        return runners.RUNNERS['postgres'](self.details, sql, None, 10, 0, True, timeout, the_pool)

    def test_a_query_cant_turn_a_pooled_connection_writable(self):
        the_pool = pool.ConnectionPool()
        self.run_ro("SELECT set_config('default_transaction_read_only', 'off', false)", the_pool)
        with self.assertRaises(Exception):  # noqa: B017 - psycopg2's ReadOnlySqlTransaction
            self.run_ro('DELETE FROM queryapigate_it', the_pool)
        self.assertEqual(self.run_ro('SELECT COUNT(*) FROM queryapigate_it', the_pool)[1], [(7,)])
        self.assertEqual(the_pool.idle_count(), 1)  # one connection served all three

    def test_the_time_limit_holds_and_doesnt_outlive_its_query(self):
        the_pool = pool.ConnectionPool()
        with self.assertRaises(ApiError) as caught:
            self.run_ro('SELECT pg_sleep(2)', the_pool, timeout=0.3)
        self.assertEqual(caught.exception.status, 504)
        self.run_ro('SELECT 1', the_pool)  # the connection that timed out was discarded; this one is fresh
        self.assertEqual(self.run_ro('SHOW statement_timeout', the_pool)[1], [('0',)])

    def test_a_write_commits_with_its_message(self):
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_ALLOW_WRITES': '1'}):
            self.write('UPDATE queryapigate_it SET name = \'committed\' WHERE id = 1')
        rows = self.query("SELECT name FROM queryapigate_it WHERE id = 1").get_json()
        self.assertEqual(rows, [{'name': 'committed'}])

    def admin(self, *statements):
        import psycopg2
        conn = psycopg2.connect(**{('dbname' if k == 'database' else k): v for k, v in self.details.items()
                                   if k in ('host', 'port', 'user', 'password', 'database')})
        conn.autocommit = True
        with conn.cursor() as cursor:
            for statement in statements:
                cursor.execute(statement)
        conn.close()

    def decoy_schema(self):
        """queryapigate_it_billing.queryapigate_it: a table of the same name in another schema, holding ids 101-102."""
        self.admin('DROP SCHEMA IF EXISTS queryapigate_it_billing CASCADE', 'CREATE SCHEMA queryapigate_it_billing',
                   'CREATE TABLE queryapigate_it_billing.queryapigate_it (id INT)',
                   'INSERT INTO queryapigate_it_billing.queryapigate_it VALUES (101), (102)')
        self.addCleanup(self.admin, 'DROP SCHEMA IF EXISTS queryapigate_it_billing CASCADE')

    def test_a_query_cant_change_the_session_for_the_next_call(self):
        # In autocommit a session setting a query makes is committed with it; each message's RESET ALL puts back the
        # defaults - for search_path, which would reroute the next call's unqualified names, and every other one
        the_pool = pool.ConnectionPool()
        settings = "SELECT current_setting('search_path'), current_setting('TimeZone'), current_setting('DateStyle')"
        before = self.run_ro(settings, the_pool)[1]
        self.run_ro("SELECT set_config('search_path', 'queryapigate_it_billing', false), "
                    "set_config('TimeZone', 'Asia/Tokyo', false), set_config('DateStyle', 'SQL, DMY', false)", the_pool)
        self.assertEqual(self.run_ro(settings, the_pool)[1], before)
        self.assertEqual(the_pool.idle_count(), 1)  # the same connection, both times
        self.assertEqual(self.run_ro("SELECT DATE '2024-01-31' AS d", the_pool)[1][0][0].isoformat(), '2024-01-31')

    def test_a_table_restricted_call_reads_the_schema_its_grant_was_checked_against(self):
        # The role's own search_path puts the decoy schema first: an unrestricted call honours it, as before; a
        # table-restricted one is pinned to public, where tableguard resolved its unqualified queryapigate_it
        self.decoy_schema()
        self.admin('ALTER ROLE CURRENT_USER SET search_path = queryapigate_it_billing, public')
        self.addCleanup(self.admin, 'ALTER ROLE CURRENT_USER RESET search_path')
        the_pool = pool.ConnectionPool()
        sql = 'SELECT min(id) FROM queryapigate_it'
        self.assertEqual(self.run_ro(sql, the_pool)[1], [(101,)])
        pinned = runners.RUNNERS['postgres'](self.details, sql, None, 10, 0, True, None, the_pool, search_path='public')
        self.assertEqual(pinned[1], [(1,)])
        self.assertEqual(self.run_ro(sql, the_pool)[1], [(101,)])  # the pin was LOCAL: gone with its message
        headers = self.key(allowed_tables=['queryapigate_it'])
        decoy = self.client.post('/execute_sql', headers=headers, json={
            'sql': 'SELECT id FROM queryapigate_it_billing.queryapigate_it', 'connection_name': 'it'})
        self.assertEqual(decoy.get_json()['code'], 'table_not_allowed')


class MySQLTests(DriverReadOnlyMixin, IntegrationBase, unittest.TestCase):
    db, env_var = 'mysql', 'QUERYAPIGATE_IT_MYSQL'
    finite_slow_sql = 'SELECT SLEEP(2) AS s'
    connection_id_sql = 'SELECT CONNECTION_ID() AS id'
    # (SLEEP() and BENCHMARK() are cut short by MySQL's limit but return normally instead of raising an error)
    slow_sql = 'SELECT COUNT(*) FROM ' + ', '.join(f'information_schema.columns c{i}' for i in range(4))

    # ADR 0027: read-only per transaction (ROLLBACK AND CHAIN), the time limit on the statement - so a stored
    # function a query calls can't turn a pooled connection writable, or lift the limit, for the calls after it

    def admin(self):
        import mysql.connector
        fields = {k: v for k, v in self.details.items() if k in ('host', 'port', 'user', 'password', 'database')}
        return mysql.connector.connect(autocommit=True, **fields)

    def flip_function(self):
        conn = self.admin()
        cursor = conn.cursor()
        cursor.execute('DROP FUNCTION IF EXISTS queryapigate_it_flip')
        cursor.execute('CREATE FUNCTION queryapigate_it_flip() RETURNS INT DETERMINISTIC SQL SECURITY INVOKER '
                       'BEGIN SET SESSION TRANSACTION READ WRITE; SET SESSION transaction_read_only = 0; '
                       'SET SESSION max_execution_time = 0; RETURN 1; END')
        conn.close()

        def drop():
            conn = self.admin()
            conn.cursor().execute('DROP FUNCTION IF EXISTS queryapigate_it_flip')
            conn.close()
        self.addCleanup(drop)

    def run_ro(self, sql, the_pool, timeout=None):
        return runners.RUNNERS['mysql'](self.details, sql, None, 10, 0, True, timeout, the_pool)

    def test_a_stored_function_cant_turn_a_pooled_connection_writable(self):
        self.flip_function()
        the_pool = pool.ConnectionPool()
        self.assertEqual(self.run_ro('SELECT queryapigate_it_flip() AS f', the_pool)[1], [(1,)])
        with self.assertRaises(Exception):  # noqa: B017 - mysql-connector's error 1792, read-only transaction
            self.run_ro('DELETE FROM queryapigate_it', the_pool)
        self.assertEqual(self.run_ro('SELECT COUNT(*) FROM queryapigate_it', the_pool)[1], [(7,)])

    def test_a_stored_function_cant_lift_the_time_limit(self):
        self.flip_function()
        the_pool = pool.ConnectionPool()
        # The same limit both times: a driver that only sets the session's limit when it changes would leave the 0
        # the function set in place for the second call
        self.run_ro('SELECT queryapigate_it_flip() AS f', the_pool, timeout=0.5)
        with self.assertRaises(ApiError) as caught:
            self.run_ro(self.slow_sql, the_pool, timeout=0.5)
        self.assertEqual(caught.exception.status, 504)

    def test_a_table_grant_doesnt_reach_another_database_and_a_per_service_user_holds_without_one(self):
        """PERFORMANCE_MYSQL_V1.md, Isolating services: one user for every service's database, and a decoy billing table
        of the same name. The grant refuses it now; the workaround the 0.42 advisory gives - a user per service, as its
        own connection - refuses it in MySQL itself, whatever the key."""
        admin = self.admin()
        cursor = admin.cursor()
        for statement in ('DROP DATABASE IF EXISTS queryapigate_it_billing', 'CREATE DATABASE queryapigate_it_billing',
                          'CREATE TABLE queryapigate_it_billing.queryapigate_it (id INT)',
                          'INSERT INTO queryapigate_it_billing.queryapigate_it VALUES (101)',
                          "DROP USER IF EXISTS 'queryapigate_it_crm'@'%'",
                          "CREATE USER 'queryapigate_it_crm'@'%' IDENTIFIED BY 'crm-pw'",
                          f"GRANT SELECT ON `{self.details['database']}`.* TO 'queryapigate_it_crm'@'%'"):
            cursor.execute(statement)
        admin.close()

        def drop():
            conn = self.admin()
            for statement in ('DROP DATABASE IF EXISTS queryapigate_it_billing',
                              "DROP USER IF EXISTS 'queryapigate_it_crm'@'%'"):
                conn.cursor().execute(statement)
            conn.close()
        self.addCleanup(drop)
        write_connections({'it': self.details,
                           'crm': {**self.details, 'user': 'queryapigate_it_crm', 'password': 'crm-pw'}})
        decoy = 'SELECT id FROM queryapigate_it_billing.queryapigate_it'
        restricted, unrestricted = self.key(allowed_tables=['queryapigate_it']), {'X-API-Key': 'admin'}
        patcher = mock.patch.dict(os.environ, {'QUERYAPIGATE_API_KEY': 'admin'})  # the admin key: no restriction
        patcher.start()
        self.addCleanup(patcher.stop)
        refused = self.client.post('/execute_sql', headers=restricted, json={'sql': decoy, 'connection_name': 'it'})
        self.assertEqual((refused.status_code, refused.get_json()['code']), (403, 'table_not_allowed'))
        shared = self.client.post('/execute_sql', headers=unrestricted, json={'sql': decoy, 'connection_name': 'it'})
        self.assertEqual(shared.status_code, 200)  # the shared user can read it: only the grant stopped it
        own = self.client.post('/execute_sql', headers=unrestricted,
                               json={'sql': 'SELECT id FROM queryapigate_it', 'connection_name': 'crm'})
        self.assertEqual(own.status_code, 200, own.get_data(as_text=True))
        denied = self.client.post('/execute_sql', headers=unrestricted, json={'sql': decoy, 'connection_name': 'crm'})
        self.assertGreaterEqual(denied.status_code, 400)
        self.assertIn('denied', denied.get_data(as_text=True).lower())

    def test_an_idle_pooled_connection_doesnt_block_ddl(self):
        the_pool = pool.ConnectionPool()
        self.run_ro('SELECT COUNT(*) FROM queryapigate_it', the_pool)  # now idle, inside its chained transaction
        self.assertEqual(the_pool.idle_count(), 1)
        conn = self.admin()
        cursor = conn.cursor()
        cursor.execute('SET SESSION lock_wait_timeout = 5')
        started = time.monotonic()
        cursor.execute('ALTER TABLE queryapigate_it ADD COLUMN queryapigate_extra INT')
        self.assertLess(time.monotonic() - started, 3)
        conn.close()


class ClickHouseTests(DriverReadOnlyMixin, IntegrationBase, unittest.TestCase):
    db, env_var = 'clickhouse', 'QUERYAPIGATE_IT_CLICKHOUSE'
    slow_sql = 'SELECT count() FROM numbers(100000000000)'
    finite_slow_sql = 'SELECT sleep(2) AS s'


class H2Tests(IntegrationBase, unittest.TestCase):
    # H2's JDBC read-only flag is only a hint, so it relies on the SQL guard (covered by test_writes_are_blocked).
    db, env_var = 'h2', 'QUERYAPIGATE_IT_H2'
    slow_sql = 'SELECT COUNT(*) FROM SYSTEM_RANGE(1, 100000) a, SYSTEM_RANGE(1, 100000) b WHERE (a.x * b.x) % 7 = 3'
    connection_id_sql = 'SELECT SESSION_ID() AS id'


class JdbcViaH2Tests(IntegrationBase, unittest.TestCase):
    """Verifies the *generic* jdbc driver end-to-end against a real database. H2 stands in for an arbitrary
    JDBC-reachable vendor here, since this project has no vendor database of its own to test against; the
    generic driver contains no H2-specific code at all - it only ever sees `jar`, `driver_class` and
    `jdbc_url`, built here from the same real H2 server H2Tests already uses - so this is real evidence the
    driver works against any JDBC-compliant database, vendor SQL dialect differences aside.
    """
    db, env_var = 'jdbc', 'QUERYAPIGATE_IT_H2'
    connection_id_sql = 'SELECT SESSION_ID() AS id'

    @classmethod
    def setUpClass(cls):
        raw = os.environ.get(cls.env_var)
        if not raw:
            raise unittest.SkipTest(f'{cls.env_var} not set')
        h2 = json.loads(raw)
        host = h2.get('host') or 'localhost'
        if h2.get('port'):
            host = f"{host}:{h2['port']}"
        cls.details = {
            'db': 'jdbc', 'active': True, 'user': h2.get('user'), 'password': h2.get('password'),
            'driver_class': 'org.h2.Driver', 'jar': config.h2_jar(),
            'jdbc_url': f"jdbc:h2:tcp://{host}/~/{h2.get('database')}",
        }

    def test_query_timeout_cancels_the_statement_and_keeps_the_service_usable(self):
        self.skipTest('generic jdbc connections have no native query-timeout cancellation (documented limitation)')

    def test_read_only_mode_holds_on_a_reused_connection(self):
        # Like H2, a jdbc connection's read-only flag is advisory only - the guard is what actually blocks
        # writes (covered unchanged by the inherited test_writes_are_blocked).
        shared = pool.get_pool()
        self.assertIsNotNone(shared)
        run = runners.RUNNERS[self.db]
        run(self.details, 'SELECT * FROM queryapigate_it', None, 10, 0, True, 5, shared)
        self.assertEqual(len(self.query('SELECT * FROM queryapigate_it', '?page_size=100').get_json()), 7)

    def test_schema_lists_the_table_and_its_columns(self):
        # Schema introspection isn't implemented for the generic jdbc dialect (see schema.py) - this
        # confirms that boundary is a clean, documented 400 through the real HTTP stack, not a crash.
        res = self.client.get('/connections/it/schema')
        self.assertEqual(res.status_code, 400)
        self.assertIn("isn't supported", res.get_json()['error'])


class DuckDBTests(IntegrationBase, unittest.TestCase):
    """DuckDB is embedded (no server to run), so unlike the drivers above this needs no QUERYAPIGATE_IT_* env
    var - only the optional `duckdb` package, which the dev/all extra already installs. It still runs
    through the same real driver and the same shared test suite as every other dialect.
    """
    db = 'duckdb'
    # A CPU-bound query, not a sleep: DuckDB only gained sleep_ms() in 1.5, and the duckdb extra allows older
    # releases. Verified interruptible on both 1.4.5 and 1.5.5.
    slow_sql = ('WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c WHERE x < 100000000) '
                'SELECT count(*) FROM c')
    # No reliable, version-stable ~2-second primitive to calibrate against (see slow_sql above), so - like
    # H2 - test_a_request_limit_does_not_leak_to_the_next_user_of_the_connection self-skips for DuckDB too.
    finite_slow_sql = None
    # No built-in per-connection identifier exists to query (DuckDB is embedded, not client/server), so the
    # two tests that assert on it (test_connections_are_reused_when_pooled and
    # test_every_request_gets_its_own_connection_when_pooling_is_disabled) self-skip, same as ClickHouse.
    connection_id_sql = None

    @classmethod
    def setUpClass(cls):
        try:
            import duckdb  # noqa: F401
        except ImportError:
            raise unittest.SkipTest(
                "the 'duckdb' package is not installed (pip install queryapigate[duckdb])") from None

    def _prepare_storage(self):
        # An absolute path unique to *this test's* tempdir - not a fixed name shared across the class -
        # because the connection pool keys purely on connection *details*, not on the file's actual
        # content or QUERYAPIGATE_HOME: a fixed relative filename would fingerprint identically across tests
        # despite each test's setUp() pointing it at a brand new (and, after the previous test's tempdir
        # cleanup, since-deleted) file, so a later test could be silently served a stale pooled connection
        # to a file that no longer exists. Real deployments hit the same staleness window far more rarely,
        # from hand-editing db_connections.json - see "Changes take effect" in
        # documentation/DATABASE_CONNECTION_CONFIGURATION.md.
        import duckdb
        path = os.path.join(self.tmp.name, 'queryapigate_it.duckdb')
        duckdb.connect(path).close()
        self.details = {'db': 'duckdb', 'database': path, 'active': True}

    def test_read_only_mode_holds_on_a_reused_connection(self):
        # Like H2/jdbc, DuckDB's read-only flag is advisory only here (see runners._DuckDB) - the guard is
        # what actually blocks writes (covered unchanged by the inherited test_writes_are_blocked).
        shared = pool.get_pool()
        self.assertIsNotNone(shared)
        run = runners.RUNNERS[self.db]
        run(self.details, 'SELECT * FROM queryapigate_it', None, 10, 0, True, 5, shared)
        self.assertEqual(len(self.query('SELECT * FROM queryapigate_it', '?page_size=100').get_json()), 7)


class TrinoTests(IntegrationBase, unittest.TestCase):
    """Trino (experimental, ADR 0026): run against a real Trino, writing to its in-memory `memory` catalog. No
    driver read-only (the SQL guard alone), and no stable per-request connection id (stateless HTTP), so the
    pooling-identity tests self-skip, as for DuckDB. slow_sql is a federated cross join of the generated tpch data."""
    db, env_var = 'trino', 'QUERYAPIGATE_IT_TRINO'
    slow_sql = 'SELECT count(*) FROM tpch.sf1000.orders a, tpch.sf1000.orders b'
    finite_slow_sql = None  # no version-stable ~2-second primitive, as for DuckDB
    connection_id_sql = None  # Trino is stateless HTTP - no per-connection id

    def test_allowed_tables_is_qualified_across_catalogs(self):
        """A bare grant reaches only the connection's own catalog/schema, never the same table name elsewhere in
        the federation (ADR 0026) - the behaviour that graduates Trino's allowed_tables."""
        secret = self.client.post('/api/v1/api-keys', json={
            'name': 'scoped', 'connections': ['it'], 'allowed_tables': ['queryapigate_it']}).get_json()['secret']
        headers = {'X-API-Key': secret}
        own = self.client.post('/execute_sql?page_size=1', headers=headers,
                               json={'sql': 'SELECT id FROM queryapigate_it', 'connection_name': 'it'})
        self.assertEqual(own.status_code, 200, own.get_data(as_text=True))  # memory.default.queryapigate_it: granted
        elsewhere = self.client.post('/execute_sql', headers=headers,
                                     json={'sql': 'SELECT name FROM tpch.tiny.nation', 'connection_name': 'it'})
        self.assertEqual(elsewhere.status_code, 403)  # a different catalog/schema is not the grant
        self.assertEqual(elsewhere.get_json()['code'], 'table_not_allowed')


class MongoTests(unittest.TestCase):
    """MongoDB against a real server (QUERYAPIGATE_IT_MONGO): find() through /execute_mongo and saved queries, the
    time limit, allowed_tables as collections, and the refusals that keep a find() a find() - each sent to the real
    server's parser, not a mock's - and streams (ADR 0022) through real cursors. Not IntegrationBase: there is no SQL
    to run."""
    db, env_var = 'mongo', 'QUERYAPIGATE_IT_MONGO'

    @classmethod
    def setUpClass(cls):
        raw = os.environ.get(cls.env_var)
        if not raw:
            raise unittest.SkipTest(f'{cls.env_var} not set')
        cls.details = {**json.loads(raw), 'db': cls.db, 'active': True}

    def setUp(self):
        import pymongo
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': self.tmp.name, 'QUERYAPIGATE_API_KEY': 'admin'})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(history.flush)  # runs first: batched runs land in this home, not while the next opens its own
        write_connections({'it': self.details})
        self.client = create_app().test_client()
        self.admin = {'X-API-Key': 'admin'}
        server = pymongo.MongoClient(self.details['host'], int(self.details.get('port') or 27017))
        self.addCleanup(server.close)
        self.mongo = server[self.details['database']]
        for name in ('queryapigate_it', 'queryapigate_it_secret', 'queryapigate_it_slow', 'queryapigate_it_many'):
            self.mongo.drop_collection(name)
            self.addCleanup(self.mongo.drop_collection, name)
        self.mongo.queryapigate_it.insert_many([{'id': i, 'name': f'Item {i}', 'price': i + 0.5} for i in range(1, 8)])
        self.mongo.queryapigate_it_secret.insert_one({'token': 'tok_live_123'})

    def find(self, headers=None, query='', **body):
        return self.client.post(f'/execute_mongo{query}', headers=headers or self.admin,
                                json={'connection_name': 'it', 'collection': 'queryapigate_it', **body})

    def key(self, **grants):
        res = self.client.post('/api/v1/api-keys', headers=self.admin, json={'name': 'partner', **grants})
        self.assertEqual(res.status_code, 201, res.get_data(as_text=True))
        return {'X-API-Key': res.get_json()['secret']}

    def test_a_find_filters_projects_sorts_and_pages(self):
        res = self.find(query='?page_size=2', filter={'price': {'$gte': 3}}, projection={'_id': 0, 'id': 1},
                        sort={'id': -1})
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        self.assertEqual(res.get_json(), [{'id': 7}, {'id': 6}])
        self.assertEqual(res.headers['X-Has-More'], 'true')

    def test_a_saved_query_binds_a_typed_parameter(self):
        self.assertEqual(save_query(self.client, headers=self.admin, body={
            'filename': 'by_id', 'author': 'a', 'description': 'd', 'query_type': 'mongo',
            'mongo_collection': 'queryapigate_it', 'mongo_filter': {'id': ':id'},
            'mongo_projection': {'_id': 0, 'name': 1}, 'query_parameters': {'id': 'int'},
            'connection_name': 'it'}).status_code, 201)
        res = self.client.get('/q/by_id?id=3', headers=self.admin)
        self.assertEqual((res.status_code, res.get_json()), (200, [{'name': 'Item 3'}]))

    def test_the_time_limit_cancels_a_find(self):
        self.mongo.queryapigate_it_slow.insert_many([{'s': 'x' * 40 + str(i)} for i in range(100_000)])
        res = self.find(collection='queryapigate_it_slow', filter={'s': {'$regex': 'y$'}}, timeout=0.001)
        self.assertEqual((res.status_code, res.get_json()['code']), (504, 'query_timeout'))
        unlimited = self.find(collection='queryapigate_it_slow', filter={'s': {'$regex': 'y$'}})
        self.assertEqual(unlimited.status_code, 200)  # the same find, given its time, simply finds nothing

    def test_allowed_tables_are_the_collections_a_key_may_read(self):
        partner = self.key(connections=['it'], allowed_tables=['queryapigate_it'])
        self.assertEqual(self.find(partner).status_code, 200)
        refused = self.find(partner, collection='queryapigate_it_secret')
        self.assertEqual((refused.status_code, refused.get_json()['code']), (403, 'table_not_allowed'))
        self.assertNotIn('tok_live', refused.get_data(as_text=True))

    def test_server_side_javascript_and_injected_operators_never_reach_the_server(self):
        js = {'$function': {'body': 'function() { return true; }', 'args': [], 'lang': 'js'}}
        for body in ({'filter': {'$where': 'true'}}, {'filter': {'$expr': js}}, {'projection': {'x': js}},
                     {'sort': {'x': js}}):
            with self.subTest(body=body):
                res = self.find(**body)
                self.assertEqual((res.status_code, res.get_json()['code']), (403, 'mongo_operator_forbidden'))
        injected = self.find(filter={'name': ':u'}, params={'u': {'$ne': None}})
        self.assertEqual((injected.status_code, injected.get_json()['code']), (400, 'param_invalid'))

    def many(self, count=2500):
        """More documents than a cursor batch (runners.MONGO_STREAM_BATCH) and the CSV lookahead - the last one with
        a field none before it has."""
        docs = [{'n': i, 'name': f'Doc {i}', 'tags': [i, i + 1]} for i in range(count)]
        docs[-1]['late'] = 'only here'
        self.mongo.queryapigate_it_many.insert_many(docs)
        self.assertEqual(save_query(self.client, headers=self.admin, body={
            'filename': 'many', 'author': 'a', 'description': 'd', 'query_type': 'mongo',
            'mongo_collection': 'queryapigate_it_many', 'mongo_filter': {}, 'mongo_projection': {'_id': 0},
            'mongo_sort': {'n': 1}, 'connection_name': 'it'}).status_code, 201)

    def test_a_find_streams_in_every_format_across_cursor_batches(self):
        import csv
        import io

        import duckdb
        self.many()
        res = self.client.get('/q/many?stream=true&format=ndjson', headers=self.admin)
        lines = res.get_data(as_text=True).splitlines()
        self.assertEqual((res.status_code, len(lines)), (200, 2500))
        self.assertEqual(json.loads(lines[-1]), {'n': 2499, 'name': 'Doc 2499', 'tags': [2499, 2500],
                                                 'late': 'only here'})
        res = self.client.get('/q/many?stream=true&format=csv', headers=self.admin)
        rows = list(csv.reader(io.StringIO(res.get_data(as_text=True))))
        self.assertEqual((rows[0], rows[1], len(rows)), (['n', 'name', 'tags'], ['0', 'Doc 0', '[0, 1]'], 2501))
        history.flush()
        self.assertEqual(history.search(query='many')[0][0]['fields_dropped'], 1)  # `late`, after the first 1,000
        res = self.client.get('/q/many?stream=true&format=parquet', headers=self.admin)
        path = os.path.join(self.tmp.name, 'many.parquet')
        with open(path, 'wb') as f:
            f.write(res.get_data())
        found = duckdb.sql(f"SELECT count(*), count(late), max(n) FROM '{path}'").fetchall()  # 2.0: no fetchone()
        self.assertEqual(found, [(2500, 1, 2499)])

    def test_a_streamed_find_keeps_to_the_time_limit(self):
        self.mongo.queryapigate_it_slow.insert_many([{'s': 'x' * 40 + str(i)} for i in range(100_000)])
        res = self.find(query='?stream=true&format=ndjson', collection='queryapigate_it_slow',
                        filter={'s': {'$regex': 'y$'}}, timeout=0.001)
        self.assertEqual((res.status_code, res.get_json()['code']), (504, 'query_timeout'))

    def test_a_client_that_leaves_early_closes_the_cursor_on_the_server(self):
        self.many()

        def open_cursors():
            return self.mongo.command('serverStatus')['metrics']['cursor']['open']['total']

        before = open_cursors()
        res = self.client.get('/q/many?stream=true&format=ndjson', headers=self.admin, buffered=False)
        first = next(iter(res.response))
        self.assertIn(b'Doc 0', first)
        self.assertEqual(open_cursors(), before + 1)  # 2,500 documents: more batches to come
        res.close()
        self.assertEqual(open_cursors(), before)

    def test_the_schema_lists_collections_and_the_server_its_databases(self):
        res = self.client.get('/connections/it/schema', headers=self.admin)
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        self.assertIn('queryapigate_it', {t['name'] for t in res.get_json()['tables']})
        listed = self.client.post('/api/v1/connections/databases', headers=self.admin, json=self.details)
        self.assertEqual(listed.status_code, 200, listed.get_data(as_text=True))
        self.assertIn(self.details['database'], json.dumps(listed.get_json()))
        tested = self.client.post('/api/v1/connections/test', headers=self.admin, json=self.details)
        self.assertEqual(tested.status_code, 200, tested.get_data(as_text=True))


if __name__ == '__main__':
    unittest.main()

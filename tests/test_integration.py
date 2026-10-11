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

from queryapigate import config, create_app, pool, runners
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
INSERT = {
    'postgres': "INSERT INTO queryapigate_it VALUES ({i}, 'Item {i}', {i}.50, '2024-01-{i:02d} 10:00:00')",
    'mysql': "INSERT INTO queryapigate_it VALUES ({i}, 'Item {i}', {i}.50, '2024-01-{i:02d} 10:00:00')",
    'h2': "INSERT INTO queryapigate_it VALUES ({i}, 'Item {i}', {i}.50, TIMESTAMP '2024-01-{i:02d} 10:00:00')",
    'clickhouse': "INSERT INTO queryapigate_it VALUES ({i}, 'Item {i}', {i}.50, '2024-01-{i:02d} 10:00:00')",
}
INSERT['jdbc'] = INSERT['h2']
INSERT['duckdb'] = INSERT['postgres']
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


class MySQLTests(DriverReadOnlyMixin, IntegrationBase, unittest.TestCase):
    db, env_var = 'mysql', 'QUERYAPIGATE_IT_MYSQL'
    finite_slow_sql = 'SELECT SLEEP(2) AS s'
    connection_id_sql = 'SELECT CONNECTION_ID() AS id'
    # (SLEEP() and BENCHMARK() are cut short by MySQL's limit but return normally instead of raising an error)
    slow_sql = 'SELECT COUNT(*) FROM ' + ', '.join(f'information_schema.columns c{i}' for i in range(4))


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


if __name__ == '__main__':
    unittest.main()

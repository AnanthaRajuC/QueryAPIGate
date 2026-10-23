"""Column masking's core (ADR 0005, masking.py): rules, tracing each result column to its source columns - through
aliases, expressions, CTEs, subqueries, unions and joins, by position - refusing what can't be traced, and the four
actions. Applying it to requests is tested with the routes that use it."""
import os
import re
import sqlite3
import tempfile
import unittest
from unittest import mock

import duckdb

from queryapigate import create_app, masking, schema
from tests.helpers import write_connections

TABLES = [
    {'name': 'customers', 'type': 'table', 'columns': [{'name': c} for c in ('id', 'email', 'phone', 'name')]},
    {'name': 'orders', 'type': 'table', 'columns': [{'name': c} for c in ('id', 'customer_id', 'total')]},
    {'name': 'customer_view', 'type': 'view', 'columns': [{'name': c} for c in ('id', 'email')]},
]
RULES = masking.validate_rules([{'column': 'customers.email', 'action': 'hash'},
                                {'column': '*.phone', 'action': 'mask'},
                                {'column': 'orders.total', 'action': 'null'}])


def sources(sql, dialect='postgres', tables=TABLES):
    return [(name, sorted(map(repr, found))) for name, found in masking.trace(sql, dialect, tables)]


def actions(sql, dialect='postgres', rules=RULES, tables=TABLES):
    return masking.plan(rules, masking.trace(sql, dialect, tables))[0]


class RuleTests(unittest.TestCase):
    def test_valid_rules(self):
        self.assertEqual(masking.validate_rules([{'column': 'Public.Customers.EMAIL', 'action': 'drop'}]),
                         [{'column': 'public.customers.email', 'action': 'drop'}])
        self.assertIsNone(masking.validate_rules(None))

    def test_bad_rules(self):
        for rules in ('customers.email', [{'column': 'email', 'action': 'hash'}],
                      [{'column': 'a.b.c.d', 'action': 'hash'}], [{'column': 'customers.email', 'action': 'blur'}],
                      [{'column': 'customers.email'}], [{'column': 'customers.email', 'action': 'hash', 'x': 1}],
                      [{'column': "c.e'; DROP", 'action': 'hash'}]):
            with self.subTest(rules=rules), self.assertRaises(Exception) as caught:
                masking.validate_rules(rules)
            self.assertEqual(caught.exception.code, 'invalid_body')

    def test_fingerprint_is_stable_and_order_free(self):
        a = masking.validate_rules([{'column': 'a.b', 'action': 'hash'}, {'column': 'c.d', 'action': 'drop'}])
        self.assertEqual(masking.fingerprint(a), masking.fingerprint(list(reversed(a))))
        self.assertNotEqual(masking.fingerprint(a), masking.fingerprint(a[:1]))
        self.assertIsNone(masking.fingerprint([]))


class TraceTests(unittest.TestCase):
    def test_what_a_result_column_is_made_of(self):
        cases = {
            'SELECT email AS e, id FROM customers': [('e', ['customers.email']), ('id', ['customers.id'])],
            "SELECT lower(email) || '-' || upper(name) AS x FROM customers": [('x', ['customers.email',
                                                                                  'customers.name'])],
            'WITH a AS (SELECT email AS x FROM customers), b AS (SELECT x AS y FROM a) SELECT y AS z FROM b':
                [('z', ['customers.email'])],
            'SELECT t.m FROM (SELECT max(email) AS m FROM customers) t': [('m', ['customers.email'])],
            'SELECT name FROM customers UNION ALL SELECT email FROM customers': [('name', ['customers.email',
                                                                                         'customers.name'])],
            'SELECT (SELECT max(c.email) FROM customers c) AS m': [('m', ['customers.email'])],
            'SELECT CASE WHEN id > 1 THEN email ELSE phone END AS v FROM customers':
                [('v', ['customers.email', 'customers.id', 'customers.phone'])],
            'SELECT 1 AS one, count(*) AS n, NULL AS nothing FROM customers': [('one', []), ('n', []), ('nothing', [])],
            "SELECT * FROM (VALUES (1, 'a')) AS v(a, b)": [('a', []), ('b', [])],
            'SELECT customer_id, RANK() OVER (ORDER BY COUNT(*) DESC) AS r, ROW_NUMBER() OVER () AS n '
            'FROM orders GROUP BY customer_id': [('customer_id', ['orders.customer_id']), ('r', []), ('n', [])],
            'SELECT RANK() OVER (ORDER BY email) AS r FROM customers': [('r', ['customers.email'])],
            'SELECT phone FROM customers WHERE email = :e AND id > {min}': [('phone', ['customers.phone'])],
        }
        for sql, expected in cases.items():
            with self.subTest(sql=sql):
                self.assertEqual(sources(sql), expected)

    def test_columns_are_traced_by_position_not_name(self):
        # two result columns named id: each keeps its own source
        self.assertEqual(sources('SELECT * FROM customers c JOIN orders o ON o.customer_id = c.id'),
                         [('id', ['customers.id']), ('email', ['customers.email']), ('phone', ['customers.phone']),
                          ('name', ['customers.name']), ('id', ['orders.id']), ('customer_id', ['orders.customer_id']),
                          ('total', ['orders.total'])])
        self.assertEqual(sources('SELECT o.total AS email, c.email AS total FROM orders o JOIN customers c '
                                 'ON c.id = o.customer_id'),
                         [('email', ['orders.total']), ('total', ['customers.email'])])
        self.assertEqual(sources('SELECT a.email, b.phone FROM customers a JOIN customers b ON a.id = b.id'),
                         [('email', ['customers.email']), ('phone', ['customers.phone'])])

    def test_each_dialect(self):
        for dialect, sql in (('mysql', 'SELECT `email` AS e FROM `customers`'),
                             ('sqlite', 'SELECT "email" AS e FROM customers'),
                             ('duckdb', 'SELECT email AS e FROM customers'),
                             ('clickhouse', 'SELECT email AS e FROM customers'),
                             ('postgres', 'SELECT "email" AS e FROM public.customers')):
            with self.subTest(dialect=dialect):
                self.assertEqual(sources(sql, dialect), [('e', ['customers.email'])])

    def test_statements_without_table_data(self):
        for sql in ('SHOW TABLES', 'DESCRIBE customers', 'EXPLAIN SELECT email FROM customers'):
            with self.subTest(sql=sql):
                self.assertIsNone(masking.trace(sql, 'duckdb', TABLES))

    def test_tricks_that_would_hide_a_column_are_refused(self):
        # each of these once came out traced to nothing, or to the wrong column - and so unmasked
        for sql, dialect in (("SELECT COLUMNS('e.*') FROM customers", 'duckdb'),     # columns picked by a pattern
                             ('SELECT e FROM customers AS c(a, e)', 'postgres'),     # e renamed from email
                             ('SELECT * FROM customers NATURAL JOIN orders', 'postgres'),  # fewer columns returned
                             ('SELECT * FROM customers JOIN orders USING (id)', 'postgres'),
                             ('SELECT * FROM (VALUES ((SELECT email FROM customers))) AS v(x)', 'postgres'),
                             ('SELECT c FROM customers c', 'postgres'),              # the whole row as one value
                             ('SELECT row_to_json(c) AS j FROM customers c', 'postgres'),
                             ('SELECT to_json(c) AS j FROM customers c', 'duckdb'),
                             ('SELECT array_agg(c) AS a FROM customers c', 'postgres'),
                             ('SELECT * FROM customers, LATERAL (SELECT email AS e2) l', 'postgres'),
                             ('WITH RECURSIVE r(n, e) AS (SELECT 1, email FROM customers UNION ALL '
                              'SELECT n + 1, e FROM r WHERE n < 3) SELECT e FROM r', 'postgres'),
                             ('SELECT version() AS v', 'postgres')):                 # a function it can't see into
            with self.subTest(sql=sql), self.assertRaises(masking.ApiError) as caught:
                masking.trace(sql, dialect, TABLES)
            self.assertEqual(caught.exception.code, 'mask_unresolvable')
        # ... while naming the columns of the same joins is fine
        self.assertEqual(sources('SELECT c.email FROM customers c JOIN orders USING (id)'),
                         [('email', ['customers.email'])])

    def test_the_result_must_have_the_traced_number_of_columns(self):
        traced = masking.trace('SELECT id, email FROM customers', 'postgres', TABLES)
        masking.check_columns(traced, ['id', 'email'])
        with self.assertRaises(masking.ApiError) as caught:
            masking.check_columns(traced, ['id'])
        self.assertEqual(caught.exception.code, 'mask_unresolvable')

    def test_what_cant_be_traced_is_refused(self):
        for sql, dialect in (('SELECT * FROM nowhere', 'postgres'),
                             ('SELECT email FROM nowhere', 'postgres'),
                             ("SELECT * FROM read_parquet('/data/*.parquet')", 'duckdb'),
                             ("SELECT email FROM read_csv('/data/c.csv')", 'duckdb'),
                             ('SELECT FROM WHERE', 'postgres'),
                             ('SELECT email FROM customers', 'h2'),
                             ('SELECT email FROM customers', 'jdbc')):
            with self.subTest(sql=sql, dialect=dialect), self.assertRaises(Exception) as caught:
                masking.trace(sql, dialect, TABLES)
            self.assertEqual(caught.exception.code, 'mask_unresolvable')


class PlanTests(unittest.TestCase):
    def test_a_rule_follows_the_data_whatever_it_is_called(self):
        self.assertEqual(actions('SELECT email AS e, lower(email) AS l, id FROM customers'), ['hash', 'hash', None])
        self.assertEqual(actions('SELECT phone FROM customers UNION SELECT name FROM customers'), ['mask'])
        self.assertEqual(actions('SELECT o.total AS email, c.email AS total FROM orders o JOIN customers c '
                                 'ON c.id = o.customer_id'), ['null', 'hash'])

    def test_the_strictest_rule_wins(self):
        rules = masking.validate_rules([{'column': 'customers.*', 'action': 'hash'},
                                        {'column': 'customers.email', 'action': 'drop'},
                                        {'column': '*.email', 'action': 'mask'}])
        self.assertEqual(actions("SELECT email, email || name AS both_ FROM customers", rules=rules),
                         ['drop', 'drop'])
        self.assertEqual(actions('SELECT name FROM customers', rules=rules), ['hash'])

    def test_schema_qualified_rules(self):
        rules = masking.validate_rules([{'column': 'sales.customers.email', 'action': 'hash'}])
        self.assertEqual(actions('SELECT email FROM sales.customers', rules=rules), ['hash'])
        self.assertEqual(actions('SELECT email FROM other.customers', rules=rules), [None])

    def test_a_view_needs_a_rule_of_its_own(self):
        with self.assertRaises(Exception) as caught:
            actions('SELECT email FROM customer_view')
        self.assertEqual(caught.exception.code, 'mask_unresolvable')
        self.assertIn("'keep'", caught.exception.message)
        self.assertEqual(actions('SELECT id FROM customer_view'), [None])  # no rule names an id column
        wildcard = masking.validate_rules([{'column': '*.email', 'action': 'hash'}])
        self.assertEqual(actions('SELECT email FROM customer_view', rules=wildcard), ['hash'])
        keep = RULES + masking.validate_rules([{'column': 'customer_view.email', 'action': 'keep'}])
        self.assertEqual(actions('SELECT email FROM customer_view', rules=keep), [None])

    def test_what_fired_is_reported(self):
        _, fired = masking.plan(RULES, masking.trace('SELECT email AS e, id FROM customers', 'postgres', TABLES))
        self.assertEqual(fired, [{'column': 'e', 'action': 'hash', 'source': 'customers.email'}])


class ApplyTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {'QUERYAPIGATE_MASK_KEY': 'k1'})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_actions(self):
        columns, rows = masking.apply(
            ['id', 'email', 'phone', 'total', 'secret'],
            iter([(1, 'jane@example.com', '+44 7700 900123', 10, 'x'), (2, None, 'ab', 5, 'y')]),
            [None, 'hash', 'mask', 'null', 'drop'])
        rows = list(rows)
        self.assertEqual(columns, ['id', 'email', 'phone', 'total'])
        self.assertTrue(re.fullmatch(r'[0-9a-f]{16}', rows[0][1]))
        self.assertEqual([r[0] for r in rows], [1, 2])
        self.assertEqual((rows[0][2], rows[0][3], rows[1][1], rows[1][2]), ('+**************', None, None, 'a***'))

    def test_hash_is_stable_and_keyed(self):
        def hashed(value):
            return list(masking.apply(['v'], [(value,)], ['hash'])[1])[0][0]
        self.assertEqual(hashed('jane@example.com'), hashed('jane@example.com'))
        self.assertNotEqual(hashed('jane@example.com'), hashed('john@example.com'))
        self.assertEqual(hashed(42), hashed('42'))
        before = hashed('jane@example.com')
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_MASK_KEY': 'k2'}):
            self.assertNotEqual(hashed('jane@example.com'), before)

    def test_mask(self):
        for value, expected in (('jane@example.com', 'j***@example.com'), ('@x.org', '***@x.org'), ('Bo', 'B***'),
                                ('', None), (12345, None), (None, None)):
            with self.subTest(value=value):
                self.assertEqual(masking._mask(value), expected)

    def test_nothing_to_do_returns_the_rows_untouched(self):
        rows = iter([(1,)])
        self.assertIs(masking.apply(['a'], rows, [None])[1], rows)


class KeyTests(unittest.TestCase):
    def test_a_store_key_made_once_and_kept(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': home}):
            os.environ.pop('QUERYAPIGATE_MASK_KEY', None)
            create_app()
            first = masking.hash_key()
            self.assertEqual(len(first), 64)
            self.assertEqual(masking.hash_key(), first)


class RealSchemaTests(unittest.TestCase):
    """Against real databases and their own schema: tracing finds exactly the columns the database returns."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        lite = os.path.join(self.tmp.name, 'shop.db')
        conn = sqlite3.connect(lite)
        conn.execute('CREATE TABLE customers (id INTEGER, email TEXT, phone TEXT)')
        conn.execute('CREATE TABLE orders (id INTEGER, customer_id INTEGER, total INTEGER)')
        conn.execute('CREATE VIEW contacts AS SELECT id, email FROM customers')
        conn.commit()
        conn.close()
        duck = os.path.join(self.tmp.name, 'shop.duckdb')
        conn = duckdb.connect(duck)
        conn.execute('CREATE TABLE customers (id INTEGER, email VARCHAR, phone VARCHAR)')
        conn.execute('CREATE TABLE orders (id INTEGER, customer_id INTEGER, total INTEGER)')
        conn.close()
        write_connections({'lite': {'db': 'sqlite', 'database': lite, 'active': True},
                           'duck': {'db': 'duckdb', 'database': duck, 'active': True}})
        create_app()

    def test_traced_columns_match_the_database(self):
        from queryapigate import engine
        for connection, dialect in (('lite', 'sqlite'), ('duck', 'duckdb')):
            tables = schema.fetch_schema(connection)['tables']
            for sql in ('SELECT * FROM customers c JOIN orders o ON o.customer_id = c.id',
                        'SELECT c.*, o.total FROM customers c JOIN orders o ON o.customer_id = c.id',
                        'WITH x AS (SELECT * FROM customers) SELECT * FROM x'):
                with self.subTest(connection=connection, sql=sql):
                    traced = masking.trace(sql, dialect, tables)
                    result = engine.execute_sql(sql, connection, 10, 0)
                    # by position: the server renames a repeated column (id_2), tracing keeps the query's own name
                    self.assertEqual(len(traced), len(result.columns))
                    for (name, _), column in zip(traced, result.columns, strict=True):
                        self.assertTrue(str(column) == name or str(column).startswith(name + '_'), (name, column))
        tables = schema.fetch_schema('lite')['tables']
        self.assertEqual({t['name']: t['type'] for t in tables}['contacts'], 'view')
        with self.assertRaises(masking.ApiError) as caught:
            masking.plan(RULES, masking.trace('SELECT email FROM contacts', 'sqlite', tables))
        self.assertEqual(caught.exception.code, 'mask_unresolvable')


class TraceCacheTests(unittest.TestCase):
    """prepare() traces a statement once per connection schema and reuses it: tracing is a pure function of the SQL,
    the dialect and the schema, and was most of a masked call's time. Against a real SQLite schema."""

    RULES = masking.validate_rules([{'column': 'customers.email', 'action': 'hash'}])
    SQL = 'SELECT c.id, c.email, o.total FROM customers c JOIN orders o ON o.customer_id = c.id'

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        lite = os.path.join(self.tmp.name, 'shop.db')
        conn = sqlite3.connect(lite)
        conn.execute('CREATE TABLE customers (id INTEGER, email TEXT, phone TEXT)')
        conn.execute('CREATE TABLE orders (id INTEGER, customer_id INTEGER, total INTEGER)')
        conn.commit()
        conn.close()
        write_connections({'lite': {'db': 'sqlite', 'database': lite, 'active': True}})
        create_app()
        masking.forget_schema()
        self.addCleanup(masking.forget_schema)
        traced = mock.patch.object(masking, 'trace', wraps=masking.trace)
        self.trace = traced.start()
        self.addCleanup(traced.stop)

    def prepare(self, sql=None):
        return masking.prepare(self.RULES, sql or self.SQL, 'lite', 'sqlite')

    def test_a_statement_is_traced_once_and_reused(self):
        first, second = self.prepare(), self.prepare()
        self.assertEqual(self.trace.call_count, 1)
        self.assertEqual(first.actions, [None, 'hash', None])
        self.assertEqual((second.actions, second.fired), (first.actions, first.fired))

    def test_cached_and_uncached_plans_agree(self):
        tables = schema.fetch_schema('lite')['tables']
        for sql in (self.SQL, 'SELECT email AS contact FROM customers',
                    'WITH x AS (SELECT * FROM customers) SELECT * FROM x',
                    'SELECT email FROM customers UNION SELECT phone FROM customers'):
            with self.subTest(sql=sql):
                direct = masking.plan(self.RULES, masking.trace(sql, 'sqlite', tables))
                for _ in range(2):  # the first fills the cache, the second reads it
                    plan_ = self.prepare(sql)
                    self.assertEqual((plan_.actions, plan_.fired), direct)

    def test_the_cached_trace_is_frozen(self):
        traced = self.prepare().traced
        self.assertIsInstance(traced, tuple)
        self.assertTrue(all(isinstance(sources, frozenset) for _, sources in traced))

    def test_forget_schema_drops_the_traces(self):
        self.prepare()
        masking.forget_schema('lite')
        self.prepare()
        self.assertEqual(self.trace.call_count, 2)

    def test_a_trace_expires_with_its_schema(self):
        now = [1000.0]
        with mock.patch.object(masking.time, 'monotonic', lambda: now[0]):
            self.prepare()
            now[0] += masking.SCHEMA_TTL - 1
            self.prepare()
            self.assertEqual(self.trace.call_count, 1)
            now[0] += 2  # the schema is read again, and the statement traced against it
            self.prepare()
            self.assertEqual(self.trace.call_count, 2)

    def test_a_statement_that_cant_be_traced_is_refused_every_time(self):
        for _ in range(2):
            with self.assertRaises(masking.ApiError) as caught:
                self.prepare('SELECT * FROM nowhere')
            self.assertEqual(caught.exception.code, 'mask_unresolvable')
        self.assertEqual(self.trace.call_count, 2)

    def test_the_cache_is_bounded(self):
        with mock.patch.object(masking, 'TRACE_CACHE_SIZE', 2):
            for sql in ('SELECT id FROM customers', 'SELECT email FROM customers', 'SELECT phone FROM customers'):
                self.prepare(sql)
            self.prepare('SELECT phone FROM customers')  # kept: the most recent
            self.assertEqual(self.trace.call_count, 3)
            self.prepare('SELECT id FROM customers')  # evicted: the oldest
            self.assertEqual(self.trace.call_count, 4)
            self.assertLessEqual(len(masking._schemas[('lite', None)][2]), 2)


class ReturningTests(unittest.TestCase):
    """What an agent's write returns (ADR 0008) is masked like a read: its RETURNING clause traced as the SELECT that
    returns the same columns from the table written - and refused when the change joins other tables in."""

    def test_returning_reads_like_a_select_of_the_table_written(self):
        for sql, expected in (
                ("INSERT INTO tickets (title) VALUES ('x') RETURNING id, title", 'SELECT id, title FROM tickets'),
                ('UPDATE tickets SET status = :s WHERE id = :id RETURNING upper(title) AS t',
                 'SELECT UPPER(title) AS t FROM tickets'),
                ('DELETE FROM tickets WHERE id = 1 RETURNING *', 'SELECT * FROM tickets')):
            with self.subTest(sql=sql):
                self.assertEqual(masking.returning_as_select(sql, 'postgres'), expected)

    def test_without_returning_or_for_a_read_nothing_changes(self):
        for sql in ('UPDATE tickets SET status = 1', 'SELECT * FROM tickets'):
            self.assertEqual(masking.returning_as_select(sql, 'postgres'), sql)

    def test_a_change_that_joins_other_tables_in_is_refused(self):
        for sql in ('UPDATE tickets SET status = u.s FROM users u WHERE u.id = tickets.owner RETURNING u.email',
                    'DELETE FROM tickets USING users u WHERE u.id = tickets.owner RETURNING u.email'):
            with self.subTest(sql=sql), self.assertRaises(masking.ApiError) as caught:
                masking.returning_as_select(sql, 'postgres')
            self.assertEqual(caught.exception.code, 'mask_unresolvable')


class SqlglotVersionTests(unittest.TestCase):
    def test_a_sqlglot_too_old_to_trace_with_says_so(self):
        """Tracing was verified on sqlglot 25.2 to <26; older ones lack what it uses (exp.SetOperation), and a masked
        caller must be told what's wrong rather than get a 500 from inside the parser."""
        import types
        for version, refused in (('25.1.0', True), ('23.0.0', True), ('25.2.0', False), ('25.34.1', False)):
            with self.subTest(version=version):
                fake = types.SimpleNamespace(__version__=version)
                if refused:
                    with self.assertRaises(masking.ApiError) as caught:
                        masking.require_supported_sqlglot(fake)
                    self.assertEqual(caught.exception.code, 'masking_unavailable')
                    self.assertIn('sqlglot>=25.2', caught.exception.message)
                else:
                    masking.require_supported_sqlglot(fake)


if __name__ == '__main__':
    unittest.main()

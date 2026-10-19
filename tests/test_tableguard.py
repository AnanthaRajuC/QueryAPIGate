"""Tests for tableguard.py - table-level SQL allow-listing (BACKLOG #21's remaining gap). Unlike sqlflow.py's
best-effort extraction, every failure case here must raise ApiError (a security check that can't verify a
query's tables must reject it, not degrade quietly) - see the module's own docstring."""
import unittest
from unittest import mock

from queryapigate import sqltools, tableguard
from queryapigate.errors import ApiError


class ExtractTablesTests(unittest.TestCase):
    def test_a_single_table(self):
        self.assertEqual(tableguard.extract_tables('SELECT * FROM Orders', 'postgres'), {'orders'})

    def test_a_join(self):
        sql = 'SELECT a.* FROM orders a JOIN customers b ON a.customer_id = b.id'
        self.assertEqual(tableguard.extract_tables(sql, 'mysql'), {'orders', 'customers'})

    def test_a_self_join_counts_once(self):
        sql = 'SELECT a.* FROM orders a JOIN orders b ON a.parent_id = b.id'
        self.assertEqual(tableguard.extract_tables(sql, 'postgres'), {'orders'})

    def test_a_cte_name_is_excluded_but_its_real_tables_are_not(self):
        sql = ('WITH recent AS (SELECT * FROM orders) '
              'SELECT r.id, c.name FROM recent r JOIN customers c ON r.customer_id = c.id '
              'WHERE r.id IN (SELECT order_id FROM refunds)')
        self.assertEqual(tableguard.extract_tables(sql, 'postgres'), {'orders', 'customers', 'refunds'})

    def test_a_cte_named_after_the_table_it_reads_still_counts_that_table(self):
        # Found by review, 2026-10-15: every table sharing a name with a CTE was dropped, the one the CTE itself reads
        # included - so `WITH secret AS (SELECT * FROM secret) SELECT * FROM secret` named no table at all, and a key
        # allowed only `orders` read `secret`. Inside its own definition a (non-recursive) CTE's name is the table.
        for sql, tables in (
                ('WITH secret AS (SELECT * FROM secret) SELECT * FROM secret', {'secret'}),
                ('WITH Secret AS (SELECT * FROM secret) SELECT * FROM SECRET', {'secret'}),
                ('WITH a AS (SELECT * FROM b), b AS (SELECT 1 AS n) SELECT * FROM a, b', {'b'}),  # b: not yet
                ('WITH b AS (SELECT * FROM orders), a AS (SELECT * FROM b) SELECT * FROM a', {'orders'}),
                ('WITH secret AS (SELECT 1 AS n) SELECT * FROM public.secret', {'secret'}),  # qualified: the table
                ('SELECT * FROM (WITH secret AS (SELECT * FROM secret) SELECT * FROM secret) x', {'secret'}),
                ('WITH x AS (SELECT 1 AS n) SELECT * FROM (WITH y AS (SELECT * FROM x) SELECT * FROM y) z', set()),
                ('WITH RECURSIVE n AS (SELECT 1 AS i UNION ALL SELECT i + 1 FROM n WHERE i < 3) SELECT * FROM n',
                 set())):  # a recursive CTE reads itself, not a table
            with self.subTest(sql=sql):
                self.assertEqual(tableguard.extract_tables(sql, 'postgres'), tables)
        with self.assertRaises(ApiError) as caught:
            sqltools.validate_sql('WITH secret AS (SELECT * FROM secret) SELECT * FROM secret', 'postgres',
                                  allow_writes=False, allowed_tables={'orders'})
        self.assertEqual(caught.exception.code, 'table_not_allowed')

    def test_a_statement_nested_too_deep_to_walk_is_refused_not_let_through(self):
        for depth in (50, 400, 3000):
            sql = 'SELECT * FROM ' + '(SELECT * FROM ' * depth + 'secret' + ') x' * depth
            with self.subTest(depth=depth):
                try:
                    self.assertEqual(tableguard.extract_tables(sql, 'postgres'), {'secret'})
                except ApiError as error:
                    self.assertEqual(error.code, 'table_check_failed')

    def test_a_derived_table_subquery_resolves_to_its_real_table(self):
        sql = 'SELECT * FROM (SELECT * FROM secret_table) x'
        self.assertEqual(tableguard.extract_tables(sql, 'postgres'), {'secret_table'})

    def test_a_union_finds_both_sides(self):
        self.assertEqual(tableguard.extract_tables('SELECT * FROM a UNION SELECT * FROM b', 'postgres'),
                         {'a', 'b'})

    def test_bare_delete_finds_the_target_table(self):
        self.assertEqual(tableguard.extract_tables('DELETE FROM secret_table', 'postgres'), {'secret_table'})

    def test_bare_update_finds_the_target_table(self):
        self.assertEqual(tableguard.extract_tables('UPDATE secret_table SET x = 1', 'postgres'),
                         {'secret_table'})

    def test_bare_insert_finds_the_target_table(self):
        self.assertEqual(tableguard.extract_tables('INSERT INTO secret_table VALUES (1)', 'postgres'),
                         {'secret_table'})

    def test_a_delete_with_a_subquery_finds_both_tables(self):
        sql = 'DELETE FROM t WHERE id IN (SELECT id FROM other)'
        self.assertEqual(tableguard.extract_tables(sql, 'postgres'), {'t', 'other'})

    def test_a_cte_backed_delete_excludes_the_cte_name(self):
        sql = 'WITH x AS (SELECT id FROM orders) DELETE FROM t WHERE id IN (SELECT id FROM x)'
        self.assertEqual(tableguard.extract_tables(sql, 'postgres'), {'t', 'orders'})

    def test_a_table_valued_function_extracts_no_table(self):
        self.assertEqual(tableguard.extract_tables('SELECT * FROM numbers(10)', 'clickhouse'), set())

    def test_bound_and_legacy_placeholders_do_not_break_parsing(self):
        self.assertEqual(tableguard.extract_tables('SELECT * FROM t WHERE a = :x AND b = {y}', 'sqlite'), {'t'})

    def test_an_unsupported_dialect_raises(self):
        with self.assertRaises(ApiError) as ctx:
            tableguard.extract_tables('SELECT * FROM t', 'h2')
        self.assertEqual(ctx.exception.status, 403)
        self.assertIn('h2', ctx.exception.message)

    def test_jdbc_and_mongo_are_also_unsupported(self):
        for dialect in ('jdbc', 'mongo'):
            with self.assertRaises(ApiError):
                tableguard.extract_tables('SELECT * FROM t', dialect)

    def test_unparseable_sql_raises_rather_than_returning_empty(self):
        with self.assertRaises(ApiError) as ctx:
            tableguard.extract_tables('SELECT FROM WHERE (((', 'postgres')
        self.assertEqual(ctx.exception.status, 403)

    def test_missing_sqlglot_raises_a_clear_error(self):
        with mock.patch.dict('sys.modules', {'sqlglot': None}):
            with self.assertRaises(ApiError) as ctx:
                tableguard.extract_tables('SELECT * FROM t', 'postgres')
        self.assertIn('sqlglot', ctx.exception.message)
        self.assertEqual(ctx.exception.status, 500)


class ValidateSqlAllowedTablesTests(unittest.TestCase):
    """sqltools.validate_sql()'s own allowed_tables enforcement - unit-level, no HTTP/engine.py involved."""

    def test_unset_means_no_restriction(self):
        sql = sqltools.validate_sql('SELECT * FROM anything', dialect='postgres', allow_writes=False,
                                    allowed_tables=None)
        self.assertEqual(sql, 'SELECT * FROM anything')

    def test_a_query_touching_only_allowed_tables_passes(self):
        sql = sqltools.validate_sql('SELECT * FROM orders', dialect='postgres', allow_writes=False,
                                    allowed_tables={'orders', 'customers'})
        self.assertEqual(sql, 'SELECT * FROM orders')

    def test_a_query_touching_a_forbidden_table_is_rejected(self):
        with self.assertRaises(ApiError) as ctx:
            sqltools.validate_sql('SELECT * FROM secret', dialect='postgres', allow_writes=False,
                                  allowed_tables={'orders'})
        self.assertEqual(ctx.exception.status, 403)
        self.assertIn('secret', ctx.exception.message)

    def test_a_join_pulling_in_one_forbidden_table_is_rejected_even_with_one_allowed(self):
        sql = 'SELECT * FROM orders o JOIN secret s ON o.id = s.order_id'
        with self.assertRaises(ApiError):
            sqltools.validate_sql(sql, dialect='postgres', allow_writes=False, allowed_tables={'orders'})

    def test_an_unsupported_dialect_rejects_every_query_fail_closed(self):
        with self.assertRaises(ApiError) as ctx:
            sqltools.validate_sql('SELECT * FROM anything', dialect='h2', allow_writes=False,
                                  allowed_tables={'anything'})
        self.assertEqual(ctx.exception.status, 403)

    def test_an_existing_write_op_restriction_is_checked_first(self):
        # A write forbidden by allowed_write_ops must surface that error, not a table one, even though the
        # statement also touches a forbidden table - the more specific/earlier check wins.
        with self.assertRaises(ApiError) as ctx:
            sqltools.validate_sql('DELETE FROM secret', dialect='postgres', allow_writes=True,
                                  allowed_write_ops=['insert'], allowed_tables={'orders'})
        self.assertIn('write operations', ctx.exception.message)


class TrinoQualificationTests(unittest.TestCase):
    """Trino's three-part names are extracted and matched fully-qualified, filled from the connection's default
    catalog/schema (ADR 0026), so a grant never matches the same table name in another catalog or schema."""
    C, S = 'hive', 'analytics'  # default catalog, schema

    def extract(self, sql):
        return tableguard.extract_tables(sql, 'trino', self.C, self.S)

    def test_a_bare_reference_is_qualified_with_the_defaults(self):
        self.assertEqual(self.extract('SELECT * FROM orders'), {'hive.analytics.orders'})

    def test_schema_and_catalog_qualified_references_keep_their_parts(self):
        self.assertEqual(self.extract('SELECT * FROM sales.orders'), {'hive.sales.orders'})
        self.assertEqual(self.extract('SELECT * FROM iceberg.sales.orders'), {'iceberg.sales.orders'})

    def test_a_join_across_catalogs_keeps_each_fully_qualified(self):
        sql = 'SELECT a.* FROM orders a JOIN tpch.sf1.customer b ON a.id = b.id'
        self.assertEqual(self.extract(sql), {'hive.analytics.orders', 'tpch.sf1.customer'})

    def test_a_cte_is_still_excluded_on_its_bare_name(self):
        self.assertEqual(self.extract('WITH recent AS (SELECT * FROM orders) SELECT * FROM recent'),
                         {'hive.analytics.orders'})

    def test_qualify_grant_fills_the_missing_parts(self):
        self.assertEqual(tableguard.qualify_grant('orders', self.C, self.S), 'hive.analytics.orders')
        self.assertEqual(tableguard.qualify_grant('sales.orders', self.C, self.S), 'hive.sales.orders')
        self.assertEqual(tableguard.qualify_grant('iceberg.sales.orders', self.C, self.S), 'iceberg.sales.orders')

    def _check(self, sql, allowed):
        return sqltools.validate_sql(sql, dialect='trino', allowed_tables=allowed,
                                     default_catalog=self.C, default_schema=self.S)

    def test_a_bare_grant_does_not_reach_another_schema_or_catalog(self):
        self._check('SELECT * FROM orders', {'orders'})  # ok
        self._check('SELECT * FROM hive.analytics.orders', {'orders'})  # ok: the same table, fully written out
        for crosses in ('SELECT * FROM hive.sales.orders', 'SELECT * FROM tpch.analytics.orders'):
            with self.assertRaises(ApiError) as ctx:
                self._check(crosses, {'orders'})
            self.assertEqual(ctx.exception.code, 'table_not_allowed')

    def test_a_qualified_grant_does_not_match_a_bare_reference_elsewhere(self):
        with self.assertRaises(ApiError) as ctx:
            self._check('SELECT * FROM orders', {'tpch.sf1.orders'})  # orders == hive.analytics.orders, not sf1's
        self.assertEqual(ctx.exception.code, 'table_not_allowed')
        self._check('SELECT * FROM tpch.sf1.orders', {'tpch.sf1.orders'})  # the exact table is allowed

    def test_a_data_reading_table_function_is_refused(self):
        # Trino's TABLE(...) polymorphic functions can read data the allow-list can't see (system.query pass-through,
        # table_changes), so they are refused for a table-restricted key, as read_parquet()/url() are elsewhere.
        for sql in ("SELECT * FROM TABLE(system.query(query => 'SELECT 1'))",
                    "SELECT * FROM TABLE(system.table_changes('c', 's', 't'))"):
            with self.assertRaises(ApiError) as ctx:
                self.extract(sql)
            self.assertEqual(ctx.exception.code, 'table_not_allowed')

    def test_a_pure_generator_names_no_table(self):
        self.assertEqual(self.extract('SELECT * FROM UNNEST(ARRAY[1, 2, 3])'), set())


if __name__ == '__main__':
    unittest.main()

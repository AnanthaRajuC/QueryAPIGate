"""Tests for queryapigate.sqlflow: best-effort table/join extraction via sqlglot - always degrades to
an `error` string instead of raising, since this only feeds a visualization, never the execution path."""
import unittest
from unittest import mock

from queryapigate import sqlflow


class ExtractFlowTests(unittest.TestCase):
    def test_single_table(self):
        result = sqlflow.extract_flow('SELECT id, name FROM customers WHERE status = :status', 'postgres')
        self.assertEqual(result['tables'], ['customers'])
        self.assertEqual(result['joins'], [])
        self.assertIsNone(result['error'])
        self.assertIsNotNone(result['formatted'])

    def test_inner_and_left_join_with_condition_and_order(self):
        sql = ('SELECT a.id FROM orders a '
               'JOIN customers b ON a.customer_id = b.id '
               'LEFT JOIN regions r ON b.region_id = r.id')
        result = sqlflow.extract_flow(sql, 'mysql')
        self.assertEqual(result['tables'], ['orders', 'customers', 'regions'])
        self.assertEqual(result['joins'], [
            {'left': 'orders', 'right': 'customers', 'type': 'JOIN', 'on': 'a.customer_id = b.id'},
            {'left': 'customers', 'right': 'regions', 'type': 'LEFT JOIN', 'on': 'b.region_id = r.id'},
        ])
        self.assertIsNone(result['error'])

    def test_bound_and_legacy_placeholders_do_not_break_parsing(self):
        result = sqlflow.extract_flow('SELECT * FROM t WHERE a = :x AND b = {y}', 'sqlite')
        self.assertEqual(result['tables'], ['t'])
        self.assertIsNone(result['error'])
        self.assertIsNone(result['formatted'])  # the {y} legacy placeholder rules out pretty-printing

    def test_unsupported_dialect_returns_a_graceful_error(self):
        result = sqlflow.extract_flow('SELECT * FROM t', 'h2')
        self.assertEqual(result, {'tables': [], 'joins': [], 'formatted': None,
                                  'error': "SQL analysis isn't available for 'h2' connections"})

    def test_jdbc_and_mongo_are_also_unsupported(self):
        for dialect in ('jdbc', 'mongo'):
            self.assertIsNotNone(sqlflow.extract_flow('SELECT * FROM t', dialect)['error'])

    def test_malformed_sql_returns_a_graceful_error_not_a_raise(self):
        result = sqlflow.extract_flow('SELEC BAD SQL(((', 'postgres')
        self.assertEqual(result, {'tables': [], 'joins': [], 'formatted': None,
                                  'error': 'Could not analyze this query'})

    def test_missing_sqlglot_degrades_gracefully(self):
        with mock.patch.dict('sys.modules', {'sqlglot': None}):
            result = sqlflow.extract_flow('SELECT * FROM t', 'postgres')
        self.assertEqual(result, {'tables': [], 'joins': [], 'formatted': None,
                                  'error': 'sqlglot is not installed'})


class PrettyPrintTests(unittest.TestCase):
    def test_formats_and_round_trips_a_bound_parameter(self):
        result = sqlflow.pretty_print('SELECT id FROM t WHERE a = :status', 'postgres')
        self.assertIn('SELECT', result)
        self.assertIn(':status', result)
        self.assertIn('\n', result)

    def test_unsupported_dialect_returns_none(self):
        self.assertIsNone(sqlflow.pretty_print('SELECT * FROM t', 'h2'))

    def test_legacy_brace_placeholder_returns_none(self):
        self.assertIsNone(sqlflow.pretty_print('SELECT * FROM t WHERE a = {x}', 'postgres'))

    def test_unparseable_sql_returns_none(self):
        self.assertIsNone(sqlflow.pretty_print('SELEC BAD SQL(((', 'postgres'))

    def test_missing_sqlglot_returns_none(self):
        with mock.patch.dict('sys.modules', {'sqlglot': None}):
            self.assertIsNone(sqlflow.pretty_print('SELECT * FROM t', 'postgres'))


if __name__ == '__main__':
    unittest.main()

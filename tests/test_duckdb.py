"""Unit tests for the 'duckdb' connection type. A real database is exercised against the actual driver in
tests/test_integration.py's DuckDBTests (needs no live server - just the optional `duckdb` package, so it
runs unconditionally in dev/CI, unlike the network-database integration tests); these tests cover the
pieces that don't need a connection at all: config, the SQL guard's pagination/dialect handling, and the
file-resolution guard shared with SQLite.
"""
import os
import tempfile
import unittest
from unittest import mock

from queryapigate import config, create_app, runners, sqltools
from queryapigate.errors import ApiError
from tests.helpers import put_connections


class ConfigTests(unittest.TestCase):
    def test_duckdb_is_a_supported_db_type(self):
        self.assertIn('duckdb', config.SUPPORTED_DB_TYPES)


class PaginationTests(unittest.TestCase):
    def test_duckdb_is_paginated_server_side_like_sqlite_and_postgres(self):
        # Unlike the generic jdbc driver, DuckDB's LIMIT/OFFSET is the same portable syntax every other
        # non-jdbc dialect here uses, so pagination is applied server-side, not fetched-then-sliced.
        self.assertTrue(sqltools.is_paginated('SELECT * FROM t', dialect='duckdb'))
        self.assertTrue(sqltools.is_paginated('WITH x AS (SELECT 1) SELECT * FROM x', dialect='duckdb'))

    def test_duckdb_defaults_to_the_safe_ansi_literal_rules(self):
        # DuckDB's SQL dialect is close to PostgreSQL: a quote only escapes via doubling, not backslash.
        self.assertNotIn('duckdb', sqltools.BACKSLASH_ESCAPE_DIALECTS)


class DuckDbConnectValidationTests(unittest.TestCase):
    """duckdb.connect() is never reached when the connection is missing or points nowhere real."""

    def test_missing_database_field(self):
        with self.assertRaises(ApiError) as ctx:
            runners._DuckDB().connect({}, True)
        self.assertIn('path not provided', ctx.exception.message)

    def test_nonexistent_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ApiError) as ctx:
                runners._DuckDB().connect({'database': os.path.join(tmp, 'nope.duckdb')}, True)
        self.assertEqual(ctx.exception.status, 404)
        self.assertIn('DuckDB', ctx.exception.message)

    def test_relative_path_resolves_against_queryapigate_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            open(os.path.join(tmp, 'rel.duckdb'), 'w').close()
            with mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': tmp}):
                # A plain, empty file isn't a valid DuckDB database, so connect() itself may still raise -
                # what matters here is that it gets *past* the file-not-found guard, i.e. found the file.
                with self.assertRaises(Exception) as ctx:  # noqa: B017 - duckdb raises its own error type
                    runners._DuckDB().connect({'database': 'rel.duckdb'}, True)
                self.assertNotIsInstance(ctx.exception, ApiError)


class ConnectionApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop('QUERYAPIGATE_API_KEY', None)
        self.client = create_app().test_client()

    def test_a_duckdb_connection_can_be_saved_and_listed(self):
        res = put_connections(self.client, {'local': {
            'db': 'duckdb', 'database': 'local.duckdb', 'active': True}})
        self.assertEqual(res.status_code, 201)
        listed = self.client.get('/api/v1/connections/local').get_json()
        self.assertEqual(listed['db'], 'duckdb')
        self.assertEqual(listed['database'], 'local.duckdb')

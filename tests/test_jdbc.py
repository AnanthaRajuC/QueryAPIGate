"""Unit tests for the generic 'jdbc' connection type. A real database is exercised against the actual
driver in tests/test_integration.py's JdbcViaH2Tests (H2 standing in for an arbitrary JDBC vendor); these
tests cover the pieces that don't need a live connection: config, the SQL guard's pagination/dialect
handling, the schema-browser boundary, and the JVM classpath-union logic.
"""
import os
import tempfile
import unittest
from unittest import mock

from queryapigate import config, create_app, runners, schema, sqltools
from queryapigate.errors import ApiError
from tests import helpers
from tests.helpers import put_connections


class ConfigTests(unittest.TestCase):
    def test_jdbc_is_a_supported_db_type(self):
        self.assertIn('jdbc', config.SUPPORTED_DB_TYPES)


class PaginationTests(unittest.TestCase):
    def test_jdbc_is_never_paginated_server_side(self):
        self.assertFalse(sqltools.is_paginated('SELECT * FROM t', dialect='jdbc'))
        self.assertFalse(sqltools.is_paginated('WITH x AS (SELECT 1) SELECT * FROM x', dialect='jdbc'))

    def test_other_dialects_are_unaffected(self):
        self.assertTrue(sqltools.is_paginated('SELECT * FROM t', dialect='postgres'))
        self.assertTrue(sqltools.is_paginated('SELECT * FROM t', dialect=None))

    def test_jdbc_defaults_to_the_safe_ansi_literal_rules(self):
        # Not in BACKSLASH_ESCAPE_DIALECTS, so a value ending in a backslash before a quote is read the
        # ANSI way (doubling-only) - the safe default for the enterprise databases this type targets.
        self.assertNotIn('jdbc', sqltools.BACKSLASH_ESCAPE_DIALECTS)


class JvmClasspathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_connections(self, connections):
        helpers.write_connections(connections)

    def test_always_includes_the_h2_jar(self):
        self.write_connections({})
        self.assertIn(config.h2_jar(), runners._jvm_classpath())

    def test_includes_every_distinct_jdbc_jar(self):
        self.write_connections({
            'oracle-db': {'db': 'jdbc', 'jar': '/opt/jars/ojdbc.jar', 'driver_class': 'oracle.jdbc.OracleDriver',
                         'jdbc_url': 'jdbc:oracle:thin:@host:1521:orcl', 'active': True},
            'mssql-db': {'db': 'jdbc', 'jar': '/opt/jars/mssql.jar',
                        'driver_class': 'com.microsoft.sqlserver.jdbc.SQLServerDriver',
                        'jdbc_url': 'jdbc:sqlserver://host:1433', 'active': True},
            'not-jdbc': {'db': 'sqlite', 'database': 'x.db', 'active': True},
        })
        jars = runners._jvm_classpath()
        self.assertIn('/opt/jars/ojdbc.jar', jars)
        self.assertIn('/opt/jars/mssql.jar', jars)
        self.assertIn(config.h2_jar(), jars)
        self.assertEqual(len(jars), 3)  # no duplicates, and sqlite contributes nothing

    def test_a_jdbc_connection_without_a_jar_contributes_nothing(self):
        self.write_connections({'broken': {'db': 'jdbc', 'active': True}})
        self.assertEqual(runners._jvm_classpath(), [config.h2_jar()])

    def test_result_is_sorted_and_deduplicated(self):
        self.write_connections({
            'a': {'db': 'jdbc', 'jar': '/z.jar', 'active': True},
            'b': {'db': 'jdbc', 'jar': '/a.jar', 'active': True},
            'c': {'db': 'jdbc', 'jar': '/a.jar', 'active': True},
        })
        jars = runners._jvm_classpath()
        self.assertEqual(jars, sorted(set(jars)))


class JdbcConnectValidationTests(unittest.TestCase):
    """jaydebeapi.connect() is never reached when a required field is missing."""

    def test_missing_jar(self):
        with self.assertRaises(ApiError) as ctx:
            runners._JDBC().connect({'driver_class': 'x.Driver', 'jdbc_url': 'jdbc:x://h'}, True)
        self.assertIn('jar', str(ctx.exception))

    def test_missing_driver_class(self):
        with self.assertRaises(ApiError) as ctx:
            runners._JDBC().connect({'jar': '/x.jar', 'jdbc_url': 'jdbc:x://h'}, True)
        self.assertIn('driver_class', str(ctx.exception))

    def test_missing_jdbc_url(self):
        with self.assertRaises(ApiError) as ctx:
            runners._JDBC().connect({'jar': '/x.jar', 'driver_class': 'x.Driver'}, True)
        self.assertIn('jdbc_url', str(ctx.exception))


class SchemaGuardTests(unittest.TestCase):
    """No real connection is made: fetch_schema rejects an unsupported dialect before calling engine."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        helpers.write_connections({'ora': {
            'db': 'jdbc', 'jar': '/x.jar', 'driver_class': 'x.Driver', 'jdbc_url': 'jdbc:x://h',
            'active': True}})

    def test_schema_introspection_is_not_supported_for_jdbc(self):
        with self.assertRaises(ApiError) as ctx:
            schema.fetch_schema('ora')
        self.assertIn("isn't supported", ctx.exception.message)


class ConnectionApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop('QUERYAPIGATE_API_KEY', None)
        self.client = create_app().test_client()

    def test_a_jdbc_connection_can_be_saved_and_listed(self):
        res = put_connections(self.client, {'ora': {
            'db': 'jdbc', 'jar': '/opt/ojdbc.jar', 'driver_class': 'oracle.jdbc.OracleDriver',
            'jdbc_url': 'jdbc:oracle:thin:@host:1521:orcl', 'user': 'app', 'password': 'secret', 'active': True}})
        self.assertEqual(res.status_code, 201)
        listed = self.client.get('/api/v1/connections/ora').get_json()
        self.assertEqual(listed['options']['driver_class'], 'oracle.jdbc.OracleDriver')  # driver settings
        self.assertEqual(listed['options']['jdbc_url'], 'jdbc:oracle:thin:@host:1521:orcl')
        self.assertEqual(listed['password'], config.PASSWORD_MASK)  # masked like every other connection

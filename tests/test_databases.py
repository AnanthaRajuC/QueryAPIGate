"""The database support matrix (BACKLOG #66): queryapigate/databases.py works it out from the code that implements
each feature, and these keep the published table, the tiers and the CI claim honest - a feature lost on a tier 1 type,
or a table edited by hand, fails here."""
import os
import re
import tempfile
import unittest
from unittest import mock

from queryapigate import config, create_app, databases, experimental, mcp_server

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(path):
    with open(os.path.join(ROOT, path)) as f:
        return f.read()


class MatrixTests(unittest.TestCase):
    def test_the_document_carries_the_table_the_code_gives(self):
        doc = read('documentation/DATABASE_CONNECTION_CONFIGURATION.md')
        block = doc.split('<!-- support-matrix:start', 1)[1].split('-->\n', 1)[1]
        published = block.split('\n<!-- support-matrix:end')[0]
        self.assertEqual(published, databases.markdown_table(), 'regenerate it: python -c '
                         '"from queryapigate import databases; print(databases.markdown_table())"')

    def test_every_type_is_tier_1_or_experimental_and_nothing_else(self):
        self.assertEqual(set(databases.TYPES), set(config.SUPPORTED_DB_TYPES))
        self.assertEqual(set(databases.TYPES) - set(databases.TIER_1), set(experimental.DB_TYPES))

    def test_tier_1_types_keep_every_essential_feature(self):
        essential = ('Saved queries and ad-hoc runs', 'Read-only by default (SQL check)', 'Query time limit',
                     '`allowed_tables` (refused where not supported)', 'Schema browser, MCP `list_tables`',
                     'Streaming exports', 'Tested against a real server in CI')
        rows = dict(databases.matrix())
        for feature in essential:
            for db in databases.TIER_1:
                with self.subTest(feature=feature, db=db):
                    self.assertNotIn(rows[feature][db], ('no', 'n/a'))

    def test_the_ci_column_matches_the_integration_tests_that_exist(self):
        classes = set(re.findall(r"^    db(?:, env_var)? = '([a-z0-9]+)'", read('tests/test_integration.py'), re.M))
        self.assertEqual(databases.INTEGRATION_TESTED, classes | {'sqlite'})  # sqlite: the main suite, on real files


class NoSqlOnMongoTests(unittest.TestCase):
    """SQL sent to a MongoDB connection is refused with what to use instead - not a 500 from inside the driver."""

    def test_rest_and_mcp_say_so(self):
        with tempfile.TemporaryDirectory() as home, \
                mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': home, 'QUERYAPIGATE_API_KEY': 'k'}):
            app = create_app()
            client = app.test_client()
            client.post('/api/v1/connections', headers={'X-API-Key': 'k'},
                        json={'name': 'docs', 'db': 'mongo', 'host': '127.0.0.1', 'port': 1, 'database': 'd'})
            res = client.post('/execute_sql', headers={'X-API-Key': 'k'},
                              json={'sql': 'SELECT 1', 'connection_name': 'docs'})
            self.assertEqual((res.status_code, res.get_json()['code']), (400, 'wrong_connection_type'))
            self.assertIn('/execute_mongo', res.get_json()['error'])
            result = mcp_server.handle_call(app, 'k', '', '10.0.0.1', 'execute_sql',
                                            {'connection_name': 'docs', 'sql': 'SELECT 1'})
            self.assertEqual(result['structuredContent']['code'], 'wrong_connection_type')


if __name__ == '__main__':
    unittest.main()

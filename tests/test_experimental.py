"""Experimental features (BACKLOG #64): queryapigate/experimental.py is the one list, and every place that tells a
user a feature is experimental agrees with it - the OpenAPI document, the Settings rows, the docs, the changelog's
definition, the Console's connection types and the startup warning. Graduating a feature from the list without
removing its marks, or adding one without marking it, fails here."""
import logging
import os
import re
import tempfile
import unittest
from unittest import mock

from queryapigate import config, create_app, experimental
from tests.helpers import write_connections

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(path):
    with open(os.path.join(ROOT, path)) as f:
        return f.read()


class MarkedEverywhereTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': self.tmp.name, 'QUERYAPIGATE_API_KEY': 'k'})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_openapi_flags_exactly_the_experimental_operations(self):
        spec = create_app().test_client().get('/openapi.json').get_json()
        flagged = {(method, path) for path, ops in spec['paths'].items() for method, op in ops.items()
                   if isinstance(op, dict) and op.get('x-experimental')}
        self.assertEqual(flagged, set(experimental.OPERATIONS))
        for method, path in flagged:
            self.assertTrue(spec['paths'][path][method]['description'].startswith('**Experimental**'))
        again = create_app().test_client().get('/openapi.json').get_json()  # a second build marks it once, not twice
        for method, path in flagged:
            self.assertEqual(again['paths'][path][method]['description'].count('**Experimental**'), 1)

    def test_settings_rows_flag_exactly_the_experimental_settings(self):
        rows = [row for section in config.describe_settings() for row in section['rows']]
        self.assertEqual({r['env'] for r in rows if r['experimental'] and r['env']}, set(experimental.SETTINGS))
        self.assertTrue(next(r for r in rows if r['label'] == 'Live updates')['experimental'])  # GET /events itself

    def test_each_feature_says_so_in_its_own_section_of_the_docs(self):
        for key, feature in experimental.FEATURES.items():
            path, heading = feature['docs']
            with self.subTest(feature=key):
                text = read(path)
                match = re.search(rf'^#+ {re.escape(heading)}\n+(.+)', text, re.M)
                self.assertIsNotNone(match, f'{path} has no "{heading}" heading')
                self.assertTrue(match.group(1).startswith('> **Experimental**'), match.group(1))

    def test_the_changelog_definition_lists_each_one(self):
        definition = read('CHANGELOG.md').split('**Experimental** features are outside that promise', 1)[1]
        definition = definition.split('\n\n', 1)[0].lower()
        for feature in experimental.FEATURES.values():
            self.assertIn(feature['name'].lower(), definition)

    def test_the_console_labels_the_experimental_connection_types(self):
        source = read('frontend/src/features/connections/ConnectionsPage.tsx')
        listed = re.search(r'EXPERIMENTAL_DB_TYPES = \[([^\]]*)\]', source).group(1)
        self.assertEqual(set(re.findall(r"'([a-z0-9]+)'", listed)), set(experimental.DB_TYPES))
        self.assertLessEqual(experimental.DB_TYPES, set(config.SUPPORTED_DB_TYPES))


class StartupWarningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': self.tmp.name, 'QUERYAPIGATE_API_KEY': 'k'})
        patcher.start()
        self.addCleanup(patcher.stop)

    def warnings(self, **env):
        with mock.patch.dict(os.environ, env), self.assertLogs('queryapigate', level=logging.WARNING) as logs:
            logging.getLogger('queryapigate').warning('marker')  # assertLogs needs at least one line
            create_app()
        return [line for line in logs.output if 'experimental' in line]

    def test_nothing_experimental_in_use_says_nothing(self):
        self.assertEqual(self.warnings(), [])

    def test_a_setting_of_one_names_it(self):
        [line] = self.warnings(QUERYAPIGATE_ALERT_ERROR_RATE='30')
        self.assertIn('Alerts are experimental (QUERYAPIGATE_ALERT_ERROR_RATE)', line)

    def test_a_connection_of_an_experimental_type_names_it(self):
        write_connections({'docs': {'db': 'mongo', 'host': 'm', 'database': 'd', 'active': True}})
        [line] = self.warnings()
        self.assertIn('H2, JDBC and MongoDB connections are experimental (connection docs)', line)

    def test_the_events_server_always_says_so(self):
        log = logging.getLogger('queryapigate')
        with self.assertLogs(log, level=logging.WARNING) as logs:
            experimental.warn_in_use(log, {}, {}, also=('live_events',))
        self.assertIn('Live events are experimental (started)', logs.output[0])


if __name__ == '__main__':
    unittest.main()

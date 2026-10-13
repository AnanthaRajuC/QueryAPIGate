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

    def test_graduated_features_say_they_are_stable_and_what_may_be_added(self):
        """Graduated in 0.20 (experimental.GRADUATION): each section opens with that, not an experimental callout."""
        connections = 'documentation/DATABASE_CONNECTION_CONFIGURATION.md'
        for path, heading in (('documentation/API.md', 'Alerts'), ('documentation/API.md', 'Exports to object storage'),
                              ('documentation/API.md', 'Column masking'),
                              (connections, 'Files on S3, GCS, R2 and the web')):
            with self.subTest(heading=heading):
                match = re.search(rf'^#+ {re.escape(heading)}\n+(.+)', read(path), re.M)
                self.assertTrue(match.group(1).startswith('> **Stable** since 0.20'), match.group(1))
                note = read(path)[match.start():match.start() + 900].replace('\n>', ' ')
                self.assertIn('may still be added', ' '.join(note.split()))

    def test_features_graduated_for_1_0_0_rc1_say_what_may_be_added_and_their_limits(self):
        """ADR 0017: each section opens with the promise, what may still be added, and its limits."""
        for heading in ('Descriptions', 'Agent usage', 'Usage quotas', 'Configuration as code'):
            with self.subTest(heading=heading):
                text = read('documentation/API.md')
                match = re.search(rf'^#+ {re.escape(heading)}\n+(.+)', text, re.M)
                self.assertTrue(match.group(1).startswith('> **Stable** since 1.0.0-rc1'), match.group(1))
                callout = text[match.start(1):].split('\n\n', 1)[0].replace('\n>', ' ')
                self.assertIn('may still be added', ' '.join(callout.split()))
                self.assertIn('Limits:', callout)

    def test_the_changelog_definition_lists_each_one(self):
        definition = read('CHANGELOG.md').split('**Experimental** features are outside that promise', 1)[1]
        definition = ' '.join(definition.split('\n\n', 1)[0].lower().split())  # however the paragraph is wrapped
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
        return [line for line in logs.output if 'experimental' in line.lower()]

    def test_nothing_experimental_in_use_says_nothing(self):
        self.assertEqual(self.warnings(), [])

    def test_a_setting_of_one_names_it(self):
        [line] = self.warnings(QUERYAPIGATE_EVENTS_MAX_STREAMS='2')
        self.assertIn('Experimental: Live events (QUERYAPIGATE_EVENTS_MAX_STREAMS)', line)
        self.assertEqual(self.warnings(QUERYAPIGATE_ALERT_ERROR_RATE='30'), [])  # graduated in 0.20

    def test_a_connection_of_an_experimental_type_names_it(self):
        write_connections({'docs': {'db': 'mongo', 'host': 'm', 'database': 'd', 'active': True}})
        [line] = self.warnings()
        self.assertIn('Experimental: H2, JDBC and MongoDB connections (connection docs)', line)

    def test_an_inactive_connection_isnt_in_use(self):
        # `queryapigate init` seeds an inactive H2 template; a fresh server shouldn't warn about it.
        write_connections({'example-h2': {'db': 'h2', 'host': 'h', 'database': 'd', 'active': False}})
        self.assertEqual(self.warnings(), [])

    def test_the_events_server_always_says_so(self):
        log = logging.getLogger('queryapigate')
        with self.assertLogs(log, level=logging.WARNING) as logs:
            experimental.warn_in_use(log, {}, {}, also=('live_events',))
        self.assertIn('Experimental: Live events (started)', logs.output[0])


if __name__ == '__main__':
    unittest.main()

"""Deprecations (BACKLOG #67): queryapigate/deprecations.py is the one list, and everything that announces a
deprecation agrees with it - the route headers, /openapi.json, the log warning (once per process), the changelog's
Deprecated heading and the docs. Nothing is deprecated today (0.15 removed what 0.14 deprecated), so the mechanism is
tested with a real route deprecated for the test, and the documents against whatever the list holds."""
import logging
import os
import re
import sqlite3
import tempfile
import unittest
from unittest import mock

from queryapigate import create_app, deprecations
from tests.helpers import save_query, write_connections

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADMIN = {'X-API-Key': 'admin-key'}
EXAMPLE = {'/q/<name>': {'since': '9.0.0', 'date': '2026-10-04', 'successor': '/v2/q/{name}', 'removal': '10.0.0',
                         'why': 'an example for this test'}}


def read(path):
    with open(os.path.join(ROOT, path)) as f:
        return f.read()


class MechanismTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': self.tmp.name, 'QUERYAPIGATE_API_KEY': 'admin-key'})
        patcher.start()
        self.addCleanup(patcher.stop)
        for name, value in (('ROUTES', EXAMPLE), ('_warned', set())):
            patcher = mock.patch.object(deprecations, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        data = os.path.join(self.tmp.name, 'data.db')
        sqlite3.connect(data).close()
        write_connections({'lite': {'db': 'sqlite', 'database': data, 'active': True}})
        self.client = create_app().test_client()
        save_query(self.client, headers=ADMIN, body={'filename': 'one', 'description': 'd', 'connection_name': 'lite',
                                                     'sql_query': 'SELECT 1 AS n'})

    def test_a_deprecated_route_still_works_and_says_so(self):
        with self.assertLogs('queryapigate', level=logging.WARNING) as logs:
            res = self.client.get('/q/one', headers=ADMIN)
        self.assertEqual(res.get_json(), [{'n': 1}])
        self.assertRegex(res.headers['Deprecation'], r'^@\d+$')  # RFC 9745: when, as @<unix seconds>
        self.assertEqual(res.headers['Link'], '</v2/q/{name}>; rel="successor-version"')
        self.assertTrue(any('/q/<name> is deprecated since 9.0.0' in line for line in logs.output), logs.output)

    def test_it_warns_once_per_process(self):
        with self.assertLogs('queryapigate', level=logging.WARNING) as logs:
            for _ in range(3):
                self.client.get('/q/one', headers=ADMIN)
            logging.getLogger('queryapigate').warning('end')
        self.assertEqual(sum('is deprecated since' in line for line in logs.output), 1)

    def test_other_routes_say_nothing(self):
        self.assertNotIn('Deprecation', self.client.get('/catalog', headers=ADMIN).headers)


class AnnouncedTests(unittest.TestCase):
    def test_nothing_is_deprecated_today(self):
        # When something is, list it here and check the changelog's Deprecated section and API.md name it.
        self.assertEqual((deprecations.ROUTES, deprecations.OTHER), ({}, {}))

    def test_the_spec_marks_exactly_the_deprecated_routes(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': home}):
            spec = create_app().test_client().get('/openapi.json').get_json()
        flagged = {path for path, item in spec['paths'].items() for op in item.values()
                   if isinstance(op, dict) and op.get('deprecated')}
        self.assertEqual(flagged, set(deprecations.ROUTES))

    def test_every_removal_was_announced_first(self):
        # 0.15 removed what 0.14 deprecated: 0.14's entry says so.
        changelog = read('CHANGELOG.md')
        deprecated = re.search(r'^## \[0\.14\.0\].*?^### Deprecated\n(.*?)(?=^###)', changelog, re.M | re.S).group(1)
        self.assertIn('/execute_sql_from_file', deprecated)
        self.assertIn('pre-SQLite JSON files', deprecated)


if __name__ == '__main__':
    unittest.main()

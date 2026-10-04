"""Deprecations (BACKLOG #67): queryapigate/deprecations.py is the one list, and everything that announces a
deprecation agrees with it - the route headers, /openapi.json, the log warning (once per process), the changelog's
Deprecated heading and the docs. Deprecating something without announcing it everywhere, or announcing something
that isn't deprecated, fails here."""
import json
import logging
import os
import re
import tempfile
import unittest
from unittest import mock

from queryapigate import create_app, deprecations
from tests import TEST_DATABASE_URL
from tests.helpers import save_query, write_connections

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADMIN = {'X-API-Key': 'admin-key'}


def read(path):
    with open(os.path.join(ROOT, path)) as f:
        return f.read()


class AppTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': self.tmp.name, 'QUERYAPIGATE_API_KEY': 'admin-key'})
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(deprecations, '_warned', set())  # each test sees its own "first use"
        patcher.start()
        self.addCleanup(patcher.stop)


class RouteTests(AppTestCase):
    def setUp(self):
        super().setUp()
        import sqlite3
        data = os.path.join(self.tmp.name, 'data.db')
        sqlite3.connect(data).close()
        write_connections({'lite': {'db': 'sqlite', 'database': data, 'active': True}})
        self.client = create_app().test_client()
        save_query(self.client, headers=ADMIN, body={'filename': 'one', 'description': 'd', 'connection_name': 'lite',
                                                     'sql_query': 'SELECT 1 AS n'})

    def test_a_deprecated_route_still_works_and_says_so(self):
        for path in deprecations.ROUTES:
            with self.subTest(path=path), self.assertLogs('queryapigate', level=logging.WARNING) as logs:
                res = self.client.post(path, headers=ADMIN, json={'filepath': 'one'})
                self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
                self.assertEqual(res.get_json(), [{'n': 1}])
                self.assertRegex(res.headers['Deprecation'], r'^@\d+$')  # RFC 9745: when, as @<unix seconds>
                self.assertEqual(res.headers['Link'], '</q/{name}>; rel="successor-version"')
                self.assertTrue(any(f'{path} is deprecated since' in line for line in logs.output), logs.output)

    def test_it_warns_once_per_process(self):
        path = next(iter(deprecations.ROUTES))
        with self.assertLogs('queryapigate', level=logging.WARNING) as logs:
            for _ in range(3):
                self.client.post(path, headers=ADMIN, json={'filepath': 'one'})
            logging.getLogger('queryapigate').warning('end')
        self.assertEqual(sum('is deprecated since' in line for line in logs.output), 1)

    def test_other_routes_say_nothing(self):
        res = self.client.get('/q/one', headers=ADMIN)
        self.assertNotIn('Deprecation', res.headers)

    def test_the_spec_marks_exactly_the_deprecated_routes(self):
        spec = self.client.get('/openapi.json').get_json()
        flagged = {path for path, item in spec['paths'].items() for op in item.values()
                   if isinstance(op, dict) and op.get('deprecated')}
        self.assertEqual(flagged, set(deprecations.ROUTES))
        again = self.client.get('/openapi.json').get_json()  # built afresh: marked once, not twice
        for path in flagged:
            self.assertEqual(again['paths'][path]['post']['description'].count('**Deprecated**'), 1)


@unittest.skipIf(TEST_DATABASE_URL, 'the pre-SQLite JSON files are imported into a SQLite store')
class LegacyImportTests(AppTestCase):
    def test_importing_the_json_files_warns(self):
        with open(os.path.join(self.tmp.name, 'db_connections.json'), 'w') as f:
            json.dump({'connections': {'old': {'db': 'sqlite', 'database': 'x.db', 'active': False}}}, f)
        with self.assertLogs('queryapigate', level=logging.WARNING) as logs:
            create_app()
        self.assertTrue(any('pre-SQLite JSON files' in line and 'deprecated since' in line for line in logs.output))

    def test_a_home_without_them_says_nothing(self):
        with self.assertLogs('queryapigate', level=logging.WARNING) as logs:
            create_app()
            logging.getLogger('queryapigate').warning('end')
        self.assertFalse(any('deprecated' in line for line in logs.output))


class AnnouncedTests(unittest.TestCase):
    def test_the_changelog_lists_each_under_deprecated(self):
        changelog = read('CHANGELOG.md')
        sections = re.findall(r'^### Deprecated\n(.*?)(?=^##)', changelog, re.M | re.S)
        listed = '\n'.join(sections)
        for path in deprecations.ROUTES:
            self.assertIn(f'`POST {path}`', listed)
        self.assertEqual(set(deprecations.OTHER), {'legacy_json_import'})  # a new entry needs its own line here
        self.assertIn('Importing the pre-SQLite JSON files', listed)

    def test_the_docs_say_so_where_each_route_is_described(self):
        api = read('documentation/API.md')
        for path in deprecations.ROUTES:
            with self.subTest(path=path):
                row = next(line for line in api.splitlines() if line.startswith(f'| [`{path}`]'))
                self.assertIn('Deprecated', row)

    def test_removal_comes_after_deprecation(self):
        version = lambda v: tuple(int(p) for p in v.split('.'))  # noqa: E731
        for entry in [*deprecations.ROUTES.values(), *deprecations.OTHER.values()]:
            self.assertGreater(version(entry['removal']), version(entry['since']))


if __name__ == '__main__':
    unittest.main()

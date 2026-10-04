"""The upgrade guarantee (BACKLOG #65): a store any released 0.x version created starts under this code - its tables
upgraded, its JSON files imported - with everything in it intact, or startup refuses with a message saying what is
wrong. Never a server that starts and then fails, or quietly loses data.

Each tests/fixtures/stores/<version>.tar.gz is a home that release's own server built through its own API
(fixtures/stores/generate.py): a connection, a saved query with two versions, a run of it, a scoped key and a role.
A new release adds its fixture; this test then covers it with no change here.
"""
import glob
import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
import unittest
from unittest import mock

from queryapigate import create_app, db, history
from tests import TEST_DATABASE_URL

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fixtures', 'stores')
ADMIN = {'X-API-Key': 'fixture-admin-key'}


def fixture_versions():
    return sorted((os.path.basename(path)[:-len('.tar.gz')] for path in glob.glob(os.path.join(FIXTURES, '*.tar.gz'))),
                  key=lambda v: tuple(int(part) for part in v.split('.')))


class FixtureTestCase(unittest.TestCase):
    def open_home(self, version):
        """A fresh home holding that version's store, with the test's QUERYAPIGATE_HOME pointed at it."""
        home = tempfile.mkdtemp(prefix=f'qag-upgrade-{version}-')
        self.addCleanup(shutil.rmtree, home, True)
        with tarfile.open(os.path.join(FIXTURES, f'{version}.tar.gz')) as tar:
            tar.extractall(home, filter='data') if hasattr(tarfile, 'data_filter') else tar.extractall(home)
        patcher = mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': home, 'QUERYAPIGATE_API_KEY': ADMIN['X-API-Key']})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(db.close)
        self.addCleanup(history.flush)
        with open(os.path.join(FIXTURES, f'{version}.expected.json')) as f:
            return home, json.load(f)


@unittest.skipIf(TEST_DATABASE_URL, 'the fixtures are SQLite stores and JSON files (PostgreSQL dumps: future work)')
class UpgradeTests(FixtureTestCase):
    def test_there_is_a_fixture_for_every_supported_release(self):
        self.assertGreaterEqual(len(fixture_versions()), 6)

    def test_every_release_store_starts_with_its_data_intact(self):
        for version in fixture_versions():
            with self.subTest(version=version):
                self.check(version)

    def get(self, client, path):
        res = client.get(path, headers=ADMIN)
        self.assertEqual(res.status_code, 200, f'{path}: {res.get_data(as_text=True)}')
        return res.get_json()

    def check(self, version):
        home, expected = self.open_home(version)
        self.assertEqual(expected['skipped'], [])
        client = create_app().test_client()

        connection = self.get(client, '/api/v1/connections/films')
        self.assertEqual(connection['db'], 'sqlite')

        query = self.get(client, '/api/v1/queries/films_by_rating')
        self.assertEqual([v['version'] for v in query['versions']], [1, 2])
        self.assertEqual(query['published_version'], 2)  # the newest was always the one served before publishing

        runs = self.get(client, '/api/v1/history?query=films_by_rating')['items']
        self.assertEqual([(r['query'], r['version'], r['status'], r['kind']) for r in runs],
                         [('films_by_rating', 2, 'success', 'saved')])

        key = self.get(client, '/api/v1/api-keys/reporting')
        self.assertEqual(key['connections'], ['films'])
        role = self.get(client, '/api/v1/roles/analyst')
        self.assertEqual(role['connections'], ['films'])

        # The connection names the fixture's original home; point it at this one and the query still runs.
        res = client.patch('/api/v1/connections/films', headers=ADMIN,
                           json={'database': os.path.join(home, 'data.db')})
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        res = client.get('/q/films_by_rating?rating=PG', headers=ADMIN)
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        self.assertEqual([row['title'] for row in res.get_json()], ['Alpha', 'Gamma'])

        self.assertTrue(self.get(client, '/api/v1/audit')['items'])


@unittest.skipIf(TEST_DATABASE_URL, 'the fixtures are SQLite stores')
class RefusalTests(FixtureTestCase):
    """A store this version can't run on stops startup with a message saying what is wrong and what to do - before
    anything is changed, rather than a server that starts and then fails on the first request that touches it."""

    def store(self, version='0.12.0'):
        home, _ = self.open_home(version)
        return os.path.join(home, 'queryapigate.db')

    def alter(self, path, *statements):
        conn = sqlite3.connect(path)
        for statement in statements:
            conn.execute(statement)
        conn.commit()
        conn.close()

    def refusal(self):
        with self.assertRaises(ValueError) as caught:
            create_app()
        return str(caught.exception)

    def test_a_missing_column_is_named(self):
        self.alter(self.store(), 'ALTER TABLE api_keys DROP COLUMN expires_at')
        message = self.refusal()
        self.assertIn('api_keys is missing expires_at', message)
        self.assertIn('backup', message)

    def test_a_store_from_a_newer_release_is_left_alone(self):
        path = self.store()
        self.alter(path, 'UPDATE schema_version SET version = 99')
        self.assertIn('newer release', self.refusal())
        conn = sqlite3.connect(path)
        self.assertEqual(conn.execute('SELECT version FROM schema_version').fetchone()[0], 99)
        conn.close()

    def test_a_columnar_history_table_with_rows_is_not_dropped(self):
        path = self.store('0.10.0')
        self.alter(path, "INSERT INTO execution_history (query_name, version, executed_at, status) "
                         "VALUES ('q', 1, 'now', 'success')")
        self.assertIn('has rows but not the shape', self.refusal())
        conn = sqlite3.connect(path)
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM execution_history').fetchone()[0], 1)
        conn.close()

    def test_the_cli_says_so_and_exits_2(self):
        self.alter(self.store(), 'UPDATE schema_version SET version = 99')
        from queryapigate import cli
        with mock.patch('sys.stderr') as stderr:
            self.assertEqual(cli.main(['init']), 2)
        self.assertIn('newer release', ''.join(call.args[0] for call in stderr.write.call_args_list))


if __name__ == '__main__':
    unittest.main()

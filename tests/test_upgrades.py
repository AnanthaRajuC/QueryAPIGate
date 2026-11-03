"""The upgrade guarantee (BACKLOG #65): a store any released 0.x version created starts under this code - its tables
upgraded, its JSON files imported - with everything in it intact, or startup refuses with a message saying what is
wrong. Never a server that starts and then fails, or quietly loses data.

Each tests/fixtures/stores/<version>.tar.gz is a home that release's own server built through its own API
(fixtures/stores/generate.py): a connection, a saved query with two versions, a run of it, a scoped key and a role.
A new release adds its fixture; this test then covers it with no change here.

Releases with the PostgreSQL store (0.12.0 on) also have <version>.postgres.tar.gz: the same estate in a PostgreSQL
schema, dumped as plain SQL. When the suite runs against PostgreSQL, each is restored into the test's own schema and
checked the same way - which is also a test of restoring a pg_dump (BACKLOG #71).

Releases with the ClickHouse run store (0.45.0 on, ADR 0029) also have <version>.runstore.tar.gz: the home, whose
metadata store says its runs are in ClickHouse, and runstore/, those tables and their rows. When the suite runs with
a run store, each is loaded into the test's own ClickHouse database and checked the same way, and its tables must be
at this code's run store schema: 0.45.0's are at version 1, so this tests that migration on a real release's tables.
"""
import glob
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tarfile
import tempfile
import unittest
from unittest import mock

from queryapigate import create_app, db, events, history, runstore
from tests import TEST_DATABASE_URL, TEST_RUN_STORE_URL
from tests.helpers import needs_sqlite_335

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fixtures', 'stores')
ADMIN = {'X-API-Key': 'fixture-admin-key'}


def fixture_versions(kind=''):
    """The releases with a fixture: SQLite/JSON homes (kind ''), or PostgreSQL dumps (kind '.postgres')."""
    suffix = kind + '.tar.gz'
    names = [os.path.basename(p)[:-len(suffix)] for p in glob.glob(os.path.join(FIXTURES, '*' + suffix))]
    return sorted((n for n in names if n.replace('.', '').isdigit()), key=lambda v: tuple(int(p) for p in v.split('.')))


def restore_postgres(sql):
    """Load a fixture's plain-SQL dump into this test's own schema (tests/__init__.py maps each home to one), in
    place of the empty one QueryAPIGate would create: the dump creates the schema itself."""
    import psycopg2
    schema = db.postgres_schema()
    db.close()
    statements = '\n'.join(line for line in sql.splitlines() if not line.startswith('\\'))  # psql-only: \restrict
    conn = psycopg2.connect(TEST_DATABASE_URL)
    conn.autocommit = True
    try:
        cursor = conn.cursor()
        cursor.execute(f'DROP SCHEMA {schema} CASCADE')
        cursor.execute(statements.replace('qag_fixture', schema))
    finally:
        conn.close()


def restore_run_store(home):
    """Load a fixture's run store (runstore/ in its home) into this test's own ClickHouse database: the tables, their
    rows, then the views that write to them, so no row is counted twice. TTL merges stay stopped on it: the fixture's
    events are older than the event log keeps, and ClickHouse would drop them partway through a test. The metadata
    store's marker names the ClickHouse the fixture was made on; it's pointed at this one, as the connection is
    pointed at this home's data.db."""
    name = runstore.database()
    runstore.execute(f'DROP DATABASE IF EXISTS {name} SYNC', db_name='default')
    runstore.execute(f'CREATE DATABASE {name}', db_name='default')
    with open(os.path.join(home, 'runstore', 'tables.json')) as f:
        tables = json.load(f)
    for table in tables:
        ddl = table['ddl'].replace('qag_fixture.', name + '.')
        runstore.execute(re.sub(r'DEFINER = \S+', 'DEFINER = CURRENT_USER', ddl), db_name=name)
        if table['engine'].endswith('MergeTree'):
            runstore.execute(f'SYSTEM STOP TTL MERGES {name}.{table["name"]}', db_name=name)
        rows = os.path.join(home, 'runstore', f'{table["name"]}.native')
        if os.path.exists(rows):
            with open(rows, 'rb') as f:
                runstore._client().request({'database': name, 'query': f'INSERT INTO {table["name"]} FORMAT Native'},
                                           f.read())
    conn = sqlite3.connect(os.path.join(home, 'queryapigate.db'))
    conn.execute('UPDATE run_store SET location = ?', (runstore.location(),))
    conn.commit()
    conn.close()
    return name


class FixtureTestCase(unittest.TestCase):
    def open_home(self, version, kind=''):
        """A fresh home holding that version's store, with the test's QUERYAPIGATE_HOME pointed at it."""
        home = tempfile.mkdtemp(prefix=f'qag-upgrade-{version}-')
        self.addCleanup(shutil.rmtree, home, True)
        with tarfile.open(os.path.join(FIXTURES, f'{version}{kind}.tar.gz')) as tar:
            tar.extractall(home, filter='data') if hasattr(tarfile, 'data_filter') else tar.extractall(home)
        patcher = mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': home, 'QUERYAPIGATE_API_KEY': ADMIN['X-API-Key']})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(db.close)
        self.addCleanup(history.flush)
        with open(os.path.join(FIXTURES, f'{version}{kind}.expected.json')) as f:
            return home, json.load(f)


class StoreChecks(FixtureTestCase):
    """What every fixture must read back, whichever store it is in - run by the three classes below."""
    kind = ''

    def restore(self, home):
        """Put a fixture's PostgreSQL dump or run store where this run's settings look for them."""
        if self.kind == '.postgres':
            with open(os.path.join(home, 'store.sql')) as f:
                restore_postgres(f.read())
        elif self.kind == '.runstore':
            name = restore_run_store(home)

            def drop():
                history.flush()  # this test's last runs first, or they'd be written to a database just dropped
                runstore.execute(f'DROP DATABASE IF EXISTS {name} SYNC', db_name='default')
            self.addCleanup(drop)

    def check_every_release(self):
        for version in fixture_versions(self.kind):
            with self.subTest(version=version):
                self.check(version)

    def get(self, client, path):
        res = client.get(path, headers=ADMIN)
        self.assertEqual(res.status_code, 200, f'{path}: {res.get_data(as_text=True)}')
        return res.get_json()

    def check(self, version):
        home, expected = self.open_home(version, self.kind)
        if tuple(int(p) for p in version.split('.')) < (0, 11):
            # Its data is (partly) in the pre-SQLite JSON files, which 0.15 stopped importing: refused, saying what
            # to do - never started without it.
            with self.assertRaisesRegex(ValueError, r'0\.10 or older .* Start QueryAPIGate 0\.14 on this folder'):
                create_app()
            return
        self.assertEqual(expected['skipped'], [])
        self.restore(home)
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
        if self.kind == '.runstore':
            self.check_run_store(client, expected)

    def check_run_store(self, client, expected):
        """A run store's tables at this code's schema, whichever release made them, and nothing lost on the way."""
        history.flush()
        self.assertEqual(runstore.query('SELECT max(version) AS v FROM run_store_schema')[0]['v'],
                         runstore._SCHEMA_VERSION)
        # The ad-hoc run's statement, in the hourly rollups as its hash whether the release kept the text (0.45) or not
        sql = "SELECT title FROM film WHERE rating = 'R'"
        self.assertEqual([row['statement'] for row in runstore.query(
            'SELECT DISTINCT statement FROM run_hours WHERE query_name IS NULL')],
            ['sha:' + hashlib.sha256(sql.encode()).hexdigest()])
        self.assertEqual(sum(row['runs'] for row in runstore.query('SELECT runs FROM run_hours')),
                         expected['run_store']['rows']['events'] + 1)
        runs = self.get(client, '/api/v1/history')['items']
        self.assertEqual(sorted(r['kind'] for r in runs), ['adhoc', 'saved', 'saved'])  # and the run just made
        self.assertEqual([r['sql'] for r in runs if r['kind'] == 'adhoc'], [sql])

    def check_event_ids(self, version):
        """ADR 0002 §3, ADR 0020 §4: the run's history row id was its event id, and stays so. A client holding the id
        before it resumes with the run; the next event's id is above every id the old release could have sent."""
        home, _ = self.open_home(version, self.kind)
        self.restore(home)
        if self.kind == '.runstore':
            newest = runstore.query('SELECT max(id) AS id FROM events')[0]['id']
        else:
            newest = db.connection().execute('SELECT MAX(rowid) FROM execution_history').fetchone()[0]
        # However old the run: eleven years, within ClickHouse's DateTime, which ends in 2106
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_EVENTS_RETENTION_HOURS': '100000'}):
            client = create_app().test_client()
        [(event_id, payload_json)] = events._replay_page(newest - 1, None, {})
        payload = json.loads(payload_json)
        self.assertEqual((event_id, payload['type'], payload['kind'], payload['query'], payload['version']),
                         (newest, 'query.run', 'saved', 'films_by_rating', 2))
        res = client.patch('/api/v1/connections/films', headers=ADMIN, json={'database': os.path.join(home, 'data.db')})
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        self.assertEqual(client.get('/q/films_by_rating?rating=PG', headers=ADMIN).status_code, 200)
        history.flush()
        if self.kind == '.runstore':  # a run store's ids are minted from the clock: above the old one, not next to it
            [(after, _)] = events._replay_page(newest, None, {}, upto=runstore.MAX_ID)
            self.assertGreater(after, newest)
        else:
            self.assertEqual([row[0] for row in events._replay_page(newest, None, {})], [newest + 1])


@unittest.skipIf(TEST_DATABASE_URL, 'the SQLite stores and JSON homes')
@unittest.skipIf(TEST_RUN_STORE_URL, "each fixture's runs are in its own metadata store, until copied to a run store")
class UpgradeTests(StoreChecks):
    def test_there_is_a_fixture_for_every_supported_release(self):
        self.assertGreaterEqual(len(fixture_versions()), 7)

    def test_every_release_store_starts_with_its_data_intact(self):
        self.check_every_release()

    def test_every_release_store_keeps_its_event_ids(self):
        for version in fixture_versions()[fixture_versions().index('0.11.0'):]:
            with self.subTest(version=version):
                self.check_event_ids(version)


@unittest.skipUnless(TEST_DATABASE_URL, 'set QUERYAPIGATE_TEST_DATABASE_URL to run')
@unittest.skipIf(TEST_RUN_STORE_URL, "each fixture's runs are in its own metadata store, until copied to a run store")
class PostgresUpgradeTests(StoreChecks):
    kind = '.postgres'

    def test_there_is_a_fixture_for_every_release_with_the_postgres_store(self):
        self.assertLessEqual({'0.12.0', '0.13.0', '0.14.0', '0.15.0'}, set(fixture_versions('.postgres')))

    def test_every_release_store_starts_with_its_data_intact(self):
        self.check_every_release()

    def test_every_release_store_keeps_its_event_ids(self):
        for version in fixture_versions(self.kind):
            with self.subTest(version=version):
                self.check_event_ids(version)


@unittest.skipIf(TEST_DATABASE_URL, "the fixtures' metadata stores are SQLite")
@unittest.skipUnless(TEST_RUN_STORE_URL, 'set QUERYAPIGATE_TEST_RUN_STORE_URL to run')
class RunStoreUpgradeTests(StoreChecks):
    kind = '.runstore'

    def test_there_is_a_fixture_for_every_release_with_the_run_store(self):
        self.assertLessEqual({'0.45.0', '0.46.0', '0.47.0', '0.48.0'}, set(fixture_versions(self.kind)))

    def test_every_release_store_starts_with_its_data_intact(self):
        self.check_every_release()

    def test_every_release_store_keeps_its_event_ids(self):
        for version in fixture_versions(self.kind):
            with self.subTest(version=version):
                self.check_event_ids(version)


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

    @needs_sqlite_335
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

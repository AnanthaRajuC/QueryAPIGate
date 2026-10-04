"""Backups that have been restored (BACKLOG #71): fill a store through the API, back it up the way DEPLOYMENT.md says,
lose it, restore it, start against it - and the admin API sees exactly what it saw before, old API keys still work,
and encrypted connection passwords decrypt with the same QUERYAPIGATE_SECRET_KEY (and only with it).

SQLite always; PostgreSQL when QUERYAPIGATE_TEST_DATABASE_URL is set and pg_dump/pg_restore are on PATH."""
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from unittest import mock

from cryptography.fernet import Fernet

from queryapigate import cli, create_app, db, history
from tests import TEST_DATABASE_URL

ADMIN = {'X-API-Key': 'admin-key'}
SNAPSHOT = ('/api/v1/connections', '/api/v1/queries', '/api/v1/queries/films', '/api/v1/api-keys', '/api/v1/roles',
            '/api/v1/audit', '/api/v1/history?limit=1000', '/api/v1/connections/deleted')


class BackupRestoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = os.path.join(self.tmp.name, 'data.db')
        conn = sqlite3.connect(self.data)
        conn.execute('CREATE TABLE film (id INTEGER, title TEXT)')
        conn.executemany('INSERT INTO film VALUES (?, ?)', [(1, 'Alpha'), (2, 'Beta')])
        conn.commit()
        conn.close()
        self.secret_key = Fernet.generate_key().decode()
        self.addCleanup(db.close)

    def use_home(self, home, **env):
        """Point this process at `home` (and its own store), as a fresh server start would."""
        history.flush()
        db.close()
        os.makedirs(home, exist_ok=True)
        patcher = mock.patch.dict(os.environ, {
            'QUERYAPIGATE_HOME': home, 'QUERYAPIGATE_API_KEY': 'admin-key', 'QUERYAPIGATE_SECRET_KEY': self.secret_key,
            'QUERYAPIGATE_HISTORY_FLUSH_INTERVAL': '0', **env})
        patcher.start()
        self.addCleanup(patcher.stop)
        return create_app().test_client()

    def call(self, client, method, path, status, **kwargs):
        res = client.open(path, method=method, headers=ADMIN, **kwargs)
        self.assertEqual(res.status_code, status, f'{method} {path}: {res.get_data(as_text=True)}')
        return res.get_json()

    def populate(self, client):
        """A small, real estate - and the scoped key's secret, which only ever exists outside the store."""
        self.call(client, 'POST', '/api/v1/connections', 201,
                  json={'name': 'lite', 'db': 'sqlite', 'database': self.data, 'active': True})
        self.call(client, 'POST', '/api/v1/connections', 201, json={  # a password stored encrypted, never reached
            'name': 'warehouse', 'db': 'postgres', 'host': '127.0.0.1', 'port': 1, 'database': 'w', 'user': 'u',
            'password': 'pa55word', 'active': True})
        self.call(client, 'POST', '/api/v1/queries', 201, json={
            'name': 'films', 'description': 'All films', 'sql': 'SELECT title FROM film ORDER BY id',
            'connection_name': 'lite', 'collection': 'catalog', 'publish': True})
        self.call(client, 'POST', '/api/v1/queries/films/versions', 201, json={
            'description': 'Films, newest first', 'sql': 'SELECT title FROM film ORDER BY id DESC',
            'connection_name': 'lite', 'publish': True})
        self.call(client, 'POST', '/api/v1/queries/films/versions', 201, json={  # a draft
            'description': 'Films, by title', 'sql': 'SELECT title FROM film ORDER BY title',
            'connection_name': 'lite'})
        secret = self.call(client, 'POST', '/api/v1/api-keys', 201,
                           json={'name': 'partner', 'collections': ['catalog']})['secret']
        self.call(client, 'POST', '/api/v1/roles', 201, json={'name': 'analyst', 'connections': ['lite']})
        self.call(client, 'POST', '/api/v1/api-keys', 201, json={'name': 'temp', 'connections': ['lite']})
        self.call(client, 'DELETE', '/api/v1/api-keys/temp', 204)
        for _ in range(3):
            self.assertEqual(client.get('/q/films', headers={'X-API-Key': secret}).status_code, 200)
        self.call(client, 'POST', '/execute_sql', 200, json={'sql': 'SELECT COUNT(*) AS n FROM film',
                                                             'connection_name': 'lite'})
        history.flush()
        return secret

    def snapshot(self, client):
        return {path: self.call(client, 'GET', path, 200) for path in SNAPSHOT}

    def check_restored(self, client, before, secret):
        after = self.snapshot(client)
        for path in SNAPSHOT:
            self.assertEqual(after[path], before[path], path)
        self.assertEqual(len(after['/api/v1/history?limit=1000']['items']), 4)
        # The scoped key's secret still works: only its hash is stored, and the hash came back
        res = client.get('/q/films', headers={'X-API-Key': secret})
        self.assertEqual((res.status_code, [r['title'] for r in res.get_json()]), (200, ['Beta', 'Alpha']))
        # The encrypted password decrypts: the run gets as far as connecting (nothing listens on port 1)
        res = client.post('/execute_sql', headers=ADMIN, json={'sql': 'SELECT 1', 'connection_name': 'warehouse'})
        self.assertEqual(res.get_json()['code'], 'connection_failed', res.get_json())


@unittest.skipIf(TEST_DATABASE_URL, 'the SQLite store')
class SqliteBackupRestoreTests(BackupRestoreCase):
    def test_backup_restore_into_a_new_home(self):
        original = os.path.join(self.tmp.name, 'home')
        client = self.use_home(original)
        secret = self.populate(client)
        before = self.snapshot(client)
        backup = os.path.join(self.tmp.name, 'backup.db')
        self.assertEqual(cli.main(['backup', backup]), 0)

        shutil.rmtree(original)  # the store is lost
        restored = os.path.join(self.tmp.name, 'restored')
        os.makedirs(restored)
        shutil.copy(backup, os.path.join(restored, 'queryapigate.db'))
        self.check_restored(self.use_home(restored), before, secret)

    def test_without_the_secret_key_encrypted_passwords_are_lost(self):
        original = os.path.join(self.tmp.name, 'home')
        self.populate(self.use_home(original))
        backup = os.path.join(self.tmp.name, 'backup.db')
        self.assertEqual(cli.main(['backup', backup]), 0)
        restored = os.path.join(self.tmp.name, 'restored')
        os.makedirs(restored)
        shutil.copy(backup, os.path.join(restored, 'queryapigate.db'))
        client = self.use_home(restored, QUERYAPIGATE_SECRET_KEY=Fernet.generate_key().decode())  # a different key
        res = client.post('/execute_sql', headers=ADMIN, json={'sql': 'SELECT 1', 'connection_name': 'warehouse'})
        self.assertEqual(res.get_json()['code'], 'password_undecryptable')
        self.assertEqual(client.get('/q/films', headers=ADMIN).status_code, 200)  # everything else is intact

    def test_backup_refuses_to_overwrite_or_to_copy_the_store_onto_itself(self):
        home = os.path.join(self.tmp.name, 'home')
        self.use_home(home)
        backup = os.path.join(self.tmp.name, 'backup.db')
        open(backup, 'w').close()
        with mock.patch('sys.stderr'):
            self.assertEqual(cli.main(['backup', backup]), 2)
            self.assertEqual(cli.main(['backup', os.path.join(home, 'queryapigate.db'), '--force']), 2)
        self.assertEqual(cli.main(['backup', backup, '--force']), 0)


PG_TOOLS = shutil.which('pg_dump') and shutil.which('pg_restore')


@unittest.skipUnless(TEST_DATABASE_URL and os.environ.get('QUERYAPIGATE_TEST_REQUIRE_PG_TOOLS'), 'CI only')
class PostgresToolsPresentTests(unittest.TestCase):
    def test_pg_dump_and_pg_restore_are_installed(self):  # else the restore test below would quietly skip
        self.assertTrue(PG_TOOLS, 'pg_dump and pg_restore must be on PATH')


@unittest.skipUnless(TEST_DATABASE_URL and PG_TOOLS, 'needs QUERYAPIGATE_TEST_DATABASE_URL, pg_dump and pg_restore')
class PostgresBackupRestoreTests(BackupRestoreCase):
    def test_pg_dump_drop_pg_restore(self):
        home = os.path.join(self.tmp.name, 'home')
        client = self.use_home(home)
        secret = self.populate(client)
        before = self.snapshot(client)
        schema = db.postgres_schema()
        dump = os.path.join(self.tmp.name, 'store.dump')
        subprocess.run(['pg_dump', '--format=custom', f'--schema={schema}', '--no-owner', '--no-privileges',
                        f'--file={dump}', TEST_DATABASE_URL], check=True)

        db.close()  # the store is lost: its whole schema dropped, outside QueryAPIGate
        import psycopg2
        conn = psycopg2.connect(TEST_DATABASE_URL)
        conn.autocommit = True
        conn.cursor().execute(f'DROP SCHEMA {schema} CASCADE')
        conn.close()
        subprocess.run(['pg_restore', '--no-owner', '--no-privileges', f'--dbname={TEST_DATABASE_URL}', dump],
                       check=True)
        self.check_restored(self.use_home(home), before, secret)

    def test_backup_command_prints_the_pg_dump_command(self):
        self.use_home(os.path.join(self.tmp.name, 'home'))
        with mock.patch('sys.stderr') as stderr:
            self.assertEqual(cli.main(['backup', 'out.dump']), 2)
        said = ''.join(c.args[0] for c in stderr.write.call_args_list)
        self.assertIn(f'pg_dump --format=custom --schema={db.postgres_schema()}', said)
        self.assertNotIn('pw', said.replace('$QUERYAPIGATE_DATABASE_URL', ''))  # never the URL's password


if __name__ == '__main__':
    unittest.main()

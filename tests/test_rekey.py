"""Rotating the encryption key (ADR 0009, BACKLOG #90): several keys in QUERYAPIGATE_SECRET_KEY - the first encrypts,
every one decrypts - and `queryapigate rekey`, which moves every stored password to the first."""
import contextlib
import io
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest import mock

from cryptography.fernet import Fernet

from queryapigate import admins, alerts, cli, config, create_app, db, destinations, rekey, store
from queryapigate.errors import ApiError

OLD, NEW = Fernet.generate_key().decode(), Fernet.generate_key().decode()


class RekeyTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = os.path.join(self.tmp.name, 'data.db')
        conn = sqlite3.connect(self.db_path)
        conn.execute('CREATE TABLE t (id INTEGER)')
        conn.execute('INSERT INTO t VALUES (1)')
        conn.commit()
        conn.close()
        self.env = mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': self.tmp.name, 'QUERYAPIGATE_API_KEY': 'admin-key',
                                                'QUERYAPIGATE_SECRET_KEY': OLD})
        self.env.start()
        self.addCleanup(self.env.stop)
        db.init_schema()
        with db.transaction() as conn:
            for table in ('connections', 'destinations', 'audit_log'):
                conn.execute(f'DELETE FROM {table}')
        store.update_connections({'a': {'db': 'sqlite', 'database': self.db_path, 'password': 'pw-a', 'active': True},
                                  'b': {'db': 'sqlite', 'database': self.db_path, 'password': 'pw-b', 'active': True},
                                  'env': {'db': 'sqlite', 'database': self.db_path, 'password': '${PW}'}})
        destinations.create('bucket', {'url': 's3://bucket/exports/', 'user': 'AKIA1', 'password': 'pw-d'})

    def add(self, name, password):
        store.update_connections({name: {'db': 'sqlite', 'database': self.db_path, 'password': password,
                                         'active': True}})

    def keys(self, value):
        return mock.patch.dict(os.environ, {'QUERYAPIGATE_SECRET_KEY': value})

    def raw(self, table, name):
        row = db.connection().execute(f'SELECT details_json FROM {table} WHERE name = ?', (name,)).fetchone()
        return json.loads(row['details_json'])['password']

    def under(self, key, table, name):
        """The plaintext, decrypted with exactly this one key - raising when it isn't under it."""
        return Fernet(key.encode()).decrypt(self.raw(table, name)[len('enc:'):].encode()).decode()


class SeveralKeysTests(RekeyTestCase):
    def test_the_first_key_encrypts_and_every_listed_key_decrypts(self):
        with self.keys(f'{NEW},{OLD}'):
            self.assertEqual(store.get_connection('a')['password'], 'pw-a')  # stored under OLD
            self.add('c', 'pw-c')
            self.assertEqual(self.under(NEW, 'connections', 'c'), 'pw-c')
        with self.keys(OLD), self.assertRaises(ApiError) as caught:
            store.get_connection('c')  # an instance still on OLD alone can't read it yet
        self.assertEqual(caught.exception.code, 'password_undecryptable')

    def test_one_key_behaves_as_before(self):
        self.assertEqual(config.secret_key(), OLD)
        self.assertEqual(self.under(OLD, 'connections', 'a'), 'pw-a')

    def test_spaces_and_empty_entries_are_ignored(self):
        with self.keys(f' {NEW} , ,{OLD} '):
            self.assertEqual(config.secret_keys(), [NEW, OLD])
            config.check_secret_keys()

    def test_a_bad_key_is_named_by_its_place_in_the_list(self):
        with self.keys(f'{NEW},not-a-key'), self.assertRaisesRegex(ValueError, 'Key 2 of 2 in QUERYAPIGATE_SECRET_KEY'):
            config.check_settings()

    def test_the_same_key_twice_is_refused(self):
        with self.keys(f'{NEW},{OLD},{NEW}'), self.assertRaisesRegex(ValueError, 'Key 3 of 3 .* same as key 1'):
            config.check_secret_keys()

    def test_a_password_no_listed_key_decrypts_says_so(self):
        with self.keys(NEW), self.assertRaises(ApiError) as caught:
            store.get_connection('a')
        self.assertIn('no longer listed', caught.exception.message)
        with self.keys(f'{NEW},{Fernet.generate_key().decode()}'), self.assertRaises(ApiError) as caught:
            store.get_connection('a')
        self.assertIn('any of the 2 keys', caught.exception.message)

    def test_the_safe_rollout_order_never_leaves_an_instance_unable_to_read(self):
        # Step 0 of the rotation: old,new everywhere - still encrypting with OLD, already able to read NEW.
        with self.keys(f'{OLD},{NEW}'):
            self.add('c', 'pw-c')
        self.assertEqual(store.get_connection('c')['password'], 'pw-c')  # an instance not restarted yet: OLD alone
        with self.keys(f'{NEW},{OLD}'):
            self.add('d', 'pw-d')
        with self.keys(f'{OLD},{NEW}'):
            self.assertEqual(store.get_connection('d')['password'], 'pw-d')  # one still on step 0 reads it


class RekeyTests(RekeyTestCase):
    def test_check_reports_what_is_under_each_key(self):
        with self.keys(f'{NEW},{OLD}'):
            found = rekey.check()
        self.assertEqual(found, {'keys': 2, 'values': 3, 'by_key': [0, 3], 'under_old_keys': 3, 'undecryptable': []})
        self.assertEqual(self.under(OLD, 'connections', 'a'), 'pw-a')  # check changes nothing

    def test_rekey_moves_every_password_to_the_first_key(self):
        with self.keys(f'{NEW},{OLD}'):
            done = rekey.rekey('tester', via='token')
        self.assertEqual((done['rekeyed'], done['by_key'], done['under_old_keys']), (3, [3, 0], 0))
        self.assertEqual(self.under(NEW, 'connections', 'a'), 'pw-a')
        self.assertEqual(self.under(NEW, 'connections', 'b'), 'pw-b')
        self.assertEqual(self.under(NEW, 'destinations', 'bucket'), 'pw-d')
        self.assertEqual(self.raw('connections', 'env'), '${PW}')  # a reference is no secret, left as written
        with self.keys(NEW):  # the old key can go
            self.assertEqual(store.get_connection('a')['password'], 'pw-a')
            self.assertEqual(destinations._usable(destinations.get('bucket'))['password'], 'pw-d')

    def test_running_it_again_changes_nothing(self):
        with self.keys(f'{NEW},{OLD}'):
            rekey.rekey('tester')
            before = self.raw('connections', 'a')
            self.assertEqual(rekey.rekey('tester')['rekeyed'], 0)
        self.assertEqual(self.raw('connections', 'a'), before)

    def test_a_password_no_key_decrypts_stops_it_and_nothing_changes(self):
        stray = Fernet.generate_key().decode()
        with self.keys(stray):
            store.update_connections({'lost': {'db': 'sqlite', 'database': self.db_path, 'password': 'pw-x'}})
        with self.keys(f'{NEW},{OLD}'):
            with self.assertRaises(ApiError) as caught:
                rekey.rekey('tester')
            self.assertEqual(rekey.check()['undecryptable'], [{'type': 'connection', 'name': 'lost'}])
        self.assertEqual((caught.exception.code, caught.exception.status), ('password_undecryptable', 409))
        self.assertIn("connection 'lost'", caught.exception.message)
        self.assertEqual(self.under(OLD, 'connections', 'a'), 'pw-a')  # rolled back: still under OLD
        self.assertNotIn('rekey', [e['action'] for e in store.read_audit_log()])

    def test_without_a_key_it_refuses(self):
        with self.keys(''):
            for run in (rekey.check, lambda: rekey.rekey('tester')):
                with self.assertRaises(ApiError) as caught:
                    run()
                self.assertEqual(caught.exception.code, 'secret_key_required')

    def test_it_is_audited_with_counts_never_values(self):
        with self.keys(f'{NEW},{OLD}'):
            rekey.rekey('tester', via='token')
        entry = [e for e in store.read_audit_log() if e['action'] == 'rekey'][0]
        self.assertEqual((entry['actor'], entry['via'], entry['changes']),
                         ('tester', 'token', {'rekeyed': 3, 'values': 3, 'keys': 2}))
        self.assertNotIn('pw-', json.dumps(entry))

    def test_a_connection_saved_meanwhile_waits_and_keeps_its_change(self):
        saved = threading.Event()
        real = rekey.report

        def slow_report(conn=None):
            # inside rekey()'s transaction: another writer starts now, and must wait for this one to commit
            if conn is not None and not saved.is_set():
                saved.set()
                writer.start()
                time.sleep(0.3)
                self.assertTrue(writer.is_alive())
            return real(conn)

        def save():  # QUERYAPIGATE_SECRET_KEY is the process's, already NEW,OLD below
            store.update_connections({'a': {'db': 'sqlite', 'database': self.db_path, 'password': 'changed'}})

        writer = threading.Thread(target=save)
        with self.keys(f'{NEW},{OLD}'):
            with mock.patch.object(rekey, 'report', slow_report):
                rekey.rekey('tester')
            writer.join(10)
        self.assertEqual(self.under(NEW, 'connections', 'a'), 'changed')
        self.assertEqual(self.under(NEW, 'connections', 'b'), 'pw-b')


class RekeyCommandTests(RekeyTestCase):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(['rekey', *argv])
        return code, out.getvalue() + err.getvalue()

    def test_check_then_rekey(self):
        with self.keys(f'{NEW},{OLD}'):
            code, out = self.run_cli('--check')
            self.assertEqual(code, 0)
            self.assertIn('3 encrypted password(s), 2 key(s) listed: 0 under the first key, 3 under key 2.', out)
            self.assertIn('Run `queryapigate rekey`', out)
            code, out = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn('Re-encrypted 3 password(s) under the first key.', out)
        self.assertIn('remove them from QUERYAPIGATE_SECRET_KEY', out)
        self.assertEqual(store.read_audit_log()[-1]['actor'], 'cli')

    def test_a_bad_key_list_exits_2(self):
        with self.keys(f'{NEW},nope'):
            code, out = self.run_cli()
        self.assertEqual(code, 2)
        self.assertIn('Key 2 of 2', out)

    def test_a_password_no_key_decrypts_exits_1(self):
        with self.keys(NEW):
            code, out = self.run_cli('--check')
        self.assertEqual(code, 1)
        self.assertIn("no listed key decrypts connection 'a'", out)


class RekeyApiTests(RekeyTestCase):
    def setUp(self):
        super().setUp()
        admins.create_admin('ops', 'admin')
        admins.create_admin('root', 'owner')
        self.ops = {'X-API-Key': admins.issue_token('ops')[1]}
        self.root = {'X-API-Key': admins.issue_token('root')[1]}

    def client(self):
        return create_app().test_client()

    def test_owners_rekey_and_admins_may_not(self):
        with self.keys(f'{NEW},{OLD}'):
            client = self.client()
            refused = client.post('/api/v1/settings/rekey', headers=self.ops)
            self.assertEqual((refused.status_code, refused.get_json()['code']), (403, 'role_forbidden'))
            checked = client.post('/api/v1/settings/rekey', headers=self.root, json={'check': True})
            self.assertEqual((checked.status_code, checked.get_json()['under_old_keys']), (200, 3))
            done = client.post('/api/v1/settings/rekey', headers=self.root)
        self.assertEqual((done.status_code, done.get_json()['rekeyed']), (200, 3))
        entry = store.read_audit_log()[-1]
        self.assertEqual((entry['action'], entry['actor'], entry['via']), ('rekey', 'root', 'token'))

    def test_errors(self):
        with self.keys(f'{NEW},{OLD}'):
            bad = self.client().post('/api/v1/settings/rekey', headers=self.root, json={'check': 'yes'})
        self.assertEqual((bad.status_code, bad.get_json()['code']), (400, 'invalid_body'))
        with self.keys(NEW):
            lost = self.client().post('/api/v1/settings/rekey', headers=self.root)
        self.assertEqual((lost.status_code, lost.get_json()['code']), (409, 'password_undecryptable'))
        self.assertEqual(len(lost.get_json()['values']), 3)

    def test_settings_and_alerts_follow_the_rotation(self):
        def state():
            security = [s for s in config.describe_settings() if s['id'] == 'security'][0]
            return [r for r in security['rows'] if r['env'] == 'QUERYAPIGATE_SECRET_KEY'][0]['value']

        def rotation_alerts():
            return [(a['severity'], a['title']) for a in alerts.collect()
                    if a['kind'] == 'encryption_key_rotation_incomplete']

        self.assertEqual((state(), rotation_alerts()), ('enabled', []))
        with self.keys(f'{NEW},{OLD}'):
            self.assertIn('3 password(s) still under an old key', state())
            self.assertEqual(rotation_alerts(), [('warning', '3 passwords are still encrypted with an old key')])
            rekey.rekey('tester')
            self.assertIn('remove the others', state())
            self.assertEqual(rotation_alerts()[0][0], 'info')

    def test_startup_warns_while_passwords_are_under_an_old_key(self):
        with self.keys(f'{NEW},{OLD}'), self.assertLogs('queryapigate', 'WARNING') as logs:
            create_app()
        self.assertTrue(any('3 password(s) are still encrypted with an old key' in m for m in logs.output))


if __name__ == '__main__':
    unittest.main()

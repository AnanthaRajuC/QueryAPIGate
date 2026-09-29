"""Tests for encryption at rest for connection passwords (QUERYAPIGATE_SECRET_KEY) - the "real fix" half of
BACKLOG #24, on top of the startup-warning-only first slice. Distinct from API key hashing (#1) and IP
allowlisting (#17): this is specifically about database connection passwords in db_connections.json."""
import json
import logging
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from cryptography.fernet import Fernet

from queryapigate import config, create_app, db, store


class AppTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        tmp = self.tmp.name
        self.db_path = os.path.join(tmp, 'test.db')
        conn = sqlite3.connect(self.db_path)
        conn.execute('CREATE TABLE t (id INTEGER)')
        conn.execute('INSERT INTO t VALUES (1)')
        conn.commit()
        conn.close()
        self.secret_key = Fernet.generate_key().decode()
        patcher = mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': tmp, 'QUERYAPIGATE_API_KEY': 'admin-key',
                                               'QUERYAPIGATE_SECRET_KEY': self.secret_key})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.write_connections({})
        self.admin_headers = {'X-API-Key': 'admin-key'}

    def write_connections(self, connections):
        """A raw write, deliberately bypassing store.update_connections()'s own encryption/mask-echo logic -
        several tests here exist specifically to prove *startup* migration (encrypt_plaintext_passwords_in_
        place()) does the encrypting, so the value written here must land on disk exactly as given."""
        db.init_schema()
        with db.transaction() as conn:
            conn.execute('DELETE FROM connections')
            timestamp = store.now()
            for name, details in connections.items():
                extra = {k: v for k, v in details.items() if k not in ('db', 'active', 'created_at', 'updated_at')}
                conn.execute(
                    'INSERT INTO connections (name, db, active, created_at, updated_at, details_json) '
                    'VALUES (?, ?, ?, ?, ?, ?)',
                    (name, details.get('db'), int(bool(details.get('active', False))), timestamp, timestamp,
                     json.dumps(extra)))

    def stored(self):
        return store.read_connections()

    def encrypted_token(self, plaintext):
        return 'enc:' + Fernet(self.secret_key.encode()).encrypt(plaintext.encode()).decode()


class MigrationTests(AppTestCase):
    def test_a_literal_password_is_encrypted_at_startup(self):
        self.write_connections({'a': {'db': 'sqlite', 'database': self.db_path, 'password': 'secret',
                                      'active': True}})
        create_app()
        self.assertTrue(self.stored()['a']['password'].startswith('enc:'))

    def test_an_env_var_reference_is_not_touched_by_migration(self):
        self.write_connections({'a': {'db': 'sqlite', 'database': self.db_path, 'password': '${PW}',
                                      'active': True}})
        create_app()
        self.assertEqual(self.stored()['a']['password'], '${PW}')

    def test_an_already_encrypted_password_is_not_re_encrypted(self):
        token = self.encrypted_token('secret')
        self.write_connections({'a': {'db': 'sqlite', 'database': self.db_path, 'password': token,
                                      'active': True}})
        create_app()
        self.assertEqual(self.stored()['a']['password'], token)

    def test_migration_does_not_run_without_a_secret_key(self):
        os.environ.pop('QUERYAPIGATE_SECRET_KEY')
        self.write_connections({'a': {'db': 'sqlite', 'database': self.db_path, 'password': 'secret',
                                      'active': True}})
        create_app()
        self.assertEqual(self.stored()['a']['password'], 'secret')

    def test_an_empty_password_is_left_alone(self):
        self.write_connections({'a': {'db': 'sqlite', 'database': self.db_path, 'password': '',
                                      'active': True}})
        create_app()
        self.assertEqual(self.stored()['a']['password'], '')


class EncryptionRoundTripTests(AppTestCase):
    def test_a_query_works_through_an_encrypted_password(self):
        client = create_app().test_client()
        client.patch('/connections', json={'connections': {'a': {
            'db': 'sqlite', 'database': self.db_path, 'password': 'secret', 'active': True}}},
            headers=self.admin_headers)
        res = client.post('/execute_sql', json={'sql': 'SELECT * FROM t', 'connection_name': 'a'},
                          headers=self.admin_headers)
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        self.assertTrue(self.stored()['a']['password'].startswith('enc:'))

    def test_get_connections_masks_an_encrypted_password(self):
        client = create_app().test_client()
        client.patch('/connections', json={'connections': {'a': {
            'db': 'sqlite', 'database': self.db_path, 'password': 'secret', 'active': True}}},
            headers=self.admin_headers)
        listed = client.get('/connections', headers=self.admin_headers).get_json()['connections']['a']
        self.assertEqual(listed['password'], config.PASSWORD_MASK)

    def test_echoing_the_mask_back_keeps_the_encrypted_value_unchanged(self):
        client = create_app().test_client()
        client.patch('/connections', json={'connections': {'a': {
            'db': 'sqlite', 'database': self.db_path, 'password': 'secret', 'active': True}}},
            headers=self.admin_headers)
        before = self.stored()['a']['password']
        client.patch('/connections', json={'connections': {'a': {
            'db': 'sqlite', 'database': self.db_path, 'password': config.PASSWORD_MASK, 'active': False}}},
            headers=self.admin_headers)
        self.assertEqual(self.stored()['a']['password'], before)

    def test_an_env_var_reference_is_never_encrypted(self):
        client = create_app().test_client()
        with mock.patch.dict(os.environ, {'PW': 'secret'}):
            client.patch('/connections', json={'connections': {'a': {
                'db': 'sqlite', 'database': self.db_path, 'password': '${PW}', 'active': True}}},
                headers=self.admin_headers)
            res = client.post('/execute_sql', json={'sql': 'SELECT * FROM t', 'connection_name': 'a'},
                              headers=self.admin_headers)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.stored()['a']['password'], '${PW}')

    def test_without_a_secret_key_a_literal_password_is_stored_as_given(self):
        os.environ.pop('QUERYAPIGATE_SECRET_KEY')
        client = create_app().test_client()
        client.patch('/connections', json={'connections': {'a': {
            'db': 'sqlite', 'database': self.db_path, 'password': 'secret', 'active': True}}},
            headers=self.admin_headers)
        self.assertEqual(self.stored()['a']['password'], 'secret')


class FailureModeTests(AppTestCase):
    def test_a_missing_key_warns_at_startup_when_encrypted_passwords_exist(self):
        token = self.encrypted_token('secret')
        self.write_connections({'a': {'db': 'sqlite', 'database': self.db_path, 'password': token,
                                      'active': True}})
        os.environ.pop('QUERYAPIGATE_SECRET_KEY')
        with self.assertLogs('queryapigate', level=logging.WARNING) as logs:
            create_app()
        self.assertTrue(any('encrypted password' in line for line in logs.output))

    def test_a_missing_key_fails_clearly_on_use(self):
        token = self.encrypted_token('secret')
        self.write_connections({'a': {'db': 'sqlite', 'database': self.db_path, 'password': token,
                                      'active': True}})
        os.environ.pop('QUERYAPIGATE_SECRET_KEY')
        client = create_app().test_client()
        res = client.post('/execute_sql', json={'sql': 'SELECT * FROM t', 'connection_name': 'a'},
                          headers=self.admin_headers)
        self.assertEqual(res.status_code, 500)
        self.assertIn('cannot be decrypted', res.get_json()['error'])

    def test_a_rotated_key_fails_clearly_on_use(self):
        token = self.encrypted_token('secret')
        self.write_connections({'a': {'db': 'sqlite', 'database': self.db_path, 'password': token,
                                      'active': True}})
        os.environ['QUERYAPIGATE_SECRET_KEY'] = Fernet.generate_key().decode()
        client = create_app().test_client()
        res = client.post('/execute_sql', json={'sql': 'SELECT * FROM t', 'connection_name': 'a'},
                          headers=self.admin_headers)
        self.assertEqual(res.status_code, 500)
        self.assertIn('rotated', res.get_json()['error'])

    def test_a_malformed_secret_key_is_rejected_at_startup(self):
        os.environ['QUERYAPIGATE_SECRET_KEY'] = 'not-a-valid-key'
        self.assertRaises(ValueError, create_app)

    def test_no_warning_when_key_is_missing_and_no_encrypted_passwords_exist(self):
        self.write_connections({'a': {'db': 'sqlite', 'database': self.db_path, 'password': '${PW}',
                                      'active': True}})
        os.environ.pop('QUERYAPIGATE_SECRET_KEY')
        with mock.patch('queryapigate.app.log.warning') as warning:
            create_app()
        for call in warning.call_args_list:
            self.assertNotIn('encrypted password', call.args[0])


class AuditLogTests(AppTestCase):
    def test_the_plaintext_password_never_appears_in_the_audit_log(self):
        client = create_app().test_client()
        client.patch('/connections', json={'connections': {'a': {
            'db': 'sqlite', 'database': self.db_path, 'password': 'super-secret-value', 'active': True}}},
            headers=self.admin_headers)
        audit = client.get('/audit_log', headers=self.admin_headers).get_json()['entries']
        self.assertNotIn('super-secret-value', json.dumps(audit))

    def test_the_ciphertext_never_appears_in_the_audit_log_either(self):
        client = create_app().test_client()
        client.patch('/connections', json={'connections': {'a': {
            'db': 'sqlite', 'database': self.db_path, 'password': 'super-secret-value', 'active': True}}},
            headers=self.admin_headers)
        stored_password = self.stored()['a']['password']
        audit = client.get('/audit_log', headers=self.admin_headers).get_json()['entries']
        self.assertNotIn(stored_password, json.dumps(audit))
        self.assertEqual(audit[0]['changes']['password'], config.PASSWORD_MASK)

    def test_changing_an_encrypted_password_is_reported_only_as_changed(self):
        client = create_app().test_client()
        client.patch('/connections', json={'connections': {'a': {
            'db': 'sqlite', 'database': self.db_path, 'password': 'first-secret', 'active': True}}},
            headers=self.admin_headers)
        client.patch('/connections', json={'connections': {'a': {
            'db': 'sqlite', 'database': self.db_path, 'password': 'second-secret', 'active': True}}},
            headers=self.admin_headers)
        audit = client.get('/audit_log', headers=self.admin_headers).get_json()['entries']
        self.assertEqual(audit[0]['changes'], {'password': 'changed'})


class ModuleLevelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.secret_key = Fernet.generate_key().decode()
        patcher = mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': self.tmp.name,
                                               'QUERYAPIGATE_SECRET_KEY': self.secret_key})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_encrypt_then_decrypt_round_trips(self):
        token = store._encrypt_password('a-real-secret')
        self.assertTrue(token.startswith('enc:'))
        self.assertEqual(store._decrypt_password(token), 'a-real-secret')

    def test_encrypt_is_a_no_op_without_a_key(self):
        os.environ.pop('QUERYAPIGATE_SECRET_KEY')
        self.assertEqual(store._encrypt_password('literal'), 'literal')

    def test_encrypt_passes_through_an_env_reference(self):
        self.assertEqual(store._encrypt_password('${PW}'), '${PW}')

    def test_encrypt_does_not_double_encrypt(self):
        token = store._encrypt_password('a-real-secret')
        self.assertEqual(store._encrypt_password(token), token)

    def test_decrypt_passes_through_a_non_encrypted_value(self):
        self.assertEqual(store._decrypt_password('${PW}'), '${PW}')
        self.assertEqual(store._decrypt_password('literal'), 'literal')

    def test_is_plaintext_password_excludes_encrypted_and_env_ref_values(self):
        token = store._encrypt_password('secret')
        self.assertTrue(store._is_plaintext_password('literal'))
        self.assertFalse(store._is_plaintext_password(token))
        self.assertFalse(store._is_plaintext_password('${PW}'))
        self.assertFalse(store._is_plaintext_password(''))
        self.assertFalse(store._is_plaintext_password(None))


if __name__ == '__main__':
    unittest.main()

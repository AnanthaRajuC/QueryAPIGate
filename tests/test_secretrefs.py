"""Secrets by reference (ADR 0015, BACKLOG #98), slice 1: `${file:...}`, the allow-list that keeps a reference from
reaching what the deployer didn't open to it, the cache that fails closed, and the server's own secrets from files."""
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from queryapigate import config, configcode, db, secretrefs, store
from queryapigate.errors import ApiError
from queryapigate.services import connections


class SecretTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.join(self.tmp.name, 'home')
        self.secrets = os.path.join(self.tmp.name, 'secrets')
        os.makedirs(self.home)
        os.makedirs(self.secrets)
        patcher = mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': self.home,
                                               'QUERYAPIGATE_SECRET_FILE_DIRS': self.secrets})
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ('QUERYAPIGATE_SECRET_KEY', 'QUERYAPIGATE_SECRET_CACHE_SECONDS'):
            os.environ.pop(name, None)
        db.init_schema()
        secretrefs.cache.clear()
        self.addCleanup(secretrefs.cache.clear)

    def secret(self, name, text):
        path = os.path.join(self.secrets, name)
        with open(path, 'w') as f:
            f.write(text)
        return path

    def refused(self, value):
        with self.assertRaises(ApiError) as caught:
            secretrefs.check_value(value)
        self.assertEqual(caught.exception.code, 'invalid_body')
        return caught.exception.message


class ReferenceTests(SecretTestCase):
    def test_what_counts_as_a_reference(self):
        for value in ('${VAR}', '${file:/run/secrets/x}', '${vault:secret/data/a#b}'):
            self.assertTrue(secretrefs.is_reference(value), value)
        for value in ('plain', '${VAR} and more', 'x${VAR}', '${Bad:x}', '${file:}', None):
            self.assertFalse(secretrefs.is_reference(value), value)

    def test_a_file_is_read_and_its_trailing_newline_dropped(self):
        path = self.secret('pw', 's3cret\n')
        self.assertEqual(secretrefs.expand('${file:' + path + '}'), 's3cret')
        self.assertEqual(secretrefs.expand('user=app;password=${file:' + path + '};'), 'user=app;password=s3cret;')

    def test_a_changed_file_is_read_again_at_once(self):
        path = self.secret('pw', 'first')
        self.assertEqual(secretrefs.expand('${file:' + path + '}'), 'first')
        self.secret('pw', 'second, rotated in place')
        os.utime(path, ns=(1, 2))  # a different mtime, as a rotation leaves - not left to the clock's resolution
        self.assertEqual(secretrefs.expand('${file:' + path + '}'), 'second, rotated in place')

    def test_what_isnt_a_secret_is_refused(self):
        big = self.secret('big', 'x' * (secretrefs.MAX_FILE_BYTES + 1))
        with self.assertRaises(ApiError) as caught:
            secretrefs.expand('${file:' + big + '}')
        self.assertIn('larger than 64 KiB', caught.exception.extra['detail'])
        self.assertEqual((caught.exception.status, caught.exception.code), (503, 'secret_unavailable'))


class AllowListTests(SecretTestCase):
    def test_nothing_is_readable_until_the_deployer_says_so(self):
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_SECRET_FILE_DIRS': ''}):
            message = self.refused('${file:' + self.secret('a', 'x') + '}')
        self.assertIn('QUERYAPIGATE_SECRET_FILE_DIRS is not set', message)

    def test_only_inside_the_folders_it_allows(self):
        outside = os.path.join(self.tmp.name, 'elsewhere')
        open(outside, 'w').close()
        self.assertIn("isn't inside a folder", self.refused('${file:' + outside + '}'))
        self.assertIn('must be an absolute path', self.refused('${file:secrets/a}'))
        link = os.path.join(self.secrets, 'link')
        os.symlink(outside, link)  # a link inside, to a file outside: what it points to is what's read
        self.assertIn("isn't inside a folder", self.refused('${file:' + link + '}'))

    def test_the_file_read_is_the_one_checked(self):
        path = self.secret('pw', 'right')
        link = os.path.join(self.secrets, 'link')
        os.symlink(path, link)
        ref = '${file:' + link + '}'
        secretrefs.check_value(ref)
        outside = os.path.join(self.tmp.name, 'outside')
        with open(outside, 'w') as f:
            f.write('wrong')
        os.remove(link)
        os.symlink(outside, link)  # swapped after the check
        with self.assertRaises(ApiError) as caught:
            secretrefs.expand(ref)
        self.assertNotIn('wrong', caught.exception.message)

    def test_never_the_servers_own_home(self):
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_SECRET_FILE_DIRS': self.tmp.name}):
            message = self.refused('${file:' + os.path.join(self.home, 'queryapigate.db') + '}')
        self.assertIn("own home folder, where its store lives", message)

    def test_never_a_file_the_servers_own_settings_or_credentials_name(self):
        # Reviewed for 0.34 (ADR 0021): the server's own secrets are often files beside the connections' -
        # QUERYAPIGATE_SECRET_KEY_FILE=/run/secrets/fernet with QUERYAPIGATE_SECRET_FILE_DIRS=/run/secrets - and a
        # reference to one, with a host of the administrator's, would send them the server's key as a password.
        fernet, token, identity = self.secret('fernet', 'k'), self.secret('vault-token', 't'), self.secret('irsa', 'j')
        syslog_key = self.secret('syslog.key', 'pem')
        link = os.path.join(self.secrets, 'also-fernet')
        os.symlink(fernet, link)
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_SECRET_KEY_FILE': fernet,
                                          'QUERYAPIGATE_VAULT_TOKEN_FILE': token,
                                          'QUERYAPIGATE_AUDIT_SYSLOG_KEY': syslog_key,
                                          'AWS_WEB_IDENTITY_TOKEN_FILE': identity}):
            for path in (fernet, token, syslog_key, identity, link):
                with self.subTest(path=path):
                    self.assertIn("a file this server's own settings or credentials name",
                                  self.refused('${file:' + path + '}'))
            self.assertEqual(secretrefs.expand('${file:' + self.secret('pw', 'fine') + '}'), 'fine')  # others still

    def test_never_the_processes_own_memory_or_service_account(self):
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_SECRET_FILE_DIRS': '/'}):
            for path in ('/proc/self/environ', '/proc/1/environ',
                         '/var/run/secrets/kubernetes.io/serviceaccount/token',
                         '/var/run/secrets/eks.amazonaws.com/serviceaccount/token'):
                with self.subTest(path=path):
                    self.assertIn('never readable as a secret', self.refused('${file:' + path + '}'))

    def test_a_provider_there_is_none_of_and_the_servers_own_settings(self):
        self.assertIn("no secret provider 'gcp-sm'", self.refused('${gcp-sm:projects/p/secrets/s}'))
        self.assertIn("one of this server's own settings", self.refused('${QUERYAPIGATE_API_KEY}'))

    def test_checked_again_when_used_in_case_the_list_shrank(self):
        path = self.secret('pw', 'x')
        connections.create({'name': 'c', 'db': 'sqlite', 'database': '${file:' + path + '}'}, 't')
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_SECRET_FILE_DIRS': ''}), self.assertRaises(ApiError) as caught:
            store.get_connection('c')
        self.assertEqual(caught.exception.code, 'connection_misconfigured')

    def test_saving_a_connection_checks_it(self):
        with self.assertRaises(ApiError) as caught:
            connections.create({'name': 'c', 'db': 'mysql', 'password': '${file:/etc/shadow}'}, 't')
        self.assertIn("isn't inside a folder QUERYAPIGATE_SECRET_FILE_DIRS allows", caught.exception.message)


class EnvironmentAllowListTests(SecretTestCase):
    """${VAR} (ADR 0017): the server's cloud, Vault and Kubernetes credentials are never reachable, and
    QUERYAPIGATE_SECRET_ENV_VARS limits the rest to what the deployer lists."""

    def test_the_servers_own_credentials_are_never_reachable(self):
        for name in ('AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN', 'AZURE_CLIENT_SECRET', 'ARM_CLIENT_SECRET',
                     'GOOGLE_APPLICATION_CREDENTIALS', 'GCP_KEY', 'VAULT_TOKEN', 'KUBERNETES_SERVICE_HOST'):
            with self.subTest(name=name):
                self.assertIn('a cloud, Vault or Kubernetes credential', self.refused('${' + name + '}'))
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_SECRET_ENV_VARS': 'AWS_*', 'AWS_SECRET_ACCESS_KEY': 'k'}):
            self.assertIn('a cloud, Vault or Kubernetes credential', self.refused('${AWS_SECRET_ACCESS_KEY}'))
            with self.assertRaises(ApiError) as caught:
                secretrefs.expand('${AWS_SECRET_ACCESS_KEY}')
            self.assertEqual(caught.exception.code, 'connection_misconfigured')
            self.assertNotIn("'k'", caught.exception.message)

    def test_unset_any_other_variable_is_reachable(self):
        with mock.patch.dict(os.environ, {'WAREHOUSE_PASSWORD': 'pw'}):
            secretrefs.check_value('${WAREHOUSE_PASSWORD}')
            self.assertEqual(secretrefs.expand('${WAREHOUSE_PASSWORD}'), 'pw')

    def test_set_only_what_it_lists(self):
        env = {'QUERYAPIGATE_SECRET_ENV_VARS': 'WAREHOUSE_PASSWORD, QAG_*', 'WAREHOUSE_PASSWORD': 'pw',
               'QAG_LEGACY_PASSWORD': 'old', 'DB_ADMIN_PW': 'root'}
        with mock.patch.dict(os.environ, env):
            secretrefs.check_value('${WAREHOUSE_PASSWORD}')
            self.assertEqual(secretrefs.expand('${QAG_LEGACY_PASSWORD}'), 'old')
            self.assertIn("isn't one of QUERYAPIGATE_SECRET_ENV_VARS", self.refused('${DB_ADMIN_PW}'))
            self.assertIn("isn't one of QUERYAPIGATE_SECRET_ENV_VARS", self.refused('${WAREHOUSE_PASSWORD_2}'))

    def test_checked_again_when_used_in_case_the_list_shrank(self):
        with mock.patch.dict(os.environ, {'DB_ADMIN_PW': 'root'}):
            connections.create({'name': 'c', 'db': 'mysql', 'host': 'h', 'password': '${DB_ADMIN_PW}'}, 't')
            with mock.patch.dict(os.environ, {'QUERYAPIGATE_SECRET_ENV_VARS': 'WAREHOUSE_*'}), \
                    self.assertRaises(ApiError) as caught:
                store.get_connection('c')
        self.assertEqual(caught.exception.code, 'connection_misconfigured')
        self.assertNotIn('root', caught.exception.message)

    def test_a_configuration_file_is_held_to_it_too(self):
        doc = {'format': 'queryapigate-config', 'format_version': 1, 'connections': [
            {'name': 'w', 'db': 'mysql', 'host': 'h', 'password': '${AWS_SECRET_ACCESS_KEY}'}]}
        with self.assertRaises(configcode.ConfigInvalid) as caught:
            configcode.diff(doc)
        self.assertIn('a cloud, Vault or Kubernetes credential', str(caught.exception.problems))

    def test_a_malformed_list_fails_at_startup(self):
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_SECRET_ENV_VARS': 'GOOD, not-a-name'}), \
                self.assertRaises(ValueError) as caught:
            config.check_settings()
        self.assertIn("'not-a-name' must be a variable's name", str(caught.exception))

    def test_startup_warns_while_references_are_in_use_and_the_list_is_unset(self):
        import logging

        from queryapigate import app
        with mock.patch.dict(os.environ, {'WAREHOUSE_PASSWORD': 'pw'}):
            connections.create({'name': 'c', 'db': 'mysql', 'host': 'h', 'password': '${WAREHOUSE_PASSWORD}'}, 't')
        log = logging.getLogger('queryapigate')
        with self.assertLogs(log, level=logging.WARNING) as logs:
            log.warning('marker')
            app.warn_env_references()
        self.assertIn('reference environment variables (WAREHOUSE_PASSWORD)', ' '.join(logs.output))
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_SECRET_ENV_VARS': 'WAREHOUSE_*'}), \
                self.assertLogs(log, level=logging.WARNING) as logs:
            log.warning('marker')
            app.warn_env_references()
        self.assertEqual(len(logs.output), 1)


class FailingClosedTests(SecretTestCase):
    def setUp(self):
        super().setUp()
        self.path = self.secret('pw', 'held')
        self.ref = '${file:' + self.path + '}'
        self.clock = [1000.0]
        patcher = mock.patch('queryapigate.secretrefs.time.monotonic', lambda: self.clock[0])
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ['QUERYAPIGATE_SECRET_CACHE_SECONDS'] = '100'
        self.assertEqual(secretrefs.expand(self.ref), 'held')

    def failing(self):
        def fetch(what):
            raise secretrefs._FetchError('the provider is down')
        return mock.patch.object(secretrefs.PROVIDERS['file'], 'fetch', fetch)

    def test_within_its_time_a_failing_provider_is_forgiven(self):
        self.clock[0] += 90  # past the refresh point (80 s), within its time (100 s)
        with self.failing(), self.assertLogs('queryapigate', 'WARNING') as logs:
            self.assertEqual(secretrefs.expand(self.ref), 'held')
        self.assertIn('using the value it gave before, good for 10s more', logs.output[0])
        self.assertIsNotNone(secretrefs.cache.status['file']['failing_since'])

    def test_past_it_never(self):
        self.clock[0] += 101
        with self.failing(), self.assertRaises(ApiError) as caught:
            secretrefs.expand(self.ref)
        self.assertIn('could not be resolved: the provider is down', caught.exception.extra['detail'])
        self.assertEqual((caught.exception.status, caught.exception.code), (503, 'secret_unavailable'))
        self.assertNotIn('held', caught.exception.message)

    def test_a_failing_provider_isnt_asked_again_for_a_while(self):
        calls = []

        def fetch(what):
            calls.append(what)
            raise secretrefs._FetchError('the provider is down')

        self.clock[0] += 90
        with mock.patch.object(secretrefs.PROVIDERS['file'], 'fetch', fetch), \
                self.assertLogs('queryapigate', 'WARNING'):
            for _ in range(5):  # within its time: what's held, without waiting on the provider each time
                self.assertEqual(secretrefs.expand(self.ref), 'held')
            self.assertEqual(len(calls), 1)
            self.clock[0] += 11  # past its time now, and past the retry interval: asked once more, then failing fast
            for _ in range(3):
                with self.assertRaises(ApiError):
                    secretrefs.expand(self.ref)
            self.assertEqual(len(calls), 2)

    def test_the_retry_interval_counts_from_the_failure_not_the_attempt(self):
        def slow_fail(what):
            self.clock[0] += 10  # a fetch that times out after 10 s
            raise secretrefs._FetchError('timed out')

        self.clock[0] += 90
        with mock.patch.object(secretrefs.PROVIDERS['file'], 'fetch', slow_fail), self.assertLogs('queryapigate'):
            self.assertEqual(secretrefs.expand(self.ref), 'held')  # 1090 -> 1100: held, good until 1100
            self.clock[0] += 1
            with self.assertRaises(ApiError):  # past its time; within 10 s of the failure: refused at once
                secretrefs.expand(self.ref)
        self.assertEqual(self.clock[0], 1101)  # nothing waited on the provider again

    def test_while_one_request_fetches_the_others_get_whats_held(self):
        self.clock[0] += 90
        key = ('file', self.path)
        secretrefs.cache._fetching.add(key)  # another request is waiting on the provider
        self.addCleanup(secretrefs.cache._fetching.discard, key)
        with self.failing():
            self.assertEqual(secretrefs.expand(self.ref), 'held')
        self.assertIsNone(secretrefs.cache.status['file']['failing_since'])  # it didn't ask the failing provider

    def test_before_the_refresh_point_the_provider_isnt_asked(self):
        self.clock[0] += 50
        with self.failing():
            self.assertEqual(secretrefs.expand(self.ref), 'held')
        self.assertNotIn('failing_since', {k for k, v in secretrefs.cache.status['file'].items() if v})


class WhoSeesWhyTests(SecretTestCase):
    def test_a_scoped_key_isnt_told_where_the_secrets_live(self):
        # Reviewed for 0.34 (ADR 0021): the reason names the reference, a Vault address or path - for the admin key and
        # administrators, kept from a partner calling a saved query, as a database's own error text is.
        from queryapigate import create_app
        from tests.helpers import save_query
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_API_KEY': 'admin'}):
            client = create_app().test_client()
            admin = {'X-API-Key': 'admin'}
            missing = os.path.join(self.secrets, 'warehouse-password')
            self.assertEqual(client.post('/api/v1/connections', headers=admin, json={
                'name': 'wh', 'db': 'postgres', 'host': 'db.internal', 'database': 'd', 'user': 'u',
                'password': '${file:' + missing + '}'}).status_code, 201)
            self.assertEqual(save_query(client, headers=admin, body={
                'author': 'a', 'description': 'd', 'filename': 'q', 'connection_name': 'wh',
                'sql_query': 'SELECT 1'}).status_code, 201)
            partner = client.post('/api/v1/api-keys', headers=admin,
                                  json={'name': 'partner', 'connections': ['wh'], 'queries': ['q']}).get_json()
            scoped = client.get('/q/q', headers={'X-API-Key': partner['secret']})
            full = client.get('/q/q', headers=admin)
        self.assertEqual(scoped.status_code, 503)
        self.assertEqual(scoped.get_json()['error'], secretrefs.UNAVAILABLE)
        self.assertNotIn(missing, scoped.get_data(as_text=True))
        self.assertIn(missing, full.get_json()['detail'])


class StoreTests(SecretTestCase):
    def test_a_connection_opens_with_what_the_file_holds(self):
        data = os.path.join(self.tmp.name, 'data.db')
        sqlite3.connect(data).close()
        connections.create({'name': 'c', 'db': 'sqlite', 'database': '${file:' + self.secret('db', data + '\n') + '}'},
                           't')
        self.assertEqual(store.get_connection('c')['database'], data)
        self.assertEqual(store.read_connections()['c']['database'], '${file:' + os.path.join(self.secrets, 'db') + '}')

    def test_a_reference_is_never_encrypted_and_always_shown_as_written(self):
        ref = '${file:' + self.secret('pw', 'x') + '}'
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_SECRET_KEY': 'Zm9vYmFyYmF6cXV4Zm9vYmFyYmF6cXV4Zm9vYmFyYmE='}):
            connections.create({'name': 'c', 'db': 'mysql', 'host': 'h', 'password': ref}, 't')
            self.assertEqual(store.read_connections()['c']['password'], ref)
            self.assertEqual(store.mask_passwords(store.read_connections())['c']['password'], ref)
            self.assertEqual(store.get_connection('c')['password'], 'x')

    def test_a_configuration_file_takes_one(self):
        ref = '${file:' + self.secret('pw', 'x') + '}'
        doc = {'format': 'queryapigate-config', 'format_version': 1,
               'connections': [{'name': 'c', 'db': 'mysql', 'password': ref}]}
        self.assertEqual(configcode.diff(doc)['changes'][0]['fields']['password'], {'from': None, 'to': ref})
        doc['connections'][0]['password'] = '${file:/etc/shadow}'
        with self.assertRaises(configcode.ConfigInvalid) as caught:
            configcode.diff(doc)
        self.assertIn("isn't inside a folder", caught.exception.problems[0])


class ServerSecretFileTests(SecretTestCase):
    def setUp(self):
        super().setUp()
        config._from_files.clear()
        self.addCleanup(config._from_files.clear)

    def test_a_secret_setting_from_its_file(self):
        path = self.secret('api-key', 'from-a-file\n')
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_API_KEY_FILE': path}):
            os.environ.pop('QUERYAPIGATE_API_KEY', None)
            config.check_settings()
            self.assertEqual(os.environ['QUERYAPIGATE_API_KEY'], 'from-a-file')
            config.check_settings()  # again, as every command does: the value it put there isn't "both set"
            row = next(r for section in config.describe_settings() for r in section['rows']
                       if r['env'] == 'QUERYAPIGATE_API_KEY')
            self.assertEqual((row['value'], row['env_value']), ('configured (from QUERYAPIGATE_API_KEY_FILE)', None))
            os.environ.pop('QUERYAPIGATE_API_KEY')

    def test_both_or_an_empty_file_is_refused(self):
        path = self.secret('api-key', 'x')
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_API_KEY_FILE': path, 'QUERYAPIGATE_API_KEY': 'y'}):
            with self.assertRaises(ValueError) as caught:
                config.check_settings()
        self.assertIn('QUERYAPIGATE_API_KEY and QUERYAPIGATE_API_KEY_FILE are both set', str(caught.exception))
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_MASK_KEY_FILE': self.secret('empty', '\n')}):
            with self.assertRaises(ValueError) as caught:
                config.check_settings()
        self.assertIn('is empty', str(caught.exception))

    def test_the_store_url_from_a_file_before_the_store_opens(self):
        missing = os.path.join(self.secrets, 'nope')
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_DATABASE_URL_FILE': missing}):
            with self.assertRaises(ValueError) as caught:
                config.check_database_url()
        self.assertIn('QUERYAPIGATE_DATABASE_URL_FILE: ' + missing + ' could not be read', str(caught.exception))


if __name__ == '__main__':
    unittest.main()

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
        self.assertIn('larger than 64 KiB', caught.exception.message)
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

    def test_never_the_servers_own_home(self):
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_SECRET_FILE_DIRS': self.tmp.name}):
            message = self.refused('${file:' + os.path.join(self.home, 'queryapigate.db') + '}')
        self.assertIn("own home folder, where its store lives", message)

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
        self.assertIn('could not be resolved: the provider is down', caught.exception.message)
        self.assertEqual((caught.exception.status, caught.exception.code), (503, 'secret_unavailable'))
        self.assertNotIn('held', caught.exception.message)

    def test_before_the_refresh_point_the_provider_isnt_asked(self):
        self.clock[0] += 50
        with self.failing():
            self.assertEqual(secretrefs.expand(self.ref), 'held')
        self.assertNotIn('failing_since', {k for k, v in secretrefs.cache.status['file'].items() if v})


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

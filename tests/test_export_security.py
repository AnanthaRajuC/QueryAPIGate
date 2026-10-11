"""The 0.17 security review's findings (exports and masking, ADR 0004/0005): a local-folder destination can only
write where the operator allowed and never the server's home; a connection or destination can't reference the
server's own QUERYAPIGATE_* settings; and a connection can't open the server's own store."""
import os
import tempfile
import unittest
from unittest import mock

from queryapigate import admins, create_app, store
from tests.helpers import export_dir
from tests.test_api_v1 import V1TestCase

LIST = '/api/v1/destinations'


class LocalFolderTests(V1TestCase):
    def test_a_local_destination_only_inside_an_allowed_folder(self):
        # no QUERYAPIGATE_EXPORT_DIRS: local folders are off entirely
        res = self.call('post', LIST, LIST, 400, json={'name': 'd', 'url': '/tmp/anywhere/'})
        self.assertIn('QUERYAPIGATE_EXPORT_DIRS', res.get_json()['error'])
        allowed = export_dir(self)
        self.call('post', LIST, LIST, 201, json={'name': 'ok', 'url': allowed + '/feed/'})
        for url in ('/tmp/elsewhere/', os.path.dirname(allowed) + '/', allowed + '/../escape/'):
            with self.subTest(url=url):
                self.assertEqual(self.call('post', LIST, LIST, 400, json={'name': 'x', 'url': url}).get_json()['code'],
                                 'invalid_body')

    def test_the_home_folder_is_never_a_destination(self):
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_EXPORT_DIRS': self.tmp.name}):  # home is inside it
            res = self.call('post', LIST, LIST, 400, json={'name': 'home', 'url': self.tmp.name + '/'})
        self.assertIn('home folder', res.get_json()['error'])

    def test_a_saved_destination_is_rechecked_at_write_time(self):
        from queryapigate import destinations
        allowed = export_dir(self)
        self.call('post', LIST, LIST, 201, json={'name': 'ok', 'url': allowed + '/feed/'})
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_EXPORT_DIRS': '/somewhere/else'}):
            with self.assertRaises(destinations.ApiError) as caught:
                destinations.open_writer(destinations.get('ok'))
        self.assertEqual(caught.exception.code, 'invalid_body')


class WithoutDuckDbTests(V1TestCase):
    """DuckDB is optional: without it, a destination test or an export says what's missing - not a bare 500."""

    def test_a_clear_error_naming_the_package(self):
        import sys
        allowed = export_dir(self)
        self.call('post', LIST, LIST, 201, json={'name': 'ok', 'url': allowed + '/feed/'})
        with mock.patch.dict(sys.modules, {'duckdb': None}):  # import duckdb now raises ImportError
            for path in (f'{LIST}/ok/test', f'{LIST}/test'):
                with self.subTest(path=path):
                    res = self.client.post(path, headers={'X-API-Key': 'admin-key'}, json={'url': allowed + '/x/'})
                    body = res.get_json()
                    self.assertEqual((res.status_code, body['code']), (500, 'exports_unavailable'))
                    self.assertIn('queryapigate[duckdb]', body['error'])


class ServerSettingTests(V1TestCase):
    def test_a_destination_cant_reference_a_server_setting(self):
        allowed = export_dir(self)
        for secret in ('${QUERYAPIGATE_SECRET_KEY}', '${QUERYAPIGATE_API_KEY}'):
            with self.subTest(secret=secret):
                res = self.call('post', LIST, LIST, 400, json={'name': 'x', 'url': 's3://b/p/', 'user': 'k',
                                                               'password': secret})
                self.assertEqual(res.get_json()['code'], 'invalid_body')
        self.call('post', LIST, LIST, 201, json={'name': 'ok', 'url': allowed + '/'})  # a normal one still fine

    def test_a_connection_cant_reference_a_server_setting(self):
        res = self.client.post('/api/v1/connections', headers={'X-API-Key': 'admin-key'},
                               json={'name': 'c', 'db': 'postgres', 'host': 'h', 'password': '${QUERYAPIGATE_API_KEY}'})
        self.assertEqual((res.status_code, res.get_json()['code']), (400, 'invalid_body'))


class StoreAccessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': self.tmp.name, 'QUERYAPIGATE_API_KEY': 'admin-key'})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = create_app().test_client()
        admins.create_admin('alice', 'owner')  # give the store a hash to steal

    def store_file(self):
        return os.path.join(self.tmp.name, 'queryapigate.db')

    def test_a_connection_cant_open_the_store(self):
        for db_value in (self.store_file(), 'file:' + self.store_file(), self.store_file() + '?mode=ro'):
            with self.subTest(db_value=db_value):
                res = self.client.post('/api/v1/connections', headers={'X-API-Key': 'admin-key'},
                                       json={'name': 'peek', 'db': 'sqlite', 'database': db_value, 'active': True})
                self.assertEqual((res.status_code, res.get_json()['code']), (403, 'store_not_allowed'))

    def test_a_duckdb_connection_cant_read_the_store_through_allowed_paths(self):
        res = self.client.post('/api/v1/connections', headers={'X-API-Key': 'admin-key'},
                               json={'name': 'lake', 'db': 'duckdb', 'database': ':memory:', 'active': True,
                                     'allowed_paths': [self.tmp.name + '/']})
        self.assertEqual((res.status_code, res.get_json()['code']), (403, 'store_not_allowed'))

    def test_a_connection_saved_before_the_check_is_refused_when_used(self):
        from queryapigate import db
        with db.transaction() as conn:
            conn.execute('INSERT INTO connections (name, db, active, created_at, updated_at, details_json) '
                         'VALUES (?, ?, 1, ?, ?, ?)',
                         ('sneaky', 'sqlite', store.now(), store.now(),
                          '{"database": "' + self.store_file() + '"}'))
        res = self.client.post('/execute_sql', headers={'X-API-Key': 'admin-key'},
                               json={'sql': 'SELECT 1', 'connection_name': 'sneaky'})
        self.assertEqual((res.status_code, res.get_json()['code']), (403, 'store_not_allowed'))


if __name__ == '__main__':
    unittest.main()

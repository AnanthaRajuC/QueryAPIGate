"""Tests for GET /settings - the read-only view of the server's configuration behind the admin UI's Settings
screen. It must reflect the real environment, and it must never hand a secret to the browser."""
import json
import os
import tempfile
import unittest
from unittest import mock

from queryapigate import create_app


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        with open(os.path.join(self.tmp.name, 'db_connections.json'), 'w') as f:
            json.dump({'connections': {}}, f)
        clean = {name: value for name, value in os.environ.items() if not name.startswith('QUERYAPIGATE_')}
        patcher = mock.patch.dict(os.environ, {**clean, 'QUERYAPIGATE_HOME': self.tmp.name,
                                               'QUERYAPIGATE_API_KEY': 'admin-key'}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = create_app().test_client()
        self.admin = {'X-API-Key': 'admin-key'}

    def rows(self):
        res = self.client.get('/settings', headers=self.admin)
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        return {row['env']: row for section in res.get_json()['sections'] for row in section['rows']}

    def test_requires_the_admin_key(self):
        self.assertEqual(self.client.get('/settings').status_code, 401)
        scoped = self.client.post('/api_keys', json={'name': 'scoped', 'connections': []}, headers=self.admin)
        key = scoped.get_json()['key']
        self.assertEqual(self.client.get('/settings', headers={'X-API-Key': key}).status_code, 403)

    def test_lists_every_setting_with_where_its_value_comes_from(self):
        rows = self.rows()
        self.assertEqual(len(rows), 19)
        self.assertEqual(rows['QUERYAPIGATE_HOME']['source'], 'env')
        self.assertEqual(rows['QUERYAPIGATE_HOME']['value'], os.path.realpath(self.tmp.name))
        self.assertEqual(rows['QUERYAPIGATE_QUERY_TIMEOUT']['source'], 'default')
        self.assertEqual(rows['QUERYAPIGATE_QUERY_TIMEOUT']['value'], '30 s')
        self.assertEqual(rows['QUERYAPIGATE_STREAM_MAX_ROWS']['value'], 'unbounded')

    def test_reflects_the_environment(self):
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_RATE_LIMIT': '60/minute', 'QUERYAPIGATE_POOL_SIZE': '7',
                                          'QUERYAPIGATE_CORS_ORIGINS': 'https://a.example.com',
                                          'QUERYAPIGATE_ALLOW_WRITES': 'yes'}):
            rows = self.rows()
        self.assertEqual(rows['QUERYAPIGATE_RATE_LIMIT']['value'], '60/minute')
        self.assertEqual(rows['QUERYAPIGATE_POOL_SIZE']['value'], '7')
        self.assertEqual(rows['QUERYAPIGATE_CORS_ORIGINS']['value'], 'https://a.example.com')
        self.assertEqual(rows['QUERYAPIGATE_ALLOW_WRITES']['value'], 'on')
        self.assertEqual(rows['QUERYAPIGATE_ALLOW_WRITES']['env_value'], 'yes')

    def test_never_returns_a_secret(self):
        from cryptography.fernet import Fernet
        secret = Fernet.generate_key().decode()
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_SECRET_KEY': secret}):
            res = self.client.get('/settings', headers=self.admin)
            rows = {row['env']: row for section in res.get_json()['sections'] for row in section['rows']}
        body = res.get_data(as_text=True)
        self.assertNotIn(secret, body)
        self.assertNotIn('admin-key', body)
        self.assertEqual(rows['QUERYAPIGATE_API_KEY']['value'], 'configured')
        self.assertEqual(rows['QUERYAPIGATE_SECRET_KEY']['value'], 'enabled')
        self.assertIsNone(rows['QUERYAPIGATE_API_KEY']['env_value'])
        self.assertIsNone(rows['QUERYAPIGATE_SECRET_KEY']['env_value'])


if __name__ == '__main__':
    unittest.main()

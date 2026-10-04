"""Tests for GET /settings - the read-only view of the server's configuration behind the admin UI's Settings
screen. It must reflect the real environment, and it must never hand a secret to the browser."""
import os
import tempfile
import unittest
from unittest import mock

from queryapigate import config, create_app
from tests import TEST_DATABASE_URL
from tests.helpers import save_query


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        clean = {name: value for name, value in os.environ.items() if not name.startswith('QUERYAPIGATE_')}
        patcher = mock.patch.dict(os.environ, {**clean, 'QUERYAPIGATE_HOME': self.tmp.name,
                                               'QUERYAPIGATE_API_KEY': 'admin-key'}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = create_app().test_client()
        self.admin = {'X-API-Key': 'admin-key'}

    def rows(self):
        res = self.client.get('/api/v1/settings', headers=self.admin)
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        return {row['env']: row for section in res.get_json()['items'] for row in section['rows']}

    def test_requires_the_admin_key(self):
        self.assertEqual(self.client.get('/api/v1/settings').status_code, 401)
        scoped = self.client.post('/api/v1/api-keys', json={'name': 'scoped', 'connections': []}, headers=self.admin)
        key = scoped.get_json()['secret']
        self.assertEqual(self.client.get('/api/v1/settings', headers={'X-API-Key': key}).status_code, 403)

    def test_lists_every_setting_with_where_its_value_comes_from(self):
        rows = self.rows()
        self.assertEqual(len(rows), 42)
        if not TEST_DATABASE_URL:  # a suite run against Postgres (tests/__init__.py) reports that backend here
            self.assertEqual(rows['QUERYAPIGATE_DATABASE_URL']['value'], 'SQLite (queryapigate.db)')
            self.assertEqual(rows['QUERYAPIGATE_DATABASE_URL']['source'], 'default')
        self.assertEqual(rows['']['label'], 'Live updates')  # GET /events (BACKLOG #43) has no env var of its own
        self.assertEqual(rows['QUERYAPIGATE_HOME']['source'], 'env')
        self.assertEqual(rows['QUERYAPIGATE_HOME']['value'], os.path.realpath(self.tmp.name))
        self.assertEqual(rows['QUERYAPIGATE_HISTORY_LIMIT']['value'], '50 runs')
        self.assertEqual(rows['QUERYAPIGATE_HISTORY_RETENTION_DAYS']['value'], 'per-version limit')
        self.assertEqual(rows['QUERYAPIGATE_HISTORY_SAMPLE_RATE']['value'], '1')
        self.assertEqual(rows['QUERYAPIGATE_HISTORY_FLUSH_INTERVAL']['value'], '1 s')
        self.assertEqual(rows['QUERYAPIGATE_QUERY_TIMEOUT']['source'], 'default')
        self.assertEqual(rows['QUERYAPIGATE_QUERY_TIMEOUT']['value'], '30 s')
        self.assertEqual(rows['QUERYAPIGATE_STREAM_MAX_ROWS']['value'], 'unbounded')
        self.assertEqual(rows['QUERYAPIGATE_MCP_PORT']['source'], 'default')
        self.assertEqual(rows['QUERYAPIGATE_MCP_PORT']['value'], '5001')
        self.assertEqual(rows['QUERYAPIGATE_MCP_MAX_ROWS']['value'], '200 rows')

    def test_mcp_section_reflects_the_environment(self):
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_MCP_PORT': '6100', 'QUERYAPIGATE_MCP_MAX_ROWS': '50'}):
            rows = self.rows()
        self.assertEqual(rows['QUERYAPIGATE_MCP_PORT']['source'], 'env')
        self.assertEqual(rows['QUERYAPIGATE_MCP_PORT']['value'], '6100')
        self.assertEqual(rows['QUERYAPIGATE_MCP_MAX_ROWS']['value'], '50 rows')

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
            res = self.client.get('/api/v1/settings', headers=self.admin)
            rows = {row['env']: row for section in res.get_json()['items'] for row in section['rows']}
        body = res.get_data(as_text=True)
        self.assertNotIn(secret, body)
        self.assertNotIn('admin-key', body)
        self.assertEqual(rows['QUERYAPIGATE_API_KEY']['value'], 'configured')
        self.assertEqual(rows['QUERYAPIGATE_SECRET_KEY']['value'], 'enabled')
        self.assertIsNone(rows['QUERYAPIGATE_API_KEY']['env_value'])
        self.assertIsNone(rows['QUERYAPIGATE_SECRET_KEY']['env_value'])

    def test_metadata_database_url_password_is_masked(self):
        url = 'postgresql://qag:s3cret-pw@db.internal:5432/meta'
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_DATABASE_URL': url}), \
                mock.patch.object(config, 'database_url', return_value=url):
            res = self.client.get('/api/v1/settings', headers=self.admin)
        rows = {row['env']: row for section in res.get_json()['items'] for row in section['rows']}
        self.assertNotIn('s3cret-pw', res.get_data(as_text=True))
        self.assertEqual(rows['QUERYAPIGATE_DATABASE_URL']['value'],
                         'PostgreSQL (postgresql://qag:********@db.internal:5432/meta)')
        self.assertIsNone(rows['QUERYAPIGATE_DATABASE_URL']['env_value'])  # never offered for "copy as .env"


class McpStatusEndpointTests(unittest.TestCase):
    """GET /settings/mcp_status - an on-demand TCP reachability probe against QUERYAPIGATE_MCP_PORT, never
    run automatically (BACKLOG #54's own reasoning for skipping this originally)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': self.tmp.name, 'QUERYAPIGATE_API_KEY': 'admin-key'})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = create_app().test_client()
        self.admin = {'X-API-Key': 'admin-key'}

    def test_requires_the_admin_key(self):
        self.assertEqual(self.client.get('/api/v1/mcp/status').status_code, 401)
        scoped = self.client.post('/api/v1/api-keys', json={'name': 'scoped', 'connections': []}, headers=self.admin)
        key = scoped.get_json()['secret']
        self.assertEqual(self.client.get('/api/v1/mcp/status', headers={'X-API-Key': key}).status_code, 403)

    def test_reports_unreachable_when_nothing_listens_on_the_port(self):
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_MCP_PORT': '18321'}):
            res = self.client.get('/api/v1/mcp/status', headers=self.admin)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json(), {'reachable': False, 'port': 18321})

    def test_reports_reachable_when_something_listens_on_the_port(self):
        import socket
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(('127.0.0.1', 0))
        server.listen(1)
        port = server.getsockname()[1]
        self.addCleanup(server.close)
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_MCP_PORT': str(port)}):
            res = self.client.get('/api/v1/mcp/status', headers=self.admin)
        self.assertEqual(res.get_json(), {'reachable': True, 'port': port})


class McpToolsEndpointTests(unittest.TestCase):
    """GET /settings/mcp_tools - what tools/list would return for an unrestricted MCP caller, computed
    in-process via mcp_server.list_tools_for() rather than a live probe of the separate MCP process."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': self.tmp.name, 'QUERYAPIGATE_API_KEY': 'admin-key'})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = create_app().test_client()
        self.admin = {'X-API-Key': 'admin-key'}

    def test_requires_the_admin_key(self):
        self.assertEqual(self.client.get('/api/v1/mcp/tools').status_code, 401)
        scoped = self.client.post('/api/v1/api-keys', json={'name': 'scoped', 'connections': []}, headers=self.admin)
        key = scoped.get_json()['secret']
        self.assertEqual(self.client.get('/api/v1/mcp/tools', headers={'X-API-Key': key}).status_code, 403)

    def test_always_lists_the_two_ad_hoc_tools(self):
        res = self.client.get('/api/v1/mcp/tools', headers=self.admin)
        self.assertEqual(res.status_code, 200)
        tools = {t['name']: t for t in res.get_json()['items']}
        self.assertEqual(tools['list_tables']['kind'], 'ad-hoc')
        self.assertEqual(tools['execute_sql']['kind'], 'ad-hoc')

    def test_lists_a_saved_query_with_its_params_and_kind(self):
        save_query(self.client, {
            'author': 'a', 'description': 'By id', 'sql_query': 'SELECT * FROM t WHERE id = :id',
            'filename': 'q1', 'connection_name': 'nope'}, headers=self.admin)
        res = self.client.get('/api/v1/mcp/tools', headers=self.admin)
        tools = {t['name']: t for t in res.get_json()['items']}
        self.assertEqual(tools['q1']['kind'], 'saved query')
        self.assertEqual(tools['q1']['description'], 'By id')
        self.assertEqual(tools['q1']['params'], ['id'])
        self.assertTrue(tools['q1']['read_only'])

    def test_works_whether_or_not_the_mcp_process_is_actually_running(self):
        # No real MCP process is ever started in this test suite - the endpoint must not depend on one.
        res = self.client.get('/api/v1/mcp/tools', headers=self.admin)
        self.assertEqual(res.status_code, 200)



class KubernetesServiceLinkTests(unittest.TestCase):
    """Kubernetes injects <SERVICE>_PORT=tcp://ip:port into every pod for each Service, so a Service named
    `queryapigate-events` sets QUERYAPIGATE_EVENTS_PORT - a value of ours, by accident. It is ignored, with a warning
    naming the cause, rather than stopping the server."""

    def setUp(self):
        patcher = mock.patch.object(config, '_ignored_links', set())
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_an_injected_service_link_is_ignored_with_a_warning(self):
        for name, default in (('QUERYAPIGATE_PORT', 5000), ('QUERYAPIGATE_EVENTS_PORT', 5002),
                              ('QUERYAPIGATE_MCP_PORT', 5001)):
            with self.subTest(name=name), mock.patch.dict(os.environ, {name: 'tcp://10.96.0.7:80'}), \
                    self.assertLogs('queryapigate', level='WARNING') as logs:
                self.assertEqual(config.port_setting(name, default), default)
                config.check_settings()  # starts, rather than refusing the setting
            self.assertIn('Kubernetes sets for a Service named', logs.output[0])

    def test_a_real_port_is_used_and_a_bad_one_refused(self):
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_EVENTS_PORT': '7002'}):
            self.assertEqual(config.events_port(), 7002)
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_EVENTS_PORT': 'seven'}):
            with self.assertRaisesRegex(ValueError, 'QUERYAPIGATE_EVENTS_PORT must be a positive integer'):
                config.check_settings()

if __name__ == '__main__':
    unittest.main()

"""One error shape for every front door (BACKLOG #69): `{error, code, request_id}` from /api/v1 and the runtime
routes alike, the same `code` in MCP tool errors, and every code the source can raise documented in API.md - so a
code can't ship that clients have no way to know about, and the docs can't list one that no longer exists."""
import ast
import glob
import os
import re
import sqlite3
import tempfile
import unittest
from unittest import mock

from queryapigate import create_app, errors, history, mcp_server
from tests.helpers import save_query, write_connections

ADMIN = {'X-API-Key': 'admin-key'}
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def raised_codes():
    """Every code literal given to ApiError(code=...), mcp_server._error(..., code) or an events-server body."""
    found = set()
    for path in glob.glob(os.path.join(ROOT, 'queryapigate', '**', '*.py'), recursive=True):
        with open(path) as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            values = []
            if isinstance(node, ast.Call) and getattr(node.func, 'id', None) in ('ApiError', '_error'):
                values = [k.value for k in node.keywords if k.arg == 'code']
                if node.func.id == '_error' and len(node.args) > 2:
                    values.append(node.args[2])
            elif isinstance(node, ast.Dict):
                values = [v for k, v in zip(node.keys, node.values, strict=False)
                          if isinstance(k, ast.Constant) and k.value == 'code']
            for value in values:
                for leaf in (value.body, value.orelse) if isinstance(value, ast.IfExp) else (value,):
                    if isinstance(leaf, ast.Constant) and isinstance(leaf.value, str):
                        found.add(leaf.value)
    return found | set(errors.DEFAULT_CODES.values())


def documented_codes():
    with open(os.path.join(ROOT, 'documentation', 'API.md')) as f:
        section = f.read().split('\n## Errors\n', 1)[1]
    first_cells = re.findall(r'^\| (`[^|]+) \|', section.split('| Code |', 1)[1], re.M)
    return set(re.findall(r'`([a-z_]+)`', ' '.join(first_cells))) | set(re.findall(r'`([a-z_]+)` \(\d{3}\)', section))


class CatalogueTests(unittest.TestCase):
    def test_every_raised_code_is_documented(self):
        self.assertEqual(raised_codes() - documented_codes(), set())

    def test_every_documented_code_is_raised(self):
        self.assertEqual(documented_codes() - raised_codes(), set())

    def test_codes_are_snake_case(self):
        for code in raised_codes():
            self.assertRegex(code, r'^[a-z]+(_[a-z]+)*$')


class ShapeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(history.flush)
        data = os.path.join(self.tmp.name, 'data.db')
        conn = sqlite3.connect(data)
        conn.execute('CREATE TABLE film (id INTEGER, title TEXT)')
        conn.execute('CREATE TABLE secret (id INTEGER)')
        conn.commit()
        conn.close()
        patcher = mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': self.tmp.name, 'QUERYAPIGATE_API_KEY': 'admin-key'})
        patcher.start()
        self.addCleanup(patcher.stop)
        write_connections({'lite': {'db': 'sqlite', 'database': data, 'active': True}})
        self.app = create_app()
        self.client = self.app.test_client()
        save_query(self.client, headers=ADMIN, body={
            'filename': 'by_id', 'description': 'd', 'connection_name': 'lite',
            'sql_query': 'SELECT title FROM film WHERE id = :id', 'query_parameters': {'id': {'type': 'int',
                                                                                              'required': True}}})
        res = self.client.post('/api/v1/api-keys', headers=ADMIN, json={
            'name': 'narrow', 'connections': ['lite'], 'allowed_tables': ['film']})
        self.narrow = {'X-API-Key': res.get_json()['secret']}

    def check(self, res, status, code):
        body = res.get_json()
        self.assertEqual((res.status_code, body.get('code')), (status, code), body)
        self.assertIsInstance(body['error'], str)
        self.assertEqual(body['request_id'], res.headers['X-Request-Id'])
        return body

    def test_runtime_routes(self):
        sql = lambda sql, headers=ADMIN, **kw: self.client.post(  # noqa: E731
            '/execute_sql', json={'sql': sql, 'connection_name': 'lite', **kw}, headers=headers)
        self.check(sql(''), 400, 'sql_required')
        self.check(sql('SELECT 1; SELECT 2'), 400, 'multiple_statements')
        self.check(sql('DELETE FROM film'), 403, 'read_only')
        self.check(sql('SELECT * FROM secret', headers=self.narrow), 403, 'table_not_allowed')
        self.check(sql('SELECT nope FROM film'), 500, 'query_failed')
        self.check(sql('SELECT 1', connection_name='gone'), 404, 'connection_not_found')
        self.check(self.client.get('/q/by_id', headers=ADMIN), 400, 'param_required')
        body = self.check(self.client.get('/q/by_id?id=x', headers=ADMIN), 400, 'param_invalid')
        self.assertIn('id', body['errors'])
        self.check(self.client.get('/q/by_id?id=1&version=9', headers=ADMIN), 404, 'version_not_found')
        self.check(self.client.get('/q/nothing', headers=ADMIN), 404, 'query_not_found')
        self.check(self.client.get('/q/by_id?id=1&format=pdf', headers=ADMIN), 400, 'invalid_format')
        self.check(self.client.get('/q/by_id?id=1&page=0', headers=ADMIN), 400, 'invalid_paging')
        self.check(self.client.get('/q/by_id?id=1'), 401, 'unauthorized')
        self.check(self.client.get('/no/such/route', headers=ADMIN), 404, 'not_found')
        self.check(self.client.delete('/health', headers=ADMIN), 405, 'method_not_allowed')

    def test_a_failed_runs_code_is_kept_in_history(self):
        self.client.post('/execute_sql', json={'sql': 'SELECT * FROM secret', 'connection_name': 'lite'},
                         headers=self.narrow)
        [run] = self.client.get('/api/v1/history?kind=adhoc', headers=ADMIN).get_json()['items']
        self.assertEqual((run['status'], run['code']), ('error', 'table_not_allowed'))

    def test_rate_limited(self):
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_RATE_LIMIT': '1/minute'}):
            self.client.get('/catalog', headers=ADMIN)
            body = self.check(self.client.get('/catalog', headers=ADMIN), 429, 'rate_limited')
        self.assertGreater(body['retry_after'], 0)

    def test_an_unexpected_error_is_internal_error(self):
        with mock.patch('queryapigate.app.store.live_versions', side_effect=RuntimeError('boom')):
            body = self.check(self.client.get('/catalog', headers=ADMIN), 500, 'internal_error')
        self.assertNotIn('boom', str(body))

    def test_mcp_tool_errors_carry_the_same_code(self):
        def call(name, arguments, key='admin-key'):
            result = mcp_server.handle_call(self.app, key, '', '10.0.0.1', name, arguments)
            self.assertTrue(result['isError'], result)
            self.assertEqual(result['content'][0]['text'], result['structuredContent']['error'])
            return result['structuredContent']['code']

        self.assertEqual(call('execute_sql', {'connection_name': 'lite'}), 'sql_required')
        self.assertEqual(call('execute_sql', {'connection_name': 'lite', 'sql': 'DELETE FROM film'}), 'read_only')
        self.assertEqual(call('by_id', {}), 'param_required')
        self.assertEqual(call('by_id', {}, key='wrong'), 'unauthorized')


if __name__ == '__main__':
    unittest.main()

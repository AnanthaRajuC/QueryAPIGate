"""Tests for mcp_server.py - the MCP server exposing read-only saved queries as tools (BACKLOG #42).

The pure helpers (is_read_only, input_schema, list_tools_for) and call_tool_for() need no `mcp` package
install - they're plain Python/Flask, exercised directly here. A real end-to-end test against the actual
`mcp` SDK's client/server wiring lives in EndToEndMcpTests, skipped when that optional extra isn't
installed."""
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest import mock

from queryapigate import apikeys, config, create_app, mcp_server, store
from tests.helpers import save_query, write_connections

try:
    import mcp  # noqa: F401
    HAVE_MCP_SDK = True
except ImportError:
    HAVE_MCP_SDK = False


class McpTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        tmp = self.tmp.name
        self.db_path = os.path.join(tmp, 'test.db')
        conn = sqlite3.connect(self.db_path)
        conn.execute('CREATE TABLE t (id INTEGER, name TEXT)')
        conn.executemany('INSERT INTO t VALUES (?, ?)', [(i, f'row{i}') for i in range(1, 6)])
        conn.commit()
        conn.close()
        patcher = mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': tmp, 'QUERYAPIGATE_API_KEY': 'admin-key'})
        patcher.start()
        self.addCleanup(patcher.stop)
        write_connections({'a': {'db': 'sqlite', 'database': self.db_path, 'active': True}})
        self.app = create_app()
        self.client = self.app.test_client()
        self.admin_headers = {'X-API-Key': 'admin-key'}

    def save(self, filename, sql='SELECT * FROM t ORDER BY id', **extra):
        res = save_query(self.client, {
            'author': 'a', 'description': 'd', 'sql_query': sql, 'filename': filename,
            'connection_name': 'a', **extra}, headers=self.admin_headers)
        self.assertEqual(res.status_code, 201, res.get_data(as_text=True))

    def catalog(self, headers):
        res = self.client.get('/catalog', headers=headers)
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        return res.get_json()['queries']

    def create_scoped_key(self, **fields):
        res = self.client.post('/api/v1/api-keys', json={'name': 'scoped', 'connections': [], **fields},
                               headers=self.admin_headers)
        return res.get_json()['secret']


class IsReadOnlyTests(unittest.TestCase):
    def test_select_is_read_only(self):
        self.assertTrue(mcp_server.is_read_only({'sql_query': 'SELECT * FROM t'}))

    def test_with_is_read_only(self):
        self.assertTrue(mcp_server.is_read_only({'sql_query': 'WITH x AS (SELECT 1) SELECT * FROM x'}))

    def test_insert_is_not_read_only(self):
        self.assertFalse(mcp_server.is_read_only({'sql_query': 'INSERT INTO t VALUES (1, 2)'}))

    def test_update_is_not_read_only(self):
        self.assertFalse(mcp_server.is_read_only({'sql_query': 'UPDATE t SET name = 1'}))

    def test_delete_is_not_read_only(self):
        self.assertFalse(mcp_server.is_read_only({'sql_query': 'DELETE FROM t'}))

    def test_mongo_find_is_always_read_only(self):
        self.assertTrue(mcp_server.is_read_only({'query_type': 'mongo', 'mongo_collection': 'c'}))


class InputSchemaTests(unittest.TestCase):
    def test_plain_placeholder_is_required_string(self):
        schema = mcp_server.input_schema({'sql_query': 'SELECT * FROM t WHERE id = :id', 'query_parameters': {}})
        self.assertEqual(schema['properties']['id'], {'type': 'string'})
        self.assertEqual(schema['required'], ['id'])

    def test_declared_rules_become_json_schema_keywords(self):
        data = {'sql_query': 'SELECT * FROM t WHERE id = :id AND name = :name',
                'query_parameters': {'id': {'type': 'int', 'min': 1, 'max': 100},
                                     'name': {'type': 'str', 'enum': ['a', 'b'], 'required': False}}}
        schema = mcp_server.input_schema(data)
        self.assertEqual(schema['properties']['id'], {'type': 'integer', 'minimum': 1, 'maximum': 100})
        self.assertEqual(schema['properties']['name'], {'type': 'string', 'enum': ['a', 'b']})
        self.assertEqual(schema['required'], ['id'])  # name is optional, not listed

    def test_a_query_with_no_placeholders_has_an_empty_schema(self):
        schema = mcp_server.input_schema({'sql_query': 'SELECT * FROM t', 'query_parameters': {}})
        self.assertEqual(schema, {'type': 'object', 'properties': {}, 'required': []})


class ListToolsForTests(McpTestCase):
    def test_matches_catalog_for_the_admin_key(self):
        self.save('q1')
        self.save('q2', sql='SELECT id FROM t WHERE id = :id')
        tool_names = {t['name'] for t in mcp_server.list_tools_for(apikeys.OPEN)}
        catalog_names = {q['name'] for q in self.catalog(self.admin_headers)}
        # The admin key also always sees the two fixed, ad-hoc tools (list_tables/execute_sql) - catalog
        # has no equivalent concept, so they're the one expected difference here.
        self.assertEqual(tool_names, catalog_names | {'list_tables', 'execute_sql'})
        self.assertEqual(tool_names, {'q1', 'q2', 'list_tables', 'execute_sql'})

    def test_matches_catalog_for_a_scoped_key(self):
        self.save('reachable')
        self.save('unreachable')
        key = self.create_scoped_key(queries=['reachable'])
        permission = apikeys.authenticate(key)
        tool_names = {t['name'] for t in mcp_server.list_tools_for(permission)}
        catalog_names = {q['name'] for q in self.catalog({'X-API-Key': key})}
        self.assertEqual(tool_names, catalog_names)
        self.assertEqual(tool_names, {'reachable'})

    def test_a_write_query_is_excluded(self):
        self.save('reader', sql='SELECT * FROM t')
        self.save('writer', sql='DELETE FROM t')
        tool_names = {t['name'] for t in mcp_server.list_tools_for(apikeys.OPEN)}
        self.assertEqual(tool_names, {'reader', 'list_tables', 'execute_sql'})

    def test_an_unauthenticated_caller_via_the_server_sees_nothing(self):
        # tools/list's own resolution: a missing or wrong key, with a server key configured, is no caller at all
        self.assertIsNone(mcp_server.permission_for_listing(self.app, 'wrong-key', '', '10.0.0.1'))
        self.assertIsNone(mcp_server.permission_for_listing(self.app, '', '', '10.0.0.1'))
        self.assertTrue(mcp_server.permission_for_listing(self.app, 'admin-key', '', '10.0.0.1').admin)

    def test_tool_shape_carries_schema_and_hints(self):
        self.save('q1', sql='SELECT * FROM t WHERE id = :id')
        tools = mcp_server.list_tools_for(apikeys.OPEN)
        tool = next(t for t in tools if t['name'] == 'q1')
        self.assertEqual(tool['description'], 'd')
        self.assertEqual(tool['inputSchema']['required'], ['id'])
        self.assertEqual(tool['outputSchema'], mcp_server.ROWS_OUTPUT_SCHEMA)
        self.assertTrue(tool['readOnlyHint'])
        self.assertTrue(tool['idempotentHint'])


class CallToolForTests(McpTestCase):
    def test_returns_the_same_rows_as_the_rest_endpoint(self):
        self.save('q1')
        rest_rows = self.client.get('/q/q1', headers=self.admin_headers).get_json()
        result = mcp_server.call_tool_for(self.app, apikeys.OPEN, 'q1', {})
        self.assertEqual(result['structuredContent']['rows'], rest_rows)
        # The text block is a serialized copy of the same structuredContent, per the MCP spec's own
        # backwards-compatibility convention for a tool with an outputSchema - not something separate.
        self.assertEqual(json.loads(result['content'][0]['text']), result['structuredContent'])

    def test_parameters_are_bound(self):
        self.save('by_id', sql='SELECT * FROM t WHERE id = :id')
        result = mcp_server.call_tool_for(self.app, apikeys.OPEN, 'by_id', {'id': 3})
        self.assertEqual(result['structuredContent']['rows'], [{'id': 3, 'name': 'row3'}])

    def test_a_connection_grant_violation_is_reported_as_an_error_result(self):
        self.save('q1')
        key = self.create_scoped_key(connections=[])  # no reach to connection 'a' or the query by name
        permission = apikeys.authenticate(key)
        result = mcp_server.call_tool_for(self.app, permission, 'q1', {})
        self.assertTrue(result['isError'])
        self.assertIn('content', result)

    def test_an_unknown_query_is_reported_as_an_error_result_not_raised(self):
        result = mcp_server.call_tool_for(self.app, apikeys.OPEN, 'nope', {})
        self.assertTrue(result['isError'])

    def test_result_is_capped_to_mcp_max_rows_and_marked_truncated(self):
        self.save('all_rows')
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_MCP_MAX_ROWS': '2'}):
            result = mcp_server.call_tool_for(self.app, apikeys.OPEN, 'all_rows', {})
        self.assertEqual(len(result['structuredContent']['rows']), 2)
        self.assertTrue(result['structuredContent']['truncated'])

    def test_an_explicit_smaller_page_size_is_honored(self):
        self.save('all_rows')
        result = mcp_server.call_tool_for(self.app, apikeys.OPEN, 'all_rows', {'page_size': 1})
        self.assertEqual(len(result['structuredContent']['rows']), 1)

    def test_the_run_is_recorded_in_execution_history(self):
        self.save('q1')
        mcp_server.call_tool_for(self.app, apikeys.OPEN, 'q1', {})
        content = store.load_versions('q1')
        history = content['1']['execution_history']
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]['status'], 'success')

    def test_the_run_is_broadcast_to_the_live_home_tab_feed(self):
        self.save('q1')
        broadcaster = self.app.extensions['queryapigate_broadcaster']
        subscriber = broadcaster.subscribe()
        mcp_server.call_tool_for(self.app, apikeys.OPEN, 'q1', {})
        event = subscriber.get_nowait()
        self.assertEqual(event['type'], 'execution')
        self.assertEqual(event['filename'], 'q1')

    def test_a_cache_ttl_carrying_query_is_actually_served_from_cache_on_the_second_call(self):
        self.save('cached', cache_ttl=60)
        mcp_server.call_tool_for(self.app, apikeys.OPEN, 'cached', {})
        mcp_server.call_tool_for(self.app, apikeys.OPEN, 'cached', {})
        content = store.load_versions('cached')
        # A cache hit returns early in run_saved(), before execution_history is ever touched - so exactly
        # one entry (not two) is the direct evidence the second call was actually served from cache.
        self.assertEqual(len(content['1']['execution_history']), 1)


class GovernanceParityTests(McpTestCase):
    """BACKLOG #61: a call over MCP is governed exactly like the same call over REST - the same outcome (allowed,
    denied, rate-limited), counted in /metrics, and recorded in history. Keeps a future front door from skipping
    what REST's request hooks apply."""

    def setUp(self):
        super().setUp()
        self.save('q1')

    def key(self, name, **grants):
        res = self.client.post('/api/v1/api-keys', json={'name': name, **grants}, headers=self.admin_headers)
        self.assertEqual(res.status_code, 201, res.get_data(as_text=True))
        return res.get_json()['secret']

    def over_rest(self, secret, query):
        return self.client.get(f'/q/{query}', headers={'X-API-Key': secret}).status_code

    def over_mcp(self, secret, query):
        result = mcp_server.handle_call(self.app, secret, '', '10.0.0.9', query, {})
        if not result.get('isError'):
            return 200
        text = result['content'][0]['text']
        return 429 if 'Rate limit' in text else 401 if 'Unauthorized' in text else 403

    def test_the_same_outcome_over_both_front_doors(self):
        for transport in ('rest', 'mcp'):
            call = self.over_rest if transport == 'rest' else self.over_mcp
            allowed = self.key(f'{transport}-allowed', connections=['a'], rate_limit='2/minute')
            denied = self.key(f'{transport}-denied', connections=[])
            self.assertEqual(call(allowed, 'q1'), 200, transport)
            self.assertEqual(call(denied, 'q1'), 403, transport)
            self.assertEqual(call('wrong-key', 'q1'), 401, transport)
            self.assertEqual(call(allowed, 'q1'), 200, transport)
            self.assertEqual(call(allowed, 'q1'), 429, transport)  # its own 2/minute

    def test_both_are_recorded_in_history_with_the_caller(self):
        secret = self.key('agent', connections=['a'])
        self.over_rest(secret, 'q1')
        self.over_mcp(secret, 'q1')
        runs = self.client.get('/api/v1/queries/q1/history', headers=self.admin_headers).get_json()['items']
        self.assertEqual([r['key_name'] for r in runs], ['agent', 'agent'])

    def test_mcp_calls_are_counted_in_metrics_apart_from_rest(self):
        secret = self.key('metrics-mcp-agent', connections=['a'])
        self.over_mcp(secret, 'q1')
        self.over_mcp(secret, 'nope')
        self.over_mcp(secret, 'execute_sql')  # no connection_name: an error result, still counted
        body = self.client.get('/metrics').get_data(as_text=True)
        self.assertIn('queryapigate_requests_total{method="MCP",endpoint="mcp.saved_query",status="200",'
                      'key="metrics-mcp-agent"} 1', body)
        self.assertIn('queryapigate_requests_total{method="MCP",endpoint="mcp.saved_query",status="404",'
                      'key="metrics-mcp-agent"} 1', body)
        self.assertIn('queryapigate_requests_total{method="MCP",endpoint="mcp.execute_sql",status="400",'
                      'key="metrics-mcp-agent"} 1', body)

    def test_the_server_wide_client_limit_applies_before_authentication(self):
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_RATE_LIMIT': '2/minute'}):
            results = [mcp_server.handle_call(self.app, 'wrong-key', '', '10.0.0.77', 'q1', {})['content'][0]['text']
                       for _ in range(3)]
        self.assertIn('Unauthorized', results[0])
        self.assertIn('Rate limit exceeded', results[2])  # guessing keys is throttled too

    def test_a_signed_in_user_can_call_tools_with_a_bearer_token(self):
        import time

        import jwt
        secret = 'a-shared-secret-that-is-long-enough-for-hs256'
        self.client.post('/api/v1/roles', json={'name': 'agents', 'connections': ['a']}, headers=self.admin_headers)
        token = jwt.encode({'sub': 'alice', 'exp': int(time.time()) + 300}, secret, algorithm='HS256')
        env = {'QUERYAPIGATE_JWT_SECRET': secret, 'QUERYAPIGATE_JWT_ROLE': 'agents'}
        with mock.patch.dict(os.environ, env):
            result = mcp_server.handle_call(self.app, '', f'Bearer {token}', '10.0.0.9', 'q1', {})
            listed = mcp_server.permission_for_listing(self.app, '', f'Bearer {token}', '10.0.0.9')
        self.assertFalse(result.get('isError'), result)
        self.assertEqual(listed.name, 'jwt:alice')
        runs = self.client.get('/api/v1/queries/q1/history', headers=self.admin_headers).get_json()['items']
        self.assertEqual(runs[0]['key_name'], 'jwt:alice')


class FixedToolsTests(McpTestCase):
    """list_tables/execute_sql: the two ad-hoc tools gated by a key's ``connections`` grant rather than any
    saved query."""

    def test_a_query_only_key_sees_neither_fixed_tool(self):
        key = self.create_scoped_key(connections=[], queries=['*'])
        permission = apikeys.authenticate(key)
        tool_names = {t['name'] for t in mcp_server.fixed_tools_for(permission)}
        self.assertEqual(tool_names, set())

    def test_a_connection_scoped_key_sees_both_fixed_tools(self):
        key = self.create_scoped_key(connections=['a'])
        permission = apikeys.authenticate(key)
        tool_names = {t['name'] for t in mcp_server.fixed_tools_for(permission)}
        self.assertEqual(tool_names, {'list_tables', 'execute_sql'})

    def test_list_tables_returns_the_table_and_its_columns(self):
        result = mcp_server.list_tables_tool(apikeys.OPEN, {'connection_name': 'a'})
        self.assertNotIn('isError', result)
        self.assertEqual(json.loads(result['content'][0]['text']), result['structuredContent'])
        table = next(t for t in result['structuredContent']['tables'] if t['name'] == 't')
        self.assertEqual({c['name'] for c in table['columns']}, {'id', 'name'})

    def test_list_tables_is_refused_for_a_connection_the_key_cannot_use(self):
        key = self.create_scoped_key(connections=[])
        permission = apikeys.authenticate(key)
        result = mcp_server.list_tables_tool(permission, {'connection_name': 'a'})
        self.assertTrue(result['isError'])
        self.assertIn('not permitted', result['content'][0]['text'])

    def test_list_tables_requires_connection_name(self):
        result = mcp_server.list_tables_tool(apikeys.OPEN, {})
        self.assertTrue(result['isError'])

    def test_execute_sql_returns_rows(self):
        result = mcp_server.execute_sql_tool(apikeys.OPEN, {'connection_name': 'a', 'sql': 'SELECT * FROM t'})
        self.assertEqual(json.loads(result['content'][0]['text']), result['structuredContent'])
        self.assertEqual(len(result['structuredContent']['rows']), 5)
        self.assertFalse(result['structuredContent']['truncated'])

    def test_execute_sql_binds_parameters(self):
        result = mcp_server.execute_sql_tool(apikeys.OPEN, {
            'connection_name': 'a', 'sql': 'SELECT * FROM t WHERE id = :id', 'params': {'id': 3}})
        self.assertEqual(result['structuredContent']['rows'], [{'id': 3, 'name': 'row3'}])

    def test_execute_sql_is_always_read_only_even_for_a_writes_allowed_key(self):
        key = self.create_scoped_key(connections=['a'], allow_writes=True)
        permission = apikeys.authenticate(key)
        result = mcp_server.execute_sql_tool(permission, {'connection_name': 'a', 'sql': "DELETE FROM t"})
        self.assertTrue(result['isError'])

    def test_execute_sql_is_refused_for_a_connection_the_key_cannot_use(self):
        key = self.create_scoped_key(connections=[])
        permission = apikeys.authenticate(key)
        result = mcp_server.execute_sql_tool(permission, {'connection_name': 'a', 'sql': 'SELECT * FROM t'})
        self.assertTrue(result['isError'])

    def test_execute_sql_honours_a_key_s_allowed_tables(self):
        conn = store.get_connection('a')
        raw = sqlite3.connect(conn['database'])
        raw.execute('CREATE TABLE other (id INTEGER)')
        raw.commit()
        raw.close()
        key = self.create_scoped_key(connections=['a'], allowed_tables=['t'])
        permission = apikeys.authenticate(key)
        allowed = mcp_server.execute_sql_tool(permission, {'connection_name': 'a', 'sql': 'SELECT * FROM t'})
        self.assertNotIn('isError', allowed)
        refused = mcp_server.execute_sql_tool(permission, {'connection_name': 'a', 'sql': 'SELECT * FROM other'})
        self.assertTrue(refused['isError'])

    def test_execute_sql_requires_sql(self):
        result = mcp_server.execute_sql_tool(apikeys.OPEN, {'connection_name': 'a'})
        self.assertTrue(result['isError'])

    def test_execute_sql_result_is_capped_to_mcp_max_rows_and_marked_truncated(self):
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_MCP_MAX_ROWS': '2'}):
            result = mcp_server.execute_sql_tool(apikeys.OPEN, {'connection_name': 'a', 'sql': 'SELECT * FROM t'})
        self.assertEqual(len(result['structuredContent']['rows']), 2)
        self.assertTrue(result['structuredContent']['truncated'])

    def test_dispatch_routes_fixed_tools_and_falls_back_to_saved_queries(self):
        self.save('q1')
        tables = mcp_server.dispatch_tool_call(self.app, apikeys.OPEN, 'list_tables', {'connection_name': 'a'})
        self.assertNotIn('isError', tables)
        rows = mcp_server.dispatch_tool_call(self.app, apikeys.OPEN, 'execute_sql',
                                             {'connection_name': 'a', 'sql': 'SELECT * FROM t'})
        self.assertNotIn('isError', rows)
        saved = mcp_server.dispatch_tool_call(self.app, apikeys.OPEN, 'q1', {})
        self.assertNotIn('isError', saved)


@unittest.skipUnless(HAVE_MCP_SDK, 'queryapigate[mcp] not installed')
class EndToEndMcpTests(McpTestCase):
    """A real MCP client talking to a real running mcp_server.run() instance over Streamable HTTP - proves
    the actual SDK wiring (session management, header propagation, JSON-RPC framing) works, not just that
    list_tools_for()/call_tool_for() are individually correct in isolation."""

    def start_server(self):
        port = config.mcp_port() + 1000  # avoid colliding with a real dev server that might be running
        thread = threading.Thread(target=mcp_server.run, args=(self.app, '127.0.0.1', port), daemon=True)
        thread.start()
        time.sleep(1.0)
        return port

    def test_a_real_client_can_list_and_call_a_tool(self):
        self.save('q1')
        port = self.start_server()

        import asyncio

        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        async def talk():
            url = f'http://127.0.0.1:{port}/mcp'
            async with streamablehttp_client(url, headers={'X-API-Key': 'admin-key'}) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    names = [t.name for t in tools.tools]
                    result = await session.call_tool('q1', {})
                    return names, result

        names, result = asyncio.run(talk())
        self.assertIn('q1', names)
        self.assertIn('list_tables', names)
        self.assertIn('execute_sql', names)
        self.assertFalse(result.isError)
        # structuredContent proves outputSchema round-trips through the real SDK's own validation/parsing,
        # not just this module's own plain-dict shape.
        self.assertEqual(len(result.structuredContent['rows']), 5)
        self.assertFalse(result.structuredContent['truncated'])
        self.assertEqual(json.loads(result.content[0].text), result.structuredContent)
        # This process's own /metrics shows the call - REST's can't, it runs in another process (BACKLOG #61).
        import urllib.request
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/metrics', timeout=5) as res:
            self.assertIn('method="MCP",endpoint="mcp.saved_query",status="200"', res.read().decode())
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=5) as res:
            self.assertEqual(json.loads(res.read())['status'], 'ok')


if __name__ == '__main__':
    unittest.main()

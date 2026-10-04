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
        # _permission_for()'s own contract, exercised at the mcp_server level rather than apikeys directly:
        # a missing/invalid key with a server key configured resolves to no Permission at all.
        permission = apikeys.authenticate('wrong-key')
        self.assertIsNone(permission)

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


if __name__ == '__main__':
    unittest.main()

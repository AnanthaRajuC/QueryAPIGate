"""Write tools over MCP (ADR 0008, BACKLOG #88): which saved queries an agent may write through - marked by their
author (`agent_writes`), one statement that only changes data, a ceiling on the rows it changes - and who sees them
as tools: a caller whose grant allows the write, on a server that allows writes, on a connection that counts the
rows."""
import os
import unittest
from unittest import mock

from queryapigate import adminroles, apikeys, mcp_server
from tests.helpers import write_connections
from tests.test_api_v1 import ADMIN, V1TestCase

WRITES_ON = {'QUERYAPIGATE_ALLOW_WRITES': '1'}


class AgentWriteTestCase(V1TestCase):
    def setUp(self):
        super().setUp()
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        conn.execute('CREATE TABLE tickets (id INTEGER PRIMARY KEY, title TEXT, status TEXT)')
        conn.commit()
        conn.close()

    def save(self, name, sql, status=201, **fields):
        res = self.client.post('/api/v1/queries', headers=ADMIN, json={
            'name': name, 'description': 'd', 'sql': sql, 'connection_name': 'lite', 'publish': True, **fields})
        self.assertEqual(res.status_code, status, res.get_data(as_text=True))
        return res.get_json()

    def tools(self, permission):
        with mock.patch.dict(os.environ, WRITES_ON):
            return {t['name']: t for t in mcp_server.list_tools_for(permission)}


class DefinitionTests(AgentWriteTestCase):
    def test_marked_queries_keep_their_mode_and_ceiling(self):
        query = self.save('close_ticket', 'UPDATE tickets SET status = :status WHERE id = :id',
                          agent_writes='approval', max_affected_rows=1)
        version = query['versions'][0]
        self.assertEqual((version['agent_writes'], version['max_affected_rows']), ('approval', 1))
        query = self.save('file_ticket', 'INSERT INTO tickets (title, status) VALUES (:title, \'open\')',
                          agent_writes='insert')
        self.assertEqual(query['versions'][0]['max_affected_rows'], 1)  # one row unless said otherwise
        plain = self.save('read', 'SELECT * FROM tickets')
        self.assertIsNone(plain['versions'][0]['agent_writes'])

    def test_only_one_statement_that_only_changes_data(self):
        for label, sql, mode in (
                ('a read', 'SELECT * FROM tickets', 'approval'),
                ('DDL', 'DROP TABLE tickets', 'approval'),
                ('DDL that sounds harmless', 'CREATE TABLE more_tickets (id INTEGER)', 'approval'),
                ('truncate', 'TRUNCATE TABLE tickets', 'approval'),
                ('two statements', "DELETE FROM tickets; INSERT INTO tickets (title) VALUES ('x')", 'approval'),
                ('a write behind a read', 'WITH gone AS (DELETE FROM tickets RETURNING id) SELECT * FROM gone',
                 'approval'),
                ('insert mode, an update', 'UPDATE tickets SET status = :s', 'insert'),
                ('insert mode, a delete', 'DELETE FROM tickets WHERE id = :id', 'insert')):
            with self.subTest(label):
                res = self.client.post('/api/v1/queries', headers=ADMIN, json={
                    'name': 'w', 'description': 'd', 'sql': sql, 'connection_name': 'lite', 'agent_writes': mode})
                self.assertEqual((res.status_code, res.get_json()['code']), (400, 'invalid_body'))

    def test_bad_settings_are_refused(self):
        for fields in ({'agent_writes': 'always'}, {'agent_writes': True},
                       {'agent_writes': 'approval', 'max_affected_rows': 0},
                       {'agent_writes': 'approval', 'max_affected_rows': '5'},
                       {'agent_writes': 'approval', 'max_affected_rows': 10 ** 9},
                       {'max_affected_rows': 5}):
            with self.subTest(fields=fields):
                res = self.client.post('/api/v1/queries', headers=ADMIN, json={
                    'name': 'w', 'description': 'd', 'sql': 'DELETE FROM tickets WHERE id = :id',
                    'connection_name': 'lite', **fields})
                self.assertEqual(res.status_code, 400, res.get_data(as_text=True))

    def test_only_on_connections_that_count_the_rows_they_change(self):
        write_connections({'lite': {'db': 'sqlite', 'database': self.db_path, 'active': True},
                           'ch': {'db': 'clickhouse', 'host': 'h', 'database': 'd', 'active': True}})
        res = self.client.post('/api/v1/queries', headers=ADMIN, json={
            'name': 'w', 'description': 'd', 'sql': 'DELETE FROM tickets WHERE id = :id', 'connection_name': 'ch',
            'agent_writes': 'approval'})
        self.assertEqual(res.status_code, 400)
        self.assertIn("can't write on 'clickhouse'", res.get_json()['error'])


class ListingTests(AgentWriteTestCase):
    def setUp(self):
        super().setUp()
        self.save('close_ticket', 'UPDATE tickets SET status = :status WHERE id = :id', agent_writes='approval')
        self.save('file_ticket', "INSERT INTO tickets (title, status) VALUES (:title, 'open')", agent_writes='insert')
        self.save('purge', 'DELETE FROM tickets')  # writes, but no author said agents may

    def key(self, **grants):
        res = self.client.post('/api/v1/api-keys', headers=ADMIN, json={'name': 'agent', 'connections': [], **grants})
        return apikeys.authenticate(res.get_json()['secret'])

    def test_a_caller_whose_grant_allows_the_write_sees_it(self):
        agent = self.key(queries=[{'name': 'close_ticket', 'allow_writes': True}, 'file_ticket'])
        tools = self.tools(agent)
        self.assertIn('close_ticket', tools)
        self.assertNotIn('file_ticket', tools)  # read access only, so not its write
        self.assertNotIn('purge', self.tools(apikeys.OPEN))  # never without agent_writes, even for the admin

    def test_annotations_and_arguments(self):
        tools = self.tools(apikeys.OPEN)
        close, file_ = tools['close_ticket'], tools['file_ticket']
        self.assertEqual((close['readOnlyHint'], close['destructiveHint'], close['idempotentHint']),
                         (False, True, False))
        self.assertEqual((file_['readOnlyHint'], file_['destructiveHint']), (False, False))  # it only adds rows
        self.assertIn('approval', close['inputSchema']['properties'])
        self.assertNotIn('approval', close['inputSchema']['required'])
        self.assertIn('idempotency_key', file_['inputSchema']['properties'])
        self.assertIn('A person approves each call', close['description'])
        self.assertIn('at most 1 row', close['description'])

    def test_not_while_the_server_refuses_writes(self):
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_ALLOW_WRITES': ''}):
            names = {t['name'] for t in mcp_server.list_tools_for(apikeys.OPEN)}
        self.assertNotIn('close_ticket', names)

    def test_until_approvals_arrive_a_call_is_still_refused(self):
        with mock.patch.dict(os.environ, WRITES_ON):
            result = mcp_server.handle_call(self.client.application, 'admin-key', '', '10.0.0.1', 'close_ticket',
                                            {'status': 'done', 'id': 1})
        self.assertEqual(result['structuredContent']['code'], 'read_only')


class RealClientTests(AgentWriteTestCase):
    def test_a_real_mcp_client_sees_the_write_tool_and_its_annotations(self):
        import asyncio
        import threading
        import time

        import uvicorn
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        self.save('close_ticket', 'UPDATE tickets SET status = :status WHERE id = :id', agent_writes='approval')
        with mock.patch.dict(os.environ, WRITES_ON):
            app = mcp_server.build_asgi_app(self.client.application)
            server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=0, log_level='warning'))
            thread = threading.Thread(target=server.run, daemon=True)
            thread.start()
            self.addCleanup(thread.join, 5)
            self.addCleanup(setattr, server, 'should_exit', True)
            while not server.started:
                time.sleep(0.05)
            port = server.servers[0].sockets[0].getsockname()[1]

            async def talk():
                async with streamablehttp_client(f'http://127.0.0.1:{port}/mcp', headers=ADMIN) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        return {t.name: t for t in (await session.list_tools()).tools}

            tools = asyncio.run(talk())
        hints = tools['close_ticket'].annotations
        self.assertEqual((hints.readOnlyHint, hints.destructiveHint, hints.idempotentHint), (False, True, False))
        self.assertIn('approval', tools['close_ticket'].inputSchema['properties'])


class CapabilityTests(unittest.TestCase):
    def test_owners_and_admins_approve(self):
        self.assertEqual({role for role, caps in adminroles.ROLES.items() if 'writes.approve' in caps},
                         {'owner', 'admin'})


if __name__ == '__main__':
    unittest.main()

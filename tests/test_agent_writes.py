"""Write tools over MCP (ADR 0008, BACKLOG #88): which saved queries an agent may write through - marked by their
author (`agent_writes`), one statement that only changes data, a ceiling on the rows it changes - and who sees them
as tools: a caller whose grant allows the write, on a server that allows writes, on a connection that counts the
rows."""
import os
import unittest
from unittest import mock

from queryapigate import adminroles, apikeys, mcp_server
from tests.helpers import needs_sqlite_335, write_connections
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
        from queryapigate import masking
        masking.forget_schema()  # another test's 'lite' may still be cached

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



class FlowTests(AgentWriteTestCase):
    """The approval round trip (ADR 0008, 2): ask, approve, run once - and every way the second call is refused."""

    def setUp(self):
        super().setUp()
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        conn.execute('ALTER TABLE tickets ADD COLUMN touched INTEGER DEFAULT 0')
        conn.executemany("INSERT INTO tickets (id, title, status) VALUES (?, ?, 'open')",
                         [(1, 'printer'), (2, 'vpn'), (3, 'laptop')])
        conn.commit()
        conn.close()
        self.save('close_ticket', 'UPDATE tickets SET status = :status, touched = touched + 1 WHERE id = :id',
                  agent_writes='approval')
        self.save('close_all', 'UPDATE tickets SET status = :status', agent_writes='approval', max_affected_rows=2)
        self.save('file_ticket', "INSERT INTO tickets (title, status) VALUES (:title, 'open') RETURNING id, title",
                  agent_writes='insert')
        self.agent = self.key('agent', [{'name': n, 'allow_writes': True}
                                        for n in ('close_ticket', 'close_all', 'file_ticket')])
        patcher = mock.patch.dict(os.environ, WRITES_ON)
        patcher.start()
        self.addCleanup(patcher.stop)

    def key(self, name, queries, **grants):
        res = self.client.post('/api/v1/api-keys', headers=ADMIN,
                               json={'name': name, 'connections': [], 'queries': queries, **grants})
        self.assertEqual(res.status_code, 201, res.get_data(as_text=True))
        return res.get_json()['secret']

    def call(self, tool, secret=None, **arguments):
        return mcp_server.handle_call(self.client.application, secret or self.agent, '', '10.0.0.1', tool, arguments)

    def ticket(self, ticket_id):
        import sqlite3
        return sqlite3.connect(self.db_path).execute('SELECT status, touched FROM tickets WHERE id = ?',
                                                     (ticket_id,)).fetchone()

    def decide(self, request_id, verdict, status=200, headers=ADMIN, **body):
        return self.call_api('post', f'/api/v1/write-requests/{request_id}/{verdict}', status, headers, body)

    def call_api(self, method, url, status, headers=ADMIN, body=None):
        res = getattr(self.client, method)(url, headers=headers, json=body if method == 'post' else None)
        self.assertEqual(res.status_code, status, res.get_data(as_text=True))
        return res.get_json()

    def test_ask_approve_run_once(self):
        asked = self.call('close_ticket', status='done', id=1)['structuredContent']
        self.assertEqual(asked['status'], 'approval_required')
        request_id = asked['request']
        self.assertEqual(asked['approve_at'], f'/console/approvals/{request_id}')
        self.assertIn(f'approval: "{request_id}"', asked['message'])
        self.assertEqual(self.ticket(1), ('open', 0))  # asking wrote nothing
        pending = self.call_api('get', '/api/v1/write-requests?status=pending', 200)['items']
        self.assertEqual([(r['id'], r['caller'], r['arguments']) for r in pending],
                         [(request_id, 'agent', {'status': 'done', 'id': 1})])
        self.assertNotIn('_arguments_hash', pending[0])
        early = self.call('close_ticket', status='done', id=1, approval=request_id)['structuredContent']
        self.assertEqual(early['code'], 'approval_pending')
        approved = self.decide(request_id, 'approve')
        self.assertEqual((approved['status'], approved['decided_by'], approved['decided_via']),
                         ('approved', 'admin', 'break-glass'))
        other = self.call('close_ticket', status='done', id=2, approval=request_id)['structuredContent']
        self.assertEqual(other['code'], 'approval_mismatch')  # an approval is for exactly one write
        stranger = self.key('stranger', [{'name': 'close_ticket', 'allow_writes': True}])
        stolen = self.call('close_ticket', secret=stranger, status='done', id=1, approval=request_id)
        self.assertEqual(stolen['structuredContent']['code'], 'write_request_not_found')  # another caller's
        done = self.call('close_ticket', status='done', id=1, approval=request_id)['structuredContent']
        self.assertEqual((done['status'], done['rows_affected']), ('done', 1))
        self.assertEqual(self.ticket(1), ('done', 1))
        again = self.call('close_ticket', status='done', id=1, approval=request_id)['structuredContent']
        self.assertEqual((again['status'], again['rows_affected']), ('done', 1))
        self.assertEqual(self.ticket(1), ('done', 1))  # a retry doesn't write twice
        self.assertEqual(self.ticket(2), ('open', 0))
        runs = self.call_api('get', '/api/v1/history?key=agent', 200)['items']
        self.assertEqual([(r['request'], r['approved_by'], r['rows'], r['transport']) for r in runs],
                         [(request_id, 'admin', 1, 'mcp')])
        from queryapigate import store
        entry = next(e for e in store.read_audit_log() if e['action'] == 'approve_write')
        # names and a hash, never the values - auditors read this, and have no data access
        self.assertEqual((entry['target'], entry['changes']['arguments']), (request_id, ['id', 'status']))
        self.assertEqual(len(entry['changes']['arguments_hash']), 64)

    def test_a_rejection_and_an_expiry(self):
        rejected = self.call('close_ticket', status='done', id=1)['structuredContent']['request']
        self.decide(rejected, 'reject', reason='Ticket 1 is still being worked on')
        refused = self.call('close_ticket', status='done', id=1, approval=rejected)['structuredContent']
        self.assertEqual(refused['code'], 'approval_rejected')
        self.assertIn('still being worked on', refused['error'])
        self.decide(rejected, 'approve', status=409)  # decided already
        import time as clock
        late = self.call('close_ticket', status='done', id=2)['structuredContent']['request']
        later = clock.time() + 16 * 60
        with mock.patch('time.time', return_value=later):
            self.decide(late, 'approve', status=409)  # expired before anyone approved it
            expired = self.call('close_ticket', status='done', id=2, approval=late)['structuredContent']
        self.assertEqual(expired['code'], 'approval_expired')
        self.assertEqual(self.ticket(2), ('open', 0))

    def test_a_write_that_changes_too_many_rows_is_rolled_back(self):
        request_id = self.call('close_all', status='closed')['structuredContent']['request']
        self.decide(request_id, 'approve')
        failed = self.call('close_all', status='closed', approval=request_id)['structuredContent']
        self.assertEqual(failed['code'], 'too_many_rows_affected')
        self.assertIn('would have changed 3 rows', failed['error'])
        self.assertEqual([self.ticket(i)[0] for i in (1, 2, 3)], ['open', 'open', 'open'])  # nothing changed
        retried = self.call('close_all', status='closed', approval=request_id)['structuredContent']
        self.assertEqual(retried['code'], 'too_many_rows_affected')  # the same answer, not a second try
        status = self.call_api('get', f'/api/v1/write-requests/{request_id}', 200)
        self.assertEqual((status['status'], status['result']['code']), ('failed', 'too_many_rows_affected'))

    @needs_sqlite_335
    def test_an_insert_runs_at_once_and_once_per_idempotency_key(self):
        first = self.call('file_ticket', title='monitor', idempotency_key='k1')['structuredContent']
        self.assertEqual((first['status'], first['rows_affected'], first['rows'][0]['title']), ('done', 1, 'monitor'))
        again = self.call('file_ticket', title='monitor', idempotency_key='k1')['structuredContent']
        self.assertEqual(again['rows'], first['rows'])
        import sqlite3
        count = sqlite3.connect(self.db_path).execute("SELECT COUNT(*) FROM tickets WHERE title = 'monitor'")
        self.assertEqual(count.fetchone()[0], 1)
        conflict = self.call('file_ticket', title='keyboard', idempotency_key='k1')['structuredContent']
        self.assertEqual(conflict['code'], 'idempotency_conflict')
        self.assertEqual(self.call('file_ticket', title='mouse')['structuredContent']['status'], 'done')

    @needs_sqlite_335
    def test_what_a_write_returns_is_masked_like_any_read(self):
        masked = self.key('masked', [{'name': 'file_ticket', 'allow_writes': True}],
                          mask=[{'column': 'tickets.title', 'action': 'hash'}])
        done = self.call('file_ticket', secret=masked, title='secret laptop')['structuredContent']
        self.assertEqual(done['status'], 'done')
        self.assertNotEqual(done['rows'][0]['title'], 'secret laptop')

    def test_only_approvers_see_and_decide(self):
        from queryapigate import admins
        request_id = self.call('close_ticket', status='done', id=1)['structuredContent']['request']
        for role, status in (('developer', 403), ('auditor', 403), ('admin', 200)):
            admins.create_admin(f'{role}-1', role)
            token = {'X-API-Key': admins.issue_token(f'{role}-1')[1]}
            with self.subTest(role=role):
                self.assertEqual(self.client.get('/api/v1/write-requests', headers=token).status_code, status)
        token = {'X-API-Key': admins.issue_token('admin-1')[1]}
        decided = self.decide(request_id, 'approve', headers=token)
        self.assertEqual((decided['decided_by'], decided['decided_via']), ('admin-1', 'token'))

    def test_a_credential_cannot_approve_the_write_it_asked_for(self):
        """Four eyes: an MCP client holding an approver's credential must not approve its own model's writes."""
        from queryapigate import admins
        admins.create_admin('alice', 'owner')
        alice = admins.issue_token('alice')[1]
        asked = self.call('close_ticket', secret=alice, status='done', id=1)['structuredContent']['request']
        refused = self.decide(asked, 'approve', status=403, headers={'X-API-Key': alice})
        self.assertEqual(refused['code'], 'own_write')
        self.decide(asked, 'approve')  # someone else - here the break-glass key - may

    def test_an_agent_cannot_pile_up_requests(self):
        from queryapigate import agentwrites
        with mock.patch.object(agentwrites, 'MAX_PENDING_PER_CALLER', 2):
            self.call('close_ticket', status='done', id=1)
            self.call('close_ticket', status='done', id=2)
            third = self.call('close_ticket', status='done', id=3)['structuredContent']
        self.assertEqual(third['code'], 'too_many_pending_writes')

    def test_waiting_writes_are_an_alert_and_every_request_is_in_agent_usage(self):
        from queryapigate import alerts
        request_id = self.call('close_ticket', status='done', id=1)['structuredContent']['request']
        found = [a for a in alerts.collect() if a['kind'] == 'writes_awaiting_approval']
        self.assertEqual([(a['title'], a['target']) for a in found],
                         [('1 write by AI agents is waiting for approval', {'type': 'approvals', 'name': 'pending'})])
        usage = {i['agent']: i for i in self.call_api('get', '/api/v1/usage/agents', 200)['items']}
        self.assertEqual(usage['agent']['writes']['pending'], 1)
        self.decide(request_id, 'reject')
        self.assertNotIn('writes_awaiting_approval', [a['kind'] for a in alerts.collect()])
        usage = {i['agent']: i for i in self.call_api('get', '/api/v1/usage/agents', 200)['items']}
        self.assertEqual((usage['agent']['writes']['rejected'], usage['agent']['writes']['pending']), (1, 0))

    def test_the_link_names_the_console_and_the_server_ceiling_still_holds(self):
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_CONSOLE_URL': 'https://data.example.com'}):
            asked = self.call('close_ticket', status='done', id=1)['structuredContent']
        self.assertTrue(asked['approve_at'].startswith('https://data.example.com/console/approvals/wr_'))
        self.decide(asked['request'], 'approve')
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_ALLOW_WRITES': ''}):
            refused = self.call('close_ticket', status='done', id=1, approval=asked['request'])['structuredContent']
        self.assertEqual(refused['code'], 'read_only')
        self.assertEqual(self.ticket(1), ('open', 0))


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

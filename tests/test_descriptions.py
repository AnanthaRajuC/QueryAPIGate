"""Descriptions for agents (BACKLOG #86, ADR 0006, descriptions.py): what tables and columns mean, written or imported
from the database's comments, served with the schema - over REST, /api/v1 and MCP's list_tables, and as a data
dictionary MCP resource - never beyond what the caller can reach, and saying what the caller gets masked. A saved
query describes its own result columns, in its MCP tool's output schema. The import itself runs against every real
database in tests/test_integration.py."""
import os
import unittest

import duckdb

from queryapigate import admins, db, descriptions, masking, mcp_server, store
from queryapigate.errors import ApiError
from tests.helpers import write_connections
from tests.test_api_v1 import ADMIN, V1TestCase

LIST = '/api/v1/connections/{name}/descriptions'
URL = '/api/v1/connections/lite/descriptions'


class DescriptionTestCase(V1TestCase):
    def setUp(self):
        super().setUp()
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        conn.execute('CREATE TABLE customer (id INTEGER, email TEXT, tier TEXT)')
        conn.execute("INSERT INTO customer VALUES (1, 'ann@example.com', 'gold')")
        conn.commit()
        conn.close()
        masking.forget_schema()

    def describe(self, *items, status=200, headers=ADMIN):
        body = {'items': [dict(zip(('table', 'column', 'description'), item, strict=True)) for item in items]}
        return self.call('patch', URL, LIST, status, headers=headers, json=body)

    def key(self, **grants):
        res = self.client.post('/api/v1/api-keys', headers=ADMIN, json={'name': 'agent', **grants})
        self.assertEqual(res.status_code, 201, res.get_data(as_text=True))
        return {'X-API-Key': res.get_json()['secret']}


class WriteTests(DescriptionTestCase):
    def test_set_change_and_remove(self):
        items = self.describe(('customer', None, 'One row per paying customer'),
                              ('Customer', 'Email', 'Where invoices go'),
                              (None, None, 'Join orders to customer on customer_id.')).get_json()['items']
        self.assertEqual([(d['table'], d['column'], d['source']) for d in items],
                         [(None, None, 'written'), ('customer', None, 'written'), ('customer', 'email', 'written')])
        self.assertEqual(items[2]['updated_by'], 'admin')
        self.describe(('customer', 'email', ' Where invoices are sent '))
        self.describe(('customer', None, None), (None, None, ''))
        items = self.call('get', URL, LIST, 200).get_json()['items']
        self.assertEqual([(d['table'], d['column'], d['description']) for d in items],
                         [('customer', 'email', 'Where invoices are sent')])

    def test_every_change_is_audited_with_what_it_said(self):
        self.describe(('customer', 'email', 'Where invoices go'))
        self.describe(('customer', 'email', 'Billing address'))
        self.describe(('customer', 'email', 'Billing address'))  # no change: nothing recorded
        entries = [e for e in store.read_audit_log() if e['action'] == 'update_descriptions']
        self.assertEqual([e['changes']['descriptions'] for e in entries], [
            {'customer.email': {'from': None, 'to': 'Where invoices go'}},
            {'customer.email': {'from': 'Where invoices go', 'to': 'Billing address'}}])

    def test_bad_bodies_are_refused(self):
        for body in ({}, {'items': []}, {'items': 'x'}, {'items': [{'table': 't'}]},
                     {'items': [{'table': None, 'column': 'c', 'description': 'd'}]},
                     {'items': [{'table': 't', 'description': 'd', 'extra': 1}]},
                     {'items': [{'table': 't', 'description': 5}]},
                     {'items': [{'table': 't', 'description': 'x' * 2001}]},
                     {'items': [{'table': 'a' * 129, 'description': 'd'}]},
                     {'items': [{'table': 't', 'description': 'a'}, {'table': 'T', 'description': 'b'}]},
                     {'items': [], 'more': 1}):
            with self.subTest(body=body):
                res = self.client.patch(URL, headers=ADMIN, json=body)
                self.assertEqual((res.status_code, res.get_json()['code']), (400, 'invalid_body'))
        self.describe((None, None, 'n' * 20000))  # the connection's notes may be long

    def test_an_unknown_connection_is_a_404(self):
        res = self.client.get('/api/v1/connections/nope/descriptions', headers=ADMIN)
        self.assertEqual((res.status_code, res.get_json()['code']), (404, 'connection_not_found'))

    def test_who_may_read_and_write(self):
        for role, read, write in (('owner', 200, 200), ('admin', 200, 200), ('developer', 200, 403),
                                  ('auditor', 200, 403)):
            with self.subTest(role=role):
                admins.create_admin(f'{role}-1', role)
                token = {'X-API-Key': admins.issue_token(f'{role}-1')[1]}
                self.assertEqual(self.client.get(URL, headers=token).status_code, read)
                res = self.client.patch(URL, headers=token, json={'items': [{'table': 't', 'description': role}]})
                self.assertEqual(res.status_code, write)
        scoped = self.key(connections=['lite'])
        self.assertEqual(self.client.get(URL, headers=scoped).status_code, 403)  # API keys: not the Management API


class ImportTests(DescriptionTestCase):
    def setUp(self):
        super().setUp()
        self.lake = os.path.join(self.tmp.name, 'lake.duckdb')
        conn = duckdb.connect(self.lake)
        conn.execute('CREATE TABLE orders (id INTEGER, amt DECIMAL(10, 2), status VARCHAR)')
        conn.execute("COMMENT ON TABLE orders IS 'One row per order'")
        conn.execute("COMMENT ON COLUMN orders.amt IS 'Net revenue in EUR, refunds excluded'")
        conn.execute("COMMENT ON COLUMN orders.status IS 'open, paid or refunded'")
        conn.close()
        write_connections({'lite': {'db': 'sqlite', 'database': self.db_path, 'active': True},
                           'lake': {'db': 'duckdb', 'database': self.lake, 'active': True}})
        self.url = '/api/v1/connections/lake/descriptions'

    def run_import(self, status=200, **body):
        return self.call('post', self.url + '/import', LIST + '/import', status,
                         json=body if body else None).get_json()

    def test_comments_become_descriptions_and_a_written_one_is_kept(self):
        self.client.patch(self.url, headers=ADMIN, json={'items': [
            {'table': 'orders', 'column': 'status', 'description': 'Where the order is: open, paid, refunded'}]})
        result = self.run_import()
        self.assertEqual((result['found'], result['imported'], result['kept']), (3, 2, 1))
        found = {(d['table'], d['column']): (d['description'], d['source']) for d in result['items']}
        self.assertEqual(found, {('orders', None): ('One row per order', 'database'),
                                 ('orders', 'amt'): ('Net revenue in EUR, refunds excluded', 'database'),
                                 ('orders', 'status'): ('Where the order is: open, paid, refunded', 'written')})
        self.assertEqual(self.run_import()['imported'], 0)  # again: nothing new
        result = self.run_import(overwrite=True)
        self.assertEqual(result['imported'], 1)
        self.assertIn(('open, paid or refunded', 'database'),
                      [(d['description'], d['source']) for d in result['items']])
        imports = [e for e in store.read_audit_log() if e['action'] == 'import_descriptions']
        self.assertEqual([e['changes']['overwrite'] for e in imports], [False, True])  # the empty one left no entry

    def test_a_database_without_comments_says_so(self):
        res = self.client.post('/api/v1/connections/lite/descriptions/import', headers=ADMIN)
        self.assertEqual((res.status_code, res.get_json()['code']), (400, 'unsupported_operation'))
        self.assertIn('no comments', res.get_json()['error'])
        res = self.client.post(self.url + '/import', headers=ADMIN, json={'overwrite': 'yes'})
        self.assertEqual(res.status_code, 400)


class ServedTests(DescriptionTestCase):
    def setUp(self):
        super().setUp()
        self.describe(('customer', None, 'One row per paying customer'),
                      ('customer', 'email', 'Where invoices go'),
                      ('film', 'title', 'As released'),
                      (None, None, 'Every film has one title.'))

    def tables(self, result):
        return {t['name']: t for t in result['tables']}

    def test_the_v1_schema_carries_them(self):
        result = self.call('get', '/api/v1/connections/lite/schema', '/api/v1/connections/{name}/schema',
                           200).get_json()
        customer = self.tables(result)['customer']
        self.assertEqual(customer['description'], 'One row per paying customer')
        self.assertEqual({c['name']: c.get('description') for c in customer['columns']},
                         {'id': None, 'email': 'Where invoices go', 'tier': None})
        self.assertEqual(result['notes'], 'Every film has one title.')

    def test_a_key_sees_only_its_tables_and_what_it_gets_masked(self):
        headers = self.key(connections=['lite'], allowed_tables=['customer'],
                           mask=[{'column': 'customer.email', 'action': 'hash'}])
        result = self.client.get('/connections/lite/schema', headers=headers).get_json()
        self.assertEqual(list(self.tables(result)), ['customer'])  # film - and its description - not there
        email = next(c for c in self.tables(result)['customer']['columns'] if c['name'] == 'email')
        self.assertEqual((email['description'], email['masked']), ('Where invoices go', 'hash'))
        self.assertNotIn('masked', self.tables(result)['customer']['columns'][0])

    def test_mcp_list_tables_carries_them(self):
        permission = self.permission(mask=[{'column': '*.email', 'action': 'drop'}])
        result = mcp_server.list_tables_tool(permission, {'connection_name': 'lite'})['structuredContent']
        email = next(c for c in self.tables(result)['customer']['columns'] if c['name'] == 'email')
        self.assertEqual((email['description'], email['masked']), ('Where invoices go', 'drop'))

    def permission(self, **grants):
        from queryapigate import apikeys
        return apikeys.permission_from_grants('agent', {'connections': ['lite'], **grants})

    def test_the_data_dictionary_resource(self):
        permission = self.permission(allowed_tables=['customer'], mask=[{'column': 'customer.email', 'action': 'hash'}])
        listed = mcp_server.list_resources_for(permission)
        self.assertEqual([r['uri'] for r in listed], ['queryapigate://connections/lite/dictionary'])
        text = mcp_server.read_resource_for(permission, listed[0]['uri'])
        self.assertIn('# Data dictionary: lite', text)
        self.assertIn('Every film has one title.', text)
        self.assertIn('## customer\nOne row per paying customer', text)
        self.assertIn('- `email` (TEXT) - Where invoices go - masked for you (hash)', text)
        self.assertNotIn('## film', text)
        self.assertNotIn('As released', text)

    def test_the_dictionary_of_a_connection_the_caller_cant_use_doesnt_exist(self):
        nobody = self.permission(connections=['other'])
        self.assertEqual(mcp_server.list_resources_for(nobody), [])
        self.assertEqual(mcp_server.list_resources_for(self.permission(connections=[], queries=['q'])), [])
        for uri in ('queryapigate://connections/lite/dictionary', 'queryapigate://connections/nope/dictionary',
                    'file:///etc/passwd'):
            with self.subTest(uri=uri), self.assertRaises(ApiError) as caught:
                mcp_server.read_resource_for(nobody, uri)
            self.assertEqual(caught.exception.status, 404)

    def test_another_database_on_the_server_isnt_described(self):
        result = descriptions.annotate({'tables': [{'name': 'customer', 'columns': [{'name': 'email'}]}]}, 'other')
        self.assertNotIn('description', result['tables'][0])


class SavedQueryColumnTests(DescriptionTestCase):
    def test_a_saved_query_describes_its_result_columns_in_its_tool(self):
        query = self.create(sql='SELECT film_id, title FROM film', publish=True,
                            columns={'title': 'As released', 'film_id': ' '})
        self.assertEqual(query['versions'][0]['columns'], {'title': 'As released'})
        tool = next(t for t in mcp_server.list_tools_for(store_permission()) if t['name'] == 'films')
        self.assertEqual(tool['outputSchema']['properties']['rows']['items']['properties'],
                         {'title': {'description': 'As released'}})
        self.assertEqual(tool['outputSchema']['required'], ['rows', 'truncated'])
        for bad in ({'title': 5}, ['title'], {'': 'x'}):
            with self.subTest(columns=bad):
                res = self.client.post('/api/v1/queries/films/versions', headers=ADMIN,
                                       json={'description': 'v', 'sql': 'SELECT 1', 'columns': bad})
                self.assertEqual(res.status_code, 400)

    def test_without_descriptions_the_tool_schema_is_the_plain_one(self):
        self.create(publish=True)
        tool = next(t for t in mcp_server.list_tools_for(store_permission()) if t['name'] == 'films')
        self.assertIs(tool['outputSchema'], mcp_server.ROWS_OUTPUT_SCHEMA)

    def test_they_travel_in_a_collection_bundle(self):
        self.create(sql='SELECT title FROM film', publish=True, collection='films', columns={'title': 'As released'})
        from queryapigate import bundle
        exported = bundle.export_bundle('films')
        self.assertEqual(exported['queries'][0]['column_descriptions'], {'title': 'As released'})
        bundle.import_bundle(exported, on_conflict='new-version')
        imported = store.load_versions('films', with_history=False)
        self.assertEqual(imported['2']['column_descriptions'], {'title': 'As released'})


class RealClientTests(DescriptionTestCase):
    def test_a_real_mcp_client_lists_and_reads_the_dictionary(self):
        import asyncio
        import threading
        import time

        import uvicorn
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        self.describe(('customer', 'email', 'Where invoices go'))
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
                    listed = (await session.list_resources()).resources
                    content = (await session.read_resource(listed[0].uri)).contents[0]
                    return listed, content

        listed, content = asyncio.run(talk())
        self.assertEqual([str(r.uri) for r in listed], ['queryapigate://connections/lite/dictionary'])
        self.assertEqual(content.mimeType, 'text/markdown')
        self.assertIn('- `email` (TEXT) - Where invoices go', content.text)


def store_permission():
    from queryapigate import apikeys
    return apikeys.OPEN


class StoreTests(DescriptionTestCase):
    def test_the_table_is_copied_when_moving_to_postgres(self):
        self.assertIn('descriptions', [table for table, _, _ in db._MIGRATED_TABLES])

    def test_names_are_matched_case_insensitively(self):
        self.describe(('CUSTOMER', 'EMAIL', 'Where invoices go'))
        result = descriptions.annotate({'tables': [{'name': 'Customer', 'columns': [{'name': 'Email'}]}]}, 'lite')
        self.assertEqual(result['tables'][0]['columns'][0]['description'], 'Where invoices go')

    def test_masked_marks_follow_the_masking_rules(self):
        rules = [{'column': 'customer.*', 'action': 'hash'}, {'column': 'customer.email', 'action': 'drop'},
                 {'column': 'customer.id', 'action': 'keep'}]
        self.assertEqual(masking.action_for(rules, 'main', 'customer', 'email'), 'drop')
        self.assertEqual(masking.action_for(rules, 'main', 'customer', 'tier'), 'hash')
        self.assertIsNone(masking.action_for([{'column': 'customer.id', 'action': 'keep'}], None, 'customer', 'id'))
        self.assertEqual(masking.action_for([{'column': 'customer.email', 'action': 'hash'}], None, 'contacts',
                                            'email', view=True), 'refused')
        self.assertIsNone(masking.action_for([{'column': 'customer.email', 'action': 'hash'}], None, 'contacts',
                                             'email'))


if __name__ == '__main__':
    unittest.main()

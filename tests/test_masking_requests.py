"""Column masking on every way a result leaves (ADR 0005): /q/<name> and /execute_sql, every format, streamed exports,
MCP tools, the response cache and run history - for a caller with rules - and exports' own rules. Administrators,
and keys without rules, see data as it is."""
import io
import os
import re
import sqlite3
import tempfile
import unittest
from unittest import mock

import duckdb

from queryapigate import apikeys, create_app, destinations, exports, history, mcp_server
from tests.helpers import export_dir, save_query, write_connections

ADMIN = {'X-API-Key': 'admin-key'}
RULES = [{'column': 'customers.email', 'action': 'hash'}, {'column': '*.phone', 'action': 'mask'},
         {'column': 'customers.secret', 'action': 'drop'}]
HEX16 = re.compile(r'^[0-9a-f]{16}$')


class MaskingTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': self.tmp.name, 'QUERYAPIGATE_API_KEY': 'admin-key',
                                               'QUERYAPIGATE_HISTORY_FLUSH_INTERVAL': '0',
                                               'QUERYAPIGATE_MASK_KEY': 'test-key'})
        patcher.start()
        self.addCleanup(patcher.stop)
        data = os.path.join(self.tmp.name, 'shop.db')
        conn = sqlite3.connect(data)
        conn.execute('CREATE TABLE customers (id INTEGER, email TEXT, phone TEXT, secret TEXT, name TEXT)')
        conn.executemany('INSERT INTO customers VALUES (?, ?, ?, ?, ?)',
                         [(1, 'jane@example.com', '+44 7700 900123', 's1', 'Jane'),
                          (2, 'john@example.com', '+1 555 0100', 's2', 'John')])
        conn.commit()
        conn.close()
        write_connections({'shop': {'db': 'sqlite', 'database': data, 'active': True}})
        self.app = create_app()
        self.client = self.app.test_client()
        save_query(self.client, headers=ADMIN, body={
            'filename': 'customer_list', 'description': 'd', 'connection_name': 'shop', 'cache_ttl': 60,
            'sql_query': 'SELECT id, email AS contact, phone, secret, name FROM customers ORDER BY id'})
        self.partner_secret = apikeys.create_key('partner', connections=['shop'], mask=RULES)
        self.partner = {'X-API-Key': self.partner_secret}
        self.plain = {'X-API-Key': apikeys.create_key('internal', connections=['shop'])}

    def run_sql(self, sql, headers, **args):
        query = '&'.join(f'{k}={v}' for k, v in args.items())
        return self.client.post(f'/execute_sql?{query}', headers=headers, json={'sql': sql, 'connection_name': 'shop'})


class SavedAndAdhocTests(MaskingTestCase):
    def test_a_saved_query_masked_for_the_partner_and_not_for_others(self):
        rows = self.client.get('/q/customer_list', headers=self.partner).get_json()
        self.assertEqual(set(rows[0]), {'id', 'contact', 'phone', 'name'})  # secret dropped
        self.assertTrue(HEX16.match(rows[0]['contact']))
        self.assertNotEqual(rows[0]['contact'], rows[1]['contact'])
        self.assertEqual((rows[0]['phone'], rows[0]['name']), ('+**************', 'Jane'))
        for headers in (ADMIN, self.plain):
            with self.subTest(headers=headers):
                plain = self.client.get('/q/customer_list', headers=headers).get_json()
                self.assertEqual((plain[0]['contact'], plain[0]['secret']), ('jane@example.com', 's1'))

    def test_the_cache_never_crosses_callers_with_different_rules(self):
        self.client.get('/q/customer_list', headers=ADMIN)  # cached for the admin...
        masked = self.client.get('/q/customer_list', headers=self.partner)
        self.assertTrue(HEX16.match(masked.get_json()[0]['contact']))  # ... not served to the partner
        again = self.client.get('/q/customer_list', headers=self.partner)
        self.assertEqual(again.headers.get('X-Cache'), 'HIT')  # the partner's own entry
        self.assertEqual(again.get_json(), masked.get_json())
        self.assertEqual(self.client.get('/q/customer_list', headers=ADMIN).get_json()[0]['contact'],
                         'jane@example.com')

    def test_ad_hoc_sql_cannot_rename_its_way_out(self):
        for sql in ('SELECT email AS e FROM customers', 'SELECT lower(email) AS e FROM customers',
                    'WITH x AS (SELECT email AS e FROM customers) SELECT e FROM x',
                    "SELECT 'a' AS e UNION ALL SELECT email FROM customers"):
            with self.subTest(sql=sql):
                values = [row['e'] for row in self.run_sql(sql, self.partner).get_json()]
                self.assertTrue(all(HEX16.match(v) for v in values if v != 'a'), values)
                self.assertNotIn('jane@example.com', values)

    def test_what_cant_be_traced_is_refused_before_it_runs(self):
        for sql in ("SELECT * FROM customers AS c(a, b)", 'SELECT c FROM customers c', 'SELECT version() AS v'):
            with self.subTest(sql=sql):
                res = self.run_sql(sql, self.partner)
                self.assertEqual((res.status_code, res.get_json()['code']), (403, 'mask_unresolvable'))
        self.assertEqual(self.run_sql('SELECT c FROM customers c', self.plain).status_code, 500)  # sqlite: no such

    def test_an_error_never_quotes_a_value(self):
        duck = os.path.join(self.tmp.name, 'shop.duckdb')
        conn = duckdb.connect(duck)
        conn.execute("CREATE TABLE customers (id INTEGER, email VARCHAR, phone VARCHAR, secret VARCHAR, name VARCHAR)")
        conn.execute("INSERT INTO customers VALUES (1, 'jane@example.com', '+44', 's1', 'Jane')")
        conn.close()
        write_connections({'shop': {'db': 'duckdb', 'database': duck, 'active': True}})
        sql = 'SELECT CAST(email AS INTEGER) AS n FROM customers'
        plain = self.run_sql(sql, self.plain).get_json()
        self.assertIn('jane@example.com', plain['detail'])  # the database's own words, for a key without rules
        masked = self.run_sql(sql, self.partner).get_json()
        self.assertEqual(masked['code'], 'query_failed')
        self.assertNotIn('detail', masked)
        self.assertNotIn('jane@example.com', str(masked))

    def test_every_format_is_masked(self):
        csv = self.client.get('/q/customer_list?format=csv', headers=self.partner).get_data(as_text=True)
        self.assertNotIn('jane@example.com', csv)
        self.assertNotIn('s1', csv.split('\n', 1)[1])
        parquet = self.client.get('/q/customer_list?format=parquet', headers=self.partner).get_data()
        table = duckdb.sql('SELECT * FROM read_parquet($p)', params={'p': self._file(parquet)}).fetchall()
        self.assertTrue(HEX16.match(table[0][1]))
        for fmt in ('csv', 'ndjson', 'parquet'):
            with self.subTest(stream=fmt):
                body = self.client.get(f'/q/customer_list?stream=true&format={fmt}', headers=self.partner).get_data()
                self.assertNotIn(b'jane@example.com', body)
                self.assertNotIn(b'john@example.com', body)

    def _file(self, data):
        path = os.path.join(self.tmp.name, 'out.parquet')
        with open(path, 'wb') as f:
            f.write(data)
        return path

    def test_history_says_what_was_masked(self):
        self.client.get('/q/customer_list', headers=self.partner)
        self.run_sql('SELECT email FROM customers', self.partner)
        history.flush()
        entries, _ = history.search(key='partner')
        masked = [e.get('masked') for e in entries]
        self.assertIn([{'column': 'email', 'action': 'hash', 'source': 'customers.email'}], masked)
        self.assertIn({'column': 'contact', 'action': 'hash', 'source': 'customers.email'}, masked[-1])

    def test_the_catalog_tells_the_caller_what_it_gets_masked(self):
        save_query(self.client, headers=ADMIN, body={'filename': 'whole_rows', 'description': 'd',
                                                     'connection_name': 'shop',
                                                     'sql_query': 'SELECT c FROM customers c'})
        catalog = self.client.get('/catalog', headers=self.partner).get_json()
        by_name = {q['name']: q for q in catalog['queries']}
        self.assertEqual(by_name['customer_list']['masking'], {
            'masked': [{'column': 'contact', 'action': 'hash'}, {'column': 'phone', 'action': 'mask'},
                       {'column': 'secret', 'action': 'drop'}], 'refused': None})
        self.assertIn("can't be traced", by_name['whole_rows']['masking']['refused'])
        self.assertNotIn('customers', str(by_name['customer_list']['masking']))  # columns and actions, no sources
        self.assertEqual(catalog['caller']['mask'], RULES)
        admin = self.client.get('/catalog', headers=ADMIN).get_json()
        self.assertNotIn('masking', admin['queries'][0])
        self.assertIsNone(admin['caller']['mask'])

    def test_a_key_from_a_role_copies_its_rules(self):
        apikeys.create_role('partners', connections=['shop'], mask=RULES[:1])
        key = {'X-API-Key': apikeys.create_key('from-role', role='partners')}
        self.assertTrue(HEX16.match(self.run_sql('SELECT email FROM customers', key).get_json()[0]['email']))

    def test_mongo_is_refused_for_a_caller_with_rules(self):
        write_connections({'docs': {'db': 'mongo', 'host': '127.0.0.1', 'port': 1, 'database': 'd', 'active': True}})
        apikeys.update_key('partner', connections=['shop', 'docs'])
        res = self.client.post('/execute_mongo', headers=self.partner,
                               json={'collection': 'c', 'connection_name': 'docs'})
        self.assertEqual((res.status_code, res.get_json()['code']), (403, 'mask_unresolvable'))


class McpTests(MaskingTestCase):
    def test_mcp_tools_are_masked(self):
        permission = apikeys.authenticate(self.partner_secret)
        saved = mcp_server.call_tool_for(self.app, permission, 'customer_list', {})
        self.assertTrue(HEX16.match(saved['structuredContent']['rows'][0]['contact']))
        adhoc = mcp_server.dispatch_tool_call(self.app, permission, 'execute_sql',
                                         {'sql': 'SELECT email, phone FROM customers', 'connection_name': 'shop'})
        row = adhoc['structuredContent']['rows'][0]
        self.assertTrue(HEX16.match(row['email']))
        self.assertEqual(row['phone'], '+**************')


class ExportTests(MaskingTestCase):
    def setUp(self):
        super().setUp()
        self.out = os.path.join(export_dir(self), 'drop') + '/'
        destinations.create('drop', {'url': self.out})
        save_query(self.client, headers=ADMIN, body={
            'filename': 'customers_after', 'description': 'd', 'connection_name': 'shop',
            'sql_query': 'SELECT id, email, phone FROM customers WHERE id > :after ORDER BY id',
            'query_parameters': {'after': {'type': 'int'}}})

    def test_an_exports_own_rules_mask_the_file(self):
        exports.create('partner-feed', {'query': 'customers_after', 'destination': 'drop', 'path': 'feed.csv',
                                        'format': 'csv', 'mask': RULES[:2],
                                        'incremental': {'column': 'id', 'parameter': 'after', 'start': 0}})
        run = exports.run('partner-feed', 'admin')
        content = io.open(run['object']).read()
        self.assertNotIn('jane@example.com', content)
        self.assertIn('+***', content)
        self.assertEqual(run['watermark']['to'], 2)

    def test_the_incremental_column_cannot_be_masked(self):
        with self.assertRaises(exports.ApiError) as caught:
            exports.create('bad', {'query': 'customers_after', 'destination': 'drop', 'path': 'x.csv',
                                   'mask': [{'column': 'customers.id', 'action': 'hash'}],
                                   'incremental': {'column': 'id', 'parameter': 'after', 'start': 0}})
        self.assertIn("'id' can't be masked", caught.exception.message)

    def test_an_export_query_its_rules_cant_follow_is_refused_when_saved(self):
        save_query(self.client, headers=ADMIN, body={'filename': 'whole_rows', 'description': 'd',
                                                     'connection_name': 'shop',
                                                     'sql_query': 'SELECT c FROM customers c'})
        with self.assertRaises(exports.ApiError) as caught:
            exports.create('rows', {'query': 'whole_rows', 'destination': 'drop', 'path': 'x.csv',
                                    'mask': RULES[:1]})
        self.assertEqual(caught.exception.code, 'mask_unresolvable')


class QueryLevelMaskTests(MaskingTestCase):
    """A saved query's own mask rules (ADR 0005) apply to every caller - the admin included - and union with any the
    caller's key has, the strictest winning per column."""

    def setUp(self):
        super().setUp()
        save_query(self.client, headers=ADMIN, body={
            'filename': 'masked_customers', 'description': 'd', 'connection_name': 'shop', 'publish': True,
            'sql_query': 'SELECT id, email AS contact, phone, name FROM customers ORDER BY id',
            'mask': [{'column': 'customers.email', 'action': 'hash'}]})

    def test_query_mask_applies_to_everyone_including_admin(self):
        rows = self.client.get('/q/masked_customers', headers=ADMIN).get_json()
        self.assertTrue(HEX16.match(rows[0]['contact']))  # email hashed even for the admin key
        self.assertEqual(rows[0]['phone'], '+44 7700 900123')  # phone untouched - no rule for it

    def test_query_mask_unions_with_key_mask(self):
        secret = apikeys.create_key('p2', connections=['shop'], mask=[{'column': '*.phone', 'action': 'mask'}])
        rows = self.client.get('/q/masked_customers', headers={'X-API-Key': secret}).get_json()
        self.assertTrue(HEX16.match(rows[0]['contact']))  # email hashed by the query's rule
        self.assertNotEqual(rows[0]['phone'], '+44 7700 900123')  # phone masked by the key's rule - union

    def test_a_query_mask_rule_is_validated(self):
        res = save_query(self.client, headers=ADMIN, body={
            'filename': 'bad', 'description': 'd', 'connection_name': 'shop',
            'sql_query': 'SELECT id FROM customers', 'mask': [{'column': 'email', 'action': 'hash'}]})
        self.assertEqual(res.status_code, 400)  # column must be table.column

if __name__ == '__main__':
    unittest.main()

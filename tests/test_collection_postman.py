"""Tests for the Postman export of a collection (BACKLOG #35): a Postman Collection v2.1 file whose requests work as
generated - every example value is run back through the same parameter validation the server applies."""
import io
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from queryapigate import cli, create_app, postman, store
from queryapigate import params as param_rules
from queryapigate.errors import ApiError
from tests.helpers import write_connections

# Every kind of rule a parameter can carry, so no type or constraint escapes the "example must be valid" check.
PARAMETER_MATRIX = {
    'plain': 'int',
    'ratio': 'float',
    'flag': 'bool',
    'label': 'str',
    'bounded': {'type': 'int', 'min': 5, 'max': 9},
    'negative': {'type': 'int', 'min': -10, 'max': -3},
    'below_one': {'type': 'int', 'max': 0},
    'fraction': {'type': 'float', 'min': 0.5},
    'choice': {'type': 'str', 'enum': ['G', 'PG', 'R']},
    'numchoice': {'type': 'int', 'enum': [3, 7]},
    'defaulted': {'type': 'str', 'default': 'PG', 'description': 'The rating'},
    'long_enough': {'type': 'str', 'min_length': 4, 'max_length': 6},
    'optional_plain': {'type': 'int', 'required': False},
    'optional_bounded': {'type': 'int', 'required': False, 'min': 2},
    'coded': {'type': 'str', 'pattern': '[A-Z]{3}-[0-9]{4}'},
}


class PostmanTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = self.tmp.name
        db = os.path.join(self.home, 't.db')
        conn = sqlite3.connect(db)
        conn.execute('CREATE TABLE t (id INTEGER)')
        conn.commit()
        conn.close()
        patcher = mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': self.home, 'QUERYAPIGATE_API_KEY': 'admin-key'})
        patcher.start()
        self.addCleanup(patcher.stop)
        write_connections({'a': {'db': 'sqlite', 'database': db, 'active': True}})

    def save(self, name, collection='reporting', connection='a', params=None, sql=None, **extra):
        params = params or {}
        sql = sql or ('SELECT 1 WHERE ' + ' AND '.join(f':{p} IS NOT NULL' for p in params) if params else 'SELECT 1')
        fields = {'sql_query': sql, 'author': 'me', 'description': f'{name} desc', 'tags': [],
                  'query_parameters': params, **({'connection_name': connection} if connection else {}), **extra}
        store.save_version(name, fields, collection)

    def item(self, document, name):
        return next(i for i in document['item'] if i['name'] == name)

    @staticmethod
    def query_entries(item):
        return {q['key']: q for q in item['request']['url']['query']}


class StructureTests(PostmanTestCase):
    def test_a_valid_v2_1_collection(self):
        self.save('q1')
        self.save('q2')
        self.save('elsewhere', collection='ops')
        document = postman.build_collection('reporting', 'https://api.example.com/')
        self.assertEqual(document['info']['schema'], postman.SCHEMA)
        self.assertEqual(document['info']['name'], 'reporting')
        self.assertEqual([i['name'] for i in document['item']], ['q1', 'q2'])  # only this collection, sorted
        self.assertEqual({v['key']: v['value'] for v in document['variable']},
                         {'baseUrl': 'https://api.example.com', 'apiKey': ''})
        request = self.item(document, 'q1')['request']
        self.assertEqual(request['method'], 'GET')
        self.assertEqual(request['url']['host'], ['{{baseUrl}}'])
        self.assertEqual(request['url']['path'], ['q', 'q1'])
        self.assertIn('q1 desc', request['description'])

    def test_authentication_comes_from_a_variable_and_no_secret_is_present(self):
        self.save('q1')
        text = json.dumps(postman.build_collection('reporting'))
        auth = {a['key']: a['value'] for a in json.loads(text)['auth']['apikey']}
        self.assertEqual(auth, {'key': 'X-API-Key', 'value': '{{apiKey}}', 'in': 'header'})
        self.assertNotIn('admin-key', text)
        self.assertNotIn('sk_', text)

    def test_no_sql_or_connection_details_leak(self):
        self.save('q1', sql='SELECT secret_column FROM hidden_table')
        text = json.dumps(postman.build_collection('reporting'))
        self.assertNotIn('secret_column', text)
        self.assertNotIn('t.db', text)

    def test_paging_and_format_are_offered_but_off(self):
        self.save('q1')
        entries = self.query_entries(self.item(postman.build_collection('reporting'), 'q1'))
        for key in ('format', 'page', 'page_size'):
            self.assertTrue(entries[key]['disabled'], key)

    def test_a_query_with_no_default_connection_asks_for_one(self):
        self.save('q1', connection=None)
        entries = self.query_entries(self.item(postman.build_collection('reporting'), 'q1'))
        self.assertNotIn('disabled', entries['connection_name'])
        self.assertIn('no default connection', entries['connection_name']['description'])
        self.save('q2')
        self.assertNotIn('connection_name', self.query_entries(self.item(postman.build_collection('reporting'), 'q2')))

    def test_names_are_percent_encoded_in_the_path_and_raw_url(self):
        self.save('my query.v2')
        request = self.item(postman.build_collection('reporting'), 'my query.v2')['request']
        self.assertEqual(request['url']['path'], ['q', 'my%20query.v2'])
        self.assertTrue(request['url']['raw'].startswith('{{baseUrl}}/q/my%20query.v2'))

    def test_uses_the_latest_version(self):
        self.save('q1', params={'a': 'int'}, sql='SELECT :a')
        self.save('q1', params={'b': 'int'}, sql='SELECT :b', collection=store._UNSET)
        entries = self.query_entries(self.item(postman.build_collection('reporting'), 'q1'))
        self.assertIn('b', entries)
        self.assertNotIn('a', entries)

    def test_unknown_or_empty_and_invalid_collections(self):
        with self.assertRaises(ApiError) as caught:
            postman.build_collection('nope')
        self.assertEqual(caught.exception.status, 404)
        with self.assertRaises(ApiError):
            postman.build_collection('Bad Name')


class ParameterTests(PostmanTestCase):
    def setUp(self):
        super().setUp()
        self.save('q', params=PARAMETER_MATRIX)
        self.document = postman.build_collection('reporting')
        self.entries = self.query_entries(self.item(self.document, 'q'))

    def test_every_example_passes_the_servers_own_validation(self):
        # The drift guard: a rule the generator mishandles (a new type, a bound it forgets) fails here, instead
        # of shipping a request that answers 400 as soon as it is sent. resolve() takes the *raw* declarations
        # and normalises them itself - handing it pre-normalised ones silently disables every rule.
        expected_types = {'plain': int, 'ratio': float, 'flag': bool, 'label': str, 'bounded': int,
                          'negative': int, 'below_one': int, 'fraction': float, 'numchoice': int}
        for name, declaration in PARAMETER_MATRIX.items():
            if name == 'coded':
                continue  # a pattern cannot be synthesised - covered separately
            value = self.entries[name]['value']
            try:
                resolved = param_rules.resolve({name: declaration}, {name: value}, used={name})
            except ApiError as error:
                self.fail(f'{name}: example {value!r} rejected: {error.message}')
            if name in expected_types:  # proves the rules were really applied: the value came back converted
                self.assertIsInstance(resolved[name], expected_types[name], name)

    def test_the_validation_check_itself_is_not_vacuous(self):
        with self.assertRaises(ApiError):
            param_rules.resolve({'bounded': PARAMETER_MATRIX['bounded']}, {'bounded': '1'}, used={'bounded'})

    def test_required_parameters_are_on_and_optional_ones_off(self):
        for name in PARAMETER_MATRIX:
            required = param_rules.read_definitions(PARAMETER_MATRIX)[name]['required']
            self.assertEqual(bool(self.entries[name].get('disabled')), not required, name)

    def test_defaults_and_enums_and_bounds_are_honoured(self):
        values = {k: v['value'] for k, v in self.entries.items()}
        self.assertEqual(values['defaulted'], 'PG')
        self.assertEqual(values['choice'], 'G')
        self.assertEqual(values['numchoice'], '3')
        self.assertEqual(values['bounded'], '5')
        self.assertEqual(values['negative'], '-10')
        self.assertEqual(values['below_one'], '0')
        self.assertEqual(values['flag'], 'true')
        self.assertEqual(len(values['long_enough']), 4)

    def test_a_pattern_is_left_empty_and_flagged(self):
        entry = self.entries['coded']
        self.assertEqual(entry['value'], '')
        self.assertNotIn('disabled', entry)  # required, so it stays on - the user must fill it in
        self.assertIn('[A-Z]{3}-[0-9]{4}', entry['description'])
        self.assertIn('fill this in', entry['description'])

    def test_descriptions_carry_the_rules(self):
        self.assertIn('The rating', self.entries['defaulted']['description'])
        self.assertIn('one of G, PG, R', self.entries['choice']['description'])
        self.assertIn('min 5', self.entries['bounded']['description'])

    def test_the_raw_url_holds_only_the_enabled_parameters(self):
        raw = self.item(self.document, 'q')['request']['url']['raw']
        sent = parse_qs(urlsplit(raw.replace('{{baseUrl}}', 'http://x')).query, keep_blank_values=True)
        enabled = {k for k, v in self.entries.items() if not v.get('disabled')}
        self.assertEqual(set(sent), enabled)


class EndToEndTests(PostmanTestCase):
    def test_the_generated_urls_actually_run(self):
        self.save('by_id', params={'id': {'type': 'int', 'min': 1}, 'kind': {'type': 'str', 'enum': ['a', 'b']}},
                  sql='SELECT :id AS id, :kind AS kind')
        client = create_app().test_client()
        document = postman.build_collection('reporting')
        for item in document['item']:
            raw = item['request']['url']['raw'].replace('{{baseUrl}}', '')
            res = client.get(raw, headers={'X-API-Key': 'admin-key'})
            self.assertEqual(res.status_code, 200, f'{raw}: {res.get_data(as_text=True)}')
        self.assertEqual(res.get_json(), [{'id': 1, 'kind': 'a'}])


class EndpointTests(PostmanTestCase):
    def setUp(self):
        super().setUp()
        self.client = create_app().test_client()
        self.admin = {'X-API-Key': 'admin-key'}
        self.save('q1')

    def test_downloads_a_file_for_the_admin(self):
        res = self.client.get('/collections/reporting/postman', headers=self.admin)
        self.assertEqual(res.status_code, 200)
        self.assertIn('attachment; filename="reporting.postman_collection.json"', res.headers['Content-Disposition'])
        document = res.get_json()
        self.assertEqual(document['info']['schema'], postman.SCHEMA)
        self.assertEqual({v['key']: v['value'] for v in document['variable']}['baseUrl'], 'http://localhost')

    def test_admin_only(self):
        key = self.client.post('/api_keys', json={'name': 'k', 'connections': ['a']},
                               headers=self.admin).get_json()['key']
        self.assertEqual(self.client.get('/collections/reporting/postman', headers={'X-API-Key': key}).status_code,
                         403)
        self.assertEqual(self.client.get('/collections/reporting/postman').status_code, 401)

    def test_unknown_and_invalid(self):
        self.assertEqual(self.client.get('/collections/nope/postman', headers=self.admin).status_code, 404)
        self.assertEqual(self.client.get('/collections/Bad/postman', headers=self.admin).status_code, 400)


class CliTests(PostmanTestCase):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_postman_format_to_stdout_and_file(self):
        self.save('q1')
        code, out, _ = self.run_cli('collection', 'export', 'reporting', '--format', 'postman',
                                    '--base-url', 'https://api.example.com')
        self.assertEqual(code, 0)
        document = json.loads(out)
        self.assertEqual(document['info']['schema'], postman.SCHEMA)
        self.assertEqual({v['key']: v['value'] for v in document['variable']}['baseUrl'], 'https://api.example.com')
        target = os.path.join(self.home, 'out', 'r.postman.json')
        code, _, err = self.run_cli('collection', 'export', 'reporting', '--format', 'postman', '--out', target)
        self.assertEqual(code, 0, err)
        with open(target) as f:
            self.assertEqual(json.load(f)['info']['name'], 'reporting')

    def test_bundle_is_still_the_default(self):
        self.save('q1')
        _, out, _ = self.run_cli('collection', 'export', 'reporting')
        self.assertEqual(json.loads(out)['format'], 'queryapigate-collection')

    def test_failure_is_a_clean_message(self):
        code, _, err = self.run_cli('collection', 'export', 'nope', '--format', 'postman')
        self.assertEqual(code, 1)
        self.assertIn('queryapigate collection export:', err)


if __name__ == '__main__':
    unittest.main()

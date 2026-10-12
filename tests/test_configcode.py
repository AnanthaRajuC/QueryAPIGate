"""Configuration as code (ADR 0014, BACKLOG #97), slice 1: a file read and checked - every problem at once, each
naming the object and the field - the store written out as one with no secret in it, and a diff of the two that
names each field that changes. Nothing here writes the store."""
import hashlib
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

import yaml

from queryapigate import apikeys, cli, configcode, db, descriptions, destinations, exports, store
from queryapigate.services import connections

KEY_HASH = hashlib.sha256(b'sk_partner').hexdigest()


class ConfigTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = {'QUERYAPIGATE_HOME': self.tmp.name}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ('QUERYAPIGATE_SECRET_KEY', 'QUERYAPIGATE_AUDIT_LOG_EXPORT_FILE', 'QUERYAPIGATE_EXPORT_DIRS'):
            os.environ.pop(name, None)
        db.init_schema()

    def build(self):
        """A server with one of everything, its passwords ${VAR} references: two connections, a destination, a role and
        a key from it, a key of its own, queries in and out of a collection, descriptions, an export."""
        connections.create({'name': 'warehouse', 'db': 'postgres', 'host': 'db1', 'port': 5432, 'user': 'ro',
                            'password': '${WAREHOUSE_PASSWORD}', 'database': 'analytics', 'sslmode': 'require'}, 't')
        connections.create({'name': 'legacy', 'db': 'mysql', 'host': 'old', 'user': 'app',
                            'password': '${LEGACY_PASSWORD}'}, 't')
        destinations.create('bucket', {'url': 's3://drop/daily/', 'user': 'AKIA', 'password': '${BUCKET_SECRET}'})
        store.save_version('monthly_revenue', {
            'sql_query': 'SELECT month, revenue\nFROM revenue WHERE month >= :since', 'author': 'ana',
            'description': 'Revenue per month', 'tags': ['finance'], 'connection_name': 'warehouse',
            'query_parameters': {'since': {'type': 'string', 'pattern': '^[0-9-]+$'}}, 'cache_ttl': 300}, 'finance')
        store.save_version('loose', {'sql_query': 'SELECT 1', 'author': 'bo', 'description': 'd', 'tags': [],
                                     'query_parameters': {}, 'connection_name': 'legacy'})
        apikeys.create_role('partner', connections=[], collections=['finance'], rate_limit='60/minute',
                            mask=[{'column': 'customers.email', 'action': 'hash'}])
        apikeys.create_key('partner-a', role='partner')
        apikeys.create_key('ops', connections='*', allow_writes=True, expires_at='2030-01-01')
        apikeys.update_key('ops', active=False)
        descriptions.write('warehouse', [('revenue', '', 'Revenue per month'), ('revenue', 'month', 'YYYY-MM')], 't')
        exports.create('daily', {'query': 'monthly_revenue', 'destination': 'bucket', 'format': 'csv',
                                 'path': 'revenue/{date}.csv', 'params': {'since': '2026-01'}})

    def exported(self, **kwargs):
        doc, variables, skipped = configcode.export(**kwargs)
        return configcode.dump(doc, variables), doc, variables, skipped

    def diff(self, doc):
        return configcode.diff(configcode.load(yaml.safe_dump(doc)))

    def file(self, **sections):
        return {'format': configcode.FORMAT, 'format_version': 1, **sections}

    def problems(self, doc):
        with self.assertRaises(configcode.ConfigInvalid) as caught:
            configcode.diff(doc)
        return caught.exception.problems

    def cli(self, *argv, stdin=None):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), mock.patch('sys.stdin', io.StringIO(stdin or '')):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()


class ExportTests(ConfigTestCase):
    def test_the_store_as_a_file_applies_back_as_no_change(self):
        self.build()
        text, _, _, _ = self.exported()
        result = configcode.diff(configcode.load(text))
        self.assertEqual(result, {'changes': [], 'not_in_file': []})

    def stored_values(self):
        """Passwords the store holds as values: one encrypted, two as plain text."""
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_SECRET_KEY': 'Zm9vYmFyYmF6cXV4Zm9vYmFyYmF6cXV4Zm9vYmFyYmE='}):
            connections.create({'name': 'encrypted', 'db': 'mysql', 'host': 'h', 'password': 'kept-encrypted'}, 't')
        connections.create({'name': 'plain', 'db': 'mysql', 'host': 'h', 'password': 'a-literal-password'}, 't')
        destinations.create('drop', {'url': 's3://x/y/', 'user': 'AKIA', 'password': 'bucket-secret'})

    def test_a_mongo_query_round_trips_too(self):
        connections.create({'name': 'docs', 'db': 'mongo', 'host': 'm', 'database': 'app'}, 't')
        store.save_version('active_users', {'query_type': 'mongo', 'mongo_collection': 'users',
                                            'mongo_filter': {'active': True, 'org': ':org'},
                                            'mongo_projection': {'email': 1}, 'author': 'a', 'description': 'd',
                                            'tags': [], 'query_parameters': {'org': 'string'},
                                            'connection_name': 'docs'})
        text, doc, _, _ = self.exported()
        query = doc['queries'][0]
        self.assertEqual((query['query_type'], query['mongo_collection'], query['mongo_filter'], query['parameters']),
                         ('mongo', 'users', {'active': True, 'org': ':org'}, {'org': 'string'}))
        self.assertNotIn('sql', query)
        self.assertEqual(configcode.diff(configcode.load(text))['changes'], [])

    def test_no_secret_leaves_the_store(self):
        self.build()
        self.stored_values()
        text, doc, variables, _ = self.exported()
        for value in ('a-literal-password', 'bucket-secret', 'kept-encrypted', 'enc:', 'sk_'):
            self.assertNotIn(value, text)
        by_name = {c['name']: c for c in doc['connections']}
        self.assertEqual(by_name['warehouse']['password'], '${WAREHOUSE_PASSWORD}')  # a reference stays as written
        self.assertEqual(by_name['plain']['password'], '${QAG_PLAIN_PASSWORD}')
        self.assertEqual(next(d for d in doc['destinations'] if d['name'] == 'drop')['password'],
                         '${QAG_DESTINATION_DROP_PASSWORD}')
        self.assertEqual([(v['name'], v['stored']) for v in variables],
                         [('QAG_ENCRYPTED_PASSWORD', 'encrypted'), ('QAG_PLAIN_PASSWORD', 'as plain text'),
                          ('QAG_DESTINATION_DROP_PASSWORD', 'as plain text')])
        self.assertIn('#   QAG_PLAIN_PASSWORD: connection plain (stored as plain text)', text)

    def test_a_stored_value_applied_back_moves_out_of_the_store(self):
        """The one difference an exported file makes: a password the store held as a value is read from the
        server's environment instead - the diff says exactly that, and which variable to set."""
        self.stored_values()
        text, _, _, _ = self.exported()
        result = configcode.diff(configcode.load(text))
        self.assertEqual([(c['kind'], c['name'], c['fields']) for c in result['changes']], [
            ('connections', 'encrypted', {'password': {'from': configcode.SECRET, 'to': '${QAG_ENCRYPTED_PASSWORD}'}}),
            ('connections', 'plain', {'password': {'from': configcode.SECRET, 'to': '${QAG_PLAIN_PASSWORD}'}}),
            ('destinations', 'drop', {'password': {'from': configcode.SECRET,
                                                   'to': '${QAG_DESTINATION_DROP_PASSWORD}'}})])
        self.assertEqual(result['changes'][1]['notes'],
                         ['the password moves out of the store: set QAG_PLAIN_PASSWORD where the server runs first'])

    def test_what_each_kind_looks_like(self):
        self.build()
        _, doc, _, _ = self.exported()
        query = next(q for q in doc['queries'] if q['name'] == 'monthly_revenue')
        self.assertEqual(query, {
            'name': 'monthly_revenue', 'collection': 'finance', 'connection_name': 'warehouse',
            'description': 'Revenue per month', 'author': 'ana', 'tags': ['finance'],
            'sql': 'SELECT month, revenue\nFROM revenue WHERE month >= :since',
            'parameters': {'since': {'type': 'string', 'pattern': '^[0-9-]+$'}}, 'cache_ttl': 300})
        keys = {k['name']: k for k in doc['api_keys']}
        self.assertEqual(keys['partner-a'], {'name': 'partner-a', 'role': 'partner',
                                             'secret_sha256': apikeys.secret_hashes()['partner-a']})
        self.assertEqual(keys['ops'], {'name': 'ops', 'connections': '*', 'allow_writes': True, 'active': False,
                                       'expires_at': '2030-01-01', 'secret_sha256': apikeys.secret_hashes()['ops']})
        self.assertEqual(doc['roles'], [{'name': 'partner', 'connections': [], 'collections': ['finance'],
                                         'rate_limit': '60/minute',
                                         'mask': [{'column': 'customers.email', 'action': 'hash'}]}])
        self.assertEqual(doc['descriptions'], {'warehouse': [{'table': 'revenue', 'description': 'Revenue per month'},
                                                             {'table': 'revenue', 'column': 'month',
                                                              'description': 'YYYY-MM'}]})
        self.assertEqual(doc['exports'][0]['path'], 'revenue/{date}.csv')
        warehouse = next(c for c in doc['connections'] if c['name'] == 'warehouse')
        self.assertEqual(warehouse['sslmode'], 'require')  # a driver option, flat as the API takes it

    def test_sql_over_several_lines_is_a_block(self):
        self.build()
        text, _, _, _ = self.exported()
        self.assertIn('  sql: |-\n    SELECT month, revenue\n    FROM revenue WHERE month >= :since\n', text)

    def test_a_key_whose_grants_left_its_role_is_written_out_in_full(self):
        self.build()
        apikeys.update_key('partner-a', rate_limit='5/minute')
        _, doc, _, _ = self.exported()
        key = next(k for k in doc['api_keys'] if k['name'] == 'partner-a')
        self.assertNotIn('role', key)
        self.assertEqual(key['rate_limit'], '5/minute')

    def test_drafts_only_queries_are_left_out_and_named(self):
        store.save_version('draft_only', {'sql_query': 'SELECT 1', 'author': 'a', 'description': 'd', 'tags': [],
                                          'query_parameters': {}}, publish=False)
        _, doc, _, skipped = self.exported()
        self.assertEqual((doc['queries'], skipped), ([], ['draft_only']))

    def test_the_examples_only_when_asked(self):
        from queryapigate import examples
        examples.load()
        _, doc, _, _ = self.exported()
        self.assertEqual((doc['queries'], doc['api_keys'], doc['roles'], doc['connections']), ([], [], [], []))
        _, doc, _, _ = self.exported(examples=True)
        self.assertTrue(doc['queries'] and doc['api_keys'] and doc['roles'] and doc['connections'])

    def test_only_the_kinds_asked_for(self):
        self.build()
        _, doc, _, _ = self.exported(kinds=('roles',))
        self.assertEqual(set(doc), {'format', 'format_version', 'roles'})


class DiffTests(ConfigTestCase):
    def setUp(self):
        super().setUp()
        self.build()
        _, self.doc, _, _ = self.exported()

    def edit(self, kind, name, **fields):
        entry = next(e for e in self.doc[kind] if e['name'] == name)
        for field, value in fields.items():
            if value is None:
                entry.pop(field, None)
            else:
                entry[field] = value
        return self.doc

    def change(self, result, kind, name):
        return next(c for c in result['changes'] if (c['kind'], c['name']) == (kind, name))

    def test_a_changed_definition_is_a_new_published_version_field_by_field(self):
        result = self.diff(self.edit('queries', 'monthly_revenue', sql='SELECT :since', cache_ttl=60))
        change = self.change(result, 'queries', 'monthly_revenue')
        self.assertEqual(change['action'], 'update')
        self.assertEqual(change['notes'], ['a new version, published'])
        self.assertEqual(change['fields']['cache_ttl'], {'from': 300, 'to': 60})
        self.assertEqual(change['fields']['sql']['to'], 'SELECT :since')
        self.assertEqual(len(result['changes']), 1)

    def test_only_the_collection_is_a_move(self):
        change = self.change(self.diff(self.edit('queries', 'monthly_revenue', collection='sales')), 'queries',
                             'monthly_revenue')
        self.assertEqual((change['notes'], change['fields']),
                         (['moved to another collection'], {'collection': {'from': 'finance', 'to': 'sales'}}))

    def test_a_query_without_a_collection_in_the_file_leaves_it(self):
        change = self.change(self.diff(self.edit('queries', 'monthly_revenue', collection=None)), 'queries',
                             'monthly_revenue')
        self.assertEqual(change['fields'], {'collection': {'from': 'finance', 'to': None}})

    def test_the_author_is_compared_only_when_the_file_gives_one(self):
        self.assertEqual(self.diff(self.edit('queries', 'monthly_revenue', author=None))['changes'], [])
        change = self.change(self.diff(self.edit('queries', 'monthly_revenue', author='cy')), 'queries',
                             'monthly_revenue')
        self.assertEqual(change['fields'], {'author': {'from': 'ana', 'to': 'cy'}})

    def test_new_objects_are_creates(self):
        self.doc['queries'].append({'name': 'top', 'connection_name': 'warehouse', 'description': 'Top',
                                    'sql': 'SELECT 1'})
        self.doc['api_keys'].append({'name': 'partner-b', 'role': 'partner', 'secret_sha256': KEY_HASH})
        result = self.diff(self.doc)
        self.assertEqual([(c['kind'], c['name'], c['action']) for c in result['changes']],
                         [('queries', 'top', 'create'), ('api_keys', 'partner-b', 'create')])
        key = self.change(result, 'api_keys', 'partner-b')
        self.assertEqual(key['fields']['collections'], {'from': None, 'to': ['finance']})  # the role's grants
        self.assertEqual(key['fields']['secret'], {'from': None, 'to': configcode.CHANGED})

    def test_a_role_changed_in_the_file_changes_its_keys(self):
        self.edit('roles', 'partner', rate_limit='10/minute')
        result = self.diff(self.doc)
        self.assertEqual([(c['kind'], c['name']) for c in result['changes']],
                         [('roles', 'partner'), ('api_keys', 'partner-a')])
        self.assertEqual(self.change(result, 'api_keys', 'partner-a')['fields'],
                         {'rate_limit': {'from': '60/minute', 'to': '10/minute'}})

    def test_a_new_secret_hash_shows_as_changed_never_as_a_value(self):
        change = self.change(self.diff(self.edit('api_keys', 'partner-a', secret_sha256=KEY_HASH)), 'api_keys',
                             'partner-a')
        self.assertEqual(change['fields'], {'secret': {'from': configcode.CHANGED, 'to': configcode.CHANGED}})
        self.assertIn('the old one keeps working for 60 minutes', change['notes'][0])
        self.assertNotIn(KEY_HASH, configcode.render(self.diff(self.doc)))

    def test_a_reference_is_compared_and_shown_as_written(self):
        change = self.change(self.diff(self.edit('connections', 'legacy', password='${LEGACY_PW}', host='new')),
                             'connections', 'legacy')
        self.assertEqual(change['fields'], {'host': {'from': 'old', 'to': 'new'},
                                            'password': {'from': '${LEGACY_PASSWORD}', 'to': '${LEGACY_PW}'}})
        change = self.change(self.diff(self.edit('connections', 'legacy', password=None)), 'connections', 'legacy')
        self.assertEqual(change['fields']['password'], {'from': '${LEGACY_PASSWORD}', 'to': None})

    def test_descriptions_by_item(self):
        self.doc['descriptions']['warehouse'][1]['description'] = 'The month, YYYY-MM'
        self.doc['descriptions']['warehouse'].append({'table': 'orders', 'description': 'One row per order'})
        change = self.change(self.diff(self.doc), 'descriptions', 'warehouse')
        self.assertEqual(change['fields'], {'orders': {'from': None, 'to': 'One row per order'},
                                            'revenue.month': {'from': 'YYYY-MM', 'to': 'The month, YYYY-MM'}})

    def test_what_the_server_has_and_the_file_doesnt_is_listed_and_left_alone(self):
        self.doc['queries'] = [q for q in self.doc['queries'] if q['name'] != 'loose']
        result = self.diff(self.doc)
        self.assertEqual((result['changes'], result['not_in_file']), ([], [{'kind': 'queries', 'name': 'loose'}]))
        self.assertIn('applying leaves them alone:\n    queries[loose]', configcode.render(result))

    def test_a_kind_left_out_of_the_file_is_left_out_of_the_diff(self):
        result = self.diff(self.file(roles=self.doc['roles']))
        self.assertEqual(result, {'changes': [], 'not_in_file': []})

    def test_a_query_with_a_newer_draft_says_so(self):
        store.save_version('loose', {'sql_query': 'SELECT 3', 'author': 'bo', 'description': 'd', 'tags': [],
                                     'query_parameters': {}, 'connection_name': 'legacy'}, publish=False)
        change = self.change(self.diff(self.edit('queries', 'loose', sql='SELECT 4')), 'queries', 'loose')
        self.assertIn('has a newer draft, version 2', change['notes'][1])

    def test_render_shows_long_text_as_a_line_diff(self):
        text = configcode.render(self.diff(self.edit(
            'queries', 'monthly_revenue', sql='SELECT month, revenue\nFROM revenue WHERE month > :since')))
        self.assertIn('~ queries[monthly_revenue]\n    (a new version, published)\n    sql:\n', text)
        self.assertIn('      -FROM revenue WHERE month >= :since\n      +FROM revenue WHERE month > :since', text)
        self.assertTrue(text.endswith('0 to create, 1 to change.'))

    def test_a_diff_writes_nothing(self):
        before = db.connection().execute('SELECT COUNT(*) FROM audit_log').fetchone()[0]
        self.doc['queries'].append({'name': 'top', 'connection_name': 'warehouse', 'description': 'T',
                                    'sql': 'SELECT 1'})
        self.diff(self.edit('roles', 'partner', rate_limit='1/minute'))
        self.assertEqual(db.connection().execute('SELECT COUNT(*) FROM audit_log').fetchone()[0], before)
        self.assertFalse(store.saved_query_exists('top'))
        self.assertEqual(apikeys.list_roles()['partner']['rate_limit'], '60/minute')


class RefusalTests(ConfigTestCase):
    def setUp(self):
        super().setUp()
        connections.create({'name': 'warehouse', 'db': 'postgres', 'host': 'h'}, 't')

    def test_a_file_that_isnt_one(self):
        for text, words in (('just: [text', 'Not valid YAML or JSON'), ('{"format": "other"}', 'expected format'),
                            ('format: queryapigate-config\nformat_version: 2', 'Unsupported format_version 2'),
                            ('format: queryapigate-config\nformat_version: 1\nusers: []', 'Unknown section(s): users')):
            with self.assertRaises(configcode.ConfigInvalid) as caught:
                configcode.load(text)
            self.assertIn(words, caught.exception.problems[0])

    def test_every_problem_at_once_each_naming_the_object_and_field(self):
        problems = self.problems(self.file(
            connections=[{'name': 'pw', 'db': 'mysql', 'password': 'hunter2'},
                         {'name': 'server', 'db': 'mysql', 'password': '${QUERYAPIGATE_API_KEY}'},
                         {'name': 'nodb'}],
            destinations=[{'name': 'b', 'url': 's3://x/', 'password': 'literal'}],
            roles=[{'name': 'r', 'rate_limit': 'often'}, {'name': 'r'}],
            queries=[{'name': 'q', 'sql': 'SELECT :a', 'description': 'd', 'parameters': {'b': 'int'}},
                     {'name': 'unknown', 'sql': 'SELECT 1', 'description': 'd', 'publish': True}],
            api_keys=[{'name': 'k', 'role': 'r', 'connections': '*', 'secret_sha256': 'not-a-hash'}],
            exports=[{'name': 'e', 'query': 'q'}]))
        expected = (
            ('connections[pw]', 'password must be a ${VAR} reference'),
            ('connections[server]', "one of this server's own settings"),
            ('connections[nodb]', 'db is required'),
            ('destinations[b]', 'password must be a ${VAR} reference'),
            ('roles[r]', 'is listed twice'),
            ('roles[r]', 'rate_limit'),
            ('queries[q]', 'query_parameters declares b'),
            ('queries[unknown]', 'unknown field(s): publish'),
            ('api_keys[k]', 'secret_sha256 must be the SHA-256'),
            ('api_keys[k]', 'takes its grants from it - leave out connections'),
            ('exports[e]', 'destination is required'))
        for where, words in expected:
            self.assertTrue(any(p.startswith(where + ':') and words in p for p in problems), (where, words, problems))

    def test_references_must_exist_in_the_file_or_on_the_server(self):
        problems = self.problems(self.file(
            queries=[{'name': 'q', 'sql': 'SELECT 1', 'description': 'd', 'connection_name': 'nowhere'}],
            roles=[{'name': 'r', 'collections': ['nothing']}],
            api_keys=[{'name': 'k', 'role': 'ghost', 'secret_sha256': KEY_HASH}],
            descriptions={'nowhere': [{'table': 't', 'description': 'd'}]},
            exports=[{'name': 'e', 'query': 'q2', 'destination': 'd', 'path': 'x.csv'}]))
        self.assertEqual(problems, [
            "queries[q]: connection_name 'nowhere' is no connection, in the file or on the server",
            "roles[r]: collection 'nothing' has no queries, in the file or on the server",
            "api_keys[k]: role 'ghost' is no role, in the file or on the server",
            'descriptions[nowhere]: is no connection, in the file or on the server',
            "exports[e]: query 'q2' is no saved query, in the file or on the server",
            "exports[e]: destination 'd' is no destination, in the file or on the server"])

    def test_what_the_file_creates_counts_as_existing(self):
        result = configcode.diff(self.file(
            connections=[{'name': 'new', 'db': 'sqlite', 'database': 'x.db'}],
            queries=[{'name': 'q', 'sql': 'SELECT 1', 'description': 'd', 'connection_name': 'new',
                      'collection': 'fresh'}],
            roles=[{'name': 'r', 'collections': ['fresh']}],
            api_keys=[{'name': 'k', 'role': 'r', 'secret_sha256': KEY_HASH}]))
        self.assertEqual(len(result['changes']), 4)

    def test_a_new_key_needs_its_hash(self):
        problems = self.problems(self.file(api_keys=[{'name': 'k', 'connections': []}]))
        self.assertEqual(problems, ['api_keys[k]: a new key needs secret_sha256 - `queryapigate config new-key` makes '
                                    'a secret and prints its hash'])

    def test_an_existing_key_keeps_its_secret_without_one(self):
        apikeys.create_key('k', connections=[])
        result = configcode.diff(self.file(api_keys=[{'name': 'k', 'connections': []}]))
        self.assertEqual(result['changes'], [])


class DocumentationTests(ConfigTestCase):
    def test_the_documented_examples_are_valid_files(self):
        import re
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for path in ('documentation/API.md', 'documentation/adr/0014-configuration-as-code.md'):
            with open(os.path.join(root, path)) as f:
                blocks = [b for b in re.findall(r'(?:~~~|```)yaml\n(.*?)(?:~~~|```)', f.read(), re.S)
                          if 'queryapigate-config' in b]
            with self.subTest(path=path):
                self.assertEqual(len(blocks), 1)
                # complete on its own - every reference is to something in it - and all of it new here
                result = configcode.diff(configcode.load(blocks[0]))
                self.assertEqual({c['action'] for c in result['changes']}, {'create'})


class CliTests(ConfigTestCase):
    def test_exit_codes_tell_no_change_changes_and_problems_apart(self):
        self.build()
        path = os.path.join(self.tmp.name, 'queryapigate.yaml')
        code, _, err = self.cli('config', 'export', '--out', path)
        self.assertEqual(code, 0)
        self.assertEqual(err, f'Wrote the configuration to {path}\n')
        self.assertEqual(self.cli('config', 'diff', path)[:2], (0, 'No changes: the server matches the file.\n'))
        with open(path) as f:
            text = f.read().replace('rate_limit: 60/minute', 'rate_limit: 10/minute')
        code, out, _ = self.cli('config', 'diff', '-', stdin=text)
        self.assertEqual(code, 2)
        self.assertIn('~ roles[partner]\n    rate_limit: "60/minute" -> "10/minute"', out)
        code, out, _ = self.cli('config', 'diff', '--json', '-', stdin=text)
        self.assertEqual([c['name'] for c in json.loads(out)['changes']], ['partner', 'partner-a'])
        code, _, err = self.cli('config', 'diff', '-', stdin=text.replace('${LEGACY_PASSWORD}', 'plain'))
        self.assertEqual(code, 1)
        self.assertIn('1 problem(s) in the file:\n  connections[legacy]: password must be a ${VAR} reference', err)

    def test_new_key_prints_a_secret_and_its_hash(self):
        code, out, _ = self.cli('config', 'new-key')
        secret = out.split('secret:')[1].split()[0]
        self.assertTrue(secret.startswith('sk_'))
        self.assertIn(f'secret_sha256: {hashlib.sha256(secret.encode()).hexdigest()}', out)
        self.assertEqual(apikeys.list_keys(), {})  # nothing stored


if __name__ == '__main__':
    unittest.main()

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


# --------------------------------------------------------------------------------------
# Slice 2: transactions that nest, and apply
# --------------------------------------------------------------------------------------

class TransactionTests(ConfigTestCase):
    def count(self):
        return db.connection().execute('SELECT COUNT(*) FROM config_managed').fetchone()[0]

    def insert(self, conn, name):
        conn.execute("INSERT INTO config_managed (kind, name, applied_at, file_hash) VALUES ('t', ?, 'now', 'h')",
                     (name,))

    def test_a_nested_block_that_fails_rolls_back_alone(self):
        with db.transaction() as conn:
            self.insert(conn, 'kept')
            with self.assertRaises(RuntimeError):
                with db.transaction() as inner:
                    self.insert(inner, 'undone')
                    raise RuntimeError
            self.insert(conn, 'also kept')
        names = [r[0] for r in db.connection().execute('SELECT name FROM config_managed ORDER BY name')]
        self.assertEqual(names, ['also kept', 'kept'])

    def test_a_nested_block_commits_only_with_the_outer_one(self):
        with self.assertRaises(RuntimeError):
            with db.transaction() as conn:
                with db.transaction() as inner:
                    self.insert(inner, 'nested')
                self.insert(conn, 'outer')
                raise RuntimeError
        self.assertEqual(self.count(), 0)
        self.assertFalse(db.in_transaction())

    def test_follow_ups_run_after_the_commit_and_never_after_a_rollback(self):
        ran = []
        with db.transaction():
            db.after_commit(lambda: ran.append('outer'))
            with self.assertRaises(RuntimeError):
                with db.transaction():
                    db.after_commit(lambda: ran.append('rolled back'))
                    raise RuntimeError
            self.assertEqual(ran, [])
        self.assertEqual(ran, ['outer'])
        with self.assertRaises(RuntimeError):
            with db.transaction():
                db.after_commit(lambda: ran.append('never'))
                raise RuntimeError
        db.after_commit(lambda: ran.append('now'))  # outside one: at once
        self.assertEqual(ran, ['outer', 'now'])

    def test_an_audit_entry_rolled_back_never_reaches_the_export_file(self):
        path = os.path.join(self.tmp.name, 'audit.jsonl')
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_AUDIT_LOG_EXPORT_FILE': path}):
            with self.assertRaises(RuntimeError):
                with db.transaction():
                    store.record_audit('t', 'rolled_back', 'x')
                    raise RuntimeError
            store.record_audit('t', 'kept', 'x')
            store.record_audit('t', 'kept too', 'x')
        with open(path) as f:
            lines = [json.loads(line) for line in f]
        self.assertEqual([e['action'] for e in lines], ['kept', 'kept too'])
        from queryapigate import auditchain
        # the rolled-back entry's seq went to the next one: the file and the store each hold one unbroken chain
        self.assertTrue(auditchain.verify_file(path)['intact'])
        self.assertTrue(auditchain.verify(db.connection())['intact'])


class ApplyTests(ConfigTestCase):
    def audit(self):
        rows = db.connection().execute('SELECT entry_json FROM audit_log ORDER BY id').fetchall()
        return [json.loads(r[0]) for r in rows]

    def promote(self):
        """dev's setup exported, then applied to an empty prod - the point of it all."""
        self.build()
        doc, _, _ = configcode.export()
        hashes = apikeys.secret_hashes()
        db.close()
        prod = tempfile.TemporaryDirectory()
        self.addCleanup(prod.cleanup)
        os.environ['QUERYAPIGATE_HOME'] = prod.name
        db.init_schema()
        return doc, hashes

    def test_a_file_applied_to_an_empty_server_makes_it_match(self):
        doc, hashes = self.promote()
        result = configcode.apply(doc)
        self.assertTrue(result['applied'])
        self.assertEqual(sorted({c['action'] for c in result['changes']}), ['create'])
        self.assertEqual(configcode.diff(doc), {'changes': [], 'not_in_file': []})
        self.assertEqual(apikeys.secret_hashes(), hashes)  # the same secrets work on prod
        self.assertEqual(store.read_published(store.load_versions('monthly_revenue')), 1)
        self.assertEqual(exports.get('daily')['destination'], 'bucket')
        self.assertFalse(apikeys.list_keys()['ops']['active'])

    def test_every_change_is_audited_via_config_under_one_entry(self):
        doc, _ = self.promote()
        result = configcode.apply(doc, actor='alice')
        entries = self.audit()
        top = entries[-1]
        self.assertEqual((top['action'], top['actor'], top['changes']['file_sha256']),
                         ('config_applied', 'alice', result['file_sha256']))
        self.assertEqual(top['changes']['created'], len(result['changes']))
        self.assertEqual({e['via'] for e in entries[:-1]}, {'config'})
        self.assertIn('create_connection', [e['action'] for e in entries])

    def test_applying_it_again_changes_and_records_nothing(self):
        doc, _ = self.promote()
        configcode.apply(doc)
        before = len(self.audit())
        result = configcode.apply(doc)
        self.assertEqual((result['applied'], result['changes'], len(self.audit())), (False, [], before))

    def test_a_problem_at_any_step_changes_nothing(self):
        doc, _ = self.promote()
        # passes the file's own checks, fails against the store as it will be: the export's path needs a value the
        # query doesn't take
        doc['exports'][0]['path'] = 'revenue/{param:nowhere}.csv'
        path = os.path.join(self.tmp.name, 'audit.jsonl')
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_AUDIT_LOG_EXPORT_FILE': path}):
            with self.assertRaises(configcode.ConfigInvalid) as caught:
                configcode.apply(doc)
        self.assertTrue(caught.exception.problems[0].startswith('exports[daily]: '), caught.exception.problems)
        self.assertEqual((store.read_connections(), apikeys.list_keys(), self.audit()), ({}, {}, []))
        self.assertFalse(os.path.exists(path))
        self.assertEqual(db.connection().execute('SELECT COUNT(*) FROM config_managed').fetchone()[0], 0)

    def test_changes_go_through_the_apis_own_paths(self):
        self.build()
        doc, _, _ = configcode.export()
        query = next(q for q in doc['queries'] if q['name'] == 'monthly_revenue')
        query['sql'] = 'SELECT 2 AS n WHERE :since IS NOT NULL'
        next(q for q in doc['queries'] if q['name'] == 'loose')['collection'] = 'finance'
        next(c for c in doc['connections'] if c['name'] == 'legacy')['host'] = 'new-host'
        doc['descriptions']['warehouse'][0]['description'] = 'Monthly revenue'
        configcode.apply(doc)
        content = store.load_versions('monthly_revenue')
        self.assertEqual(store.read_published(content), 2)
        self.assertEqual(store.read_collection(store.load_versions('loose')), 'finance')
        self.assertEqual(store.read_connections()['legacy']['host'], 'new-host')
        self.assertEqual(descriptions.list_for('warehouse')[0]['description'], 'Monthly revenue')
        actions = [e['action'] for e in self.audit() if e['via'] == 'config']
        self.assertEqual(sorted(actions), ['move_query', 'save_query', 'update_connection', 'update_descriptions'])

    def test_a_new_secret_hash_rotates_the_key_with_the_usual_grace(self):
        self.build()
        apikeys.set_secret_hash('partner-a', hashlib.sha256(b'sk_old').hexdigest(), grace_minutes=0)
        doc, _, _ = configcode.export()
        next(k for k in doc['api_keys'] if k['name'] == 'partner-a')['secret_sha256'] = KEY_HASH
        configcode.apply(doc)
        self.assertEqual(apikeys.authenticate('sk_partner').name, 'partner-a')
        self.assertEqual(apikeys.authenticate('sk_old').name, 'partner-a')  # still, for its grace period
        entry = next(e for e in self.audit() if e['action'] == 'set_key_secret')
        self.assertEqual(entry['changes']['grace_minutes'], apikeys.DEFAULT_GRACE_MINUTES)

    def test_a_new_keys_random_secret_never_works(self):
        apikeys_before = set(apikeys.list_keys())
        with mock.patch('secrets.token_urlsafe', return_value='predictable'):
            configcode.apply(self.file(api_keys=[{'name': 'k', 'connections': [], 'secret_sha256': KEY_HASH}]))
        self.assertEqual(set(apikeys.list_keys()) - apikeys_before, {'k'})
        self.assertIsNone(apikeys.authenticate('sk_predictable'))
        self.assertEqual(apikeys.authenticate('sk_partner').name, 'k')

    def test_one_secret_names_one_key(self):
        apikeys.create_key('first', connections=[])
        apikeys.set_secret_hash('first', KEY_HASH, grace_minutes=0)
        with self.assertRaises(configcode.ConfigInvalid) as caught:
            configcode.apply(self.file(api_keys=[{'name': 'second', 'connections': [], 'secret_sha256': KEY_HASH}]))
        self.assertIn('another key already has this secret_sha256', caught.exception.problems[0].lower())
        self.assertNotIn('second', apikeys.list_keys())


class PruneTests(ConfigTestCase):
    def setUp(self):
        super().setUp()
        self.build()
        self.doc, _, _ = configcode.export(kinds=('queries', 'descriptions'))
        configcode.apply(self.doc)  # the file now manages these queries and descriptions

    def test_only_what_a_file_manages_is_deleted(self):
        store.save_version('made_in_the_console', {'sql_query': 'SELECT 1', 'author': 'a', 'description': 'd',
                                                   'tags': [], 'query_parameters': {}})
        self.doc['queries'] = [q for q in self.doc['queries'] if q['name'] != 'loose']
        result = configcode.diff(self.doc, prune=True)
        self.assertEqual([(c['kind'], c['name'], c['action']) for c in result['changes']],
                         [('queries', 'loose', 'delete')])
        self.assertEqual(result['not_in_file'], [{'kind': 'queries', 'name': 'made_in_the_console'}])
        self.assertIn('1 to delete.', configcode.render(result))
        self.assertTrue(store.saved_query_exists('loose'))  # a diff deletes nothing
        configcode.apply(self.doc, prune=True)
        self.assertFalse(store.saved_query_exists('loose'))
        self.assertTrue(store.saved_query_exists('made_in_the_console'))

    def test_without_prune_nothing_is_deleted(self):
        self.doc['queries'] = []
        self.assertEqual(configcode.apply(self.doc)['changes'], [])
        self.assertTrue(store.saved_query_exists('loose'))

    def test_a_managed_connections_descriptions_are_listed_in_full(self):
        self.doc['descriptions']['warehouse'] = self.doc['descriptions']['warehouse'][:1]
        configcode.apply(self.doc, prune=True)
        self.assertEqual([d['column'] for d in descriptions.list_for('warehouse')], [None])

    def test_a_destination_an_export_still_uses_is_not_deleted(self):
        doc, _, _ = configcode.export(kinds=('destinations',))
        configcode.apply(doc)
        with self.assertRaises(configcode.ConfigInvalid) as caught:
            configcode.apply(self.file(destinations=[]), prune=True)
        self.assertIn('used by export(s) daily', caught.exception.problems[0])
        self.assertEqual([d['name'] for d in destinations.list_all()], ['bucket'])


class ApiTests(ConfigTestCase):
    def setUp(self):
        super().setUp()
        os.environ['QUERYAPIGATE_API_KEY'] = 'admin-key'
        from queryapigate import admins, create_app
        self.client = create_app().test_client()
        admins.create_admin('ops', 'admin')
        self.ops = {'X-API-Key': admins.issue_token('ops')[1]}
        self.owner = {'X-API-Key': 'admin-key'}
        self.doc = self.file(roles=[{'name': 'r', 'connections': []}])

    def test_diff_and_apply_as_json_or_yaml(self):
        response = self.client.post('/api/v1/config/diff', json=self.doc, headers=self.owner)
        self.assertEqual((response.status_code, response.get_json()['changes'][0]['action']), (200, 'create'))
        response = self.client.post('/api/v1/config/apply?dry_run=true', data=yaml.safe_dump(self.doc),
                                    content_type='application/yaml', headers=self.owner)
        self.assertEqual((response.status_code, response.get_json()['applied']), (200, False))
        self.assertEqual(apikeys.list_roles(), {})
        response = self.client.post('/api/v1/config/apply', json=self.doc, headers=self.owner)
        self.assertTrue(response.get_json()['applied'])
        self.assertIn('r', apikeys.list_roles())
        entry = json.loads(db.connection().execute(
            "SELECT entry_json FROM audit_log WHERE entry_json LIKE '%config_applied%'").fetchone()[0])
        self.assertEqual((entry['actor'], entry['via']), ('admin', 'break-glass'))

    def test_owners_only(self):
        for path in ('/api/v1/config/diff', '/api/v1/config/apply'):
            response = self.client.post(path, json=self.doc, headers=self.ops)
            self.assertEqual((response.status_code, response.get_json()['code']), (403, 'role_forbidden'))

    def test_every_problem_in_the_answer(self):
        response = self.client.post('/api/v1/config/diff', headers=self.owner, json=self.file(
            connections=[{'name': 'c', 'db': 'mysql', 'password': 'plain'}], roles=[{'name': 'r', 'x': 1}]))
        body = response.get_json()
        self.assertEqual((response.status_code, body['code'], len(body['problems'])), (400, 'invalid_config', 2))


class CliApplyTests(ConfigTestCase):
    def test_apply_dry_run_and_prune(self):
        self.build()
        path = os.path.join(self.tmp.name, 'queryapigate.yaml')
        self.cli('config', 'export', '--kinds', 'roles', '--out', path)
        with open(path) as f:
            text = f.read().replace('60/minute', '10/minute')
        code, out, _ = self.cli('config', 'apply', '--dry-run', '-', stdin=text)
        self.assertEqual(code, 0)
        self.assertTrue(out.endswith('Dry run: nothing was changed.\n'))
        self.assertEqual(apikeys.list_roles()['partner']['rate_limit'], '60/minute')
        code, out, _ = self.cli('config', 'apply', '-', stdin=text)
        self.assertEqual(code, 0)
        self.assertIn('Applied: the server matches the file', out)
        self.assertEqual(apikeys.list_roles()['partner']['rate_limit'], '10/minute')
        code, out, _ = self.cli('config', 'apply', '-', stdin=text)
        self.assertIn('No changes: the server matches the file.', out)


# --------------------------------------------------------------------------------------
# Slice 3: applied at startup, and locked to the file
# --------------------------------------------------------------------------------------

class StartupTests(ConfigTestCase):
    def write(self, doc):
        path = os.path.join(self.tmp.name, 'queryapigate.yaml')
        with open(path, 'w') as f:
            yaml.safe_dump(doc, f)
        return path

    def start(self, path, **env):
        from queryapigate import create_app
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_CONFIG_FILE': path, **env}):
            return create_app()

    def test_the_file_is_applied_before_serving(self):
        path = self.write(self.file(roles=[{'name': 'from-the-file', 'connections': []}]))
        with self.assertLogs('queryapigate', 'INFO') as logs:
            self.start(path)
        self.assertIn('from-the-file', apikeys.list_roles())
        self.assertTrue(any('1 created, 0 changed, 0 deleted' in line for line in logs.output))
        entry = json.loads(db.connection().execute(
            "SELECT entry_json FROM audit_log WHERE entry_json LIKE '%config_applied%'").fetchone()[0])
        self.assertEqual(entry['actor'], 'startup')

    def test_once_per_file_so_a_console_change_survives_a_restart(self):
        path = self.write(self.file(roles=[{'name': 'r', 'connections': [], 'rate_limit': '5/minute'}]))
        self.start(path)
        apikeys.update_role('r', rate_limit='50/minute')  # changed in the Console since
        with self.assertLogs('queryapigate', 'INFO') as logs:
            self.start(path)  # a restart, or the next instance
        self.assertEqual(apikeys.list_roles()['r']['rate_limit'], '50/minute')
        self.assertTrue(any('is already applied' in line for line in logs.output))
        self.assertEqual(configcode.apply(configcode.load(open(path).read()))['changes'][0]['fields'],
                         {'rate_limit': {'from': '50/minute', 'to': '5/minute'}})  # applied by hand, it does

    def test_a_file_that_cant_be_applied_stops_startup_changing_nothing(self):
        path = self.write(self.file(roles=[{'name': 'r'}], connections=[{'name': 'c', 'db': 'mysql',
                                                                          'password': 'plain'}]))
        with self.assertRaises(ValueError) as caught:
            self.start(path)
        self.assertIn('was not applied - nothing in it was. connections[c]: password must be a ${VAR}',
                      str(caught.exception))
        self.assertEqual(apikeys.list_roles(), {})
        with self.assertRaises(ValueError) as caught:
            self.start(os.path.join(self.tmp.name, 'missing.yaml'))
        self.assertIn('could not be read', str(caught.exception))

    def test_prune_needs_a_file(self):
        from queryapigate import config
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_CONFIG_PRUNE': '1'}):
            with self.assertRaises(ValueError) as caught:
                config.check_settings()
        self.assertIn('QUERYAPIGATE_CONFIG_FILE is not', str(caught.exception))

    def test_with_prune_startup_deletes_what_the_file_dropped(self):
        self.start(self.write(self.file(roles=[{'name': 'a', 'connections': []}, {'name': 'b', 'connections': []}])))
        self.start(self.write(self.file(roles=[{'name': 'a', 'connections': []}])), QUERYAPIGATE_CONFIG_PRUNE='1')
        self.assertEqual(set(apikeys.list_roles()), {'a'})


class LockTests(ConfigTestCase):
    def setUp(self):
        super().setUp()
        self.build()
        doc, _, _ = configcode.export(kinds=('queries', 'roles', 'connections'))
        configcode.apply(doc)
        os.environ['QUERYAPIGATE_API_KEY'] = 'admin-key'
        from queryapigate import create_app
        self.client = create_app().test_client()
        self.admin = {'X-API-Key': 'admin-key'}

    def locked(self):
        return mock.patch.dict(os.environ, {'QUERYAPIGATE_CONFIG_LOCKED': '1'})

    def test_what_a_file_manages_changes_only_through_a_file(self):
        with self.locked():
            for method, path, body in (
                    ('patch', '/api/v1/roles/partner', {'rate_limit': '1/minute'}),
                    ('delete', '/api/v1/queries/loose', None),
                    ('post', '/api/v1/queries/loose/versions', {'sql': 'SELECT 2', 'description': 'd'}),
                    ('patch', '/api/v1/connections/legacy', {'host': 'elsewhere'}),
                    ('patch', '/api/v1/collections/finance', {'name': 'money'})):
                response = getattr(self.client, method)(path, json=body, headers=self.admin)
                with self.subTest(path=path):
                    self.assertEqual((response.status_code, response.get_json()['code']), (409, 'config_managed'))
            # what no file manages is as it always was
            self.assertEqual(self.client.patch('/api/v1/api-keys/ops', json={'rate_limit': '1/minute'},
                                               headers=self.admin).status_code, 200)
            # and the file itself still can
            doc, _, _ = configcode.export(kinds=('roles',))
            doc['roles'][0]['rate_limit'] = '1/minute'
            self.assertEqual(self.client.post('/api/v1/config/apply', json=doc, headers=self.admin).status_code, 200)
        self.assertEqual(apikeys.list_roles()['partner']['rate_limit'], '1/minute')

    def test_unlocked_the_api_changes_anything_and_the_diff_shows_the_drift(self):
        response = self.client.patch('/api/v1/roles/partner', json={'rate_limit': '1/minute'}, headers=self.admin)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get('/api/v1/config/managed', headers=self.admin).get_json()['locked'], False)
        doc, _, _ = configcode.export(kinds=('roles',))
        doc['roles'][0]['rate_limit'] = '60/minute'
        change = configcode.diff(doc)['changes'][0]
        self.assertEqual(change['fields'], {'rate_limit': {'from': '1/minute', 'to': '60/minute'}})

    def test_the_managed_listing(self):
        with self.locked():
            body = self.client.get('/api/v1/config/managed', headers=self.admin).get_json()
        self.assertTrue(body['locked'])
        self.assertIn(('roles', 'partner'), [(i['kind'], i['name']) for i in body['items']])


# --------------------------------------------------------------------------------------
# Slice 4: what the security review changed
# --------------------------------------------------------------------------------------

class ReviewTests(ConfigTestCase):
    def test_a_reference_to_the_servers_own_opentelemetry_settings_is_refused(self):
        problems = self.problems(self.file(connections=[
            {'name': 'c', 'db': 'mysql', 'host': 'theirs.example', 'password': '${OTEL_EXPORTER_OTLP_HEADERS}'}]))
        self.assertIn("one of this server's own settings", problems[0])
        from queryapigate.errors import ApiError
        with self.assertRaises(ApiError):  # through the API too, as for QUERYAPIGATE_*
            connections.create({'name': 'c', 'db': 'mysql', 'password': '${OTEL_EXPORTER_OTLP_HEADERS}'}, 't')

    def test_yaml_aliases_cant_make_a_small_file_huge(self):
        lines = ['format: queryapigate-config', 'format_version: 1', 'a: &a ["x","x","x","x","x","x","x","x","x"]']
        for i in range(1, 9):
            lines.append(f'{chr(97 + i)}: &{chr(97 + i)} [' + ','.join(['*' + chr(96 + i)] * 9) + ']')
        with self.assertRaises(configcode.ConfigInvalid) as caught:
            configcode.load('\n'.join(lines))
        self.assertIn('more than 1,000,000 values', caught.exception.problems[0])

    def test_a_failed_startup_is_one_line_in_the_log(self):
        from queryapigate import create_app
        path = os.path.join(self.tmp.name, 'bad.yaml')
        with open(path, 'w') as f:
            yaml.safe_dump(self.file(roles=[{'name': 'r', 'x': 1}]), f)
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_CONFIG_FILE': path}), \
                self.assertLogs('queryapigate', 'ERROR') as logs, self.assertRaises(ValueError):
            create_app()
        self.assertIn('was not applied - nothing in it was. roles[r]: unknown field(s): x', logs.output[0])


class ReviewApiTests(ConfigTestCase):
    def setUp(self):
        super().setUp()
        self.build()
        configcode.apply(configcode.export(kinds=('queries', 'exports'))[0])
        os.environ['QUERYAPIGATE_API_KEY'] = 'admin-key'
        from queryapigate import create_app
        self.client = create_app().test_client()
        self.admin = {'X-API-Key': 'admin-key'}

    def test_locked_an_exports_watermark_still_moves(self):
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_CONFIG_LOCKED': '1'}):
            moved = self.client.patch('/api/v1/exports/daily', json={'watermark': None}, headers=self.admin)
            changed = self.client.patch('/api/v1/exports/daily', json={'format': 'parquet'}, headers=self.admin)
        self.assertEqual((moved.status_code, changed.status_code), (200, 409))

    def test_what_the_api_deletes_no_file_manages_any_more(self):
        self.assertEqual(self.client.delete('/api/v1/exports/daily', headers=self.admin).status_code, 204)
        self.assertEqual(self.client.delete('/api/v1/queries/loose', headers=self.admin).status_code, 204)
        managed = {(i['kind'], i['name']) for i in configcode.managed_listing()['items']}
        self.assertNotIn(('exports', 'daily'), managed)
        self.assertNotIn(('queries', 'loose'), managed)
        self.assertIn(('queries', 'monthly_revenue'), managed)
        # one made again in the Console, of the same name, is the Console's: a prune leaves it alone
        store.save_version('loose', {'sql_query': 'SELECT 1', 'author': 'a', 'description': 'd', 'tags': [],
                                     'query_parameters': {}})
        doc = self.file(queries=[])
        self.assertEqual(configcode.diff(doc, prune=True)['changes'][0]['name'], 'monthly_revenue')
        self.assertNotIn('loose', [c['name'] for c in configcode.diff(doc, prune=True)['changes']])


class RemoteCliTests(ConfigTestCase):
    """`config diff|apply --server URL`: the file posted to a running server, the key from QAG_API_KEY, and nothing of
    the local home touched - CI runs it in a checkout."""

    def setUp(self):
        super().setUp()
        os.environ['QUERYAPIGATE_API_KEY'] = 'admin-key'
        from queryapigate import create_app
        self.client = create_app().test_client()
        self.path = os.path.join(self.tmp.name, 'queryapigate.yaml')
        with open(self.path, 'w') as f:
            yaml.safe_dump(self.file(roles=[{'name': 'r', 'connections': []}]), f)

    def urlopen(self, request, timeout=None):
        from urllib.error import HTTPError
        path = request.full_url.replace('http://prod.example', '')
        response = self.client.post(path, data=request.data, headers=dict(request.header_items()))
        body = io.BytesIO(response.data)
        if response.status_code >= 400:
            raise HTTPError(request.full_url, response.status_code, 'error', {}, body)
        body.__enter__ = lambda *a: body
        return body

    def remote(self, *argv, key='admin-key'):
        with mock.patch('urllib.request.urlopen', self.urlopen), mock.patch.dict(os.environ, {'QAG_API_KEY': key}), \
                mock.patch.object(cli.db, 'init_schema', side_effect=AssertionError('opened a local store')):
            return self.cli('config', *argv, '--server', 'http://prod.example', self.path)  # none made where CI runs

    def test_diff_then_apply_against_the_server(self):
        code, out, _ = self.remote('diff')
        self.assertEqual(code, 2)
        self.assertIn('+ roles[r]', out)
        code, out, _ = self.remote('apply')
        self.assertEqual(code, 0)
        self.assertIn('Applied: the server matches the file', out)
        self.assertIn('r', apikeys.list_roles())
        self.assertEqual(self.remote('diff')[0], 0)

    def test_its_problems_and_a_wrong_key(self):
        with open(self.path, 'w') as f:
            yaml.safe_dump(self.file(roles=[{'name': 'r', 'x': 1}]), f)
        code, _, err = self.remote('diff')
        self.assertEqual(code, 1)
        self.assertIn('1 problem(s) in the file:\n  roles[r]: unknown field(s): x', err)
        code, _, err = self.remote('diff', key='wrong')
        self.assertEqual(code, 1)
        self.assertIn('answered 401', err)
        with mock.patch.dict(os.environ, {'QAG_API_KEY': ''}):
            code, _, err = self.cli('config', 'diff', '--server', 'http://prod.example', self.path)
        self.assertIn('QAG_API_KEY', err)


class RealServerTests(unittest.TestCase):
    """`--server` against `queryapigate serve` in a process of its own, over real HTTP - what CI does with it. The CLI
    runs in another process, its home an empty directory that must stay empty (ADR 0017)."""

    @classmethod
    def setUpClass(cls):
        import socket
        import subprocess
        import sys
        import time
        import urllib.request
        cls.tmp = tempfile.TemporaryDirectory()
        cls.server_home = os.path.join(cls.tmp.name, 'server')
        cls.cli_home = os.path.join(cls.tmp.name, 'cli')
        os.makedirs(cls.server_home)
        os.makedirs(cls.cli_home)
        with socket.socket() as s:
            s.bind(('127.0.0.1', 0))
            port = s.getsockname()[1]
        cls.base = f'http://127.0.0.1:{port}'
        env = {k: v for k, v in os.environ.items() if not k.startswith('QUERYAPIGATE_')}
        cls.process = subprocess.Popen(
            [sys.executable, '-m', 'queryapigate', 'serve', '--port', str(port)],
            env={**env, 'QUERYAPIGATE_HOME': cls.server_home, 'QUERYAPIGATE_API_KEY': 'owner-key'},
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 30
        while True:
            try:
                with urllib.request.urlopen(cls.base + '/health', timeout=2):
                    break
            except OSError:
                if time.monotonic() > deadline or cls.process.poll() is not None:
                    cls.tearDownClass()
                    raise
                time.sleep(0.2)

    @classmethod
    def tearDownClass(cls):
        cls.process.terminate()
        try:
            cls.process.wait(timeout=10)
        except Exception:
            cls.process.kill()
        cls.tmp.cleanup()

    def cli(self, *argv, key='owner-key'):
        import subprocess
        import sys
        env = {k: v for k, v in os.environ.items() if not k.startswith('QUERYAPIGATE_')}
        return subprocess.run([sys.executable, '-m', 'queryapigate', 'config', *argv, '--server', self.base, self.path],
                              env={**env, 'QUERYAPIGATE_HOME': self.cli_home, 'QAG_API_KEY': key},
                              capture_output=True, text=True, timeout=60)

    def setUp(self):
        self.path = os.path.join(self.tmp.name, 'queryapigate.yaml')
        with open(self.path, 'w') as f:
            yaml.safe_dump({'format': 'queryapigate-config', 'format_version': 1,
                            'roles': [{'name': 'from-file', 'connections': [], 'rate_limit': '60/minute'}]}, f)

    def role(self):
        import urllib.error
        import urllib.request
        request = urllib.request.Request(self.base + '/api/v1/roles/from-file', headers={'X-API-Key': 'owner-key'})
        try:
            with urllib.request.urlopen(request, timeout=10) as res:
                return json.load(res)
        except urllib.error.HTTPError as error:
            return error.code

    def test_diff_apply_diff_and_a_wrong_key(self):
        refused = self.cli('diff', key='wrong')
        self.assertEqual(refused.returncode, 1)
        self.assertIn('answered 401', refused.stderr)
        diff = self.cli('diff')
        self.assertEqual(diff.returncode, 2, diff.stderr)
        self.assertIn('+ roles[from-file]', diff.stdout)
        self.assertEqual(self.role(), 404)
        applied = self.cli('apply')
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertIn('Applied: the server matches the file', applied.stdout)
        self.assertEqual(self.role()['rate_limit'], '60/minute')
        self.assertEqual(self.cli('diff').returncode, 0)
        self.assertEqual(os.listdir(self.cli_home), [], 'the CLI opened a store of its own')

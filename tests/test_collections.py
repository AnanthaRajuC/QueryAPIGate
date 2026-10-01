"""Tests for collections (BACKLOG #35): one named group per saved query, stored on the query file itself, granted
to keys live and read-only, moved and renamed without ever narrowing access part-way.

The tests that matter most for "no drift" are the ones that compare independent code paths: the run path,
/openapi.json and /catalog must agree on what a key can reach, and every route must appear in the OpenAPI spec."""
import json
import os
import re
import sqlite3
import tempfile
import unittest
from unittest import mock

from queryapigate import apikeys, collection_admin, create_app, db, store
from queryapigate.errors import ApiError
from tests.helpers import write_connections


class AppTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        tmp = self.tmp.name
        db_path = os.path.join(tmp, 'test.db')
        conn = sqlite3.connect(db_path)
        conn.execute('CREATE TABLE t (id INTEGER)')
        conn.execute('INSERT INTO t VALUES (1)')
        conn.commit()
        conn.close()
        patcher = mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': tmp, 'QUERYAPIGATE_API_KEY': 'admin-key'})
        patcher.start()
        self.addCleanup(patcher.stop)
        write_connections({
            'a': {'db': 'sqlite', 'database': db_path, 'active': True},
            'b': {'db': 'sqlite', 'database': db_path, 'active': True},
        })
        self.client = create_app().test_client()
        self.admin = {'X-API-Key': 'admin-key'}
        apikeys._last_recorded_use.clear()

    def save(self, name, collection=store._UNSET, connection='a', sql='SELECT id FROM t'):
        body = {'filename': name, 'author': 'me', 'description': f'{name} query', 'sql_query': sql,
                'connection_name': connection}
        if collection is not store._UNSET:
            body['collection'] = collection
        res = self.client.patch('/save_sql_to_file', json=body, headers=self.admin)
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        return res.get_json()

    def move(self, name, collection):
        return self.client.put(f'/saved_sql/{name}/collection', json={'collection': collection}, headers=self.admin)

    def make_key(self, name, **fields):
        res = self.client.post('/api_keys', json={'name': name, **fields}, headers=self.admin)
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        return {'X-API-Key': res.get_json()['key']}

    def call(self, name, headers):
        return self.client.get(f'/q/{name}', headers=headers).status_code

    def collections(self):
        return self.client.get('/collections', headers=self.admin).get_json()

    def audit(self):
        return self.client.get('/audit_log', headers=self.admin).get_json()['entries']

    def query_file(self, name):
        return store.load_versions(name)


class NameTests(unittest.TestCase):
    def test_valid_names(self):
        for name in ('reporting', 'a', 'partner-a', 'v1.2', 'team_x', '9lives', 'x' * 63):
            self.assertEqual(store.validate_collection_name(name), name)

    def test_invalid_names_are_rejected_not_folded(self):
        for name in ('Reporting', 'has space', '', '-lead', '.lead', '../x', 'a/b', 'x' * 64, 'é', None, 5, ['a']):
            with self.assertRaises(ApiError, msg=repr(name)):
                store.validate_collection_name(name)


class StorageTests(AppTestCase):
    def test_collection_is_stored_on_the_query_file_itself(self):
        self.save('q1', collection='reporting')
        self.assertEqual(self.query_file('q1')['collection'], 'reporting')

    def test_a_query_with_no_collection_has_no_key_in_its_file(self):
        self.save('q1')
        self.assertNotIn('collection', self.query_file('q1'))

    def test_a_new_version_keeps_the_collection_unless_told_otherwise(self):
        self.save('q1', collection='reporting')
        self.save('q1')  # says nothing about a collection
        self.assertEqual(self.query_file('q1')['collection'], 'reporting')
        self.save('q1', collection='ops')
        self.assertEqual(self.query_file('q1')['collection'], 'ops')
        self.save('q1', collection=None)
        self.assertNotIn('collection', self.query_file('q1'))

    def test_a_bad_collection_saves_nothing(self):
        res = self.client.patch('/save_sql_to_file', headers=self.admin, json={
            'filename': 'q1', 'author': 'me', 'description': 'd', 'sql_query': 'SELECT 1', 'collection': 'Bad Name'})
        self.assertEqual(res.status_code, 400)
        self.assertFalse(store.saved_query_exists('q1'))

    def test_moving_is_not_a_new_version_and_touches_nothing_else(self):
        self.save('q1')
        before = self.query_file('q1')
        self.assertEqual(self.move('q1', 'reporting').status_code, 200)
        after = self.query_file('q1')
        self.assertEqual(after.pop('collection'), 'reporting')
        self.assertEqual(after, before)  # same versions, same timestamps, same everything else

    def test_deleting_the_query_removes_its_membership(self):
        self.save('q1', collection='reporting')
        self.client.delete('/saved_sql/q1', headers=self.admin)
        self.assertEqual(self.collections()['collections'], {})

    def test_deleting_one_version_keeps_the_collection(self):
        self.save('q1', collection='reporting')
        self.save('q1')
        self.client.delete('/saved_sql/q1?version=2', headers=self.admin)
        self.assertEqual(self.collections()['collections']['reporting']['queries'], ['q1'])

    def test_a_hand_edited_invalid_value_reads_as_no_collection(self):
        self.save('q1', collection='reporting')
        # Simulates a hand-edited invalid value - bypasses set_collection()'s own validation the same way
        # directly hand-editing the old JSON file could.
        with db.transaction() as conn:
            conn.execute("UPDATE saved_queries SET collection = 'Not Valid!' WHERE name = 'q1'")
        self.assertEqual(self.collections()['collections'], {})
        self.assertEqual(self.collections()['uncollected'], ['q1'])

    def test_list_files_reports_the_collection(self):
        self.save('q1', collection='reporting')
        self.save('q2')
        files = {f['filename']: f['collection'] for f in
                 self.client.get('/list_files', headers=self.admin).get_json()['files']}
        self.assertEqual(files, {'q1': 'reporting', 'q2': None})


class MoveEndpointTests(AppTestCase):
    def test_move_in_and_out(self):
        self.save('q1')
        res = self.move('q1', 'reporting').get_json()
        self.assertEqual((res['from'], res['to']), (None, 'reporting'))
        res = self.move('q1', None).get_json()
        self.assertEqual((res['from'], res['to']), ('reporting', None))
        self.assertNotIn('collection', self.query_file('q1'))

    def test_only_admin_can_move(self):
        self.save('q1')
        scoped = self.make_key('k', connections=['a'])
        res = self.client.put('/saved_sql/q1/collection', json={'collection': 'x'}, headers=scoped)
        self.assertEqual(res.status_code, 403)

    def test_bad_requests(self):
        self.save('q1')
        self.assertEqual(self.client.put('/saved_sql/q1/collection', json={}, headers=self.admin).status_code, 400)
        self.assertEqual(self.move('q1', 'Bad Name').status_code, 400)
        self.assertEqual(self.move('q1', '').status_code, 400)
        self.assertEqual(self.move('nope', 'x').status_code, 404)

    def test_the_response_and_audit_name_the_keys_that_gain_and_lose_access(self):
        self.save('q1', collection='ops')
        self.save('q2', collection='reporting')
        self.make_key('reader', collections=['reporting'])
        self.make_key('ops-reader', collections=['ops'])
        res = self.move('q2', 'ops').get_json()
        self.assertEqual(res['access']['keys'], {'gain': ['ops-reader'], 'lose': ['reader']})
        entry = self.audit()[0]
        self.assertEqual(entry['action'], 'move_query')
        self.assertEqual(entry['target'], 'q2')
        self.assertEqual(entry['changes']['collection'], {'from': 'reporting', 'to': 'ops'})
        self.assertEqual(entry['changes']['keys_gaining_access'], ['ops-reader'])
        self.assertEqual(entry['changes']['keys_losing_access'], ['reader'])

    def test_a_key_that_reaches_both_collections_neither_gains_nor_loses(self):
        self.save('q1', collection='one')
        self.save('q2', collection='two')
        self.make_key('both', collections=['one', 'two'])
        res = self.move('q1', 'two').get_json()
        self.assertEqual(res['access']['keys'], {'gain': [], 'lose': []})

    def test_a_no_op_move_is_not_audited(self):
        self.save('q1', collection='reporting')
        count = len(self.audit())
        self.assertEqual(self.move('q1', 'reporting').status_code, 200)
        self.assertEqual(len(self.audit()), count)

    def test_saving_with_a_collection_is_audited(self):
        self.save('q1', collection='reporting')
        self.assertEqual(self.audit()[0]['changes']['collection'], 'reporting')


class GrantTests(AppTestCase):
    def setUp(self):
        super().setUp()
        self.save('rep1', collection='reporting', connection='a')
        self.save('rep2', collection='reporting', connection='b')
        self.save('other', collection='ops', connection='a')
        self.save('loose', connection='a')

    def test_a_collection_key_runs_exactly_its_collections_queries_with_no_connection_grant(self):
        key = self.make_key('partner', connections=[], collections=['reporting'])
        self.assertEqual(self.call('rep1', key), 200)
        self.assertEqual(self.call('rep2', key), 200)  # reaches it whatever connection it sits on
        self.assertEqual(self.call('other', key), 403)
        self.assertEqual(self.call('loose', key), 403)

    def test_a_collection_never_grants_ad_hoc_sql(self):
        key = self.make_key('partner', connections=[], collections=['reporting'])
        res = self.client.post('/execute_sql', json={'sql': 'SELECT 1', 'connection_name': 'a'}, headers=key)
        self.assertEqual(res.status_code, 403)

    def test_a_collection_never_grants_writes(self):
        self.save('mutate', collection='writers', sql='DELETE FROM t')
        key = self.make_key('partner', connections=[], collections=['writers'])
        res = self.client.get('/q/mutate', headers=key)
        self.assertNotEqual(res.status_code, 200)
        self.assertEqual(self.client.get('/q/rep1', headers=self.admin).status_code, 200)  # table untouched

    def test_the_grant_is_live(self):
        key = self.make_key('partner', connections=[], collections=['reporting'])
        self.assertEqual(self.call('loose', key), 403)
        self.move('loose', 'reporting')
        self.assertEqual(self.call('loose', key), 200)
        self.move('loose', None)
        self.assertEqual(self.call('loose', key), 403)

    def test_grants_are_additive_with_queries_and_connections(self):
        key = self.make_key('mixed', connections=[], queries=['loose'], collections=['reporting'])
        self.assertEqual([self.call(n, key) for n in ('rep1', 'loose', 'other')], [200, 200, 403])
        key = self.make_key('mixed2', connections=['a'], collections=['reporting'])
        self.assertEqual([self.call(n, key) for n in ('rep1', 'rep2', 'other', 'loose')], [200, 200, 200, 200])

    def test_unknown_collection_is_rejected_and_lists_the_real_ones(self):
        res = self.client.post('/api_keys', json={'name': 'k', 'collections': ['reportng']}, headers=self.admin)
        self.assertEqual(res.status_code, 400)
        message = res.get_json()['error']
        self.assertIn('reportng', message)
        self.assertIn('reporting', message)

    def test_malformed_grants_are_rejected(self):
        for value in ('*', 'reporting', ['Reporting'], ['reporting', 'reporting'], [1], {'a': 1}):
            res = self.client.post('/api_keys', json={'name': 'k', 'collections': value}, headers=self.admin)
            self.assertEqual(res.status_code, 400, value)
        self.assertNotIn('k', self.client.get('/api_keys', headers=self.admin).get_json()['keys'])

    def test_an_empty_collection_grant_is_inert_not_a_wildcard(self):
        key = self.make_key('none', connections=[], collections=[])
        self.assertEqual(self.call('rep1', key), 403)

    def test_update_replaces_and_only_new_names_must_exist(self):
        self.make_key('k', connections=[], collections=['reporting'])
        self.move('rep1', 'ops')
        self.move('rep2', 'ops')  # 'reporting' is now empty: the grant is inert but still recorded
        res = self.client.patch('/api_keys/k', json={'collections': ['reporting']}, headers=self.admin)
        self.assertEqual(res.status_code, 200)  # re-saving an already-held, now-empty name is fine
        res = self.client.patch('/api_keys/k', json={'collections': ['reporting', 'nope']}, headers=self.admin)
        self.assertEqual(res.status_code, 400)
        res = self.client.patch('/api_keys/k', json={'collections': ['ops']}, headers=self.admin)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.client.get('/api_keys', headers=self.admin).get_json()['keys']['k']['collections'],
                         ['ops'])

    def test_a_key_stored_before_the_field_existed_still_works(self):
        secret = 'sk_legacy'
        with db.transaction() as conn:
            conn.execute("""
                INSERT INTO api_keys (name, hash, active, expires_at, created_at, details_json)
                VALUES ('old', ?, 1, NULL, '2026-01-01 00:00:00', ?)
            """, (apikeys._hash(secret), json.dumps({'connections': ['a'], 'allow_writes': False, 'queries': []})))
        self.assertEqual(self.call('rep1', {'X-API-Key': secret}), 200)  # via its connection grant, unchanged
        listed = self.client.get('/api_keys', headers=self.admin).get_json()['keys']['old']
        self.assertEqual(listed['collections'], [])
        res = self.client.patch('/api_keys/old', json={'active': True}, headers=self.admin)
        self.assertEqual(res.status_code, 200)
        self.assertEqual([e for e in self.audit() if e['action'] == 'update_key'], [])  # no phantom diff


class RoleTests(AppTestCase):
    def setUp(self):
        super().setUp()
        self.save('rep1', collection='reporting')

    def test_a_role_carries_collections_and_a_key_copies_them(self):
        res = self.client.post('/roles', json={'name': 'partner', 'connections': [], 'collections': ['reporting']},
                               headers=self.admin)
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        key = self.make_key('k', role='partner')
        self.assertEqual(self.call('rep1', key), 200)
        listed = self.client.get('/api_keys', headers=self.admin).get_json()['keys']['k']
        self.assertEqual(listed['collections'], ['reporting'])
        self.assertEqual(listed['created_from_role'], 'partner')

    def test_role_validation_matches_key_validation(self):
        res = self.client.post('/roles', json={'name': 'r', 'collections': ['nope']}, headers=self.admin)
        self.assertEqual(res.status_code, 400)
        res = self.client.post('/roles', json={'name': 'r', 'collections': ['Bad']}, headers=self.admin)
        self.assertEqual(res.status_code, 400)

    def test_a_role_and_explicit_collections_cannot_be_combined(self):
        self.client.post('/roles', json={'name': 'partner', 'collections': ['reporting']}, headers=self.admin)
        res = self.client.post('/api_keys', json={'name': 'k', 'role': 'partner', 'collections': ['reporting']},
                               headers=self.admin)
        self.assertEqual(res.status_code, 400)

    def test_editing_the_role_afterward_does_not_change_an_existing_key(self):
        self.client.post('/roles', json={'name': 'partner', 'connections': [], 'collections': ['reporting']},
                         headers=self.admin)
        key = self.make_key('k', role='partner')
        self.client.patch('/roles/partner', json={'collections': []}, headers=self.admin)
        self.assertEqual(self.call('rep1', key), 200)

    def test_a_key_can_still_be_made_from_a_role_whose_collection_has_since_emptied(self):
        self.client.post('/roles', json={'name': 'partner', 'connections': [], 'collections': ['reporting']},
                         headers=self.admin)
        self.move('rep1', None)
        self.make_key('k', role='partner')  # must not fail on the role's now-empty collection

    def test_update_role_collections(self):
        self.client.post('/roles', json={'name': 'partner'}, headers=self.admin)
        res = self.client.patch('/roles/partner', json={'collections': ['reporting']}, headers=self.admin)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.client.get('/roles', headers=self.admin).get_json()['roles']['partner']['collections'],
                         ['reporting'])


class ConnectionOverrideTests(AppTestCase):
    """A `queries` or `collections` grant authorises a saved query on *its own* connection. It must not let a
    caller redirect that query's SQL to a connection they were never granted with ?connection_name=."""

    def setUp(self):
        super().setUp()
        self.save('q', collection='grp', connection='a')
        self.save('no_default', collection='grp', connection=None)

    def test_neither_grant_can_be_redirected_to_another_connection(self):
        for grant in ({'queries': ['q']}, {'collections': ['grp']}):
            key = self.make_key(f'k-{next(iter(grant))}', connections=[], **grant)
            self.assertEqual(self.client.get('/q/q', headers=key).status_code, 200)
            self.assertEqual(self.client.get('/q/q?connection_name=a', headers=key).status_code, 200)  # same one
            res = self.client.get('/q/q?connection_name=b', headers=key)
            self.assertEqual(res.status_code, 403, grant)
            self.assertIn("'b'", res.get_json()['error'])
            res = self.client.post('/q/q', json={'connection_name': 'b'}, headers=key)
            self.assertEqual(res.status_code, 403, grant)

    def test_a_query_with_no_default_connection_needs_connection_access_to_run_at_all(self):
        key = self.make_key('k', connections=[], collections=['grp'])
        self.assertEqual(self.client.get('/q/no_default?connection_name=a', headers=key).status_code, 403)

    def test_a_key_that_holds_the_connection_may_still_choose_it(self):
        key = self.make_key('k', connections=['b'], collections=['grp'])
        self.assertEqual(self.client.get('/q/q?connection_name=b', headers=key).status_code, 200)
        self.assertEqual(self.client.get('/q/q?connection_name=a', headers=key).status_code, 200)  # its own default

    def test_admin_may_override(self):
        self.assertEqual(self.client.get('/q/q?connection_name=b', headers=self.admin).status_code, 200)


class SurfaceAgreementTests(AppTestCase):
    """The run path, /openapi.json and /catalog each decide whether a caller may reach a saved query. They must
    give the same answer for every kind of grant - the point of apikeys.can_run_saved()."""

    def test_all_three_surfaces_agree_for_every_grant_type(self):
        self.save('in_rep_a', collection='reporting', connection='a')
        self.save('in_rep_b', collection='reporting', connection='b')
        self.save('in_ops_a', collection='ops', connection='a')
        self.save('loose_a', connection='a')
        self.save('loose_b', connection='b')
        keys = {
            'admin': self.admin,
            'conn_a': self.make_key('conn_a', connections=['a']),
            'all_conns': self.make_key('all_conns', connections='*'),
            'by_name': self.make_key('by_name', connections=[], queries=['loose_b']),
            'all_names': self.make_key('all_names', connections=[], queries='*'),
            'by_collection': self.make_key('by_collection', connections=[], collections=['reporting']),
            'two_collections': self.make_key('two_collections', connections=[], collections=['ops', 'reporting']),
            'mixed': self.make_key('mixed', connections=['b'], queries=['in_ops_a'], collections=['reporting']),
            'nothing': self.make_key('nothing', connections=[]),
        }
        names = ['in_rep_a', 'in_rep_b', 'in_ops_a', 'loose_a', 'loose_b']
        for label, headers in keys.items():
            by_run = {n for n in names if self.call(n, headers) == 200}
            spec = self.client.get('/openapi.json', headers=headers).get_json()
            by_openapi = {n for n in names if f'/q/{n}' in spec['paths']}
            by_catalog = {q['name'] for q in self.client.get('/catalog', headers=headers).get_json()['queries']}
            self.assertEqual(by_run, by_openapi, label)
            self.assertEqual(by_run, by_catalog, label)
        # and the expected answers, so "all three wrong the same way" cannot pass
        expect = {'by_collection': {'in_rep_a', 'in_rep_b'}, 'nothing': set(), 'conn_a': {'in_rep_a', 'in_ops_a',
                                                                                            'loose_a'},
                  'two_collections': {'in_rep_a', 'in_rep_b', 'in_ops_a'},
                  'mixed': {'in_rep_a', 'in_rep_b', 'in_ops_a', 'loose_b'}}
        for label, want in expect.items():
            got = {n for n in names if self.call(n, keys[label]) == 200}
            self.assertEqual(got, want, label)

    def test_catalog_and_openapi_carry_the_collection(self):
        self.save('q1', collection='reporting')
        self.save('q2')
        catalog = {q['name']: q['collection'] for q in
                   self.client.get('/catalog', headers=self.admin).get_json()['queries']}
        self.assertEqual(catalog, {'q1': 'reporting', 'q2': None})


class DescribeTests(AppTestCase):
    def test_lists_queries_keys_and_roles_and_the_uncollected(self):
        self.save('q1', collection='reporting')
        self.save('q2', collection='reporting')
        self.save('q3')
        self.make_key('k', collections=['reporting'])
        self.client.post('/roles', json={'name': 'r', 'collections': ['reporting']}, headers=self.admin)
        data = self.collections()
        self.assertEqual(data['collections'], {'reporting': {'queries': ['q1', 'q2'], 'keys': ['k'], 'roles': ['r']}})
        self.assertEqual(data['uncollected'], ['q3'])

    def test_an_inert_grant_is_visible(self):
        self.save('q1', collection='reporting')
        self.make_key('k', collections=['reporting'])
        self.move('q1', None)
        self.assertEqual(self.collections()['collections'], {'reporting': {'queries': [], 'keys': ['k'], 'roles': []}})

    def test_admin_only(self):
        scoped = self.make_key('k', connections=['a'])
        self.assertEqual(self.client.get('/collections', headers=scoped).status_code, 403)


class RenameTests(AppTestCase):
    def setUp(self):
        super().setUp()
        self.save('q1', collection='old')
        self.save('q2', collection='old')
        self.save('q3', collection='keep')
        self.key = self.make_key('k', connections=[], collections=['old'])
        self.client.post('/roles', json={'name': 'r', 'collections': ['old']}, headers=self.admin)

    def rename(self, old, new, **extra):
        return self.client.patch(f'/collections/{old}', json={'name': new, **extra}, headers=self.admin)

    def test_rename_moves_queries_and_every_grant(self):
        res = self.rename('old', 'new')
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        self.assertEqual(res.get_json()['queries'], ['q1', 'q2'])
        data = self.collections()['collections']
        self.assertEqual(sorted(data), ['keep', 'new'])
        self.assertEqual(data['new'], {'queries': ['q1', 'q2'], 'keys': ['k'], 'roles': ['r']})
        self.assertEqual(self.call('q1', self.key), 200)  # the key kept its reach through the rename

    def test_rename_is_audited(self):
        self.rename('old', 'new')
        entry = self.audit()[0]
        self.assertEqual((entry['action'], entry['target']), ('rename_collection', 'old'))
        self.assertEqual((entry['changes']['to'], entry['changes']['keys'], entry['changes']['roles']),
                         ('new', ['k'], ['r']))

    def test_an_existing_target_needs_merge(self):
        res = self.rename('old', 'keep')
        self.assertEqual(res.status_code, 400)
        self.assertIn('merge', res.get_json()['error'])
        self.assertEqual(self.collections()['collections']['old']['queries'], ['q1', 'q2'])  # nothing changed
        self.assertEqual(self.rename('old', 'keep', merge=True).status_code, 200)
        self.assertEqual(self.collections()['collections']['keep']['queries'], ['q1', 'q2', 'q3'])

    def test_bad_requests(self):
        self.assertEqual(self.rename('nope', 'x').status_code, 404)
        self.assertEqual(self.rename('old', 'old').status_code, 400)
        self.assertEqual(self.rename('old', 'Bad Name').status_code, 400)
        self.assertEqual(self.rename('old', 'x', merge='yes').status_code, 400)
        self.assertEqual(self.client.patch('/collections/old', json={}, headers=self.admin).status_code, 400)

    def test_a_collection_only_a_grant_still_names_can_be_renamed(self):
        self.move('q1', None)
        self.move('q2', None)
        self.assertEqual(self.rename('old', 'new').status_code, 200)
        self.assertEqual(self.collections()['collections']['new'], {'queries': [], 'keys': ['k'], 'roles': ['r']})

    def test_admin_only(self):
        scoped = self.make_key('s', connections=['a'])
        res = self.client.patch('/collections/old', json={'name': 'x'}, headers=scoped)
        self.assertEqual(res.status_code, 403)

    def _reach(self):
        return [self.call(n, self.key) for n in ('q1', 'q2')]

    def test_an_interruption_before_the_move_loses_no_access_and_rerunning_finishes(self):
        with mock.patch.object(store, 'move_collection', side_effect=OSError('disk gone')):
            with self.assertRaises(OSError):
                collection_admin.rename_collection('old', 'new')
        self.assertEqual(self._reach(), [200, 200])  # grants were widened first, queries still under 'old'
        self.assertEqual(sorted(apikeys.list_keys()['k']['collections']), ['new', 'old'])
        self.assertEqual(self.rename('old', 'new', merge=True).status_code, 200)
        self.assertEqual(apikeys.list_keys()['k']['collections'], ['new'])
        self.assertEqual(self.collections()['collections']['new']['queries'], ['q1', 'q2'])
        self.assertEqual(self._reach(), [200, 200])

    def test_an_interruption_during_the_move_moves_nothing_not_half(self):
        """store.move_collection() is now one real SQL transaction - a genuine improvement over the old
        per-file-atomic-but-not-overall approach: a failure after some rows were already touched rolls
        every one of them back, so a partial move (the old "half done" state this test used to assert)
        can no longer happen at all. Injects the failure on the *second* UPDATE inside the transaction -
        proving the first row's change is undone too, not just that the second one never landed.
        A database connection's methods can't be monkeypatched directly (sqlite3.Connection is a C type), so
        this wraps it in a thin proxy instead - transparent for everything except the one statement being made
        flaky, and backend-neutral, so it proves the same rollback on SQLite and Postgres."""
        real_conn = db.connection()

        class FlakyConn:
            calls = 0

            def execute(self, sql, *args, **kwargs):
                if sql.strip().startswith('UPDATE saved_queries SET collection'):
                    FlakyConn.calls += 1
                    if FlakyConn.calls == 2:
                        raise ConnectionError('interrupted')
                return real_conn.execute(sql, *args, **kwargs)

            def __getattr__(self, attr):
                return getattr(real_conn, attr)

        with mock.patch.object(db, 'connection', return_value=FlakyConn()):
            with self.assertRaises(ConnectionError):
                collection_admin.rename_collection('old', 'new')
        self.assertEqual(store.collection_members(), {'old': ['q1', 'q2'], 'keep': ['q3']})  # nothing moved
        self.assertEqual(self._reach(), [200, 200])  # and the key still reaches both
        self.assertEqual(self.rename('old', 'new', merge=True).status_code, 200)
        self.assertEqual(store.collection_members(), {'new': ['q1', 'q2'], 'keep': ['q3']})
        self.assertEqual(apikeys.list_keys()['k']['collections'], ['new'])
        self.assertEqual(apikeys.list_roles()['r']['collections'], ['new'])

    def test_every_query_is_still_loadable_after_a_rename(self):
        self.rename('old', 'new')
        for name in ('q1', 'q2', 'q3'):
            store.load_versions(name)  # must not raise


class RouteDocumentationTests(AppTestCase):
    """A route with no OpenAPI entry is documentation drift. Every route must be described, except the
    handful of pages that are not part of the JSON API."""
    NOT_API = {'/', '/docs', '/favicon.ico', '/openapi.json', '/ui'}

    def test_every_route_is_in_the_openapi_spec(self):
        app = create_app()
        documented = {re.sub(r'\{[^}]+\}', '{x}', path) for path in
                      self.client.get('/openapi.json').get_json()['paths']}
        missing = sorted(rule.rule for rule in app.url_map.iter_rules()
                         if rule.endpoint != 'static' and rule.rule not in self.NOT_API
                         and re.sub(r'<[^>]+>', '{x}', rule.rule) not in documented)
        self.assertEqual(missing, [])


if __name__ == '__main__':
    unittest.main()

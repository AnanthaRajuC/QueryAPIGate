"""Workspaces kept apart (ADR 0023, delivery step 3): two workspaces with the same names for everything - a connection
`lite` on different databases, a saved query `films`, a key `partner`, a role `analyst` - and every route reaching only
the workspace it acts in. A lookup that forgot its workspace shows up here as the other workspace's data."""
import os
import sqlite3

from queryapigate import admins, history, quotas
from tests.test_api_v1 import ADMIN, V1TestCase

B = '/w/team-b'


class IsolationTestCase(V1TestCase):
    def setUp(self):
        super().setUp()
        # default's `lite` (V1TestCase's: Alpha, Beta) - and team-b's own `lite`, on another database
        self.b_db = os.path.join(self.tmp.name, 'b.db')
        conn = sqlite3.connect(self.b_db)
        conn.execute('CREATE TABLE film (film_id INTEGER PRIMARY KEY, title TEXT)')
        conn.execute("INSERT INTO film VALUES (1, 'Bravo'), (2, 'Bolero'), (3, 'Brazil')")
        conn.commit()
        conn.close()
        self.ok('post', '/api/v1/workspaces', json={'name': 'team-b'}, status=201)
        self.ok('post', f'{B}/api/v1/connections', json={'name': 'lite', 'db': 'sqlite', 'database': self.b_db,
                                                          'active': True}, status=201)
        for prefix in ('', B):
            self.ok('post', f'{prefix}/api/v1/queries', status=201, json={
                'name': 'films', 'description': 'All films', 'connection_name': 'lite', 'publish': True,
                'sql': 'SELECT film_id, title FROM film ORDER BY film_id'})
            self.ok('post', f'{prefix}/api/v1/roles', status=201, json={'name': 'analyst', 'connections': ['lite']})
        self.default_key = self.ok('post', '/api/v1/api-keys', status=201,
                                   json={'name': 'partner', 'connections': ['lite']}).get_json()['secret']
        self.b_key = self.ok('post', f'{B}/api/v1/api-keys', status=201,
                             json={'name': 'partner', 'connections': ['lite']}).get_json()['secret']

    def ok(self, method, url, status=200, headers=ADMIN, **kwargs):
        res = getattr(self.client, method)(url, headers=headers, **kwargs)
        self.assertEqual(res.status_code, status, f'{method.upper()} {url}: {res.get_data(as_text=True)}')
        return res

    def titles(self, url, headers=ADMIN):
        return [row['title'] for row in self.ok('get', url, headers=headers).get_json()]

    def code(self, method, url, status, headers=ADMIN, **kwargs):
        return self.ok(method, url, status=status, headers=headers, **kwargs).get_json()['code']


class SameNamesTests(IsolationTestCase):
    def test_each_workspace_has_its_own_of_every_name(self):
        lite = self.ok('get', '/api/v1/connections/lite').get_json()
        b_lite = self.ok('get', f'{B}/api/v1/connections/lite').get_json()
        self.assertEqual((lite['database'], b_lite['database']), (self.db_path, self.b_db))
        for path in ('/api/v1/queries', '/api/v1/api-keys', '/api/v1/roles', '/api/v1/connections'):
            with self.subTest(path=path):
                names = [i['name'] for i in self.ok('get', path).get_json()['items']]
                b_names = [i['name'] for i in self.ok('get', f'{B}{path}').get_json()['items']]
                self.assertEqual(names, b_names)  # the same names - and below, never each other's things

    def test_a_saved_query_runs_on_its_own_workspace_connection(self):
        self.assertEqual(self.titles('/q/films'), ['Alpha', 'Beta'])
        self.assertEqual(self.titles(f'{B}/q/films'), ['Bravo', 'Bolero', 'Brazil'])
        res = self.ok('post', f'{B}/execute_sql', json={'sql': 'SELECT COUNT(*) AS n FROM film',
                                                        'connection_name': 'lite'})
        self.assertEqual(res.get_json(), [{'n': 3}])

    def test_changing_or_deleting_in_one_leaves_the_other_alone(self):
        self.ok('delete', f'{B}/api/v1/queries/films', status=204)
        self.ok('get', f'{B}/api/v1/queries/films', status=404)
        self.assertEqual(self.titles('/q/films'), ['Alpha', 'Beta'])
        self.ok('delete', f'{B}/api/v1/roles/analyst', status=204)
        self.ok('get', '/api/v1/roles/analyst')
        self.ok('patch', f'{B}/api/v1/connections/lite', json={'active': False})
        self.assertTrue(self.ok('get', '/api/v1/connections/lite').get_json()['active'])

    def test_run_history_is_per_workspace(self):
        self.titles('/q/films')
        self.titles(f'{B}/q/films')
        self.titles(f'{B}/q/films')
        history.flush()
        runs = self.ok('get', '/api/v1/history?query=films').get_json()['items']
        b_runs = self.ok('get', f'{B}/api/v1/history?query=films').get_json()['items']
        self.assertEqual((len(runs), len(b_runs)), (1, 2))
        versions = self.ok('get', f'{B}/api/v1/queries/films/history').get_json()['items']
        self.assertEqual(len(versions), 2)

    def test_a_streamed_run_is_recorded_in_its_workspace(self):
        res = self.ok('get', f'{B}/q/films?stream=true&format=csv')
        self.assertEqual(res.get_data(as_text=True).count('\n'), 4)  # its header and team-b's three films
        history.flush()
        b_runs = self.ok('get', f'{B}/api/v1/history?query=films').get_json()['items']
        self.assertEqual([r['rows'] for r in b_runs], [3])
        self.assertEqual(self.ok('get', '/api/v1/history?query=films').get_json()['items'], [])

    def test_a_cached_response_is_its_own_workspaces(self):
        for prefix in ('', B):
            self.ok('post', f'{prefix}/api/v1/queries', status=201, json={
                'name': 'cached', 'description': 'Cached', 'connection_name': 'lite', 'publish': True,
                'cache_ttl': 60, 'sql': 'SELECT film_id, title FROM film ORDER BY film_id'})
        self.assertEqual(self.titles('/q/cached'), ['Alpha', 'Beta'])
        self.assertEqual(self.client.get('/q/cached', headers=ADMIN).headers['X-Cache'], 'HIT')
        self.assertEqual(self.titles(f'{B}/q/cached'), ['Bravo', 'Bolero', 'Brazil'])  # not default's cached one
        self.assertEqual(self.client.get(f'{B}/q/cached', headers=ADMIN).headers['X-Cache'], 'HIT')


class KeyTests(IsolationTestCase):
    def test_a_key_acts_in_its_own_workspace_with_or_without_naming_it(self):
        b = {'X-API-Key': self.b_key}
        self.assertEqual(self.titles('/q/films', headers=b), ['Bravo', 'Bolero', 'Brazil'])
        self.assertEqual(self.titles(f'{B}/q/films', headers=b), ['Bravo', 'Bolero', 'Brazil'])
        self.assertEqual(self.titles('/q/films', headers={'X-API-Key': self.default_key}), ['Alpha', 'Beta'])

    def test_naming_another_workspace_is_404_as_for_none(self):
        for headers, url in (({'X-API-Key': self.b_key}, '/w/default/q/films'),
                             ({'X-API-Key': self.default_key}, f'{B}/q/films'),
                             ({'X-API-Key': self.default_key}, '/w/no-such/q/films')):
            with self.subTest(url=url):
                self.assertEqual(self.code('get', url, 404, headers=headers), 'workspace_not_found')

    def test_quotas_count_each_workspaces_key_apart(self):
        for prefix in ('', B):
            self.ok('patch', f'{prefix}/api/v1/api-keys/partner', json={'quota': {'calls': 1, 'per': 'day'}})
        quotas._counter.pending.clear()
        quotas._counter.read.clear()
        b = {'X-API-Key': self.b_key}
        self.ok('get', '/q/films', headers=b)
        self.assertEqual(self.code('get', '/q/films', 429, headers=b), 'quota_exceeded')
        self.ok('get', '/q/films', headers={'X-API-Key': self.default_key})  # default's partner: its own count

    def test_rate_limits_count_each_workspaces_key_apart(self):
        # a bucket named by the key's name alone was one budget for both partners: team-b's calls refused default's
        for prefix in ('', B):
            self.ok('patch', f'{prefix}/api/v1/api-keys/partner', json={'rate_limit': '1/minute'})
        b = {'X-API-Key': self.b_key}
        self.ok('get', '/q/films', headers=b)
        self.assertEqual(self.code('get', '/q/films', 429, headers=b), 'rate_limited')
        res = self.ok('get', '/q/films', headers={'X-API-Key': self.default_key})  # default's partner: its own budget
        self.assertEqual(res.headers['X-RateLimit-Key-Remaining'], '0')

    def test_concurrency_slots_are_each_workspaces_key_apart(self):
        from types import SimpleNamespace
        from unittest import mock

        from queryapigate import limits
        callers = [SimpleNamespace(name='partner', workspace=w, max_concurrent=1) for w in ('default', 'team-b')]
        with mock.patch.object(limits, 'caller', return_value=callers[1]):
            held = limits.acquire()  # team-b's partner has its one query running
        try:
            with mock.patch.object(limits, 'caller', return_value=callers[0]):
                limits.acquire().release()  # and default's partner may still run one
        finally:
            held.release()


class AdministratorTests(IsolationTestCase):
    def member_of_b(self, role='admin'):
        admins.create_admin('bea', None)
        _, secret = admins.issue_token('bea')
        self.ok('put', '/api/v1/workspaces/team-b/members/bea', json={'role': role})
        return {'X-API-Key': secret}

    def test_a_member_works_in_their_workspace_and_nowhere_else(self):
        bea = self.member_of_b()
        names = [q['name'] for q in self.ok('get', f'{B}/api/v1/queries', headers=bea).get_json()['items']]
        self.assertEqual(names, ['films'])
        self.assertEqual(self.titles(f'{B}/q/films', headers=bea), ['Bravo', 'Bolero', 'Brazil'])
        self.ok('post', f'{B}/api/v1/connections', status=201, headers=bea,
                json={'name': 'more', 'db': 'sqlite', 'database': self.b_db, 'active': True})
        self.assertEqual(self.code('get', '/api/v1/queries', 403, headers=bea), 'workspace_required')
        self.assertEqual(self.code('get', '/w/default/api/v1/queries', 404, headers=bea), 'workspace_not_found')
        self.assertEqual(self.code('get', '/w/default/q/films', 404, headers=bea), 'workspace_not_found')
        self.assertNotIn('more', [c['name'] for c in self.ok('get', '/api/v1/connections').get_json()['items']])

    def test_a_server_wide_administrator_reaches_any_workspace_by_name(self):
        self.ok('get', f'{B}/api/v1/queries')
        self.assertEqual(self.code('get', '/w/no-such/api/v1/queries', 404), 'workspace_not_found')
        me = self.ok('get', f'{B}/api/v1/me').get_json()
        self.assertEqual((me['workspace'], me['role'], me['server_wide']), ('team-b', 'owner', True))

    def test_a_workspace_that_owns_things_cant_be_removed(self):
        res = self.ok('delete', '/api/v1/workspaces/team-b', status=409).get_json()
        self.assertEqual(res['contents'], {'connections': 1, 'queries': 1, 'API keys': 1, 'roles': 1})
        for path in ('/api/v1/api-keys/partner', '/api/v1/roles/analyst', '/api/v1/queries/films',
                     '/api/v1/connections/lite'):
            self.ok('delete', f'{B}{path}', status=204, json={'reason': 'moving out'})
        self.ok('delete', '/api/v1/workspaces/team-b', status=204)
        self.assertEqual(self.titles('/q/films'), ['Alpha', 'Beta'])  # default never noticed


class ListingTests(IsolationTestCase):
    """Everything that lists saved queries, connections or what they mean - a query only team-b has shows up there
    and only there."""

    def setUp(self):
        super().setUp()
        self.ok('post', f'{B}/api/v1/queries', status=201, json={
            'name': 'b_only', 'description': 'Team B', 'connection_name': 'lite', 'publish': True,
            'collection': 'b-reports', 'sql': 'SELECT title FROM film'})

    def test_the_catalog_and_the_openapi_document(self):
        catalog = self.ok('get', '/catalog').get_json()
        b_catalog = self.ok('get', f'{B}/catalog').get_json()
        names = {q['name'] for q in catalog['queries']} if isinstance(catalog, dict) else {q['name'] for q in catalog}
        b_names = {q['name'] for q in b_catalog['queries']} if isinstance(b_catalog, dict) \
            else {q['name'] for q in b_catalog}
        self.assertNotIn('b_only', names)
        self.assertIn('b_only', b_names)
        self.assertNotIn('/q/b_only', self.ok('get', '/openapi.json').get_json()['paths'])
        self.assertIn('/q/b_only', self.ok('get', f'{B}/openapi.json').get_json()['paths'])
        # a key's own workspace's queries, by its grants (lite - so b_only too); never another workspace's
        b_key_paths = self.ok('get', '/openapi.json', headers={'X-API-Key': self.b_key}).get_json()['paths']
        self.assertIn('/q/b_only', b_key_paths)
        default_key_paths = self.ok('get', '/openapi.json', headers={'X-API-Key': self.default_key}).get_json()['paths']
        self.assertNotIn('/q/b_only', default_key_paths)

    def test_mcp_tools_are_the_callers_workspace(self):
        from queryapigate import apikeys, mcp_server
        app = self.client.application
        admin_tools = {t['name'] for t in mcp_server.list_tools_for(apikeys.OPEN)}
        self.assertNotIn('b_only', admin_tools)  # an administrator over MCP: default
        self.ok('patch', f'{B}/api/v1/api-keys/partner', json={'collections': ['b-reports']})
        b = mcp_server.permission_for_listing(app, self.b_key, '', '10.0.0.1')
        self.assertIn('b_only', {t['name'] for t in mcp_server.list_tools_for(b)})
        result = mcp_server.handle_call(app, self.b_key, '', '10.0.0.1', 'b_only', {})
        self.assertEqual([r['title'] for r in result['structuredContent']['rows']], ['Bravo', 'Bolero', 'Brazil'])

    def test_schemas_collections_and_descriptions(self):
        tables = self.ok('get', f'{B}/connections/lite/schema').get_json()['tables']
        self.assertEqual([t['name'] for t in tables], ['film'])
        self.assertEqual([c['name'] for c in self.ok('get', '/api/v1/collections').get_json()['items']], [])
        self.assertEqual([c['name'] for c in self.ok('get', f'{B}/api/v1/collections').get_json()['items']],
                         ['b-reports'])
        self.ok('patch', f'{B}/api/v1/connections/lite/descriptions',
                json={'items': [{'table': 'film', 'description': "Team B's films"}]})
        b_items = self.ok('get', f'{B}/api/v1/connections/lite/descriptions').get_json()['items']
        self.assertEqual([i['description'] for i in b_items], ["Team B's films"])
        self.assertEqual(self.ok('get', '/api/v1/connections/lite/descriptions').get_json()['items'], [])

    def test_destinations_and_exports(self):
        res = self.ok('post', f'{B}/api/v1/destinations', status=400,
                      json={'name': 'drop', 'url': f'{self.tmp.name}/out/'}).get_json()
        self.assertIn('only in the default workspace', res['error'])  # the server's folders aren't one team's
        self.ok('post', f'{B}/api/v1/destinations', status=201, json={
            'name': 'lake', 'url': 's3://team-b-bucket/exports/', 'region': 'eu-west-1', 'user': 'id',
            'password': 'secret'})
        self.ok('post', f'{B}/api/v1/exports', status=201, json={
            'name': 'nightly', 'query': 'b_only', 'destination': 'lake', 'path': 'films/{run}.parquet'})
        self.assertEqual(self.ok('get', '/api/v1/exports').get_json()['items'], [])
        self.assertEqual(self.ok('get', '/api/v1/destinations').get_json()['items'], [])
        self.ok('get', f'{B}/api/v1/exports/nightly')
        self.ok('get', '/api/v1/exports/nightly', status=404)
        # an export names its own workspace's query: default has no b_only
        self.assertEqual(self.code('post', '/api/v1/exports', 404, json={
            'name': 'x', 'query': 'b_only', 'destination': 'lake', 'path': 'x/{run}.parquet'}),
            'destination_not_found')


class CacheAdminTests(IsolationTestCase):
    def test_each_workspace_manages_only_its_own_cached_responses(self):
        for prefix in ('', B):
            self.ok('post', f'{prefix}/api/v1/queries', status=201, json={
                'name': 'cached', 'description': 'Cached', 'connection_name': 'lite', 'publish': True,
                'cache_ttl': 60, 'sql': 'SELECT title FROM film'})
            self.ok('get', f'{prefix}/q/cached')
        entries = self.ok('get', '/api/v1/cache/entries').get_json()['items']
        b_entries = self.ok('get', f'{B}/api/v1/cache/entries').get_json()['items']
        self.assertEqual((len(entries), len(b_entries)), (1, 1))
        key, b_key = entries[0]['key'], b_entries[0]['key']
        self.assertNotEqual(key, b_key)
        self.ok('get', f'/api/v1/cache/entries/{b_key}', status=404)  # not default's to read
        self.ok('delete', f'/api/v1/cache/entries/{b_key}', status=204)  # nor to evict: a no-op, as for none
        self.assertEqual(len(self.ok('get', f'{B}/api/v1/cache/entries').get_json()['items']), 1)
        self.ok('delete', '/api/v1/cache/entries', status=204)  # default's own, all of them
        self.assertEqual(self.ok('get', '/api/v1/cache/entries').get_json()['items'], [])
        self.assertEqual(len(self.ok('get', f'{B}/api/v1/cache/entries').get_json()['items']), 1)
        self.assertIn('Bravo', self.ok('get', f'{B}/api/v1/cache/entries/{b_key}').get_data(as_text=True))


class SignedInUserTests(IsolationTestCase):
    def test_a_signed_in_user_is_confined_to_their_workspace_not_the_path(self):
        """With no QUERYAPIGATE_JWT_WORKSPACE_CLAIM configured, a signed-in user acts in the default workspace
        whatever the path names (ADR 0023 §4). team-b's role of the same name never lets a default user reach
        team-b's rows by changing the /w/ prefix - the security review's finding (the full matrix is in test_jwt)."""
        from unittest import mock

        from tests.test_jwt import SECRET, bearer, token
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_JWT_SECRET': SECRET, 'QUERYAPIGATE_JWT_ROLE': 'mobile'}):
            for prefix in ('', B):  # a `mobile` role of the same name in each workspace
                self.ok('post', f'{prefix}/api/v1/roles', status=201, json={'name': 'mobile', 'connections': ['lite']})
            user = bearer(token({'sub': 'alice'}))
            self.assertEqual(self.titles('/q/films', headers=user), ['Alpha', 'Beta'])  # default's, the user's own
            self.assertEqual(self.code('get', f'{B}/q/films', 404, headers=user), 'workspace_not_found')

"""The Management API's queries resource (/api/v1, v1.py, BACKLOG #72).

Every /api/v1 response in these tests is validated against the schema /openapi.json publishes for it - the contract
the Console's generated TypeScript types rely on (ADR 0001)."""
import copy
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

import jsonschema

from queryapigate import app as app_module
from queryapigate import create_app, history, openapi, store
from tests.helpers import write_connections

ADMIN = {'X-API-Key': 'admin-key'}


def _to_json_schema(node):
    """OpenAPI 3.0 schema -> JSON Schema: `nullable: true` becomes "or null"; everything else carries over."""
    if isinstance(node, list):
        return [_to_json_schema(item) for item in node]
    if not isinstance(node, dict):
        return node
    node = {key: _to_json_schema(value) for key, value in node.items()}
    if node.pop('nullable', False):
        return {'anyOf': [node, {'type': 'null'}]}
    return node


SPEC = openapi.build_spec('test')
COMPONENTS = _to_json_schema(copy.deepcopy(SPEC['components']))


def response_schema(path, method, status):
    operation = SPEC['paths'][path][method]
    response = operation['responses'][str(status)]
    schema = response.get('content', {}).get('application/json', {}).get('schema')
    return None if schema is None else {**_to_json_schema(schema), 'components': COMPONENTS}


class V1TestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(history.flush)
        self.db_path = os.path.join(self.tmp.name, 'data.db')
        conn = sqlite3.connect(self.db_path)
        conn.execute('CREATE TABLE film (film_id INTEGER PRIMARY KEY, title TEXT)')
        conn.execute("INSERT INTO film VALUES (1, 'Alpha'), (2, 'Beta')")
        conn.commit()
        conn.close()
        patcher = mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': self.tmp.name, 'QUERYAPIGATE_API_KEY': 'admin-key'})
        patcher.start()
        self.addCleanup(patcher.stop)
        write_connections({'lite': {'db': 'sqlite', 'database': self.db_path, 'active': True}})
        self.client = create_app().test_client()

    def call(self, method, url, spec_path, status, headers=ADMIN, **kwargs):
        """Make a request, check its status, and validate the body against the spec's schema for that status."""
        res = getattr(self.client, method)(url, headers=headers, **kwargs)
        self.assertEqual(res.status_code, status, res.get_data(as_text=True))
        schema = response_schema(spec_path, method, status)
        if schema is not None:
            jsonschema.validate(res.get_json(), schema)
        return res

    def create(self, name='films', sql='SELECT film_id, title FROM film ORDER BY film_id', **fields):
        body = {'name': name, 'description': 'All films', 'sql': sql, 'connection_name': 'lite', **fields}
        return self.call('post', '/api/v1/queries', '/api/v1/queries', 201, json=body).get_json()

    def add_version(self, name='films', status=201, headers=None, **fields):
        body = {'description': 'v', 'sql': 'SELECT title FROM film WHERE film_id = :id', 'connection_name': 'lite',
                'parameters': {'id': {'type': 'integer'}}, **fields}
        return self.call('post', f'/api/v1/queries/{name}/versions', '/api/v1/queries/{name}/versions', status,
                         headers={**ADMIN, **(headers or {})}, json=body)


class QueryLifecycleTests(V1TestCase):
    def test_create_starts_as_an_unpublished_draft(self):
        query = self.create()
        self.assertEqual((query['published_version'], query['latest_version'], query['has_draft']), (None, 1, True))
        self.assertEqual(query['versions'][0]['status'], 'draft')
        self.assertEqual(query['endpoint'], '/q/films')
        self.assertEqual(self.client.get('/q/films', headers=ADMIN).status_code, 404)

    def test_create_and_publish_at_once(self):
        query = self.create(publish=True)
        self.assertEqual(query['published_version'], 1)
        self.assertEqual(self.client.get('/q/films', headers=ADMIN).get_json()[0]['title'], 'Alpha')

    def test_draft_publish_and_rollback(self):
        self.create(publish=True)
        query = self.add_version().get_json()
        self.assertEqual([v['status'] for v in query['versions']], ['published', 'draft'])
        self.assertEqual(query['versions'][1]['placeholders'], ['id'])
        self.assertEqual(query['versions'][1]['parameters'], {'id': {'type': 'integer', 'required': True}})

        query = self.call('post', '/api/v1/queries/films/publish', '/api/v1/queries/{name}/publish', 200,
                          json={'version': 2}).get_json()
        self.assertEqual([v['status'] for v in query['versions']], ['previous', 'published'])
        self.assertEqual(self.client.get('/q/films?id=2', headers=ADMIN).get_json(), [{'title': 'Beta'}])

        query = self.call('post', '/api/v1/queries/films/publish', '/api/v1/queries/{name}/publish', 200,
                          json={'version': 1}).get_json()  # roll back
        self.assertEqual([v['status'] for v in query['versions']], ['published', 'draft'])

    def test_unpublish_keeps_every_version(self):
        self.create(publish=True)
        query = self.call('post', '/api/v1/queries/films/unpublish', '/api/v1/queries/{name}/unpublish', 200).get_json()
        self.assertIsNone(query['published_version'])
        self.assertEqual(query['version_count'], 1)
        self.assertEqual(self.client.get('/q/films', headers=ADMIN).status_code, 404)

    def test_changes_are_audited(self):
        self.create()
        self.add_version()
        self.call('post', '/api/v1/queries/films/publish', '/api/v1/queries/{name}/publish', 200, json={'version': 2})
        actions = [e['action'] for e in store.read_audit_log()]
        self.assertEqual(actions[-3:], ['save_query', 'save_query', 'publish_query'])

    def test_get_one_version(self):
        self.create()
        version = self.call('get', '/api/v1/queries/films/versions/1', '/api/v1/queries/{name}/versions/{version}',
                            200).get_json()
        self.assertEqual(version['sql'], 'SELECT film_id, title FROM film ORDER BY film_id')
        res = self.call('get', '/api/v1/queries/films/versions/9', '/api/v1/queries/{name}/versions/{version}', 404)
        self.assertEqual(res.get_json()['code'], 'version_not_found')

    def test_collection_and_cache_ttl_change_in_place(self):
        self.create()
        query = self.call('patch', '/api/v1/queries/films', '/api/v1/queries/{name}', 200,
                          json={'collection': 'catalog'}).get_json()
        self.assertEqual(query['collection'], 'catalog')
        query = self.call('patch', '/api/v1/queries/films/versions/1', '/api/v1/queries/{name}/versions/{version}',
                          200, json={'cache_ttl': 60}).get_json()
        self.assertEqual(query['versions'][0]['cache_ttl'], 60)
        self.assertEqual(query['version_count'], 1)  # not a new version

    def test_delete_a_version_then_the_query(self):
        self.create(publish=True)
        self.add_version()
        query = self.call('delete', '/api/v1/queries/films/versions/2', '/api/v1/queries/{name}/versions/{version}',
                          200).get_json()
        self.assertEqual(query['version_count'], 1)
        self.call('delete', '/api/v1/queries/films', '/api/v1/queries/{name}', 204)
        self.assertFalse(store.saved_query_exists('films'))

    def test_deleting_the_last_version_deletes_the_query(self):
        self.create()
        self.call('delete', '/api/v1/queries/films/versions/1', '/api/v1/queries/{name}/versions/{version}', 204)
        self.assertFalse(store.saved_query_exists('films'))


class ListTests(V1TestCase):
    def test_list_filters(self):
        self.create('a', publish=True, collection='sales', tags=['finance'])
        self.create('b')
        self.create('c', publish=True)
        self.add_version('c')
        res = self.call('get', '/api/v1/queries', '/api/v1/queries', 200)
        self.assertEqual([q['name'] for q in res.get_json()['items']], ['a', 'b', 'c'])

        def names(query):
            return [q['name'] for q in self.call('get', f'/api/v1/queries?{query}', '/api/v1/queries', 200)
                    .get_json()['items']]
        self.assertEqual(names('status=published'), ['a', 'c'])
        self.assertEqual(names('status=unpublished'), ['b'])
        self.assertEqual(names('status=draft'), ['b', 'c'])
        self.assertEqual(names('collection=sales'), ['a'])
        self.assertEqual(names('search=finance'), ['a'])
        self.assertEqual(names('connection=lite'), ['a', 'b', 'c'])
        res = self.call('get', '/api/v1/queries?status=bogus', '/api/v1/queries', 400)
        self.assertEqual(res.get_json()['code'], 'invalid_filter')

class ErrorTests(V1TestCase):
    def test_errors_carry_a_stable_code_and_the_request_id(self):
        res = self.call('get', '/api/v1/queries/nope', '/api/v1/queries/{name}', 404)
        body = res.get_json()
        self.assertEqual(body['code'], 'query_not_found')
        self.assertEqual(body['request_id'], res.headers['X-Request-Id'])

    def test_a_taken_name_is_a_conflict(self):
        self.create()
        res = self.call('post', '/api/v1/queries', '/api/v1/queries', 409,
                        json={'name': 'films', 'description': 'd', 'sql': 'SELECT 1'})
        self.assertEqual(res.get_json()['code'], 'query_exists')

    def test_invalid_definitions_and_unknown_fields_are_refused(self):
        res = self.call('post', '/api/v1/queries', '/api/v1/queries', 400, json={'name': 'x', 'description': 'd'})
        self.assertEqual(res.get_json()['code'], 'invalid_body')
        res = self.call('post', '/api/v1/queries', '/api/v1/queries', 400,
                        json={'name': 'x', 'description': 'd', 'sql': 'SELECT 1', 'filename': 'x'})
        self.assertEqual(res.get_json()['code'], 'unknown_field')
        res = self.call('post', '/api/v1/queries', '/api/v1/queries', 400,
                        json={'name': 'x', 'description': 'd', 'sql': 'SELECT :a', 'parameters': {'a': 'nope'}})
        self.assertIn('a', res.get_json()['errors'])

    def test_scoped_keys_and_anonymous_callers_are_refused(self):
        key = self.client.post('/api/v1/api-keys', json={'name': 'scoped', 'connections': ['lite']},
                               headers=ADMIN).get_json()['secret']
        res = self.call('get', '/api/v1/queries', '/api/v1/queries', 403, headers={'X-API-Key': key})
        self.assertEqual(res.get_json()['code'], 'admin_only')
        res = self.call('get', '/api/v1/queries', '/api/v1/queries', 401, headers={})
        self.assertEqual(res.get_json()['code'], 'unauthorized')

    def test_unknown_v1_routes_answer_in_the_v1_error_shape(self):
        body = self.client.get('/api/v1/nothing-here', headers=ADMIN).get_json()
        self.assertEqual(body['code'], 'not_found')
        self.assertIn('request_id', body)

    def test_runtime_routes_answer_in_the_same_error_shape(self):
        body = self.client.get('/connections/nope/schema', headers=ADMIN).get_json()
        self.assertEqual(body['code'], 'connection_not_found')
        self.assertIn('request_id', body)


class ConcurrencyTests(V1TestCase):
    def test_if_match_refuses_a_change_made_to_a_stale_copy(self):
        etag = self.call('post', '/api/v1/queries', '/api/v1/queries', 201,
                         json={'name': 'films', 'description': 'd', 'sql': 'SELECT 1'}).headers['ETag']
        self.add_version(headers={'If-Match': etag})  # matches: accepted, and changes the ETag
        res = self.add_version(status=412, headers={'If-Match': etag})  # the old copy is stale now
        self.assertEqual(res.get_json()['code'], 'precondition_failed')
        current = self.client.get('/api/v1/queries/films', headers=ADMIN).headers['ETag']
        self.assertNotEqual(current, etag)
        self.call('post', '/api/v1/queries/films/publish', '/api/v1/queries/{name}/publish', 200,
                  headers={**ADMIN, 'If-Match': current}, json={'version': 2})

    def test_an_edit_made_elsewhere_also_invalidates_the_etag(self):
        etag = self.call('post', '/api/v1/queries', '/api/v1/queries', 201,
                         json={'name': 'films', 'description': 'd', 'sql': 'SELECT 1'}).headers['ETag']
        store.set_collection('films', 'x')  # e.g. a bundle import, or another instance
        self.call('post', '/api/v1/queries/films/unpublish', '/api/v1/queries/{name}/unpublish', 412,
                  headers={**ADMIN, 'If-Match': etag})


class ValidateTests(V1TestCase):
    def test_a_valid_definition(self):
        res = self.call('post', '/api/v1/queries/validate', '/api/v1/queries/validate', 200, json={
            'description': 'd', 'sql': 'SELECT f.title FROM film f WHERE f.film_id = :id', 'connection_name': 'lite',
            'parameters': {'id': 'int'}})
        self.assertEqual(res.get_json(), {'valid': True, 'errors': [], 'placeholders': ['id'], 'tables': ['film']})

    def test_every_problem_is_reported_not_raised(self):
        res = self.call('post', '/api/v1/queries/validate', '/api/v1/queries/validate', 200, json={
            'description': 'd', 'sql': 'DELETE FROM film', 'connection_name': 'missing'})
        body = res.get_json()
        self.assertFalse(body['valid'])
        fields = {e.get('field') for e in body['errors']}
        self.assertTrue({'connection_name', 'sql'} <= fields, body)


class HistoryAndConnectionTests(V1TestCase):
    def test_a_querys_history_pages_newest_first(self):
        self.create(publish=True)
        for _ in range(3):
            self.client.get('/q/films', headers=ADMIN)
        page = self.call('get', '/api/v1/queries/films/history?limit=2', '/api/v1/queries/{name}/history',
                         200).get_json()
        self.assertEqual(len(page['items']), 2)
        self.assertEqual({e['query'] for e in page['items']}, {'films'})
        rest = self.call('get', f"/api/v1/queries/films/history?limit=2&cursor={page['next_cursor']}",
                         '/api/v1/queries/{name}/history', 200).get_json()
        self.assertEqual((len(rest['items']), rest['next_cursor']), (1, None))

    def test_connections_and_their_schema(self):
        items = self.call('get', '/api/v1/connections', '/api/v1/connections', 200).get_json()['items']
        self.assertEqual([{k: i[k] for k in ('name', 'db', 'active', 'host', 'port', 'database')} for i in items],
                         [{'name': 'lite', 'db': 'sqlite', 'active': True, 'host': None, 'port': None,
                           'database': self.db_path}])
        self.assertNotIn('password', str(items))
        tables = self.call('get', '/api/v1/connections/lite/schema', '/api/v1/connections/{name}/schema',
                           200).get_json()['tables']
        self.assertEqual([t['name'] for t in tables], ['film'])
        self.assertEqual([c['name'] for c in tables[0]['columns']], ['film_id', 'title'])
        res = self.call('get', '/api/v1/connections/nope/schema', '/api/v1/connections/{name}/schema', 404)
        self.assertEqual(res.get_json()['code'], 'connection_not_found')

    def test_schema_of_another_database_is_passed_through(self):
        # SQLite has one database per file, so asking for another is a clear 400, not silently the default one
        res = self.call('get', '/api/v1/connections/lite/schema?database=other', '/api/v1/connections/{name}/schema',
                        400)
        self.assertIn("Switching databases isn't supported", res.get_json()['error'])


class ConnectionTests(V1TestCase):
    C = '/api/v1/connections/{name}'

    def create(self, name='pg', **fields):
        body = {'name': name, 'db': 'postgres', 'host': 'db.internal', 'port': 5432, 'user': 'app',
                'password': 's3cret', 'database': 'shop', 'options': {'sslmode': 'require'}, **fields}
        return self.call('post', '/api/v1/connections', '/api/v1/connections', 201, json=body)

    def test_create_returns_it_with_the_password_masked(self):
        res = self.create()
        body = res.get_json()
        self.assertEqual((body['name'], body['db'], body['host'], body['user'], body['active']),
                         ('pg', 'postgres', 'db.internal', 'app', True))
        self.assertEqual(body['password'], '********')
        self.assertEqual(body['options'], {'sslmode': 'require'})
        self.assertNotIn('s3cret', res.get_data(as_text=True))
        self.assertIn('ETag', res.headers)
        self.assertEqual(store.read_connections()['pg']['password'], 's3cret')  # stored for real
        self.assertEqual(store.read_audit_log()[-1]['action'], 'create_connection')
        self.assertNotIn('s3cret', str(store.read_audit_log()[-1]))

    def test_a_taken_name_and_a_bad_body_are_refused(self):
        self.create()
        self.assertEqual(self.call('post', '/api/v1/connections', '/api/v1/connections', 409,
                                   json={'name': 'pg', 'db': 'sqlite'}).get_json()['code'], 'connection_exists')
        self.assertEqual(self.call('post', '/api/v1/connections', '/api/v1/connections', 400,
                                   json={'name': 'x', 'db': 'oracle'}).get_json()['code'], 'invalid_body')
        self.assertEqual(self.call('post', '/api/v1/connections', '/api/v1/connections', 400,
                                   json={'name': '../x', 'db': 'sqlite'}).get_json()['code'], 'invalid_name')

    def test_patch_merges_and_keeps_what_it_does_not_mention(self):
        self.create()
        res = self.call('patch', '/api/v1/connections/pg', self.C, 200, json={'host': 'db2.internal', 'active': False})
        body = res.get_json()
        self.assertEqual((body['host'], body['active'], body['user']), ('db2.internal', False, 'app'))
        self.assertEqual(body['options'], {'sslmode': 'require'})  # a driver option the form never shows survives
        stored = store.read_connections()['pg']
        self.assertEqual(stored['password'], 's3cret')  # left out: kept
        self.call('patch', '/api/v1/connections/pg', self.C, 200, json={'password': '********'})
        self.assertEqual(store.read_connections()['pg']['password'], 's3cret')  # the mask: kept
        self.call('patch', '/api/v1/connections/pg', self.C, 200,
                  json={'password': 'n3w', 'options': {'sslmode': None}})
        stored = store.read_connections()['pg']
        self.assertEqual(stored['password'], 'n3w')
        self.assertNotIn('sslmode', stored)
        changes = store.read_audit_log()[-1]['changes']
        self.assertEqual(changes['password'], 'changed')

    def test_patch_honours_if_match(self):
        etag = self.create().headers['ETag']
        self.call('patch', '/api/v1/connections/pg', self.C, 200, headers={**ADMIN, 'If-Match': etag},
                  json={'user': 'other'})
        res = self.call('patch', '/api/v1/connections/pg', self.C, 412, headers={**ADMIN, 'If-Match': etag},
                        json={'user': 'again'})
        self.assertEqual(res.get_json()['code'], 'precondition_failed')

    def test_delete_needs_a_reason_and_appears_in_deleted(self):
        self.create()
        res = self.call('delete', '/api/v1/connections/pg', self.C, 400, json={})
        self.assertEqual(res.get_json()['code'], 'reason_required')
        self.call('delete', '/api/v1/connections/pg', self.C, 204, json={'reason': 'moved to the new cluster'})
        self.assertNotIn('pg', store.read_connections())
        deleted = self.call('get', '/api/v1/connections/deleted', '/api/v1/connections/deleted',
                            200).get_json()['items']
        self.assertEqual((deleted[0]['name'], deleted[0]['reason'], deleted[0]['host']),
                         ('pg', 'moved to the new cluster', 'db.internal'))

    def test_test_and_databases_probe_without_saving(self):
        res = self.call('post', '/api/v1/connections/test', '/api/v1/connections/test', 200,
                        json={'db': 'sqlite', 'database': self.db_path})
        self.assertIn('elapsed_ms', res.get_json())
        res = self.call('post', '/api/v1/connections/test', '/api/v1/connections/test', 200, json={'name': 'lite'})
        self.assertIn('elapsed_ms', res.get_json())
        res = self.call('post', '/api/v1/connections/test', '/api/v1/connections/test', 502,
                        json={'db': 'sqlite', 'database': os.path.join(self.tmp.name, 'nope', 'x.db')})
        self.assertEqual(res.get_json()['code'], 'connection_failed')
        self.assertEqual(set(store.read_connections()), {'lite'})  # nothing saved

    def test_get_one_and_its_example_flag(self):
        self.create()
        body = self.call('get', '/api/v1/connections/pg', self.C, 200).get_json()
        self.assertFalse(body['example'])
        self.assertEqual(body['usage']['queries'], 0)
        res = self.call('get', '/api/v1/connections/nope', self.C, 404)
        self.assertEqual(res.get_json()['code'], 'connection_not_found')


class AccessTests(V1TestCase):
    K = '/api/v1/api-keys/{name}'
    R = '/api/v1/roles/{name}'

    def create_key(self, status=201, **fields):
        body = {'name': 'reporting', 'connections': ['lite'], **fields}
        return self.call('post', '/api/v1/api-keys', '/api/v1/api-keys', status, json=body)

    def create_role(self, name='analyst', **fields):
        body = {'name': name, 'connections': ['lite'], 'rate_limit': '100/minute', **fields}
        return self.call('post', '/api/v1/roles', '/api/v1/roles', 201, json=body)

    def test_create_shows_the_secret_once_and_never_again(self):
        res = self.create_key(expires_at='2999-01-01')
        body = res.get_json()
        self.assertTrue(body['secret'].startswith('sk_'))
        self.assertEqual(res.headers['Cache-Control'], 'no-store')
        self.assertEqual((body['connections'], body['active'], body['expired'], body['expires_at']),
                         (['lite'], True, False, '2999-01-01'))
        self.assertEqual(set(body['usage']), {'queries', 'errors', 'rows'})  # live counters, process-wide
        listed = self.call('get', '/api/v1/api-keys', '/api/v1/api-keys', 200)
        self.assertNotIn(body['secret'], listed.get_data(as_text=True))
        one = self.call('get', '/api/v1/api-keys/reporting', self.K, 200)
        self.assertNotIn('secret', one.get_json())
        self.assertNotIn(body['secret'], str(store.read_audit_log()))
        self.assertEqual(store.read_audit_log()[-1]['action'], 'create_key')
        # the secret works, for what it grants
        run = self.client.post('/execute_sql', headers={'X-API-Key': body['secret']},
                               json={'sql': 'SELECT 1 AS one', 'connection_name': 'lite'})
        self.assertEqual(run.status_code, 200, run.get_data(as_text=True))

    def test_bad_names_taken_names_and_unknown_fields_are_refused(self):
        self.create_key()
        self.assertEqual(self.create_key(409).get_json()['code'], 'key_exists')
        self.assertEqual(self.create_key(400, name='../x').get_json()['code'], 'invalid_name')
        self.assertEqual(self.create_key(400, name='k2', secret='mine').get_json()['code'], 'unknown_field')
        self.assertEqual(self.create_key(404, name='k3', connections=None, role='nope').get_json()['code'],
                         'role_not_found')
        res = self.call('get', '/api/v1/api-keys/nope', self.K, 404)
        self.assertEqual(res.get_json()['code'], 'key_not_found')

    def test_patch_changes_what_it_names_and_null_clears(self):
        etag = self.create_key(rate_limit='10/minute', allowed_ips=['10.0.0.0/8']).headers['ETag']
        res = self.call('patch', '/api/v1/api-keys/reporting', self.K, 200, headers={**ADMIN, 'If-Match': etag},
                        json={'allow_writes': True, 'rate_limit': None})
        body = res.get_json()
        self.assertEqual((body['allow_writes'], body['rate_limit'], body['allowed_ips'], body['connections']),
                         (True, None, ['10.0.0.0/8'], ['lite']))
        self.assertEqual(store.read_audit_log()[-1]['action'], 'update_key')
        stale = self.call('patch', '/api/v1/api-keys/reporting', self.K, 412, headers={**ADMIN, 'If-Match': etag},
                          json={'active': False})
        self.assertEqual(stale.get_json()['code'], 'precondition_failed')
        self.assertFalse(self.call('patch', '/api/v1/api-keys/reporting', self.K, 200,
                                   json={'active': False}).get_json()['active'])

    def test_delete_revokes(self):
        secret = self.create_key().get_json()['secret']
        self.call('delete', '/api/v1/api-keys/reporting', self.K, 204)
        self.assertEqual(self.client.get('/catalog', headers={'X-API-Key': secret}).status_code, 401)
        self.assertEqual(store.read_audit_log()[-1]['action'], 'delete_key')
        self.call('delete', '/api/v1/api-keys/reporting', self.K, 404)

    def test_a_key_from_a_role_copies_its_grants_once(self):
        role = self.create_role().get_json()
        self.assertEqual((role['connections'], role['rate_limit'], role['keys_created']),
                         (['lite'], '100/minute', 0))
        key = self.create_key(connections=None, role='analyst').get_json()
        self.assertEqual((key['created_from_role'], key['rate_limit']), ('analyst', '100/minute'))
        self.assertEqual(self.call('get', '/api/v1/roles/analyst', self.R, 200).get_json()['keys_created'], 1)
        # combining a role with explicit grants is refused
        self.create_key(400, name='k2', role='analyst')
        # editing, then deleting, the role leaves the key alone
        self.call('patch', '/api/v1/roles/analyst', self.R, 200, json={'rate_limit': '5/minute'})
        self.call('delete', '/api/v1/roles/analyst', self.R, 204)
        self.assertEqual(self.call('get', '/api/v1/api-keys/reporting', self.K, 200).get_json()['rate_limit'],
                         '100/minute')
        self.assertEqual([e['action'] for e in store.read_audit_log()][-4:],
                         ['create_role', 'create_key', 'update_role', 'delete_role'])

    def test_roles_list_conflicts_and_if_match(self):
        etag = self.create_role().headers['ETag']
        self.create_role('writer', allow_writes=True)
        items = self.call('get', '/api/v1/roles', '/api/v1/roles', 200).get_json()['items']
        self.assertEqual([r['name'] for r in items], ['analyst', 'writer'])
        res = self.call('post', '/api/v1/roles', '/api/v1/roles', 409, json={'name': 'analyst'})
        self.assertEqual(res.get_json()['code'], 'role_exists')
        res = self.call('patch', '/api/v1/roles/analyst', self.R, 400, json={'expires_at': '2999-01-01'})
        self.assertEqual(res.get_json()['code'], 'unknown_field')
        self.call('patch', '/api/v1/roles/analyst', self.R, 200, json={'allowed_ips': ['10.0.0.1']})
        self.call('delete', '/api/v1/roles/analyst', self.R, 412, headers={**ADMIN, 'If-Match': etag})
        res = self.call('get', '/api/v1/roles/nope', self.R, 404)
        self.assertEqual(res.get_json()['code'], 'role_not_found')

    def test_scoped_keys_cannot_manage_access(self):
        secret = self.create_key().get_json()['secret']
        self.call('get', '/api/v1/api-keys', '/api/v1/api-keys', 403, headers={'X-API-Key': secret})
        self.call('post', '/api/v1/roles', '/api/v1/roles', 403, headers={'X-API-Key': secret}, json={'name': 'x'})


class HistoryAndAuditTests(V1TestCase):
    def test_history_across_queries_pages_and_filters(self):
        self.create('films', publish=True)
        self.create('titles', sql='SELECT title FROM film', publish=True)
        for name in ('films', 'titles', 'films'):
            self.assertEqual(self.client.get(f'/q/{name}', headers=ADMIN).status_code, 200)
        history.flush()
        page = self.call('get', '/api/v1/history?limit=2', '/api/v1/history', 200).get_json()
        self.assertEqual(len(page['items']), 2)
        self.assertIsNotNone(page['next_cursor'])
        rest = self.call('get', f"/api/v1/history?limit=10&cursor={page['next_cursor']}", '/api/v1/history',
                         200).get_json()
        self.assertIsNone(rest['next_cursor'])
        everything = page['items'] + rest['items']
        self.assertEqual({e['query'] for e in everything}, {'films', 'titles'})
        only = self.call('get', '/api/v1/history?query=titles', '/api/v1/history', 200).get_json()['items']
        self.assertEqual([e['query'] for e in only], ['titles'])
        res = self.call('get', '/api/v1/history?status=maybe', '/api/v1/history', 400)
        self.assertEqual(res.get_json()['code'], 'invalid_filter')

    def test_audit_newest_first_with_filters(self):
        self.create('films')
        self.call('post', '/api/v1/roles', '/api/v1/roles', 201, json={'name': 'analyst'})
        self.call('delete', '/api/v1/roles/analyst', '/api/v1/roles/{name}', 204)
        log = self.call('get', '/api/v1/audit', '/api/v1/audit', 200).get_json()
        self.assertEqual([e['action'] for e in log['items']][:2], ['delete_role', 'create_role'])
        self.assertEqual(log['total'], len(log['items']))
        self.assertIn('create_role', log['actions'])
        self.assertEqual(log['retention'], 500)
        only = self.call('get', '/api/v1/audit?action=create_role', '/api/v1/audit', 200).get_json()
        self.assertEqual([e['target'] for e in only['items']], ['analyst'])
        self.assertEqual(only['total'], log['total'])  # the stored total, before filtering
        self.assertEqual(only['actions'], log['actions'])
        text = self.call('get', '/api/v1/audit?q=ANALY', '/api/v1/audit', 200).get_json()['items']
        self.assertEqual({e['target'] for e in text}, {'analyst'})

    def test_both_are_admin_only(self):
        secret = self.call('post', '/api/v1/api-keys', '/api/v1/api-keys', 201,
                           json={'name': 'k', 'connections': ['lite']}).get_json()['secret']
        for path in ('/api/v1/audit', '/api/v1/history'):
            self.call('get', path, path, 403, headers={'X-API-Key': secret})


class SettingsAndMcpTests(V1TestCase):
    def test_settings_by_section_never_with_a_secret(self):
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_SECRET_KEY': 'k' * 44, 'QUERYAPIGATE_QUERY_TIMEOUT': '45'}):
            res = self.call('get', '/api/v1/settings', '/api/v1/settings', 200)
        sections = res.get_json()['items']
        self.assertIn('mcp', [s['id'] for s in sections])
        rows = {r['env']: r for s in sections for r in s['rows']}
        timeout = rows['QUERYAPIGATE_QUERY_TIMEOUT']
        self.assertEqual((timeout['source'], timeout['env_value']), ('env', '45'))
        self.assertIsNone(rows['QUERYAPIGATE_API_KEY']['env_value'])  # a secret: never exported
        self.assertNotIn('admin-key', res.get_data(as_text=True))
        self.assertNotIn('k' * 44, res.get_data(as_text=True))

    def test_mcp_status_and_tools(self):
        self.create('films', publish=True)
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_MCP_PORT': '1'}):  # nothing listens on port 1
            status = self.call('get', '/api/v1/mcp/status', '/api/v1/mcp/status', 200).get_json()
        self.assertEqual(status, {'reachable': False, 'port': 1})
        tools = {t['name']: t for t in self.call('get', '/api/v1/mcp/tools', '/api/v1/mcp/tools',
                                                 200).get_json()['items']}
        self.assertEqual(tools['execute_sql']['kind'], 'ad-hoc')
        self.assertEqual((tools['films']['kind'], tools['films']['read_only']), ('saved query', True))

    def test_all_three_are_admin_only(self):
        secret = self.call('post', '/api/v1/api-keys', '/api/v1/api-keys', 201,
                           json={'name': 'k', 'connections': ['lite']}).get_json()['secret']
        for path in ('/api/v1/settings', '/api/v1/mcp/status', '/api/v1/mcp/tools'):
            self.call('get', path, path, 403, headers={'X-API-Key': secret})


class SummaryFlowAndCacheTests(V1TestCase):
    def test_a_summary_says_when_it_was_created_and_last_used_and_its_cache_ttl(self):
        self.create('films', publish=True, cache_ttl=30)
        item = self.call('get', '/api/v1/queries', '/api/v1/queries', 200).get_json()['items'][0]
        self.assertIsNotNone(item['created_at'])
        self.assertIsNone(item['last_used_at'])
        self.assertEqual(item['cache_ttl'], 30)
        self.client.get('/q/films', headers=ADMIN)
        item = self.call('get', '/api/v1/queries', '/api/v1/queries', 200).get_json()['items'][0]
        self.assertIsNotNone(item['last_used_at'])
        one = self.call('get', '/api/v1/queries/films', '/api/v1/queries/{name}', 200).get_json()
        self.assertEqual(one['last_used_at'], item['last_used_at'])
        self.assertEqual([v['run_count'] for v in one['versions']], [1])

    def test_flow_names_the_tables_a_version_touches(self):
        self.create('films')
        flow = self.call('get', '/api/v1/queries/films/versions/1/flow',
                         '/api/v1/queries/{name}/versions/{version}/flow', 200).get_json()
        self.assertEqual(flow['tables'], ['film'])
        self.assertIsNone(flow['error'])
        res = self.call('get', '/api/v1/queries/films/versions/9/flow',
                        '/api/v1/queries/{name}/versions/{version}/flow', 404)
        self.assertEqual(res.get_json()['code'], 'version_not_found')

    def test_cache_entries_list_read_evict_and_clear(self):
        self.create('films', publish=True, cache_ttl=60)
        live = self.client.get('/q/films', headers=ADMIN)
        items = self.call('get', '/api/v1/cache/entries', '/api/v1/cache/entries', 200).get_json()['items']
        self.assertEqual([(e['meta']['name'], e['meta']['version']) for e in items], [('films', 1)])
        key = items[0]['key']
        body = self.call('get', f'/api/v1/cache/entries/{key}', '/api/v1/cache/entries/{key}', 200)
        self.assertEqual((body.content_type, body.get_data()), (live.content_type, live.get_data()))
        self.call('delete', f'/api/v1/cache/entries/{key}', '/api/v1/cache/entries/{key}', 204)
        res = self.call('get', f'/api/v1/cache/entries/{key}', '/api/v1/cache/entries/{key}', 404)
        self.assertEqual(res.get_json()['code'], 'cache_entry_not_found')
        self.client.get('/q/films', headers=ADMIN)
        self.call('delete', '/api/v1/cache/entries', '/api/v1/cache/entries', 204)
        after = self.call('get', '/api/v1/cache/entries', '/api/v1/cache/entries', 200).get_json()
        self.assertEqual(after['items'], [])


class CollectionAndExampleTests(V1TestCase):
    C = '/api/v1/collections/{name}'

    def test_list_shows_queries_and_who_reaches_each_collection(self):
        self.create('films', collection='catalog')
        self.create('titles', sql='SELECT title FROM film')
        self.call('post', '/api/v1/api-keys', '/api/v1/api-keys', 201,
                  json={'name': 'partner', 'connections': [], 'collections': ['catalog']})
        listing = self.call('get', '/api/v1/collections', '/api/v1/collections', 200).get_json()
        self.assertEqual(listing['items'], [{'name': 'catalog', 'queries': ['films'], 'keys': ['partner'],
                                             'roles': []}])
        self.assertEqual(listing['uncollected'], ['titles'])

    def test_rename_moves_queries_and_grants_and_refuses_a_taken_name(self):
        self.create('films', collection='catalog')
        self.create('titles', sql='SELECT title FROM film', collection='other')
        self.call('post', '/api/v1/api-keys', '/api/v1/api-keys', 201,
                  json={'name': 'partner', 'connections': [], 'collections': ['catalog']})
        res = self.call('patch', '/api/v1/collections/catalog', self.C, 409, json={'name': 'other'})
        self.assertEqual(res.get_json()['code'], 'collection_exists')
        self.assertEqual(self.call('patch', '/api/v1/collections/nope', self.C, 404,
                                   json={'name': 'x'}).get_json()['code'], 'collection_not_found')
        self.assertEqual(self.call('patch', '/api/v1/collections/catalog', self.C, 400,
                                   json={'name': 'Bad Name'}).get_json()['code'], 'invalid_name')
        body = self.call('patch', '/api/v1/collections/catalog', self.C, 200, json={'name': 'films-v2'}).get_json()
        self.assertEqual(body, {'name': 'films-v2', 'moved': {'queries': ['films'], 'keys': ['partner'], 'roles': []}})
        key = self.call('get', '/api/v1/api-keys/partner', '/api/v1/api-keys/{name}', 200).get_json()
        self.assertEqual(key['collections'], ['films-v2'])
        self.assertEqual(store.read_audit_log()[-1]['action'], 'rename_collection')
        self.call('patch', '/api/v1/collections/films-v2', self.C, 200, json={'name': 'other', 'merge': True})
        names = [c['name'] for c in self.call('get', '/api/v1/collections', '/api/v1/collections', 200)
                 .get_json()['items']]
        self.assertEqual(names, ['other'])

    def test_postman_export(self):
        self.create('films', collection='catalog', publish=True)
        res = self.call('get', '/api/v1/collections/catalog/postman', '/api/v1/collections/{name}/postman', 200)
        self.assertIn('catalog.postman_collection.json', res.headers['Content-Disposition'])
        self.assertEqual(res.get_json()['info']['name'], 'catalog')
        self.call('get', '/api/v1/collections/nope/postman', '/api/v1/collections/{name}/postman', 404)

    def test_examples_load_status_and_unload(self):
        status = self.call('get', '/api/v1/examples', '/api/v1/examples', 200).get_json()
        self.assertEqual((status['loaded'], status['partial']), (False, False))
        res = self.call('post', '/api/v1/examples', '/api/v1/examples', 200)
        loaded = res.get_json()
        self.assertEqual(res.headers['Cache-Control'], 'no-store')
        self.assertTrue(loaded['status']['loaded'])
        self.assertTrue(loaded['added']['queries'])
        self.assertEqual(sorted(loaded['key_secrets']), loaded['added']['keys'])
        self.assertNotIn(next(iter(loaded['key_secrets'].values()), 'none'), str(store.read_audit_log()))
        again = self.call('post', '/api/v1/examples', '/api/v1/examples', 200).get_json()
        self.assertEqual(again['added']['queries'], [])  # idempotent
        removed = self.call('delete', '/api/v1/examples', '/api/v1/examples', 200).get_json()
        self.assertTrue(removed['removed']['connection'])
        self.assertFalse(removed['status']['loaded'] or removed['status']['partial'])


class DeprecationTests(V1TestCase):
    def test_the_legacy_management_routes_are_gone(self):
        for method, path in (('get', '/list_files'), ('patch', '/save_sql_to_file'), ('get', '/connections'),
                             ('get', '/api_keys'), ('get', '/roles'), ('get', '/audit_log'), ('get', '/history'),
                             ('get', '/settings'), ('get', '/cache/entries'), ('get', '/collections'),
                             ('get', '/examples'), ('get', '/query_flow')):
            res = getattr(self.client, method)(path, headers=ADMIN)
            self.assertIn(res.status_code, (404, 405), f'{method} {path}')
        # what stays: the runtime routes, and browsing a schema (a scoped key may, for a connection it is granted)
        self.assertEqual(self.client.get('/connections/lite/schema', headers=ADMIN).status_code, 200)

    def test_a_deprecated_route_says_so_in_its_headers(self):
        # none today - the mechanism a 1.x deprecation will use
        with mock.patch.dict(app_module.DEPRECATED_ENDPOINTS, {'api.catalog': '/api/v1/somewhere'}):
            res = self.client.get('/catalog', headers=ADMIN)
        self.assertEqual(res.headers['Deprecation'], 'true')
        self.assertIn('</api/v1/somewhere>; rel="successor-version"', res.headers['Link'])
        self.assertNotIn('Deprecation', self.client.get('/catalog', headers=ADMIN).headers)

    def test_the_headers_and_the_spec_name_the_same_operations(self):
        flagged = {(path, method) for path, item in SPEC['paths'].items() for method, op in item.items()
                   if isinstance(op, dict) and op.get('deprecated')}
        app = create_app()
        by_endpoint = {}
        for rule in app.url_map.iter_rules():
            for method in rule.methods - {'HEAD', 'OPTIONS'}:
                path = rule.rule.replace('<', '{').replace('>', '}')
                by_endpoint.setdefault(rule.endpoint, set()).add((path, method.lower()))
        from_headers = set().union(set(), *(by_endpoint[e] for e in app_module.DEPRECATED_ENDPOINTS))
        self.assertEqual(flagged, from_headers)

if __name__ == '__main__':
    unittest.main()

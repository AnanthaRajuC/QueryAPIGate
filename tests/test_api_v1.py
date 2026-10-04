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

    def test_a_legacy_saved_query_appears_published(self):
        self.client.patch('/save_sql_to_file', headers=ADMIN, json={
            'author': 'a', 'description': 'legacy', 'sql_query': 'SELECT 1', 'filename': 'old'})
        item = self.call('get', '/api/v1/queries', '/api/v1/queries', 200).get_json()['items'][0]
        self.assertEqual((item['name'], item['published_version'], item['has_draft']), ('old', 1, False))


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
        self.assertEqual(res.get_json()['code'], 'invalid_request')
        res = self.call('post', '/api/v1/queries', '/api/v1/queries', 400,
                        json={'name': 'x', 'description': 'd', 'sql': 'SELECT 1', 'filename': 'x'})
        self.assertEqual(res.get_json()['code'], 'unknown_field')
        res = self.call('post', '/api/v1/queries', '/api/v1/queries', 400,
                        json={'name': 'x', 'description': 'd', 'sql': 'SELECT :a', 'parameters': {'a': 'nope'}})
        self.assertIn('a', res.get_json()['errors'])

    def test_scoped_keys_and_anonymous_callers_are_refused(self):
        key = self.client.post('/api_keys', json={'name': 'scoped', 'connections': ['lite']},
                               headers=ADMIN).get_json()['key']
        res = self.call('get', '/api/v1/queries', '/api/v1/queries', 403, headers={'X-API-Key': key})
        self.assertEqual(res.get_json()['code'], 'forbidden')
        res = self.call('get', '/api/v1/queries', '/api/v1/queries', 401, headers={})
        self.assertEqual(res.get_json()['code'], 'unauthorized')

    def test_unknown_v1_routes_answer_in_the_v1_error_shape(self):
        body = self.client.get('/api/v1/nothing-here', headers=ADMIN).get_json()
        self.assertEqual(body['code'], 'not_found')
        self.assertIn('request_id', body)

    def test_legacy_routes_keep_their_error_shape(self):
        body = self.client.get('/connections/nope/schema', headers=ADMIN).get_json()
        self.assertNotIn('code', body)


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

    def test_a_legacy_edit_also_invalidates_the_etag(self):
        etag = self.call('post', '/api/v1/queries', '/api/v1/queries', 201,
                         json={'name': 'films', 'description': 'd', 'sql': 'SELECT 1'}).headers['ETag']
        self.client.put('/saved_sql/films/collection', headers=ADMIN, json={'collection': 'x'})
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
        self.assertEqual(items, [{'name': 'lite', 'db': 'sqlite', 'active': True, 'host': None, 'port': None,
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


class DeprecationTests(V1TestCase):
    def test_replaced_legacy_routes_say_so_in_headers_and_in_the_spec(self):
        res = self.client.get('/list_files', headers=ADMIN)
        self.assertEqual(res.headers['Deprecation'], 'true')
        self.assertIn('/api/v1/queries', res.headers['Link'])
        self.assertNotIn('Deprecation', self.client.get('/catalog', headers=ADMIN).headers)
        # The two lists - headers (app.py) and the spec (openapi.py) - name the same operations
        flagged = {(path, method) for path, item in SPEC['paths'].items() for method, op in item.items()
                   if isinstance(op, dict) and op.get('deprecated')}
        app = create_app()
        by_endpoint = {}
        for rule in app.url_map.iter_rules():
            for method in rule.methods - {'HEAD', 'OPTIONS'}:
                path = rule.rule.replace('<', '{').replace('>', '}')
                by_endpoint.setdefault(rule.endpoint, set()).add((path, method.lower()))
        from_headers = set().union(*(by_endpoint[e] for e in app_module.DEPRECATED_ENDPOINTS))
        self.assertEqual(flagged, from_headers)


if __name__ == '__main__':
    unittest.main()

"""Tests for QueryAPIGate. Run from the repository root:  python -m unittest discover -s tests -t ."""
import json
import logging
import os
import re
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import duckdb

from queryapigate import config, create_app, db, engine, history, metrics, pool, runners, schema, sqltools, store
from queryapigate.errors import ApiError
from queryapigate.formats import ResultSetDTO
from tests import TEST_DATABASE_URL
from tests.helpers import put_connections, save_query, write_connections


class ApiTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        tmp = self.tmp.name

        self.db_path = os.path.join(tmp, 'test.db')
        conn = sqlite3.connect(self.db_path)
        conn.execute('CREATE TABLE actor (actor_id INTEGER, name TEXT, born TEXT)')
        conn.executemany('INSERT INTO actor VALUES (?, ?, ?)',
                         [(i, f'Actor {i}', f'19{i:02d}-01-01') for i in range(1, 26)])
        conn.commit()
        conn.close()

        self.saved_dir = os.path.join(tmp, 'saved_sql')

        patcher = mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': tmp})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(history.flush)  # runs first: batched runs land before the home is removed, not during
        for name in ('QUERYAPIGATE_API_KEY', 'QUERYAPIGATE_ALLOW_WRITES', 'QUERYAPIGATE_MAX_PAGE_SIZE'):
            os.environ.pop(name, None)

        write_connections({
            'lite': {'db': 'sqlite', 'database': self.db_path, 'active': True},
            'off': {'db': 'sqlite', 'database': self.db_path, 'active': False},
            'pg': {'db': 'postgres', 'host': 'h', 'user': 'u', 'password': 'secret', 'database': 'd',
                   'active': True},
        })

        self.client = create_app().test_client()

    def run_sql(self, sql, query='', **body):
        return self.client.post(f'/execute_sql{query}', json={'sql': sql, 'connection_name': 'lite', **body})

    def save(self, filename='q', sql='SELECT * FROM actor ORDER BY actor_id', headers=None, **extra):
        """Save (or add a published version to) a saved query through /api/v1 - 201 when saved."""
        return save_query(self.client, {'author': 'a', 'description': 'd', 'sql_query': sql, 'filename': filename,
                                        **extra}, headers=headers)

    def names(self, **filters):
        return [q['name'] for q in self.client.get('/api/v1/queries', query_string=filters).get_json()['items']]

    def connections(self, headers=None):
        """Every connection's detail, by name, as /api/v1 shows it (passwords masked)."""
        out = {}
        for item in self.client.get('/api/v1/connections', headers=headers).get_json()['items']:
            out[item['name']] = self.client.get(f"/api/v1/connections/{item['name']}", headers=headers).get_json()
        return out


class ExecuteSqlTests(ApiTestCase):
    def test_json_default_and_pagination(self):
        res = self.run_sql('SELECT * FROM actor ORDER BY actor_id')
        rows = res.get_json()
        self.assertEqual(len(rows), 10)
        self.assertEqual(list(rows[0]), ['actor_id', 'name', 'born'])  # column order preserved
        self.assertEqual(rows[0]['name'], 'Actor 1')

        rows = self.run_sql('SELECT * FROM actor ORDER BY actor_id', '?page=3&page_size=10').get_json()
        self.assertEqual([r['actor_id'] for r in rows], [21, 22, 23, 24, 25])

    def test_the_statements_own_limit_is_kept_and_paged_within(self):
        # BACKLOG #74: this used to assert the opposite - the page size replaced the query's own LIMIT
        rows = self.run_sql('SELECT actor_id FROM actor ORDER BY actor_id LIMIT 2;', '?page_size=4').get_json()
        self.assertEqual([r['actor_id'] for r in rows], [1, 2])
        rows = self.run_sql('select actor_id from actor order by actor_id limit 3 offset 1',
                            '?page_size=4').get_json()
        self.assertEqual([r['actor_id'] for r in rows], [2, 3, 4])

    def test_trailing_line_comment_does_not_swallow_limit(self):
        rows = self.run_sql('SELECT actor_id FROM actor -- everything', '?page_size=3').get_json()
        self.assertEqual(len(rows), 3)

    def test_formats(self):
        csv_res = self.run_sql('SELECT actor_id, name FROM actor ORDER BY actor_id', '?format=csv&page_size=2')
        self.assertEqual(csv_res.mimetype, 'text/csv')
        self.assertEqual(csv_res.get_data(as_text=True).splitlines(), ['actor_id,name', '1,Actor 1', '2,Actor 2'])

        tsv_res = self.run_sql('SELECT actor_id, name FROM actor ORDER BY actor_id', '?format=tsv&page_size=1')
        self.assertEqual(tsv_res.get_data(as_text=True).splitlines(), ['actor_id\tname', '1\tActor 1'])

        xml_res = self.run_sql('SELECT actor_id, name FROM actor ORDER BY actor_id', '?format=xml&page_size=1')
        self.assertEqual(xml_res.get_data(as_text=True),
                         '<data><item><actor_id>1</actor_id><name>Actor 1</name></item></data>')

        yaml_res = self.run_sql('SELECT actor_id, name FROM actor ORDER BY actor_id', '?format=yaml&page_size=1')
        self.assertEqual(yaml_res.get_data(as_text=True), '- actor_id: 1\n  name: Actor 1\n')

        xlsx_res = self.run_sql('SELECT actor_id FROM actor', '?format=xlsx')
        self.assertTrue(xlsx_res.get_data().startswith(b'PK'))
        self.assertIn('result.xlsx', xlsx_res.headers['Content-Disposition'])

    def test_format_from_body_and_bad_format(self):
        res = self.run_sql('SELECT actor_id FROM actor', format='csv')
        self.assertEqual(res.mimetype, 'text/csv')
        self.assertEqual(self.run_sql('SELECT 1', '?format=pdf').status_code, 400)

    def test_xml_sanitises_column_names_and_duplicate_columns_are_kept(self):
        res = self.run_sql('SELECT actor_id AS "1 id", actor_id, actor_id FROM actor', '?format=json&page_size=1')
        self.assertEqual(res.get_json(), [{'1 id': 1, 'actor_id': 1, 'actor_id_2': 1}])
        xml = self.run_sql('SELECT actor_id AS "1 id" FROM actor', '?format=xml&page_size=1')
        self.assertEqual(xml.get_data(as_text=True), '<data><item><_1_id>1</_1_id></item></data>')

    def test_empty_result(self):
        res = self.run_sql('SELECT * FROM actor WHERE actor_id < 0')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json(), {'message': 'No results returned'})

    def test_non_select_statements_are_paged_without_limit_rewriting(self):
        # EXPLAIN can't take a LIMIT clause, so it is fetched whole and sliced instead
        res = self.run_sql('EXPLAIN QUERY PLAN SELECT * FROM actor', '?page_size=5')
        self.assertEqual(res.status_code, 200)

    def test_validation_errors(self):
        self.assertEqual(self.client.post('/execute_sql', json={'connection_name': 'lite'}).status_code, 400)
        self.assertEqual(self.client.post('/execute_sql', json={'sql': 'SELECT 1'}).status_code, 400)
        self.assertEqual(self.client.post('/execute_sql', data='nope').status_code, 400)
        for query in ('?page=abc', '?page=0', '?page_size=-1', '?page_size=100000'):
            self.assertEqual(self.run_sql('SELECT 1', query).status_code, 400, query)

    def test_connection_errors(self):
        res = self.client.post('/execute_sql', json={'sql': 'SELECT 1', 'connection_name': 'nope'})
        self.assertEqual(res.status_code, 404)
        res = self.client.post('/execute_sql', json={'sql': 'SELECT 1', 'connection_name': 'off'})
        self.assertEqual(res.status_code, 403)

    def test_bad_sql_returns_500_with_detail(self):
        res = self.run_sql('SELECT * FROM missing_table')
        self.assertEqual(res.status_code, 500)
        self.assertIn('missing_table', res.get_json()['detail'])


class ReadOnlyTests(ApiTestCase):
    def test_writes_and_multiple_statements_are_rejected(self):
        expected = {
            'DELETE FROM actor': 403,
            'DROP TABLE actor': 403,
            "INSERT INTO actor VALUES (99, 'x', 'y')": 403,
            '/* hi */ UPDATE actor SET name = "x"': 403,
            'SELECT 1 /*! DROP TABLE actor */': 403,
            'SELECT 1; DELETE FROM actor': 400,
        }
        for sql, status in expected.items():
            self.assertEqual(self.run_sql(sql).status_code, status, sql)
        self.assertEqual(len(self.run_sql('SELECT * FROM actor', '?page_size=100').get_json()), 25)

    def test_semicolon_inside_literal_is_fine(self):
        res = self.run_sql("SELECT ';' AS semi FROM actor", '?page_size=1')
        self.assertEqual(res.get_json(), [{'semi': ';'}])

    def test_sqlite_connection_is_read_only_at_driver_level(self):
        with self.assertRaises(sqlite3.OperationalError):
            runners._run_sqlite({'database': self.db_path}, 'DELETE FROM actor', None, 10, 0, True)

    def test_writes_can_be_enabled(self):
        os.environ['QUERYAPIGATE_ALLOW_WRITES'] = '1'
        res = self.run_sql("INSERT INTO actor VALUES (99, 'New', '2000-01-01')")
        self.assertEqual(res.get_json(), {'message': 'No results returned'})
        rows = self.run_sql('SELECT name FROM actor WHERE actor_id = 99').get_json()
        self.assertEqual(rows, [{'name': 'New'}])


class SavedQueryTests(ApiTestCase):
    def test_the_query_list_on_a_fresh_install_is_an_empty_list_not_an_error(self):
        res = self.client.get('/api/v1/queries')
        self.assertEqual((res.status_code, res.get_json()), (200, {'items': []}))

    def test_save_versions_and_list(self):
        first = self.save('my query')
        self.assertEqual(first.status_code, 201)
        self.assertTrue(first.get_json()['versions'][0]['uuid'])
        self.save('my query', sql='SELECT 2', tags=['x'])
        self.save('another')

        saved = store.load_versions('my query')
        self.assertEqual(sorted(store.version_numbers(saved)), [1, 2])
        self.assertEqual(store.read_published(saved), 2)
        self.assertEqual(saved['2']['version'], 2)
        self.assertEqual(saved['2']['tags'], ['x'])

        self.assertEqual(self.names(), ['another', 'my query'])
        detail = self.client.get('/api/v1/queries/my query').get_json()
        self.assertEqual([v['version'] for v in detail['versions']], [1, 2])

    def test_malformed_json_body_is_rejected_not_ignored(self):
        self.save('q', sql='SELECT * FROM actor WHERE actor_id = :id', connection_name='lite',
                  query_parameters={'id': {'type': 'int', 'default': 1}})
        # Ignoring it would quietly run with every parameter at its default
        res = self.client.post('/q/q', data='{"params": {"id": 5', content_type='application/json')
        self.assertEqual((res.status_code, res.get_json()['error'], res.get_json()['code']),
                         (400, 'Request body is not valid JSON', 'invalid_body'))
        # An absent body is still fine - the body is optional on this endpoint
        res = self.client.post('/q/q', content_type='application/json')
        self.assertEqual((res.status_code, res.get_json()[0]['actor_id']), (200, 1))

    def test_running_a_saved_query_still_records_history(self):
        # run_saved() loads the query without its history, which must not stop the run from being recorded
        self.save('q', connection_name='lite')
        for _ in range(3):
            self.assertEqual(self.client.get('/q/q').status_code, 200)
        self.assertEqual(len(store.load_versions('q')['1']['execution_history']), 3)
        self.assertEqual(store.load_versions('q', with_history=False)['1']['execution_history'], [])

    def test_save_validation(self):
        self.assertEqual(self.client.post('/api/v1/queries', json={'author': 'a'}).status_code, 400)
        for name in ('../evil', 'a/b', '.hidden', ''):
            self.assertEqual(self.save(name).status_code, 400, name)
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, 'evil.json')))

    def test_execute_latest_version(self):
        self.save('q', sql='SELECT actor_id FROM actor ORDER BY actor_id')
        self.save('q', sql='SELECT name FROM actor ORDER BY actor_id')
        res = self.client.post('/q/q?page_size=2', json={'connection_name': 'lite', 'format': 'csv'})
        self.assertEqual(res.get_data(as_text=True).splitlines(), ['name', 'Actor 1', 'Actor 2'])

    def test_parameters(self):
        self.save('p', sql="SELECT name FROM actor WHERE actor_id = {id} AND born LIKE '{year}%'")
        ok = self.client.post('/q/p', json={'connection_name': 'lite', 'placeholders': {'id': 3, 'year': '1903'}})
        self.assertEqual(ok.get_json(), [{'name': 'Actor 3'}])

        for bad in ({'id': '1 OR 1=1; --', 'year': '1903'}, {'id': 3, 'year': "x' OR '1'='1"},
                    {'id': '3 -- ', 'year': 'x'}, {'id': [1], 'year': 'x'}):
            res = self.client.post('/q/p', json={'connection_name': 'lite', 'placeholders': bad})
            self.assertEqual(res.status_code, 400, bad)

        missing = self.client.post('/q/p', json={'connection_name': 'lite', 'placeholders': {'id': 3}})
        self.assertEqual(missing.status_code, 400)
        self.assertIn('year', missing.get_json()['error'])

    def test_a_name_is_never_a_path(self):
        self.save('q')
        for name in ('..%2Fdb_connections.json', '%2Fetc%2Fpasswd', 'saved_sql%2F..%2Fq'):
            res = self.client.post(f'/q/{name}', json={'connection_name': 'lite'})
            self.assertEqual(res.status_code, 404, name)

    def test_query_flow_extracts_tables_and_joins(self):
        self.save('q', sql='SELECT a.name FROM actor a JOIN film_actor fa ON a.actor_id = fa.actor_id',
                  connection_name='lite')
        res = self.client.get('/api/v1/queries/q/versions/1/flow')
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data['tables'], ['actor', 'film_actor'])
        self.assertEqual(data['joins'], [{'left': 'actor', 'right': 'film_actor', 'type': 'JOIN',
                                          'on': 'a.actor_id = fa.actor_id'}])
        self.assertIsNone(data['error'])
        self.assertIsInstance(data['formatted'], str)
        self.assertTrue(data['formatted'])

    def test_query_flow_on_a_mongo_saved_query_is_gracefully_unavailable(self):
        put_connections(self.client, {
            'mg': {'db': 'mongo', 'host': 'h', 'user': 'u', 'database': 'd', 'active': True}})
        save_query(self.client, {
            'filename': 'm', 'author': 'a', 'description': 'd', 'query_type': 'mongo',
            'mongo_collection': 'users', 'mongo_filter': {}, 'connection_name': 'mg'})
        res = self.client.get('/api/v1/queries/m/versions/1/flow')
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data, {'tables': [], 'joins': [], 'formatted': None,
                                'error': "SQL analysis isn't available for this query"})

    def test_query_flow_requires_admin(self):
        self.save('q')
        os.environ['QUERYAPIGATE_API_KEY'] = 'k3y'
        self.addCleanup(os.environ.pop, 'QUERYAPIGATE_API_KEY', None)
        res = self.client.get('/api/v1/queries/q/versions/1/flow')
        self.assertEqual(res.status_code, 401)
        ok = self.client.get('/api/v1/queries/q/versions/1/flow', headers={'X-API-Key': 'k3y'})
        self.assertEqual(ok.status_code, 200)


class ConnectionTests(ApiTestCase):
    def test_get_masks_passwords(self):
        conns = self.connections()
        self.assertEqual(conns['pg']['password'], config.PASSWORD_MASK)
        self.assertNotIn('secret', json.dumps(conns))

    def test_patch_keeps_masked_password_and_validates(self):
        self.assertEqual(self.client.patch('/api/v1/connections/pg', json={
            'host': 'new-host', 'password': config.PASSWORD_MASK}).status_code, 200)
        self.assertEqual(self.client.post('/api/v1/connections', json={
            'name': 'fresh', 'db': 'sqlite', 'database': 'x.db'}).status_code, 201)

        stored = store.read_connections()
        self.assertEqual(stored['pg']['password'], 'secret')
        self.assertEqual(stored['pg']['host'], 'new-host')
        self.assertIn('fresh', stored)

        for bad in ({}, {'name': 'x'}, {'name': 'x', 'db': 'oracle'}, {'name': '../x', 'db': 'sqlite'}):
            self.assertEqual(self.client.post('/api/v1/connections', json=bad).status_code, 400, bad)

    def test_create_and_update_set_created_and_updated_at(self):
        res = self.client.post('/api/v1/connections', json={'name': 'fresh', 'db': 'sqlite', 'database': 'x.db'})
        self.assertEqual(res.status_code, 201)
        first = res.get_json()
        self.assertTrue(first['created_at'])
        self.assertEqual(first['created_at'], first['updated_at'])

        res = self.client.patch('/api/v1/connections/fresh', json={'database': 'y.db'})
        self.assertEqual(res.status_code, 200)
        second = res.get_json()
        self.assertEqual(second['created_at'], first['created_at'])  # unchanged by an update
        self.assertGreaterEqual(second['updated_at'], first['updated_at'])

    def test_a_client_cannot_fake_created_at(self):
        res = self.client.post('/api/v1/connections', json={
            'name': 'fresh', 'db': 'sqlite', 'database': 'x.db', 'created_at': '2000-01-01 00:00:00'})
        self.assertEqual(res.status_code, 201)
        self.assertNotEqual(res.get_json()['created_at'], '2000-01-01 00:00:00')


@unittest.skipIf(TEST_DATABASE_URL, 'pre-SQLite files only ever concerned SQLite stores')
class LegacyHomeTests(unittest.TestCase):
    """0.15 stopped importing the pre-SQLite JSON files (deprecated in 0.14). A home that has nothing but those -
    from 0.9 or older - is refused with what to do, rather than started empty; a home from 0.10 on still holds them,
    never deleted after their import, and is left alone."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = self.tmp.name
        patcher = mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': self.home, 'QUERYAPIGATE_API_KEY': 'admin-key'})
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, name, content):
        path = os.path.join(self.home, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            json.dump(content, f)

    def test_a_home_of_only_legacy_files_is_refused_saying_what_to_do(self):
        for name, content in (('db_connections.json', {'connections': {'a': {'db': 'sqlite'}}}),
                              ('api_keys.json', {'keys': {'k': {'hash': 'h'}}}), ('roles.json', {'roles': {'r': {}}}),
                              ('audit_log.json', {'entries': [{'action': 'x'}]}),
                              ('saved_sql/q.json', {'1': {'sql_query': 'SELECT 1'}})):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as home, \
                    mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': home}):
                self.home = home
                self.write(name, content)
                with self.assertRaisesRegex(ValueError, r'0\.10 or older .* Start QueryAPIGate 0\.14 on this folder'):
                    create_app()
                db.close()

    def test_empty_leftover_files_are_ignored(self):
        self.write('db_connections.json', {'connections': {}})
        self.write('roles.json', {'roles': {}})
        self.assertEqual(create_app().test_client().get('/health').status_code, 200)

    def test_the_cli_says_so_and_exits_2(self):
        self.write('db_connections.json', {'connections': {'a': {'db': 'sqlite'}}})
        from queryapigate import cli
        with mock.patch('sys.stderr') as stderr:
            self.assertEqual(cli.main(['backup', os.path.join(self.home, 'b.db')]), 2)
        self.assertIn('no longer reads', ''.join(str(c) for c in stderr.write.call_args_list))

    def test_a_home_whose_store_has_data_ignores_leftover_files(self):
        client = create_app().test_client()
        client.post('/api/v1/connections', headers={'X-API-Key': 'admin-key'},
                    json={'name': 'made', 'db': 'sqlite', 'database': 'x.db'})
        self.write('db_connections.json', {'connections': {'stale': {'db': 'sqlite', 'database': 'y.db'}}})
        names = [c['name'] for c in create_app().test_client().get(
            '/api/v1/connections', headers={'X-API-Key': 'admin-key'}).get_json()['items']]
        self.assertEqual(names, ['made'])

    def test_a_fresh_home_starts(self):
        self.assertEqual(create_app().test_client().get('/health').status_code, 200)


@unittest.skipUnless(TEST_DATABASE_URL, 'set QUERYAPIGATE_TEST_DATABASE_URL to run')
class LegacyFilesIgnoredOnPostgresTests(unittest.TestCase):
    """Legacy JSON files are never deleted after their one-time import into queryapigate.db, so an old home
    still holds a stale copy. On Postgres they must be ignored - its data arrives through
    `queryapigate migrate-to-postgres`, from queryapigate.db, never from these."""

    def test_stale_legacy_files_are_not_imported(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': home}):
            with open(os.path.join(home, 'db_connections.json'), 'w') as f:
                json.dump({'connections': {'stale': {'db': 'sqlite', 'database': 'x.db', 'active': True}}}, f)
            with open(os.path.join(home, 'api_keys.json'), 'w') as f:
                json.dump({'keys': {'stale': {'hash': 'h', 'connections': '*', 'created_at': 'now'}}}, f)
            create_app()
            self.assertEqual(store.read_connections(), {})
            self.assertEqual(db.connection().execute('SELECT COUNT(*) FROM api_keys').fetchone()[0], 0)
            db.close()


class TestConnectionTests(ApiTestCase):
    def test_succeeds_against_a_real_connection(self):
        res = self.client.post('/api/v1/connections/test', json={'db': 'sqlite', 'database': self.db_path})
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        self.assertIn('elapsed_ms', res.get_json())

    def test_nothing_is_saved(self):
        self.client.post('/api/v1/connections/test', json={'db': 'sqlite', 'database': self.db_path})
        self.assertEqual(set(store.read_connections()), {'lite', 'off', 'pg'})

    def test_unreachable_host_is_502_with_the_password_redacted(self):
        res = self.client.post('/api/v1/connections/test', json={
            'db': 'postgres', 'host': 'h', 'user': 'u', 'password': 'secret', 'database': 'd'})
        self.assertEqual(res.status_code, 502)
        self.assertNotIn('secret', res.get_data(as_text=True))

    def test_a_masked_password_is_resolved_from_the_named_connection(self):
        # 'pg' is stored with password 'secret' (see setUp); echoing the mask back, as the edit form does
        # for a password the admin never retyped, must try the real one, not the literal mask string.
        body = {'name': 'pg', 'db': 'postgres', 'host': 'h', 'user': 'u',
                'password': config.PASSWORD_MASK, 'database': 'd'}
        res = self.client.post('/api/v1/connections/test', json=body)
        self.assertEqual(res.status_code, 502)  # 'h' is unreachable; the point is it never 400s on the mask itself
        self.assertNotIn('secret', res.get_data(as_text=True))

    def test_unsupported_db_type_is_400(self):
        self.assertEqual(self.client.post('/api/v1/connections/test', json={'db': 'oracle'}).status_code, 400)
        self.assertEqual(self.client.post('/api/v1/connections/test', json={}).status_code, 400)

    def test_requires_the_admin_key(self):
        os.environ['QUERYAPIGATE_API_KEY'] = 'admin-key'
        self.client = create_app().test_client()
        body = {'db': 'sqlite', 'database': self.db_path}
        self.assertEqual(self.client.post('/api/v1/connections/test', json=body).status_code, 401)
        admin = {'X-API-Key': 'admin-key'}
        scoped = self.client.post('/api/v1/api-keys', json={'name': 'scoped', 'connections': []}, headers=admin)
        key = scoped.get_json()['secret']
        res = self.client.post('/api/v1/connections/test', json=body, headers={'X-API-Key': key})
        self.assertEqual(res.status_code, 403)


class ListDatabasesTests(ApiTestCase):
    def test_ad_hoc_mysql_lists_databases(self):
        cursor = mock.MagicMock()
        cursor.description = [('name',)]
        cursor.fetchall.return_value = [('orders',), ('billing',)]
        conn = mock.MagicMock()
        conn.cursor.return_value = cursor
        with mock.patch('mysql.connector.connect', return_value=conn):
            res = self.client.post('/api/v1/connections/databases',
                                   json={'db': 'mysql', 'host': 'h', 'user': 'u', 'password': 'p'})
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        self.assertEqual(res.get_json()['databases'], ['orders', 'billing'])

    def test_ad_hoc_postgres_bootstraps_with_the_postgres_database_when_none_is_given_yet(self):
        cursor = mock.MagicMock()
        cursor.description = [('name',)]
        cursor.fetchall.return_value = [('warehouse',)]
        conn = mock.MagicMock()
        conn.cursor.return_value.__enter__.return_value = cursor
        with mock.patch('psycopg2.connect', return_value=conn) as connect:
            res = self.client.post('/api/v1/connections/databases', json={'db': 'postgres', 'host': 'h', 'user': 'u'})
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        self.assertEqual(connect.call_args.kwargs['dbname'], 'postgres')
        self.assertEqual(res.get_json()['databases'], ['warehouse'])

    def test_named_saved_connection_uses_its_own_database_to_connect(self):
        cursor = mock.MagicMock()
        cursor.description = [('name',)]
        cursor.fetchall.return_value = [('d',)]
        conn = mock.MagicMock()
        conn.cursor.return_value.__enter__.return_value = cursor
        with mock.patch('psycopg2.connect', return_value=conn) as connect:
            res = self.client.post('/api/v1/connections/databases', json={'name': 'pg'})
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        self.assertEqual(connect.call_args.kwargs['dbname'], 'd')  # 'pg' is already configured with database 'd'
        self.assertEqual(res.get_json()['databases'], ['d'])

    def test_unsupported_dialect_is_a_clear_error_not_a_guess(self):
        res = self.client.post('/api/v1/connections/databases', json={'db': 'sqlite', 'database': self.db_path})
        self.assertEqual(res.status_code, 400)
        self.assertIn('sqlite', res.get_json()['error'])

    def test_password_is_never_leaked_on_failure(self):
        res = self.client.post('/api/v1/connections/databases',
                               json={'db': 'postgres', 'host': 'h', 'user': 'u', 'password': 'secret'})
        self.assertEqual(res.status_code, 502)
        self.assertNotIn('secret', res.get_data(as_text=True))

    def test_requires_the_admin_key(self):
        os.environ['QUERYAPIGATE_API_KEY'] = 'admin-key'
        self.client = create_app().test_client()
        self.assertEqual(self.client.post('/api/v1/connections/databases', json={'name': 'pg'}).status_code, 401)


class ExecuteAgainstADifferentDatabaseTests(ApiTestCase):
    def test_admin_can_run_against_a_different_database_on_the_same_connection(self):
        os.environ['QUERYAPIGATE_API_KEY'] = 'admin-key'
        self.client = create_app().test_client()
        recorder = mock.MagicMock(return_value=(['x'], [(1,)]))
        with mock.patch.dict(engine.RUNNERS, {'sqlite': recorder}):
            res = self.client.post('/execute_sql', json={
                'sql': 'SELECT 1', 'connection_name': 'lite', 'database': 'other.db'},
                headers={'X-API-Key': 'admin-key'})
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        self.assertEqual(recorder.call_args.args[0]['database'], 'other.db')

    def test_a_scoped_key_cannot_choose_a_different_database(self):
        os.environ['QUERYAPIGATE_API_KEY'] = 'admin-key'
        self.client = create_app().test_client()
        admin = {'X-API-Key': 'admin-key'}
        scoped = self.client.post('/api/v1/api-keys', json={'name': 'scoped', 'connections': ['lite']}, headers=admin)
        key = scoped.get_json()['secret']
        res = self.client.post('/execute_sql', json={
            'sql': 'SELECT 1', 'connection_name': 'lite', 'database': 'other.db'}, headers={'X-API-Key': key})
        self.assertEqual(res.status_code, 403)

    def test_schema_browsing_a_different_database_is_admin_only(self):
        os.environ['QUERYAPIGATE_API_KEY'] = 'admin-key'
        self.client = create_app().test_client()
        admin = {'X-API-Key': 'admin-key'}
        scoped = self.client.post('/api/v1/api-keys', json={'name': 'scoped', 'connections': ['lite']}, headers=admin)
        key = scoped.get_json()['secret']
        self.assertEqual(self.client.get('/connections/lite/schema?database=other.db',
                                         headers={'X-API-Key': key}).status_code, 403)
        # The admin key clears the permission check; 'lite' is sqlite, which doesn't support switching
        # databases at all, so its own (different) error surfaces instead - never 403 for the admin key.
        self.assertEqual(self.client.get('/connections/lite/schema?database=other.db', headers=admin).status_code, 400)


class SchemaTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        conn = sqlite3.connect(self.db_path)
        conn.execute('CREATE VIEW young_actor AS SELECT actor_id, name FROM actor WHERE born > "1910"')
        conn.execute('CREATE TABLE studio (id INTEGER PRIMARY KEY, name TEXT)')
        conn.execute('CREATE TABLE film (id INTEGER PRIMARY KEY, studio_id INTEGER REFERENCES studio(id), title TEXT)')
        conn.commit()
        conn.close()

    def test_primary_and_foreign_keys_are_marked(self):
        body = self.client.get('/connections/lite/schema').get_json()
        tables = {t['name']: t for t in body['tables']}
        film_columns = {c['name']: c for c in tables['film']['columns']}
        studio_columns = {c['name']: c for c in tables['studio']['columns']}
        self.assertTrue(studio_columns['id']['primary_key'])
        self.assertIsNone(studio_columns['id']['foreign_key'])
        self.assertTrue(film_columns['id']['primary_key'])
        self.assertFalse(film_columns['studio_id']['primary_key'])
        self.assertEqual(film_columns['studio_id']['foreign_key'], {'table': 'studio', 'column': 'id'})
        self.assertFalse(film_columns['title']['primary_key'])
        self.assertIsNone(film_columns['title']['foreign_key'])

    def test_a_table_with_no_keys_reports_false_and_none(self):
        body = self.client.get('/connections/lite/schema').get_json()
        tables = {t['name']: t for t in body['tables']}
        columns = {c['name']: c for c in tables['actor']['columns']}
        self.assertEqual([c['primary_key'] for c in columns.values()], [False, False, False])
        self.assertEqual([c['foreign_key'] for c in columns.values()], [None, None, None])

    def test_lists_tables_and_views_with_their_columns(self):
        body = self.client.get('/connections/lite/schema').get_json()
        self.assertEqual(body['truncated'], False)
        tables = {t['name']: t for t in body['tables']}
        self.assertEqual(tables['actor']['type'], 'table')
        self.assertEqual(tables['young_actor']['type'], 'view')
        columns = {c['name']: c for c in tables['actor']['columns']}
        self.assertEqual(set(columns), {'actor_id', 'name', 'born'})
        self.assertEqual(columns['actor_id']['position'], 1)
        self.assertTrue(columns['actor_id']['nullable'])

    def test_unknown_connection_is_404(self):
        self.assertEqual(self.client.get('/connections/nope/schema').status_code, 404)

    def test_inactive_connection_is_403(self):
        self.assertEqual(self.client.get('/connections/off/schema').status_code, 403)

    def test_unreachable_connection_is_502_connection_failed_not_a_crash(self):
        res = self.client.get('/connections/pg/schema')
        self.assertEqual((res.status_code, res.get_json()['code']), (502, 'connection_failed'))

    def test_database_with_no_tables_is_an_empty_list_not_an_error(self):
        empty_path = os.path.join(self.tmp.name, 'empty.db')
        sqlite3.connect(empty_path).close()
        put_connections(self.client, {'empty': {'db': 'sqlite', 'database': empty_path, 'active': True}})
        self.assertEqual(self.client.get('/connections/empty/schema').get_json(), {'tables': [], 'truncated': False})

    def test_truncates_when_more_columns_exist_than_the_cap(self):
        with mock.patch.object(schema, 'ROW_CAP', 2):
            body = self.client.get('/connections/lite/schema').get_json()
        self.assertTrue(body['truncated'])
        self.assertEqual(sum(len(t['columns']) for t in body['tables']), 2)


class TableDdlTests(ApiTestCase):
    """BACKLOG #38: a table's real CREATE TABLE text, for the dialects that support it (mysql/sqlite/
    clickhouse) - real sqlite here, since it needs no server."""
    def setUp(self):
        super().setUp()
        conn = sqlite3.connect(self.db_path)
        conn.execute('CREATE TABLE studio (id INTEGER PRIMARY KEY, name TEXT)')
        conn.commit()
        conn.close()

    def test_returns_the_real_ddl(self):
        res = self.client.get('/connections/lite/table_ddl', query_string={'table': 'studio'})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json(), {'ddl': 'CREATE TABLE studio (id INTEGER PRIMARY KEY, name TEXT)'})

    def test_a_nonexistent_table_is_404_not_a_raw_sql_error(self):
        res = self.client.get('/connections/lite/table_ddl', query_string={'table': 'nope'})
        self.assertEqual(res.status_code, 404)

    def test_a_sql_injection_attempt_is_rejected_as_a_nonexistent_table(self):
        res = self.client.get('/connections/lite/table_ddl',
                              query_string={'table': "studio'; DROP TABLE studio; --"})
        self.assertEqual(res.status_code, 404)
        # the table must still be there - the attempt never reached a SQL string
        still_there = self.client.get('/connections/lite/table_ddl', query_string={'table': 'studio'})
        self.assertEqual(still_there.status_code, 200)

    def test_table_is_required(self):
        self.assertEqual(self.client.get('/connections/lite/table_ddl').status_code, 400)

    def test_an_unsupported_dialect_is_a_clear_error_not_a_crash(self):
        res = self.client.get('/connections/pg/table_ddl', query_string={'table': 'whatever'})
        self.assertEqual(res.status_code, 400)
        self.assertIn('postgres', res.get_json()['error'])


class DuckDBSchemaTests(ApiTestCase):
    """schema.py's PK/FK detection for duckdb goes through _KEY_QUERIES (unlike sqlite, folded into its base
    query above) - a real duckdb file, not mocks, since duckdb is embedded and needs no server."""
    def setUp(self):
        super().setUp()
        self.duckdb_path = os.path.join(self.tmp.name, 'ducks.duckdb')
        con = duckdb.connect(self.duckdb_path)
        con.execute('CREATE TABLE studio (id INTEGER PRIMARY KEY, name VARCHAR)')
        con.execute('CREATE TABLE film (id INTEGER PRIMARY KEY, studio_id INTEGER '
                    'REFERENCES studio(id), title VARCHAR)')
        con.close()
        put_connections(self.client, {'dk': {'db': 'duckdb', 'database': self.duckdb_path, 'active': True}})

    def test_primary_and_foreign_keys_are_marked(self):
        body = self.client.get('/connections/dk/schema').get_json()
        tables = {t['name']: t for t in body['tables']}
        film_columns = {c['name']: c for c in tables['film']['columns']}
        studio_columns = {c['name']: c for c in tables['studio']['columns']}
        self.assertTrue(studio_columns['id']['primary_key'])
        self.assertTrue(film_columns['id']['primary_key'])
        self.assertEqual(film_columns['studio_id']['foreign_key'], {'table': 'studio', 'column': 'id'})
        self.assertFalse(film_columns['title']['primary_key'])
        self.assertIsNone(film_columns['title']['foreign_key'])

    def test_a_broken_key_query_degrades_to_no_badges_not_an_error(self):
        real_execute = engine.execute_sql

        def flaky(sql, *a, **kw):
            if 'duckdb_constraints' in sql:
                raise RuntimeError('simulated permissions failure reading the constraint catalogue')
            return real_execute(sql, *a, **kw)

        with mock.patch.object(engine, 'execute_sql', side_effect=flaky):
            res = self.client.get('/connections/dk/schema')
        self.assertEqual(res.status_code, 200)
        tables = {t['name']: t for t in res.get_json()['tables']}
        self.assertEqual({c['primary_key'] for c in tables['studio']['columns']}, {False})
        self.assertEqual({c['foreign_key'] for c in tables['film']['columns']}, {None})


class ApiKeyTests(ApiTestCase):
    def test_api_key_is_enforced_only_when_configured(self):
        self.assertEqual(self.client.get('/api/v1/connections').status_code, 200)
        os.environ['QUERYAPIGATE_API_KEY'] = 'k3y'
        self.assertEqual(self.client.get('/api/v1/connections').status_code, 401)
        self.assertEqual(self.client.get('/api/v1/connections', headers={'X-API-Key': 'wrong'}).status_code, 401)
        self.assertEqual(self.client.get('/api/v1/connections', headers={'X-API-Key': 'k3y'}).status_code, 200)
        self.assertEqual(self.client.get('/api/v1/connections', headers={'X-API-Key': 'k\u00e9y'}).status_code,
                         401)


class DriverWiringTests(unittest.TestCase):
    """The network runners are covered for real in test_integration.py; these check the driver hand-off."""

    def test_postgres_readonly_session_paginates_and_binds(self):
        cursor = mock.MagicMock()
        cursor.description = [('id',)]
        cursor.fetchall.return_value = [(1,)]
        conn = mock.MagicMock()
        conn.cursor.return_value.__enter__.return_value = cursor
        with mock.patch('psycopg2.connect', return_value=conn) as connect:
            columns, rows = runners._run_postgres(
                {'host': 'h', 'user': 'u', 'password': 'p', 'database': 'd', 'db': 'postgres', 'active': True},
                "SELECT id FROM t WHERE n = :n AND s LIKE '%x' LIMIT 500", {'n': 4}, 5, 10, True)
        kwargs = connect.call_args.kwargs
        self.assertEqual((kwargs['dbname'], kwargs['host']), ('d', 'h'))
        self.assertNotIn('active', kwargs)
        self.assertNotIn('db', kwargs)
        conn.set_session.assert_called_once_with(readonly=True)
        cursor.execute.assert_called_once_with("SELECT id FROM t WHERE n = %s AND s LIKE '%%x'\nLIMIT 6 OFFSET 10", [4])
        self.assertEqual((columns, rows), (['id'], [(1,)]))
        conn.close.assert_called_once()

    def test_clickhouse_single_round_trip_and_readonly_setting(self):
        client = mock.MagicMock()
        client.execute.return_value = ([(1,)], [('id', 'UInt8')])
        with mock.patch('clickhouse_driver.Client', return_value=client) as ctor:
            columns, rows = runners._run_clickhouse(
                {'host': 'h', 'user': 'u', 'password': 'p', 'database': 'd', 'db': 'clickhouse'},
                'SELECT id FROM t WHERE id = :id', {'id': 7}, 5, 0, True)
        self.assertNotIn('db', ctor.call_args.kwargs)
        self.assertEqual(client.execute.call_count, 1)  # data and column names come from one round trip
        query, args = client.execute.call_args.args
        self.assertEqual((query, args), ('SELECT id FROM t WHERE id = %(id)s\nLIMIT 6 OFFSET 0', {'id': 7}))
        self.assertEqual(client.execute.call_args.kwargs['settings'], {'readonly': 1})
        self.assertEqual(columns, ['id'])
        client.disconnect.assert_called_once()

    def test_clickhouse_statement_without_result_set(self):
        client = mock.MagicMock()
        client.execute.return_value = []  # what the driver returns for DDL
        with mock.patch('clickhouse_driver.Client', return_value=client):
            self.assertEqual(runners._run_clickhouse({'host': 'h'}, 'CREATE TABLE t (a Int8)', None, 5, 0, False),
                             ([], []))


    def test_postgres_sets_statement_timeout_and_maps_cancellation(self):
        import psycopg2.errors
        cursor = mock.MagicMock()
        cursor.description = [('id',)]
        cursor.execute.side_effect = [None, psycopg2.errors.QueryCanceled('canceled')]
        conn = mock.MagicMock()
        conn.cursor.return_value.__enter__.return_value = cursor
        with mock.patch('psycopg2.connect', return_value=conn):
            with self.assertRaises(ApiError) as caught:
                runners._run_postgres({'host': 'h'}, 'SELECT pg_sleep(9)', None, 5, 0, True, 1.5)
        self.assertEqual(cursor.execute.call_args_list[0].args, ('SET LOCAL statement_timeout = 1500',))
        self.assertEqual(caught.exception.status, 504)
        conn.close.assert_called_once()

    def test_clickhouse_timeout_setting_precedes_readonly_and_maps_error(self):
        from clickhouse_driver.errors import ServerException
        client = mock.MagicMock()
        client.execute.side_effect = ServerException('Timeout exceeded', 159, None)
        with mock.patch('clickhouse_driver.Client', return_value=client) as ctor:
            with self.assertRaises(ApiError) as caught:
                runners._run_clickhouse({'host': 'h'}, 'SELECT 1', None, 5, 0, True, 1.2)
        settings = client.execute.call_args.kwargs['settings']
        self.assertEqual(list(settings.items()), [('max_execution_time', 2), ('readonly', 1)])
        # socket backstop comes from the server-wide limit (default 30s) because one client serves many requests
        self.assertEqual(ctor.call_args.kwargs['send_receive_timeout'], 35)
        self.assertEqual(caught.exception.status, 504)
        client.disconnect.assert_called_once()

    def test_clickhouse_other_server_errors_are_not_reported_as_timeouts(self):
        from clickhouse_driver.errors import ServerException
        client = mock.MagicMock()
        client.execute.side_effect = ServerException('Unknown table', 60, None)
        with mock.patch('clickhouse_driver.Client', return_value=client):
            with self.assertRaises(ServerException):
                runners._run_clickhouse({'host': 'h'}, 'SELECT 1', None, 5, 0, True, 1)

    def test_mysql_limit_variable_fallback_and_error_mapping(self):
        import mysql.connector
        cursor = mock.MagicMock()
        cursor.description = [('a',)]
        timeout_error = mysql.connector.Error('Query execution was interrupted', errno=3024)

        def execute(sql, *args):
            if sql.startswith('SET SESSION max_execution_time'):
                raise mysql.connector.Error('Unknown system variable', errno=1193)  # e.g. MariaDB
            if sql.startswith('SELECT'):
                raise timeout_error

        cursor.execute.side_effect = execute
        conn = mock.MagicMock()
        conn.cursor.return_value = cursor
        with mock.patch('mysql.connector.connect', return_value=conn):
            with self.assertRaises(ApiError) as caught:
                runners._run_mysql({'host': 'h'}, 'SELECT SLEEP(9)', None, 5, 0, True, 2)
        statements = [c.args[0] for c in cursor.execute.call_args_list]
        self.assertIn('SET SESSION max_statement_time = 2', statements)  # MariaDB variable, in seconds
        self.assertEqual(caught.exception.status, 504)
        conn.close.assert_called_once()

    def test_h2_sets_query_timeout_and_maps_cancellation(self):
        import jaydebeapi
        cursor = mock.MagicMock()
        cursor.description = [('C',)]
        cursor.execute.side_effect = [None, jaydebeapi.DatabaseError('Statement was canceled or the session timed out')]
        conn = mock.MagicMock()
        conn.cursor.return_value = cursor
        with mock.patch('jaydebeapi.connect', return_value=conn):
            with self.assertRaises(ApiError) as caught:
                runners._run_h2({'host': 'h', 'database': 'd'}, 'SELECT 1', None, 5, 0, True, 0.5)
        self.assertEqual(cursor.execute.call_args_list[0].args, ('SET QUERY_TIMEOUT 500',))
        self.assertEqual(caught.exception.status, 504)


class FakeDriver(runners._Driver):
    """Counts connects/closes so the pool's behaviour can be observed without a database."""

    def __init__(self):
        self.connects = 0
        self.closed = []
        self.alive = True
        self.fail_reset = False
        self.resets = 0

    def connect(self, details, read_only):
        self.connects += 1
        return f'conn-{self.connects}'

    def is_alive(self, session):
        return self.alive

    def reset(self, session):
        self.resets += 1
        if self.fail_reset:
            raise RuntimeError('reset failed')

    def close(self, session):
        self.closed.append(session.conn)


class PoolTests(unittest.TestCase):
    def setUp(self):
        self.driver = FakeDriver()
        self.pool = pool.ConnectionPool()
        self.details = {'host': 'h', 'user': 'u'}
        patcher = mock.patch.dict(os.environ, {'QUERYAPIGATE_POOL_SIZE': '2', 'QUERYAPIGATE_POOL_IDLE_TIMEOUT': '300'})
        patcher.start()
        self.addCleanup(patcher.stop)

    def use(self, details=None, read_only=True):
        with self.pool.checkout(self.driver, details or self.details, read_only) as session:
            return session.conn

    def test_idle_connection_is_reused_and_reset_each_time(self):
        self.assertEqual(self.use(), 'conn-1')
        self.assertEqual(self.use(), 'conn-1')
        self.assertEqual(self.driver.connects, 1)
        self.assertEqual(self.driver.resets, 2)
        self.assertEqual(self.pool.idle_count(), 1)

    def test_state_survives_reuse_but_not_across_connections(self):
        with self.pool.checkout(self.driver, self.details, True) as session:
            session.state['timeout'] = 5
        with self.pool.checkout(self.driver, self.details, True) as session:
            self.assertEqual(session.state, {'timeout': 5})

    def test_different_settings_or_mode_never_share_a_connection(self):
        first = self.use()
        self.assertNotEqual(self.use({**self.details, 'password': 'other'}), first)
        self.assertNotEqual(self.use(read_only=False), first)
        self.assertEqual(self.use(), first)  # the original key still has its own connection

    def test_failed_request_discards_the_connection(self):
        with self.assertRaises(ValueError):
            with self.pool.checkout(self.driver, self.details, True):
                raise ValueError('boom')
        self.assertEqual(self.driver.closed, ['conn-1'])
        self.assertEqual(self.pool.idle_count(), 0)
        self.assertEqual(self.use(), 'conn-2')

    def test_only_pool_size_idle_connections_are_kept(self):
        a, b, c = (self.pool.checkout(self.driver, self.details, True) for _ in range(3))
        sessions = [ctx.__enter__() for ctx in (a, b, c)]  # three concurrent users -> three connections
        self.assertEqual(self.driver.connects, 3)
        for ctx in (a, b, c):
            ctx.__exit__(None, None, None)
        self.assertEqual(self.pool.idle_count(), 2)
        self.assertEqual(len(self.driver.closed), 1)
        self.assertEqual(len(sessions), 3)

    def test_expired_idle_connections_are_closed_not_reused(self):
        self.use()
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_POOL_IDLE_TIMEOUT': '1'}):
            for queue in self.pool._idle.values():
                for session in queue:
                    session.idle_since -= 10
            self.assertEqual(self.use(), 'conn-2')
        self.assertIn('conn-1', self.driver.closed)

    def test_dead_connection_is_detected_before_reuse(self):
        self.use()
        for queue in self.pool._idle.values():
            for session in queue:
                session.idle_since -= pool.VALIDATE_AFTER + 1  # long enough idle to be worth checking
        self.driver.alive = False
        self.assertEqual(self.use(), 'conn-2')
        self.assertIn('conn-1', self.driver.closed)

    def test_recently_used_connection_is_trusted_without_a_check(self):
        self.use()
        self.driver.alive = False  # would be caught, but the connection was idle for less than VALIDATE_AFTER
        self.assertEqual(self.use(), 'conn-1')

    def test_connection_that_cannot_be_reset_is_closed(self):
        self.driver.fail_reset = True
        self.use()
        self.assertEqual(self.driver.closed, ['conn-1'])
        self.assertEqual(self.pool.idle_count(), 0)

    def test_close_all_and_disabled_pool(self):
        self.use()
        self.pool.close_all()
        self.assertEqual(self.driver.closed, ['conn-1'])
        self.assertEqual(self.pool.idle_count(), 0)
        self.assertIsNotNone(pool.get_pool())
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_POOL_SIZE': '0'}):
            self.assertIsNone(pool.get_pool())

    def test_settings_parsing(self):
        for junk, expected in (('abc', config.DEFAULT_POOL_SIZE), ('-3', 0), ('7', 7)):
            with mock.patch.dict(os.environ, {'QUERYAPIGATE_POOL_SIZE': junk}):
                self.assertEqual(config.pool_size(), expected, junk)
        for junk in ('abc', '0', '-1'):
            with mock.patch.dict(os.environ, {'QUERYAPIGATE_POOL_IDLE_TIMEOUT': junk}):
                self.assertEqual(config.pool_idle_timeout(), config.DEFAULT_POOL_IDLE_TIMEOUT, junk)

    def test_many_threads_share_the_pool_safely(self):
        errors = []

        def worker():
            try:
                for _ in range(40):
                    with self.pool.checkout(self.driver, self.details, True) as session:
                        self.assertTrue(session.conn.startswith('conn-'))
            except Exception as error:  # noqa: BLE001 - report from the thread
                errors.append(error)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertLessEqual(self.driver.connects, 8 * 40)
        self.assertLessEqual(self.pool.idle_count(), 2)
        self.assertEqual(self.driver.connects - len(self.driver.closed), self.pool.idle_count())  # nothing leaked

    def test_engine_only_hands_the_pool_to_runners_when_enabled(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': tmp}):
            write_connections({'c': {'db': 'mysql', 'host': 'h', 'active': True}})
            recorder = mock.MagicMock(return_value=(['a'], [(1,)]))
            with mock.patch.dict(engine.RUNNERS, {'mysql': recorder}):
                engine.execute_sql('SELECT 1', 'c', 10, 0)
                self.assertIs(recorder.call_args.args[-1], pool.get_pool())
                self.assertIsNotNone(recorder.call_args.args[-1])
                with mock.patch.dict(os.environ, {'QUERYAPIGATE_POOL_SIZE': '0'}):
                    engine.execute_sql('SELECT 1', 'c', 10, 0)
                self.assertIsNone(recorder.call_args.args[-1])


class PooledDriverTests(unittest.TestCase):
    """What each driver does when its connection is borrowed repeatedly (mocked; real servers in test_integration)."""

    def setUp(self):
        self.pool = pool.ConnectionPool()
        patcher = mock.patch.dict(os.environ, {'QUERYAPIGATE_POOL_SIZE': '2'})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_mysql_reuses_the_connection_ends_each_transaction_and_applies_changed_limits_only(self):
        cursor = mock.MagicMock()
        cursor.description = [('a',)]
        cursor.fetchall.return_value = [(1,)]
        conn = mock.MagicMock()
        conn.cursor.return_value = cursor
        with mock.patch('mysql.connector.connect', return_value=conn) as connect:
            for timeout in (2, 2, None):
                runners._run_mysql({'host': 'h'}, 'SELECT 1', None, 5, 0, True, timeout, self.pool)
        self.assertEqual(connect.call_count, 1)
        self.assertEqual(conn.rollback.call_count, 3)  # a pooled connection must not keep a stale snapshot
        conn.close.assert_not_called()
        limits = [c.args[0] for c in cursor.execute.call_args_list if 'max_execution_time' in c.args[0]]
        self.assertEqual(limits, ['SET SESSION max_execution_time = 2000',   # first use
                                  'SET SESSION max_execution_time = 0'])     # limit removed again, not left at 2s

    def test_postgres_sets_a_transaction_local_limit_on_every_use_and_rolls_back(self):
        cursor = mock.MagicMock()
        cursor.description = [('a',)]
        cursor.fetchall.return_value = [(1,)]
        conn = mock.MagicMock()
        conn.closed = 0
        conn.cursor.return_value.__enter__.return_value = cursor
        with mock.patch('psycopg2.connect', return_value=conn) as connect:
            for _ in range(2):
                runners._run_postgres({'host': 'h'}, 'SELECT 1', None, 5, 0, True, 3, self.pool)
        self.assertEqual(connect.call_count, 1)
        conn.set_session.assert_called_once_with(readonly=True)
        self.assertEqual(conn.rollback.call_count, 2)
        local = [c.args[0] for c in cursor.execute.call_args_list if c.args[0].startswith('SET')]
        self.assertEqual(local, ['SET LOCAL statement_timeout = 3000'] * 2)

    def test_connection_used_by_a_failed_query_is_not_pooled(self):
        client = mock.MagicMock()
        client.execute.side_effect = RuntimeError('network down')
        with mock.patch('clickhouse_driver.Client', return_value=client):
            with self.assertRaises(RuntimeError):
                runners._run_clickhouse({'host': 'h'}, 'SELECT 1', None, 5, 0, True, 1, self.pool)
        client.disconnect.assert_called_once()
        self.assertEqual(self.pool.idle_count(), 0)

    def test_clickhouse_client_is_reused(self):
        client = mock.MagicMock()
        client.execute.return_value = ([(1,)], [('a', 'UInt8')])
        with mock.patch('clickhouse_driver.Client', return_value=client) as ctor:
            for _ in range(3):
                runners._run_clickhouse({'host': 'h'}, 'SELECT 1', None, 5, 0, True, 1, self.pool)
        self.assertEqual(ctor.call_count, 1)
        client.disconnect.assert_not_called()

    def test_h2_threads_are_attached_to_the_jvm_as_daemons_so_shutdown_cannot_hang(self):
        import jpype
        thread = mock.MagicMock()
        fake_java = mock.MagicMock()
        fake_java.lang.Thread = thread
        # patch the module dict: reading the real jpype.java would demand a running JVM
        with mock.patch('jpype.isJVMStarted', return_value=True), mock.patch.dict(jpype.__dict__, {'java': fake_java}):
            thread.isAttached.return_value = False
            runners._attach_thread_as_daemon()
            thread.attachAsDaemon.assert_called_once()
            thread.attachAsDaemon.reset_mock()
            thread.isAttached.return_value = True  # already attached: leave it alone
            runners._attach_thread_as_daemon()
            thread.attachAsDaemon.assert_not_called()
        with mock.patch('jpype.isJVMStarted', return_value=False):  # nothing to attach to before the JVM starts
            runners._attach_thread_as_daemon()

    def test_h2_dead_connection_is_replaced(self):
        first, second = mock.MagicMock(), mock.MagicMock()
        for conn in (first, second):
            conn.cursor.return_value.description = [('C',)]
            conn.cursor.return_value.fetchall.return_value = [(1,)]
        first.jconn.isValid.return_value = False
        with mock.patch('jaydebeapi.connect', side_effect=[first, second]) as connect:
            runners._run_h2({'host': 'h', 'database': 'd'}, 'SELECT 1', None, 5, 0, True, None, self.pool)
            for queue in self.pool._idle.values():
                for session in queue:
                    session.idle_since -= pool.VALIDATE_AFTER + 1
            runners._run_h2({'host': 'h', 'database': 'd'}, 'SELECT 1', None, 5, 0, True, None, self.pool)
        self.assertEqual(connect.call_count, 2)
        first.close.assert_called_once()


RULES_SQL = ('SELECT actor_id, name FROM actor WHERE actor_id >= :min_id AND (:q IS NULL OR name LIKE :q) '
             'ORDER BY actor_id')
RULES = {'min_id': {'type': 'int', 'min': 1, 'max': 25, 'default': 1, 'description': 'First actor id to include'},
         'q': {'type': 'str', 'required': False, 'min_length': 2, 'description': 'Name filter, e.g. Actor 2%'}}


class ParameterRuleApiTests(ApiTestCase):
    def save_rules(self, filename='rules', **extra):
        return self.save(filename, sql=RULES_SQL, query_parameters=RULES, connection_name='lite', **extra)

    def test_rules_are_validated_when_a_query_is_saved(self):
        self.assertEqual(self.save_rules().status_code, 201)
        res = self.save('bad', sql='SELECT :a', query_parameters={'a': {'type': 'int', 'min': 5, 'max': 1},
                                                                   'b': {'colour': 'red'}})
        self.assertEqual(res.status_code, 400)
        self.assertEqual(set(res.get_json()['errors']), {'a', 'b'})
        self.assertFalse(os.path.exists(os.path.join(self.saved_dir, 'bad.json')))

    def test_declaring_a_parameter_the_sql_does_not_use_is_rejected(self):
        res = self.save('typo', sql='SELECT :id', query_parameters={'idd': 'int'})
        self.assertEqual(res.status_code, 400)
        self.assertIn('idd', res.get_json()['error'])
        # {name} text placeholders count as use
        self.assertEqual(self.save('legacy', sql="SELECT '{id}'", query_parameters={'id': 'int'}).status_code, 201)

    def test_defaults_and_optional_parameters_apply(self):
        self.save_rules()
        rows = self.client.get('/q/rules?page_size=3').get_json()
        self.assertEqual([r['actor_id'] for r in rows], [1, 2, 3])          # min_id defaulted to 1, q is NULL
        rows = self.client.get('/q/rules?min_id=24').get_json()
        self.assertEqual([r['actor_id'] for r in rows], [24, 25])
        rows = self.client.get('/q/rules?q=Actor 2%').get_json()
        self.assertEqual([r['name'] for r in rows][:2], ['Actor 2', 'Actor 20'])

    def test_violations_return_400_with_a_field_by_field_explanation(self):
        self.save_rules()
        res = self.client.get('/q/rules?min_id=0&q=x')
        self.assertEqual(res.status_code, 400)
        body = res.get_json()
        self.assertEqual(body['errors'], {'min_id': 'must be at least 1', 'q': 'must be at least 2 characters long'})
        self.assertIn('Invalid parameters', body['error'])
        self.assertEqual(self.client.get('/q/rules?min_id=26').status_code, 400)
        self.assertEqual(self.client.get('/q/rules?min_id=abc').get_json()['errors'],
                         {'min_id': 'must be an integer'})

    def test_json_body_values_are_checked_too(self):
        self.save_rules()
        ok = self.client.post('/q/rules?page_size=2', json={'params': {'min_id': 24}})
        self.assertEqual([r['actor_id'] for r in ok.get_json()], [24, 25])
        bad = self.client.post('/q/rules', json={'params': {'min_id': 'x'}})
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(bad.get_json()['errors'], {'min_id': 'must be an integer'})

    def test_rejected_requests_are_not_recorded_as_runs(self):
        # they never reached the database, and recording them would let callers flood the history
        self.save_rules()
        self.client.get('/q/rules?min_id=24')
        for _ in range(3):
            self.assertEqual(self.client.get('/q/rules?min_id=abc').status_code, 400)
        history = store.load_versions('rules')['1']['execution_history']
        self.assertEqual([e['status'] for e in history], ['success'])

    def test_undeclared_parameters_still_work_as_before(self):
        self.save('plain', sql='SELECT :x AS x', connection_name='lite')
        self.assertEqual(self.client.get('/q/plain?x=5').get_json(), [{'x': '5'}])


class SavedQueryOpenApiTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.save('rules', sql=RULES_SQL, query_parameters=RULES, connection_name='lite',
                  description='Actors from an id', tags=['demo', 'actors'])
        self.save('plain', sql='SELECT :x AS x', description='Echo')
        self.save('my query', sql='SELECT 1 AS one', connection_name='lite')

    def spec(self, **headers):
        return self.client.get('/openapi.json', headers=headers).get_json()

    def parameters(self, spec, path, method='get'):
        return {p['name']: p for p in spec['paths'][path][method]['parameters']}

    def test_every_saved_query_gets_a_documented_endpoint_with_its_rules(self):
        spec = self.spec()
        self.assertEqual({'/q/rules', '/q/plain', '/q/my%20query'} <= set(spec['paths']), True)
        operation = spec['paths']['/q/rules']['get']
        self.assertEqual(operation['summary'], 'Actors from an id')
        self.assertIn('demo, actors', operation['description'])
        p = self.parameters(spec, '/q/rules')
        self.assertEqual(p['min_id']['schema'], {'type': 'integer', 'minimum': 1, 'maximum': 25, 'default': 1})
        self.assertFalse(p['min_id']['required'])
        self.assertEqual(p['min_id']['description'], 'First actor id to include')
        self.assertEqual(p['q']['schema'], {'type': 'string', 'minLength': 2})
        self.assertFalse(p['q']['required'])

    def test_connection_is_required_only_when_the_query_has_no_default(self):
        spec = self.spec()
        self.assertFalse(self.parameters(spec, '/q/rules')['connection_name']['required'])
        self.assertEqual(self.parameters(spec, '/q/rules')['connection_name']['schema']['default'], 'lite')
        self.assertTrue(self.parameters(spec, '/q/plain')['connection_name']['required'])

    def test_undeclared_parameters_are_documented_as_required_text(self):
        x = self.parameters(self.spec(), '/q/plain')['x']
        self.assertEqual((x['required'], x['schema']), (True, {'type': 'string'}))

    def test_post_operation_describes_the_json_body(self):
        body = self.spec()['paths']['/q/rules']['post']['requestBody']['content']['application/json']['schema']
        self.assertEqual(body['properties']['params']['properties']['min_id']['default'], 1)
        self.assertNotIn('required', body['properties']['params'])  # nothing is required; OpenAPI forbids []
        plain = self.spec()['paths']['/q/plain']['post']['requestBody']
        self.assertEqual(plain['content']['application/json']['schema']['properties']['params']['required'], ['x'])

    def test_generic_documentation_is_still_there_and_operation_ids_are_unique(self):
        spec = self.spec()
        self.assertIn('/execute_sql', spec['paths'])
        self.assertIn('/q/{name}', spec['paths'])
        ids = [op['operationId'] for item in spec['paths'].values() for op in item.values()
               if isinstance(op, dict) and 'operationId' in op]
        self.assertEqual(len(ids), len(set(ids)))

    def test_the_sql_text_is_never_published(self):
        text = json.dumps(self.spec())
        for fragment in ('SELECT', 'FROM actor', 'LIKE :q', 'WHERE'):
            self.assertNotIn(fragment, text)

    def test_an_api_key_hides_the_saved_query_section_from_anonymous_readers(self):
        os.environ['QUERYAPIGATE_API_KEY'] = 'k3y'
        anonymous = self.spec()
        self.assertIn('/execute_sql', anonymous['paths'])                       # generic API stays public
        self.assertFalse([p for p in anonymous['paths'] if p in ('/q/rules', '/q/plain', '/q/my%20query')])
        self.assertNotIn('Actors from an id', json.dumps(anonymous))
        wrong = self.spec(**{'X-API-Key': 'nope'})
        self.assertNotIn('/q/rules', wrong['paths'])
        self.assertIn('/q/rules', self.spec(**{'X-API-Key': 'k3y'})['paths'])

    def test_unreadable_saved_files_do_not_break_the_document(self):
        # Simulates corrupt/unexpected stored data directly (bypassing normal save validation) - a
        # malformed fields_json ('junk', the equivalent of the old "not valid JSON" file) and a
        # semantically-wrong-but-parseable one ('odd', sql_query not even a string) must each be skipped,
        # not break the whole document.
        with db.transaction() as conn:
            conn.execute("INSERT INTO saved_queries (name, collection, example) VALUES ('junk', NULL, 0)")
            conn.execute("""INSERT INTO saved_query_versions
                (query_name, version, uuid, status, created_at, last_modified_at, fields_json)
                VALUES ('junk', 1, 'u', 'active', 'now', 'now', '{not json')""")
            conn.execute("INSERT INTO saved_queries (name, collection, example) VALUES ('odd', NULL, 0)")
            conn.execute("""INSERT INTO saved_query_versions
                (query_name, version, uuid, status, created_at, last_modified_at, fields_json)
                VALUES ('odd', 1, 'u', 'active', 'now', 'now', ?)""",
                (json.dumps({'sql_query': 12, 'query_parameters': 'weird'}),))
        spec = self.spec()
        self.assertIn('/q/rules', spec['paths'])
        self.assertNotIn('/q/junk', spec['paths'])
        self.assertNotIn('/q/odd', spec['paths'])

    def test_no_saved_queries_yet(self):
        for name in ('rules', 'plain', 'my query'):
            self.client.delete(f'/api/v1/queries/{name}')
        spec = self.spec()
        self.assertEqual([p for p in spec['paths'] if p.startswith('/q/') and p != '/q/{name}'], [])

    def test_the_document_is_valid_openapi(self):
        try:
            from openapi_spec_validator import validate
        except ImportError:
            self.skipTest('openapi-spec-validator is not installed (pip install -e ".[dev]")')
        validate(self.spec())                                     # with saved queries
        os.environ['QUERYAPIGATE_API_KEY'] = 'k3y'
        validate(self.spec())                                     # anonymous view of a keyed server
        validate(self.spec(**{'X-API-Key': 'k3y'}))
        for name in ('rules', 'plain', 'my query'):
            self.client.delete(f'/api/v1/queries/{name}', headers={'X-API-Key': 'k3y'})
        validate(self.spec())                                     # nothing saved yet

    def test_docs_page_lets_you_supply_an_api_key(self):
        page = self.client.get('/docs').get_data(as_text=True)
        self.assertIn('X-API-Key', page)
        self.assertIn('sessionStorage', page)
        self.assertIn('href="console/"', page)  # links to the admin UI

    def test_ui_moved_to_the_console(self):
        os.environ['QUERYAPIGATE_API_KEY'] = 'k3y'
        res = self.client.get('/ui')  # public, like the Console itself: old bookmarks keep working
        self.assertEqual(res.status_code, 301)
        self.assertEqual(res.headers['Location'], '/console/')

    def test_the_console_never_renders_unsanitized_html(self):
        # React escapes everything it renders; the only way around that is dangerouslySetInnerHTML (or a raw
        # .innerHTML assignment). The one deliberate use is Help's docs browser, which renders fetched markdown -
        # and only after DOMPurify.sanitize().
        src = Path(__file__).resolve().parent.parent / 'frontend' / 'src'
        users = {}
        for path in src.rglob('*.ts*'):
            text = path.read_text()
            if 'dangerouslySetInnerHTML' in text or re.search(r'\.innerHTML\s*=', text):
                users[path.relative_to(src).as_posix()] = text
        self.assertEqual(sorted(users), ['features/help/HelpPage.tsx'])
        self.assertIn('DOMPurify.sanitize(', users['features/help/HelpPage.tsx'])


class ParameterTests(ApiTestCase):
    def test_bound_parameters_accept_any_text_safely(self):
        nasty = "x'; DROP TABLE actor; --"
        res = self.run_sql('SELECT :n AS echo, name FROM actor WHERE actor_id = :id', params={'n': nasty, 'id': 3})
        self.assertEqual(res.get_json(), [{'echo': nasty, 'name': 'Actor 3'}])
        self.assertEqual(len(self.run_sql('SELECT * FROM actor', '?page_size=100').get_json()), 25)

    def test_bound_parameter_validation(self):
        self.assertEqual(self.run_sql('SELECT :a', params={}).status_code, 400)
        self.assertEqual(self.run_sql('SELECT :a', params={'a': [1]}).status_code, 400)
        self.assertEqual(self.run_sql('SELECT 1', params='nope').status_code, 400)

    def test_markers_inside_literals_and_casts_are_ignored(self):
        self.assertEqual(sqltools.named_parameters("SELECT ':a', \":b\", x::int, y, /* :c */ :d -- :e"), ['d'])

    def test_bind_styles(self):
        sql = "SELECT * FROM t WHERE a = :a AND b LIKE '50%' AND c = :a"
        self.assertEqual(sqltools.bind_parameters(sql, {'a': 1}, 'qmark'),
                         ("SELECT * FROM t WHERE a = ? AND b LIKE '50%' AND c = ?", [1, 1]))
        self.assertEqual(sqltools.bind_parameters(sql, {'a': 1}, 'format'),
                         ("SELECT * FROM t WHERE a = %s AND b LIKE '50%%' AND c = %s", [1, 1]))
        self.assertEqual(sqltools.bind_parameters(sql, {'a': 1}, 'pyformat'),
                         ("SELECT * FROM t WHERE a = %(a)s AND b LIKE '50%%' AND c = %(a)s", {'a': 1}))
        # no parameters: statement is untouched so drivers do not apply % formatting to it
        self.assertEqual(sqltools.bind_parameters("SELECT '50%'", {}, 'format'), ("SELECT '50%'", None))


FOREVER = 'WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) SELECT count(*) FROM c'


class TimeoutTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        os.environ.pop('QUERYAPIGATE_QUERY_TIMEOUT', None)

    def test_effective_timeout_rules(self):
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_QUERY_TIMEOUT': '10'}):
            self.assertEqual(config.effective_timeout(None), 10)
            self.assertEqual(config.effective_timeout(3), 3)      # a request may ask for less...
            self.assertEqual(config.effective_timeout(99), 10)    # ...but never more
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_QUERY_TIMEOUT': '0'}):
            self.assertIsNone(config.effective_timeout(None))     # 0 disables the server limit
            self.assertEqual(config.effective_timeout(99), 99)    # a request can still set its own
        for junk in ('abc', '-5', ''):
            with mock.patch.dict(os.environ, {'QUERYAPIGATE_QUERY_TIMEOUT': junk}):
                self.assertEqual(config.effective_timeout(None), config.DEFAULT_QUERY_TIMEOUT, junk)
        self.assertEqual(config.effective_timeout(None), config.DEFAULT_QUERY_TIMEOUT)

    def test_timeout_parameter_validation(self):
        for bad in ('abc', '0', '-1', 'nan', 'inf'):
            self.assertEqual(self.run_sql('SELECT 1', f'?timeout={bad}').status_code, 400, bad)
        self.assertEqual(self.run_sql('SELECT 1', '?timeout=5').status_code, 200)
        self.assertEqual(self.run_sql('SELECT 1', timeout=5).status_code, 200)  # also accepted in the body

    def test_runaway_query_is_cancelled_with_504(self):
        started = time.monotonic()
        res = self.run_sql(FOREVER, '?timeout=0.5')
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(res.status_code, 504)
        self.assertIn('time limit', res.get_json()['error'])
        self.assertEqual(res.get_json()['timeout'], 0.5)
        self.assertEqual(self.run_sql('SELECT 1 AS one').get_json(), [{'one': 1}])  # service still healthy

    def test_request_cannot_raise_the_server_limit(self):
        os.environ['QUERYAPIGATE_QUERY_TIMEOUT'] = '0.5'
        started = time.monotonic()
        res = self.run_sql(FOREVER, '?timeout=1000')
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(res.status_code, 504)

    def test_server_default_applies_without_a_request_timeout(self):
        os.environ['QUERYAPIGATE_QUERY_TIMEOUT'] = '0.5'
        self.assertEqual(self.run_sql(FOREVER).status_code, 504)

    def test_fast_queries_are_unaffected(self):
        res = self.run_sql('SELECT * FROM actor ORDER BY actor_id', '?timeout=5&page_size=3')
        self.assertEqual(len(res.get_json()), 3)

    def test_saved_query_timeout_is_recorded_in_history(self):
        self.save('slow', sql=FOREVER, connection_name='lite')
        self.assertEqual(self.client.get('/q/slow?timeout=0.5').status_code, 504)
        entry = store.load_versions('slow')['1']['execution_history'][0]
        self.assertEqual(entry['status'], 'error')
        self.assertIn('time limit', entry['error'])

    def test_timeout_is_not_treated_as_a_query_parameter(self):
        self.save('t', sql='SELECT :id AS id', connection_name='lite')
        self.assertEqual(self.client.get('/q/t?id=4&timeout=5').get_json(), [{'id': '4'}])

    def test_openapi_documents_the_timeout_parameter(self):
        spec = self.client.get('/openapi.json').get_json()
        names = [p['name'] for p in spec['paths']['/execute_sql']['post']['parameters']]
        self.assertIn('timeout', names)


class ValueCoercionTests(unittest.TestCase):
    def test_driver_subclasses_become_plain_types_so_yaml_can_dump_them(self):
        class JInt(int):
            pass

        class JDouble(float):
            pass

        class JString(str):
            pass

        dto = ResultSetDTO([(JInt(1), JDouble(2.5), JString('x'), True, None)], ['a', 'b', 'c', 'd', 'e'])
        self.assertEqual([type(v) for v in dto.rows[0]], [int, float, str, bool, type(None)])
        with create_app().test_request_context():
            self.assertIn('a: 1', dto.to_yaml().get_data(as_text=True))


class NamedQueryTests(ApiTestCase):
    def test_get_query_string_params_and_default_connection(self):
        self.save('by_id', sql='SELECT name FROM actor WHERE actor_id = :id',
                  query_parameters={'id': 'int'}, connection_name='lite')
        res = self.client.get('/q/by_id?id=4')
        self.assertEqual(res.get_json(), [{'name': 'Actor 4'}])
        self.assertEqual(self.client.get('/q/by_id?id=abc').status_code, 400)
        self.assertEqual(self.client.get('/q/by_id').status_code, 400)  # id missing
        self.assertEqual(self.client.get('/q/nope?id=1').status_code, 404)
        csv_res = self.client.get('/q/by_id?id=5&format=csv')
        self.assertEqual(csv_res.get_data(as_text=True).splitlines(), ['name', 'Actor 5'])

    def test_post_body_connection_override_and_version_selection(self):
        self.save('v', sql='SELECT 1 AS one')
        self.save('v', sql='SELECT 2 AS two')
        body = {'connection_name': 'lite'}
        self.assertEqual(self.client.post('/q/v', json=body).get_json(), [{'two': 2}])
        self.assertEqual(self.client.post('/q/v?version=1', json=body).get_json(), [{'one': 1}])
        self.assertEqual(self.client.post('/q/v?version=9', json=body).status_code, 404)
        self.assertEqual(self.client.post('/q/v', json={}).status_code, 400)  # no connection anywhere

    def test_execution_history_is_recorded_and_capped(self):
        self.save('h', sql='SELECT actor_id FROM actor WHERE actor_id = :id', connection_name='lite')
        self.client.get('/q/h?id=1')
        self.client.get('/q/h')  # fails: missing parameter
        history = store.load_versions('h')['1']['execution_history']
        self.assertEqual([e['status'] for e in history], ['success', 'error'])
        self.assertEqual(history[0]['rows'], 1)
        self.assertIn('duration_ms', history[0])
        self.assertIn('parameter', history[1]['error'])

        for _ in range(config.HISTORY_LIMIT + 5):
            self.client.get('/q/h?id=1')
        self.assertEqual(len(store.load_versions('h')['1']['execution_history']), config.HISTORY_LIMIT)

    def test_execution_history_entries_carry_the_request_id_and_calling_key(self):
        # Lets a slow/failed row an admin sees in the History tab be traced back to the structured log line
        # (or caller) that produced it, rather than only guessable by timestamp.
        self.save('h2', sql='SELECT actor_id FROM actor WHERE actor_id = :id', connection_name='lite')
        res = self.client.get('/q/h2?id=1')
        entry = store.load_versions('h2')['1']['execution_history'][0]
        self.assertEqual(entry['request_id'], res.headers['X-Request-Id'])
        self.assertEqual(entry['key_name'], '-')  # this fixture runs open, no API key configured

    def test_execution_history_entries_carry_serialization_time_separately_from_query_time(self):
        self.save('h3', sql='SELECT actor_id FROM actor WHERE actor_id = :id', connection_name='lite')
        self.client.get('/q/h3?id=1')
        entry = store.load_versions('h3')['1']['execution_history'][0]
        self.assertIn('serialization_ms', entry)
        self.assertIn('duration_ms', entry)  # query execution time - a separate measurement, see app.render()

    def test_delete_versions_and_files(self):
        self.save('x')
        self.save('x')
        self.assertEqual(self.client.delete('/api/v1/queries/x/versions/2').status_code, 200)
        self.assertEqual(self.client.delete('/api/v1/queries/x/versions/2').status_code, 404)
        versions = self.client.get('/api/v1/queries/x').get_json()['versions']
        self.assertEqual([v['version'] for v in versions], [1])
        self.assertEqual(self.client.delete('/api/v1/queries/x/versions/1').status_code, 204)  # the last: query gone
        self.assertFalse(store.saved_query_exists('x'))
        self.assertEqual(self.client.delete('/api/v1/queries/x').status_code, 404)
        self.assertEqual(self.client.delete('/api/v1/queries/..%2Fdb_connections').status_code, 404)

    def test_changing_or_deleting_a_connection_closes_its_pooled_connections(self):
        self.client.post('/api/v1/connections', json={'name': 'new', 'db': 'sqlite', 'database': 'x.db'})
        with mock.patch('queryapigate.pool.close_pooled_connections') as close:
            self.client.patch('/api/v1/connections/new', json={'database': 'y.db'})
            self.assertEqual(close.call_count, 1)
            self.client.delete('/api/v1/connections/new', json={'reason': 'cleanup'})
            self.assertEqual(close.call_count, 2)
            self.client.patch('/api/v1/connections/lite', json={'db': 'oracle'})  # rejected: no change
            self.assertEqual(close.call_count, 2)

    def test_delete_connection(self):
        self.assertEqual(self.client.delete('/api/v1/connections/off', json={'reason': 'no longer used'}).status_code,
                         204)
        self.assertNotIn('off', store.read_connections())
        self.assertEqual(self.client.delete('/api/v1/connections/off', json={'reason': 'again'}).status_code, 404)

    def test_delete_connection_requires_a_reason(self):
        for body in (None, {}, {'reason': ''}, {'reason': '   '}):
            kwargs = {'json': body} if body is not None else {}
            self.assertEqual(self.client.delete('/api/v1/connections/off', **kwargs).status_code, 400, body)
        self.assertIn('off', store.read_connections())  # never deleted

    def test_missing_connection_is_404_even_without_a_reason(self):
        self.assertEqual(self.client.delete('/api/v1/connections/nope').status_code, 404)


class PaginationAndFormatTests(ApiTestCase):
    def test_has_more_header_and_page_headers(self):
        res = self.run_sql('SELECT * FROM actor', '?page=2&page_size=10')
        self.assertEqual((res.headers['X-Page'], res.headers['X-Page-Size'], res.headers['X-Has-More']),
                         ('2', '10', 'true'))
        res = self.run_sql('SELECT * FROM actor', '?page=3&page_size=10')
        self.assertEqual(res.headers['X-Has-More'], 'false')
        self.assertEqual(len(res.get_json()), 5)
        res = self.run_sql('SELECT * FROM actor', '?page=1&page_size=25')  # exactly one full page
        self.assertEqual(res.headers['X-Has-More'], 'false')

    def test_ndjson(self):
        res = self.run_sql('SELECT actor_id, name FROM actor ORDER BY actor_id', '?format=ndjson&page_size=2')
        self.assertEqual(res.mimetype, 'application/x-ndjson')
        self.assertEqual([json.loads(line) for line in res.get_data(as_text=True).splitlines()],
                         [{'actor_id': 1, 'name': 'Actor 1'}, {'actor_id': 2, 'name': 'Actor 2'}])


class StreamingTests(ApiTestCase):
    """The memory-bounding claim itself (constant memory regardless of result size) needs a real database
    and a lot more than 25 rows to actually demonstrate - covered end-to-end in
    tests/test_integration.py's shared IntegrationBase.test_streaming_export_returns_every_row for every
    dialect, plus a live-process RSS check against real MySQL/PostgreSQL/ClickHouse servers during
    development (documented in runners.py's per-driver stream() docstrings). These tests cover what a
    25-row SQLite fixture *can* prove: the full result comes back (not one page), only csv/tsv/ndjson are
    accepted, page/page_size are rejected, writes are rejected even with QUERYAPIGATE_ALLOW_WRITES, a saved
    query's streamed run is recorded in its history without being cached, and a client disconnecting
    partway through does not surface as a request error.
    """
    def test_streams_every_row_not_one_page(self):
        res = self.run_sql('SELECT actor_id FROM actor ORDER BY actor_id', '?stream=true&format=csv')
        self.assertEqual(res.status_code, 200)
        lines = res.get_data(as_text=True).strip().splitlines()
        self.assertEqual(len(lines), 26)  # header + all 25 rows, not the default page size of 10
        self.assertEqual(lines[0], 'actor_id')
        self.assertEqual(lines[1:], [str(i) for i in range(1, 26)])

    def test_ndjson_stream(self):
        res = self.run_sql('SELECT actor_id, name FROM actor ORDER BY actor_id', '?stream=true&format=ndjson')
        self.assertEqual(res.mimetype, 'application/x-ndjson')
        rows = [json.loads(line) for line in res.get_data(as_text=True).splitlines()]
        self.assertEqual(len(rows), 25)
        self.assertEqual(rows[0], {'actor_id': 1, 'name': 'Actor 1'})

    def test_content_disposition_offers_a_filename(self):
        res = self.run_sql('SELECT * FROM actor', '?stream=true&format=csv')
        self.assertEqual(res.headers['Content-Disposition'], 'attachment; filename="lite.csv"')

    def test_stream_max_rows_truncates_the_export(self):
        # config.stream_max_rows() is read lazily, inside the generator that streams the body - not at
        # request-dispatch time - so the patched env var must still be in effect when .get_data() actually
        # drains it, not just when .post() is called.
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_STREAM_MAX_ROWS': '5'}):
            res = self.run_sql('SELECT actor_id FROM actor ORDER BY actor_id', '?stream=true&format=csv')
            lines = res.get_data(as_text=True).strip().splitlines()
        self.assertEqual(lines, ['actor_id', '1', '2', '3', '4', '5'])  # header + exactly the cap, not 26

    def test_stream_max_rows_does_not_affect_a_result_already_under_the_cap(self):
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_STREAM_MAX_ROWS': '1000'}):
            res = self.run_sql('SELECT actor_id FROM actor ORDER BY actor_id', '?stream=true&format=csv')
            line_count = len(res.get_data(as_text=True).strip().splitlines())
        self.assertEqual(line_count, 26)

    def test_a_malformed_stream_max_rows_is_rejected_at_startup(self):
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_STREAM_MAX_ROWS': 'not-a-number'}):
            self.assertRaises(ValueError, create_app)

    def test_saved_query_stream_history_reflects_the_truncated_row_count(self):
        self.save('capped', sql='SELECT * FROM actor ORDER BY actor_id', connection_name='lite')
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_STREAM_MAX_ROWS': '4'}):
            res = self.client.get('/q/capped?stream=true&format=csv')
            res.get_data()
        entry = store.load_versions('capped')['1']['execution_history'][0]
        self.assertEqual((entry['status'], entry['rows']), ('success', 4))

    def test_json_format_is_rejected(self):
        res = self.run_sql('SELECT * FROM actor', '?stream=true&format=json')
        self.assertEqual(res.status_code, 400)
        self.assertIn('csv', res.get_json()['error'])

    def test_xlsx_format_is_rejected(self):
        res = self.run_sql('SELECT * FROM actor', '?stream=true&format=xlsx')
        self.assertEqual(res.status_code, 400)

    def test_page_and_page_size_are_rejected(self):
        self.assertEqual(self.run_sql('SELECT * FROM actor', '?stream=true&format=csv&page=2').status_code, 400)
        self.assertEqual(
            self.run_sql('SELECT * FROM actor', '?stream=true&format=csv&page_size=5').status_code, 400)

    def test_writes_are_rejected_even_with_allow_writes(self):
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_ALLOW_WRITES': '1'}):
            res = self.run_sql('DELETE FROM actor', '?stream=true&format=csv')
            self.assertEqual(res.status_code, 403)
            self.assertEqual(len(self.run_sql('SELECT * FROM actor').get_json()) > 0, True)

    def test_empty_result_streams_just_the_header(self):
        res = self.run_sql('SELECT * FROM actor WHERE 1=0', '?stream=true&format=csv')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_data(as_text=True).strip(), 'actor_id,name,born')

    def test_saved_query_stream_is_recorded_in_history_and_not_cached(self):
        self.save('allrows', sql='SELECT * FROM actor ORDER BY actor_id', connection_name='lite', cache_ttl=60)
        res = self.client.get('/q/allrows?stream=true&format=csv')
        self.assertEqual(res.status_code, 200)
        res.get_data()  # history is only recorded once the streamed body is actually drained
        self.assertIsNone(res.headers.get('X-Cache'))
        entry = store.load_versions('allrows')['1']['execution_history'][0]
        self.assertEqual((entry['status'], entry['rows']), ('success', 25))

    def test_saved_query_stream_history_carries_the_request_id_and_calling_key(self):
        # request_id/key_name are captured up front in stream_sql_response(), before the response starts
        # streaming - by the time the wrapping generator (_record_stream_history) actually runs, the
        # request/app context that produced them is already gone (no stream_with_context() is used).
        self.save('allrows2', sql='SELECT * FROM actor ORDER BY actor_id', connection_name='lite')
        res = self.client.get('/q/allrows2?stream=true&format=csv')
        res.get_data()
        entry = store.load_versions('allrows2')['1']['execution_history'][0]
        self.assertEqual(entry['request_id'], res.headers['X-Request-Id'])
        self.assertEqual(entry['key_name'], '-')

    def test_a_client_disconnecting_partway_through_is_not_logged_as_an_error(self):
        # A real premature disconnect needs a real socket (see test_integration.py's own end-to-end streaming
        # checks); at the unit level, closing the generator early is exactly what the WSGI server does on one
        # (GeneratorExit), so this exercises the same code path in engine._drain / app._record_stream_history.
        columns, rows = engine.stream_sql('SELECT * FROM actor ORDER BY actor_id', 'lite')
        next(rows)
        rows.close()  # raises GeneratorExit inside the generator, same as an early WSGI response close

    def test_a_client_disconnecting_partway_through_still_settles_the_active_query_gauge(self):
        before = int(re.search(r'queryapigate_active_queries (-?\d+)', metrics.render()).group(1))
        columns, rows = engine.stream_sql('SELECT * FROM actor ORDER BY actor_id', 'lite')
        # stays active for as long as rows is open
        self.assertIn(f'queryapigate_active_queries {before + 1}', metrics.render())
        next(rows)
        rows.close()
        self.assertIn(f'queryapigate_active_queries {before}', metrics.render())  # settles back once closed

    def test_runaway_streamed_query_is_cancelled_with_504(self):
        started = time.monotonic()
        res = self.run_sql(FOREVER, '?stream=true&format=csv&timeout=0.5')
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(res.status_code, 504)
        self.assertIn('time limit', res.get_json()['error'])
        self.assertEqual(self.run_sql('SELECT 1 AS one').get_json(), [{'one': 1}])  # service still healthy


class ConnectionSecretsTests(ApiTestCase):
    def test_env_var_references_are_expanded_for_use_and_left_visible_in_listing(self):
        write_connections({'env': {'db': 'sqlite', 'database': '${IT_DB_PATH}', 'password': '${IT_PW}',
                                   'active': True}})
        conns = self.connections()
        self.assertEqual(conns['env']['password'], '${IT_PW}')
        res = self.client.post('/execute_sql', json={'sql': 'SELECT 1 AS one', 'connection_name': 'env'})
        self.assertEqual(res.status_code, 500)  # variable not set
        self.assertIn('IT_DB_PATH', res.get_json()['error'])
        with mock.patch.dict(os.environ, {'IT_DB_PATH': self.db_path, 'IT_PW': 'pw'}):
            res = self.client.post('/execute_sql', json={'sql': 'SELECT 1 AS one', 'connection_name': 'env'})
        self.assertEqual(res.get_json(), [{'one': 1}])

    def test_a_literal_password_triggers_a_startup_warning(self):
        write_connections({'plain': {'db': 'sqlite', 'database': self.db_path,
                                     'password': 'literal-secret', 'active': True}})
        with self.assertLogs('queryapigate', level=logging.WARNING) as logs:
            create_app()
        self.assertTrue(any('plain' in line and 'literal password' in line for line in logs.output))

    def test_an_env_var_reference_does_not_trigger_the_warning(self):
        write_connections({'env': {'db': 'sqlite', 'database': self.db_path,
                                   'password': '${IT_PW}', 'active': True}})
        with mock.patch('queryapigate.app.log.warning') as warning:
            create_app()
        for call in warning.call_args_list:
            self.assertNotIn('literal password', call.args[0])

    def test_an_empty_password_does_not_trigger_the_warning(self):
        write_connections({'nopass': {'db': 'sqlite', 'database': self.db_path,
                                      'password': '', 'active': True}})
        with mock.patch('queryapigate.app.log.warning') as warning:
            create_app()
        for call in warning.call_args_list:
            self.assertNotIn('literal password', call.args[0])

    def test_multiple_plaintext_connections_are_all_named_in_one_warning(self):
        write_connections({
            'first': {'db': 'sqlite', 'database': self.db_path, 'password': 'a', 'active': True},
            'second': {'db': 'sqlite', 'database': self.db_path, 'password': 'b', 'active': True}})
        with self.assertLogs('queryapigate', level=logging.WARNING) as logs:
            create_app()
        line = next(line for line in logs.output if 'literal password' in line)
        self.assertIn('first', line)
        self.assertIn('second', line)


class ServiceEndpointTests(ApiTestCase):
    @unittest.skipUnless(hasattr(time, 'tzset'), 'needs time.tzset() to change the process time zone')
    def test_health_says_which_zone_timestamps_are_in(self):
        def zone(tz):
            with mock.patch.dict(os.environ, {'TZ': tz}):
                time.tzset()
                body = self.client.get('/health').get_json()
            time.tzset()
            return body['time_zone'], body['utc_offset']

        self.addCleanup(time.tzset)
        self.assertEqual(zone('Asia/Kolkata'), ('Asia/Kolkata', '+05:30'))
        self.assertEqual(zone('Asia/Kathmandu'), ('Asia/Kathmandu', '+05:45'))  # zones without daylight saving,
        self.assertEqual(zone('America/Lima'), ('America/Lima', '-05:00'))      # so the offsets never change
        self.assertEqual(zone('UTC'), ('UTC', '+00:00'))

    def test_health_docs_and_openapi_are_public(self):
        os.environ['QUERYAPIGATE_API_KEY'] = 'k3y'
        self.assertEqual(self.client.get('/health').get_json()['status'], 'ok')
        self.assertEqual(self.client.get('/docs').status_code, 200)
        spec = self.client.get('/openapi.json').get_json()
        self.assertEqual(spec['openapi'], '3.0.3')
        self.assertIn('/q/{name}', spec['paths'])
        self.assertEqual(self.client.get('/api/v1/connections').status_code, 401)

    def test_root_redirects_to_docs_even_with_an_api_key(self):
        os.environ['QUERYAPIGATE_API_KEY'] = 'k3y'
        res = self.client.get('/')
        self.assertEqual(res.status_code, 302)
        self.assertTrue(res.headers['Location'].endswith('/docs'))
        self.assertEqual(self.client.get('/favicon.ico').status_code, 204)

    def test_unknown_route_returns_json(self):
        res = self.client.get('/nope')
        self.assertEqual(res.status_code, 404)
        self.assertIn('error', res.get_json())


def _mongo_cursor(docs):
    """A chainable fake of a pymongo Cursor: .sort()/.skip()/.limit()/.max_time_ms() all return itself,
    same as the real thing, and iterating it yields ``docs`` - matches how runners.mongo_find() calls it."""
    cursor = mock.MagicMock()
    cursor.sort.return_value = cursor
    cursor.skip.return_value = cursor
    cursor.limit.return_value = cursor
    cursor.max_time_ms.return_value = cursor
    cursor.__iter__.return_value = iter(docs)
    return cursor


def _mongo_client(docs=(), collection_names=(), database_names=()):
    """A fake pymongo.MongoClient: client[db][collection].find(...) is a _mongo_cursor(docs), client.admin.
    command('ping') succeeds, and list_database_names()/list_collection_names() return the given names."""
    coll = mock.MagicMock()
    coll.find.return_value = _mongo_cursor(list(docs))
    database = mock.MagicMock()
    database.__getitem__.return_value = coll
    database.list_collection_names.return_value = list(collection_names)
    client = mock.MagicMock()
    client.__getitem__.return_value = database
    client.list_database_names.return_value = list(database_names)
    client.admin.command.return_value = {'ok': 1.0}
    return client


class MongoConnectionTests(ApiTestCase):
    """Mirrors TestConnectionTests/ListDatabasesTests, but for the mongo dialect - pymongo.MongoClient is
    mocked (see _mongo_client() above) since these tests run without a real MongoDB server."""

    def test_connection_test_pings_the_server(self):
        with mock.patch('pymongo.MongoClient', return_value=_mongo_client()):
            res = self.client.post('/api/v1/connections/test', json={
                'db': 'mongo', 'host': 'h', 'user': 'u', 'password': 'p', 'database': 'd'})
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        self.assertIn('elapsed_ms', res.get_json())

    def test_connection_test_failure_redacts_the_password(self):
        with mock.patch('pymongo.MongoClient', side_effect=Exception('auth failed for secret')):
            res = self.client.post('/api/v1/connections/test', json={
                'db': 'mongo', 'host': 'h', 'user': 'u', 'password': 'secret', 'database': 'd'})
        self.assertEqual(res.status_code, 502)
        self.assertNotIn('secret', res.get_data(as_text=True))

    def test_list_databases(self):
        client = _mongo_client(database_names=['orders', 'billing'])
        with mock.patch('pymongo.MongoClient', return_value=client):
            res = self.client.post('/api/v1/connections/databases',
                                   json={'db': 'mongo', 'host': 'h', 'user': 'u', 'database': 'd'})
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        self.assertEqual(res.get_json()['databases'], ['orders', 'billing'])

    def test_schema_lists_collections_not_columns(self):
        put_connections(self.client, {
            'mg': {'db': 'mongo', 'host': 'h', 'user': 'u', 'database': 'd', 'active': True}})
        client = _mongo_client(collection_names=['orders', 'users'])
        with mock.patch('pymongo.MongoClient', return_value=client):
            res = self.client.get('/connections/mg/schema')
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        body = res.get_json()
        self.assertEqual([t['name'] for t in body['tables']], ['orders', 'users'])
        self.assertTrue(all(t['type'] == 'collection' and t['columns'] == [] for t in body['tables']))


class MongoQueryTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        put_connections(self.client, {
            'mg': {'db': 'mongo', 'host': 'h', 'user': 'u', 'database': 'd', 'active': True}})

    def test_ad_hoc_find(self):
        docs = [{'_id': 1, 'name': 'a'}, {'_id': 2, 'name': 'b'}]
        with mock.patch('pymongo.MongoClient', return_value=_mongo_client(docs=docs)):
            res = self.client.post('/execute_mongo', json={
                'collection': 'users', 'filter': {}, 'connection_name': 'mg'})
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        rows = res.get_json()
        self.assertEqual(len(rows), 2)
        self.assertEqual({r['name'] for r in rows}, {'a', 'b'})

    def test_pagination_has_more_header(self):
        # page_size defaults to 10; 11 docs back means the "limit + 1" trick correctly reports another page.
        docs = [{'_id': i} for i in range(11)]
        with mock.patch('pymongo.MongoClient', return_value=_mongo_client(docs=docs)):
            res = self.client.post('/execute_mongo', json={'collection': 'users', 'connection_name': 'mg'})
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        self.assertEqual(res.headers.get('X-Has-More'), 'true')
        self.assertEqual(len(res.get_json()), 10)

    def test_where_is_rejected(self):
        res = self.client.post('/execute_mongo', json={
            'collection': 'users', 'filter': {'$where': 'true'}, 'connection_name': 'mg'})
        self.assertEqual(res.status_code, 403)

    def test_collection_is_required(self):
        res = self.client.post('/execute_mongo', json={'filter': {}, 'connection_name': 'mg'})
        self.assertEqual(res.status_code, 400)

    def test_saved_mongo_query_runs_through_q_name(self):
        saved = save_query(self.client, {
            'filename': 'active_users', 'author': 'a', 'description': 'd', 'query_type': 'mongo',
            'mongo_collection': 'users', 'mongo_filter': {'active': ':active'},
            'query_parameters': {'active': 'bool'}, 'connection_name': 'mg'})
        self.assertEqual(saved.status_code, 201, saved.get_data(as_text=True))
        with mock.patch('pymongo.MongoClient', return_value=_mongo_client(docs=[{'_id': 1}])) as ctor:
            res = self.client.get('/q/active_users?active=true')
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        self.assertEqual(len(res.get_json()), 1)
        self.assertTrue(ctor.called)  # actually reached the driver, not short-circuited before execution

    def test_saved_mongo_query_substitutes_the_placeholder_into_the_real_filter(self):
        save_query(self.client, {
            'filename': 'by_status', 'author': 'a', 'description': 'd', 'query_type': 'mongo',
            'mongo_collection': 'orders', 'mongo_filter': {'status': ':status'}, 'connection_name': 'mg'})
        coll = mock.MagicMock()
        coll.find.return_value = _mongo_cursor([])
        database = mock.MagicMock()
        database.__getitem__.return_value = coll
        client = mock.MagicMock()
        client.__getitem__.return_value = database
        with mock.patch('pymongo.MongoClient', return_value=client):
            res = self.client.get('/q/by_status?status=shipped')
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        self.assertEqual(coll.find.call_args.args[0], {'status': 'shipped'})

    def test_scoped_key_without_a_grant_on_the_connection_is_refused(self):
        os.environ['QUERYAPIGATE_API_KEY'] = 'admin-key'
        self.client = create_app().test_client()
        admin = {'X-API-Key': 'admin-key'}
        put_connections(self.client, {
            'mg': {'db': 'mongo', 'host': 'h', 'user': 'u', 'database': 'd', 'active': True}}, headers=admin)
        save_query(self.client, {
            'filename': 'orders', 'author': 'a', 'description': 'd', 'query_type': 'mongo',
            'mongo_collection': 'orders', 'mongo_filter': {}, 'connection_name': 'mg'}, headers=admin)
        scoped = self.client.post('/api/v1/api-keys', json={'name': 'scoped', 'connections': []}, headers=admin)
        key = scoped.get_json()['secret']
        res = self.client.get('/q/orders', headers={'X-API-Key': key})
        self.assertEqual(res.status_code, 403)

    def test_streaming_is_not_supported_yet(self):
        save_query(self.client, {
            'filename': 'orders2', 'author': 'a', 'description': 'd', 'query_type': 'mongo',
            'mongo_collection': 'orders', 'mongo_filter': {}, 'connection_name': 'mg'})
        res = self.client.get('/q/orders2?stream=true')
        self.assertEqual(res.status_code, 400)

    def test_openapi_and_catalog_list_a_saved_mongo_query(self):
        save_query(self.client, {
            'filename': 'orders3', 'author': 'a', 'description': 'd', 'query_type': 'mongo',
            'mongo_collection': 'orders', 'mongo_filter': {'status': ':status'},
            'query_parameters': {'status': 'string'}, 'connection_name': 'mg'})
        spec = self.client.get('/openapi.json').get_json()
        self.assertIn('/q/orders3', spec['paths'])  # not silently excluded, unlike before _is_runnable() existed
        param_names = [p['name'] for p in spec['paths']['/q/orders3']['get']['parameters']]
        self.assertIn('status', param_names)  # the declared mongo_filter parameter made it through
        catalog_names = [q['name'] for q in self.client.get('/catalog').get_json()['queries']]
        self.assertIn('orders3', catalog_names)


if __name__ == '__main__':
    unittest.main()

"""The plans a Rust data plane applies (dataplane.py, ADR 0028): which `/q/` calls get one, what it holds, and - the
spec the data plane is built to - that a plan, applied as the data plane applies it, sends exactly the SQL the runner
sends. The PostgreSQL and MySQL runners are replaced by recording fakes, so no database is needed."""
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from queryapigate import apikeys, create_app, dataplane, runners, schema, sqltools
from tests.helpers import save_query, write_connections

ADMIN = {'X-API-Key': 'admin-key'}
SECRET = 'dp-secret'
COLUMNS = ['id', 'email', 'name']
TABLES = {'tables': [{'name': 'customers', 'columns': [{'name': c} for c in COLUMNS]}]}


class PlanTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': self.tmp.name, 'QUERYAPIGATE_API_KEY': 'admin-key',
                                               'QUERYAPIGATE_HISTORY_FLUSH_INTERVAL': '0',
                                               'QUERYAPIGATE_MASK_KEY': 'test-key',
                                               'QUERYAPIGATE_DATAPLANE_SECRET': SECRET})
        patcher.start()
        self.addCleanup(patcher.stop)
        lite = os.path.join(self.tmp.name, 'lite.db')
        conn = sqlite3.connect(lite)
        conn.execute('CREATE TABLE customers (id INTEGER, email TEXT, name TEXT)')
        conn.execute("INSERT INTO customers VALUES (1, 'a@example.com', 'A')")
        conn.commit()
        conn.close()
        server = {'host': 'db.internal', 'database': 'shop', 'user': 'u', 'active': True}
        write_connections({'pg': {'db': 'postgres', **server}, 'my': {'db': 'mysql', **server},
                           'lite': {'db': 'sqlite', 'database': lite, 'active': True}})
        self.sent = []
        fake = {'postgres': self.runner, 'mysql': self.runner}
        for patch in (mock.patch.dict(runners.RUNNERS, fake), mock.patch.object(schema, 'fetch_schema',
                                                                               return_value=TABLES)):
            patch.start()
            self.addCleanup(patch.stop)
        self.client = create_app().test_client()
        for name, connection, sql, parameters in (
                ('by_id', 'pg', 'SELECT id, email, name FROM customers WHERE id = :id', {'id': 'int'}),
                ('my_by_id', 'my', 'SELECT id, email, name FROM customers WHERE id = :id', {'id': 'int'}),
                ('paged', 'pg', 'SELECT id, email, name FROM customers ORDER BY id LIMIT :n OFFSET 2', {'n': 'int'}),
                ('lite_by_id', 'lite', 'SELECT id, email, name FROM customers WHERE id = :id', {'id': 'int'}),
                ('text_placeholder', 'pg', 'SELECT id, email, name FROM customers WHERE id = {id}', {'id': 'int'}),
                ('with_first', 'my', 'WITH c AS (SELECT * FROM customers) SELECT id, email, name FROM c', {})):
            res = save_query(self.client, headers=ADMIN, body={'filename': name, 'connection_name': connection,
                                                              'sql_query': sql, 'query_parameters': parameters,
                                                              'description': name})
            self.assertEqual(res.status_code, 201, res.get_data(as_text=True))
        everything = ['pg', 'my', 'lite']
        self.plain = apikeys.create_key('plain', connections=everything)
        self.masked = apikeys.create_key('masked', connections=everything,
                                         mask=[{'column': 'customers.email', 'action': 'hash'}])
        self.restricted = apikeys.create_key('restricted', connections=everything, allowed_tables=['customers'])
        self.limited = apikeys.create_key('limited', connections=everything, rate_limit='100/minute')

    def runner(self, details, sql, params, limit, offset, read_only, timeout=None, pool=None, **options):
        self.sent.append({'sql': sql, 'params': params, 'limit': limit, 'offset': offset, 'read_only': read_only,
                          'options': options})
        return COLUMNS, [(1, 'a@example.com', 'A')]

    def call(self, path, key, secret=SECRET):
        headers = {'X-API-Key': key, **({dataplane.SECRET_HEADER: secret} if secret else {})}
        res = self.client.get(path, headers=headers)
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        value = res.headers.get(dataplane.PLAN_HEADER)
        return res, dataplane.decode(value) if value else None


class WhoGetsAPlanTests(PlanTestCase):
    def test_a_scoped_key_on_postgres_gets_one_from_the_data_plane(self):
        _res, plan = self.call('/q/by_id?id=1', self.plain)
        self.assertIsNotNone(plan)
        self.assertEqual(plan['key'], {'workspace': 'default', 'name': 'plain'})
        self.assertEqual(plan['query'], {'name': 'by_id', 'version': 1})
        self.assertEqual(plan['connection'], {'name': 'pg', 'dialect': 'postgres'})
        self.assertEqual(set(plan['rows']), {'api_key', 'saved_query', 'connection'})
        self.assertIsNone(plan['mask'])
        self.assertIsNone(plan['search_path'])
        self.assertEqual(plan['used'], ['id'])

    def test_and_on_mysql(self):
        _res, plan = self.call('/q/my_by_id?id=1', self.plain)
        self.assertEqual(plan['connection']['dialect'], 'mysql')
        self.assertEqual(plan['statement']['hint_at'], 6)

    def test_never_without_the_secret_or_with_a_wrong_one(self):
        for secret in (None, 'wrong', SECRET + 'x', ''):
            with self.subTest(secret=secret):
                self.assertIsNone(self.call('/q/by_id?id=1', self.plain, secret=secret)[1])

    def test_never_when_the_server_has_no_secret_set(self):
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_DATAPLANE_SECRET': ''}):
            self.assertIsNone(self.call('/q/by_id?id=1', self.plain)[1])

    def test_never_for_an_administrator(self):
        res = self.client.get('/q/by_id?id=1', headers={**ADMIN, dataplane.SECRET_HEADER: SECRET})
        self.assertIsNone(res.headers.get(dataplane.PLAN_HEADER))

    def test_never_for_a_key_with_a_rate_limit(self):
        self.assertIsNone(self.call('/q/by_id?id=1', self.limited)[1])

    def test_never_on_a_connection_the_prototype_does_not_serve(self):
        self.assertIsNone(self.call('/q/lite_by_id?id=1', self.plain)[1])

    def test_never_for_a_text_placeholder_whose_value_is_part_of_the_sql(self):
        self.assertIsNone(self.call('/q/text_placeholder?id=1', self.plain)[1])

    def test_never_for_mysql_sql_the_time_limit_hint_cannot_go_on(self):
        self.assertIsNone(self.call('/q/with_first', self.plain)[1])

    def test_never_for_another_format_or_a_stream(self):
        for path in ('/q/by_id?id=1&format=csv', '/q/by_id?id=1&format=xml'):
            with self.subTest(path=path):
                self.assertIsNone(self.call(path, self.plain)[1])

    def test_a_refusal_gets_none(self):
        res = self.client.get('/q/by_id?id=x', headers={'X-API-Key': self.plain, dataplane.SECRET_HEADER: SECRET})
        self.assertEqual(res.status_code, 400)
        self.assertIsNone(res.headers.get(dataplane.PLAN_HEADER))

    def test_the_rows_digests_change_with_the_key_s_grants(self):
        before = self.call('/q/by_id?id=1', self.restricted)[1]['rows']
        apikeys.update_key('restricted', allowed_tables=['customers', 'orders'])
        after = self.call('/q/by_id?id=1', self.restricted)[1]['rows']
        self.assertNotEqual(before['api_key'], after['api_key'])
        self.assertEqual(before['saved_query'], after['saved_query'])

    def test_the_metadata_cache_is_bypassed_for_the_data_plane(self):
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_METADATA_CACHE_TTL': '60'}):
            self.call('/q/by_id?id=1', self.restricted)  # cached for ordinary calls, from here on
            apikeys.update_key('restricted', allowed_tables=['orders'])
            res = self.client.get('/q/by_id?id=1', headers={'X-API-Key': self.restricted,
                                                             dataplane.SECRET_HEADER: SECRET})
        self.assertEqual(res.status_code, 403)  # the fresh grant, not the cached one


class WhatAPlanHoldsTests(PlanTestCase):
    def test_the_statement_in_segments_around_its_parameters(self):
        statement = self.call('/q/by_id?id=1', self.plain)[1]['statement']
        self.assertEqual(statement['segments'], ['SELECT id, email, name FROM customers WHERE id = ', {'param': 'id'},
                                                 ''])
        self.assertTrue(statement['paginated'])
        self.assertIsNone(statement['limit'])
        self.assertIsNone(statement['hint_at'])

    def test_its_own_window_apart_to_be_resolved_per_call(self):
        statement = self.call('/q/paged?n=5', self.plain)[1]['statement']
        self.assertEqual(statement['segments'], ['SELECT id, email, name FROM customers ORDER BY id'])
        self.assertEqual(statement['limit'], {'param': 'n', 'clause': 'LIMIT'})
        self.assertEqual(statement['offset'], {'value': 2})

    def test_a_masked_key_s_actions_by_column_and_when_they_expire(self):
        plan = self.call('/q/by_id?id=1', self.masked)[1]
        self.assertEqual(plan['mask']['columns'], 3)
        self.assertEqual(plan['mask']['actions'], [None, 'hash', None])
        self.assertEqual([f['column'] for f in plan['mask']['fired']], ['email'])
        self.assertTrue(0 < plan['expires_in'] <= 60)

    def test_a_table_restricted_postgres_call_pins_its_search_path(self):
        self.assertEqual(self.call('/q/by_id?id=1', self.restricted)[1]['search_path'], 'public')


def literal(value):
    """A parameter's value as SQL text - what both drivers' client-side `format` binding puts in its place, for the
    values params.py lets through. The data plane quotes strings for its dialect; this is enough for comparing."""
    if value is None:
        return 'NULL'
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, (int, float)):
        return repr(value)
    return "'" + value.replace("'", "''") + "'"


def as_runner_sends(sql, params, limit, offset, dialect, hint=None):
    """The text a driver receives from the runner (runners._prepare(), and the MySQL hint), with its `%s` markers
    filled as client-side binding fills them."""
    sql, args, _sliced = runners._prepare(sql, params, 'format', limit, offset, dialect)
    if hint is not None:
        end = runners._leading_select(sql)
        sql = f'{sql[:end]} {hint}{sql[end:]}'
    if args is None:
        return sql
    return sql % tuple(literal(a) for a in args)


def as_plan_sends(statement, params, limit, offset, hint=None):
    """The same text, built from a plan the way the data plane builds it - the algorithm it implements."""
    pieces = []
    for i, segment in enumerate(statement['segments']):
        if isinstance(segment, dict):
            pieces.append(literal(params[segment['param']]))
        elif i == 0 and hint is not None:
            at = statement['hint_at']
            pieces.append(f'{segment[:at]} {hint}{segment[at:]}')
        else:
            pieces.append(segment)
    sql = ''.join(pieces)
    if not statement['paginated']:
        return sql
    window = limit + 1  # the one-more-row probe for has_more
    own_limit, own_offset = (None if statement[part] is None else sqltools._window_value(
        str(statement[part]['value']) if 'value' in statement[part] else ':' + statement[part]['param'], params,
        statement[part].get('clause', part.upper())) for part in ('limit', 'offset'))
    own_offset = own_offset or 0
    if own_limit is not None:
        window = max(0, min(window, own_limit - offset))
    return f'{sql}\nLIMIT {window} OFFSET {own_offset + offset}'


STATEMENTS = [
    ('SELECT * FROM t WHERE id = :id', {'id': 7}),
    ('SELECT * FROM t WHERE name = :name AND id > :id', {'name': "O'Brien", 'id': 1}),
    ("SELECT ':not_a_param', id FROM t WHERE a = :a", {'a': 'x'}),
    ('SELECT id FROM t -- :nor_this\nWHERE a = :a', {'a': 1}),
    ("SELECT '100%' AS pct, id FROM t WHERE a = :a", {'a': 'x%y'}),
    ('/* 50% off */ SELECT id FROM t WHERE a = :a', {'a': 2}),
    ('SELECT id FROM t', {}),
    ('SELECT id FROM t LIMIT 3', {}),
    ('SELECT id FROM t LIMIT :n', {'n': 4}),
    ('SELECT id FROM t LIMIT :n OFFSET :m', {'n': 5, 'm': '2'}),
    ('SELECT id FROM t LIMIT ALL', {}),
    ('SELECT id FROM t LIMIT 2, 10', {}),
    ('SELECT id FROM t OFFSET 3', {}),
    ('SELECT id FROM t OFFSET 1 ROWS FETCH FIRST 2 ROWS ONLY', {}),
    ('SELECT id FROM t FETCH FIRST ROW ONLY', {}),
    ("SELECT 'x LIMIT 3' AS s FROM t WHERE a = :a", {'a': True}),
    ('SELECT * FROM (SELECT * FROM t LIMIT 2) s WHERE a = :a', {'a': None}),
    ('WITH c AS (SELECT * FROM t WHERE a = :a) SELECT * FROM c LIMIT 10', {'a': 1.5}),
    ('SELECT id FROM t WHERE a = :a AND b = :a', {'a': 3}),
    ('SHOW TABLES', {}),
]


class AppliedAsTheDataPlaneDoesTests(unittest.TestCase):
    """A plan's statement, built per call as the data plane builds it, is the text the runner sends - for every way a
    statement ends, binds and pages. This is the spec the Rust side is tested against too."""

    def test_the_same_sql_as_the_runner_for_every_window_and_page(self):
        for dialect in dataplane.DIALECTS:
            for sql, params in STATEMENTS:
                statement = dataplane.statement(sql, dialect)
                if statement is None:
                    self.assertEqual(dialect, 'mysql')
                    self.assertIsNone(runners._leading_select(sql))
                    continue
                hint = '/*+ MAX_EXECUTION_TIME(500) */' if dialect == 'mysql' else None
                for limit, offset in ((10, 0), (1, 0), (2, 1), (3, 5), (100, 40)):
                    with self.subTest(dialect=dialect, sql=sql, limit=limit, offset=offset):
                        expected = as_runner_sends(sql, params, limit, offset, dialect, hint)
                        self.assertEqual(as_plan_sends(statement, params, limit, offset, hint), expected)

    def test_a_statement_that_is_not_paged_is_sent_as_written(self):
        statement = dataplane.statement('SHOW TABLES', 'postgres')
        self.assertFalse(statement['paginated'])
        self.assertEqual(statement['segments'], ['SHOW TABLES'])


if __name__ == '__main__':
    unittest.main()


class ClientAddressTests(unittest.TestCase):
    """Behind the data plane every request comes over loopback: the client's own address travels in X-QAG-Peer, taken
    only beside the secret - so open-access mode's loopback rule and a key's allowed_ips judge the client, not it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': self.tmp.name,
                                               'QUERYAPIGATE_DATAPLANE_SECRET': SECRET})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop('QUERYAPIGATE_API_KEY', None)
        write_connections({})
        self.client = create_app().test_client()

    def get(self, path, peer=None, secret=SECRET, **headers):
        if secret is not None:
            headers[dataplane.SECRET_HEADER] = secret
        if peer is not None:
            headers[dataplane.PEER_HEADER] = peer
        return self.client.get(path, headers=headers)  # the test client's own address is 127.0.0.1

    def test_open_access_mode_refuses_a_remote_client_the_data_plane_forwards(self):
        res = self.get('/catalog', peer='203.0.113.9')
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.get_json()['code'], 'open_access_remote')
        self.assertEqual(self.get('/catalog', peer='127.0.0.1').status_code, 200)

    def test_a_peer_that_is_not_an_address_counts_as_remote(self):
        for peer in ('', 'localhost', '127.0.0.1, 10.0.0.1', None):
            with self.subTest(peer=peer):
                self.assertEqual(self.get('/catalog', peer=peer).status_code, 403)

    def test_without_the_secret_a_client_s_x_qag_peer_is_ignored(self):
        for secret in (None, 'wrong'):
            with self.subTest(secret=secret):
                # the real peer, loopback, decides - a client can't claim another address, nor lose its own
                self.assertEqual(self.get('/catalog', peer='203.0.113.9', secret=secret).status_code, 200)

    def test_a_key_s_allowed_ips_judge_the_client(self):
        key = apikeys.create_key('office', connections=[], allowed_ips=['10.0.0.0/8'])
        self.assertEqual(self.get('/catalog', peer='10.1.2.3', **{'X-API-Key': key}).status_code, 200)
        self.assertEqual(self.get('/catalog', peer='192.0.2.1', **{'X-API-Key': key}).status_code, 401)


class RunReportTests(PlanTestCase):
    """The data plane reports the runs it served (POST /internal/dataplane/runs): each recorded as run_saved() records
    one, and counted in /metrics - and nothing without its secret."""

    RUN = {'query': 'by_id', 'version': 1, 'entry': {
        'executed_at': '2026-10-27 12:00:00', 'connection_name': 'pg', 'request_id': 'dp-run-1', 'key_name': 'plain',
        'transport': 'rest', 'status': 'success', 'rows': 3, 'duration_ms': 1, 'serialization_ms': 0.1},
        'metrics': {'dialect': 'postgres', 'query_seconds': 0.001, 'serialization_seconds': 0.0001, 'method': 'GET',
                    'status': 200, 'seconds': 0.002}}

    def report(self, secret=SECRET):
        headers = {dataplane.SECRET_HEADER: secret} if secret else {}
        return self.client.post('/internal/dataplane/runs', json={'runs': [self.RUN]}, headers=headers)

    def history(self):
        from queryapigate import store
        return store.load_versions('by_id')['1']['execution_history']

    def test_without_the_secret_it_isn_t_there(self):
        for secret in (None, 'wrong'):
            with self.subTest(secret=secret):
                self.assertEqual(self.report(secret).status_code, 404)
        self.assertEqual(self.history(), [])

    def test_a_reported_run_is_recorded_as_python_records_one(self):
        self.assertEqual(self.report().status_code, 204)
        self.assertEqual(self.history(), [self.RUN['entry']])

    def test_and_counted_in_metrics(self):
        import re

        def counts():  # /metrics counts for the whole process, so before and after
            text = self.client.get('/metrics').get_data(as_text=True)
            found = {}
            for name in ('queryapigate_requests_total{method="GET",endpoint="api.run_named_query",status="200",'
                         'key="plain"}', 'queryapigate_rows_returned_total{connection="pg",dialect="postgres",'
                         'key="plain"}'):
                match = re.search(re.escape(name) + r' (\d+)', text)
                found[name] = int(match.group(1)) if match else 0
            return list(found.values())
        before = counts()
        self.report()
        self.assertEqual([after - was for after, was in zip(counts(), before, strict=True)], [1, 3])

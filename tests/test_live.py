"""Live queries (live.py, ADR 0033 - a prototype): a real `queryapigate events` server with QUERYAPIGATE_LIVE_QUERIES
on, subscribed to over plain HTTP, kept fresh by writes through the main server and by signed Debezium posts."""
import base64
import hashlib
import hmac
import json
import os
import sqlite3
import tempfile
import time
import types
import unittest
import uuid
from unittest import mock

from queryapigate import create_app, live
from queryapigate.errors import ApiError
from tests.helpers import save_query, write_connections
from tests.test_events_server import EventServerTestCase, Stream
from tests.test_jwt import SECRET as JWT_SECRET
from tests.test_jwt import bearer
from tests.test_jwt import token as jwt_token

SECRET = 'whsec_' + base64.b64encode(b'0123456789abcdef0123456789abcdef').decode()


def signed(body, secret=SECRET, msg_id='msg_1', stamp=None):
    """Standard Webhooks headers for ``body``, as Debezium Server's HTTP sink sends them."""
    stamp = str(int(time.time()) if stamp is None else stamp)
    key = base64.b64decode(secret.removeprefix('whsec_'))
    signature = base64.b64encode(hmac.new(key, f'{msg_id}.{stamp}.'.encode() + body, hashlib.sha256).digest())
    return {'webhook-id': msg_id, 'webhook-timestamp': stamp, 'webhook-signature': f'v1,{signature.decode()}',
            'Content-Type': 'application/json'}


def change(table, op='u', schema='main', name='shop'):
    """One Debezium change event, wrapped as the JSON converter does with schemas enabled."""
    return {'schema': {}, 'payload': {'before': None, 'after': {'id': 1}, 'op': op, 'ts_ms': 1,
                                      'source': {'name': name, 'connector': 'postgresql', 'db': 'shop',
                                                 'schema': schema, 'table': table}}}


class SignatureTests(unittest.TestCase):
    def test_accepts_its_own_signature(self):
        body = b'{"a": 1}'
        self.assertTrue(live.verify_signature(SECRET, signed(body), body))

    def test_refuses_a_changed_body_another_secret_and_an_old_timestamp(self):
        body = b'{"a": 1}'
        self.assertFalse(live.verify_signature(SECRET, signed(body), b'{"a": 2}'))
        other = 'whsec_' + base64.b64encode(b'x' * 32).decode()
        self.assertFalse(live.verify_signature(SECRET, signed(body, secret=other), body))
        self.assertFalse(live.verify_signature(SECRET, signed(body, stamp=time.time() - 600), body))
        self.assertFalse(live.verify_signature(SECRET, {}, body))
        self.assertFalse(live.verify_signature(None, signed(body), body))

    def test_any_of_several_signatures_may_match(self):  # a sender rotating its secret signs with both
        body = b'{}'
        headers = signed(body)
        headers['webhook-signature'] = 'v1,bm9wZQ== ' + headers['webhook-signature']
        self.assertTrue(live.verify_signature(SECRET, headers, body))


class DebeziumTablesTests(unittest.TestCase):
    def test_reduces_a_batch_to_its_tables(self):
        body = json.dumps([change('Film'), change('film', 'c'), change('rental', 'd', schema='public'),
                           {'before': None, 'after': {}, 'op': 'c', 'source': {'db': 'shop', 'table': 'stock'}}])
        self.assertEqual(live.debezium_tables(body), {'main.film', 'public.rental', 'shop.stock'})

    def test_a_mongodb_change_names_its_collection_in_its_database(self):
        body = json.dumps({'before': None, 'after': '{"_id": 1}', 'op': 'u', 'ts_ms': 1,
                           'source': {'name': 'shop', 'connector': 'mongodb', 'db': 'rental', 'rs': 'rs0',
                                      'collection': 'Films'}})
        self.assertEqual(live.debezium_tables(body, server_name='shop'), {'rental.films'})

    def test_snapshots_tombstones_and_heartbeats_touch_nothing(self):
        self.assertEqual(live.debezium_tables(json.dumps([change('film', 'r'), None, {'ts_ms': 1}])), set())
        self.assertEqual(live.debezium_tables(''), set())

    def test_refuses_another_server_and_what_isnt_json(self):
        with self.assertRaises(ValueError):
            live.debezium_tables(json.dumps(change('film', name='other')), server_name='shop')
        with self.assertRaises(ValueError):
            live.debezium_tables('not json')


class PlanTests(unittest.TestCase):
    """What a subscription listens on: plan() reads the saved query's published version."""

    def plan(self, saved, connection):
        app = create_app()
        with mock.patch.object(live.store, 'resolve_saved_file', return_value='q'), \
                mock.patch.object(live.store, 'load_versions', return_value=[]), \
                mock.patch.object(live.store, 'select_version', return_value=(1, saved)), \
                mock.patch.object(live.store, 'get_connection', return_value=connection):
            return live.plan(app, mock.Mock(workspace='default'), 'q')

    def test_a_mongodb_query_listens_on_its_collection(self):
        saved = {'query_type': 'mongo', 'connection_name': 'docs', 'mongo_collection': 'Films'}
        self.assertEqual(self.plan(saved, {'db': 'mongo', 'database': 'rental'}), ('docs', {'rental.films'}))

    def test_a_mongodb_connection_without_a_database_listens_on_any_change(self):
        saved = {'query_type': 'mongo', 'connection_name': 'docs', 'mongo_collection': 'films'}
        self.assertEqual(self.plan(saved, {'db': 'mongo'}), ('docs', None))

    def test_sql_tableguard_cant_read_listens_on_any_change(self):  # a jdbc connection: SQL Server, Oracle, Db2
        saved = {'connection_name': 'erp', 'sql_query': 'SELECT * FROM dbo.film'}
        self.assertEqual(self.plan(saved, {'db': 'jdbc'}), ('erp', None))


class GroupKeyTests(unittest.TestCase):
    """Subscriptions sharing a group share its result: never across callers, workspaces or signed-in claims."""

    @staticmethod
    def caller(name='jwt:alice', claims=None, workspace='default'):
        return types.SimpleNamespace(workspace=workspace, name=name, via=None, claims=claims)

    def test_the_same_caller_query_and_parameters_share_one(self):
        self.assertEqual(live.group_key(self.caller(), 'orders', {'a': '1'}),
                         live.group_key(self.caller(), 'orders', {'a': '1'}))
        self.assertNotEqual(live.group_key(self.caller(), 'orders', {'a': '1'}),
                            live.group_key(self.caller(), 'orders', {'a': '2'}))
        self.assertNotEqual(live.group_key(self.caller(), 'orders', {}),
                            live.group_key(self.caller(workspace='acme'), 'orders', {}))

    def test_one_user_with_other_claims_gets_another_group(self):  # from_claim parameters come from the claims
        one = self.caller(claims={'sub': 'alice', 'tenant': 'acme', 'exp': 1})
        other = self.caller(claims={'sub': 'alice', 'tenant': 'globex', 'exp': 1})
        self.assertNotEqual(live.group_key(one, 'orders', {}), live.group_key(other, 'orders', {}))

    def test_a_refreshed_token_with_the_same_claims_shares_it(self):
        one = self.caller(claims={'sub': 'alice', 'tenant': 'acme', 'iat': 1, 'exp': 2, 'jti': 'a'})
        refreshed = self.caller(claims={'sub': 'alice', 'tenant': 'acme', 'iat': 5, 'exp': 9, 'jti': 'b'})
        self.assertEqual(live.group_key(one, 'orders', {}), live.group_key(refreshed, 'orders', {}))


class FlaskAppTests(unittest.TestCase):
    def test_two_first_subscriptions_at_once_build_one_app(self):
        import threading
        built, gate = [], threading.Barrier(2)

        def slow_create_app():
            built.append(1)
            time.sleep(0.2)
            return object()

        queries = live.LiveQueries()
        apps = []
        with mock.patch('queryapigate.app.create_app', side_effect=slow_create_app):
            threads = [threading.Thread(target=lambda: (gate.wait(), apps.append(queries.flask_app())))
                       for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(len(built), 1)
        self.assertIs(apps[0], apps[1])


class LiveTestCase(EventServerTestCase):
    """A store with `film` and `rental`; saved queries reading each; live queries on, re-runs not throttled."""

    def setUp(self):
        self.extra = tempfile.TemporaryDirectory()
        self.addCleanup(self.extra.cleanup)
        self.shop = os.path.join(self.extra.name, 'shop.db')
        conn = sqlite3.connect(self.shop)
        conn.executescript('CREATE TABLE film (id INTEGER PRIMARY KEY, title TEXT);'
                           "INSERT INTO film VALUES (1, 'Alpha'), (2, 'Beta');"
                           'CREATE TABLE rental (id INTEGER PRIMARY KEY, film_id INTEGER);')
        conn.close()
        sources = {'shop': {'connection': 'shop', 'secret': SECRET, 'server_name': 'shop'}}
        patcher = mock.patch.dict(os.environ, {'QUERYAPIGATE_LIVE_QUERIES': '1', 'QUERYAPIGATE_ALLOW_WRITES': '1',
                                               'QUERYAPIGATE_LIVE_MIN_INTERVAL': '0',
                                               'QUERYAPIGATE_CDC_SOURCES': json.dumps(sources)})
        patcher.start()
        self.addCleanup(patcher.stop)
        super().setUp()

    def start_server(self, **options):
        write_connections({'lite': {'db': 'sqlite', 'database': os.path.join(self.tmp.name, 'data.db'),
                                    'active': True},
                           'shop': {'db': 'sqlite', 'database': self.shop, 'active': True}})
        self.client = create_app().test_client()
        save_query(self.client, headers=self.admin, body={
            'filename': 'film', 'description': 'd', 'connection_name': 'shop',
            'sql_query': 'SELECT id, title FROM film WHERE id = :id',
            'query_parameters': {'id': {'type': 'int'}}})
        save_query(self.client, headers=self.admin, body={
            'filename': 'rename_film', 'description': 'd', 'connection_name': 'shop',
            'sql_query': 'UPDATE film SET title = :title WHERE id = :id',
            'query_parameters': {'id': {'type': 'int'}, 'title': {'type': 'str'}}})
        self.reader = self.client.post('/api/v1/api-keys', headers=self.admin,
                                       json={'name': 'reader', 'connections': [], 'queries': ['film']}
                                       ).get_json()['secret']
        super().start_server(**options)
        self.server.live._flask_app = self.client.application

    def subscribe(self, path, key='admin'):
        stream = Stream(self.server.port, path=path, headers={'X-API-Key': key})
        self.addCleanup(stream.close)
        return stream

    def write(self, sql):
        response = self.client.post('/execute_sql', headers=self.admin, json={'connection_name': 'shop',
                                                                              'sql': sql})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))


class RateLimitTests(LiveTestCase):
    """ADR 0033 §12: opening a stream and each group's re-run are calls against the caller's own rate limit; a re-run
    it refuses - or max_concurrent does - is postponed, not dropped, and no error event is sent."""

    def start_server(self, **options):
        super().start_server(**options)
        # a name of its own: with QUERYAPIGATE_REDIS_URL set, a bucket outlives the test that spent it
        self.limited = self.client.post('/api/v1/api-keys', headers=self.admin, json={
            'name': f'limited-{uuid.uuid4().hex[:8]}', 'connections': [], 'queries': ['film'],
            'rate_limit': '3/minute'}).get_json()['secret']

    def test_opening_a_stream_is_a_call(self):
        for _ in range(3):  # joining the group runs nothing new, and still counts - as a cache hit on /q/ does
            stream = self.subscribe('/live/film?id=1', key=self.limited)
            self.assertEqual(stream.status, 200)
            stream.events(1)
        refused = self.subscribe('/live/film?id=1', key=self.limited)
        self.assertEqual(refused.status, 429)
        self.assertEqual(refused.body()['code'], 'rate_limited')
        self.assertGreaterEqual(int(refused.headers['retry-after']), 1)
        self.assertEqual(self.subscribe('/live/film?id=1').status, 200)  # the admin key has no rate limit

    def test_each_re_run_is_a_call_and_one_over_the_limit_waits(self):
        stream = self.subscribe('/live/film?id=1', key=self.limited)  # 1 of 3
        stream.events(1)
        for title in ('Two', 'Three'):  # 2 and 3: run and pushed
            self.write(f"UPDATE film SET title = '{title}' WHERE id = 1")
            self.assertEqual(stream.events(1)[0][1]['rows'], [{'id': 1, 'title': title}])
        self.write("UPDATE film SET title = 'Four' WHERE id = 1")  # over the limit: held back, no error event
        self.assertTrue(stream.nothing_more(1.5))
        self.assertEqual(self.server.live.postponed, 1)
        group = next(iter(self.server.live.groups.values()))
        self.assertTrue(group.dirty)
        self.assertGreater(group.not_before, time.monotonic())
        self.assertIn('queryapigate_live_postponed_total 1', '\n'.join(self.server.live.metrics_lines()))

    def test_a_postponed_re_run_runs_once_it_is_allowed(self):
        stream = self.subscribe('/live/film?id=1', key=self.limited)
        stream.events(1)
        answers = iter([1, None])  # refused with "wait 1 s", then allowed
        with mock.patch.object(live.LiveQueries, '_take', lambda _self, _permission: next(answers)):
            started = time.monotonic()
            self.write("UPDATE film SET title = 'Later' WHERE id = 1")
            (_, pushed), = stream.events(1, timeout=6)
        self.assertEqual((pushed['rows'], pushed['reason']), ([{'id': 1, 'title': 'Later'}], 'change'))
        self.assertGreaterEqual(time.monotonic() - started, 0.9)
        self.assertEqual(self.server.live.postponed, 1)

    def test_max_concurrent_postpones_instead_of_an_error_event(self):
        stream = self.subscribe('/live/film?id=1')
        stream.events(1)
        real, calls = live.run_once, []

        def busy_once(*args):
            calls.append(1)
            if len(calls) == 1:
                raise ApiError('This caller already has 1 queries running', 429, code='too_many_concurrent')
            return real(*args)

        with mock.patch.object(live, 'run_once', busy_once):
            self.write("UPDATE film SET title = 'Busy' WHERE id = 1")
            (_, pushed), = stream.events(1, timeout=6)
        self.assertEqual(pushed['rows'], [{'id': 1, 'title': 'Busy'}])  # a result, never an error event
        self.assertEqual((len(calls), self.server.live.postponed), (2, 1))


class SignedInTests(LiveTestCase):
    """A signed-in user (jwtauth.py) subscribing to a query bound to their token's claims (`from_claim`)."""

    def setUp(self):
        patcher = mock.patch.dict(os.environ, {'QUERYAPIGATE_JWT_SECRET': JWT_SECRET,
                                               'QUERYAPIGATE_JWT_ROLE': 'mobile'})
        patcher.start()
        self.addCleanup(patcher.stop)
        super().setUp()

    def start_server(self, **options):
        conn = sqlite3.connect(self.shop)
        conn.executescript('CREATE TABLE orders (user_id TEXT, tenant TEXT, item TEXT);'
                           "INSERT INTO orders VALUES ('alice', 'acme', 'anvil'), ('alice', 'globex', 'globe'),"
                           "('bob', 'acme', 'bucket');")
        conn.close()
        super().start_server(**options)
        save_query(self.client, headers=self.admin, body={
            'filename': 'my_orders', 'description': 'd', 'connection_name': 'shop',
            'sql_query': 'SELECT item FROM orders WHERE user_id = :user_id AND tenant = :tenant ORDER BY item',
            'query_parameters': {'user_id': {'type': 'str', 'from_claim': 'sub'},
                                 'tenant': {'type': 'str', 'from_claim': 'tenant'}}})
        self.client.post('/api/v1/roles', headers=self.admin,
                         json={'name': 'mobile', 'connections': [], 'queries': ['my_orders']})

    def subscribe_as(self, **claims):
        stream = Stream(self.server.port, path='/live/my_orders', headers=bearer(jwt_token(claims)))
        self.addCleanup(stream.close)
        return stream

    def test_each_user_sees_their_own_rows_and_a_write_updates_only_theirs(self):
        alice, bob = self.subscribe_as(tenant='acme'), self.subscribe_as(sub='bob', tenant='acme')
        self.assertEqual(alice.events(1)[0][1]['rows'], [{'item': 'anvil'}])
        self.assertEqual(bob.events(1)[0][1]['rows'], [{'item': 'bucket'}])
        self.write("INSERT INTO orders VALUES ('bob', 'acme', 'broom')")
        self.assertEqual(bob.events(1)[0][1]['rows'], [{'item': 'broom'}, {'item': 'bucket'}])
        self.assertTrue(alice.nothing_more(1.0))

    def test_one_user_with_two_tenants_never_sees_the_other_tenants_rows(self):  # tokens differing in a claim
        acme = self.subscribe_as(tenant='acme')
        self.assertEqual(acme.events(1)[0][1]['rows'], [{'item': 'anvil'}])
        globex = self.subscribe_as(tenant='globex')
        self.assertEqual(globex.events(1)[0][1]['rows'], [{'item': 'globe'}])


class SubscribeTests(LiveTestCase):
    def test_the_first_event_is_the_current_result(self):
        stream = self.subscribe('/live/film?id=1')
        self.assertEqual(stream.status, 200)
        self.assertEqual(stream.headers['content-type'], 'text/event-stream')
        (_, first), = stream.events(1)
        self.assertEqual(first['rows'], [{'id': 1, 'title': 'Alpha'}])
        self.assertEqual((first['query'], first['params'], first['reason']), ('film', {'id': '1'}, 'subscribe'))

    def test_a_key_granted_only_the_query_may_subscribe(self):  # what ADR 0033's first draft couldn't serve
        stream = self.subscribe('/live/film?id=2', key=self.reader)
        self.assertEqual(stream.events(1)[0][1]['rows'], [{'id': 2, 'title': 'Beta'}])

    def test_a_key_that_may_not_run_it_is_refused_as_GET_q_would_be(self):
        stream = self.subscribe('/live/film?id=1', key=self.alice)
        self.assertEqual(stream.status, 403)
        self.assertEqual(stream.body()['code'], 'connection_forbidden')
        self.assertEqual(self.subscribe('/live/film?id=1', key='wrong').status, 401)
        self.assertEqual(self.subscribe('/live/nope').status, 404)

    def test_a_query_that_writes_cant_be_live(self):
        stream = self.subscribe('/live/rename_film?id=1&title=x')
        self.assertEqual(stream.status, 403)
        self.assertEqual(stream.body()['code'], 'read_only')
        conn = sqlite3.connect(self.shop)
        self.assertEqual(conn.execute('SELECT title FROM film WHERE id = 1').fetchone()[0], 'Alpha')
        conn.close()

    def test_a_missing_parameter_is_the_runs_own_400(self):
        self.assertEqual(self.subscribe('/live/film').status, 400)

    def test_every_run_is_in_history_as_live(self):
        self.subscribe('/live/film?id=1').events(1)
        runs = self.client.get('/api/v1/history?query=film', headers=self.admin).get_json()['items']
        self.assertEqual(runs[0]['transport'], 'live')


class WriteSignalTests(LiveTestCase):
    def test_a_write_to_its_table_pushes_the_new_result(self):
        stream = self.subscribe('/live/film?id=1')
        stream.events(1)
        self.write("UPDATE film SET title = 'Alpha II' WHERE id = 1")
        (_, pushed), = stream.events(1)
        self.assertEqual((pushed['rows'], pushed['reason']), ([{'id': 1, 'title': 'Alpha II'}], 'change'))

    def test_a_saved_write_query_signals_too(self):
        stream = self.subscribe('/live/film?id=2', key=self.reader)
        stream.events(1)
        self.assertEqual(self.client.get('/q/rename_film?id=2&title=Gamma', headers=self.admin).status_code, 200)
        self.assertEqual(stream.events(1)[0][1]['rows'], [{'id': 2, 'title': 'Gamma'}])

    def test_a_write_elsewhere_or_one_that_changes_nothing_it_shows_pushes_nothing(self):
        stream = self.subscribe('/live/film?id=1')
        stream.events(1)
        self.write('INSERT INTO rental (film_id) VALUES (1)')       # another table: not re-run at all
        self.write("UPDATE film SET title = 'Beta II' WHERE id = 2")  # its table, another row: re-run, same result
        # the film write is re-run, found unchanged - after the signal is delivered, which with a run store waits
        # for it to settle (runstore.SETTLE_SECONDS): wait for the count, rather than a fixed second
        deadline = time.monotonic() + 10
        while self.server.live.unchanged < 1 and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertGreaterEqual(self.server.live.unchanged, 1)
        self.assertTrue(stream.nothing_more(1.0))

    def test_subscribers_alike_share_one_run(self):
        first, second = self.subscribe('/live/film?id=1'), self.subscribe('/live/film?id=1')
        first.events(1), second.events(1)
        self.assertEqual(len(self.server.live.groups), 1)
        self.write("UPDATE film SET title = 'Shared' WHERE id = 1")
        self.assertEqual(first.events(1)[0][1]['rows'][0]['title'], 'Shared')
        self.assertEqual(second.events(1)[0][1]['rows'][0]['title'], 'Shared')
        self.assertEqual(self.server.live.reruns, 1)

    def test_a_burst_of_writes_is_re_run_at_most_once_per_interval(self):
        stream = self.subscribe('/live/film?id=1')
        stream.events(1)
        with mock.patch('queryapigate.config.live_min_interval', return_value=1.0):
            for n in range(10):
                self.write(f"UPDATE film SET title = 'v{n}' WHERE id = 1")
            time.sleep(1.5)
        self.assertLessEqual(self.server.live.reruns, 3)
        latest = stream.events(1)[-1][1]
        while not stream.nothing_more(0.3):
            pass
        self.assertTrue(latest['rows'])

    def test_the_admin_stream_sees_the_signal_without_row_data(self):
        events_stream = self.open('admin')
        self.write("UPDATE film SET title = 'Seen' WHERE id = 1")
        (_, signal), = events_stream.events(1)
        self.assertEqual(signal['type'], 'table.changed')
        self.assertEqual((signal['connection_name'], signal['tables'], signal['source']),
                         ('shop', ['main.film'], 'write'))
        self.assertNotIn('Seen', json.dumps(signal))
        later = []  # the admin's own query.run for the write follows; the signal mustn't come again
        while True:
            try:
                later += events_stream.events(1, timeout=0.6)
            except (TimeoutError, OSError):
                break
        # once: a signal names no key, and was sent twice to a stream that sees everything (fixed 2026-11-11)
        self.assertEqual([e for _, e in later if e['type'] == 'table.changed'], [])

    def test_other_keys_event_streams_dont_see_signals(self):
        mine = self.open(self.alice)
        self.write("UPDATE film SET title = 'Hidden' WHERE id = 1")
        self.assertTrue(mine.nothing_more(0.6))


class IngestTests(LiveTestCase):
    def post(self, payload, source='shop', **header_overrides):
        body = json.dumps(payload).encode()
        headers = {**signed(body), **header_overrides}
        return self.client.post(f'/ingest/cdc/{source}', data=body, headers=headers)

    def test_a_signed_change_is_stored_and_pushes_the_new_result(self):
        stream = self.subscribe('/live/film?id=1')
        stream.events(1)
        conn = sqlite3.connect(self.shop)  # changed outside QueryAPIGate, as Debezium would see it
        conn.execute("UPDATE film SET title = 'From Debezium' WHERE id = 1")
        conn.commit()
        conn.close()
        self.assertEqual(self.post([change('film')]).status_code, 204)
        self.assertEqual(stream.events(1)[0][1]['rows'][0]['title'], 'From Debezium')

    def test_refused_without_a_valid_signature_or_source_as_not_found(self):
        self.assertEqual(self.post(change('film'), **{'webhook-signature': 'v1,bm9wZQ=='}).status_code, 404)
        self.assertEqual(self.post(change('film'), source='nope').status_code, 404)
        self.assertEqual(self.client.post('/ingest/cdc/shop', json=change('film')).status_code, 404)
        # an API key is no way in, even the admin's
        self.assertEqual(self.client.post('/ingest/cdc/shop', json=change('film'), headers=self.admin).status_code,
                         404)

    def test_another_server_or_a_body_that_isnt_a_change_is_422(self):
        self.assertEqual(self.post(change('film', name='other')).status_code, 422)
        body = b'nope'
        self.assertEqual(self.client.post('/ingest/cdc/shop', data=body, headers=signed(body)).status_code, 422)

    def test_a_snapshot_is_accepted_and_signals_nothing(self):
        admin_events = self.open('admin')
        self.assertEqual(self.post([change('film', 'r')]).status_code, 204)
        self.assertTrue(admin_events.nothing_more(0.6))


class OffTests(unittest.TestCase):
    def test_off_by_default(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': home,
                                                                              'QUERYAPIGATE_LIVE_QUERIES': ''}):
            client = create_app().test_client()
            body = b'{}'
            self.assertEqual(client.post('/ingest/cdc/shop', data=body, headers=signed(body)).status_code, 404)


if __name__ == '__main__':
    unittest.main()

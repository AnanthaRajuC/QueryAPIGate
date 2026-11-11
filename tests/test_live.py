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
import unittest
from unittest import mock

from queryapigate import create_app, live
from tests.helpers import save_query, write_connections
from tests.test_events_server import EventServerTestCase, Stream

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

    def test_snapshots_tombstones_and_heartbeats_touch_nothing(self):
        self.assertEqual(live.debezium_tables(json.dumps([change('film', 'r'), None, {'ts_ms': 1}])), set())
        self.assertEqual(live.debezium_tables(''), set())

    def test_refuses_another_server_and_what_isnt_json(self):
        with self.assertRaises(ValueError):
            live.debezium_tables(json.dumps(change('film', name='other')), server_name='shop')
        with self.assertRaises(ValueError):
            live.debezium_tables('not json')


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

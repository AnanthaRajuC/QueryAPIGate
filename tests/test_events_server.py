"""Tests for `queryapigate events` (events.py): the asyncio Server-Sent Events server. Each test runs a real
EventServer on a free port, on its own event loop in a background thread, and talks plain HTTP to it - against
whichever metadata backend the suite runs on (see tests/__init__.py)."""
import asyncio
import json
import os
import socket
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest import mock

from queryapigate import config, create_app, db, events
from tests import TEST_DATABASE_URL
from tests.helpers import save_query, write_connections


class Stream:
    """One raw HTTP connection to the events server, read line by line."""

    def __init__(self, port, path='/events', headers=None, method='GET'):
        self.sock = socket.create_connection(('127.0.0.1', port), timeout=5)
        lines = [f'{method} {path} HTTP/1.1', 'Host: localhost'] + [f'{k}: {v}' for k, v in (headers or {}).items()]
        self.sock.sendall(('\r\n'.join(lines) + '\r\n\r\n').encode())
        self.file = self.sock.makefile('rb')
        self.status = int(self.file.readline().split()[1])
        self.headers = {}
        while True:
            line = self.file.readline().decode().rstrip('\r\n')
            if not line:
                break
            name, _, value = line.partition(':')
            self.headers[name.strip().lower()] = value.strip()

    def body(self):
        return json.loads(self.file.read(int(self.headers['content-length'])))

    def events(self, count, timeout=5.0):
        """The next ``count`` events as (id, payload), skipping comments and the retry line."""
        found, current_id, deadline = [], None, time.monotonic() + timeout
        while len(found) < count:
            self.sock.settimeout(max(0.05, deadline - time.monotonic()))
            line = self.file.readline().decode()
            if not line:
                raise ConnectionError('stream closed')
            if line.startswith('id: '):
                current_id = int(line[4:])
            elif line.startswith('data: '):
                found.append((current_id, json.loads(line[6:])))
        return found

    def nothing_more(self, wait=0.4):
        """True when no further event arrives within ``wait`` seconds."""
        try:
            self.events(1, timeout=wait)
        except (socket.timeout, TimeoutError):
            return True
        return False

    def closed(self, timeout=5.0):
        self.sock.settimeout(timeout)
        try:
            while self.file.readline():
                pass
        except (socket.timeout, TimeoutError):
            return False
        except ConnectionError:
            pass
        return True

    def close(self):
        self.file.close()
        self.sock.close()


class EventServerTestCase(unittest.TestCase):
    server_options = {}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        data = os.path.join(self.tmp.name, 'data.db')
        conn = sqlite3.connect(data)
        conn.execute('CREATE TABLE t (id INTEGER)')
        conn.execute('INSERT INTO t VALUES (1)')
        conn.commit()
        conn.close()
        env = {'QUERYAPIGATE_HOME': self.tmp.name, 'QUERYAPIGATE_API_KEY': 'admin',
               'QUERYAPIGATE_HISTORY_FLUSH_INTERVAL': '0',  # each run is in history (and so an event) at once
               'QUERYAPIGATE_RATE_LIMIT': '', 'QUERYAPIGATE_CORS_ORIGINS': ''}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        write_connections({'lite': {'db': 'sqlite', 'database': data, 'active': True}})
        self.client = create_app().test_client()
        self.admin = {'X-API-Key': 'admin'}
        save_query(self.client, headers=self.admin, body={
            'author': 'a', 'description': 'd', 'filename': 'q', 'connection_name': 'lite', 'sql_query': 'SELECT 1'})
        self.alice = self.key('alice')
        self.bob = self.key('bob')
        self.start_server(**self.server_options)

    def key(self, name):
        return self.client.post('/api/v1/api-keys', headers=self.admin,
                                json={'name': name, 'connections': [], 'queries': ['q']}).get_json()['secret']

    def start_server(self, **options):
        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever, daemon=True)
        thread.start()
        server = events.EventServer('127.0.0.1', 0, poll_interval=options.pop('poll_interval', 0.05), **options)
        asyncio.run_coroutine_threadsafe(server.start(), loop).result(10)
        self.loop, self.server = loop, server

        def stop():
            asyncio.run_coroutine_threadsafe(server.stop(), loop).result(10)
            # Wait for the loop's worker threads too (asyncio.to_thread - checking a key opens a store connection):
            # one still connecting when the test's patched environment is restored reads os.environ while it is
            # being changed, which libpq does in C - and that crashes the interpreter, not just the test.
            asyncio.run_coroutine_threadsafe(loop.shutdown_default_executor(), loop).result(10)
            loop.call_soon_threadsafe(loop.stop)
            thread.join(5)
            loop.close()
        self.addCleanup(stop)

    def open(self, key=None, **headers):
        if key is not None:
            headers['X-API-Key'] = key
        stream = Stream(self.server.port, headers=headers)
        self.addCleanup(stream.close)
        return stream

    def run_query(self, key):
        self.assertEqual(self.client.get('/q/q', headers={'X-API-Key': key}).status_code, 200)


class RequestTests(EventServerTestCase):
    def test_health(self):
        stream = Stream(self.server.port, path='/health')
        self.assertEqual((stream.status, stream.body()['status']), (200, 'ok'))

    def test_only_get_events_is_served(self):
        self.assertEqual(Stream(self.server.port, path='/q/q').status, 404)
        self.assertEqual(Stream(self.server.port, method='POST', headers=self.admin).status, 405)

    def test_a_key_is_required_and_must_be_real(self):
        self.assertEqual(self.open().status, 401)
        self.assertEqual(self.open('sk_wrong').status, 401)
        self.assertEqual(self.open('admin').status, 200)

    def test_a_garbage_request_is_dropped_without_harming_the_server(self):
        sock = socket.create_connection(('127.0.0.1', self.server.port), timeout=5)
        sock.sendall(b'\x00\x01 not http at all\r\n\r\n')
        self.assertEqual(sock.recv(100), b'')  # closed, nothing answered
        sock.close()
        self.assertEqual(self.open('admin').status, 200)

    def test_the_stream_is_text_event_stream(self):
        stream = self.open('admin')
        self.assertEqual(stream.headers['content-type'], 'text/event-stream')
        self.assertEqual(stream.headers['cache-control'], 'no-cache')


class DeliveryTests(EventServerTestCase):
    def test_a_recorded_run_arrives_with_its_history_id(self):
        stream = self.open('admin')
        self.run_query(self.alice)
        (event_id, payload), = stream.events(1)
        stored = db.connection().execute('SELECT MAX(rowid) FROM execution_history').fetchone()[0]
        self.assertEqual(event_id, stored)
        self.assertEqual((payload['type'], payload['filename'], payload['version'], payload['entry']['key_name']),
                         ('execution', 'q', 1, 'alice'))

    def test_a_scoped_key_sees_only_its_own_runs_and_the_admin_sees_all(self):
        admin, alice = self.open('admin'), self.open(self.alice)
        self.run_query(self.bob)
        self.run_query(self.alice)
        self.assertEqual([p['entry']['key_name'] for _, p in admin.events(2)], ['bob', 'alice'])
        self.assertEqual([p['entry']['key_name'] for _, p in alice.events(1)], ['alice'])
        self.assertTrue(alice.nothing_more())

    def test_history_from_before_the_server_started_is_not_sent_again(self):
        self.run_query(self.alice)  # recorded before the second server below starts
        self.start_server(poll_interval=0.05)
        stream = self.open('admin')
        self.assertTrue(stream.nothing_more())

    def test_a_run_committed_out_of_id_order_is_still_delivered(self):
        """On Postgres a smaller id can commit after a larger one was delivered (see events.py)."""
        stream = self.open('admin')
        self.run_query(self.alice)
        (newest, _), = stream.events(1)
        with db.transaction() as conn:  # a straggler with an id below the newest one already delivered
            conn.execute("INSERT INTO execution_history (rowid, query_name, version, executed_at, entry_json, status, "
                         "key_name) VALUES (?, 'q', 1, '2026-01-01 00:00:00', ?, 'success', 'alice')",
                         (newest - 1 if newest > 1 else newest + 5000, json.dumps({'key_name': 'alice', 'late': True})))
        (_, payload), = stream.events(1)
        self.assertTrue(payload['entry']['late'])
        self.assertTrue(stream.nothing_more())

    @unittest.skipUnless(TEST_DATABASE_URL, 'set QUERYAPIGATE_TEST_DATABASE_URL to run')
    def test_postgres_notify_delivers_without_waiting_for_the_poll(self):
        self.start_server(poll_interval=30)  # only a NOTIFY can wake this one in time
        stream = self.open('admin')
        time.sleep(0.5)  # let its LISTEN connection come up
        started = time.monotonic()
        self.run_query(self.alice)
        stream.events(1, timeout=5)
        self.assertLess(time.monotonic() - started, 5)


class ResumeTests(EventServerTestCase):
    def test_last_event_id_replays_what_was_missed_then_continues_live_without_repeats(self):
        first = self.open('admin')
        for _ in range(3):
            self.run_query(self.alice)
        ids = [event_id for event_id, _ in first.events(3)]
        resumed = self.open('admin', **{'Last-Event-ID': str(ids[0])})
        self.assertEqual([event_id for event_id, _ in resumed.events(2)], ids[1:])
        self.run_query(self.alice)
        (live_id, _), = resumed.events(1)
        self.assertGreater(live_id, ids[-1])
        self.assertTrue(resumed.nothing_more())

    def test_a_replay_only_includes_runs_the_key_may_see(self):
        first = self.open('admin')
        self.run_query(self.bob)
        self.run_query(self.alice)
        (bob_run, _), _ = first.events(2)
        resumed = self.open(self.alice, **{'Last-Event-ID': str(bob_run - 1)})
        self.assertEqual([p['entry']['key_name'] for _, p in resumed.events(1)], ['alice'])
        self.assertTrue(resumed.nothing_more())

    def test_the_query_parameter_form_works_too(self):
        first = self.open('admin')
        self.run_query(self.alice)
        self.run_query(self.alice)
        ids = [event_id for event_id, _ in first.events(2)]
        stream = Stream(self.server.port, path=f'/events?last_event_id={ids[0]}', headers=self.admin)
        self.addCleanup(stream.close)
        self.assertEqual([event_id for event_id, _ in stream.events(1)], ids[1:])


class LimitTests(EventServerTestCase):
    def test_a_revoked_key_s_stream_is_closed(self):
        with mock.patch.object(events, '_RECHECK_SECONDS', 0.2), mock.patch.object(events, '_HEARTBEAT_SECONDS', 0.1):
            stream = self.open(self.alice)
            self.assertEqual(self.client.delete('/api/v1/api-keys/alice', headers=self.admin).status_code, 204)
            self.assertTrue(stream.closed(timeout=5))

    def test_a_client_that_hangs_up_frees_its_slot_at_once(self):
        with mock.patch.object(events, '_HEARTBEAT_SECONDS', 60):  # no heartbeat write to notice it by
            stream = self.open('admin')
            deadline = time.monotonic() + 2
            while not self.server.clients and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(len(self.server.clients), 1)
            stream.close()
            while self.server.clients and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(len(self.server.clients), 0)

    def test_connections_beyond_the_limit_get_a_503(self):
        self.start_server(poll_interval=0.05, max_connections=2)
        self.open('admin')
        self.open('admin')
        refused = self.open('admin')
        self.assertEqual((refused.status, refused.headers['retry-after']), (503, '5'))

    def test_the_server_wide_rate_limit_applies_to_connection_attempts(self):
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_RATE_LIMIT': '2/minute'}):
            statuses = [self.open('admin').status for _ in range(3)]
        self.assertEqual(statuses, [200, 200, 429])

    def test_a_client_too_far_behind_is_cut_off_rather_than_buffered(self):
        async def scenario():
            writer = mock.Mock()
            client = events._Client(None, writer)
            server = events.EventServer('127.0.0.1', 0)
            server.by_key[None] = {client}
            with mock.patch.object(events, '_QUEUE_SIZE', 3):
                client.queue = asyncio.Queue(maxsize=3)
                for rowid in range(1, 6):
                    server._deliver(rowid, 'k', b'data')
            return client
        client = asyncio.run(scenario())
        self.assertTrue(client.too_slow)
        self.assertEqual(client.queue.qsize(), 1)
        self.assertIsNone(client.queue.get_nowait())  # the pump's signal to close the stream


class CorsTests(EventServerTestCase):
    def test_an_allowed_origin_gets_cors_headers_and_a_preflight(self):
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_CORS_ORIGINS': 'https://app.example.com'}):
            preflight = Stream(self.server.port, method='OPTIONS', headers={
                'Origin': 'https://app.example.com', 'Access-Control-Request-Method': 'GET'})
            self.assertEqual(preflight.status, 204)
            self.assertIn('X-API-Key', preflight.headers['access-control-allow-headers'])
            stream = self.open('admin', Origin='https://app.example.com')
            self.assertEqual(stream.headers['access-control-allow-origin'], 'https://app.example.com')
            other = self.open('admin', Origin='https://evil.example.com')
            self.assertNotIn('access-control-allow-origin', other.headers)


class SettingsTests(unittest.TestCase):
    def test_malformed_values_stop_startup(self):
        for name, value in (('QUERYAPIGATE_EVENTS_PORT', '0'), ('QUERYAPIGATE_EVENTS_MAX_CONNECTIONS', 'lots'),
                            ('QUERYAPIGATE_EVENTS_MAX_STREAMS', '-1'), ('QUERYAPIGATE_EVENTS_POLL_INTERVAL', '0')):
            with mock.patch.dict(os.environ, {name: value}), self.assertRaises(ValueError, msg=f'{name}={value}'):
                config.check_settings()

    def test_defaults(self):
        env = {name: '' for name in ('QUERYAPIGATE_EVENTS_PORT', 'QUERYAPIGATE_EVENTS_MAX_CONNECTIONS',
                                     'QUERYAPIGATE_EVENTS_MAX_STREAMS', 'QUERYAPIGATE_EVENTS_POLL_INTERVAL')}
        with mock.patch.dict(os.environ, env):
            self.assertEqual((config.events_port(), config.events_max_connections(), config.events_max_streams(),
                              config.events_poll_interval()), (5002, 10_000, 4, 1.0))


if __name__ == '__main__':
    unittest.main()

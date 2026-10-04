"""Tests for GET /events - Server-Sent Events for the admin UI's Home tab (BACKLOG #43).

Flask's test client eagerly pulls at least the first chunk out of a streaming response's generator as
part of client.get() itself (confirmed by direct experiment - not documented behavior worth relying on
blindly) - so every test here shrinks _SSE_HEARTBEAT_SECONDS first, or a test with nothing queued yet would
block for the real 15s default during the .get() call, not just when a test explicitly reads a heartbeat.
"""
import json
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from queryapigate import app as app_module
from queryapigate import create_app
from tests.helpers import save_query, write_connections


class EventsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        tmp = self.tmp.name
        self.db_path = os.path.join(tmp, 'test.db')
        conn = sqlite3.connect(self.db_path)
        conn.execute('CREATE TABLE t (id INTEGER)')
        conn.execute('INSERT INTO t VALUES (1)')
        conn.commit()
        conn.close()
        patcher = mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': tmp, 'QUERYAPIGATE_API_KEY': 'admin-key'})
        patcher.start()
        self.addCleanup(patcher.stop)
        heartbeat_patcher = mock.patch.object(app_module, '_SSE_HEARTBEAT_SECONDS', 0.05)
        heartbeat_patcher.start()
        self.addCleanup(heartbeat_patcher.stop)
        write_connections({'a': {'db': 'sqlite', 'database': self.db_path, 'active': True}})
        self.app = create_app()
        self.client = self.app.test_client()
        self.admin_headers = {'X-API-Key': 'admin-key'}
        save_query(self.client, {
            'author': 'a', 'description': 'd', 'sql_query': 'SELECT * FROM t', 'filename': 'q1',
            'connection_name': 'a'}, headers=self.admin_headers)

    def create_scoped_key(self, **fields):
        res = self.client.post('/api/v1/api-keys', json={'name': 'scoped', 'connections': [], **fields},
                               headers=self.admin_headers)
        return res.get_json()['secret']

    def broadcaster(self):
        return self.app.extensions['queryapigate_broadcaster']

    def next_frame(self, gen):
        frame = next(gen)
        return frame.decode() if isinstance(frame, bytes) else frame

    def next_data_frame(self, gen, max_tries=20):
        """The next `data: ...` frame, skipping any keepalive comment lines in between - how many
        heartbeats land before a real event is a real-time race (the shrunk heartbeat interval vs. however
        long the triggering request actually takes), not something a test should assume a fixed count of."""
        for _ in range(max_tries):
            frame = self.next_frame(gen)
            if frame.startswith('data: '):
                return frame
        self.fail('no data frame arrived within the attempt budget')

    def test_anonymous_is_unauthorized(self):
        res = self.client.get('/events')
        self.assertEqual(res.status_code, 401)

    def test_a_scoped_non_admin_key_may_connect(self):
        # Was forbidden before per-subscriber filtering existed; a scoped key now gets its own personal
        # feed, same as any other endpoint a real key can reach.
        key = self.create_scoped_key()
        res = self.client.get('/events', headers={'X-API-Key': key})
        self.assertEqual(res.status_code, 200)
        res.close()

    def test_connecting_registers_a_subscriber_and_disconnecting_removes_it(self):
        self.assertEqual(self.broadcaster().subscriber_count(), 0)
        res = self.client.get('/events', headers=self.admin_headers)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.mimetype, 'text/event-stream')
        self.assertEqual(self.broadcaster().subscriber_count(), 1)
        res.close()
        self.assertEqual(self.broadcaster().subscriber_count(), 0)

    def test_a_published_execution_reaches_a_connected_subscriber(self):
        res = self.client.get('/events', headers=self.admin_headers)
        gen = iter(res.response)
        self.client.get('/q/q1', headers=self.admin_headers)
        frame = self.next_data_frame(gen)
        event = json.loads(frame[len('data: '):].strip())
        self.assertEqual(event['type'], 'execution')
        self.assertEqual(event['filename'], 'q1')
        self.assertEqual(event['entry']['status'], 'success')
        res.close()

    def test_a_heartbeat_is_sent_when_no_event_arrives_in_time(self):
        res = self.client.get('/events', headers=self.admin_headers)
        gen = iter(res.response)
        frame = self.next_frame(gen)
        self.assertEqual(frame, ': keepalive\n\n')
        res.close()

    def test_a_scoped_key_receives_only_its_own_executions(self):
        key_alice = self.create_scoped_key(name='alice', queries=['q1'])
        key_bob = self.create_scoped_key(name='bob', queries=['q1'])
        res = self.client.get('/events', headers={'X-API-Key': key_alice})
        gen = iter(res.response)
        self.client.get('/q/q1', headers={'X-API-Key': key_bob})   # someone else's run - must not arrive
        self.client.get('/q/q1', headers={'X-API-Key': key_alice})  # alice's own run - the one she sees
        frame = self.next_data_frame(gen)
        event = json.loads(frame[len('data: '):].strip())
        self.assertEqual(event['entry']['key_name'], 'alice')
        res.close()

    def test_the_admin_key_still_sees_a_scoped_key_s_execution(self):
        key_alice = self.create_scoped_key(name='alice', queries=['q1'])
        res = self.client.get('/events', headers=self.admin_headers)
        gen = iter(res.response)
        self.client.get('/q/q1', headers={'X-API-Key': key_alice})
        frame = self.next_data_frame(gen)
        event = json.loads(frame[len('data: '):].strip())
        self.assertEqual(event['entry']['key_name'], 'alice')
        res.close()


    def test_streams_beyond_the_cap_get_a_503_instead_of_a_request_thread(self):
        """Each open stream holds one of the WSGI server's request threads - unbounded, a few clients would
        leave none for anything else (8 open streams froze the Docker image's single worker entirely)."""
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_EVENTS_MAX_STREAMS': '2'}):
            first = self.client.get('/events', headers=self.admin_headers)
            second = self.client.get('/events', headers=self.admin_headers)
            third = self.client.get('/events', headers=self.admin_headers)
            self.assertEqual((first.status_code, second.status_code, third.status_code), (200, 200, 503))
            self.assertEqual(third.headers['Retry-After'], '5')
            self.assertIn('queryapigate events', third.get_json()['error'])
            first.close()  # a closed stream gives its slot back
            fourth = self.client.get('/events', headers=self.admin_headers)
            self.assertEqual(fourth.status_code, 200)
            second.close()
            fourth.close()
        self.assertEqual(app_module._open_streams, 0)

    def test_a_cap_of_zero_turns_streaming_off_on_this_server(self):
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_EVENTS_MAX_STREAMS': '0'}):
            self.assertEqual(self.client.get('/events', headers=self.admin_headers).status_code, 503)
        self.assertEqual(app_module._open_streams, 0)


if __name__ == '__main__':
    unittest.main()

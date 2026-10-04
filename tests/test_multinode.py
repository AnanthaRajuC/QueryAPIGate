"""Several real servers sharing one store behave as one service (BACKLOG #57).

Three `queryapigate serve` processes - each with its own QUERYAPIGATE_HOME, as separate machines would have - share a
PostgreSQL store and a Redis, with one `queryapigate events` process beside them. Every check makes a change through
one node and looks for it on another: keys, saved-query versions, connections, rate limits, the response cache,
history, the audit log and live events. Everything here goes over HTTP to real processes; nothing is shared in
memory, so a value held in one process's memory would fail these tests.

Runs when both QUERYAPIGATE_TEST_DATABASE_URL and QUERYAPIGATE_TEST_REDIS_URL are set (CI's PostgreSQL job)."""
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid

DATABASE_URL = os.environ.get('QUERYAPIGATE_TEST_DATABASE_URL')
REDIS_URL = os.environ.get('QUERYAPIGATE_TEST_REDIS_URL')
ADMIN = 'multinode-admin-key'
NODES = 3


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def call(base, method, path, body=None, key=ADMIN):
    """(status, parsed body or text, headers)."""
    data = None if body is None else json.dumps(body).encode()
    headers = {'Content-Type': 'application/json'}
    if key:
        headers['X-API-Key'] = key
    request = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as res:
            status, raw, out_headers = res.status, res.read(), res.headers
    except urllib.error.HTTPError as error:
        status, raw, out_headers = error.code, error.read(), error.headers
    try:
        return status, json.loads(raw) if raw else None, out_headers
    except ValueError:
        return status, raw.decode(), out_headers


def eventually(check, timeout=10.0):
    """Retry `check` until it returns a true value - for what is written in the background (history batches)."""
    deadline = time.monotonic() + timeout
    while True:
        result = check()
        if result or time.monotonic() > deadline:
            return result
        time.sleep(0.1)


@unittest.skipUnless(DATABASE_URL and REDIS_URL,
                     'set QUERYAPIGATE_TEST_DATABASE_URL and QUERYAPIGATE_TEST_REDIS_URL to run')
class MultiNodeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg2
        cls.schema = 'qag_multinode_' + uuid.uuid4().hex[:12]
        conn = psycopg2.connect(DATABASE_URL)
        conn.autocommit = True
        conn.cursor().execute(f'CREATE SCHEMA {cls.schema}')
        conn.close()
        cls.tmp = tempfile.TemporaryDirectory()
        cls.data = os.path.join(cls.tmp.name, 'shop.db')  # the database the API publishes - one, like a real one
        cls.other = os.path.join(cls.tmp.name, 'other.db')
        for path, name in ((cls.data, 'shop'), (cls.other, 'other')):
            db = sqlite3.connect(path)
            db.execute('CREATE TABLE things (id INTEGER, label TEXT)')
            db.execute('INSERT INTO things VALUES (1, ?)', (name,))
            db.commit()
            db.close()
        separator = '&' if '?' in DATABASE_URL else '?'
        env = {**os.environ, 'QUERYAPIGATE_API_KEY': ADMIN, 'QUERYAPIGATE_REDIS_URL': REDIS_URL,
               'QUERYAPIGATE_DATABASE_URL': f'{DATABASE_URL}{separator}options=-csearch_path%3D{cls.schema}',
               'QUERYAPIGATE_HISTORY_FLUSH_INTERVAL': '0.1', 'PYTHONUNBUFFERED': '1'}
        env.pop('QUERYAPIGATE_TEST_DATABASE_URL', None)
        cls.processes, cls.nodes = [], []
        for i in range(NODES):
            home = os.path.join(cls.tmp.name, f'node{i}')
            os.makedirs(home)
            port = free_port()
            cls.processes.append(cls.start(['serve', '--port', str(port)], {**env, 'QUERYAPIGATE_HOME': home}))
            cls.nodes.append(f'http://127.0.0.1:{port}')
        events_home = os.path.join(cls.tmp.name, 'events')
        os.makedirs(events_home)
        cls.events_port = free_port()
        cls.processes.append(cls.start(['events', '--port', str(cls.events_port)],
                                       {**env, 'QUERYAPIGATE_HOME': events_home}))
        for base in [*cls.nodes, f'http://127.0.0.1:{cls.events_port}']:
            if not eventually(lambda base=base: cls.healthy(base), timeout=30):
                cls.tearDownClass()
                raise RuntimeError(f'{base} did not start')
        status, body, _ = call(cls.nodes[0], 'POST', '/api/v1/connections',
                               {'name': 'shop', 'db': 'sqlite', 'database': cls.data, 'active': True})
        assert status == 201, body

    @classmethod
    def start(cls, args, env):
        return subprocess.Popen([sys.executable, '-m', 'queryapigate', *args], env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    @staticmethod
    def healthy(base):
        try:
            with urllib.request.urlopen(base + '/health', timeout=2) as res:
                return res.status == 200
        except OSError:
            return False

    @classmethod
    def tearDownClass(cls):
        for process in cls.processes:
            process.terminate()
        for process in cls.processes:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
        import psycopg2
        conn = psycopg2.connect(DATABASE_URL)
        conn.autocommit = True
        conn.cursor().execute(f'DROP SCHEMA IF EXISTS {cls.schema} CASCADE')
        conn.close()
        cls.tmp.cleanup()

    def unique(self, prefix):
        return f'{prefix}_{uuid.uuid4().hex[:8]}'

    def save_query(self, node, name, sql, **fields):
        status, body, _ = call(node, 'POST', '/api/v1/queries',
                               {'name': name, 'sql': sql, 'connection_name': 'shop', 'description': 'd',
                                'publish': True, **fields})
        self.assertEqual(status, 201, body)

    def new_key(self, node, **grants):
        status, body, _ = call(node, 'POST', '/api/v1/api-keys', {'name': self.unique('key'), **grants})
        self.assertEqual(status, 201, body)
        return body['name'], body['secret']

    # ---- the checks ----

    def test_a_key_created_on_one_node_works_on_the_others_and_revoking_it_is_immediate(self):
        a, b, c = self.nodes
        name, secret = self.new_key(a, connections=['shop'])
        self.assertEqual(call(b, 'POST', '/execute_sql', {'sql': 'SELECT 1 AS x', 'connection_name': 'shop'},
                              key=secret)[0], 200)
        self.assertEqual(call(c, 'DELETE', f'/api/v1/api-keys/{name}')[0], 204)
        status, body, _ = call(b, 'POST', '/execute_sql', {'sql': 'SELECT 1', 'connection_name': 'shop'}, key=secret)
        self.assertEqual((status, body['code']), (401, 'unauthorized'))

    def test_a_new_version_published_on_one_node_is_what_the_others_run(self):
        a, b, c = self.nodes
        name = self.unique('q')
        self.save_query(a, name, "SELECT 'v1' AS v")
        self.assertEqual(call(b, 'GET', f'/q/{name}')[1], [{'v': 'v1'}])
        version = {'sql': "SELECT 'v2' AS v", 'description': 'd', 'connection_name': 'shop', 'publish': True}
        status, body, _ = call(c, 'POST', f'/api/v1/queries/{name}/versions', version)
        self.assertEqual(status, 201, body)
        self.assertEqual(call(a, 'GET', f'/q/{name}')[1], [{'v': 'v2'}])
        self.assertEqual(call(b, 'GET', f'/q/{name}')[1], [{'v': 'v2'}])

    def test_a_connection_changed_on_one_node_is_used_by_the_others(self):
        a, b, _ = self.nodes
        name = self.unique('conn')
        call(a, 'POST', '/api/v1/connections', {'name': name, 'db': 'sqlite', 'database': self.data, 'active': True})
        sql = {'sql': 'SELECT label FROM things', 'connection_name': name}
        self.assertEqual(call(b, 'POST', '/execute_sql', sql)[1], [{'label': 'shop'}])  # b now holds a pooled one
        self.assertEqual(call(a, 'PATCH', f'/api/v1/connections/{name}', {'database': self.other})[0], 200)
        self.assertEqual(call(b, 'POST', '/execute_sql', sql)[1], [{'label': 'other'}])

    def test_a_keys_rate_limit_is_one_budget_across_the_nodes(self):
        _, secret = self.new_key(self.nodes[0], connections=['shop'], rate_limit='4/minute')
        body = {'sql': 'SELECT 1', 'connection_name': 'shop'}
        statuses = [call(self.nodes[i % NODES], 'POST', '/execute_sql', body, key=secret)[0] for i in range(6)]
        self.assertEqual(statuses, [200, 200, 200, 200, 429, 429])

    def test_the_response_cache_is_shared_and_cleared_everywhere(self):
        a, b, c = self.nodes
        name = self.unique('cached')
        self.save_query(a, name, 'SELECT id FROM things', cache_ttl=60)
        self.assertEqual(call(a, 'GET', f'/q/{name}')[2]['X-Cache'], 'MISS')
        self.assertEqual(call(b, 'GET', f'/q/{name}')[2]['X-Cache'], 'HIT')
        self.assertEqual(call(c, 'DELETE', '/api/v1/cache/entries')[0], 204)
        self.assertEqual(call(b, 'GET', f'/q/{name}')[2]['X-Cache'], 'MISS')

    def test_runs_on_every_node_are_in_one_history(self):
        name = self.unique('hist')
        self.save_query(self.nodes[0], name, 'SELECT 1 AS one')
        request_ids = []
        for node in self.nodes:
            request_ids.append(call(node, 'GET', f'/q/{name}')[2]['X-Request-Id'])

        def recorded():
            items = call(self.nodes[2], 'GET', f'/api/v1/history?query={name}')[1]['items']
            return {item['request_id'] for item in items} >= set(request_ids)
        self.assertTrue(eventually(recorded), 'every node\'s run should be in the shared history')

    def test_a_change_on_one_node_is_in_the_audit_log_seen_from_another(self):
        name, _ = self.new_key(self.nodes[1], connections=[])
        items = call(self.nodes[2], 'GET', f'/api/v1/audit?target={name}')[1]['items']
        self.assertEqual([item['action'] for item in items], ['create_key'])

    def test_one_events_stream_sees_runs_from_every_node(self):
        name = self.unique('live')
        self.save_query(self.nodes[0], name, 'SELECT 1 AS one')
        received, ready = [], threading.Event()

        def listen():
            sock = socket.create_connection(('127.0.0.1', self.events_port), timeout=15)
            sock.sendall(f'GET /events HTTP/1.1\r\nHost: x\r\nX-API-Key: {ADMIN}\r\n\r\n'.encode())
            stream = sock.makefile('rb')
            ready.set()
            try:
                while len(received) < NODES:
                    line = stream.readline().decode()
                    if not line:
                        break
                    if line.startswith('data: '):
                        event = json.loads(line[6:])
                        if event.get('filename') == name:
                            received.append(event['entry']['request_id'])
            except OSError:
                pass
            finally:
                sock.close()

        thread = threading.Thread(target=listen, daemon=True)
        thread.start()
        ready.wait(5)
        time.sleep(0.5)  # the stream's first poll of the store, so it starts from "now"
        sent = [call(node, 'GET', f'/q/{name}')[2]['X-Request-Id'] for node in self.nodes]
        thread.join(15)
        self.assertEqual(sorted(received), sorted(sent))


if __name__ == '__main__':
    unittest.main()

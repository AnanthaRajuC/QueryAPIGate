"""End to end: the data plane in front of a real gunicorn forwards every request unchanged (ADR 0028, step 2).

The same requests go to the Python server directly and through the data plane, and must come back the same - status,
headers and body - apart from what a proxy must change (hop-by-hop headers, Date). Also: `/events` and a streamed
export pass through as they're produced, a client's own X-QAG-* headers never reach Python, and open-access mode still
refuses a client that isn't on loopback, though every forwarded request reaches Python from loopback.

Run by hand, not by the repository's CI (no Rust toolchain there yet):

    cargo build --release --manifest-path dataplane/Cargo.toml
    .venv/bin/python dataplane/tests/test_forwarding.py

QUERYAPIGATE_DATAPLANE_BIN names another build of the binary.
"""
import http.client
import json
import os
import shutil
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
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SECRET = 'forwarding-test-secret-0123456789'
ADMIN = 'forwarding-admin-key'
IGNORED = {'date', 'connection', 'keep-alive', 'transfer-encoding'}  # what a proxy may, or must, change


def binary():
    named = os.environ.get('QUERYAPIGATE_DATAPLANE_BIN')
    if named:
        return Path(named)
    for build in ('release', 'debug'):
        path = ROOT / 'dataplane' / 'target' / build / 'queryapigate-dataplane'
        if path.exists():
            return path
    return None


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def local_address():
    """This machine's address on its default route, if it has one - not loopback."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        try:
            sock.connect(('192.0.2.1', 9))  # TEST-NET-1: nothing is sent, the kernel only picks a route
        except OSError:
            return None
        address = sock.getsockname()[0]
    return None if address.startswith('127.') else address


def wait_for(url, process, what):
    for _ in range(200):
        if process.poll() is not None:
            raise RuntimeError(f'{what} exited with {process.returncode}')
        try:
            urllib.request.urlopen(url, timeout=1).read()
            return
        except (urllib.error.URLError, ConnectionError, OSError):
            time.sleep(0.05)
    raise RuntimeError(f'{what} did not start')


class Pair:
    """A gunicorn and a data plane in front of it, on a throwaway home."""

    def __init__(self, home, env, listen_host='127.0.0.1'):
        self.python_port, self.port = free_port(), free_port()
        self.listen_host = listen_host
        self.env = {**os.environ, 'QUERYAPIGATE_HOME': str(home), 'QUERYAPIGATE_DATAPLANE_SECRET': SECRET,
                    'QUERYAPIGATE_HISTORY_FLUSH_INTERVAL': '0', 'QUERYAPIGATE_ACCESS_LOG': 'off',
                    'PYTHONPATH': str(ROOT), **env}
        if 'QUERYAPIGATE_DATABASE_URL' not in env:
            self.env.pop('QUERYAPIGATE_DATABASE_URL', None)
        self.home = Path(home)
        self.log = log = open(self.home / 'python.log', 'a')
        self.dataplane_log = open(self.home / 'dataplane.log', 'a')
        self.python = subprocess.Popen(
            [str(Path(sys.executable).parent / 'gunicorn'), '--bind', f'127.0.0.1:{self.python_port}', '--workers',
             '1', '--worker-class', 'gthread', '--threads', '4', '--no-control-socket', '-c',
             'python:queryapigate.gunicorn_conf', 'queryapigate.app:create_app()'],
            env=self.env, cwd=home, stdout=log, stderr=log, start_new_session=True)
        # The data plane reads the store Python makes: Python first
        wait_for(f'http://127.0.0.1:{self.python_port}/health', self.python, 'gunicorn')
        self.dataplane = subprocess.Popen(
            [str(binary())], env={**self.env, 'QUERYAPIGATE_DATAPLANE_LISTEN': f'{listen_host}:{self.port}',
                                  'QUERYAPIGATE_DATAPLANE_UPSTREAM': f'http://127.0.0.1:{self.python_port}',
                                  'QUERYAPIGATE_DATAPLANE_THREADS': '2'},
            stdout=self.dataplane_log, stderr=self.dataplane_log, start_new_session=True)
        wait_for(f'http://{listen_host}:{self.port}/health', self.dataplane, 'the data plane')

    def stop(self):
        for process in (self.dataplane, self.python):
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
        self.log.close()
        self.dataplane_log.close()


def request(host, port, method, path, headers=None, body=None):
    """(status, {header: value}, body) - raw, through http.client, so nothing is followed or decoded."""
    conn = http.client.HTTPConnection(host, port, timeout=10)
    data = json.dumps(body).encode() if body is not None else None
    headers = {**(headers or {}), **({'Content-Type': 'application/json'} if data is not None else {})}
    conn.request(method, path, body=data, headers=headers)
    res = conn.getresponse()
    payload = res.read()
    found = {}
    for name, value in res.getheaders():
        found.setdefault(name.lower(), []).append(value)
    conn.close()
    return res.status, found, payload


@unittest.skipIf(binary() is None, 'build it first: cargo build --release --manifest-path dataplane/Cargo.toml')
@unittest.skipIf(shutil.which('gunicorn', path=str(Path(sys.executable).parent)) is None, 'needs gunicorn')
class ForwardingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        home = Path(cls.tmp.name)
        data = home / 'films.db'
        conn = sqlite3.connect(data)
        conn.execute('CREATE TABLE film (id INTEGER, title TEXT, rating TEXT)')
        conn.executemany('INSERT INTO film VALUES (?, ?, ?)', [(i, f'Film {i}', 'PG' if i % 2 else 'R')
                                                               for i in range(1, 26)])
        conn.commit()
        conn.close()
        cls.pair = Pair(home, {'QUERYAPIGATE_API_KEY': ADMIN})
        admin = {'X-API-Key': ADMIN}
        python = ('127.0.0.1', cls.pair.python_port)
        for path, body in (
                ('/api/v1/connections', {'name': 'films', 'db': 'sqlite', 'database': str(data), 'active': True}),
                ('/api/v1/queries', {'name': 'by_rating', 'connection_name': 'films', 'description': 'd',
                                     'sql': 'SELECT id, title FROM film WHERE rating = :rating ORDER BY id',
                                     'parameters': {'rating': 'str'}, 'publish': True})):
            status, _h, payload = request(*python, 'POST', path, admin, body)
            assert status == 201, payload
        status, _h, payload = request(*python, 'POST', '/api/v1/api-keys', admin,
                                      {'name': 'reporting', 'connections': ['films']})
        assert status == 201, payload
        cls.key = json.loads(payload)['secret']

    @classmethod
    def tearDownClass(cls):
        cls.pair.stop()
        cls.tmp.cleanup()

    def both(self, method, path, headers=None, body=None):
        """The same request, direct and through the data plane, each with the same request ID."""
        headers = {'X-Request-Id': 'forwarding-test', **(headers or {})}
        direct = request('127.0.0.1', self.pair.python_port, method, path, headers, body)
        forwarded = request('127.0.0.1', self.pair.port, method, path, headers, body)
        return direct, forwarded

    def assertSame(self, method, path, headers=None, body=None):
        (status, headers_a, body_a), (status_b, headers_b, body_b) = self.both(method, path, headers, body)
        self.assertEqual(status_b, status, body_b)
        self.assertEqual(body_b, body_a)
        strip = lambda found: {k: v for k, v in found.items() if k not in IGNORED}  # noqa: E731
        self.assertEqual(strip(headers_b), strip(headers_a))
        return status, body_a

    def test_requests_come_back_the_same_through_the_data_plane(self):
        admin, key = {'X-API-Key': ADMIN}, {'X-API-Key': self.key}
        for method, path, headers, body in (
                ('GET', '/health', None, None),
                ('GET', '/openapi.json', key, None),
                ('GET', '/catalog', key, None),
                ('GET', '/q/by_rating?rating=PG', key, None),
                ('GET', '/q/by_rating?rating=R&page=2&page_size=3', key, None),
                ('POST', '/q/by_rating', key, {'params': {'rating': 'PG'}, 'format': 'csv'}),
                ('GET', '/q/by_rating?rating=PG&format=xml', key, None),
                ('GET', '/q/by_rating', key, None),  # 400: the parameter is required
                ('GET', '/q/missing', key, None),  # 404
                ('GET', '/catalog', {'X-API-Key': 'wrong'}, None),  # 401
                ('POST', '/execute_sql', admin, {'sql': 'SELECT COUNT(*) AS n FROM film', 'connection_name': 'films'}),
                ('GET', '/api/v1/connections', admin, None),
                ('GET', '/api/v1/queries/by_rating', admin, None),
                ('OPTIONS', '/q/by_rating', {'Origin': 'https://example.com', 'Access-Control-Request-Method': 'GET'},
                 None)):
            with self.subTest(method=method, path=path):
                self.assertSame(method, path, headers, body)

    def test_a_streamed_export_passes_through_whole(self):
        status, body = self.assertSame('GET', '/q/by_rating?rating=PG&format=csv&stream=true',
                                       {'X-API-Key': self.key})
        self.assertEqual(status, 200)
        self.assertEqual(body.decode().count('\n'), 14)  # the header and 13 films

    def test_a_client_s_own_x_qag_headers_never_reach_python(self):
        spoofed = {'X-API-Key': self.key, 'X-QAG-Dataplane': SECRET, 'X-QAG-Peer': '203.0.113.9',
                   'X-QAG-Plan': 'e30=', 'X-QAG-Dataplane-Version': '0.0.1'}
        # Even sending the right secret, straight to the data plane, buys nothing: it is replaced, not passed on
        (_s, _h, _b), (status, headers, _body) = self.both('GET', '/q/by_rating?rating=PG', spoofed)
        self.assertEqual(status, 200)
        self.assertNotIn('x-qag-plan', headers)

    def test_live_events_stream_through_as_they_happen(self):
        conn = http.client.HTTPConnection('127.0.0.1', self.pair.port, timeout=10)
        conn.request('GET', '/events', headers={'X-API-Key': ADMIN})
        # The stream's headers go out with its first event, so the run that makes one starts first
        trigger = threading.Timer(0.5, lambda: request('127.0.0.1', self.pair.port, 'GET', '/q/by_rating?rating=R',
                                                       {'X-API-Key': self.key}))
        trigger.start()
        started = time.monotonic()
        res = conn.getresponse()
        self.assertEqual(res.status, 200)
        self.assertEqual(res.getheader('Content-Type'), 'text/event-stream; charset=utf-8')
        line = res.fp.readline()
        while line and not line.startswith(b'data:'):
            line = res.fp.readline()
        self.assertTrue(line.startswith(b'data:'), line)
        self.assertLess(time.monotonic() - started, 5)  # as it happened, not buffered until the stream ends
        self.assertEqual(json.loads(line[5:])['query'], 'by_rating')
        conn.close()
        trigger.join()


@unittest.skipIf(binary() is None, 'build the data plane first')
@unittest.skipIf(local_address() is None, 'needs a network address that is not loopback')
class OpenAccessTests(unittest.TestCase):
    """No key configured: the Python server answers loopback clients only. Behind the data plane every request
    reaches it from loopback, so the client's own address must be what it judges."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        env = {name: '' for name in ('QUERYAPIGATE_API_KEY', 'QUERYAPIGATE_JWT_SECRET', 'QUERYAPIGATE_JWKS_URL')}
        cls.address = local_address()
        cls.pair = Pair(Path(cls.tmp.name), env, listen_host=cls.address)

    @classmethod
    def tearDownClass(cls):
        cls.pair.stop()
        cls.tmp.cleanup()

    def test_a_client_that_is_not_on_loopback_is_refused(self):
        status, _h, body = request(self.address, self.pair.port, 'GET', '/catalog')
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body)['code'], 'open_access_remote')

    def test_health_stays_public(self):
        self.assertEqual(request(self.address, self.pair.port, 'GET', '/health')[0], 200)


if __name__ == '__main__':
    unittest.main()

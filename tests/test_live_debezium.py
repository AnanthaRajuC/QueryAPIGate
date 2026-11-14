"""Live queries against a real Debezium Server (ADR 0033 §7, item 6): section 9's manual test, run in CI.

PostgreSQL 16 with logical decoding and Debezium Server 3.7.1 run in Docker, each pinned by digest. QueryAPIGate runs
as it's deployed - `queryapigate serve` and `queryapigate events`, two processes on one store - and is set up only
through its API: a connection, a saved query, a key that may run only that query, and a CDC source whose generated
secret Debezium signs its posts with. Changes are made in PostgreSQL directly, as another application would make them,
and must reach a `/live` stream through Debezium.

Skipped unless QUERYAPIGATE_TEST_DEBEZIUM=1 (and Docker can run containers): it takes about a minute, most of it
Debezium starting. The `Live queries (Debezium)` workflow runs it on pull requests that touch live queries.
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
import uuid

from tests.test_events_server import Stream

POSTGRES_IMAGE = 'postgres:16@sha256:63e83b5a3dc36a917dccb76fe1bd2ac51f1ec9c1deb47ff20d4f04233bf6eae9'
DEBEZIUM_IMAGE = ('quay.io/debezium/server:3.7.1.Final'
                  '@sha256:90561ee67e28312784c35a439e75d4fb416caa6b5726c1d0c05c962d1d2251bf')
ADMIN = {'X-API-Key': 'admin'}
PUSH_WITHIN = 30  # seconds from a change in PostgreSQL to the new result on the stream (measured: 0.5 to 2)
ENABLED = os.environ.get('QUERYAPIGATE_TEST_DEBEZIUM') == '1'

SCHEMA = """
CREATE TABLE film (id int PRIMARY KEY, title text NOT NULL);
INSERT INTO film VALUES (1, 'Academy Dinosaur'), (2, 'Ace Goldfinger'), (3, 'Adaptation Holes');
CREATE TABLE rental (id int PRIMARY KEY, film_id int NOT NULL, returned boolean NOT NULL DEFAULT false);
INSERT INTO rental SELECT n, 1 + n % 3 FROM generate_series(1, 2000) AS n;
"""

PROPERTIES = """\
debezium.source.connector.class=io.debezium.connector.postgresql.PostgresConnector
debezium.source.plugin.name=pgoutput
debezium.source.database.hostname=127.0.0.1
debezium.source.database.port={pg_port}
debezium.source.database.user=postgres
debezium.source.database.password=pw
debezium.source.database.dbname=shop
debezium.source.topic.prefix=shop
debezium.source.table.include.list=public.film,public.rental
debezium.source.snapshot.mode=no_data
debezium.source.offset.storage.file.filename=/tmp/offsets.dat
debezium.source.offset.flush.interval.ms=0
debezium.sink.type=http
debezium.sink.http.url=http://127.0.0.1:{api_port}{ingest_path}
debezium.sink.http.batch.enabled=true
debezium.sink.http.authentication.type=standard-webhooks
debezium.sink.http.authentication.webhook.secret={secret}
debezium.format.value=json
quarkus.http.port={health_port}
"""


def _free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def _docker_usable():
    if shutil.which('docker') is None:
        return False
    return subprocess.run(['docker', 'info'], capture_output=True).returncode == 0


def _wait(what, check, timeout):
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        try:
            if check():
                print(f'{what}: {time.monotonic() - started:.1f} s', file=sys.stderr)
                return
        except Exception:  # not up yet
            pass
        time.sleep(0.5)
    raise AssertionError(f'{what} not ready after {timeout} s')


def _call(port, method, path, body=None):
    request = urllib.request.Request(f'http://127.0.0.1:{port}{path}', method=method,
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={**ADMIN, 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read() or b'null')
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b'null')


@unittest.skipUnless(ENABLED, 'QUERYAPIGATE_TEST_DEBEZIUM=1 runs it - with Docker, about a minute')
class DebeziumTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not _docker_usable():
            raise unittest.SkipTest('Docker cannot run containers here')
        cls.containers, cls.processes = [], []
        cls.tmp = tempfile.mkdtemp(prefix='qag-debezium-')
        try:
            cls._start()
        except BaseException:
            cls._dump_logs()
            cls._stop()
            raise

    @classmethod
    def tearDownClass(cls):
        if os.environ.get('QUERYAPIGATE_TEST_DEBEZIUM_LOGS') == '1':
            cls._dump_logs()
        cls._stop()

    # ---- the stack ----

    @classmethod
    def _start(cls):
        import psycopg2
        suffix = uuid.uuid4().hex[:8]
        cls.pg_port, cls.api_port, cls.events_port, cls.health_port = (_free_port() for _ in range(4))

        cls.postgres = cls._container(f'qag-debezium-pg-{suffix}', [
            '-p', f'127.0.0.1:{cls.pg_port}:5432', '-e', 'POSTGRES_PASSWORD=pw', '-e', 'POSTGRES_DB=shop',
            POSTGRES_IMAGE, 'postgres', '-c', 'wal_level=logical'])
        # The image's first start initialises on a Unix socket only: a TCP connection means the real server is up
        _wait('PostgreSQL', lambda: psycopg2.connect(host='127.0.0.1', port=cls.pg_port, user='postgres',
                                                     password='pw', dbname='shop').close() or True, 90)
        cls.db = psycopg2.connect(host='127.0.0.1', port=cls.pg_port, user='postgres', password='pw', dbname='shop')
        cls.db.autocommit = True
        with cls.db.cursor() as cur:
            cur.execute(SCHEMA)

        env = {**os.environ, 'QUERYAPIGATE_HOME': cls.tmp, 'QUERYAPIGATE_API_KEY': 'admin',
               'QUERYAPIGATE_LIVE_QUERIES': '1', 'QUERYAPIGATE_RATE_LIMIT': '',
               'QUERYAPIGATE_HISTORY_FLUSH_INTERVAL': '0'}
        for name in ('QUERYAPIGATE_DATABASE_URL', 'QUERYAPIGATE_RUN_STORE_URL', 'QUERYAPIGATE_REDIS_URL'):
            env.pop(name, None)  # one SQLite store in the temporary home, shared by both processes
        for command, port in (('serve', cls.api_port), ('events', cls.events_port)):
            log = open(os.path.join(cls.tmp, f'{command}.log'), 'w')
            cls.processes.append((command, log, subprocess.Popen(
                [sys.executable, '-m', 'queryapigate', command, '--port', str(port)],
                env=env, stdout=log, stderr=subprocess.STDOUT)))
        _wait('queryapigate serve', lambda: _call(cls.api_port, 'GET', '/health')[0] == 200, 60)
        _wait('queryapigate events', lambda: _call(cls.events_port, 'GET', '/health')[0] == 200, 60)

        # Set up as an administrator would, through the API only
        cls._created(_call(cls.api_port, 'POST', '/api/v1/connections', {
            'name': 'shop', 'db': 'postgres', 'host': '127.0.0.1', 'port': cls.pg_port, 'user': 'postgres',
            'password': 'pw', 'database': 'shop'}))
        cls._created(_call(cls.api_port, 'POST', '/api/v1/queries', {
            'name': 'film', 'description': 'One film', 'connection_name': 'shop', 'publish': True,
            'sql': 'SELECT id, title FROM film WHERE id = :id', 'parameters': {'id': {'type': 'int'}}}))
        cls.reader = cls._created(_call(cls.api_port, 'POST', '/api/v1/api-keys', {
            'name': 'reader', 'connections': [], 'queries': ['film']}))['secret']
        source = cls._created(_call(cls.api_port, 'POST', '/api/v1/cdc-sources', {
            'name': 'shop', 'connection': 'shop', 'server_name': 'shop'}))

        properties = os.path.join(cls.tmp, 'application.properties')
        with open(properties, 'w') as f:
            f.write(PROPERTIES.format(pg_port=cls.pg_port, api_port=cls.api_port, ingest_path=source['ingest_path'],
                                      secret=source['secret'], health_port=cls.health_port))
        os.chmod(properties, 0o644)  # Debezium Server reads it as its own user
        cls.debezium = cls._container(f'qag-debezium-server-{suffix}', [
            '--network', 'host', '-v', f'{properties}:/debezium/config/application.properties:ro', DEBEZIUM_IMAGE])
        _wait('Debezium Server', lambda: urllib.request.urlopen(
            f'http://127.0.0.1:{cls.health_port}/q/health/ready', timeout=5).status == 200, 120)

        # Ready isn't streaming yet: change a table no test watches until a post has arrived, signed and verified
        def received():
            with cls.db.cursor() as cur:
                cur.execute('UPDATE rental SET returned = NOT returned WHERE id = 1')
            return _call(cls.api_port, 'GET', '/api/v1/cdc-sources/shop')[1]['last_received_at'] is not None
        _wait('a first change through Debezium', received, 90)

    @classmethod
    def _container(cls, name, args):
        subprocess.run(['docker', 'run', '-d', '--name', name, *args], check=True, capture_output=True)
        cls.containers.append(name)
        return name

    @staticmethod
    def _created(answer):
        status, body = answer
        assert status == 201, (status, body)
        return body

    @classmethod
    def _dump_logs(cls):
        for name in cls.containers:
            logs = subprocess.run(['docker', 'logs', '--tail', '40', name], capture_output=True, text=True)
            print(f'\n---- {name} ----\n{logs.stdout}{logs.stderr}', file=sys.stderr)
        for command, log, _ in cls.processes:
            log.flush()
            with open(log.name) as f:
                print(f'\n---- queryapigate {command} ----\n' + ''.join(f.readlines()[-40:]), file=sys.stderr)

    @classmethod
    def _stop(cls):
        for _, log, process in cls.processes:
            process.terminate()
            try:
                process.wait(10)
            except subprocess.TimeoutExpired:
                process.kill()
            log.close()
        if getattr(cls, 'db', None) is not None:
            cls.db.close()
        for name in cls.containers:  # -v: PostgreSQL's anonymous volume goes too
            subprocess.run(['docker', 'rm', '-fv', name], capture_output=True)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # ---- helpers ----

    def execute(self, sql, params=None):
        with self.db.cursor() as cur:
            cur.execute(sql, params)

    def subscribe(self, path, key=None):
        stream = Stream(self.events_port, path=path, headers={'X-API-Key': key or self.reader})
        self.addCleanup(stream.close)
        self.assertEqual(stream.status, 200)
        return stream

    def title(self, film_id):
        with self.db.cursor() as cur:
            cur.execute('SELECT title FROM film WHERE id = %s', (film_id,))
            return cur.fetchone()[0]

    # ---- tests ----

    def test_a_change_made_in_the_database_reaches_the_stream(self):
        stream = self.subscribe('/live/film?id=1')
        self.assertEqual(stream.events(1)[0][1]['rows'], [{'id': 1, 'title': self.title(1)}])
        started = time.monotonic()
        self.execute("UPDATE film SET title = 'Changed in PostgreSQL' WHERE id = 1")
        result = stream.events(1, timeout=PUSH_WITHIN)[0][1]
        self.assertEqual((result['rows'], result['reason']), ([{'id': 1, 'title': 'Changed in PostgreSQL'}], 'change'))
        print(f'\nfrom the UPDATE to the new result: {time.monotonic() - started:.2f} s', file=sys.stderr)

    def test_a_change_to_another_row_sends_nothing(self):
        stream = self.subscribe('/live/film?id=3')
        stream.events(1)
        self.execute("UPDATE film SET title = 'Another row' WHERE id = 2")  # re-runs film 3's query: same result
        self.execute("UPDATE film SET title = 'Third, changed' WHERE id = 3")
        # the next event is the change to film 3: nothing was sent for film 2's
        self.assertEqual(stream.events(1, timeout=PUSH_WITHIN)[0][1]['rows'], [{'id': 3, 'title': 'Third, changed'}])

    def test_a_large_update_is_a_few_signals_and_one_result(self):
        signals = self.subscribe('/events?type=table.changed', key='admin')
        stream = self.subscribe('/live/film?id=2')
        stream.events(1)
        self.execute('UPDATE rental SET returned = NOT returned')  # 2,000 rows, none of them the stream's table
        self.execute("UPDATE film SET title = 'After the rentals' WHERE id = 2")
        self.assertEqual(stream.events(1, timeout=PUSH_WITHIN)[0][1]['rows'], [{'id': 2, 'title': 'After the rentals'}])
        # Debezium posts in the log's order, so the rentals' signals are all in before the film's: collect them
        heard = []
        while True:
            try:
                heard.append(signals.events(1, timeout=3)[0][1])
            except (socket.timeout, TimeoutError):
                break
        rental = [event for event in heard if event.get('tables') == ['public.rental']]
        self.assertGreaterEqual(len(rental), 1, heard)
        # batched by Debezium's HTTP sink: a handful of posts, not 2,000 (section 9 saw 10 to 11 for 2,000 rows)
        self.assertLess(len(rental), 50, rental)

    def test_a_post_signed_with_another_secret_is_refused(self):
        body = b'[]'
        stamp = str(int(time.time()))
        request = urllib.request.Request(
            f'http://127.0.0.1:{self.api_port}/ingest/cdc/shop', method='POST', data=body,
            headers={'webhook-id': 'msg_forged', 'webhook-timestamp': stamp,
                     'webhook-signature': 'v1,bm90IHRoZSBzZWNyZXQ=', 'Content-Type': 'application/json'})
        with self.assertRaises(urllib.error.HTTPError) as refused:
            urllib.request.urlopen(request, timeout=10)
        self.assertEqual(refused.exception.code, 404)


if __name__ == '__main__':
    unittest.main()

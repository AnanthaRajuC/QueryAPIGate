"""Rate-limited keys and a server-wide limit, served by the data plane with Redis (ADR 0032, section 8).

The same sequences of calls go through the data plane and to Python directly, and must come back the same - status,
headers, body - call by call, across the refill boundary. The two paths draw from one gunicorn and one Redis, so each
has its own key with the same limit, and, behind one trusted proxy, its own client address (IPv4 and IPv6). Plus:

- one budget, spent half through the data plane and half through Python, is refused at the call Python alone refuses;
- a call forwarded after it was counted (its query fails in the database) is counted once;
- with Redis cut mid-sequence, nothing is refused that Redis didn't refuse, and no limit is lifted;
- the data plane's 429s reach Python's /metrics and its key_rate_limited alert;
- without QUERYAPIGATE_REDIS_URL, every limited call is Python's.

Both processes reach Redis through a relay the tests can cut.

    cargo build --release --manifest-path dataplane/Cargo.toml
    QUERYAPIGATE_DATAPLANE_TEST_POSTGRES=127.0.0.1:55435:postgres:pw:postgres \\
    QUERYAPIGATE_DATAPLANE_TEST_REDIS_URL=redis://127.0.0.1:56379/0 \\
    .venv/bin/python dataplane/tests/test_rate_limits.py
"""
import itertools
import json
import os
import re
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from test_forwarding import IGNORED, Pair, binary, free_port, request  # noqa: E402
from test_queries import details  # noqa: E402

ADMIN = 'rate-limit-admin-key'
POSTGRES = 'QUERYAPIGATE_DATAPLANE_TEST_POSTGRES'
REDIS = os.environ.get('QUERYAPIGATE_DATAPLANE_TEST_REDIS_URL')
COMPARED_HEADERS = IGNORED | {'server'}
QUERIES = {
    'rl_item': ('SELECT id, name FROM rl_item WHERE id = :id', {'id': 'int'}),
    # 10 / 0 fails in the database: the data plane forwards a call it has counted already
    'rl_ratio': ('SELECT 10 / :n AS x', {'n': 'int'}),
}
# Each path's client, behind the one trusted proxy: the server-wide limit counts by address
THROUGH, DIRECT = '198.51.100.1', '198.51.100.2'
_admin_addresses = itertools.count(1)


class Relay:
    """A TCP relay in front of Redis, which a test can cut - as if Redis had stopped - and restore."""

    def __init__(self, url):
        self.parts = urllib.parse.urlsplit(url)
        self.target = (self.parts.hostname, self.parts.port or 6379)
        self.port = free_port()
        self.lock = threading.Lock()
        self.sockets = []
        self.listener = None
        self.restore()

    @property
    def url(self):
        auth = self.parts.netloc.rpartition('@')[0]
        netloc = f'{auth}@127.0.0.1:{self.port}' if auth else f'127.0.0.1:{self.port}'
        return urllib.parse.urlunsplit((self.parts.scheme, netloc, self.parts.path, '', ''))

    def restore(self):
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(('127.0.0.1', self.port))
        listener.listen()
        self.listener = listener
        threading.Thread(target=self._accept, args=(listener,), daemon=True).start()

    def cut(self):
        """Every connection dropped, and none taken: connecting is refused, as it is to a stopped Redis."""
        self.listener.shutdown(socket.SHUT_RDWR)
        self.listener.close()
        with self.lock:
            for sock in self.sockets:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                sock.close()
            self.sockets.clear()

    def _accept(self, listener):
        while True:
            try:
                client, _ = listener.accept()
            except OSError:
                return
            upstream = socket.create_connection(self.target)
            with self.lock:
                self.sockets += [client, upstream]
            for source, sink in ((client, upstream), (upstream, client)):
                threading.Thread(target=self._pump, args=(source, sink), daemon=True).start()

    @staticmethod
    def _pump(source, sink):
        try:
            while data := source.recv(65536):
                sink.sendall(data)
        except OSError:
            pass
        finally:
            for sock in (source, sink):
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass


def load(settings):
    import psycopg2
    conn = psycopg2.connect(host=settings['host'], port=settings['port'], user=settings['user'],
                            password=settings['password'], dbname=settings['database'])
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute('DROP TABLE IF EXISTS rl_item; CREATE TABLE rl_item (id int PRIMARY KEY, name text)')
    cur.executemany('INSERT INTO rl_item VALUES (%s, %s)', [(i, f'item {i}') for i in range(1, 6)])
    conn.close()


@unittest.skipIf(binary() is None, 'build the data plane first')
@unittest.skipIf(details(POSTGRES) is None, f'set {POSTGRES}')
@unittest.skipIf(REDIS is None, 'set QUERYAPIGATE_DATAPLANE_TEST_REDIS_URL')
class LimitedTestCase(unittest.TestCase):
    """A gunicorn and the data plane in front of it, both counting in one Redis, behind one trusted proxy."""

    env = {}
    redis = True

    @classmethod
    def setUpClass(cls):
        import redis
        settings = details(POSTGRES)
        load(settings)
        redis.Redis.from_url(REDIS).flushdb()  # the buckets, from full
        cls.relay = Relay(REDIS)
        cls.tmp = tempfile.TemporaryDirectory()
        cls.pair = Pair(Path(cls.tmp.name), {'QUERYAPIGATE_API_KEY': ADMIN, 'QUERYAPIGATE_TRUST_PROXY': '1',
                                             **({'QUERYAPIGATE_REDIS_URL': cls.relay.url} if cls.redis else {}),
                                             **cls.env})
        status, _h, body = cls.api('POST', '/api/v1/connections', {'name': 'shop', 'db': 'postgres', **settings,
                                                                    'active': True})
        assert status == 201, body
        for name, (sql, parameters) in QUERIES.items():
            status, _h, body = cls.api('POST', '/api/v1/queries', {'name': name, 'connection_name': 'shop',
                                                                    'description': name, 'sql': sql,
                                                                    'parameters': parameters, 'publish': True})
            assert status == 201, (name, body)

    @classmethod
    def tearDownClass(cls):
        cls.pair.stop()
        cls.relay.cut()
        cls.tmp.cleanup()

    @classmethod
    def api(cls, method, path, body=None):
        """An administrator's call, each from its own address: the server-wide limit is for the tests' callers."""
        address = next(_admin_addresses)
        headers = {'X-API-Key': ADMIN, 'X-Forwarded-For': f'192.0.2.{address % 250 + 1}'}
        return request('127.0.0.1', cls.pair.python_port, method, path, headers, body)

    @classmethod
    def key(cls, name, rate_limit=None):
        status, _h, body = cls.api('POST', '/api/v1/api-keys', {'name': name, 'connections': ['shop'],
                                                                **({'rate_limit': rate_limit} if rate_limit else {})})
        assert status == 201, body
        return json.loads(body)['secret']

    def call(self, port, secret, client, query, value, rid):
        path = f'/q/{query}?' + urllib.parse.urlencode({'n' if query == 'rl_ratio' else 'id': value})
        return request('127.0.0.1', port, 'GET', path, {'X-API-Key': secret, 'X-Forwarded-For': client,
                                                        'X-Request-Id': rid})

    def served(self):
        """What the data plane answered itself so far: its queries run, and its 429s."""
        log = (self.pair.home / 'dataplane.log').read_text()
        return log.count('Executing on '), len(re.findall(r' -> 429 in ', log))

    def compare(self, steps, limit, through_client=THROUGH, direct_client=DIRECT):
        """Each step - a call (query, value), or a pause in seconds - made through the data plane and directly, each
        with its own key of `limit`; returns the pairs of answers, having checked each pair is the same."""
        tag = f'{self.id().rsplit(".", 1)[1][:40]}-{time.monotonic_ns()}'
        through_key = self.key(f'through-{tag}', limit)
        direct_key = self.key(f'direct-{tag}', limit)
        answers = []
        for number, step in enumerate(steps):
            if isinstance(step, (int, float)):
                time.sleep(step)
                continue
            rid = f'rl-{number}-{time.monotonic_ns()}'
            through = self.call(self.pair.port, through_key, through_client, *step, rid)
            direct = self.call(self.pair.python_port, direct_key, direct_client, *step, rid)
            answers.append((through, direct))
        strip = lambda h: {k: v for k, v in h.items() if k not in COMPARED_HEADERS}  # noqa: E731
        different = [{'call': n, 'through': (t[0], strip(t[1]), t[2][:200]), 'direct': (d[0], strip(d[1]), d[2][:200])}
                     for n, (t, d) in enumerate(answers) if (t[0], strip(t[1]), t[2]) != (d[0], strip(d[1]), d[2])]
        self.assertEqual(different[:3], [])
        return answers


def statuses(answers, side=0):
    return [answer[side][0] for answer in answers]


def header(answer, name):
    values = answer[1].get(name.lower())
    return values[0] if values else None


class KeyLimitTests(LimitedTestCase):
    def test_a_limited_key_is_answered_as_python_answers_it(self):
        before = self.served()
        # 2/second refills a token each half second; the pauses leave the buckets well between whole tokens
        fast = self.compare([('rl_item', 1)] * 3 + [0.75] + [('rl_item', 2)] * 2 + [0.6] + [('rl_item', 3)] * 2
                            + [('rl_item', 'x'), ('rl_ratio', 1), 0.8, ('rl_ratio', 0), ('rl_item', 1)], '2/second')
        # 3/minute: a whole burst refused, its Retry-After counted down from 20 s
        slow = self.compare([('rl_item', 1)] * 6, '3/minute')
        self.assertEqual(statuses(slow), [200, 200, 200, 429, 429, 429])
        self.assertEqual([header(t, 'X-RateLimit-Key-Remaining') for t, _d in slow], ['2', '1', '0', '0', '0', '0'])
        self.assertEqual({header(t, 'Retry-After') for t, _d in slow[3:]}, {'20'})
        self.assertIn(429, statuses(fast))
        # A new key's first two calls are Python's (its use is recorded, then its plan made), the rest the data plane's
        queries, refusals = (after - was for after, was in zip(self.served(), before, strict=True))
        self.assertGreaterEqual(queries, 4, 'the data plane served too few of the limited calls')
        self.assertGreaterEqual(refusals, 6, 'the data plane refused too few of them itself')

    def test_one_budget_spent_through_both_is_refused_where_python_alone_refuses(self):
        secret = self.key('shared', '6/minute')
        before = self.served()
        answers = [self.call(self.pair.port if n % 2 else self.pair.python_port, secret, f'203.0.113.{n}', 'rl_item',
                             1, f'shared-{n}') for n in range(9)]
        self.assertEqual([a[0] for a in answers], [200] * 6 + [429] * 3)
        self.assertEqual([header(a, 'X-RateLimit-Key-Remaining') for a in answers], ['5', '4', '3', '2', '1', '0',
                                                                                      '0', '0', '0'])
        queries, refusals = (after - was for after, was in zip(self.served(), before, strict=True))
        self.assertGreaterEqual(queries + refusals, 3, 'the data plane counted too few of the calls itself')

    def test_a_call_forwarded_after_counting_is_counted_once(self):
        secret = self.key('ratio', '10/minute')
        call = lambda n, rid: self.call(self.pair.port, secret, THROUGH, 'rl_ratio', n, rid)  # noqa: E731
        first = call(1, 'ratio-0')  # forwarded, as a key's first call in a minute is: it records the key's use
        served = call(1, 'ratio-1')
        before = self.served()[0]
        failed = call(0, 'ratio-2')  # counted here, then forwarded: 10 / 0 fails in the database
        self.assertEqual(self.served()[0], before + 1, 'the data plane did not run the failing query itself')
        after = call(1, 'ratio-3')
        self.assertEqual([header(a, 'X-RateLimit-Key-Remaining') for a in (first, served, failed, after)],
                         ['9', '8', '7', '6'])
        self.assertNotEqual(failed[0], 200)
        direct = self.call(self.pair.python_port, self.key('ratio-direct', '10/minute'), DIRECT, 'rl_ratio', 0,
                           'ratio-2')
        strip = lambda h: {k: v for k, v in h.items() if k not in COMPARED_HEADERS | {'x-ratelimit-key-remaining'}}  # noqa: E731
        self.assertEqual((failed[0], strip(failed[1]), failed[2]), (direct[0], strip(direct[1]), direct[2]))

    def test_its_429s_reach_python_s_metrics_and_alerts(self):
        secret = self.key('noisy', '1/minute')
        answers = [self.call(self.pair.port, secret, THROUGH, 'rl_item', 1, f'noisy-{n}') for n in range(13)]
        self.assertEqual([a[0] for a in answers], [200] + [429] * 12)
        time.sleep(1)  # reported with the runs
        text = self.api('GET', '/metrics')[2].decode()
        found = re.search(r'queryapigate_requests_total\{method="GET",endpoint="api.run_named_query",status="429",'
                          r'key="noisy"\} (\d+)', text)
        self.assertEqual(int(found.group(1)) if found else 0, 12)
        alerts = json.loads(self.api('GET', '/api/v1/alerts')[2])['items']
        self.assertIn('key_rate_limited:noisy', [alert['id'] for alert in alerts])

    def test_with_redis_cut_nothing_is_refused_that_redis_didnt_and_no_limit_is_lifted(self):
        through_key, direct_key = self.key('cut-through', '3/minute'), self.key('cut-direct', '3/minute')
        for secret, port, client in ((through_key, self.pair.port, THROUGH),
                                     (direct_key, self.pair.python_port, DIRECT)):
            for n in range(2):  # each key's use recorded, a plan held - and two of three calls spent, in Redis
                self.assertEqual(self.call(port, secret, client, 'rl_item', 1, f'cut-warm-{n}')[0], 200)
        self.relay.cut()
        try:
            before = self.served()
            answers = []
            for n in range(5):
                rid = f'cut-{n}'
                answers.append((self.call(self.pair.port, through_key, THROUGH, 'rl_item', 1, rid),
                                self.call(self.pair.python_port, direct_key, DIRECT, 'rl_item', 1, rid)))
            self.assertEqual(self.served(), before, 'the data plane answered a limited call without Redis')
            # Python counts in its own memory meanwhile, from a full bucket (ratelimit.py): enforced, not lifted
            self.assertEqual(statuses(answers), [200, 200, 200, 429, 429])
            self.assertEqual(statuses(answers), statuses(answers, 1))
            strip = lambda h: {k: v for k, v in h.items() if k not in COMPARED_HEADERS}  # noqa: E731
            self.assertEqual([(t[0], strip(t[1]), t[2]) for t, _d in answers],
                             [(d[0], strip(d[1]), d[2]) for _t, d in answers])
        finally:
            self.relay.restore()
            time.sleep(5.5)  # both processes leave Redis alone for 5 s after a failure


class ServerLimitTests(LimitedTestCase):
    env = {'QUERYAPIGATE_RATE_LIMIT': '4/minute'}

    def test_the_server_wide_limit_and_a_key_s_together(self):
        before = self.served()
        # The key's 2/minute refuses first, with both limits' headers; then the server's 4/minute, with its own only
        answers = self.compare([('rl_item', 1)] * 6, '2/minute')
        self.assertEqual(statuses(answers), [200, 200, 429, 429, 429, 429])
        through = [t for t, _d in answers]
        self.assertEqual([header(a, 'X-RateLimit-Remaining') for a in through], ['3', '2', '1', '0', '0', '0'])
        self.assertEqual([header(a, 'X-RateLimit-Key-Remaining') for a in through], ['1', '0', '0', '0', None, None])
        self.assertEqual(json.loads(through[4][2])['error'], 'Rate limit exceeded')
        self.assertGreaterEqual(self.served()[1] - before[1], 3, 'the data plane refused too few of them itself')

    def test_by_ipv6_address_too(self):
        answers = self.compare([('rl_item', 2)] * 6, None, through_client='2001:db8::1', direct_client='2001:db8::2')
        self.assertEqual(statuses(answers), [200, 200, 200, 200, 429, 429])
        self.assertEqual(header(answers[4][0], 'Retry-After'), '15')


class WithoutRedisTests(LimitedTestCase):
    redis = False

    def test_every_limited_call_is_python_s(self):
        limited, plain = self.key('limited', '100/minute'), self.key('plain')
        for n in range(3):  # each key's use recorded, a plan for the plain one
            for secret in (limited, plain):
                self.assertEqual(self.call(self.pair.port, secret, THROUGH, 'rl_item', 1, f'warm-{n}')[0], 200)
        before = self.served()
        answers = [self.call(self.pair.port, limited, THROUGH, 'rl_item', 1, f'limited-{n}') for n in range(4)]
        self.assertEqual(self.served(), before)
        self.assertEqual([header(a, 'X-RateLimit-Key-Remaining') for a in answers], ['96', '95', '94', '93'])
        self.call(self.pair.port, plain, THROUGH, 'rl_item', 1, 'plain')
        self.assertEqual(self.served()[0], before[0] + 1)  # the plain key's call is still the data plane's


del LimitedTestCase  # runs only through its subclasses

if __name__ == '__main__':
    unittest.main()

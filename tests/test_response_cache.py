"""Tests for the opt-in, per-saved-query response cache."""
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

from queryapigate import cache, config, create_app
from queryapigate.rediscache import RedisResponseCache


class ResponseCacheTests(unittest.TestCase):
    """Unit tests for cache.ResponseCache, with a fake clock so TTL logic doesn't need real time.sleep()."""

    def setUp(self):
        self.now = 1000.0
        self.cache = cache.ResponseCache(clock=lambda: self.now)

    def test_set_then_get_round_trips(self):
        key = cache.ResponseCache.key(name='q', version=1, values={'id': 1})
        etag = self.cache.set(key, b'hello', 'text/plain', [('X-Page', '1')], ttl=10)
        body, content_type, headers, got_etag = self.cache.get(key)
        self.assertEqual(body, b'hello')
        self.assertEqual(content_type, 'text/plain')
        self.assertEqual(headers, [('X-Page', '1')])
        self.assertEqual(got_etag, etag)

    def test_etag_is_a_hash_of_the_body(self):
        key = cache.ResponseCache.key(name='q')
        etag = self.cache.set(key, b'hello', 'text/plain', [], ttl=10)
        import hashlib
        self.assertEqual(etag, hashlib.sha256(b'hello').hexdigest())

    def test_missing_key_is_none(self):
        self.assertIsNone(self.cache.get('nope'))

    def test_entry_expires_after_its_ttl(self):
        key = cache.ResponseCache.key(name='q')
        self.cache.set(key, b'hello', 'text/plain', [], ttl=10)
        self.now += 9.999
        self.assertIsNotNone(self.cache.get(key))
        self.now += 0.002
        self.assertIsNone(self.cache.get(key))

    def test_key_is_order_independent(self):
        a = cache.ResponseCache.key(name='q', values={'a': 1, 'b': 2})
        b = cache.ResponseCache.key(values={'b': 2, 'a': 1}, name='q')
        self.assertEqual(a, b)

    def test_key_distinguishes_different_values(self):
        a = cache.ResponseCache.key(name='q', values={'id': 1})
        b = cache.ResponseCache.key(name='q', values={'id': 2})
        self.assertNotEqual(a, b)

    def test_least_recently_used_entry_is_evicted_first(self):
        with mock.patch.object(cache, 'MAX_ENTRIES', 2):
            self.cache.set('a', b'1', 'text/plain', [], ttl=100)
            self.cache.set('b', b'2', 'text/plain', [], ttl=100)
            self.cache.get('a')  # touch 'a' so 'b' becomes the least recently used
            self.cache.set('c', b'3', 'text/plain', [], ttl=100)
            self.assertIsNotNone(self.cache.get('a'))
            self.assertIsNone(self.cache.get('b'))
            self.assertIsNotNone(self.cache.get('c'))


class _FakeRedisPipeline:
    """Just enough of redis-py's pipeline object for RedisResponseCache.set() - queues ops, applies them
    (or raises, simulating a write failure) on execute()."""

    def __init__(self, client):
        self._client = client
        self._ops = []

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def hset(self, name, mapping):
        self._ops.append(('hset', name, mapping))

    def expire(self, name, ttl):
        self._ops.append(('expire', name, ttl))

    def execute(self):
        if self._client._fail == 'hset':
            raise self._client.RedisError('simulated write failure')
        for op, name, value in self._ops:
            if op == 'hset':
                self._client._apply_hset(name, value)
            else:
                self._client._ttls[name] = value
        self._ops = []


class _FakeRedis:
    """A minimal in-memory stand-in for the four redis-py calls RedisResponseCache actually makes - no real
    Redis server needed, the same spirit as the MagicMock cursors/connections runners.py's own DB driver
    tests use. `fail='hset'` or `fail='hgetall'` makes the matching call raise redis.RedisError instead."""

    def __init__(self, fail=None):
        self._data = {}  # name -> {field bytes: value bytes}
        self._ttls = {}  # name -> last ttl passed to expire()
        self._fail = fail
        import redis
        self.RedisError = redis.RedisError

    def _apply_hset(self, name, mapping):
        entry = self._data.setdefault(name, {})
        for field, value in mapping.items():
            key = field.encode() if isinstance(field, str) else field
            entry[key] = value if isinstance(value, bytes) else str(value).encode()

    def pipeline(self):
        return _FakeRedisPipeline(self)

    def hgetall(self, name):
        if self._fail == 'hgetall':
            raise self.RedisError('simulated read failure')
        return dict(self._data.get(name, {}))

    def scan_iter(self, match):
        prefix = match.rstrip('*')
        return iter([k for k in self._data if k.startswith(prefix)])


class RedisResponseCacheTests(unittest.TestCase):
    """RedisResponseCache against the fake client above - proves the get/set/expire/error-handling logic
    without a real Redis server, matching how runners.py's DB drivers are tested via mocked cursors."""

    def setUp(self):
        self.fake = _FakeRedis()
        self.cache = RedisResponseCache('redis://localhost:6379/0', client=self.fake)

    def test_set_then_get_round_trips(self):
        key = cache.ResponseCache.key(name='q', version=1, values={'id': 1})
        etag = self.cache.set(key, b'hello', 'text/plain', [['X-Page', '1']], ttl=10)
        body, content_type, headers, got_etag = self.cache.get(key)
        self.assertEqual(body, b'hello')
        self.assertEqual(content_type, 'text/plain')
        self.assertEqual(headers, [['X-Page', '1']])
        self.assertEqual(got_etag, etag)

    def test_etag_is_a_hash_of_the_body(self):
        key = cache.ResponseCache.key(name='q')
        etag = self.cache.set(key, b'hello', 'text/plain', [], ttl=10)
        self.assertEqual(etag, hashlib.sha256(b'hello').hexdigest())

    def test_missing_key_is_none(self):
        self.assertIsNone(self.cache.get('nope'))

    def test_expire_is_called_with_the_ttl(self):
        key = cache.ResponseCache.key(name='q')
        self.cache.set(key, b'hello', 'text/plain', [], ttl=42)
        self.assertEqual(self.fake._ttls['qag:cache:' + key], 42)

    def test_a_read_failure_is_treated_as_a_miss(self):
        failing = RedisResponseCache('redis://localhost:6379/0', client=_FakeRedis(fail='hgetall'))
        self.assertIsNone(failing.get('anything'))

    def test_a_write_failure_is_a_silent_noop_but_still_returns_an_etag(self):
        failing = RedisResponseCache('redis://localhost:6379/0', client=_FakeRedis(fail='hset'))
        etag = failing.set('k', b'hello', 'text/plain', [], ttl=10)
        self.assertEqual(etag, hashlib.sha256(b'hello').hexdigest())
        self.assertIsNone(failing.get('k'))  # the write never actually landed

    def test_size_counts_only_this_caches_entries(self):
        self.cache.set(cache.ResponseCache.key(name='a'), b'1', 'text/plain', [], ttl=10)
        self.cache.set(cache.ResponseCache.key(name='b'), b'2', 'text/plain', [], ttl=10)
        self.assertEqual(self.cache.size(), 2)


class RedisConfigTests(unittest.TestCase):
    """config.py's QUERYAPIGATE_REDIS_URL validation and redaction - independent of any real Redis server
    or of create_app(), same spirit as LegacySettingsTests in test_cli.py for the SQL2API_ startup check."""

    def setUp(self):
        clean = {name: value for name, value in os.environ.items() if not name.startswith('QUERYAPIGATE_')}
        patcher = mock.patch.dict(os.environ, clean, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_malformed_scheme_is_rejected_at_startup(self):
        os.environ['QUERYAPIGATE_REDIS_URL'] = 'http://localhost:6379'
        self.assertRaises(ValueError, config.check_settings)

    def test_a_real_redis_url_passes_validation(self):
        os.environ['QUERYAPIGATE_REDIS_URL'] = 'redis://localhost:6379/0'
        config.check_settings()  # must not raise

    def test_missing_redis_package_is_a_clear_startup_error(self):
        os.environ['QUERYAPIGATE_REDIS_URL'] = 'redis://localhost:6379/0'
        with mock.patch.dict(sys.modules, {'redis': None}):
            with self.assertRaises(ValueError) as ctx:
                config.check_settings()
        self.assertIn('queryapigate[redis]', str(ctx.exception))

    def test_redact_hides_the_password(self):
        redacted = config.redact_redis_url('redis://user:secret@localhost:6379/0')
        self.assertNotIn('secret', redacted)
        self.assertIn('user', redacted)
        self.assertIn('localhost:6379', redacted)

    def test_redact_is_a_no_op_without_a_password(self):
        url = 'redis://localhost:6379/0'
        self.assertEqual(config.redact_redis_url(url), url)


class AppTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        tmp = self.tmp.name
        self.db_path = os.path.join(tmp, 'test.db')
        conn = sqlite3.connect(self.db_path)
        conn.execute('CREATE TABLE t (id INTEGER, name TEXT)')
        conn.executemany('INSERT INTO t VALUES (?, ?)', [(1, 'a'), (2, 'b')])
        conn.commit()
        conn.close()
        with open(os.path.join(tmp, 'db_connections.json'), 'w') as f:
            json.dump({'connections': {'lite': {'db': 'sqlite', 'database': self.db_path, 'active': True}}}, f)
        patcher = mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': tmp})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop('QUERYAPIGATE_API_KEY', None)
        os.environ.pop('QUERYAPIGATE_ALLOW_WRITES', None)
        self.client = create_app().test_client()

    def save(self, filename='q', sql='SELECT * FROM t WHERE id = :id', cache_ttl=None, **extra):
        body = {'author': 'a', 'description': 'd', 'sql_query': sql, 'filename': filename,
                'connection_name': 'lite', **extra}
        if cache_ttl is not None:
            body['cache_ttl'] = cache_ttl
        res = self.client.patch('/save_sql_to_file', json=body)
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        return res


class CacheHeaderTests(AppTestCase):
    def test_first_call_is_a_miss_second_is_a_hit_with_identical_body(self):
        self.save(cache_ttl=60)
        first = self.client.get('/q/q?id=1')
        self.assertEqual(first.headers['X-Cache'], 'MISS')
        self.assertIn('ETag', first.headers)
        self.assertEqual(first.headers['Cache-Control'], 'max-age=60')
        second = self.client.get('/q/q?id=1')
        self.assertEqual(second.headers['X-Cache'], 'HIT')
        self.assertEqual(second.get_data(), first.get_data())
        self.assertEqual(second.headers['ETag'], first.headers['ETag'])

    def test_no_cache_ttl_means_no_caching_headers_at_all(self):
        self.save(cache_ttl=None)
        res = self.client.get('/q/q?id=1')
        self.assertNotIn('X-Cache', res.headers)
        self.assertNotIn('ETag', res.headers)

    def test_if_none_match_gets_a_304_with_no_body(self):
        self.save(cache_ttl=60)
        first = self.client.get('/q/q?id=1')
        etag = first.headers['ETag']
        second = self.client.get('/q/q?id=1', headers={'If-None-Match': etag})
        self.assertEqual(second.status_code, 304)
        self.assertEqual(second.get_data(), b'')
        self.assertEqual(second.headers['ETag'], etag)

    def test_different_parameter_values_are_different_cache_entries(self):
        self.save(cache_ttl=60)
        first = self.client.get('/q/q?id=1')
        second = self.client.get('/q/q?id=2')
        self.assertEqual(first.headers['X-Cache'], 'MISS')
        self.assertEqual(second.headers['X-Cache'], 'MISS')
        self.assertNotEqual(first.get_data(), second.get_data())

    def test_different_format_is_a_different_cache_entry(self):
        self.save(cache_ttl=60)
        self.client.get('/q/q?id=1')  # warm the json entry
        csv_res = self.client.get('/q/q?id=1&format=csv')
        self.assertEqual(csv_res.headers['X-Cache'], 'MISS')

    def test_post_and_get_share_the_same_cache_entry(self):
        # id must be declared so GET's query-string '1' and POST's JSON 1 resolve to the same typed value.
        self.save(cache_ttl=60, query_parameters={'id': 'int'})
        get_res = self.client.get('/q/q?id=1')
        self.assertEqual(get_res.headers['X-Cache'], 'MISS')
        post_res = self.client.post('/q/q', json={'params': {'id': 1}})
        self.assertEqual(post_res.headers['X-Cache'], 'HIT')
        self.assertEqual(post_res.get_data(), get_res.get_data())


class RedisBackedAppCacheTests(AppTestCase):
    """Swaps in a RedisResponseCache (backed by the fake client, no real Redis server) after create_app()
    and re-runs the core miss/hit/304 flow through real HTTP requests - proving the two backends are
    interchangeable from the outside, exactly as cache_lookup()/cache_store() in app.py assume."""

    def setUp(self):
        super().setUp()
        self.client.application.extensions['queryapigate_cache'] = RedisResponseCache(
            'redis://localhost:6379/0', client=_FakeRedis())

    def test_first_call_is_a_miss_second_is_a_hit_with_identical_body(self):
        self.save(cache_ttl=60)
        first = self.client.get('/q/q?id=1')
        self.assertEqual(first.headers['X-Cache'], 'MISS')
        second = self.client.get('/q/q?id=1')
        self.assertEqual(second.headers['X-Cache'], 'HIT')
        self.assertEqual(second.get_data(), first.get_data())
        self.assertEqual(second.headers['ETag'], first.headers['ETag'])

    def test_if_none_match_gets_a_304_with_no_body(self):
        self.save(cache_ttl=60)
        first = self.client.get('/q/q?id=1')
        etag = first.headers['ETag']
        second = self.client.get('/q/q?id=1', headers={'If-None-Match': etag})
        self.assertEqual(second.status_code, 304)
        self.assertEqual(second.get_data(), b'')


class ExecutionHistoryInteractionTests(AppTestCase):
    def history_count(self):
        files = self.client.get('/list_files').get_json()['files']
        matching = [f for f in files if f['filename'] == 'q']
        return len(matching[0]['versions'][0].get('execution_history', []))

    def test_a_cache_hit_is_not_recorded_in_execution_history(self):
        self.save(cache_ttl=60)
        self.client.get('/q/q?id=1')  # miss - recorded
        after_miss = self.history_count()
        self.client.get('/q/q?id=1')  # hit - not recorded
        self.client.get('/q/q?id=1')  # hit - not recorded
        self.assertEqual(self.history_count(), after_miss)

    def test_a_cache_miss_is_still_recorded_as_usual(self):
        self.save(cache_ttl=None)
        self.client.get('/q/q?id=1')
        self.client.get('/q/q?id=1')
        self.assertEqual(self.history_count(), 2)


class WriteSafetyTests(AppTestCase):
    """A saved query that writes must never be served from cache, no matter its cache_ttl."""

    def test_a_cached_write_still_runs_every_time(self):
        os.environ['QUERYAPIGATE_ALLOW_WRITES'] = '1'
        self.save(filename='ins', sql="INSERT INTO t VALUES (99, 'x')", cache_ttl=60)
        self.client.post('/q/ins')
        self.client.post('/q/ins')
        conn = sqlite3.connect(self.db_path)
        count = conn.execute('SELECT COUNT(*) FROM t WHERE id = 99').fetchone()[0]
        conn.close()
        self.assertEqual(count, 2)

    def test_a_write_response_never_carries_cache_headers(self):
        os.environ['QUERYAPIGATE_ALLOW_WRITES'] = '1'
        self.save(filename='ins2', sql="INSERT INTO t VALUES (100, 'y')", cache_ttl=60)
        res = self.client.post('/q/ins2')
        self.assertNotIn('X-Cache', res.headers)


class CacheTtlValidationTests(AppTestCase):
    def test_negative_is_rejected(self):
        res = self.client.patch('/save_sql_to_file', json={
            'author': 'a', 'description': 'd', 'filename': 'q', 'connection_name': 'lite',
            'sql_query': 'SELECT * FROM t WHERE id = :id', 'cache_ttl': -1})
        self.assertEqual(res.status_code, 400)

    def test_non_integer_is_rejected(self):
        for bad in ('60', 1.5, True):
            res = self.client.patch('/save_sql_to_file', json={
                'author': 'a', 'description': 'd', 'filename': 'q', 'connection_name': 'lite',
                'sql_query': 'SELECT * FROM t WHERE id = :id', 'cache_ttl': bad})
            self.assertEqual(res.status_code, 400, bad)

    def test_zero_is_accepted_and_means_no_caching(self):
        self.save(cache_ttl=0)
        res = self.client.get('/q/q?id=1')
        self.assertNotIn('X-Cache', res.headers)

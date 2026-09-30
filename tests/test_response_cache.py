"""Tests for the opt-in, per-saved-query response cache."""
import hashlib
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

from queryapigate import cache, config, create_app
from queryapigate.rediscache import RedisResponseCache
from tests.helpers import write_connections


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

    def test_list_entries_reports_metadata_size_and_ttl_remaining(self):
        self.cache.set('k', b'hello', 'application/json', [], ttl=30, meta={'name': 'q', 'version': 1})
        self.now += 10
        entries = self.cache.list_entries()
        self.assertEqual(len(entries), 1)
        e = entries[0]
        self.assertEqual(e['key'], 'k')
        self.assertEqual(e['meta'], {'name': 'q', 'version': 1})
        self.assertEqual(e['content_type'], 'application/json')
        self.assertEqual(e['size_bytes'], len(b'hello'))
        self.assertEqual(e['ttl_remaining_s'], 20.0)

    def test_list_entries_omits_expired_entries(self):
        self.cache.set('k', b'hello', 'text/plain', [], ttl=10)
        self.now += 10.001
        self.assertEqual(self.cache.list_entries(), [])

    def test_list_entries_defaults_meta_to_an_empty_dict_when_not_given(self):
        self.cache.set('k', b'hello', 'text/plain', [], ttl=10)
        self.assertEqual(self.cache.list_entries()[0]['meta'], {})

    def test_get_body_returns_body_and_content_type_only(self):
        self.cache.set('k', b'hello', 'text/plain', [('X-Page', '1')], ttl=10)
        self.assertEqual(self.cache.get_body('k'), (b'hello', 'text/plain'))

    def test_get_body_is_none_for_a_missing_key(self):
        self.assertIsNone(self.cache.get_body('nope'))

    def test_delete_removes_one_entry_and_is_a_noop_for_a_missing_one(self):
        self.cache.set('a', b'1', 'text/plain', [], ttl=10)
        self.cache.set('b', b'2', 'text/plain', [], ttl=10)
        self.cache.delete('a')
        self.cache.delete('does-not-exist')  # must not raise
        self.assertIsNone(self.cache.get('a'))
        self.assertIsNotNone(self.cache.get('b'))

    def test_clear_removes_every_entry(self):
        self.cache.set('a', b'1', 'text/plain', [], ttl=10)
        self.cache.set('b', b'2', 'text/plain', [], ttl=10)
        self.cache.clear()
        self.assertEqual(self.cache.size(), 0)
        self.assertEqual(self.cache.list_entries(), [])


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
                self._client._ttls[_enc(name)] = value
        self._ops = []


def _enc(x):
    return x.encode() if isinstance(x, str) else x


class _FakeRedis:
    """A minimal in-memory stand-in for the redis-py calls RedisResponseCache actually makes - no real
    Redis server needed, the same spirit as the MagicMock cursors/connections runners.py's own DB driver
    tests use. Keys/fields are always stored and returned as bytes, matching a real client constructed with
    decode_responses=False. `fail='hset'` (etc.) makes the matching call raise redis.RedisError instead."""

    def __init__(self, fail=None):
        self._data = {}  # name (bytes) -> {field bytes: value bytes}
        self._ttls = {}  # name (bytes) -> last ttl passed to expire()
        self._fail = fail
        import redis
        self.RedisError = redis.RedisError

    def _apply_hset(self, name, mapping):
        entry = self._data.setdefault(_enc(name), {})
        for field, value in mapping.items():
            entry[_enc(field)] = value if isinstance(value, bytes) else str(value).encode()

    def pipeline(self):
        return _FakeRedisPipeline(self)

    def hgetall(self, name):
        if self._fail == 'hgetall':
            raise self.RedisError('simulated read failure')
        return dict(self._data.get(_enc(name), {}))

    def hmget(self, name, *fields):
        if self._fail == 'hmget':
            raise self.RedisError('simulated read failure')
        entry = self._data.get(_enc(name), {})
        return [entry.get(_enc(f)) for f in fields]

    def ttl(self, name):
        if self._fail == 'ttl':
            raise self.RedisError('simulated read failure')
        return self._ttls.get(_enc(name), -2)  # -2: redis-py's own "key does not exist" convention

    def delete(self, *names):
        if self._fail == 'delete':
            raise self.RedisError('simulated write failure')
        for name in names:
            self._data.pop(_enc(name), None)
            self._ttls.pop(_enc(name), None)

    def scan_iter(self, match):
        prefix = _enc(match.rstrip('*'))
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
        self.assertEqual(self.fake._ttls[('qag:cache:' + key).encode()], 42)

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

    def test_list_entries_reports_metadata_size_and_ttl(self):
        key = cache.ResponseCache.key(name='q')
        self.cache.set(key, b'hello', 'application/json', [], ttl=30, meta={'name': 'q', 'version': 1})
        entries = self.cache.list_entries()
        self.assertEqual(len(entries), 1)
        e = entries[0]
        self.assertEqual(e['key'], key)  # the bare key, qag:cache: prefix stripped
        self.assertEqual(e['meta'], {'name': 'q', 'version': 1})
        self.assertEqual(e['content_type'], 'application/json')
        self.assertEqual(e['size_bytes'], len(b'hello'))
        self.assertEqual(e['ttl_remaining_s'], 30.0)

    def test_list_entries_defaults_meta_to_an_empty_dict_when_not_given(self):
        key = cache.ResponseCache.key(name='q')
        self.cache.set(key, b'hello', 'text/plain', [], ttl=10)
        self.assertEqual(self.cache.list_entries()[0]['meta'], {})

    def test_list_entries_is_empty_on_a_read_failure(self):
        failing = RedisResponseCache('redis://localhost:6379/0', client=_FakeRedis(fail='hmget'))
        key = cache.ResponseCache.key(name='q')
        # written via the healthy fake, then read back through a client that fails hmget - proves
        # list_entries() degrades to empty rather than raising, same failure posture as get()/size().
        self.cache.set(key, b'hello', 'text/plain', [], ttl=10)
        failing._client._data = self.fake._data
        failing._client._ttls = self.fake._ttls
        self.assertEqual(failing.list_entries(), [])

    def test_get_body_returns_body_and_content_type_only(self):
        key = cache.ResponseCache.key(name='q')
        self.cache.set(key, b'hello', 'text/plain', [], ttl=10)
        self.assertEqual(self.cache.get_body(key), (b'hello', 'text/plain'))

    def test_get_body_is_none_for_a_missing_key(self):
        self.assertIsNone(self.cache.get_body('nope'))

    def test_delete_removes_one_entry_and_is_a_noop_for_a_missing_one(self):
        a, b = cache.ResponseCache.key(name='a'), cache.ResponseCache.key(name='b')
        self.cache.set(a, b'1', 'text/plain', [], ttl=10)
        self.cache.set(b, b'2', 'text/plain', [], ttl=10)
        self.cache.delete(a)
        self.cache.delete('does-not-exist')  # must not raise
        self.assertIsNone(self.cache.get(a))
        self.assertIsNotNone(self.cache.get(b))

    def test_clear_removes_every_entry(self):
        self.cache.set(cache.ResponseCache.key(name='a'), b'1', 'text/plain', [], ttl=10)
        self.cache.set(cache.ResponseCache.key(name='b'), b'2', 'text/plain', [], ttl=10)
        self.cache.clear()
        self.assertEqual(self.cache.size(), 0)
        self.assertEqual(self.cache.list_entries(), [])


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
        patcher = mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': tmp})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop('QUERYAPIGATE_API_KEY', None)
        os.environ.pop('QUERYAPIGATE_ALLOW_WRITES', None)
        write_connections({'lite': {'db': 'sqlite', 'database': self.db_path, 'active': True}})
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


class CacheEntriesEndpointTests(AppTestCase):
    """GET/DELETE /cache/entries and GET/DELETE /cache/entries/<key> - the admin UI's Caching-screen cache
    browser (BACKLOG: real-time cache entries), reusing the same result-rendering component API Designer's
    own Run tab does for the body preview."""

    def test_lists_a_live_entry_with_its_metadata(self):
        self.save(cache_ttl=60)
        self.client.get('/q/q?id=1')  # a miss, so one entry now exists
        res = self.client.get('/cache/entries')
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        entries = res.get_json()['entries']
        self.assertEqual(len(entries), 1)
        e = entries[0]
        self.assertEqual(e['meta']['name'], 'q')
        self.assertEqual(e['meta']['version'], 1)
        self.assertEqual(e['meta']['connection'], 'lite')
        self.assertEqual(e['meta']['format'], 'json')
        self.assertEqual(e['content_type'], 'application/json')
        self.assertGreater(e['size_bytes'], 0)
        self.assertGreater(e['ttl_remaining_s'], 0)

    def test_empty_when_nothing_is_cached(self):
        res = self.client.get('/cache/entries')
        self.assertEqual(res.get_json()['entries'], [])

    def test_entry_body_is_served_with_its_real_content_type(self):
        self.save(cache_ttl=60)
        live = self.client.get('/q/q?id=1')
        key = self.client.get('/cache/entries').get_json()['entries'][0]['key']
        res = self.client.get(f'/cache/entries/{key}')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.content_type, live.content_type)
        self.assertEqual(res.get_data(), live.get_data())

    def test_entry_body_404s_for_an_unknown_key(self):
        res = self.client.get('/cache/entries/does-not-exist')
        self.assertEqual(res.status_code, 404)

    def test_delete_one_entry_removes_it(self):
        self.save(cache_ttl=60)
        self.client.get('/q/q?id=1')
        key = self.client.get('/cache/entries').get_json()['entries'][0]['key']
        res = self.client.delete(f'/cache/entries/{key}')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.client.get('/cache/entries').get_json()['entries'], [])
        # a deleted entry is a clean miss, not an error, on the next real call
        self.assertEqual(self.client.get('/q/q?id=1').headers['X-Cache'], 'MISS')

    def test_delete_one_is_a_noop_for_an_unknown_key(self):
        res = self.client.delete('/cache/entries/does-not-exist')
        self.assertEqual(res.status_code, 200)

    def test_clear_removes_every_entry(self):
        self.save(filename='q1', sql='SELECT * FROM t WHERE id = :id', cache_ttl=60)
        self.save(filename='q2', sql='SELECT * FROM t WHERE id = :id', cache_ttl=60)
        self.client.get('/q/q1?id=1')
        self.client.get('/q/q2?id=1')
        self.assertEqual(len(self.client.get('/cache/entries').get_json()['entries']), 2)
        res = self.client.delete('/cache/entries')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.client.get('/cache/entries').get_json()['entries'], [])

    def test_every_cache_route_is_admin_only(self):
        self.save(cache_ttl=60)
        self.client.get('/q/q?id=1')
        key = self.client.get('/cache/entries').get_json()['entries'][0]['key']
        os.environ['QUERYAPIGATE_API_KEY'] = 'admin-key'
        client = create_app().test_client()
        scoped = client.post('/api_keys', json={'name': 'scoped', 'connections': []},
                             headers={'X-API-Key': 'admin-key'}).get_json()['key']
        headers = {'X-API-Key': scoped}
        self.assertEqual(client.get('/cache/entries', headers=headers).status_code, 403)
        self.assertEqual(client.get(f'/cache/entries/{key}', headers=headers).status_code, 403)
        self.assertEqual(client.delete(f'/cache/entries/{key}', headers=headers).status_code, 403)
        self.assertEqual(client.delete('/cache/entries', headers=headers).status_code, 403)


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


class SetCacheTtlEndpointTests(AppTestCase):
    """PUT /saved_sql/<name>/cache_ttl - editing a version's cache_ttl in place, without a new version."""

    def test_sets_a_ttl_on_the_latest_version_without_a_new_version(self):
        self.save(cache_ttl=None)
        res = self.client.put('/saved_sql/q/cache_ttl', json={'cache_ttl': 90})
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        body = res.get_json()
        self.assertEqual(body['version'], 1)
        self.assertEqual(body['cache_ttl'], 90)
        files = self.client.get('/list_files').get_json()['files']
        v = [f for f in files if f['filename'] == 'q'][0]['versions']
        self.assertEqual(len(v), 1)  # still one version - not a new one
        self.assertEqual(v[0]['cache_ttl'], 90)

    def test_takes_effect_immediately_on_the_next_run(self):
        self.save(cache_ttl=None)
        first = self.client.get('/q/q?id=1')
        self.assertNotIn('X-Cache', first.headers)
        self.client.put('/saved_sql/q/cache_ttl', json={'cache_ttl': 60})
        second = self.client.get('/q/q?id=1')
        self.assertEqual(second.headers['X-Cache'], 'MISS')
        third = self.client.get('/q/q?id=1')
        self.assertEqual(third.headers['X-Cache'], 'HIT')

    def test_zero_clears_it_and_stops_caching(self):
        self.save(cache_ttl=60)
        self.client.get('/q/q?id=1')
        res = self.client.put('/saved_sql/q/cache_ttl', json={'cache_ttl': 0})
        self.assertEqual(res.status_code, 200)
        self.assertIsNone(res.get_json()['cache_ttl'])
        after = self.client.get('/q/q?id=1')
        self.assertNotIn('X-Cache', after.headers)

    def test_null_also_clears_it(self):
        self.save(cache_ttl=60)
        res = self.client.put('/saved_sql/q/cache_ttl', json={'cache_ttl': None})
        self.assertEqual(res.status_code, 200)
        self.assertIsNone(res.get_json()['cache_ttl'])

    def test_targets_a_specific_older_version(self):
        self.save(cache_ttl=None)
        self.save(cache_ttl=None)  # v2, now the latest
        res = self.client.put('/saved_sql/q/cache_ttl?version=1', json={'cache_ttl': 45})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()['version'], 1)
        files = self.client.get('/list_files').get_json()['files']
        v = [f for f in files if f['filename'] == 'q'][0]['versions']
        self.assertEqual(v[0]['cache_ttl'], 45)
        self.assertIsNone(v[1].get('cache_ttl'))

    def test_a_negative_value_is_rejected(self):
        self.save(cache_ttl=None)
        res = self.client.put('/saved_sql/q/cache_ttl', json={'cache_ttl': -1})
        self.assertEqual(res.status_code, 400)

    def test_a_non_integer_value_is_rejected(self):
        self.save(cache_ttl=None)
        res = self.client.put('/saved_sql/q/cache_ttl', json={'cache_ttl': '60'})
        self.assertEqual(res.status_code, 400)

    def test_missing_cache_ttl_field_is_rejected(self):
        self.save(cache_ttl=None)
        res = self.client.put('/saved_sql/q/cache_ttl', json={})
        self.assertEqual(res.status_code, 400)

    def test_a_nonexistent_version_is_404(self):
        self.save(cache_ttl=None)
        res = self.client.put('/saved_sql/q/cache_ttl?version=99', json={'cache_ttl': 30})
        self.assertEqual(res.status_code, 404)

    def test_only_admin_may_edit_it(self):
        self.save(cache_ttl=None)
        os.environ['QUERYAPIGATE_API_KEY'] = 'admin-key'
        client = create_app().test_client()
        scoped = client.post('/api_keys', json={'name': 'scoped', 'connections': []},
                             headers={'X-API-Key': 'admin-key'}).get_json()['key']
        res = client.put('/saved_sql/q/cache_ttl', json={'cache_ttl': 30}, headers={'X-API-Key': scoped})
        self.assertEqual(res.status_code, 403)

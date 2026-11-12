"""The response cache kept fresh by live queries' signals (ADR 0033 §7): a write through QueryAPIGate, or a change
Debezium reports, makes the cached responses it concerns a miss - so a long cache_ttl never serves a stale result.
Both backends: in-process (cache.py) and Redis (rediscache.py - a fake client, and a real one when
QUERYAPIGATE_TEST_REDIS_URL is set)."""
import json
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from queryapigate import cache, create_app, live
from queryapigate.rediscache import RedisResponseCache
from tests.helpers import save_query, write_connections
from tests.test_live import SECRET, signed

REDIS_URL = os.environ.get('QUERYAPIGATE_TEST_REDIS_URL')


class _Pipeline:
    """redis-py's pipeline, as far as RedisResponseCache uses it: queued calls, their results on execute()."""

    def __init__(self, client):
        self.client, self.calls = client, []

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def __getattr__(self, name):
        return lambda *args, **kwargs: self.calls.append((name, args, kwargs))

    def execute(self):
        results = [getattr(self.client, name)(*args, **kwargs) for name, args, kwargs in self.calls]
        self.calls = []
        return results


class _Connection:
    """A pooled connection, as far as RedisResponseCache uses one: packed commands sent, then their replies read in
    order - HGETALL's as RESP2 sends it, a flat list of fields and values."""

    def __init__(self, client):
        self.client, self.replies, self.disconnected = client, [], False

    def pack_commands(self, commands):
        return list(commands)

    def send_packed_command(self, commands):
        for name, *args in commands:
            reply = getattr(self.client, name.lower())(*args)
            self.replies.append([x for pair in reply.items() for x in pair] if name == 'HGETALL' else reply)

    def read_response(self):
        return self.replies.pop(0)

    def disconnect(self):
        self.disconnected = True


class _Pool:
    def __init__(self, client):
        self.client, self.released = client, []

    def get_connection(self):
        return _Connection(self.client)

    def release(self, conn):
        self.released.append(conn)


class FakeRedis:
    """An in-memory Redis for the hash calls the cache makes, bytes in and out (decode_responses=False)."""

    def __init__(self):
        import redis
        self.RedisError = redis.RedisError
        self.hashes, self.ttls = {}, {}
        self.connection_pool = _Pool(self)

    @staticmethod
    def _bytes(value):
        return value if isinstance(value, bytes) else str(value).encode()

    def pipeline(self, transaction=True):
        return _Pipeline(self)

    def hset(self, name, mapping):
        self.hashes.setdefault(self._bytes(name), {}).update(
            {self._bytes(k): self._bytes(v) for k, v in mapping.items()})

    def expire(self, name, ttl):
        self.ttls[self._bytes(name)] = ttl

    def hgetall(self, name):
        return dict(self.hashes.get(self._bytes(name), {}))

    def hmget(self, name, *fields):
        found = self.hashes.get(self._bytes(name), {})
        return [found.get(self._bytes(field)) for field in fields]

    def hincrby(self, name, field, amount):
        found = self.hashes.setdefault(self._bytes(name), {})
        found[self._bytes(field)] = self._bytes(int(found.get(self._bytes(field), b'0')) + amount)
        return int(found[self._bytes(field)])


FILM = live.cache_fields('default', 'shop', {'main.film'})
RENTAL_SIGNAL = live.signal_fields('default', 'shop', {'main.rental'})
FILM_SIGNAL = live.signal_fields('default', 'shop', {'main.film'})


class ResponseFieldsTests(unittest.TestCase):
    """live.response_fields(): cache_fields() of the tables a saved query's SQL reads, memoized - the same answer."""

    def test_the_same_names_as_built_each_time(self):
        details = {'db': 'sqlite', 'database': '/x.db'}
        for sql in ('SELECT id FROM film WHERE id = :id', 'SELECT * FROM film JOIN rental USING (id)',
                    'SELECT * FROM {table}', 'SELECT 1'):
            with self.subTest(sql=sql):
                built = live.cache_fields('default', 'shop', live.tables_read(sql, details))
                self.assertEqual(live.response_fields('default', 'shop', sql, details), built)
                self.assertEqual(live.response_fields('default', 'shop', sql, details), built)  # from the memo

    def test_workspaces_and_connections_never_share_names(self):
        details, sql = {'db': 'sqlite'}, 'SELECT id FROM film'
        names = {live.response_fields(w, c, sql, details) for w in ('default', 'acme') for c in ('shop', 'other')}
        self.assertEqual(len(names), 4)


class BackendContract:
    """What each backend must do with the counters - run by a subclass per backend (make())."""

    def make(self):
        raise NotImplementedError

    def setUp(self):
        self.cache = self.make()

    def store(self, key='k', fields=FILM):
        hit, generations = self.cache.lookup(key, fields)
        self.assertIsNone(hit)
        self.cache.set(key, b'body', 'application/json', [], 300, generations=generations)

    def test_a_hit_until_a_signal_on_its_table(self):
        self.store()
        self.assertIsNotNone(self.cache.lookup('k', FILM)[0])
        self.cache.bump(RENTAL_SIGNAL)  # another table
        self.assertIsNotNone(self.cache.lookup('k', FILM)[0])
        self.cache.bump(FILM_SIGNAL)
        self.assertIsNone(self.cache.lookup('k', FILM)[0])

    def test_a_signal_naming_no_tables_reaches_every_entry_on_its_connection(self):
        self.store()
        self.cache.bump(live.signal_fields('default', 'shop', None))
        self.assertIsNone(self.cache.lookup('k', FILM)[0])

    def test_an_entry_whose_tables_cant_be_told_misses_after_any_signal_on_its_connection(self):
        unknown = live.cache_fields('default', 'shop', None)
        self.store(fields=unknown)
        self.cache.bump(live.signal_fields('default', 'other', {'main.film'}))  # another connection
        self.assertIsNotNone(self.cache.lookup('k', unknown)[0])
        self.cache.bump(RENTAL_SIGNAL)
        self.assertIsNone(self.cache.lookup('k', unknown)[0])

    def test_a_change_during_the_query_leaves_its_result_out_of_date(self):
        _hit, before = self.cache.lookup('k', FILM)  # read before the query runs...
        self.cache.bump(FILM_SIGNAL)                  # ...the data changes while it runs...
        self.cache.set('k', b'old', 'application/json', [], 300, generations=before)
        self.assertIsNone(self.cache.lookup('k', FILM)[0])  # ...so what it read is never served

    def test_another_workspace_s_signal_leaves_it_alone(self):
        self.store()
        self.cache.bump(live.signal_fields('acme', 'shop', {'main.film'}))
        self.assertIsNotNone(self.cache.lookup('k', FILM)[0])

    def test_without_fields_counters_are_ignored(self):  # live queries off: the hit path as before
        self.cache.set('k', b'body', 'application/json', [], 300)
        self.cache.bump(FILM_SIGNAL)
        self.assertIsNotNone(self.cache.get('k'))


class InProcessCounterTests(BackendContract, unittest.TestCase):
    def make(self):
        return cache.ResponseCache()


class FakeRedisCounterTests(BackendContract, unittest.TestCase):
    def make(self):
        return RedisResponseCache('redis://unused', client=FakeRedis())

    def test_redis_down_is_a_miss_and_stores_nothing(self):
        backend = self.make()
        with mock.patch.object(FakeRedis, 'hgetall', side_effect=backend._redis_error('down')):
            self.assertEqual(backend.lookup('k', FILM), (None, None))

    def test_a_connection_that_fails_part_way_is_dropped_and_still_returned_to_the_pool(self):
        pool = self.cache._client.connection_pool
        with mock.patch.object(_Connection, 'read_response', side_effect=self.cache._redis_error('mid-read')):
            self.assertEqual(self.cache.lookup('k', FILM), (None, None))
        self.assertTrue(pool.released[-1].disconnected)
        self.store()
        self.assertIsNotNone(self.cache.lookup('k', FILM)[0])
        self.assertFalse(pool.released[-1].disconnected)


@unittest.skipUnless(REDIS_URL, 'set QUERYAPIGATE_TEST_REDIS_URL')
class RedisCounterTests(BackendContract, unittest.TestCase):
    def make(self):
        import redis
        client = redis.Redis.from_url(REDIS_URL)
        client.delete('qag:cache-generations')
        self.addCleanup(client.delete, 'qag:cache-generations')
        backend = RedisResponseCache(REDIS_URL)
        self.addCleanup(backend.clear)
        return backend

    def test_an_error_reply_drops_the_connection_and_the_next_lookup_is_clean(self):
        import redis
        self.store()
        redis.Redis.from_url(REDIS_URL).set('qag:cache-generations', 'not a hash')  # HMGET answers WRONGTYPE
        self.assertEqual(self.cache.lookup('k', FILM), (None, None))
        redis.Redis.from_url(REDIS_URL).delete('qag:cache-generations')
        self.store('k2')  # the reply left unread didn't stay on a pooled connection to answer this one
        self.assertIsNotNone(self.cache.lookup('k2', FILM)[0])
        self.assertIsNone(self.cache.lookup('nope', FILM)[0])


class AppTests(unittest.TestCase):
    """Through real requests: GET /q/ with a cache_ttl, kept fresh by writes and by Debezium's signed posts."""
    live = '1'

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.shop = os.path.join(self.tmp.name, 'shop.db')
        conn = sqlite3.connect(self.shop)
        conn.executescript('CREATE TABLE film (id INTEGER PRIMARY KEY, title TEXT);'
                           "INSERT INTO film VALUES (1, 'Alpha'), (2, 'Beta');"
                           'CREATE TABLE rental (id INTEGER PRIMARY KEY, film_id INTEGER);')
        conn.close()
        sources = {'shop': {'connection': 'shop', 'secret': SECRET, 'server_name': 'shop'}}
        patcher = mock.patch.dict(os.environ, {
            'QUERYAPIGATE_HOME': self.tmp.name, 'QUERYAPIGATE_API_KEY': 'admin', 'QUERYAPIGATE_ALLOW_WRITES': '1',
            'QUERYAPIGATE_LIVE_QUERIES': self.live, 'QUERYAPIGATE_CDC_SOURCES': json.dumps(sources),
            'QUERYAPIGATE_RATE_LIMIT': ''})
        patcher.start()
        self.addCleanup(patcher.stop)
        write_connections({'shop': {'db': 'sqlite', 'database': self.shop, 'active': True}})
        self.client = create_app().test_client()
        self.admin = {'X-API-Key': 'admin'}
        self.use_backend()
        res = save_query(self.client, headers=self.admin, body={
            'filename': 'film', 'description': 'd', 'connection_name': 'shop', 'cache_ttl': 3600,
            'sql_query': 'SELECT id, title FROM film WHERE id = :id', 'query_parameters': {'id': {'type': 'int'}}})
        self.assertEqual(res.status_code, 201, res.get_data(as_text=True))

    def use_backend(self):
        pass  # in-process: what create_app() made

    def get(self):
        res = self.client.get('/q/film?id=1', headers=self.admin)
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        return res.headers['X-Cache'], res.get_json()[0]['title']

    def write(self, sql):
        res = self.client.post('/execute_sql', headers=self.admin, json={'connection_name': 'shop', 'sql': sql})
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))

    def debezium(self, table):
        body = json.dumps([{'before': None, 'after': {'id': 1}, 'op': 'u', 'ts_ms': 1,
                            'source': {'name': 'shop', 'db': 'shop', 'schema': 'main', 'table': table}}]).encode()
        res = self.client.post('/ingest/cdc/shop', data=body, headers=signed(body))
        self.assertEqual(res.status_code, 204, res.get_data(as_text=True))

    def change_behind_queryapigate(self, title):
        conn = sqlite3.connect(self.shop)
        conn.execute('UPDATE film SET title = ? WHERE id = 1', (title,))
        conn.commit()
        conn.close()

    def test_a_write_through_queryapigate_makes_the_next_call_a_miss_with_the_new_row(self):
        self.assertEqual(self.get(), ('MISS', 'Alpha'))
        self.assertEqual(self.get(), ('HIT', 'Alpha'))
        self.write("UPDATE film SET title = 'Alpha II' WHERE id = 1")
        self.assertEqual(self.get(), ('MISS', 'Alpha II'))
        self.assertEqual(self.get(), ('HIT', 'Alpha II'))

    def test_a_write_to_another_table_keeps_the_hit(self):
        self.get()
        self.write('INSERT INTO rental VALUES (1, 1)')
        self.assertEqual(self.get(), ('HIT', 'Alpha'))

    def test_a_change_debezium_reports_makes_it_a_miss(self):
        self.get()
        self.change_behind_queryapigate('Alpha (CDC)')
        self.assertEqual(self.get(), ('HIT', 'Alpha'))  # nothing told QueryAPIGate yet: the cached one
        self.debezium('rental')
        self.assertEqual(self.get(), ('HIT', 'Alpha'))
        self.debezium('film')
        self.assertEqual(self.get(), ('MISS', 'Alpha (CDC)'))


class FakeRedisAppTests(AppTests):
    def use_backend(self):
        self.client.application.extensions['queryapigate_cache'] = RedisResponseCache(
            'redis://unused', client=FakeRedis())


@unittest.skipUnless(REDIS_URL, 'set QUERYAPIGATE_TEST_REDIS_URL')
class RedisAppTests(AppTests):
    def use_backend(self):
        backend = RedisResponseCache(REDIS_URL)
        self.addCleanup(backend.clear)
        self.client.application.extensions['queryapigate_cache'] = backend
        import redis
        client = redis.Redis.from_url(REDIS_URL)
        client.delete('qag:cache-generations')  # one hash for every instance: each test starts it afresh
        self.addCleanup(client.delete, 'qag:cache-generations')


class LiveQueriesOffTests(AppTests):
    """With live queries off nothing signals: the cache works exactly as before, a write and all."""
    live = ''

    def test_a_write_through_queryapigate_makes_the_next_call_a_miss_with_the_new_row(self):
        self.get()
        self.write("UPDATE film SET title = 'Alpha II' WHERE id = 1")
        self.assertEqual(self.get(), ('HIT', 'Alpha'))

    def test_a_change_debezium_reports_makes_it_a_miss(self):
        self.skipTest('ingest answers 404 with live queries off')


if __name__ == '__main__':
    unittest.main()

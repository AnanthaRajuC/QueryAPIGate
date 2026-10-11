"""A caller's own limits (ADR 0011, BACKLOG #94): max_concurrent, query_timeout and max_rows on keys and roles - each
narrowing the server's, never widening it - applied on every front door."""
import os
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock

import redis

from queryapigate import apikeys, create_app, limits, mcp_server, runners
from tests.helpers import create_key, save_query, secret_of, write_connections

ADMIN = {'X-API-Key': 'admin-key'}
REDIS_URL = os.environ.get('QUERYAPIGATE_TEST_REDIS_URL')


class LimitsTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        data = os.path.join(self.tmp.name, 'data.db')
        conn = sqlite3.connect(data)
        conn.execute('CREATE TABLE t (id INTEGER)')
        conn.executemany('INSERT INTO t VALUES (?)', [(i,) for i in range(50)])
        conn.commit()
        conn.close()
        patcher = mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': self.tmp.name, 'QUERYAPIGATE_API_KEY': 'admin-key',
                                               'QUERYAPIGATE_REDIS_URL': REDIS_URL or '',
                                               'QUERYAPIGATE_STREAM_MAX_ROWS': ''})
        patcher.start()
        self.addCleanup(patcher.stop)
        limits._counters.clear()
        self.addCleanup(limits._counters.clear)
        write_connections({'lite': {'db': 'sqlite', 'database': data, 'active': True}})
        self.app = create_app()
        self.client = self.app.test_client()
        res = save_query(self.client, {'author': 'a', 'description': 'd', 'filename': 'ids', 'connection_name': 'lite',
                                       'sql_query': 'SELECT id FROM t ORDER BY id'}, headers=ADMIN)
        self.assertEqual(res.status_code, 201, res.get_data(as_text=True))

    def key(self, name='partner', **fields):
        return secret_of(create_key(self.client, ADMIN, name=name, queries=['ids'], connections=['lite'], **fields))

    def get(self, secret, path='/q/ids', **kwargs):
        return self.client.get(path, headers={'X-API-Key': secret}, **kwargs)


class GrantTests(LimitsTestCase):
    def test_set_shown_changed_and_cleared_on_a_key(self):
        self.key(max_concurrent=2, query_timeout=1.5, max_rows=20)
        key = self.client.get('/api/v1/api-keys/partner', headers=ADMIN).get_json()
        self.assertEqual((key['max_concurrent'], key['query_timeout'], key['max_rows']), (2, 1.5, 20))
        self.assertEqual(key['effective_limits'], {'max_concurrent': 2, 'query_timeout': 1.5,
                                                   'max_rows': {'page': 20, 'stream': 20, 'mcp': 20}})
        res = self.client.patch('/api/v1/api-keys/partner', headers=ADMIN, json={'max_rows': None, 'query_timeout': 90})
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        key = res.get_json()
        self.assertEqual((key['max_rows'], key['query_timeout']), (None, 90))
        # above the server's own limits: granted as asked, the server's applies
        self.assertEqual(key['effective_limits']['query_timeout'], 30.0)
        self.assertEqual(key['effective_limits']['max_rows'], {'page': 1000, 'stream': None, 'mcp': 200})
        audit = self.client.get('/api/v1/audit?target=partner', headers=ADMIN).get_json()['items']
        self.assertIn('max_rows', audit[0]['changes'])

    def test_bad_values_are_refused(self):
        for field, value in (('max_concurrent', 0), ('max_concurrent', 1001), ('max_concurrent', 1.5),
                             ('max_concurrent', True), ('max_rows', 'ten'), ('query_timeout', 0),
                             ('query_timeout', 86401)):
            res = create_key(self.client, ADMIN, name='bad', **{field: value})
            self.assertEqual((res.status_code, res.get_json()['code']), (400, 'invalid_body'), (field, value))

    def test_a_role_carries_them_to_the_keys_made_from_it(self):
        res = self.client.post('/api/v1/roles', headers=ADMIN, json={'name': 'partners', 'queries': ['ids'],
                                                                      'max_concurrent': 3, 'max_rows': 5})
        self.assertEqual(res.status_code, 201, res.get_data(as_text=True))
        secret = secret_of(create_key(self.client, ADMIN, name='from-role', role='partners'))
        self.assertEqual(len(self.get(secret).get_json()), 5)
        res = create_key(self.client, ADMIN, name='mixed', role='partners', max_rows=9)
        self.assertEqual(res.status_code, 400)  # grants come from the role, as for every other field


class RowsAndTimeTests(LimitsTestCase):
    def test_max_rows_cuts_a_page_a_stream_and_an_mcp_result(self):
        secret = self.key(max_rows=7)
        page = self.get(secret, '/q/ids?page_size=20')
        self.assertEqual((len(page.get_json()), page.headers['X-Page-Size'], page.headers['X-Has-More']),
                         (7, '7', 'true'))
        self.assertEqual(self.get(secret, '/q/ids?page=2&page_size=20').get_json()[0]['id'], 7)  # pages of 7
        stream = self.get(secret, '/q/ids?stream=true&format=ndjson').get_data(as_text=True).strip().splitlines()
        self.assertEqual(len(stream), 7)
        result = mcp_server.call_as(self.app, apikeys.authenticate(secret), 'ids', {})
        self.assertEqual(len(result['structuredContent']['rows']), 7)
        self.assertTrue(result['structuredContent']['truncated'])
        self.assertEqual(len(self.client.get('/q/ids?page_size=20', headers=ADMIN).get_json()), 20)  # not the admin

    def test_ad_hoc_sql_too(self):
        secret = self.key(max_rows=4)
        res = self.client.post('/execute_sql', headers={'X-API-Key': secret},
                               json={'connection_name': 'lite', 'sql': 'SELECT id FROM t', 'page_size': 30})
        self.assertEqual(len(res.get_json()), 4)

    def test_query_timeout_is_the_smallest_of_the_key_the_request_and_the_server(self):
        seen = []
        real = runners.RUNNERS['sqlite']

        def recording(details, sql, params, limit, offset, read_only, timeout, pool):
            seen.append(timeout)
            return real(details, sql, params, limit, offset, read_only, timeout, pool)

        tight, loose = self.key(query_timeout=2), self.key('loose', query_timeout=500)
        with mock.patch.dict(runners.RUNNERS, {'sqlite': recording}):
            self.get(tight)
            self.get(tight, '/q/ids?timeout=1')
            self.get(loose)
            self.client.get('/q/ids', headers=ADMIN)
        self.assertEqual(seen, [2, 1, 30.0, 30.0])


class ConcurrencyTests(LimitsTestCase):
    def test_a_stream_holds_its_slot_until_its_last_row_is_read(self):
        secret = self.key(max_concurrent=1)
        streaming = self.get(secret, '/q/ids?stream=true&format=ndjson', buffered=False)
        next(streaming.response)  # started: the query is running, its rows not all read
        refused = self.get(secret)
        self.assertEqual((refused.status_code, refused.get_json()['code'], refused.headers['Retry-After']),
                         (429, 'too_many_concurrent', '1'))
        self.assertEqual(refused.get_json()['max_concurrent'], 1)
        b''.join(streaming.response)  # the last row read
        streaming.close()
        self.assertEqual(self.get(secret).status_code, 200)

    def test_a_stream_closed_before_its_first_row_frees_its_slot(self):
        secret = self.key(max_concurrent=1)
        streaming = self.get(secret, '/q/ids?stream=true&format=ndjson', buffered=False)
        streaming.close()
        self.assertEqual(self.get(secret).status_code, 200)

    def test_running_queries_count_across_front_doors_and_each_caller_has_its_own(self):
        secret, other = self.key(max_concurrent=2), self.key('other', max_concurrent=1)
        started, finish = threading.Barrier(3), threading.Event()
        real = runners.RUNNERS['sqlite']

        def slow(*args):
            started.wait(5)
            finish.wait(5)
            return real(*args)

        results = []
        with mock.patch.dict(runners.RUNNERS, {'sqlite': slow}):
            threads = [threading.Thread(target=lambda: results.append(self.get(secret).status_code)),
                       threading.Thread(target=lambda: results.append(mcp_server.call_as(
                           self.app, apikeys.authenticate(secret), 'ids', {}).get('isError', False)))]
            for thread in threads:
                thread.start()
            started.wait(5)  # both running: one over REST, one over MCP
            refused = mcp_server.call_as(self.app, apikeys.authenticate(secret), 'ids', {})
            self.assertEqual(refused['structuredContent']['code'], 'too_many_concurrent')
            self.assertEqual(self.client.post('/execute_sql', headers={'X-API-Key': secret}, json={
                'connection_name': 'lite', 'sql': 'SELECT 1'}).status_code, 429)
            finish.set()
            for thread in threads:
                thread.join(10)
        self.assertEqual(sorted(results, key=str), sorted([200, False], key=str))
        self.assertEqual(self.get(other).status_code, 200)  # another caller's own allowance
        self.assertEqual(self.get(secret).status_code, 200)  # both slots back

    def test_a_failing_query_gives_its_slot_back(self):
        secret = self.key(max_concurrent=1)
        with mock.patch.dict(runners.RUNNERS, {'sqlite': mock.Mock(side_effect=RuntimeError('boom'))}):
            self.assertEqual(self.get(secret).status_code, 500)
        self.assertEqual(self.get(secret).status_code, 200)

    def test_the_admin_has_no_limit(self):
        self.assertIsNone(limits.caller())
        self.assertIs(limits.acquire(), limits.NO_SLOT)


class LeaseTests(unittest.TestCase):
    def test_a_slot_never_released_is_freed_when_its_lease_ends(self):
        now = [100.0]
        local = limits._Local(clock=lambda: now[0])
        self.assertTrue(local.acquire('p', 1, 'a', lease=10))
        self.assertFalse(local.acquire('p', 1, 'b', lease=10))
        now[0] += 11  # the holder died without releasing it
        self.assertTrue(local.acquire('p', 1, 'b', lease=10))
        local.renew('p', 'b', lease=10)
        now[0] += 9
        self.assertFalse(local.acquire('p', 1, 'c', lease=10))  # renewed: still held
        local.release('p', 'b')
        self.assertEqual(local.running('p'), 0)


class _FailingClient:
    def register_script(self, _script):
        def run(**_kwargs):
            raise redis.ConnectionError('Connection refused')
        return run

    def __getattr__(self, _name):
        def fail(*_args, **_kwargs):
            raise redis.ConnectionError('Connection refused')
        return fail


class RedisFallbackTests(unittest.TestCase):
    def test_redis_down_counts_in_this_process_instead(self):
        shared = limits._Shared('redis://unused', client=_FailingClient())
        self.assertTrue(shared.acquire('p', 1, 'a', 30))
        self.assertFalse(shared.acquire('p', 1, 'b', 30))  # still enforced, per process
        shared.release('p', 'a')
        self.assertTrue(shared.acquire('p', 1, 'c', 30))


@unittest.skipUnless(REDIS_URL, 'set QUERYAPIGATE_TEST_REDIS_URL to run against Redis')
class RedisTests(unittest.TestCase):
    def setUp(self):
        self.client = redis.Redis.from_url(REDIS_URL)
        self.name = f'test-{os.getpid()}-{id(self)}'
        self.addCleanup(self.client.delete, limits._PREFIX + self.name)

    def test_instances_share_one_count_and_a_dead_holders_lease_runs_out(self):
        one, two = limits._Shared(REDIS_URL), limits._Shared(REDIS_URL)  # two instances
        self.assertTrue(one.acquire(self.name, 2, 'a', 30))
        self.assertTrue(two.acquire(self.name, 2, 'b', 0.3))
        self.assertFalse(one.acquire(self.name, 2, 'c', 30))
        self.assertEqual(two.running(self.name), 2)
        import time
        time.sleep(0.5)  # 'b' - its instance gone - is past its lease
        self.assertTrue(one.acquire(self.name, 2, 'c', 30))
        one.release(self.name, 'a')
        one.release(self.name, 'c')
        self.assertEqual(one.running(self.name), 0)


if __name__ == '__main__':
    unittest.main()

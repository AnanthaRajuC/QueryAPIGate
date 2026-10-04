"""Rate limits shared across instances through Redis (BACKLOG #55, ratelimit.SharedRateLimiter).

The failure tests run anywhere, with a client that fails. The sharing tests need a real Redis - the token bucket is a
Lua script, which only Redis can run - and run when QUERYAPIGATE_TEST_REDIS_URL is set (CI's PostgreSQL job sets it).
Each "instance" is a separate create_app(), with its own in-process state, as a separate server would have."""
import logging
import os
import tempfile
import unittest
import uuid
from unittest import mock

import redis

from queryapigate import alerts, create_app, metrics, ratelimit
from tests.helpers import create_key, secret_of

REDIS_URL = os.environ.get('QUERYAPIGATE_TEST_REDIS_URL')
ADMIN = {'X-API-Key': 'admin-key'}


class _FailingClient:
    def __init__(self):
        self.calls = 0

    def register_script(self, _source):
        def run(keys, args):
            self.calls += 1
            raise redis.ConnectionError('Connection refused')
        return run


class FallbackTests(unittest.TestCase):
    def setUp(self):
        for name, value in (('_degraded_at', None), ('_last_warned', 0.0)):  # each test its own first failure
            patcher = mock.patch.object(ratelimit, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = _FailingClient()
        self.limiter = ratelimit.SharedRateLimiter(None, 'client', ratelimit.RateLimiter(), client=self.client)

    def test_a_redis_failure_falls_back_to_counting_here(self):
        with self.assertLogs('queryapigate', level=logging.WARNING) as logs:
            results = [self.limiter.hit('10.0.0.1', 2, 60)[0] for _ in range(3)]
        self.assertEqual(results, [True, True, False])  # still limited - per instance, never lifted
        self.assertIn('Rate limits fall back to per-instance counting', logs.output[0])

    def test_after_a_failure_redis_is_left_alone_for_a_few_seconds(self):
        with self.assertLogs('queryapigate', level=logging.WARNING):
            for _ in range(5):
                self.limiter.hit('10.0.0.1', 100, 60)
        self.assertEqual(self.client.calls, 1)  # one timeout, not one per request

    def test_the_failure_shows_in_metrics_and_alerts(self):
        def fallbacks():
            line = next(x for x in metrics.render().splitlines() if x.startswith('queryapigate_rate_limit_fallbacks'))
            return int(line.split()[-1])
        before = fallbacks()
        with self.assertLogs('queryapigate', level=logging.WARNING):
            self.limiter.hit('10.0.0.1', 100, 60)
        self.assertEqual(fallbacks(), before + 1)
        self.assertIsNotNone(ratelimit.degraded())
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': home}):
            create_app()
            kinds = [a['kind'] for a in alerts.collect()]
        self.assertIn('rate_limits_not_shared', kinds)


@unittest.skipUnless(REDIS_URL, 'set QUERYAPIGATE_TEST_REDIS_URL to a scratch Redis')
class SharedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = {'QUERYAPIGATE_HOME': self.tmp.name, 'QUERYAPIGATE_API_KEY': 'admin-key',
               'QUERYAPIGATE_REDIS_URL': REDIS_URL, 'QUERYAPIGATE_RATE_LIMIT': ''}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.address = f'10.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}.7'  # a bucket of this test's own

    def instances(self, n=2):
        return [create_app().test_client() for _ in range(n)]

    def call(self, client, headers=ADMIN):
        return client.get('/catalog', headers=headers, environ_base={'REMOTE_ADDR': self.address})

    def test_the_server_wide_limit_is_one_budget_across_instances(self):
        os.environ['QUERYAPIGATE_RATE_LIMIT'] = '4/minute'
        a, b = self.instances()
        statuses = [self.call((a, b)[i % 2]).status_code for i in range(6)]
        self.assertEqual(statuses, [200, 200, 200, 200, 429, 429])
        refused = self.call(a)
        self.assertEqual(refused.get_json()['code'], 'rate_limited')
        self.assertGreaterEqual(int(refused.headers['Retry-After']), 1)

    def test_a_keys_limit_is_one_budget_across_instances(self):
        a, b = self.instances()
        secret = secret_of(create_key(a, headers=ADMIN, name=f'k-{uuid.uuid4().hex[:8]}', connections=[],
                                      rate_limit='3/minute'))
        headers = {'X-API-Key': secret}
        statuses = [self.call((a, b)[i % 2], headers).status_code for i in range(5)]
        self.assertEqual(statuses, [200, 200, 200, 429, 429])
        self.assertEqual(self.call(b, headers).headers['X-RateLimit-Key-Remaining'], '0')

    def test_mcp_and_rest_share_a_keys_budget(self):
        from queryapigate import mcp_server
        rest, = self.instances(1)
        mcp_app = create_app()  # what `queryapigate mcp` runs its calls through
        secret = secret_of(create_key(rest, headers=ADMIN, name=f'k-{uuid.uuid4().hex[:8]}', connections=[],
                                      rate_limit='2/minute'))
        self.assertEqual(self.call(rest, {'X-API-Key': secret}).status_code, 200)
        result = mcp_server.handle_call(mcp_app, secret, '', self.address, 'no_such_tool', {})
        self.assertNotEqual(result['structuredContent'].get('code'), 'rate_limited')  # its own error, not a limit
        self.assertEqual(self.call(rest, {'X-API-Key': secret}).status_code, 429)

    def test_a_changed_limit_starts_a_fresh_bucket(self):
        os.environ['QUERYAPIGATE_RATE_LIMIT'] = '2/minute'
        a, = self.instances(1)
        self.assertEqual([self.call(a).status_code for _ in range(3)], [200, 200, 429])
        os.environ['QUERYAPIGATE_RATE_LIMIT'] = '3/minute'
        b, = self.instances(1)
        self.assertEqual(self.call(b).status_code, 200)

    def test_it_counts_like_the_in_process_limiter(self):
        shared = ratelimit.SharedRateLimiter(REDIS_URL, f'parity-{uuid.uuid4().hex}', ratelimit.RateLimiter())
        local = ratelimit.RateLimiter()
        self.assertEqual([shared.hit('c', 5, 60)[:2] for _ in range(7)],
                         [local.hit('c', 5, 60)[:2] for _ in range(7)])


if __name__ == '__main__':
    unittest.main()

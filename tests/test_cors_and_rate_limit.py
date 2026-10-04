"""Tests for CORS support and rate limiting."""
import io
import logging
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest import mock

from queryapigate import cli, config, cors, create_app
from queryapigate.ratelimit import KeyRateLimiters, RateLimiter

ORIGIN = 'https://app.example.com'


class AppTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ('QUERYAPIGATE_API_KEY', 'QUERYAPIGATE_CORS_ORIGINS', 'QUERYAPIGATE_RATE_LIMIT',
                     'QUERYAPIGATE_TRUST_PROXY'):
            os.environ.pop(name, None)

    def client(self):
        return create_app().test_client()


class CorsTests(AppTestCase):
    def test_off_by_default_no_headers_even_when_an_origin_is_sent(self):
        res = self.client().get('/api/v1/connections', headers={'Origin': ORIGIN})
        self.assertNotIn('Access-Control-Allow-Origin', res.headers)

    def test_listed_origin_is_echoed_with_vary_and_exposed_pagination_headers(self):
        os.environ['QUERYAPIGATE_CORS_ORIGINS'] = f'{ORIGIN}, https://other.example.org'
        res = self.client().get('/api/v1/connections', headers={'Origin': ORIGIN})
        self.assertEqual(res.headers['Access-Control-Allow-Origin'], ORIGIN)
        self.assertIn('Origin', res.headers['Vary'])
        for name in ('X-Page', 'X-Has-More', 'X-RateLimit-Remaining', 'X-RateLimit-Key-Remaining', 'X-Cache',
                     'Deprecation', 'Link'):  # a page's JS cannot read them otherwise
            self.assertIn(name, res.headers['Access-Control-Expose-Headers'])
        self.assertNotIn('Access-Control-Allow-Credentials', res.headers)

    def test_unlisted_origins_get_nothing(self):
        os.environ['QUERYAPIGATE_CORS_ORIGINS'] = ORIGIN
        client = self.client()
        for origin in ('https://evil.example.com', 'http://app.example.com', 'https://app.example.com:8443',
                       'https://app.example.com.evil.com', 'null', ''):
            res = client.get('/api/v1/connections', headers={'Origin': origin})
            self.assertNotIn('Access-Control-Allow-Origin', res.headers, origin)
        self.assertNotIn('Access-Control-Allow-Origin', client.get('/api/v1/connections').headers)  # no Origin at all

    def test_matching_ignores_case_and_a_trailing_slash(self):
        os.environ['QUERYAPIGATE_CORS_ORIGINS'] = 'HTTPS://App.Example.com/'
        res = self.client().get('/api/v1/connections', headers={'Origin': ORIGIN})
        self.assertEqual(res.headers['Access-Control-Allow-Origin'], ORIGIN)

    def test_wildcard(self):
        os.environ['QUERYAPIGATE_CORS_ORIGINS'] = '*'
        res = self.client().get('/api/v1/connections', headers={'Origin': 'https://anything.example'})
        self.assertEqual(res.headers['Access-Control-Allow-Origin'], '*')
        self.assertNotIn('Vary', res.headers)

    def test_preflight_is_answered_without_an_api_key(self):
        os.environ['QUERYAPIGATE_CORS_ORIGINS'] = ORIGIN
        os.environ['QUERYAPIGATE_API_KEY'] = 'k3y'  # browsers cannot attach the key to the permission check
        res = self.client().options('/execute_sql', headers={
            'Origin': ORIGIN, 'Access-Control-Request-Method': 'POST',
            'Access-Control-Request-Headers': 'content-type, x-api-key'})
        self.assertEqual(res.status_code, 204)
        self.assertEqual(res.headers['Access-Control-Allow-Origin'], ORIGIN)
        self.assertIn('POST', res.headers['Access-Control-Allow-Methods'])
        self.assertIn('X-API-Key', res.headers['Access-Control-Allow-Headers'])
        self.assertEqual(res.headers['Access-Control-Max-Age'], '600')

    def test_a_browser_can_send_and_read_the_request_id(self):
        os.environ['QUERYAPIGATE_CORS_ORIGINS'] = ORIGIN
        pre = self.client().options('/health', headers={
            'Origin': ORIGIN, 'Access-Control-Request-Method': 'GET', 'Access-Control-Request-Headers': 'x-request-id'})
        self.assertIn('X-Request-Id', pre.headers['Access-Control-Allow-Headers'])
        res = self.client().get('/health', headers={'Origin': ORIGIN, 'X-Request-Id': 'trace-1'})
        self.assertIn('X-Request-Id', res.headers['Access-Control-Expose-Headers'])

    def test_preflight_from_an_unlisted_origin_grants_nothing(self):
        os.environ['QUERYAPIGATE_CORS_ORIGINS'] = ORIGIN
        res = self.client().options('/execute_sql', headers={
            'Origin': 'https://evil.example.com', 'Access-Control-Request-Method': 'POST'})
        self.assertEqual(res.status_code, 204)
        self.assertNotIn('Access-Control-Allow-Origin', res.headers)
        self.assertNotIn('Access-Control-Allow-Methods', res.headers)

    def test_cors_is_not_authentication(self):
        os.environ['QUERYAPIGATE_CORS_ORIGINS'] = ORIGIN
        os.environ['QUERYAPIGATE_API_KEY'] = 'k3y'
        client = self.client()
        res = client.get('/api/v1/connections', headers={'Origin': ORIGIN})
        self.assertEqual(res.status_code, 401)
        self.assertEqual(res.headers['Access-Control-Allow-Origin'], ORIGIN)  # so the page can read the 401
        self.assertEqual(client.get('/api/v1/connections', headers={'Origin': ORIGIN, 'X-API-Key': 'k3y'}).status_code,
                         200)

    def test_a_plain_options_request_is_not_mistaken_for_a_preflight(self):
        os.environ['QUERYAPIGATE_CORS_ORIGINS'] = ORIGIN
        os.environ['QUERYAPIGATE_API_KEY'] = 'k3y'
        self.assertEqual(self.client().options('/api/v1/connections').status_code,
                         401)  # no Access-Control-Request-Method

    def test_wildcard_without_an_api_key_warns_loudly(self):
        os.environ['QUERYAPIGATE_CORS_ORIGINS'] = '*'
        with self.assertLogs('queryapigate', level=logging.WARNING) as logs:
            create_app()
        # Any line, not the first: the shared logger may also carry another thread's warning (the history writer's)
        self.assertTrue(any('QUERYAPIGATE_CORS_ORIGINS=*' in line for line in logs.output), logs.output)
        os.environ['QUERYAPIGATE_API_KEY'] = 'k3y'
        with mock.patch('queryapigate.app.log.warning') as warning:  # not assertNoLogs: other threads may log
            create_app()
        self.assertEqual([c for c in warning.call_args_list if 'QUERYAPIGATE_CORS_ORIGINS=*' in str(c)], [])

    def test_a_server_without_any_key_says_so_at_startup(self):
        # Under gunicorn (the Docker image) nothing else would: `queryapigate serve`'s own warning needs a public host.
        with mock.patch('queryapigate.app.log.warning') as warning:
            create_app()
        self.assertTrue([c for c in warning.call_args_list if 'No API key is configured' in str(c)])
        os.environ['QUERYAPIGATE_API_KEY'] = 'k3y'
        with mock.patch('queryapigate.app.log.warning') as warning:
            create_app()
        self.assertEqual([c for c in warning.call_args_list if 'No API key is configured' in str(c)], [])

    def test_helpers(self):
        self.assertIsNone(cors.allow_origin_value(ORIGIN))  # off
        self.assertIsNone(config.cors_origins())
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_CORS_ORIGINS': ' , '}):
            self.assertIsNone(config.cors_origins())


class RateLimitTests(AppTestCase):
    def test_off_by_default(self):
        client = self.client()
        for _ in range(20):
            res = client.get('/api/v1/connections')
        self.assertEqual(res.status_code, 200)
        self.assertNotIn('X-RateLimit-Limit', res.headers)

    def test_requests_over_the_limit_get_429_with_retry_after(self):
        os.environ['QUERYAPIGATE_RATE_LIMIT'] = '3/minute'
        client = self.client()
        remaining = []
        for _ in range(3):
            res = client.get('/api/v1/connections')
            self.assertEqual((res.status_code, res.headers['X-RateLimit-Limit']), (200, '3'))
            remaining.append(int(res.headers['X-RateLimit-Remaining']))
        self.assertEqual(remaining, [2, 1, 0])
        res = client.get('/api/v1/connections')
        self.assertEqual(res.status_code, 429)
        self.assertEqual(res.get_json()['error'], 'Rate limit exceeded')
        self.assertTrue(1 <= int(res.headers['Retry-After']) <= 20)
        self.assertEqual(res.get_json()['retry_after'], int(res.headers['Retry-After']))
        self.assertEqual(res.headers['X-RateLimit-Remaining'], '0')

    def test_health_checks_and_preflights_are_never_throttled(self):
        os.environ['QUERYAPIGATE_RATE_LIMIT'] = '1/minute'
        os.environ['QUERYAPIGATE_CORS_ORIGINS'] = ORIGIN
        client = self.client()
        self.assertEqual(client.get('/api/v1/connections').status_code, 200)
        self.assertEqual(client.get('/api/v1/connections').status_code, 429)
        for _ in range(5):
            self.assertEqual(client.get('/health').status_code, 200)
            self.assertEqual(client.options('/api/v1/connections', headers={
                'Origin': ORIGIN, 'Access-Control-Request-Method': 'GET'}).status_code, 204)

    def test_guessing_the_api_key_is_throttled_because_the_limit_comes_first(self):
        os.environ['QUERYAPIGATE_RATE_LIMIT'] = '3/minute'
        os.environ['QUERYAPIGATE_API_KEY'] = 'k3y'
        client = self.client()
        statuses = [client.get('/api/v1/connections', headers={'X-API-Key': f'guess{i}'}).status_code for i in range(5)]
        self.assertEqual(statuses, [401, 401, 401, 429, 429])
        # ...and even the right key is refused while the client is being throttled
        self.assertEqual(client.get('/api/v1/connections', headers={'X-API-Key': 'k3y'}).status_code, 429)

    def test_rejections_are_readable_by_a_browser(self):
        os.environ['QUERYAPIGATE_RATE_LIMIT'] = '1/minute'
        os.environ['QUERYAPIGATE_CORS_ORIGINS'] = ORIGIN
        client = self.client()
        client.get('/api/v1/connections')
        res = client.get('/api/v1/connections', headers={'Origin': ORIGIN})
        self.assertEqual(res.status_code, 429)
        self.assertEqual(res.headers['Access-Control-Allow-Origin'], ORIGIN)
        self.assertIn('Retry-After', res.headers['Access-Control-Expose-Headers'])

    def test_clients_are_counted_separately_by_address(self):
        os.environ['QUERYAPIGATE_RATE_LIMIT'] = '1/minute'
        client = self.client()
        get = lambda ip: client.get('/api/v1/connections', environ_base={'REMOTE_ADDR': ip}).status_code  # noqa: E731
        self.assertEqual([get('10.0.0.1'), get('10.0.0.1'), get('10.0.0.2')], [200, 429, 200])

    def test_forwarded_headers_are_ignored_unless_a_proxy_is_trusted(self):
        os.environ['QUERYAPIGATE_RATE_LIMIT'] = '1/minute'
        client = self.client()
        # a client cannot dodge the limit by inventing X-Forwarded-For values
        codes = [client.get('/api/v1/connections', headers={'X-Forwarded-For': f'203.0.113.{i}'}).status_code
                 for i in range(3)]
        self.assertEqual(codes, [200, 429, 429])

    def test_behind_a_trusted_proxy_the_real_client_address_is_used(self):
        os.environ['QUERYAPIGATE_RATE_LIMIT'] = '1/minute'
        os.environ['QUERYAPIGATE_TRUST_PROXY'] = '1'
        client = self.client()
        get = lambda ip: client.get('/api/v1/connections', headers={'X-Forwarded-For': ip},  # noqa: E731
                                    environ_base={'REMOTE_ADDR': '172.17.0.2'}).status_code  # the proxy itself
        self.assertEqual([get('203.0.113.1'), get('203.0.113.1'), get('203.0.113.2')], [200, 429, 200])

    def test_only_the_trusted_hop_is_believed(self):
        os.environ['QUERYAPIGATE_RATE_LIMIT'] = '1/minute'
        os.environ['QUERYAPIGATE_TRUST_PROXY'] = '1'
        client = self.client()
        # the proxy appends the address it actually saw; anything a client prepended is not trusted
        get = lambda forged: client.get('/api/v1/connections',  # noqa: E731
                                        headers={'X-Forwarded-For': f'{forged}, 203.0.113.9'},
                                        environ_base={'REMOTE_ADDR': '172.17.0.2'}).status_code
        self.assertEqual([get('1.1.1.1'), get('2.2.2.2'), get('3.3.3.3')], [200, 429, 429])

    def test_malformed_setting_fails_at_startup_instead_of_silently_disabling_the_limit(self):
        for bad in ('fast', '0/minute', '10/week', '-5/second', '60', '/minute', '5 per minute'):
            with mock.patch.dict(os.environ, {'QUERYAPIGATE_RATE_LIMIT': bad}):
                with self.assertRaises(ValueError, msg=bad):
                    create_app()

    def test_the_command_line_reports_a_malformed_setting_cleanly(self):
        os.environ['QUERYAPIGATE_RATE_LIMIT'] = 'fast'
        err = io.StringIO()
        with redirect_stderr(err):
            code = cli.main(['serve', '--port', '5999'])
        self.assertEqual(code, 2)
        self.assertIn("QUERYAPIGATE_RATE_LIMIT must look like '60/minute'", err.getvalue())

    def test_accepted_spellings(self):
        for text, expected in (('60/minute', (60, 60)), (' 5 / second ', (5, 1)), ('100/HOURS', (100, 3600)),
                               ('1/day', (1, 86400)), ('7/Minutes', (7, 60))):
            self.assertEqual(config.parse_rate_limit(text), expected, text)

    def test_format_rate_limit_is_the_exact_inverse_of_parse_rate_limit(self):
        for text in ('60/minute', '5/second', '100/hour', '1/day'):
            self.assertEqual(config.format_rate_limit(config.parse_rate_limit(text)), text)
        self.assertIsNone(config.format_rate_limit(None))


class KeyRateLimitTests(AppTestCase):
    """A key's own optional `rate_limit` grant - checked in *addition* to QUERYAPIGATE_RATE_LIMIT above, never
    instead of it (see apikeys.py's module docstring). Requests go to /connections, an admin-only endpoint
    a scoped key normally gets 403 from - which is fine here: the rate-limit check runs in the
    before_request gate, ahead of that route's own admin check, the same way the existing IP-based tests
    above use /connections' 401 to prove rate limiting fires before authentication."""

    def create_key(self, name='scoped', rate_limit=None, **extra):
        os.environ.setdefault('QUERYAPIGATE_API_KEY', 'admin-key')
        client = self.client()
        res = client.post('/api/v1/api-keys', json={'name': name, 'connections': [], 'rate_limit': rate_limit, **extra},
                          headers={'X-API-Key': 'admin-key'})
        self.assertEqual(res.status_code, 201, res.get_data(as_text=True))
        return res.get_json()['secret'], client

    def test_over_the_per_key_limit_gets_429_with_its_own_error_and_headers(self):
        key, client = self.create_key('tight', rate_limit='2/minute')
        headers = {'X-API-Key': key}
        for expected_remaining in (1, 0):
            res = client.get('/api/v1/connections', headers=headers)
            self.assertEqual(res.status_code, 403)  # not admin - but still counted, see class docstring
            self.assertEqual(res.headers['X-RateLimit-Key-Limit'], '2')
            self.assertEqual(res.headers['X-RateLimit-Key-Remaining'], str(expected_remaining))
        res = client.get('/api/v1/connections', headers=headers)
        self.assertEqual(res.status_code, 429)
        self.assertEqual(res.get_json()['error'], 'Rate limit exceeded for this API key')
        self.assertIn('retry_after', res.get_json())

    def test_a_key_with_no_rate_limit_is_never_throttled_by_this_mechanism(self):
        key, client = self.create_key('unlimited')
        headers = {'X-API-Key': key}
        statuses = [client.get('/api/v1/connections', headers=headers).status_code for _ in range(20)]
        self.assertTrue(all(s == 403 for s in statuses), statuses)  # never 429 - always the plain admin-only 403

    def test_two_keys_with_the_same_limit_have_independent_budgets(self):
        key_a, client = self.create_key('key-a', rate_limit='1/minute')
        key_b, _ = self.create_key('key-b', rate_limit='1/minute')
        self.assertEqual(client.get('/api/v1/connections', headers={'X-API-Key': key_a}).status_code, 403)
        self.assertEqual(client.get('/api/v1/connections', headers={'X-API-Key': key_a}).status_code, 429)
        # key_b's own budget is untouched by key_a's use
        self.assertEqual(client.get('/api/v1/connections', headers={'X-API-Key': key_b}).status_code, 403)

    def test_applies_in_addition_to_the_server_wide_limit_not_instead_of_it(self):
        os.environ['QUERYAPIGATE_RATE_LIMIT'] = '100/minute'  # generous - should not be what bites
        key, client = self.create_key('tight', rate_limit='1/minute')
        headers = {'X-API-Key': key}
        self.assertEqual(client.get('/api/v1/connections', headers=headers).status_code, 403)
        res = client.get('/api/v1/connections', headers=headers)
        self.assertEqual(res.status_code, 429)
        self.assertEqual(res.get_json()['error'], 'Rate limit exceeded for this API key')  # the per-key one

    def test_the_admin_key_is_never_limited_by_this_mechanism(self):
        os.environ['QUERYAPIGATE_API_KEY'] = 'admin-key'
        client = self.client()
        headers = {'X-API-Key': 'admin-key'}
        statuses = [client.get('/api/v1/connections', headers=headers).status_code for _ in range(20)]
        self.assertTrue(all(s == 200 for s in statuses), statuses)

    def test_health_and_metrics_are_exempt_from_the_key_limit_too(self):
        key, client = self.create_key('tight', rate_limit='1/minute')
        headers = {'X-API-Key': key}
        client.get('/api/v1/connections', headers=headers)
        self.assertEqual(client.get('/api/v1/connections', headers=headers).status_code, 429)
        for _ in range(5):
            self.assertEqual(client.get('/health', headers=headers).status_code, 200)
            self.assertEqual(client.get('/metrics', headers=headers).status_code, 200)

    def test_malformed_rate_limit_is_rejected_with_a_message_naming_the_right_field(self):
        os.environ.setdefault('QUERYAPIGATE_API_KEY', 'admin-key')
        client = self.client()
        res = client.post('/api/v1/api-keys', json={'name': 'bad', 'rate_limit': 'fast'},
                          headers={'X-API-Key': 'admin-key'})
        self.assertEqual(res.status_code, 400)
        self.assertIn('rate_limit must look like', res.get_json()['error'])
        self.assertNotIn('QUERYAPIGATE_RATE_LIMIT', res.get_json()['error'])  # the env var, not this field

    def test_clearing_rate_limit_via_explicit_null_removes_the_limit(self):
        key, client = self.create_key('tight', rate_limit='1/minute')
        headers = {'X-API-Key': key}
        client.get('/api/v1/connections', headers=headers)
        self.assertEqual(client.get('/api/v1/connections', headers=headers).status_code, 429)
        client.patch('/api/v1/api-keys/tight', json={'rate_limit': None}, headers={'X-API-Key': 'admin-key'})
        self.assertEqual(client.get('/api/v1/connections', headers=headers).status_code, 403)  # limited no more


class TokenBucketTests(unittest.TestCase):
    """The limiter itself, driven by a fake clock."""

    def setUp(self):
        self.now = 1000.0
        self.limiter = RateLimiter(clock=lambda: self.now)

    def test_burst_then_steady_rate(self):
        results = [self.limiter.hit('a', 60, 60)[0] for _ in range(61)]
        self.assertEqual(results, [True] * 60 + [False])          # the whole quota may be used at once
        self.now += 1                                             # 60/minute refills one token per second
        self.assertTrue(self.limiter.hit('a', 60, 60)[0])
        self.assertFalse(self.limiter.hit('a', 60, 60)[0])

    def test_remaining_and_retry_after(self):
        self.assertEqual(self.limiter.hit('a', 2, 60), (True, 1, 0))
        self.assertEqual(self.limiter.hit('a', 2, 60), (True, 0, 0))
        allowed, remaining, retry_after = self.limiter.hit('a', 2, 60)
        self.assertEqual((allowed, remaining, retry_after), (False, 0, 30))  # one token every 30 s
        self.now += 10
        self.assertEqual(self.limiter.hit('a', 2, 60)[2], 20)                # the wait shrinks as time passes

    def test_tokens_never_exceed_the_quota(self):
        self.limiter.hit('a', 3, 60)
        self.now += 10_000
        self.assertEqual(self.limiter.hit('a', 3, 60), (True, 2, 0))

    def test_clients_are_independent(self):
        for _ in range(3):
            self.limiter.hit('a', 3, 60)
        self.assertFalse(self.limiter.hit('a', 3, 60)[0])
        self.assertTrue(self.limiter.hit('b', 3, 60)[0])

    def test_changing_the_limit_starts_fresh(self):
        for _ in range(3):
            self.limiter.hit('a', 3, 60)
        self.assertFalse(self.limiter.hit('a', 3, 60)[0])
        self.assertTrue(self.limiter.hit('a', 10, 60)[0])

    def test_idle_clients_are_forgotten_and_memory_is_bounded(self):
        for i in range(50):
            self.limiter.hit(f'c{i}', 5, 60)
        self.now += 61                                            # everyone is idle for more than a period
        self.limiter.hit('fresh', 5, 60)
        self.assertEqual(self.limiter.size(), 1)
        self.limiter.MAX_CLIENTS = 10
        for i in range(100):
            self.limiter.hit(f'flood{i}', 5, 60)
        self.assertLessEqual(self.limiter.size(), 10)             # least recently seen are dropped first

    def test_many_threads_cannot_exceed_the_quota(self):
        import threading
        allowed = []

        def worker():
            for _ in range(50):
                allowed.append(self.limiter.hit('shared', 100, 3600)[0])

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(allowed.count(True), 100)                # exactly the quota, no matter the interleaving


class KeyRateLimitersTests(unittest.TestCase):
    """KeyRateLimiters routes each distinct (count, period) spec to its own RateLimiter, since one
    RateLimiter enforces exactly one spec at a time and wipes every bucket when a *different* spec arrives
    (see RateLimiter.hit()) - two keys configured with different limits would otherwise keep clobbering
    each other's state if fed through a single shared instance."""

    def setUp(self):
        self.now = 1000.0
        self.limiters = KeyRateLimiters(clock=lambda: self.now)

    def test_different_specs_do_not_clobber_each_others_buckets(self):
        # if these shared one RateLimiter instance, 'b's differing spec would wipe every bucket, including
        # the one this loop is still exhausting for 'a'.
        for _ in range(2):
            self.assertTrue(self.limiters.hit('a', 2, 60)[0])
            self.limiters.hit('b', 5, 3600)
        self.assertFalse(self.limiters.hit('a', 2, 60)[0])  # 'a's quota is genuinely exhausted, not reset

    def test_the_same_spec_is_shared_by_two_different_keys_with_independent_buckets(self):
        self.assertEqual(self.limiters.hit('a', 1, 60), (True, 0, 0))
        self.assertFalse(self.limiters.hit('a', 1, 60)[0])
        self.assertEqual(self.limiters.hit('b', 1, 60), (True, 0, 0))  # unaffected by 'a's own bucket


if __name__ == '__main__':
    unittest.main()

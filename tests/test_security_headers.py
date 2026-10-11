"""Security headers on every response (ADR 0010, BACKLOG #93): nosniff everywhere; on API responses a CSP that runs
nothing and a no-referrer policy; no-store unless the route caches; HSTS only when set and only over HTTPS; /docs's
Swagger UI pinned to an exact release with integrity hashes."""
import asyncio
import base64
import hashlib
import os
import re
import sqlite3
import tempfile
import unittest
import urllib.error
import urllib.request
from unittest import mock

from queryapigate import cli, config, create_app, openapi, secheaders
from tests.helpers import create_key, save_query, secret_of, write_connections

ADMIN = {'X-API-Key': 'admin-key'}
API_CSP = "default-src 'none'; frame-ancestors 'none'"


class HeadersTestCase(unittest.TestCase):
    env = {}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        data = os.path.join(self.tmp.name, 'data.db')
        conn = sqlite3.connect(data)
        conn.execute('CREATE TABLE t (id INTEGER)')
        conn.execute('INSERT INTO t VALUES (1)')
        conn.commit()
        conn.close()
        patcher = mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': self.tmp.name, 'QUERYAPIGATE_API_KEY': 'admin-key',
                                               **self.env})
        patcher.start()
        self.addCleanup(patcher.stop)
        write_connections({'lite': {'db': 'sqlite', 'database': data, 'active': True}})
        self.client = create_app().test_client()

    def save(self, name, **fields):
        res = save_query(self.client, {'author': 'a', 'description': 'd', 'filename': name, 'connection_name': 'lite',
                                       'sql_query': 'SELECT id FROM t', **fields}, headers=ADMIN)
        self.assertEqual(res.status_code, 201, res.get_data(as_text=True))

    def assert_api_headers(self, res, cache='no-store'):
        self.assertEqual(res.headers['X-Content-Type-Options'], 'nosniff', res.request.path)
        self.assertEqual(res.headers['Content-Security-Policy'], API_CSP, res.request.path)
        self.assertEqual(res.headers['Referrer-Policy'], 'no-referrer', res.request.path)
        self.assertEqual(res.headers['Cache-Control'], cache, res.request.path)
        self.assertNotIn('Strict-Transport-Security', res.headers)


class ApiResponseTests(HeadersTestCase):
    def test_every_kind_of_api_response(self):
        self.save('q')
        for path, headers, status in (('/health', {}, 200), ('/q/q', ADMIN, 200), ('/q/q?format=csv', ADMIN, 200),
                                      ('/q/q?format=parquet', ADMIN, 200), ('/api/v1/queries', ADMIN, 200),
                                      ('/api/v1/settings', ADMIN, 200), ('/openapi.json', ADMIN, 200),
                                      ('/metrics', ADMIN, 200), ('/q/q', {}, 401), ('/q/nothing', ADMIN, 404),
                                      ('/api/v1/queries', {'X-API-Key': 'wrong'}, 401)):
            res = self.client.get(path, headers=headers)
            self.assertEqual(res.status_code, status, path)
            self.assert_api_headers(res)
        res = self.client.post('/execute_sql', json={'connection_name': 'lite', 'sql': 'SELECT 1'}, headers=ADMIN)
        self.assert_api_headers(res)

    def test_a_cached_query_stays_private_and_keeps_its_max_age(self):
        self.save('cached', cache_ttl=30)
        for _ in range(2):  # the miss, then the hit
            self.assert_api_headers(self.client.get('/q/cached', headers=ADMIN), cache='private, max-age=30')

    def test_a_new_secret_is_never_stored(self):
        res = create_key(self.client, ADMIN, name='k', connections=['lite'])
        secret_of(res)
        self.assert_api_headers(res)

    def test_the_live_stream_keeps_no_cache(self):
        with self.client.get('/events', headers=ADMIN, buffered=False) as res:
            self.assertEqual(res.headers['Cache-Control'], 'no-cache')
            self.assertEqual(res.headers['X-Content-Type-Options'], 'nosniff')


class HtmlPageTests(HeadersTestCase):
    def test_docs_has_its_own_policy_and_exactly_the_pinned_files(self):
        res = self.client.get('/docs')
        self.assertEqual(res.headers['Content-Security-Policy'], openapi.DOCS_CSP)
        self.assertEqual((res.headers['X-Content-Type-Options'], res.headers['Referrer-Policy']),
                         ('nosniff', 'same-origin'))
        html = res.get_data(as_text=True)
        loaded = re.findall(r'(?:src|href)="(https://[^"]+)" integrity="([^"]+)" crossorigin="anonymous"', html)
        self.assertEqual(sorted(loaded), sorted((openapi.SWAGGER_UI_BASE + name, digest)
                                                for name, digest in openapi.SWAGGER_UI_FILES.items()))
        self.assertRegex(openapi.SWAGGER_UI_VERSION, r'^\d+\.\d+\.\d+$')  # an exact release, never a range
        self.assertEqual(re.findall(r'https://[^"\s;]+', html), [u for u, _ in loaded])  # nothing else from outside

    def test_the_docs_policy_allows_exactly_its_own_inline_script(self):
        html = openapi.DOCS_HTML
        scripts = re.findall(r'<script>(.*?)</script>', html, re.S)
        self.assertEqual(len(scripts), 1)
        digest = base64.b64encode(hashlib.sha256(scripts[0].encode()).digest()).decode()
        self.assertIn(f"'sha256-{digest}'", openapi.DOCS_CSP)
        self.assertNotIn("'unsafe-eval'", openapi.DOCS_CSP)
        self.assertNotIn("script-src 'unsafe-inline'", openapi.DOCS_CSP)

    def test_the_console_keeps_its_own_policy(self):
        res = self.client.get('/console/')
        self.assertIn("frame-ancestors 'none'", res.headers['Content-Security-Policy'])
        self.assertNotEqual(res.headers['Content-Security-Policy'], API_CSP)
        self.assertEqual(res.headers['X-Content-Type-Options'], 'nosniff')


@unittest.skipUnless(os.environ.get('QUERYAPIGATE_TEST_NETWORK', '1') == '1', 'no network')
class PinnedFilesTests(unittest.TestCase):
    def test_the_integrity_hashes_match_the_files_on_the_cdn(self):
        for name, digest in openapi.SWAGGER_UI_FILES.items():
            try:
                with urllib.request.urlopen(openapi.SWAGGER_UI_BASE + name, timeout=20) as res:
                    body = res.read()
            except (urllib.error.URLError, OSError) as error:
                self.skipTest(f'the CDN is not reachable: {error}')
            self.assertEqual('sha384-' + base64.b64encode(hashlib.sha384(body).digest()).decode(), digest, name)


class HstsTests(HeadersTestCase):
    env = {'QUERYAPIGATE_HSTS_MAX_AGE': '31536000'}

    def test_sent_only_over_https(self):
        self.assertNotIn('Strict-Transport-Security', self.client.get('/health').headers)
        res = self.client.get('/health', base_url='https://api.example.com')
        self.assertEqual(res.headers['Strict-Transport-Security'], 'max-age=31536000')

    def test_behind_a_trusted_proxy_the_forwarded_scheme_counts(self):
        forwarded = {'X-Forwarded-Proto': 'https'}
        self.assertNotIn('Strict-Transport-Security', self.client.get('/health', headers=forwarded).headers)
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_TRUST_PROXY': '1'}):
            client = create_app().test_client()
        self.assertEqual(client.get('/health', headers=forwarded).headers['Strict-Transport-Security'],
                         'max-age=31536000')

    def test_off_by_default_even_over_https(self):
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_HSTS_MAX_AGE': ''}):
            res = self.client.get('/health', base_url='https://api.example.com')
        self.assertNotIn('Strict-Transport-Security', res.headers)

    def test_a_bad_value_stops_the_server(self):
        for raw in ('a year', '-1', '0', str(config.MAX_HSTS_SECONDS + 1)):
            with mock.patch.dict(os.environ, {'QUERYAPIGATE_HSTS_MAX_AGE': raw}), \
                    self.assertRaisesRegex(ValueError, 'QUERYAPIGATE_HSTS_MAX_AGE'):
                config.check_settings()


class OtherServersTests(unittest.TestCase):
    def test_the_asgi_wrapper_adds_what_a_response_lacks_and_keeps_what_it_has(self):
        async def app(scope, receive, send):
            await send({'type': 'http.response.start', 'status': 200,
                        'headers': [(b'content-type', b'application/json'), (b'cache-control', b'max-age=3600')]})
            await send({'type': 'http.response.body', 'body': b'{}'})

        sent = []

        async def send(message):
            sent.append(message)

        asyncio.run(secheaders.asgi(app)({'type': 'http'}, None, send))
        headers = dict(sent[0]['headers'])
        self.assertEqual(headers[b'cache-control'], b'max-age=3600')  # the route's own, kept
        self.assertEqual(headers[b'x-content-type-options'], b'nosniff')
        self.assertEqual(headers[b'content-security-policy'], API_CSP.encode())
        self.assertEqual(headers[b'referrer-policy'], b'no-referrer')
        self.assertEqual(sent[1]['body'], b'{}')

    def test_the_development_server_has_no_version_banner(self):
        handler = cli._handler()
        self.assertEqual(handler.version_string(handler.__new__(handler)), 'QueryAPIGate')


if __name__ == '__main__':
    unittest.main()

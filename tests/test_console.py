"""Serving the QueryAPIGate Console's static build at /console (queryapigate/console.py, ADR 0001)."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from queryapigate import console, create_app


class ConsoleTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': self.tmp.name, 'QUERYAPIGATE_API_KEY': 'admin-key'})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.dist = Path(self.tmp.name) / 'console_dist'
        dist_patcher = mock.patch.object(console, 'DIST', self.dist)
        dist_patcher.start()
        self.addCleanup(dist_patcher.stop)
        self.client = create_app().test_client()

    def get(self, path, client=None, **kwargs):
        """GET, buffer the body and close the response - send_from_directory streams an open file otherwise."""
        res = (client or self.client).get(path, **kwargs)
        res.get_data()
        res.close()
        return res

    def build(self):
        (self.dist / 'assets').mkdir(parents=True)
        (self.dist / 'index.html').write_text('<!doctype html><title>Console</title><div id="root"></div>')
        (self.dist / 'assets' / 'index-abc123.js').write_text('console.log(1)')
        (self.dist / 'favicon.svg').write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')


class NotBuiltTests(ConsoleTestCase):
    def test_a_checkout_without_a_build_explains_how_to_build_it(self):
        for path in ('/console', '/console/', '/console/queries'):
            res = self.get(path)
            self.assertEqual(res.status_code, 200, path)
            page = res.get_data(as_text=True)
            self.assertIn("isn't built", page)
            self.assertIn('npm run build', page)
            self.assertIn('href="/ui"', page)


class BuiltTests(ConsoleTestCase):
    def setUp(self):
        super().setUp()
        self.build()

    def test_index_is_public_even_with_an_api_key_set(self):
        res = self.get('/console/')
        self.assertEqual(res.status_code, 200)
        self.assertIn('<div id="root">', res.get_data(as_text=True))
        self.assertEqual(res.headers['Cache-Control'], 'no-cache')

    def test_client_side_routes_fall_back_to_index(self):
        for path in ('/console', '/console/queries', '/console/govern/api-keys'):
            res = self.get(path)
            self.assertEqual(res.status_code, 200, path)
            self.assertIn('<div id="root">', res.get_data(as_text=True), path)

    def test_hashed_assets_are_cached_forever_other_files_are_not(self):
        res = self.get('/console/assets/index-abc123.js')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_data(as_text=True), 'console.log(1)')
        self.assertEqual(res.headers['Cache-Control'], 'public, max-age=31536000, immutable')
        res = self.get('/console/favicon.svg')
        self.assertEqual(res.status_code, 200)
        self.assertNotIn('immutable', res.headers.get('Cache-Control', ''))

    def test_a_missing_asset_is_a_404_not_index_html(self):
        res = self.get('/console/assets/missing-999.js')
        self.assertEqual(res.status_code, 404)
        self.assertNotIn('<div id="root">', res.get_data(as_text=True))

    def test_nothing_outside_the_build_directory_is_served(self):
        secret = Path(self.tmp.name) / 'secret.txt'
        secret.write_text('do not serve')
        for path in ('/console/../secret.txt', '/console/%2e%2e/secret.txt', '/console/assets/../../secret.txt'):
            res = self.get(path)
            self.assertNotIn('do not serve', res.get_data(as_text=True), path)

    def test_every_response_carries_a_strict_content_security_policy(self):
        for path in ('/console/', '/console/queries', '/console/assets/index-abc123.js',
                     '/console/assets/missing-999.js'):
            res = self.get(path)
            csp = res.headers.get('Content-Security-Policy', '')
            self.assertIn("script-src 'self'", csp, path)
            self.assertIn("frame-ancestors 'none'", csp, path)
            self.assertNotIn('unsafe-eval', csp, path)
            self.assertEqual(res.headers.get('X-Content-Type-Options'), 'nosniff', path)

    def test_loading_the_console_never_spends_a_clients_rate_limit(self):
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_RATE_LIMIT': '2/minute'}):
            client = create_app().test_client()
            for _ in range(5):
                self.assertEqual(self.get('/console/assets/index-abc123.js', client).status_code, 200)
            self.assertEqual(self.get('/health', client).status_code, 200)  # also exempt, unchanged
            statuses = [self.get('/catalog', client, headers={'X-API-Key': 'admin-key'}).status_code for _ in range(3)]
            self.assertEqual(statuses[-1], 429)  # API calls are still limited


if __name__ == '__main__':
    unittest.main()

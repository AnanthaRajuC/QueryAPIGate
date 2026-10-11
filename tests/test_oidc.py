"""An identity provider's metadata (oidc.py), shared by the MCP server's OAuth (ADR 0006) and the Console's sign-in
(ADR 0007) - and the Console sign-in settings' startup checks (ADR 0007)."""
import os
import unittest
from unittest import mock

from queryapigate import adminroles, config, oidc

ISSUER = 'https://idp.example.com/realms/acme'
GOOD = {'issuer': ISSUER, 'jwks_uri': ISSUER + '/certs', 'authorization_endpoint': ISSUER + '/auth',
        'token_endpoint': ISSUER + '/token', 'end_session_endpoint': ISSUER + '/logout'}


class MetadataTests(unittest.TestCase):
    def setUp(self):
        oidc.forget()
        self.addCleanup(oidc.forget)
        self.clock = [1000.0]
        patcher = mock.patch('time.monotonic', lambda: self.clock[0])
        patcher.start()
        self.addCleanup(patcher.stop)

    def serve(self, *documents):
        """fetch_json answering each discovery URL in turn with the next document (an exception is raised)."""
        answers = list(documents)

        def fetch(url):
            answer = answers.pop(0) if answers else OSError('no more')
            if isinstance(answer, Exception):
                raise answer
            return answer
        return mock.patch.object(oidc, 'fetch_json', side_effect=fetch)

    def test_the_first_document_that_answers_and_names_the_issuer(self):
        with self.serve(OSError('404'), {**GOOD, 'issuer': 'https://other.example.com'}, GOOD):
            self.assertEqual(oidc.metadata(ISSUER), GOOD)
        self.assertEqual(oidc.jwks_uri(ISSUER), ISSUER + '/certs')

    def test_a_document_with_any_plain_http_endpoint_is_refused_whole(self):
        for endpoint in ('jwks_uri', 'authorization_endpoint', 'token_endpoint', 'end_session_endpoint'):
            with self.subTest(endpoint=endpoint):
                oidc.forget()
                with self.serve(*[{**GOOD, endpoint: 'http://idp.example.com/x'}] * 3):
                    self.assertIsNone(oidc.metadata(ISSUER))

    def test_kept_for_an_hour_then_fetched_again_and_kept_through_an_outage(self):
        with self.serve(GOOD) as fetch:
            oidc.metadata(ISSUER)
            self.clock[0] += oidc.FRESH_FOR - 1
            oidc.metadata(ISSUER)
        self.assertEqual(fetch.call_count, 1)
        self.clock[0] += 2
        with self.serve(OSError('down'), OSError('down'), OSError('down')), self.assertLogs('queryapigate'):
            self.assertEqual(oidc.metadata(ISSUER), GOOD)  # stale, but sign-in keeps working

    def test_a_provider_never_reached_is_tried_again_after_a_while_not_every_time(self):
        with self.serve(OSError('x'), OSError('x'), OSError('x')) as fetch, self.assertLogs('queryapigate'):
            self.assertIsNone(oidc.metadata(ISSUER))
            self.assertIsNone(oidc.metadata(ISSUER))
        self.assertEqual(fetch.call_count, 3)  # one round of the three URLs, not two
        self.clock[0] += oidc.RETRY + 1
        with self.serve(GOOD):
            self.assertEqual(oidc.metadata(ISSUER), GOOD)


SSO = {'QUERYAPIGATE_SSO_ISSUER': ISSUER, 'QUERYAPIGATE_SSO_CLIENT_ID': 'queryapigate',
       'QUERYAPIGATE_SSO_CLIENT_SECRET': 's3cret', 'QUERYAPIGATE_SSO_REDIRECT_URL':
       'https://data.example.com/auth/sso/callback', 'QUERYAPIGATE_SSO_ROLES': 'owner=qag-owners,admin=qag-admins'}


class SettingsTests(unittest.TestCase):
    def check(self, **env):
        with mock.patch.dict(os.environ, {**{name: '' for name in config._SSO_VARS}, **env}):
            config.check_settings()
            return config.sso_enabled()

    def test_the_role_names_are_the_admin_roles(self):
        self.assertEqual(set(config.SSO_ROLE_ORDER), set(adminroles.ROLES))

    def test_good_configurations(self):
        self.assertFalse(self.check())
        self.assertTrue(self.check(**SSO))
        self.assertTrue(self.check(**{**SSO, 'QUERYAPIGATE_SSO_ROLES': '', 'QUERYAPIGATE_SSO_DEFAULT_ROLE': 'auditor',
                                      'QUERYAPIGATE_SSO_ALLOWED_DOMAINS': 'example.com'}))
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_SSO_ROLES': 'admin=a, admin=b ,auditor=c'}):
            self.assertEqual(config.sso_roles(), [('admin', 'a'), ('admin', 'b'), ('auditor', 'c')])

    def test_unsafe_or_incomplete_ones_stop_startup(self):
        for label, env in (
                ('half set', {'QUERYAPIGATE_SSO_ISSUER': ISSUER}),
                ('plain http issuer', {**SSO, 'QUERYAPIGATE_SSO_ISSUER': 'http://idp.example.com/'}),
                ('another callback path', {**SSO, 'QUERYAPIGATE_SSO_REDIRECT_URL': 'https://data.example.com/cb'}),
                ('no roles', {**SSO, 'QUERYAPIGATE_SSO_ROLES': ''}),
                ('a role that does not exist', {**SSO, 'QUERYAPIGATE_SSO_ROLES': 'superuser=everyone'}),
                ('not role=group', {**SSO, 'QUERYAPIGATE_SSO_ROLES': 'admin'}),
                ('a default role for anyone', {**SSO, 'QUERYAPIGATE_SSO_DEFAULT_ROLE': 'auditor'}),
                ('owner by default', {**SSO, 'QUERYAPIGATE_SSO_DEFAULT_ROLE': 'owner',
                                      'QUERYAPIGATE_SSO_ALLOWED_DOMAINS': 'example.com'}),
                ('session too long', {**SSO, 'QUERYAPIGATE_SSO_SESSION_HOURS': '999'}),
                ('session not a number', {**SSO, 'QUERYAPIGATE_SSO_SESSION_HOURS': 'eight'}),
                ('sso only, misspelt', {**SSO, 'QUERYAPIGATE_SSO_ONLY': 'perhaps'})):
            with self.subTest(label), self.assertRaises(ValueError):
                self.check(**env)

    def test_the_secret_never_shows_in_settings(self):
        with mock.patch.dict(os.environ, SSO):
            rows = [r for s in config.describe_settings() if s['id'] == 'sso' for r in s['rows']]
        secret = next(r for r in rows if r['env'] == 'QUERYAPIGATE_SSO_CLIENT_SECRET')
        self.assertEqual((secret['value'], secret['env_value']), ('configured', None))
        self.assertNotIn('s3cret', repr(rows))


if __name__ == '__main__':
    unittest.main()

"""Console sign-in through the identity provider (ADR 0007, sso.py): the authorization code flow with PKCE against a
local stand-in provider that issues real signed ID tokens - and checks, at its token endpoint, the PKCE verifier and
the client secret - so every way a sign-in must fail runs for real: a replayed or foreign state, a wrong nonce, a
token for another client, another issuer or key, no group with a role. Then the session: its cookie, its CSRF token,
sign-out, and what it never does (authenticate alone, or beat an API key header)."""
import base64
import hashlib
import http.server
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.parse
from unittest import mock

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from queryapigate import admins, cors, create_app, jwtauth, oidc, sso, store

CLIENT_ID, CLIENT_SECRET = 'queryapigate-console', 'console-secret'
CALLBACK = 'http://localhost/auth/sso/callback'


def b64(n):
    return base64.urlsafe_b64encode(n.to_bytes((n.bit_length() + 7) // 8, 'big')).rstrip(b'=').decode()


class Provider:
    """A stand-in identity provider: OpenID Connect metadata, keys, and a token endpoint that redeems the codes a
    test hands out with authorize()."""

    def __init__(self):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.codes = {}       # code -> (challenge, claims)
        self.token_requests = []
        provider = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def _send(self, status, body):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path == '/realms/qag/.well-known/openid-configuration':
                    base = provider.issuer
                    self._send(200, {'issuer': base, 'jwks_uri': base + '/certs',
                                     'authorization_endpoint': base + '/auth', 'token_endpoint': base + '/token',
                                     'end_session_endpoint': base + '/logout',
                                     'code_challenge_methods_supported': ['S256']})
                elif self.path == '/realms/qag/certs':
                    numbers = provider.key.public_key().public_numbers()
                    self._send(200, {'keys': [{'kty': 'RSA', 'kid': 'k1', 'use': 'sig', 'alg': 'RS256',
                                               'n': b64(numbers.n), 'e': b64(numbers.e)}]})
                else:
                    self._send(404, {})

            def do_POST(self):
                length = int(self.headers.get('Content-Length', 0))
                form = dict(urllib.parse.parse_qsl(self.rfile.read(length).decode()))
                provider.token_requests.append((form, self.headers.get('Authorization')))
                expected = 'Basic ' + base64.b64encode(f'{CLIENT_ID}:{CLIENT_SECRET}'.encode()).decode()
                found = provider.codes.pop(form.get('code'), None)
                if self.headers.get('Authorization') != expected or found is None:
                    return self._send(400, {'error': 'invalid_grant'})
                challenge, claims = found
                verifier = form.get('code_verifier', '')
                if base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode() \
                        != challenge or form.get('redirect_uri') != CALLBACK:
                    return self._send(400, {'error': 'invalid_grant'})
                self._send(200, {'access_token': 'x', 'token_type': 'Bearer',
                                 'id_token': provider.sign(claims)})

            def log_message(self, *args):
                pass

        self.server = http.server.HTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.issuer = f'http://127.0.0.1:{self.server.server_port}/realms/qag'

    def sign(self, claims, key=None):
        return jwt.encode({k: v for k, v in claims.items() if v is not None}, key or self.key, algorithm='RS256',
                          headers={'kid': 'k1'})

    def authorize(self, login_url, **claims):
        """What the provider does once the person signs in: a code for this request, and the ID token it will
        redeem for - with the nonce the request carried, unless the test says otherwise."""
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(login_url).query))
        now = int(time.time())
        full = {'iss': self.issuer, 'aud': CLIENT_ID, 'sub': 'u-alice', 'nonce': query['nonce'], 'iat': now,
                'exp': now + 300, 'preferred_username': 'alice', 'email': 'alice@example.com',
                'email_verified': True, 'groups': ['/qag-admins'], **claims}
        code = os.urandom(8).hex()
        self.codes[code] = (query['code_challenge'], full)
        return code, query['state']


class SsoTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.provider = Provider()

    @classmethod
    def tearDownClass(cls):
        cls.provider.server.shutdown()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {
            'QUERYAPIGATE_HOME': self.tmp.name, 'QUERYAPIGATE_API_KEY': 'admin-key',
            'QUERYAPIGATE_SSO_ISSUER': self.provider.issuer, 'QUERYAPIGATE_SSO_CLIENT_ID': CLIENT_ID,
            'QUERYAPIGATE_SSO_CLIENT_SECRET': CLIENT_SECRET, 'QUERYAPIGATE_SSO_REDIRECT_URL': CALLBACK,
            'QUERYAPIGATE_SSO_ROLES': 'owner=qag-owners,admin=qag-admins,auditor=qag-audit'})
        patcher.start()
        self.addCleanup(patcher.stop)
        oidc.forget()
        self.addCleanup(oidc.forget)
        jwtauth._jwks_clients.clear()
        self.app = create_app()
        self.browser = self.app.test_client()  # Secure cookies need https
        self.browser.environ_base['wsgi.url_scheme'] = 'https'

    def login(self, next_page=None):
        res = self.browser.get('/auth/sso/login' + (f'?next={urllib.parse.quote(next_page)}' if next_page else ''))
        self.assertEqual(res.status_code, 302, res.get_data(as_text=True))
        return res.headers['Location']

    def sign_in(self, next_page=None, **claims):
        """The whole flow; returns the callback's response."""
        code, state = self.provider.authorize(self.login(next_page), **claims)
        return self.browser.get(f'/auth/sso/callback?code={code}&state={state}')

    def csrf(self):
        return self.browser.get('/auth/session').get_json().get('csrf')

    def me(self, **headers):
        return self.browser.get('/api/v1/me', headers=headers)

    def refused(self, res):
        self.assertEqual(res.status_code, 302)
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(res.headers['Location']).query))
        self.assertTrue(res.headers['Location'].startswith('/console/?'), res.headers['Location'])
        return query.get('sso_error')


class FlowTests(SsoTestCase):
    def test_signing_in_creates_the_administrator_and_a_session(self):
        login_url = self.login()
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(login_url).query))
        self.assertTrue(login_url.startswith(self.provider.issuer + '/auth?'))
        self.assertEqual((query['response_type'], query['client_id'], query['redirect_uri'],
                          query['code_challenge_method'], query['scope']),
                         ('code', CLIENT_ID, CALLBACK, 'S256', 'openid email profile'))
        res = self.sign_in()
        self.assertEqual((res.status_code, res.headers['Location']), (302, '/console/'))
        cookie = next(c for c in res.headers.getlist('Set-Cookie') if c.startswith(sso.SESSION_COOKIE + '='))
        for part in ('Secure', 'HttpOnly', 'SameSite=Strict', 'Path=/'):
            self.assertIn(part, cookie)
        self.assertNotIn('Domain=', cookie)
        session = self.browser.get('/auth/session').get_json()
        self.assertEqual((session['signed_in'], session['admin'], session['role']), (True, 'alice', 'admin'))
        me = self.me(**{sso.CSRF_HEADER: session['csrf']}).get_json()
        self.assertEqual((me['name'], me['role'], me['via']), ('alice', 'admin', 'sso'))
        alice = admins.get_admin('alice')
        self.assertEqual((alice['email'], alice['created_by'], alice['sso']['subject']),
                         ('alice@example.com', 'sso', 'u-alice'))
        actions = [(e['action'], e.get('via')) for e in store.read_audit_log()]
        self.assertIn(('create_admin', 'sso'), actions)
        self.assertIn(('sign_in', 'sso'), actions)

    def test_the_token_endpoint_got_the_secret_and_the_pkce_verifier(self):
        self.sign_in()
        form, authorization = self.provider.token_requests[-1]
        self.assertEqual(form['grant_type'], 'authorization_code')
        self.assertIn('code_verifier', form)
        self.assertNotIn('client_secret', form)       # in the Authorization header, not the body
        self.assertTrue(authorization.startswith('Basic '))

    def test_the_next_sign_in_finds_the_same_administrator(self):
        self.sign_in()
        self.sign_in(preferred_username='someone-else', email='new@example.com')
        self.assertEqual([a['name'] for a in admins.list_admins()], ['alice'])  # bound by sub, not by name or email

    def test_a_name_already_taken_gets_a_number(self):
        admins.create_admin('alice', 'developer')
        self.sign_in()
        self.assertEqual(self.browser.get('/auth/session').get_json()['admin'], 'alice-2')
        self.assertIsNone(admins.get_admin('alice')['sso'])  # the existing one is never taken over by its name

    def test_only_console_pages_after_sign_in(self):
        for asked, landed in (('/console/queries/films', '/console/queries/films'),
                              ('https://evil.example.com/', '/console/'), ('//evil.example.com/', '/console/'),
                              ('/console//evil.example.com', '/console/'), ('/api/v1/me', '/console/')):
            with self.subTest(asked=asked):
                self.assertEqual(self.sign_in(next_page=asked).headers['Location'], landed)


class RefusedTests(SsoTestCase):
    def test_a_callback_works_once(self):
        code, state = self.provider.authorize(self.login())
        first = self.browser.get(f'/auth/sso/callback?code={code}&state={state}')
        self.assertEqual(first.headers['Location'], '/console/')
        again = self.browser.get(f'/auth/sso/callback?code={code}&state={state}')
        self.assertEqual(self.refused(again), 'sso_state')

    def test_a_sign_in_started_in_another_browser_cant_finish_in_this_one(self):
        attacker = self.app.test_client()
        attacker.environ_base['wsgi.url_scheme'] = 'https'
        code, state = self.provider.authorize(attacker.get('/auth/sso/login').headers['Location'])
        # the attacker sends the victim the callback link for the attacker's own sign-in
        self.assertEqual(self.refused(self.browser.get(f'/auth/sso/callback?code={code}&state={state}')), 'sso_state')
        self.assertFalse(self.browser.get('/auth/session').get_json()['signed_in'])

    def test_a_sign_in_left_too_long_expires(self):
        code, state = self.provider.authorize(self.login())
        with mock.patch('time.time', return_value=time.time() + sso.LOGIN_TTL + 5):
            res = self.browser.get(f'/auth/sso/callback?code={code}&state={state}')
        self.assertEqual(self.refused(res), 'sso_state')

    def test_every_id_token_not_for_this_sign_in_is_refused(self):
        other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        for label, claims in (('wrong nonce', {'nonce': 'not-the-one'}), ('another client', {'aud': 'other-app'}),
                              ('another issuer', {'iss': 'https://login.example.com'}),
                              ('expired', {'exp': int(time.time()) - 120}), ('no nonce', {'nonce': None}),
                              ('several audiences, azp another', {'aud': [CLIENT_ID, 'x'], 'azp': 'x'})):
            with self.subTest(label):
                self.assertEqual(self.refused(self.sign_in(**claims)), 'sso_id_token')
        with mock.patch.object(self.provider, 'sign', lambda claims, key=None: jwt.encode(
                claims, other_key, algorithm='RS256', headers={'kid': 'k1'})):
            self.assertEqual(self.refused(self.sign_in()), 'sso_id_token')
        self.assertEqual(admins.list_admins(), [])

    def test_no_group_with_a_role_no_sign_in(self):
        self.assertEqual(self.refused(self.sign_in(groups=['/everyone'])), 'sso_no_role')
        self.assertEqual(admins.list_admins(), [])
        entry = next(e for e in store.read_audit_log() if e['action'] == 'sign_in_refused')
        self.assertEqual(entry['changes']['code'], 'sso_no_role')

    def test_the_provider_ending_the_sign_in(self):
        _, state = self.provider.authorize(self.login())
        res = self.browser.get(f'/auth/sso/callback?error=access_denied&state={state}')
        self.assertEqual(self.refused(res), 'sso_denied')


class RoleTests(SsoTestCase):
    def test_the_most_capable_group_wins_and_groups_decide_at_every_sign_in(self):
        self.sign_in(groups=['qag-audit', '/qag-owners'])
        self.assertEqual(admins.get_admin('alice')['role'], 'owner')
        self.sign_in(groups=['qag-audit'])  # moved out of the owners' group at the provider
        self.assertEqual(admins.get_admin('alice')['role'], 'auditor')
        self.assertIn('update_admin', [e['action'] for e in store.read_audit_log()])

    def test_a_default_role_only_for_a_verified_email_at_an_allowed_domain(self):
        env = {'QUERYAPIGATE_SSO_ROLES': '', 'QUERYAPIGATE_SSO_DEFAULT_ROLE': 'auditor',
               'QUERYAPIGATE_SSO_ALLOWED_DOMAINS': 'example.com'}
        with mock.patch.dict(os.environ, env):
            self.assertEqual(self.refused(self.sign_in(groups=None, email_verified=False)), 'sso_no_role')
            self.assertEqual(self.refused(self.sign_in(groups=None, email='eve@elsewhere.com')), 'sso_no_role')
            self.assertEqual(self.sign_in(groups=None).headers['Location'], '/console/')
        self.assertEqual(admins.get_admin('alice')['role'], 'auditor')

    def test_a_switched_off_administrator_cant_sign_in_or_keep_a_session(self):
        self.sign_in()
        csrf = self.csrf()
        admins.update_admin('alice', active=False)
        self.assertEqual(self.me(**{sso.CSRF_HEADER: csrf}).status_code, 401)
        self.assertEqual(self.refused(self.sign_in()), 'sso_inactive')


class SessionTests(SsoTestCase):
    def setUp(self):
        super().setUp()
        self.sign_in()
        self.token = self.csrf()

    def test_the_cookie_never_authenticates_alone(self):
        self.assertEqual(self.me().status_code, 401)                                    # no CSRF token
        self.assertEqual(self.me(**{sso.CSRF_HEADER: 'guess'}).status_code, 401)        # a wrong one
        self.assertEqual(self.browser.post('/api/v1/administrators', json={'name': 'mallory', 'role': 'owner'})
                         .status_code, 401)
        self.assertEqual(self.me(**{sso.CSRF_HEADER: self.token}).status_code, 200)

    def test_a_key_header_is_judged_alone(self):
        res = self.me(**{sso.CSRF_HEADER: self.token, 'X-API-Key': 'wrong'})
        self.assertEqual(res.status_code, 401)

    def test_cross_origin_requests_cant_send_the_csrf_header(self):
        self.assertNotIn(sso.CSRF_HEADER.lower(), cors.ALLOWED_HEADERS.lower())

    def test_sign_out(self):
        self.assertEqual(self.browser.post('/auth/sso/logout').status_code, 401)  # needs the CSRF token too
        res = self.browser.post('/auth/sso/logout', headers={sso.CSRF_HEADER: self.token})
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.get_json()['provider_sign_out'].startswith(self.provider.issuer + '/logout?client_id='))
        self.assertEqual(self.me(**{sso.CSRF_HEADER: self.token}).status_code, 401)
        self.assertIn(('sign_out', 'sso'), [(e['action'], e.get('via')) for e in store.read_audit_log()])

    def test_a_session_ends_when_it_expires(self):
        with mock.patch('time.time', return_value=time.time() + 8 * 3600 + 5):
            self.assertEqual(self.me(**{sso.CSRF_HEADER: self.token}).status_code, 401)

    def test_only_a_hash_of_the_session_is_stored(self):
        from queryapigate import db
        cookie = self.browser.get_cookie(sso.SESSION_COOKIE).value
        stored = [row['id_hash'] for row in db.connection().execute('SELECT id_hash FROM admin_sessions')]
        self.assertNotIn(cookie, stored)
        self.assertIn(hashlib.sha256(cookie.encode()).hexdigest(), stored)

    def test_actions_are_audited_via_sso(self):
        res = self.browser.post('/api/v1/roles', json={'name': 'readers', 'connections': []},
                                headers={sso.CSRF_HEADER: self.token})
        self.assertEqual(res.status_code, 201, res.get_data(as_text=True))
        entry = next(e for e in store.read_audit_log() if e['action'] == 'create_role')
        self.assertEqual((entry['actor'], entry['via']), ('alice', 'sso'))


class OffTests(unittest.TestCase):
    def test_without_the_settings_there_is_no_sign_in(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': home}):
            client = create_app().test_client()
            self.assertEqual(client.get('/auth/sso/login').status_code, 404)
            self.assertEqual(client.get('/auth/session').get_json()['sso'], False)


if __name__ == '__main__':
    unittest.main()

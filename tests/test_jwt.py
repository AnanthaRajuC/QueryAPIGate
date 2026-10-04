"""Tests for signed-in users: bearer tokens (jwtauth.py) and parameters bound to their claims (`from_claim`).
Tokens are signed here - with a shared secret, or an RSA key served from a local JWKS endpoint - so every path,
including every way a token must be refused, runs for real. Runs on whichever metadata backend the suite does."""
import base64
import hashlib
import hmac
import http.server
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest import mock

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from queryapigate import config, create_app, history, jwtauth, mcp_server, metrics, params
from tests.helpers import save_query, write_connections

SECRET = 'a-shared-secret-that-is-long-enough-for-hs256'
JWT_ENV = ('QUERYAPIGATE_JWT_SECRET', 'QUERYAPIGATE_JWT_JWKS_URL', 'QUERYAPIGATE_JWT_ISSUER',
           'QUERYAPIGATE_JWT_AUDIENCE', 'QUERYAPIGATE_JWT_ALGORITHMS', 'QUERYAPIGATE_JWT_ROLE',
           'QUERYAPIGATE_JWT_ROLE_CLAIM', 'QUERYAPIGATE_JWT_USER_CLAIM')


def token(claims=None, key=SECRET, algorithm='HS256', headers=None, **extra):
    body = {'sub': 'alice', 'exp': int(time.time()) + 300, **(claims or {}), **extra}
    return jwt.encode({k: v for k, v in body.items() if v is not None}, key, algorithm=algorithm, headers=headers)


def bearer(value):
    return {'Authorization': f'Bearer {value}'}


class JwtTestCase(unittest.TestCase):
    env = {'QUERYAPIGATE_JWT_SECRET': SECRET, 'QUERYAPIGATE_JWT_ROLE': 'mobile'}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        data = os.path.join(self.tmp.name, 'data.db')
        conn = sqlite3.connect(data)
        conn.execute('CREATE TABLE orders (user_id TEXT, item TEXT)')
        conn.executemany('INSERT INTO orders VALUES (?, ?)', [('alice', 'apples'), ('alice', 'avocados'),
                                                              ('bob', 'bananas'), ('42', 'numbered')])
        conn.commit()
        conn.close()
        env = {name: '' for name in JWT_ENV}
        patcher = mock.patch.dict(os.environ, {**env, 'QUERYAPIGATE_HOME': self.tmp.name,
                                               'QUERYAPIGATE_API_KEY': 'admin', 'QUERYAPIGATE_RATE_LIMIT': '',
                                               **self.env})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(history.flush)
        write_connections({'lite': {'db': 'sqlite', 'database': data, 'active': True}})
        self.app = create_app()
        self.client = self.app.test_client()
        self.admin = {'X-API-Key': 'admin'}
        self.save('my_orders', 'SELECT item FROM orders WHERE user_id = :user_id ORDER BY item',
                  {'user_id': {'type': 'str', 'from_claim': 'sub'}})
        self.save('all_orders', 'SELECT item FROM orders ORDER BY item', {})
        self.role('mobile', queries=['my_orders'])

    def save(self, name, sql, parameters):
        res = save_query(self.client, headers=self.admin, body={
            'author': 'a', 'description': 'd', 'filename': name, 'connection_name': 'lite', 'sql_query': sql,
            'query_parameters': parameters})
        self.assertEqual(res.status_code, 201, res.get_data(as_text=True))

    def role(self, name, **grants):
        res = self.client.post('/api/v1/roles', headers=self.admin, json={'name': name, 'connections': [], **grants})
        self.assertEqual(res.status_code, 201, res.get_data(as_text=True))

    def items(self, res):
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        return [row['item'] for row in res.get_json()]


class VerificationTests(JwtTestCase):
    def test_a_valid_token_runs_the_query_as_that_user(self):
        self.assertEqual(self.items(self.client.get('/q/my_orders', headers=bearer(token()))), ['apples', 'avocados'])
        self.assertEqual(self.items(self.client.get('/q/my_orders', headers=bearer(token(sub='bob')))), ['bananas'])

    def test_every_invalid_token_is_a_plain_401(self):
        now = int(time.time())
        cases = {
            'expired': token(exp=now - 120),
            'not yet valid': token(nbf=now + 600),
            'no expiry': token(exp=None),
            'wrong secret': token(key='another-secret-that-is-also-long-enough-123'),
            'unsigned (alg none)': jwt.encode({'sub': 'alice', 'exp': now + 300}, None, algorithm='none'),
            'wrong algorithm': token(algorithm='HS512'),
            'garbage': 'not.a.token',
            'no user claim': token(sub=None),
        }
        for label, value in cases.items():
            res = self.client.get('/q/my_orders', headers=bearer(value))
            self.assertEqual((res.status_code, res.get_json()), (401, {'error': 'Unauthorized'}), label)
        self.assertEqual(self.client.get('/q/my_orders', headers={'Authorization': 'Basic abc'}).status_code, 401)

    def test_clock_skew_within_the_leeway_is_tolerated(self):
        res = self.client.get('/q/my_orders', headers=bearer(token(exp=int(time.time()) - 10)))
        self.assertEqual(res.status_code, 200)

    def test_with_jwt_on_and_no_api_keys_anonymous_callers_are_refused(self):
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_API_KEY': ''}):
            self.assertEqual(self.client.get('/q/all_orders').status_code, 401)

    def test_an_api_key_header_is_judged_alone(self):
        res = self.client.get('/q/my_orders', headers={'X-API-Key': 'wrong', **bearer(token())})
        self.assertEqual(res.status_code, 401)  # a wrong key never falls through to the token

    def test_the_role_s_grants_apply(self):
        self.assertEqual(self.client.get('/q/all_orders', headers=bearer(token())).status_code, 403)
        self.assertEqual(self.client.get('/api/v1/connections', headers=bearer(token())).status_code, 403)

    def test_a_token_mapping_to_no_existing_role_is_refused(self):
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_JWT_ROLE': 'nope'}):
            self.assertEqual(self.client.get('/q/my_orders', headers=bearer(token())).status_code, 401)

    def test_the_role_claim_picks_the_first_existing_role(self):
        self.role('analyst', queries=['all_orders', 'my_orders'])
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_JWT_ROLE_CLAIM': 'app_metadata.roles'}):
            value = token(app_metadata={'roles': ['unknown', 'analyst']})
            self.assertEqual(len(self.items(self.client.get('/q/all_orders', headers=bearer(value)))), 4)
            fallback = token()  # no role claim: QUERYAPIGATE_JWT_ROLE
            self.assertEqual(self.client.get('/q/all_orders', headers=bearer(fallback)).status_code, 403)

    def test_issuer_and_audience_are_enforced_when_set(self):
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_JWT_ISSUER': 'https://login.example.com/',
                                          'QUERYAPIGATE_JWT_AUDIENCE': 'queryapigate, other-api'}):
            good = token(iss='https://login.example.com/', aud='queryapigate')
            self.assertEqual(self.client.get('/q/my_orders', headers=bearer(good)).status_code, 200)
            for label, value in (('wrong issuer', token(iss='https://evil.example.com/', aud='queryapigate')),
                                 ('wrong audience', token(iss='https://login.example.com/', aud='someone-else')),
                                 ('no audience', token(iss='https://login.example.com/'))):
                self.assertEqual(self.client.get('/q/my_orders', headers=bearer(value)).status_code, 401, label)

    def test_the_role_s_allowed_ips_apply_to_signed_in_users(self):
        self.role('office', queries=['my_orders'], allowed_ips=['10.0.0.0/8'])
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_JWT_ROLE': 'office'}):
            self.assertEqual(self.client.get('/q/my_orders', headers=bearer(token())).status_code, 401)
            res = self.client.get('/q/my_orders', headers=bearer(token()), environ_base={'REMOTE_ADDR': '10.1.2.3'})
            self.assertEqual(res.status_code, 200)

    def test_the_role_s_rate_limit_is_counted_per_user(self):
        self.role('limited', queries=['my_orders'], rate_limit='2/minute')
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_JWT_ROLE': 'limited'}):
            alice = [self.client.get('/q/my_orders', headers=bearer(token())).status_code for _ in range(3)]
            bob = self.client.get('/q/my_orders', headers=bearer(token(sub='bob'))).status_code
        self.assertEqual((alice, bob), ([200, 200, 429], 200))

    def test_history_names_the_user_and_metrics_do_not(self):
        self.client.get('/q/my_orders', headers=bearer(token(sub='carol')))
        entries = self.client.get('/api/v1/history?key=jwt:carol', headers=self.admin).get_json()['items']
        self.assertEqual([e['key_name'] for e in entries], ['jwt:carol'])
        labels = {key[3] for key in metrics._request_counts}
        self.assertIn('jwt', labels)
        self.assertFalse(any(isinstance(label, str) and label.startswith('jwt:') for label in labels))


class ClaimParameterTests(JwtTestCase):
    def test_a_user_cannot_choose_the_value(self):
        for source in ({'query_string': {'user_id': 'bob'}},
                       {'method': 'POST', 'json': {'params': {'user_id': 'bob'}}}):
            method = source.pop('method', 'GET')
            res = self.client.open('/q/my_orders', method=method, headers=bearer(token()), **source)
            self.assertEqual(res.status_code, 400)
            self.assertIn('comes from your sign-in token', res.get_json()['error'])

    def test_a_token_without_the_claim_is_refused(self):
        self.save('by_tenant', 'SELECT item FROM orders WHERE user_id = :tenant', {'tenant': {'from_claim': 'tenant'}})
        self.role('tenant-role', queries=['by_tenant'])
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_JWT_ROLE': 'tenant-role'}):
            res = self.client.get('/q/by_tenant', headers=bearer(token()))
            self.assertEqual(res.status_code, 403)
            self.assertIn("'tenant'", res.get_json()['error'])

    def test_a_numeric_claim_binds_to_a_text_parameter(self):
        # (`sub` itself must be a string - PyJWT refuses a numeric one - but custom claims such as a uid needn't be)
        self.save('by_uid', 'SELECT item FROM orders WHERE user_id = :uid ORDER BY item',
                  {'uid': {'from_claim': 'uid'}})
        self.role('uid-role', queries=['by_uid'])
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_JWT_ROLE': 'uid-role'}):
            self.assertEqual(self.items(self.client.get('/q/by_uid', headers=bearer(token(uid=42)))), ['numbered'])

    def test_api_keys_cannot_run_it_but_the_admin_key_can_supply_the_value(self):
        key = self.client.post('/api/v1/api-keys', headers=self.admin, json={
            'name': 'partner', 'connections': [], 'queries': ['my_orders']}).get_json()['secret']
        self.assertEqual(self.client.get('/q/my_orders?user_id=bob', headers={'X-API-Key': key}).status_code, 403)
        self.assertEqual(self.items(self.client.get('/q/my_orders?user_id=bob', headers=self.admin)), ['bananas'])

    def test_mcp_callers_with_an_api_key_are_refused_too(self):
        key = self.client.post('/api/v1/api-keys', headers=self.admin,
                               json={'name': 'agent', 'connections': [], 'queries': ['my_orders']}).get_json()['secret']
        from queryapigate import apikeys
        permission = apikeys.authenticate(key)
        result = mcp_server.call_tool_for(self.app, permission, 'my_orders', {'user_id': 'bob'})
        self.assertTrue(result['isError'])
        self.assertIn("signed-in user's token", result['content'][0]['text'])
        self.assertNotIn('bananas', json.dumps(result))

    def test_the_parameter_is_not_advertised_to_callers(self):
        spec = self.client.get('/openapi.json', headers=self.admin).get_json()
        operation = spec['paths']['/q/my_orders']['get']
        self.assertNotIn('user_id', [p['name'] for p in operation.get('parameters', [])])
        self.assertIn('BearerAuth', spec['components']['securitySchemes'])
        saved = {'sql_query': 'SELECT 1 WHERE :user_id', 'query_parameters': {'user_id': {'from_claim': 'sub'}}}
        self.assertEqual(mcp_server.input_schema(saved)['properties'], {})

    def test_definition_rules(self):
        for spec in ({'from_claim': 'sub', 'default': 'x'}, {'from_claim': 'sub', 'required': False},
                     {'from_claim': ''}, {'from_claim': 7}):
            res = save_query(self.client, headers=self.admin, body={
                'author': 'a', 'description': 'd', 'filename': 'bad', 'connection_name': 'lite',
                'sql_query': 'SELECT :p', 'query_parameters': {'p': spec}})
            self.assertEqual(res.status_code, 400, spec)

    def test_an_unusable_definition_still_fails_closed(self):
        """A stored definition that no longer parses must not lose its from_claim and become client-supplied."""
        spec = params.read_definition({'from_claim': 'sub', 'type': 'not-a-type'})
        self.assertEqual(spec['from_claim'], 'sub')


class JwksTests(unittest.TestCase):
    """RS256 against a local JWKS endpoint, as with an identity provider."""

    @classmethod
    def setUpClass(cls):
        cls.keys = {'k1': rsa.generate_private_key(public_exponent=65537, key_size=2048),
                    'k2': rsa.generate_private_key(public_exponent=65537, key_size=2048)}
        cls.published = ['k1']
        cls.fetches = 0

        def jwk(kid):
            numbers = cls.keys[kid].public_key().public_numbers()

            def b64(n):
                return base64.urlsafe_b64encode(n.to_bytes((n.bit_length() + 7) // 8, 'big')).rstrip(b'=').decode()
            return {'kty': 'RSA', 'kid': kid, 'use': 'sig', 'alg': 'RS256', 'n': b64(numbers.n), 'e': b64(numbers.e)}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                cls.fetches += 1
                body = json.dumps({'keys': [jwk(kid) for kid in cls.published]}).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass
        cls.server = http.server.HTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f'http://127.0.0.1:{cls.server.server_port}/.well-known/jwks.json'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        env = {name: '' for name in JWT_ENV}
        patcher = mock.patch.dict(os.environ, {**env, 'QUERYAPIGATE_JWT_JWKS_URL': self.url,
                                               'QUERYAPIGATE_JWT_ISSUER': 'https://login.example.com/',
                                               'QUERYAPIGATE_JWT_AUDIENCE': 'queryapigate'})
        patcher.start()
        self.addCleanup(patcher.stop)
        jwtauth._jwks_clients.clear()
        self.addCleanup(jwtauth._jwks_clients.clear)

    def sign(self, kid, **claims):
        return token({'iss': 'https://login.example.com/', 'aud': 'queryapigate', **claims}, key=self.keys[kid],
                     algorithm='RS256', headers={'kid': kid})

    def test_a_token_signed_by_a_published_key_verifies(self):
        self.assertEqual(jwtauth.verify(self.sign('k1'))['sub'], 'alice')

    def test_an_unknown_key_is_refused_and_a_rotated_in_one_is_picked_up(self):
        self.assertIsNone(jwtauth.verify(self.sign('k2')))
        type(self).published = ['k1', 'k2']  # the provider rotates a new key in
        try:
            self.assertIsNone(jwtauth.verify(self.sign('k2')))  # within PyJWT's refetch cooldown: not fetched yet
            jwtauth._jwks_client(self.url).cooldown_duration = 0  # ...the cooldown passes
            self.assertEqual(jwtauth.verify(self.sign('k2'))['sub'], 'alice')
        finally:
            type(self).published = ['k1']

    def test_made_up_key_ids_cannot_make_the_server_hammer_the_provider(self):
        self.assertEqual(jwtauth.verify(self.sign('k1'))['sub'], 'alice')  # keys fetched once
        before = type(self).fetches
        for i in range(20):
            forged = token({'iss': 'https://login.example.com/', 'aud': 'queryapigate'}, key=self.keys['k2'],
                           algorithm='RS256', headers={'kid': f'made-up-{i}'})
            self.assertIsNone(jwtauth.verify(forged))
        self.assertLessEqual(type(self).fetches - before, 1)

    def test_algorithm_confusion_is_refused(self):
        """An HMAC token "signed" with the provider's public key as its secret - the classic attack on servers that
        let the token pick its own algorithm."""
        public_pem = self.keys['k1'].public_key().public_bytes(serialization.Encoding.PEM,
                                                               serialization.PublicFormat.SubjectPublicKeyInfo)
        # PyJWT itself refuses to HMAC-sign with a PEM key, so the token is built by hand.
        header = base64.urlsafe_b64encode(json.dumps({'alg': 'HS256', 'typ': 'JWT', 'kid': 'k1'}).encode()).rstrip(b'=')
        payload = base64.urlsafe_b64encode(json.dumps({'sub': 'mallory', 'exp': int(time.time()) + 300,
                                                       'iss': 'https://login.example.com/',
                                                       'aud': 'queryapigate'}).encode()).rstrip(b'=')
        signature = base64.urlsafe_b64encode(hmac.new(public_pem, header + b'.' + payload, hashlib.sha256).digest())
        forged = (header + b'.' + payload + b'.' + signature.rstrip(b'=')).decode()
        self.assertIsNone(jwtauth.verify(forged))

    def test_wrong_audience_from_the_same_provider_is_refused(self):
        self.assertIsNone(jwtauth.verify(self.sign('k1', aud='some-other-app')))


class SettingsTests(unittest.TestCase):
    def check(self, **env):
        base = {name: '' for name in JWT_ENV}
        with mock.patch.dict(os.environ, {**base, **env}):
            config.check_settings()

    def test_unsafe_or_incomplete_configurations_stop_startup(self):
        cases = {
            'no way to verify': {'QUERYAPIGATE_JWT_ROLE': 'mobile'},
            'both': {'QUERYAPIGATE_JWT_SECRET': SECRET, 'QUERYAPIGATE_JWT_JWKS_URL': 'https://x/jwks.json',
                     'QUERYAPIGATE_JWT_ROLE': 'mobile'},
            'short secret': {'QUERYAPIGATE_JWT_SECRET': 'short', 'QUERYAPIGATE_JWT_ROLE': 'mobile'},
            'no role': {'QUERYAPIGATE_JWT_SECRET': SECRET},
            'jwks without issuer/audience': {'QUERYAPIGATE_JWT_JWKS_URL': 'https://login.example.com/jwks.json',
                                             'QUERYAPIGATE_JWT_ROLE': 'mobile'},
            'jwks over plain http': {'QUERYAPIGATE_JWT_JWKS_URL': 'http://login.example.com/jwks.json',
                                     'QUERYAPIGATE_JWT_ISSUER': 'i', 'QUERYAPIGATE_JWT_AUDIENCE': 'a',
                                     'QUERYAPIGATE_JWT_ROLE': 'mobile'},
            'hmac with jwks': {'QUERYAPIGATE_JWT_JWKS_URL': 'https://login.example.com/jwks.json',
                               'QUERYAPIGATE_JWT_ISSUER': 'i', 'QUERYAPIGATE_JWT_AUDIENCE': 'a',
                               'QUERYAPIGATE_JWT_ROLE': 'mobile', 'QUERYAPIGATE_JWT_ALGORITHMS': 'RS256,HS256'},
            'rsa with a secret': {'QUERYAPIGATE_JWT_SECRET': SECRET, 'QUERYAPIGATE_JWT_ROLE': 'mobile',
                                  'QUERYAPIGATE_JWT_ALGORITHMS': 'RS256'},
            'none': {'QUERYAPIGATE_JWT_SECRET': SECRET, 'QUERYAPIGATE_JWT_ROLE': 'mobile',
                     'QUERYAPIGATE_JWT_ALGORITHMS': 'none'},
        }
        for label, env in cases.items():
            with self.assertRaises(ValueError, msg=label):
                self.check(**env)

    def test_good_configurations_are_accepted(self):
        self.check(QUERYAPIGATE_JWT_SECRET=SECRET, QUERYAPIGATE_JWT_ROLE='mobile')
        self.check(QUERYAPIGATE_JWT_JWKS_URL='https://login.example.com/.well-known/jwks.json',
                   QUERYAPIGATE_JWT_ISSUER='https://login.example.com/', QUERYAPIGATE_JWT_AUDIENCE='api',
                   QUERYAPIGATE_JWT_ROLE_CLAIM='roles')
        self.check()  # off entirely


if __name__ == '__main__':
    unittest.main()


class EventsServerTests(JwtTestCase):
    """Each signed-in user's live stream from `queryapigate events` carries only their own runs."""

    def setUp(self):
        super().setUp()
        import asyncio

        from queryapigate import events
        from tests.test_events_server import Stream
        self.events, self.Stream = events, Stream
        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever, daemon=True)
        thread.start()
        server = events.EventServer('127.0.0.1', 0, poll_interval=0.05)
        asyncio.run_coroutine_threadsafe(server.start(), loop).result(10)
        self.server = server

        def stop():
            asyncio.run_coroutine_threadsafe(server.stop(), loop).result(10)
            loop.call_soon_threadsafe(loop.stop)
            thread.join(5)
            loop.close()
        self.addCleanup(stop)
        patcher = mock.patch.dict(os.environ, {'QUERYAPIGATE_HISTORY_FLUSH_INTERVAL': '0'})
        patcher.start()
        self.addCleanup(patcher.stop)

    def open(self, value):
        stream = self.Stream(self.server.port, headers=bearer(value))
        self.addCleanup(stream.close)
        return stream

    def test_a_user_s_stream_carries_only_their_own_runs(self):
        alice = self.open(token())
        self.assertEqual(alice.status, 200)
        self.client.get('/q/my_orders', headers=bearer(token(sub='bob')))
        self.client.get('/q/my_orders', headers=bearer(token()))
        (_, payload), = alice.events(1)
        self.assertEqual(payload['entry']['key_name'], 'jwt:alice')
        self.assertTrue(alice.nothing_more())

    def test_an_expired_token_is_refused(self):
        self.assertEqual(self.open(token(exp=int(time.time()) - 120)).status, 401)

    def test_the_stream_closes_once_the_token_expires(self):
        with mock.patch.object(jwtauth, '_LEEWAY', 0), mock.patch.object(self.events, '_RECHECK_SECONDS', 0.2), \
                mock.patch.object(self.events, '_HEARTBEAT_SECONDS', 0.1):
            stream = self.open(token(exp=int(time.time()) + 2))
            self.assertEqual(stream.status, 200)
            self.assertTrue(stream.closed(timeout=8))

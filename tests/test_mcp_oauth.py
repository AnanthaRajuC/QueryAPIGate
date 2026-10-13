"""OAuth for the MCP server (ADR 0006, BACKLOG #85): /mcp as an OAuth 2.1 resource server, with the organisation's
identity provider as its authorization server. Tokens are signed here with RSA keys a local stand-in for the
provider publishes - its OpenID Connect metadata and JWKS - so every way a token must be refused runs for real:
another audience (REST's), another issuer, an unknown key, an HMAC forgery, no scope, no role. The HTTP-level
answers (401/403 with WWW-Authenticate, the metadata routes) are checked through the real ASGI app, and a real MCP
client calls a tool with a token."""
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
from cryptography.hazmat.primitives.asymmetric import rsa

from queryapigate import apikeys, config, create_app, governance, jwtauth, masking, mcp_server, oidc
from tests.helpers import save_query, write_connections

try:
    import mcp  # noqa: F401
    HAVE_MCP_SDK = True
except ImportError:
    HAVE_MCP_SDK = False

RESOURCE = 'http://127.0.0.1:5001/mcp'
CLEARED = {name: '' for name in ('QUERYAPIGATE_JWT_SECRET', 'QUERYAPIGATE_JWT_JWKS_URL', 'QUERYAPIGATE_JWT_ISSUER',
                                 'QUERYAPIGATE_JWT_AUDIENCE', 'QUERYAPIGATE_JWT_ALGORITHMS', 'QUERYAPIGATE_JWT_ROLE',
                                 'QUERYAPIGATE_JWT_ROLE_CLAIM', 'QUERYAPIGATE_JWT_USER_CLAIM',
                                 *config._MCP_OAUTH_VARS)}


def b64(n):
    return base64.urlsafe_b64encode(n.to_bytes((n.bit_length() + 7) // 8, 'big')).rstrip(b'=').decode()


class Provider:
    """A stand-in identity provider on a local port, laid out like Keycloak: issuer /realms/qag, its OpenID Connect
    metadata appended to that path, keys at /realms/qag/certs."""

    def __init__(self):
        self.keys = {'k1': rsa.generate_private_key(public_exponent=65537, key_size=2048)}
        self.metadata_fetches = 0
        provider = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == '/realms/qag/.well-known/openid-configuration':
                    provider.metadata_fetches += 1
                    body = {'issuer': provider.issuer, 'jwks_uri': provider.issuer + '/certs'}
                elif self.path == '/realms/qag/certs':
                    body = {'keys': [provider.jwk(kid) for kid in provider.keys]}
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = http.server.HTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.issuer = f'http://127.0.0.1:{self.server.server_port}/realms/qag'

    def jwk(self, kid):
        numbers = self.keys[kid].public_key().public_numbers()
        return {'kty': 'RSA', 'kid': kid, 'use': 'sig', 'alg': 'RS256', 'n': b64(numbers.n), 'e': b64(numbers.e)}

    def token(self, kid='k1', key=None, **claims):
        body = {'iss': self.issuer, 'aud': RESOURCE, 'sub': 'alice', 'scope': 'openid queryapigate.mcp',
                'exp': int(time.time()) + 300, **claims}
        return jwt.encode({k: v for k, v in body.items() if v is not None}, key or self.keys[kid], algorithm='RS256',
                          headers={'kid': kid})


class McpOAuthTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.provider = Provider()

    @classmethod
    def tearDownClass(cls):
        cls.provider.server.shutdown()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        data = os.path.join(self.tmp.name, 'data.db')
        conn = sqlite3.connect(data)
        conn.execute('CREATE TABLE customer (id INTEGER, email TEXT)')
        conn.executemany('INSERT INTO customer VALUES (?, ?)', [(1, 'ann@example.com'), (2, 'bob@example.com')])
        conn.commit()
        conn.close()
        patcher = mock.patch.dict(os.environ, {
            **CLEARED, 'QUERYAPIGATE_HOME': self.tmp.name, 'QUERYAPIGATE_API_KEY': 'admin-key',
            'QUERYAPIGATE_MCP_RESOURCE_URL': RESOURCE, 'QUERYAPIGATE_MCP_AUTH_SERVER': self.provider.issuer,
            'QUERYAPIGATE_MCP_ROLE': 'agents', 'QUERYAPIGATE_MCP_ROLE_CLAIM': 'qag_role'})
        patcher.start()
        self.addCleanup(patcher.stop)
        jwtauth._jwks_clients.clear()
        self.addCleanup(jwtauth._jwks_clients.clear)
        oidc.forget()
        self.addCleanup(oidc.forget)
        write_connections({'shop': {'db': 'sqlite', 'database': data, 'active': True}})
        masking.forget_schema()  # another test's 'shop' may still be cached
        self.app = create_app()
        self.client = self.app.test_client()
        admin = {'X-API-Key': 'admin-key'}
        self.client.post('/api/v1/roles', headers=admin, json={
            'name': 'agents', 'connections': ['shop'], 'mask': [{'column': 'customer.email', 'action': 'hash'}]})
        self.client.post('/api/v1/roles', headers=admin, json={'name': 'analysts', 'connections': ['shop']})
        res = save_query(self.client, {'author': 'a', 'description': 'Customers', 'connection_name': 'shop',
                                       'sql_query': 'SELECT id, email FROM customer ORDER BY id',
                                       'filename': 'customers'}, headers=admin)
        self.assertEqual(res.status_code, 201, res.get_data(as_text=True))

    def authenticate(self, token):
        return governance.authenticate_mcp('', f'Bearer {token}', '10.0.0.1')


class TokenTests(McpOAuthTestCase):
    def test_a_token_for_the_mcp_server_lets_the_agent_in_with_its_role(self):
        permission, refusal = self.authenticate(self.provider.token())
        self.assertIsNone(refusal)
        self.assertEqual(permission.name, 'jwt:alice')
        self.assertEqual(set(permission.connections), {'shop'})
        self.assertTrue(permission.mask)  # the role's masking rules come with it
        analyst, _ = self.authenticate(self.provider.token(qag_role='analysts'))
        self.assertFalse(analyst.mask)    # the role claim picks another role

    def test_every_token_not_issued_for_it_is_a_401(self):
        other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        for label, token in (
                ('REST audience', self.provider.token(aud='queryapigate')),
                ('no audience', self.provider.token(aud=None)),
                ('audience of the issuer', self.provider.token(aud=self.provider.issuer)),
                ('another issuer', self.provider.token(iss='https://login.example.com/')),
                ('expired', self.provider.token(exp=int(time.time()) - 120)),
                ('no expiry', self.provider.token(exp=None)),
                ('unknown key', self.provider.token(kid='k9', key=other)),
                ('wrong key, known id', self.provider.token(key=other)),
                ('not a token', 'not-a-token')):
            with self.subTest(label):
                permission, refusal = self.authenticate(token)
                self.assertIsNone(permission)
                self.assertEqual((refusal.status, refusal.code), (401, 'unauthorized'))

    def test_an_hmac_token_signed_with_the_public_key_is_refused(self):
        from cryptography.hazmat.primitives import serialization
        public_pem = self.provider.keys['k1'].public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)

        def part(value):
            return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b'=')
        header = part({'alg': 'HS256', 'typ': 'JWT', 'kid': 'k1'})
        payload = part({'iss': self.provider.issuer, 'aud': RESOURCE, 'sub': 'mallory', 'scope': 'queryapigate.mcp',
                        'exp': int(time.time()) + 300})
        signature = base64.urlsafe_b64encode(hmac.new(public_pem, header + b'.' + payload, hashlib.sha256).digest())
        forged = (header + b'.' + payload + b'.' + signature.rstrip(b'=')).decode()
        self.assertEqual(self.authenticate(forged)[1].status, 401)

    def test_a_valid_token_without_the_scope_or_a_role_is_a_403(self):
        for label, token, code in (
                ('no scope', self.provider.token(scope='openid profile'), 'insufficient_scope'),
                ('no role', self.provider.token(qag_role='nobody'), None)):
            with self.subTest(label):
                if code is None:  # the claim names no role, and the fixed role is gone too
                    self.client.delete('/api/v1/roles/agents', headers={'X-API-Key': 'admin-key'})
                    code = 'forbidden'
                permission, refusal = self.authenticate(token)
                self.assertIsNone(permission)
                self.assertEqual((refusal.status, refusal.code), (403, code))

    def test_scopes_as_providers_send_them(self):
        for label, claims in (('scp list', {'scope': None, 'scp': ['queryapigate.mcp']}),
                              ('scp string', {'scope': None, 'scp': 'openid queryapigate.mcp'})):
            with self.subTest(label):
                self.assertIsNone(self.authenticate(self.provider.token(**claims))[1])
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_MCP_SCOPE': 'api://queryapigate/queryapigate.mcp'}):
            self.assertIsNone(self.authenticate(self.provider.token())[1])  # Azure-style: the local name in the token
            self.assertEqual(self.authenticate(self.provider.token(scope='queryapigate'))[1].code, 'insufficient_scope')

    def test_an_agent_is_named_apart_from_rest_users_of_another_provider(self):
        self.assertEqual(self.authenticate(self.provider.token())[0].name, 'jwt:alice')  # REST signs nobody in
        other = {'QUERYAPIGATE_JWT_SECRET': 'x' * 40, 'QUERYAPIGATE_JWT_ROLE': 'analysts'}
        with mock.patch.dict(os.environ, other):  # REST's users come from elsewhere: 'alice' may be someone else
            self.assertEqual(self.authenticate(self.provider.token())[0].name, 'mcp:alice')
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_JWT_JWKS_URL': self.provider.issuer + '/certs',
                                          'QUERYAPIGATE_JWT_ISSUER': self.provider.issuer,
                                          'QUERYAPIGATE_JWT_AUDIENCE': 'queryapigate',
                                          'QUERYAPIGATE_JWT_ROLE': 'analysts'}):  # the same provider: one person
            self.assertEqual(self.authenticate(self.provider.token())[0].name, 'jwt:alice')

    def test_a_rest_token_is_refused_on_mcp_and_an_mcp_token_on_rest(self):
        jwks = self.provider.issuer + '/certs'
        rest = {'QUERYAPIGATE_JWT_JWKS_URL': jwks, 'QUERYAPIGATE_JWT_ISSUER': self.provider.issuer,
                'QUERYAPIGATE_JWT_AUDIENCE': 'queryapigate', 'QUERYAPIGATE_JWT_ROLE': 'analysts'}
        with mock.patch.dict(os.environ, rest):
            rest_token = self.provider.token(aud='queryapigate')
            self.assertEqual(self.client.get('/q/customers', headers={'Authorization': f'Bearer {rest_token}'})
                             .status_code, 200)
            self.assertEqual(self.authenticate(rest_token)[1].status, 401)  # valid for REST, not for /mcp
            both = self.provider.token(aud=['queryapigate', RESOURCE])  # names both: REST still refuses it
            self.assertEqual(self.client.get('/q/customers', headers={'Authorization': f'Bearer {both}'})
                             .status_code, 401)
            mcp_token = self.provider.token()
            self.assertEqual(self.client.get('/q/customers', headers={'Authorization': f'Bearer {mcp_token}'})
                             .status_code, 401)

    def test_api_keys_still_work_and_a_wrong_one_never_falls_through_to_the_token(self):
        permission, _ = governance.authenticate_mcp('admin-key', '', '10.0.0.1')
        self.assertTrue(permission.admin)
        permission, refusal = governance.authenticate_mcp('wrong', f'Bearer {self.provider.token()}', '10.0.0.1')
        self.assertEqual((permission, refusal.status), (None, 401))

    def test_with_oauth_on_nobody_gets_in_without_a_credential(self):
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_API_KEY': ''}):
            self.assertTrue(apikeys.auth_required())
            self.assertEqual(governance.authenticate_mcp('', '', '10.0.0.1')[1].status, 401)

    def test_the_provider_s_metadata_is_fetched_once(self):
        before = self.provider.metadata_fetches
        for _ in range(5):
            self.assertIsNone(self.authenticate(self.provider.token())[1])
        self.assertEqual(self.provider.metadata_fetches - before, 1)

    def test_metadata_naming_another_issuer_is_not_used(self):
        with mock.patch.object(oidc, 'fetch_json', return_value={'issuer': 'https://evil.example.com',
                                                                     'jwks_uri': 'https://evil.example.com/k'}):
            self.assertIsNone(oidc.jwks_uri(self.provider.issuer))
            self.assertEqual(self.authenticate(self.provider.token())[1].status, 401)

    def test_a_tool_call_runs_as_the_agent_masked_by_its_role(self):
        result = mcp_server.handle_call(self.app, '', f'Bearer {self.provider.token()}', '10.0.0.1', 'customers', {})
        self.assertFalse(result.get('isError'), result)
        emails = [row['email'] for row in result['structuredContent']['rows']]
        self.assertNotIn('ann@example.com', emails)
        runs = self.client.get('/api/v1/queries/customers/history', headers={'X-API-Key': 'admin-key'})
        self.assertEqual(runs.get_json()['items'][0]['key_name'], 'jwt:alice')


class RedirectTests(unittest.TestCase):
    def test_metadata_redirected_off_https_is_not_used(self):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.geturl.return_value = 'http://evil.example.com/.well-known/openid-configuration'
        response.read.return_value = b'{}'
        with mock.patch('urllib.request.urlopen', return_value=response), self.assertRaises(ValueError):
            oidc.fetch_json('https://idp.example.com/.well-known/openid-configuration')

    def test_signing_keys_are_never_fetched_through_a_redirect(self):
        """The keys come through PyJWT's client, which follows no redirect at all - from 2.14, the floor CI tests. A
        jwks_uri sent elsewhere (to plain HTTP, say) yields no key, so every token is refused (ADR 0017's review)."""
        def serve(handler):
            server = http.server.HTTPServer(('127.0.0.1', 0), handler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.shutdown)
            return f'http://127.0.0.1:{server.server_port}'

        class Quiet(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

        numbers = rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key().public_numbers()
        jwk = {'kty': 'RSA', 'kid': 'k1', 'use': 'sig', 'alg': 'RS256', 'n': b64(numbers.n), 'e': b64(numbers.e)}

        class Keys(Quiet):  # what a redirect followed would get: a key set that works
            def do_GET(self):
                body = json.dumps({'keys': [jwk]}).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(body)

        keys = serve(Keys)
        jwt.PyJWKClient(keys + '/certs', cache_keys=False).fetch_data()  # fetched directly, they're fine

        class Redirect(Quiet):
            def do_GET(self):
                self.send_response(302)
                self.send_header('Location', keys + '/certs')
                self.end_headers()

        with self.assertRaises(jwt.PyJWKClientConnectionError):
            jwt.PyJWKClient(serve(Redirect) + '/certs', cache_keys=False).fetch_data()


class DiscoveryTests(unittest.TestCase):
    def test_where_metadata_is_looked_for(self):
        self.assertEqual(oidc.discovery_urls('https://idp.example.com/realms/qag'), [
            'https://idp.example.com/.well-known/oauth-authorization-server/realms/qag',
            'https://idp.example.com/.well-known/openid-configuration/realms/qag',
            'https://idp.example.com/realms/qag/.well-known/openid-configuration'])
        self.assertEqual(oidc.discovery_urls('https://tenant.auth0.com/'), [
            'https://tenant.auth0.com/.well-known/oauth-authorization-server',
            'https://tenant.auth0.com/.well-known/openid-configuration'])


class SettingsTests(unittest.TestCase):
    def check(self, **env):
        with mock.patch.dict(os.environ, {**CLEARED, **env}):
            config.check_settings()

    def test_unsafe_or_incomplete_configurations_stop_startup(self):
        good = {'QUERYAPIGATE_MCP_RESOURCE_URL': 'https://data.example.com/mcp',
                'QUERYAPIGATE_MCP_AUTH_SERVER': 'https://idp.example.com/realms/qag',
                'QUERYAPIGATE_MCP_ROLE': 'agents'}
        self.check(**good)
        for label, env in (
                ('no auth server', {**good, 'QUERYAPIGATE_MCP_AUTH_SERVER': ''}),
                ('only a role', {'QUERYAPIGATE_MCP_ROLE': 'agents'}),
                ('plain http', {**good, 'QUERYAPIGATE_MCP_AUTH_SERVER': 'http://idp.example.com/'}),
                ('query string', {**good, 'QUERYAPIGATE_MCP_RESOURCE_URL': 'https://data.example.com/mcp?x=1'}),
                ('no role', {**good, 'QUERYAPIGATE_MCP_ROLE': ''}),
                ('shared audience', {**good, 'QUERYAPIGATE_JWT_SECRET': 'x' * 40, 'QUERYAPIGATE_JWT_ROLE': 'r',
                                     'QUERYAPIGATE_JWT_AUDIENCE': 'queryapigate,https://data.example.com/mcp'})):
            with self.subTest(label), self.assertRaises(ValueError):
                self.check(**env)


@unittest.skipUnless(HAVE_MCP_SDK, 'the mcp package is not installed')
class HttpTests(McpOAuthTestCase):
    """The answers an OAuth client acts on, through the real ASGI app."""

    def setUp(self):
        super().setUp()
        from starlette.testclient import TestClient
        self.http = TestClient(mcp_server.build_asgi_app(self.app))
        self.http.__enter__()
        self.addCleanup(self.http.__exit__, None, None, None)

    def post(self, headers=None):
        return self.http.post('/mcp/', headers={'Accept': 'application/json, text/event-stream', **(headers or {})},
                              json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})

    def test_its_metrics_need_the_token_too(self):
        token = 't' * 32
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_METRICS_TOKEN': token}):
            refused = self.http.get('/metrics')
            self.assertEqual(refused.status_code, 401)
            self.assertEqual(refused.headers['WWW-Authenticate'], 'Bearer realm="metrics"')
            self.assertEqual(self.http.get('/metrics', headers={'Authorization': f'Bearer {token}'}).status_code, 200)
        self.assertEqual(self.http.get('/metrics').status_code, 200)  # unset: open, as before

    def test_the_protected_resource_metadata(self):
        for path in ('/.well-known/oauth-protected-resource', '/.well-known/oauth-protected-resource/mcp'):
            with self.subTest(path):
                res = self.http.get(path)
                self.assertEqual(res.status_code, 200)
                self.assertEqual(res.json(), {'resource': RESOURCE, 'authorization_servers': [self.provider.issuer],
                                              'scopes_supported': ['queryapigate.mcp'],
                                              'bearer_methods_supported': ['header'], 'resource_name': 'QueryAPIGate'})

    def test_no_credential_is_a_401_pointing_at_the_metadata(self):
        res = self.post()
        self.assertEqual(res.status_code, 401)
        self.assertEqual(res.json()['code'], 'unauthorized')
        self.assertEqual(res.json()['request_id'], res.headers['X-Request-Id'])
        self.assertEqual(res.headers['WWW-Authenticate'],
                         'Bearer resource_metadata="http://127.0.0.1:5001/.well-known/oauth-protected-resource/mcp", '
                         'scope="queryapigate.mcp"')

    def test_a_bad_token_and_a_missing_scope(self):
        res = self.post({'Authorization': f'Bearer {self.provider.token(aud="queryapigate")}'})
        self.assertEqual(res.status_code, 401)
        self.assertTrue(res.headers['WWW-Authenticate'].startswith('Bearer error="invalid_token", resource_metadata='))
        res = self.post({'Authorization': f'Bearer {self.provider.token(scope="openid")}'})
        self.assertEqual((res.status_code, res.json()['code']), (403, 'insufficient_scope'))
        self.assertIn('error="insufficient_scope"', res.headers['WWW-Authenticate'])
        self.assertIn('scope="queryapigate.mcp"', res.headers['WWW-Authenticate'])

    def test_a_valid_token_or_key_gets_through(self):
        for headers in ({'Authorization': f'Bearer {self.provider.token()}'}, {'X-API-Key': 'admin-key'}):
            with self.subTest(headers=list(headers)):
                res = self.post(headers)
                self.assertEqual(res.status_code, 200, res.text)
                self.assertIn('customers', res.text)

    def test_without_oauth_there_is_no_metadata_and_a_plain_challenge(self):
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_MCP_RESOURCE_URL': '', 'QUERYAPIGATE_MCP_AUTH_SERVER': ''}):
            from starlette.testclient import TestClient
            with TestClient(mcp_server.build_asgi_app(self.app)) as http:
                self.assertEqual(http.get('/.well-known/oauth-protected-resource').status_code, 404)
                res = http.post('/mcp/', headers={'Accept': 'application/json, text/event-stream',
                                                  'X-API-Key': 'wrong'},
                                json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})
        self.assertEqual((res.status_code, res.headers['WWW-Authenticate']), (401, 'Bearer realm="queryapigate"'))


@unittest.skipUnless(HAVE_MCP_SDK, 'the mcp package is not installed')
class EndToEndTests(McpOAuthTestCase):
    def test_a_real_client_calls_a_tool_with_an_access_token(self):
        import asyncio

        import uvicorn
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        server = uvicorn.Server(uvicorn.Config(mcp_server.build_asgi_app(self.app), host='127.0.0.1', port=0,
                                               log_level='warning'))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(setattr, server, 'should_exit', True)
        while not server.started:
            time.sleep(0.05)
        port = server.servers[0].sockets[0].getsockname()[1]

        async def talk():
            headers = {'Authorization': f'Bearer {self.provider.token()}'}
            async with streamablehttp_client(f'http://127.0.0.1:{port}/mcp', headers=headers) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    names = [t.name for t in (await session.list_tools()).tools]
                    return names, await session.call_tool('execute_sql', {'connection_name': 'shop',
                                                                          'sql': 'SELECT email AS e FROM customer'})

        names, result = asyncio.run(talk())
        self.assertIn('customers', names)
        self.assertFalse(result.isError, result)
        self.assertNotIn('ann@example.com', [row['e'] for row in result.structuredContent['rows']])


if __name__ == '__main__':
    unittest.main()

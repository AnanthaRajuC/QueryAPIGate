"""OAuth for the MCP server against a real identity provider (ADR 0006): Keycloak, set up here through its admin API -
a realm, a client scope `queryapigate.mcp` that adds the MCP server's URL to the token's audience, a client and a
user - then real access tokens from it on /mcp. Proves the parts a stand-in can't: that the server finds Keycloak's
keys through its published metadata, and accepts the tokens it really issues, and only those.

CI's integration job runs it against Keycloak 26. Locally it's skipped unless QUERYAPIGATE_TEST_KEYCLOAK names a
Keycloak (any realm-admin `admin`/`admin`), e.g.:
  docker run -d --rm --name qag-kc-test -p 58080:8080 -e KC_BOOTSTRAP_ADMIN_USERNAME=admin \\
      -e KC_BOOTSTRAP_ADMIN_PASSWORD=admin quay.io/keycloak/keycloak:26.0.7 start-dev
  QUERYAPIGATE_TEST_KEYCLOAK=http://localhost:58080"""
import json
import os
import unittest
import urllib.error
import urllib.parse
import urllib.request

from tests.test_mcp_oauth import RESOURCE, McpOAuthTestCase

KEYCLOAK = os.environ.get('QUERYAPIGATE_TEST_KEYCLOAK', '').rstrip('/')
REALM = 'qag-test'


def call(method, url, body=None, token=None, form=False):
    data = None
    headers = {}
    if body is not None:
        data = urllib.parse.urlencode(body).encode() if form else json.dumps(body).encode()
        headers['Content-Type'] = 'application/x-www-form-urlencoded' if form else 'application/json'
    if token:
        headers['Authorization'] = f'Bearer {token}'
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=10) as response:
        raw = response.read()
        return json.loads(raw) if raw else None


def set_up_realm():
    admin = call('POST', f'{KEYCLOAK}/realms/master/protocol/openid-connect/token', form=True,
                 body={'grant_type': 'password', 'client_id': 'admin-cli', 'username': 'admin', 'password': 'admin'})
    token = admin['access_token']
    try:
        call('DELETE', f'{KEYCLOAK}/admin/realms/{REALM}', token=token)
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
    # The realm first, bare, so it keeps Keycloak's default client scopes (`basic` puts `sub` in a token); then ours.
    call('POST', f'{KEYCLOAK}/admin/realms', token=token, body={'realm': REALM, 'enabled': True})
    base = f'{KEYCLOAK}/admin/realms/{REALM}'
    for name, audience in (('queryapigate.mcp', RESOURCE), ('rest-api', 'queryapigate')):
        call('POST', f'{base}/client-scopes', token=token, body={
            'name': name, 'protocol': 'openid-connect', 'attributes': {'include.in.token.scope': 'true'},
            'protocolMappers': [{'name': f'{name}-audience', 'protocol': 'openid-connect',
                                 'protocolMapper': 'oidc-audience-mapper',
                                 'config': {'included.custom.audience': audience, 'access.token.claim': 'true'}}]})
    call('POST', f'{base}/clients', token=token, body={
        'clientId': 'agent', 'publicClient': True, 'directAccessGrantsEnabled': True, 'standardFlowEnabled': True,
        'redirectUris': ['http://127.0.0.1/callback']})  # naming its scopes here would drop the default ones
    client_id = call('GET', f'{base}/clients?clientId=agent', token=token)[0]['id']
    for scope in call('GET', f'{base}/client-scopes', token=token):
        if scope['name'] in ('queryapigate.mcp', 'rest-api'):
            call('PUT', f"{base}/clients/{client_id}/optional-client-scopes/{scope['id']}", token=token)
    call('POST', f'{base}/users', token=token, body={
        'username': 'alice', 'enabled': True, 'email': 'alice@example.com', 'emailVerified': True,
        'firstName': 'Alice', 'lastName': 'Ito',
        'credentials': [{'type': 'password', 'value': 'pw', 'temporary': False}]})


def user_token(scope):
    issued = call('POST', f'{KEYCLOAK}/realms/{REALM}/protocol/openid-connect/token', form=True,
                  body={'grant_type': 'password', 'client_id': 'agent', 'username': 'alice', 'password': 'pw',
                        'scope': scope})
    return issued['access_token']


@unittest.skipUnless(KEYCLOAK, 'set QUERYAPIGATE_TEST_KEYCLOAK to a Keycloak URL')
class KeycloakTests(McpOAuthTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        set_up_realm()

    def setUp(self):
        super().setUp()
        from unittest import mock
        patcher = mock.patch.dict(os.environ, {'QUERYAPIGATE_MCP_AUTH_SERVER': f'{KEYCLOAK}/realms/{REALM}'})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_its_metadata_is_found_and_its_tokens_accepted(self):
        permission, refusal = self.authenticate(user_token('openid queryapigate.mcp'))
        self.assertIsNone(refusal)
        self.assertTrue(permission.name.startswith('jwt:'))
        self.assertTrue(permission.mask)

    def test_a_token_without_the_scope_or_for_rest_is_refused(self):
        refusal = self.authenticate(user_token('openid'))[1]
        self.assertEqual(refusal.status, 401)  # no MCP audience either: not issued for it at all
        refusal = self.authenticate(user_token('openid rest-api'))[1]
        self.assertEqual(refusal.status, 401)

    def test_a_tool_call_with_a_keycloak_token(self):
        from queryapigate import mcp_server
        result = mcp_server.handle_call(self.app, '', f"Bearer {user_token('openid queryapigate.mcp')}", '10.0.0.1',
                                        'customers', {})
        self.assertFalse(result.get('isError'), result)
        self.assertNotIn('ann@example.com', [row['email'] for row in result['structuredContent']['rows']])


if __name__ == '__main__':
    unittest.main()

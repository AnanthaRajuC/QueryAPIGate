"""Console sign-in through a real Keycloak (ADR 0007): a realm set up through its admin API - a confidential client,
a groups claim, a group and a user in it - and the whole authorization code flow driven as a browser would: the
server's /auth/sso/login, Keycloak's own login page, the redirect back to /auth/sso/callback, a session.

CI's integration job runs it; locally it's skipped unless QUERYAPIGATE_TEST_KEYCLOAK names a Keycloak - see
tests/test_mcp_keycloak.py for the one-line start."""
import html
import os
import re
import tempfile
import unittest
import urllib.error
import urllib.parse
from unittest import mock

from tests.test_mcp_keycloak import KEYCLOAK, call

REALM = 'qag-sso-test'
CLIENT_ID, SECRET = 'queryapigate-console', 'console-secret'
CALLBACK = 'http://localhost/auth/sso/callback'


def set_up_realm():
    admin = call('POST', f'{KEYCLOAK}/realms/master/protocol/openid-connect/token', form=True,
                 body={'grant_type': 'password', 'client_id': 'admin-cli', 'username': 'admin', 'password': 'admin'})
    token = admin['access_token']
    try:
        call('DELETE', f'{KEYCLOAK}/admin/realms/{REALM}', token=token)
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
    call('POST', f'{KEYCLOAK}/admin/realms', token=token, body={'realm': REALM, 'enabled': True})
    base = f'{KEYCLOAK}/admin/realms/{REALM}'
    call('POST', f'{base}/clients', token=token, body={
        'clientId': CLIENT_ID, 'publicClient': False, 'clientAuthenticatorType': 'client-secret', 'secret': SECRET,
        'standardFlowEnabled': True, 'directAccessGrantsEnabled': False, 'redirectUris': [CALLBACK],
        'attributes': {'pkce.code.challenge.method': 'S256'},
        'protocolMappers': [{'name': 'groups', 'protocol': 'openid-connect',
                             'protocolMapper': 'oidc-group-membership-mapper',
                             'config': {'claim.name': 'groups', 'full.path': 'false', 'id.token.claim': 'true',
                                        'access.token.claim': 'false', 'userinfo.token.claim': 'true'}}]})
    for group in ('qag-admins', 'everyone'):
        call('POST', f'{base}/groups', token=token, body={'name': group})
    groups = {g['name']: g['id'] for g in call('GET', f'{base}/groups', token=token)}
    for user, group in (('alice', 'qag-admins'), ('bob', 'everyone')):
        call('POST', f'{base}/users', token=token, body={
            'username': user, 'enabled': True, 'email': f'{user}@example.com', 'emailVerified': True,
            'firstName': user.title(), 'lastName': 'Test',
            'credentials': [{'type': 'password', 'value': 'pw', 'temporary': False}]})
        user_id = call('GET', f'{base}/users?username={user}', token=token)[0]['id']
        call('PUT', f'{base}/users/{user_id}/groups/{groups[group]}', token=token)


def keycloak_sign_in(login_url, username):
    """Keycloak's login page, as a browser fills it in; returns where Keycloak sends the browser next."""
    import httpx
    with httpx.Client() as web:
        page = web.get(login_url)
        action = html.unescape(re.search(r'action="([^"]+)"', page.text).group(1))
        # Keycloak's cookies are Secure; a browser sends them to http://localhost, Python's jar doesn't
        cookies = '; '.join(f'{c.name}={c.value}' for c in web.cookies.jar)
        answer = web.post(action, data={'username': username, 'password': 'pw'}, headers={'Cookie': cookies})
        return answer.headers['location']


@unittest.skipUnless(KEYCLOAK, 'set QUERYAPIGATE_TEST_KEYCLOAK to a Keycloak URL')
class KeycloakSignInTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        set_up_realm()

    def setUp(self):
        from queryapigate import create_app, jwtauth, oidc
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {
            'QUERYAPIGATE_HOME': self.tmp.name, 'QUERYAPIGATE_API_KEY': 'admin-key',
            'QUERYAPIGATE_SSO_ISSUER': f'{KEYCLOAK}/realms/{REALM}', 'QUERYAPIGATE_SSO_CLIENT_ID': CLIENT_ID,
            'QUERYAPIGATE_SSO_CLIENT_SECRET': SECRET, 'QUERYAPIGATE_SSO_REDIRECT_URL': CALLBACK,
            'QUERYAPIGATE_SSO_ROLES': 'admin=qag-admins'})
        patcher.start()
        self.addCleanup(patcher.stop)
        oidc.forget()
        jwtauth._jwks_clients.clear()
        self.browser = create_app().test_client()
        self.browser.environ_base['wsgi.url_scheme'] = 'https'

    def sign_in(self, username):
        login = self.browser.get('/auth/sso/login?next=/console/alerts')
        self.assertEqual(login.status_code, 302)
        back = keycloak_sign_in(login.headers['Location'], username)
        self.assertTrue(back.startswith(CALLBACK + '?'), back)
        return self.browser.get('/auth/sso/callback?' + urllib.parse.urlsplit(back).query)

    def test_a_person_in_a_mapped_group_signs_in(self):
        from queryapigate import admins, sso
        res = self.sign_in('alice')
        self.assertEqual((res.status_code, res.headers['Location']), (302, '/console/alerts'))
        session = self.browser.get('/auth/session').get_json()
        self.assertEqual((session['admin'], session['role'], session['email']), ('alice', 'admin', 'alice@example.com'))
        me = self.browser.get('/api/v1/me', headers={sso.CSRF_HEADER: session['csrf']}).get_json()
        self.assertEqual((me['name'], me['via']), ('alice', 'sso'))
        self.assertTrue(admins.get_admin('alice')['sso']['issuer'].endswith('/realms/' + REALM))
        res = self.browser.post('/auth/sso/logout', headers={sso.CSRF_HEADER: session['csrf']})
        self.assertIn('/protocol/openid-connect/logout', res.get_json()['provider_sign_out'])

    def test_a_person_in_no_mapped_group_is_refused(self):
        from queryapigate import admins
        res = self.sign_in('bob')
        self.assertIn('sso_error=sso_no_role', res.headers['Location'])
        self.assertEqual(admins.list_admins(), [])


if __name__ == '__main__':
    unittest.main()

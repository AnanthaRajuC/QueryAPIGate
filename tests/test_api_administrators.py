"""/api/v1/administrators (ADR 0003): owners manage administrators and anyone's tokens; every administrator manages
their own tokens; every change is audited; responses match /openapi.json."""
import os
from datetime import date, timedelta
from unittest import mock

from queryapigate import admins, store
from tests.test_api_v1 import V1TestCase

ADMINS = '/api/v1/administrators'
ONE = '/api/v1/administrators/{name}'
TOKENS = '/api/v1/administrators/{name}/tokens'
TOKEN = '/api/v1/administrators/{name}/tokens/{token_id}'


class AdministratorApiTests(V1TestCase):
    def token(self, name, role):
        admins.create_admin(name, role)
        return {'X-API-Key': admins.issue_token(name)[1]}

    def test_create_read_change_and_remove(self):
        created = self.call('post', ADMINS, ADMINS, 201,
                            json={'name': 'alice', 'role': 'developer', 'email': 'alice@corp.com'}).get_json()
        self.assertEqual((created['role'], created['created_by'], created['tokens']), ('developer', 'admin', 0))
        self.call('post', ADMINS, ADMINS, 409, json={'name': 'alice', 'role': 'admin'})
        self.call('post', ADMINS, ADMINS, 400, json={'name': 'bob', 'role': 'admin', 'shoe_size': 9})
        listed = self.call('get', ADMINS, ADMINS, 200).get_json()['items']
        self.assertEqual([a['name'] for a in listed], ['alice'])

        etag = self.call('get', f'{ADMINS}/alice', ONE, 200).headers['ETag']
        changed = self.call('patch', f'{ADMINS}/alice', ONE, 200, json={'role': 'admin', 'email': None},
                            headers={'X-API-Key': 'admin-key', 'If-Match': etag}).get_json()
        self.assertEqual((changed['role'], changed['email']), ('admin', None))
        self.call('patch', f'{ADMINS}/alice', ONE, 412, json={'active': False},
                  headers={'X-API-Key': 'admin-key', 'If-Match': etag})
        self.call('delete', f'{ADMINS}/alice', ONE, 204)
        self.call('get', f'{ADMINS}/alice', ONE, 404)
        self.assertEqual([(e['action'], e['target'], e['via']) for e in store.read_audit_log()],
                         [('create_admin', 'alice', 'break-glass'), ('update_admin', 'alice', 'break-glass'),
                          ('delete_admin', 'alice', 'break-glass')])
        self.assertEqual(store.read_audit_log()[1]['changes'],
                         {'email': {'from': 'alice@corp.com', 'to': None},
                          'role': {'from': 'developer', 'to': 'admin'}})

    def test_tokens_are_shown_once_and_work(self):
        self.call('post', ADMINS, ADMINS, 201, json={'name': 'ci-deploy', 'role': 'developer'})
        expires = (date.today() + timedelta(days=30)).isoformat()
        res = self.call('post', f'{ADMINS}/ci-deploy/tokens', TOKENS, 201, json={'label': 'ci', 'expires_at': expires})
        self.assertEqual(res.headers['Cache-Control'], 'no-store')
        issued = res.get_json()
        me = self.client.get('/api/v1/me', headers={'X-API-Key': issued['secret']}).get_json()
        self.assertEqual((me['name'], me['role']), ('ci-deploy', 'developer'))
        listed = self.call('get', f'{ADMINS}/ci-deploy/tokens', TOKENS, 200).get_json()['items']
        self.assertEqual([(t['id'], t['label'], t['expires_at']) for t in listed], [(issued['id'], 'ci', expires)])
        self.assertNotIn('secret', listed[0])
        self.call('delete', f"{ADMINS}/ci-deploy/tokens/{issued['id']}", TOKEN, 204)
        self.call('delete', f"{ADMINS}/ci-deploy/tokens/{issued['id']}", TOKEN, 404)
        self.assertEqual(self.client.get('/api/v1/me', headers={'X-API-Key': issued['secret']}).status_code, 401)

    def test_everyone_manages_their_own_tokens_and_only_owners_anyone_elses(self):
        dev = self.token('dev', 'developer')
        self.token('other', 'auditor')
        mine = self.call('post', f'{ADMINS}/dev/tokens', TOKENS, 201, headers=dev, json={'label': 'laptop'})
        self.call('get', f'{ADMINS}/dev/tokens', TOKENS, 200, headers=dev)
        self.call('delete', f"{ADMINS}/dev/tokens/{mine.get_json()['id']}", TOKEN, 204, headers=dev)
        for method, url in (('get', f'{ADMINS}/other/tokens'), ('post', f'{ADMINS}/other/tokens')):
            res = getattr(self.client, method)(url, headers=dev, json={})
            self.assertEqual((res.status_code, res.get_json()['code']), (403, 'role_forbidden'))
        for method, url in (('get', ADMINS), ('post', ADMINS)):
            res = getattr(self.client, method)(url, headers=dev, json={'name': 'x', 'role': 'owner'})
            self.assertEqual((res.status_code, res.get_json()['capability']), (403, 'admins.' + (
                'read' if method == 'get' else 'write')))
        owner = self.token('boss', 'owner')
        self.call('post', f'{ADMINS}/dev/tokens', TOKENS, 201, headers=owner, json={})

    def test_the_only_owner_cannot_strand_the_server(self):
        owner = self.token('boss', 'owner')
        with mock.patch.dict(os.environ):
            os.environ.pop('QUERYAPIGATE_API_KEY')  # no break-glass key to fall back on
            for method, body in (('patch', {'role': 'admin'}), ('patch', {'active': False}), ('delete', None)):
                with self.subTest(method=method, body=body):
                    res = self.call(method, f'{ADMINS}/boss', ONE, 409, headers=owner, json=body)
                    self.assertEqual(res.get_json()['code'], 'last_owner')
        self.call('patch', f'{ADMINS}/boss', ONE, 200, headers=owner, json={'role': 'admin'})  # with the key set

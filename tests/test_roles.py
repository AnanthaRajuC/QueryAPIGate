"""Tests for named permission roles (BACKLOG #22): a reusable *template* for a key's grant fields, copied
onto a key once at creation time - never consulted again afterward. Distinct from a key's own permissions
(tests/test_api_keys.py), which remain the sole source of truth once a key exists."""
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from queryapigate import apikeys, create_app, db
from tests.helpers import write_connections


class AppTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        tmp = self.tmp.name
        self.db_path = os.path.join(tmp, 'test.db')
        conn = sqlite3.connect(self.db_path)
        conn.execute('CREATE TABLE t (id INTEGER)')
        conn.commit()
        conn.close()
        patcher = mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': tmp, 'QUERYAPIGATE_API_KEY': 'admin-key'})
        patcher.start()
        self.addCleanup(patcher.stop)
        write_connections({
            'a': {'db': 'sqlite', 'database': self.db_path, 'active': True},
            'b': {'db': 'sqlite', 'database': self.db_path, 'active': True},
        })
        self.client = create_app().test_client()
        self.admin_headers = {'X-API-Key': 'admin-key'}
        apikeys._last_recorded_use.clear()

    def roles(self):
        return {r['name']: r for r in self.client.get('/api/v1/roles', headers=self.admin_headers).get_json()['items']}

    def keys(self):
        return {k['name']: k for k in self.client.get('/api/v1/api-keys', headers=self.admin_headers)
                .get_json()['items']}

    def create_role(self, name='reporting', **fields):
        res = self.client.post('/api/v1/roles', json={'name': name, **fields}, headers=self.admin_headers)
        self.assertEqual(res.status_code, 201, res.get_data(as_text=True))
        return res.get_json()

    def create_key_from_role(self, name, role, **extra):
        res = self.client.post('/api/v1/api-keys', json={'name': name, 'role': role, **extra},
                               headers=self.admin_headers)
        self.assertEqual(res.status_code, 201, res.get_data(as_text=True))
        return res.get_json()['secret']


class RoleCrudTests(AppTestCase):
    def test_create_list_delete(self):
        self.create_role('reporting', connections=['a'], rate_limit='200/hour')
        roles = self.roles()
        self.assertEqual(roles['reporting']['connections'], ['a'])
        self.assertEqual(roles['reporting']['rate_limit'], '200/hour')
        self.assertEqual(self.client.delete('/api/v1/roles/reporting', headers=self.admin_headers).status_code, 204)
        self.assertNotIn('reporting', self.roles())

    def test_duplicate_name_is_rejected(self):
        self.create_role('reporting')
        res = self.client.post('/api/v1/roles', json={'name': 'reporting'}, headers=self.admin_headers)
        self.assertEqual(res.status_code, 409)

    def test_invalid_name_is_rejected(self):
        for name in ('bad/name!', '', None):
            res = self.client.post('/api/v1/roles', json={'name': name}, headers=self.admin_headers)
            self.assertEqual(res.status_code, 400, name)

    def test_malformed_fields_are_rejected(self):
        res = self.client.post('/api/v1/roles', json={'name': 'x', 'connections': 'a'}, headers=self.admin_headers)
        self.assertEqual(res.status_code, 400)
        res = self.client.post('/api/v1/roles', json={'name': 'x', 'rate_limit': 'garbage'}, headers=self.admin_headers)
        self.assertEqual(res.status_code, 400)

    def test_update_of_an_unknown_role_is_404(self):
        res = self.client.patch('/api/v1/roles/nope', json={'connections': ['a']}, headers=self.admin_headers)
        self.assertEqual(res.status_code, 404)

    def test_delete_of_an_unknown_role_is_404(self):
        self.assertEqual(self.client.delete('/api/v1/roles/nope', headers=self.admin_headers).status_code, 404)

    def test_patch_updates_fields(self):
        self.create_role('reporting', connections=['a'])
        self.client.patch('/api/v1/roles/reporting', json={'connections': ['a', 'b'], 'allow_writes': True},
                          headers=self.admin_headers)
        role = self.roles()['reporting']
        self.assertEqual(role['connections'], ['a', 'b'])
        self.assertTrue(role['allow_writes'])

    def test_patch_can_clear_rate_limit_via_explicit_null(self):
        self.create_role('reporting', rate_limit='100/minute')
        self.client.patch('/api/v1/roles/reporting', json={'rate_limit': None}, headers=self.admin_headers)
        role = self.roles()['reporting']
        self.assertIsNone(role['rate_limit'])

    def test_only_admin_can_manage_roles(self):
        res = self.client.post('/api/v1/api-keys', json={'name': 'scoped', 'connections': []},
                               headers=self.admin_headers)
        scoped = res.get_json()['secret']
        headers = {'X-API-Key': scoped}
        self.assertEqual(self.client.get('/api/v1/roles', headers=headers).status_code, 403)
        self.assertEqual(self.client.post('/api/v1/roles', json={'name': 'x'}, headers=headers).status_code, 403)
        self.assertEqual(self.client.patch('/api/v1/roles/x', json={}, headers=headers).status_code, 403)
        self.assertEqual(self.client.delete('/api/v1/roles/x', headers=headers).status_code, 403)


class CreateFromRoleTests(AppTestCase):
    def test_a_key_created_from_a_role_gets_its_fields(self):
        self.create_role('reporting', connections=['a'], allow_writes=False, rate_limit='200/hour')
        key = self.create_key_from_role('k1', 'reporting')
        listed = self.keys()['k1']
        self.assertEqual(listed['connections'], ['a'])
        self.assertFalse(listed['allow_writes'])
        self.assertEqual(listed['rate_limit'], '200/hour')
        self.assertEqual(listed['created_from_role'], 'reporting')
        res = self.client.post('/execute_sql', json={'sql': 'SELECT * FROM t', 'connection_name': 'a'},
                               headers={'X-API-Key': key})
        self.assertEqual(res.status_code, 200)

    def test_a_key_created_without_a_role_has_no_created_from_role(self):
        res = self.client.post('/api/v1/api-keys', json={'name': 'k1', 'connections': ['a']},
                               headers=self.admin_headers)
        self.assertEqual(res.status_code, 201)
        listed = self.keys()['k1']
        self.assertIsNone(listed['created_from_role'])

    def test_combining_role_with_an_explicit_field_is_rejected(self):
        self.create_role('reporting', connections=['a'])
        for field, value in (('connections', ['b']), ('allow_writes', True), ('queries', ['q']),
                             ('rate_limit', '10/minute'), ('allowed_ips', ['203.0.113.5']),
                             ('allowed_write_ops', ['insert'])):
            res = self.client.post('/api/v1/api-keys', json={'name': 'x', 'role': 'reporting', field: value},
                                   headers=self.admin_headers)
            self.assertEqual(res.status_code, 400, field)

    def test_expires_at_can_still_be_set_alongside_a_role(self):
        # expires_at is deliberately not part of a role template - it's inherently per-key, not shared.
        self.create_role('reporting', connections=['a'])
        res = self.client.post('/api/v1/api-keys', json={'name': 'k1', 'role': 'reporting', 'expires_at': '2030-01-01'},
                               headers=self.admin_headers)
        self.assertEqual(res.status_code, 201, res.get_data(as_text=True))
        listed = self.keys()['k1']
        self.assertEqual(listed['expires_at'], '2030-01-01')

    def test_an_unknown_role_is_rejected(self):
        res = self.client.post('/api/v1/api-keys', json={'name': 'k1', 'role': 'does-not-exist'},
                               headers=self.admin_headers)
        self.assertEqual(res.status_code, 404)

    def test_editing_the_role_afterward_does_not_affect_an_existing_key(self):
        self.create_role('reporting', connections=['a'])
        key = self.create_key_from_role('k1', 'reporting')
        self.client.patch('/api/v1/roles/reporting', json={'connections': []}, headers=self.admin_headers)
        res = self.client.post('/execute_sql', json={'sql': 'SELECT * FROM t', 'connection_name': 'a'},
                               headers={'X-API-Key': key})
        self.assertEqual(res.status_code, 200)  # k1 still has its own copy of 'a', unaffected

    def test_deleting_the_role_afterward_does_not_affect_an_existing_key(self):
        self.create_role('reporting', connections=['a'])
        key = self.create_key_from_role('k1', 'reporting')
        self.client.delete('/api/v1/roles/reporting', headers=self.admin_headers)
        res = self.client.post('/execute_sql', json={'sql': 'SELECT * FROM t', 'connection_name': 'a'},
                               headers={'X-API-Key': key})
        self.assertEqual(res.status_code, 200)

    def test_two_keys_from_the_same_role_are_independent_afterward(self):
        self.create_role('reporting', connections=['a'])
        key1 = self.create_key_from_role('k1', 'reporting')
        self.create_key_from_role('k2', 'reporting')
        self.client.patch('/api/v1/api-keys/k2', json={'connections': []}, headers=self.admin_headers)
        res = self.client.post('/execute_sql', json={'sql': 'SELECT * FROM t', 'connection_name': 'a'},
                               headers={'X-API-Key': key1})
        self.assertEqual(res.status_code, 200)  # k1 untouched by k2's later change


class RoleAuditTests(AppTestCase):
    def entries(self):
        return self.client.get('/api/v1/audit', headers=self.admin_headers).get_json()['items']

    def test_create_is_recorded(self):
        self.create_role('reporting', connections=['a'])
        entry = self.entries()[0]
        self.assertEqual((entry['action'], entry['target']), ('create_role', 'reporting'))
        self.assertEqual(entry['changes']['connections'], ['a'])

    def test_update_is_recorded_as_a_diff(self):
        self.create_role('reporting', connections=['a'])
        self.client.patch('/api/v1/roles/reporting', json={'allow_writes': True}, headers=self.admin_headers)
        entry = self.entries()[0]
        self.assertEqual(entry['action'], 'update_role')
        self.assertEqual(entry['changes'], {'allow_writes': {'from': False, 'to': True}})

    def test_delete_is_recorded_with_what_the_role_granted(self):
        self.create_role('reporting', connections=['a'])
        self.client.delete('/api/v1/roles/reporting', headers=self.admin_headers)
        entry = self.entries()[0]
        self.assertEqual(entry['action'], 'delete_role')
        self.assertEqual(entry['changes']['connections'], ['a'])


class ModuleLevelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(db.close)
        os.environ.pop('QUERYAPIGATE_API_KEY', None)
        db.init_schema()

    def test_list_roles_is_empty_by_default(self):
        self.assertEqual(apikeys.list_roles(), {})

    def test_create_role_then_create_key_from_it(self):
        apikeys.create_role('reporting', connections=['a'], allow_writes=False, rate_limit='50/minute')
        secret = apikeys.create_key('k1', role='reporting')
        self.assertTrue(secret.startswith('sk_'))
        permission = apikeys.authenticate(secret)
        self.assertEqual(permission.connections, frozenset({'a'}))
        self.assertFalse(permission.allow_writes)
        self.assertEqual(permission.rate_limit, (50, 60))


if __name__ == '__main__':
    unittest.main()

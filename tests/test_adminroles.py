"""Admin roles (ADR 0003, adminroles.py): every /api/v1 operation needs a capability, each role holds a fixed set,
and the check is applied before any route runs - so each role reaches exactly what the table says."""
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from queryapigate import adminroles, admins, create_app
from tests.helpers import create_key, save_query, secret_of, write_connections

ADMIN = {'X-API-Key': 'admin-key'}


def v1_operations(app):
    return {rule.endpoint for rule in app.url_map.iter_rules() if rule.rule.startswith('/api/v1/')}


class TableTests(unittest.TestCase):
    def test_every_operation_has_a_capability(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': home}):
            operations = v1_operations(create_app())
        self.assertEqual(operations - set(adminroles.OPERATIONS), set(),
                         'decide which capability each new /api/v1 operation needs, in adminroles.OPERATIONS')
        self.assertEqual(set(adminroles.OPERATIONS) - operations, set(), 'an entry for an operation that is gone')

    def test_the_table_is_consistent(self):
        self.assertLessEqual(set(adminroles.OPERATIONS.values()), set(adminroles.CAPABILITIES))
        self.assertEqual(set(adminroles.ROLES), set(adminroles.ROLE_NAMES))
        for role, capabilities in adminroles.ROLES.items():
            self.assertLessEqual(capabilities, set(adminroles.CAPABILITIES), role)
            self.assertIn('self', capabilities, role)
        self.assertEqual(adminroles.ROLES['owner'], set(adminroles.CAPABILITIES))

    def test_the_roles_nest_where_the_adr_says(self):
        roles = adminroles.ROLES
        self.assertLess(roles['admin'], roles['owner'])
        self.assertLess(roles['developer'], roles['admin'])
        self.assertNotIn('access.write', roles['auditor'])
        self.assertNotIn('queries.write', roles['auditor'])
        self.assertNotIn('connections.write', roles['developer'])
        self.assertNotIn('access.read', roles['developer'])


class RoleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': self.tmp.name, 'QUERYAPIGATE_API_KEY': 'admin-key'})
        patcher.start()
        self.addCleanup(patcher.stop)
        data = os.path.join(self.tmp.name, 'data.db')
        sqlite3.connect(data).close()
        write_connections({'lite': {'db': 'sqlite', 'database': data, 'active': True}})
        self.client = create_app().test_client()
        save_query(self.client, headers=ADMIN, body={'filename': 'one', 'description': 'd', 'connection_name': 'lite',
                                                     'sql_query': 'SELECT 1 AS n'})
        self.as_role = {}
        for role in adminroles.ROLE_NAMES:
            admins.create_admin(f'{role}-person', role)
            self.as_role[role] = {'X-API-Key': admins.issue_token(f'{role}-person')[1]}

    def status(self, role, method, path, **kwargs):
        res = self.client.open(path, method=method, headers=self.as_role[role], **kwargs)
        return res.status_code, (res.get_json() or {}).get('code') if res.status_code >= 400 else None

    def test_each_role_reaches_what_the_table_says(self):
        cases = [  # (method, path, body, capability)
            ('GET', '/api/v1/queries', None, 'queries.read'),
            ('POST', '/api/v1/queries/validate', {'sql': 'SELECT 1', 'connection_name': 'lite'}, 'queries.write'),
            ('GET', '/api/v1/connections', None, 'connections.read'),
            ('POST', '/api/v1/connections/test', {'db': 'sqlite', 'database': ':memory:'}, 'connections.write'),
            ('GET', '/api/v1/api-keys', None, 'access.read'),
            ('POST', '/api/v1/roles', {'name': 'r', 'connections': []}, 'access.write'),
            ('GET', '/api/v1/cache/entries', None, 'cache.read'),
            ('DELETE', '/api/v1/cache/entries', None, 'cache.write'),
            ('GET', '/api/v1/audit', None, 'observe'),
            ('GET', '/api/v1/settings', None, 'settings.read'),
            ('GET', '/api/v1/me', None, 'self'),
        ]
        for role in adminroles.ROLE_NAMES:
            for method, path, body, capability in cases:
                with self.subTest(role=role, path=path, method=method):
                    status, code = self.status(role, method, path, json=body)
                    if capability in adminroles.ROLES[role]:  # allowed: whatever the operation then says, not 403
                        self.assertNotEqual(status, 403, code)
                    else:
                        self.assertEqual((status, code), (403, 'role_forbidden'))

    def test_a_refusal_names_the_capability(self):
        res = self.client.post('/api/v1/roles', headers=self.as_role['developer'], json={'name': 'r'})
        body = res.get_json()
        self.assertEqual((body['code'], body['capability'], body['role']), ('role_forbidden', 'access.write',
                                                                            'developer'))

    def test_who_am_i(self):
        me = self.client.get('/api/v1/me', headers=self.as_role['developer']).get_json()
        self.assertEqual((me['name'], me['role'], me['via'], me['data_access']),
                         ('developer-person', 'developer', 'token', True))
        self.assertEqual(me['capabilities'], sorted(adminroles.ROLES['developer']))
        me = self.client.get('/api/v1/me', headers=ADMIN).get_json()
        self.assertEqual((me['name'], me['role'], me['via']), ('admin', 'owner', 'break-glass'))

    def test_data_access_for_every_role_but_auditor(self):
        for role in adminroles.ROLE_NAMES:
            with self.subTest(role=role):
                adhoc = self.client.post('/execute_sql', headers=self.as_role[role],
                                         json={'sql': 'SELECT 2 AS n', 'connection_name': 'lite'})
                saved = self.client.get('/q/one', headers=self.as_role[role])
                if role in adminroles.DATA_ROLES:
                    self.assertEqual((adhoc.get_json(), saved.get_json()), ([{'n': 2}], [{'n': 1}]))
                else:
                    self.assertEqual(adhoc.status_code, 403)
                    self.assertEqual(saved.status_code, 403)

    def test_a_scoped_key_manages_nothing(self):
        key = {'X-API-Key': secret_of(create_key(self.client, headers=ADMIN, name='app', connections=['lite']))}
        res = self.client.get('/api/v1/me', headers=key)
        self.assertEqual((res.status_code, res.get_json()['code']), (403, 'admin_only'))

    def test_an_open_server_is_an_owner(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': home}):
            os.environ.pop('QUERYAPIGATE_API_KEY')
            me = create_app().test_client().get('/api/v1/me').get_json()
        self.assertEqual((me['name'], me['role'], me['via']), (None, 'owner', 'open'))


if __name__ == '__main__':
    unittest.main()

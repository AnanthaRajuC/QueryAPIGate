"""Workspaces and their members through the Management API (ADR 0023, delivery step 2): who may create, see and
change them; what a role from a membership is - the same role, in one workspace, without what only a server-wide
role gives; and an administrator with roles only in other workspaces refused with what to name."""
import logging
import os
from unittest import mock

from queryapigate import admins, db, experimental, store
from tests.test_api_v1 import ADMIN, V1TestCase

LIST = '/api/v1/workspaces'
ONE = '/api/v1/workspaces/{name}'
MEMBERS = '/api/v1/workspaces/{name}/members'
MEMBER = '/api/v1/workspaces/{name}/members/{admin}'


class WorkspaceTestCase(V1TestCase):
    def person(self, name, role=None):
        """An administrator - with `role` server-wide, or none - and the headers to call as them."""
        admins.create_admin(name, role)
        _, secret = admins.issue_token(name)
        return {'X-API-Key': secret}

    def workspace(self, name='team-a', headers=ADMIN, status=201, **fields):
        return self.call('post', LIST, LIST, status, headers=headers, json={'name': name, **fields})

    def member(self, workspace, admin, role, headers=ADMIN, status=200):
        return self.call('put', f'{LIST}/{workspace}/members/{admin}', MEMBER, status, headers=headers,
                         json={'role': role})

    def audit(self, action):
        return [entry for entry in store.read_audit_log() if entry['action'] == action]


class WorkspaceLifecycleTests(WorkspaceTestCase):
    def test_an_owner_creates_describes_lists_and_removes_one(self):
        created = self.workspace(description='Payments').get_json()
        self.assertEqual((created['name'], created['description'], created['created_by'], created['members']),
                         ('team-a', 'Payments', 'admin', 0))
        names = [w['name'] for w in self.call('get', LIST, LIST, 200).get_json()['items']]
        self.assertEqual(names, ['default', 'team-a'])
        changed = self.call('patch', f'{LIST}/team-a', ONE, 200, json={'description': 'Payments, EU'}).get_json()
        self.assertEqual(changed['description'], 'Payments, EU')
        self.call('delete', f'{LIST}/team-a', ONE, 204)
        self.call('get', f'{LIST}/team-a', ONE, 404)
        self.assertEqual([len(self.audit(a)) for a in ('create_workspace', 'update_workspace', 'delete_workspace')],
                         [1, 1, 1])

    def test_names_and_the_default_workspace_are_protected(self):
        self.workspace()
        self.assertEqual(self.workspace(status=409).get_json()['code'], 'workspace_exists')
        for name in ('has space', '-dash', '', 'x' * 101):
            with self.subTest(name=name):
                self.assertEqual(self.workspace(name, status=400).get_json()['code'], 'invalid_name')
        res = self.call('delete', f'{LIST}/default', ONE, 409)
        self.assertEqual(res.get_json()['code'], 'workspace_protected')
        self.call('post', LIST, LIST, 400, json={'name': 'team-b', 'colour': 'blue'})

    def test_a_workspace_that_owns_anything_stays(self):
        self.workspace()
        with db.transaction() as tx:
            tx.execute("INSERT INTO connections (workspace, name, db, active, created_at, updated_at, details_json) "
                       "VALUES ('team-a', 'billing', 'sqlite', 1, 'now', 'now', '{}')")
        res = self.call('delete', f'{LIST}/team-a', ONE, 409).get_json()
        self.assertEqual((res['code'], res['contents']), ('workspace_not_empty', {'connections': 1}))
        with db.transaction() as tx:
            tx.execute("DELETE FROM connections WHERE workspace = 'team-a'")
        self.call('delete', f'{LIST}/team-a', ONE, 204)

    def test_creating_and_removing_them_is_an_owners(self):
        for role in ('admin', 'developer', 'auditor'):
            with self.subTest(role=role):
                headers = self.person(f'{role}-person', role)
                res = self.workspace(f'by-{role}', headers=headers, status=403).get_json()
                self.assertEqual((res['code'], res['capability']), ('role_forbidden', 'workspaces.write'))


class MemberTests(WorkspaceTestCase):
    def test_an_owner_gives_changes_and_takes_away_a_role(self):
        self.workspace()
        self.person('alice')
        self.assertEqual(self.member('team-a', 'alice', 'developer').get_json()['role'], 'developer')
        self.member('team-a', 'alice', 'admin')
        items = self.call('get', f'{LIST}/team-a/members', MEMBERS, 200).get_json()['items']
        self.assertEqual([(m['admin'], m['role'], m['added_by']) for m in items], [('alice', 'admin', 'admin')])
        self.assertEqual(self.call('get', '/api/v1/administrators/alice', '/api/v1/administrators/{name}',
                                   200).get_json()['workspaces'], {'team-a': 'admin'})
        self.call('delete', f'{LIST}/team-a/members/alice', MEMBER, 204)
        self.assertEqual(self.call('delete', f'{LIST}/team-a/members/alice', MEMBER, 404).get_json()['code'],
                         'member_not_found')
        changes = [e['changes'] for e in self.audit('set_workspace_member')]
        self.assertEqual([c['role'] for c in changes], [{'from': None, 'to': 'developer'},
                                                        {'from': 'developer', 'to': 'admin'}])
        self.assertEqual(self.audit('remove_workspace_member')[0]['changes'], {'admin': 'alice', 'role': 'admin'})

    def test_only_real_administrators_and_roles(self):
        self.workspace()
        self.assertEqual(self.member('team-a', 'nobody', 'developer', status=404).get_json()['code'],
                         'admin_not_found')
        self.person('alice')
        self.assertEqual(self.member('team-a', 'alice', 'superuser', status=400).get_json()['code'], 'invalid_body')
        self.assertEqual(self.member('team-z', 'alice', 'developer', status=404).get_json()['code'],
                         'workspace_not_found')

    def test_a_workspace_owner_manages_its_members_and_no_others(self):
        self.workspace()
        self.workspace('team-b')
        lead = self.person('lead')
        self.person('alice')
        self.member('team-a', 'lead', 'owner')
        self.member('team-b', 'lead', 'developer')
        self.member('team-a', 'alice', 'developer', headers=lead)  # their own workspace: theirs to staff
        res = self.member('team-b', 'alice', 'developer', headers=lead, status=403).get_json()
        self.assertEqual(res['code'], 'role_forbidden')  # a developer there
        self.workspace('team-c', headers=lead, status=403)  # and no workspace of their own making

    def test_removing_an_administrator_ends_their_memberships(self):
        self.workspace()
        self.person('alice')
        self.member('team-a', 'alice', 'developer')
        self.call('delete', '/api/v1/administrators/alice', '/api/v1/administrators/{name}', 204)
        self.assertEqual(self.call('get', f'{LIST}/team-a/members', MEMBERS, 200).get_json()['items'], [])


class MembershipRoleTests(WorkspaceTestCase):
    def test_an_administrator_needs_a_role_said_on_creation_and_null_is_one(self):
        res = self.call('post', '/api/v1/administrators', '/api/v1/administrators', 400, json={'name': 'bob'})
        self.assertIn('role is required', res.get_json()['error'])
        created = self.call('post', '/api/v1/administrators', '/api/v1/administrators', 201,
                            json={'name': 'bob', 'role': None}).get_json()
        self.assertEqual((created['role'], created['workspaces']), (None, {}))

    def test_with_roles_only_in_other_workspaces_a_call_says_which(self):
        self.workspace()
        alice = self.person('alice')
        self.member('team-a', 'alice', 'admin')
        res = self.call('get', '/api/v1/queries', '/api/v1/queries', 403, headers=alice).get_json()
        self.assertEqual((res['code'], res['workspace'], res['workspaces']), ('workspace_required', 'default',
                                                                                ['team-a']))
        me = self.call('get', '/api/v1/me', '/api/v1/me', 200, headers=alice).get_json()  # their own account works
        self.assertEqual((me['role'], me['server_wide'], me['workspace'], me['workspaces']),
                         (None, False, 'default', {'team-a': 'admin'}))
        self.call('get', '/api/v1/administrators/alice/tokens', '/api/v1/administrators/{name}/tokens', 200,
                  headers=alice)
        # and the workspaces they belong to - only those
        names = [w['name'] for w in self.call('get', LIST, LIST, 200, headers=alice).get_json()['items']]
        self.assertEqual(names, ['team-a'])
        self.call('get', f'{LIST}/default', ONE, 404, headers=alice)  # not confirmed to exist, to them

    def test_a_role_from_a_membership_has_none_of_what_is_server_wide(self):
        alice = self.person('alice')
        self.member('default', 'alice', 'owner')
        self.call('get', '/api/v1/queries', '/api/v1/queries', 200, headers=alice)  # an owner, in default
        me = self.call('get', '/api/v1/me', '/api/v1/me', 200, headers=alice).get_json()
        self.assertEqual((me['role'], me['server_wide'], me['data_access']), ('owner', False, True))
        self.assertFalse(set(me['capabilities']) & {'admins.read', 'settings.read', 'workspaces.write'})
        for method, url, spec in (('get', '/api/v1/administrators', '/api/v1/administrators'),
                                  ('get', '/api/v1/settings', '/api/v1/settings'),
                                  ('post', LIST, LIST)):
            with self.subTest(url=url):
                res = self.call(method, url, spec, 403, headers=alice, json={'name': 'x'}).get_json()
                self.assertEqual(res['code'], 'role_forbidden')
                self.assertIn('server-wide', res['error'])
        # /metrics, once it needs more than any caller (a scraper's token, or an observer): every workspace's counts
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_METRICS_TOKEN': 'scraper-token'}):
            self.assertEqual(self.client.get('/metrics', headers=alice).status_code, 401)
            self.assertEqual(self.client.get('/metrics', headers=ADMIN).status_code, 200)

    def test_a_server_wide_role_sees_every_workspace(self):
        self.workspace()
        auditor = self.person('aud', 'auditor')
        names = [w['name'] for w in self.call('get', LIST, LIST, 200, headers=auditor).get_json()['items']]
        self.assertEqual(names, ['default', 'team-a'])
        self.call('get', f'{LIST}/team-a/members', MEMBERS, 200, headers=auditor)
        self.member('team-a', 'aud', 'developer', headers=auditor, status=403)  # seeing isn't staffing


class MarkedTests(WorkspaceTestCase):
    def test_the_routes_are_experimental_and_a_second_workspace_says_so_at_startup(self):
        paths = self.client.get('/openapi.json', headers=ADMIN).get_json()['paths']
        self.assertTrue(paths[LIST]['post']['x-experimental'])
        self.assertTrue(paths[MEMBER]['put']['x-experimental'])
        log = logging.getLogger('queryapigate')
        with self.assertNoLogs(log, level=logging.WARNING):
            experimental.warn_in_use(log, {}, {}, store.workspace_names())
        self.workspace()
        with self.assertLogs(log, level=logging.WARNING) as logs:
            experimental.warn_in_use(log, {}, {}, store.workspace_names())
        self.assertIn('Experimental: Workspaces (workspace team-a)', logs.output[0])

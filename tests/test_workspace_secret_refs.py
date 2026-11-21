"""A secret reached through the server's identity is a server-wide role's to point anywhere (ADR 0023): the allow-lists
for ${VAR}, ${file:...}, Vault and AWS are the whole server's, one for every workspace. A role from a membership naming
one - or repointing a connection that holds one at a host of its own - would have the server send it another team's
secret as a password. Members still run these connections, switch them on and off, and type passwords of their own."""
from unittest import mock

from queryapigate import admins
from queryapigate.errors import ApiError
from tests.test_workspace_config_cli import FILE
from tests.test_workspace_isolation import B, IsolationTestCase

WAREHOUSE = {'db': 'postgres', 'host': 'warehouse.internal', 'database': 'sales', 'user': 'reader', 'active': False}


class SecretReferenceTests(IsolationTestCase):
    def setUp(self):
        super().setUp()
        admins.create_admin('lead', None)  # no server-wide role: team-b's owner, by membership
        self.ok('put', '/api/v1/workspaces/team-b/members/lead', json={'role': 'owner'})
        self.lead = {'X-API-Key': admins.issue_token('lead')[1]}

    def refused(self, method, url, capability='connections.write', **kwargs):
        res = self.ok(method, url, status=403, headers=self.lead, **kwargs).get_json()
        self.assertEqual((res['code'], res['capability']), ('role_forbidden', capability))

    def test_a_workspace_role_cant_name_a_secret_reference(self):
        for password in ('${TEAM_A_WAREHOUSE_PW}', '${file:/run/secrets/team-a}', 'pre-${TEAM_A_PW}-post'):
            with self.subTest(password=password):
                fields = {**WAREHOUSE, 'password': password}
                self.refused('post', f'{B}/api/v1/connections', json={'name': 'grab', **fields})
                self.refused('post', f'{B}/api/v1/connections/test', json=fields)  # a probe sends it to the host
                self.refused('post', f'{B}/api/v1/connections/databases', json=fields)
        self.refused('post', f'{B}/api/v1/connections', json={
            'name': 'grab', **WAREHOUSE, 'credentials': {'source': 'aws-rds-iam'}})
        self.ok('get', f'{B}/api/v1/connections/grab', status=404)

    def test_nor_through_a_configuration_file(self):
        doc = {**FILE, 'connections': [{'name': 'grab', **WAREHOUSE, 'password': '${TEAM_A_WAREHOUSE_PW}'}]}
        res = self.ok('post', f'{B}/api/v1/config/apply', status=400, headers=self.lead, json=doc).get_json()
        self.assertEqual(res['code'], 'invalid_config')
        self.assertIn('reaches a secret through the server', res['error'])
        self.ok('get', f'{B}/api/v1/connections/grab', status=404)

    def test_nor_a_destination(self):
        fields = {'url': 's3://bucket/exports/', 'user': 'AKIAEXAMPLE', 'password': '${TEAM_A_S3_SECRET}'}
        self.refused('post', f'{B}/api/v1/destinations', capability='destinations.write',
                     json={'name': 'grab', **fields})
        self.refused('post', f'{B}/api/v1/destinations/test', capability='destinations.write', json=fields)
        self.ok('post', f'{B}/api/v1/destinations', status=201, json={'name': 'lake', **fields})  # a server admin's
        self.refused('patch', f'{B}/api/v1/destinations/lake', capability='destinations.write',
                     json={'url': 's3://bucket-of-mine/'})  # the same secret, written somewhere else

    def test_nor_repoint_one_a_server_administrator_set_up(self):
        self.ok('post', f'{B}/api/v1/connections', status=201,
                json={'name': 'wh', **WAREHOUSE, 'password': '${TEAM_B_WAREHOUSE_PW}'})
        for change in ({'host': 'mine.example.com'}, {'port': 15432}, {'user': 'admin'},
                       {'options': {'sslmode': 'disable'}}, {'password': '${TEAM_A_WAREHOUSE_PW}'}):
            with self.subTest(change=change):
                self.refused('patch', f'{B}/api/v1/connections/wh', json=change)
                self.refused('post', f'{B}/api/v1/connections/test', json={'name': 'wh', **WAREHOUSE,
                                                                           'password': '********', **change})
        stored = self.ok('get', f'{B}/api/v1/connections/wh').get_json()
        self.assertEqual(stored['host'], 'warehouse.internal')

    def test_it_still_switches_them_and_types_its_own_password(self):
        self.ok('post', f'{B}/api/v1/connections', status=201,
                json={'name': 'wh', **WAREHOUSE, 'password': '${TEAM_B_WAREHOUSE_PW}'})
        self.ok('patch', f'{B}/api/v1/connections/wh', headers=self.lead, json={'active': True})
        self.ok('patch', f'{B}/api/v1/connections/wh', headers=self.lead, json={'active': False})
        self.ok('patch', f'{B}/api/v1/connections/wh', headers=self.lead, json={'password': '********'})
        # the Console's Test button on it: the form's own fields, unchanged, with the mask - let through to the host
        probe = {'name': 'wh', 'db': 'postgres', 'host': 'warehouse.internal', 'database': 'sales', 'user': 'reader',
                 'password': '********'}
        reached = ApiError('The probe got past every check', 418, code='probe_reached')
        with mock.patch('queryapigate.services.connections.store.resolve_ad_hoc', side_effect=reached):
            self.assertEqual(self.code('post', f'{B}/api/v1/connections/test', 418, headers=self.lead, json=probe),
                             'probe_reached')
        # a password of its own: nothing of the server's goes with it, wherever it points
        self.ok('patch', f'{B}/api/v1/connections/wh', headers=self.lead,
                json={'password': 'typed-by-the-team', 'host': 'other.internal'})
        self.ok('post', f'{B}/api/v1/connections', status=201, headers=self.lead,
                json={'name': 'own', **WAREHOUSE, 'password': 'typed-by-the-team'})
        self.ok('post', f'{B}/api/v1/destinations', status=201, headers=self.lead,
                json={'name': 'own', 'url': 's3://bucket/b/', 'user': 'AKIAB', 'password': 'typed'})

    def test_a_server_wide_role_sets_them_up_in_any_workspace(self):
        with mock.patch.dict('os.environ', {'QUERYAPIGATE_SECRET_ENV_VARS': 'TEAM_B_*'}):
            self.ok('post', f'{B}/api/v1/connections', status=201,
                    json={'name': 'wh', **WAREHOUSE, 'password': '${TEAM_B_WAREHOUSE_PW}'})
            self.ok('patch', f'{B}/api/v1/connections/wh', json={'host': 'replica.internal'})

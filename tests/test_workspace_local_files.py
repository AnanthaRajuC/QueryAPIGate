"""A connection that opens files on the server is a server-wide role's to set up (ADR 0023): a role from a workspace
membership choosing the path could read any file the server can - another workspace's database, a secret's file. It
still runs and switches off the ones a server administrator set up for its workspace, and uses remote storage."""
import os

from queryapigate import admins
from tests.test_workspace_config_cli import FILE
from tests.test_workspace_isolation import B, IsolationTestCase


class LocalFileTests(IsolationTestCase):
    def setUp(self):
        super().setUp()
        admins.create_admin('lead', None)  # no server-wide role: team-b's owner, by membership
        self.ok('put', '/api/v1/workspaces/team-b/members/lead', json={'role': 'owner'})
        self.lead = {'X-API-Key': admins.issue_token('lead')[1]}
        self.elsewhere = self.db_path  # default's own database: not team-b's to open

    def refused(self, method, url, **kwargs):
        res = self.ok(method, url, status=403, headers=self.lead, **kwargs).get_json()
        self.assertEqual((res['code'], res['capability']), ('role_forbidden', 'connections.write'))
        return res

    def test_a_workspace_role_cant_point_a_connection_at_a_file(self):
        for fields in ({'db': 'sqlite', 'database': self.elsewhere},
                       {'db': 'duckdb', 'database': ':memory:', 'allowed_paths': ['/etc/']},
                       {'db': 'duckdb', 'database': os.path.join(self.tmp.name, 'other.duckdb')},
                       {'db': 'duckdb', 'database': ':memory:', 'allowed_paths': ['s3://bucket/', '/etc/']},
                       {'db': 'h2', 'database': 'mem:scratch'},
                       {'db': 'jdbc', 'jdbc_url': 'jdbc:sqlite:' + self.elsewhere, 'jar': '/tmp/driver.jar'}):
            with self.subTest(fields=fields):
                self.refused('post', f'{B}/api/v1/connections', json={'name': 'grab', **fields})
                self.refused('post', f'{B}/api/v1/connections/test', json=fields)  # a probe opens it too
                self.refused('post', f'{B}/api/v1/connections/databases', json=fields)
        self.ok('get', f'{B}/api/v1/connections/grab', status=404)

    def test_nor_through_a_configuration_file(self):
        doc = {**FILE, 'connections': [{'name': 'grab', 'db': 'sqlite', 'database': self.elsewhere}]}
        res = self.ok('post', f'{B}/api/v1/config/apply', status=400, headers=self.lead, json=doc).get_json()
        self.assertEqual(res['code'], 'invalid_config')
        self.assertIn('connections[grab]: A connection that opens files on this server', res['error'])
        self.ok('get', f'{B}/api/v1/connections/grab', status=404)

    def test_nor_repoint_one_a_server_administrator_set_up(self):
        for change in ({'database': self.elsewhere}, {'db': 'duckdb'}, {'db': 'h2', 'database': 'mem:x'}):
            with self.subTest(change=change):
                self.refused('patch', f'{B}/api/v1/connections/lite', json=change)
        self.assertEqual(self.ok('get', f'{B}/api/v1/connections/lite').get_json()['database'], self.b_db)

    def test_it_still_runs_tests_and_switches_off_its_workspaces_own(self):
        rows = self.ok('post', f'{B}/execute_sql', headers=self.lead,
                       json={'sql': 'SELECT title FROM film ORDER BY film_id', 'connection_name': 'lite'}).get_json()
        self.assertEqual([r['title'] for r in rows], ['Bravo', 'Bolero', 'Brazil'])
        self.ok('post', f'{B}/api/v1/connections/test', headers=self.lead, json={'name': 'lite'})
        self.ok('patch', f'{B}/api/v1/connections/lite', headers=self.lead, json={'active': False})
        self.ok('patch', f'{B}/api/v1/connections/lite', headers=self.lead, json={'active': True})

    def test_remote_storage_and_server_databases_are_a_workspaces_own(self):
        self.ok('post', f'{B}/api/v1/connections', status=201, headers=self.lead, json={
            'name': 'lake', 'db': 'duckdb', 'database': ':memory:', 'allowed_paths': ['s3://bucket/data/']})
        self.ok('post', f'{B}/api/v1/connections', status=201, headers=self.lead, json={
            'name': 'warehouse', 'db': 'postgres', 'host': 'db.internal', 'database': 'sales', 'active': False})

    def test_a_server_wide_role_sets_one_up_in_any_workspace(self):
        folder = os.path.join(self.tmp.name, 'files')
        os.mkdir(folder)
        self.ok('post', f'{B}/api/v1/connections', status=201, json={
            'name': 'files', 'db': 'duckdb', 'database': ':memory:', 'allowed_paths': [folder + '/']})
        self.ok('patch', f'{B}/api/v1/connections/lite', json={'database': self.b_db})

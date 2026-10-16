"""Configuration files and the CLI, per workspace (ADR 0023, delivery step 5): a file says the workspace it describes
(`workspace:`, `default` without one) and is applied there - over the API only to that workspace, so never anywhere
it doesn't say - and the CLI's commands act on one workspace's things with --workspace."""
import contextlib
import io
import os

import yaml

from queryapigate import admins, cli
from tests.test_workspace_isolation import ADMIN, B, IsolationTestCase

FILE = {'format': 'queryapigate-config', 'format_version': 1, 'workspace': 'team-b',
        'connections': [{'name': 'warehouse', 'db': 'sqlite', 'database': '/tmp/warehouse.db', 'active': False}]}


class ConfigFileTests(IsolationTestCase):
    def setUp(self):
        super().setUp()
        self.doc = dict(FILE)

    def test_a_file_is_applied_to_the_workspace_it_describes(self):
        applied = self.ok('post', f'{B}/api/v1/config/apply', json=self.doc).get_json()
        self.assertTrue(applied['applied'])
        self.ok('get', f'{B}/api/v1/connections/warehouse')
        self.ok('get', '/api/v1/connections/warehouse', status=404)
        managed = self.ok('get', f'{B}/api/v1/config/managed').get_json()['items']
        self.assertEqual([(m['kind'], m['name']) for m in managed], [('connections', 'warehouse')])
        self.assertEqual(self.ok('get', '/api/v1/config/managed').get_json()['items'], [])

    def test_never_to_another_one(self):
        res = self.ok('post', '/api/v1/config/apply', status=400, json=self.doc).get_json()
        self.assertEqual(res['code'], 'invalid_config')
        self.assertIn('/w/team-b/api/v1/config/apply', res['error'])
        unnamed = {k: v for k, v in self.doc.items() if k != 'workspace'}  # a file without one is default's
        self.ok('post', f'{B}/api/v1/config/diff', status=400, json=unnamed)
        self.assertEqual(self.code('post', '/api/v1/config/diff', 400, json={**self.doc, 'workspace': 'no-such'}),
                         'invalid_config')

    def test_a_workspaces_owner_applies_its_file_an_admin_there_doesnt(self):
        for name, role in (('lead', 'owner'), ('dev', 'admin')):
            admins.create_admin(name, None)
            self.ok('put', f'/api/v1/workspaces/team-b/members/{name}', json={'role': role})
        lead = {'X-API-Key': admins.issue_token('lead')[1]}
        dev = {'X-API-Key': admins.issue_token('dev')[1]}
        self.ok('post', f'{B}/api/v1/config/apply', headers=lead, json=self.doc)
        self.assertEqual(self.code('post', f'{B}/api/v1/config/apply', 403, headers=dev, json=self.doc),
                         'role_forbidden')


class CliTests(IsolationTestCase):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_export_runs_a_workspaces_query(self):
        target = os.path.join(self.tmp.name, '{name}.csv')
        self.assertEqual(self.run_cli('export', 'films', '--workspace', 'team-b', '--out', target)[0], 0)
        with open(os.path.join(self.tmp.name, 'films.csv')) as f:
            self.assertIn('Bravo', f.read())
        self.assertEqual(self.run_cli('export', 'films', '--out', target)[0], 0)  # default's, without it
        with open(os.path.join(self.tmp.name, 'films.csv')) as f:
            self.assertIn('Alpha', f.read())

    def test_config_export_names_its_workspace_and_applies_back_there(self):
        code, text, _ = self.run_cli('config', 'export', '--workspace', 'team-b')
        self.assertEqual(code, 0)
        doc = yaml.safe_load(text)
        self.assertEqual(doc['workspace'], 'team-b')
        self.assertEqual([c['name'] for c in doc['connections']], ['lite'])
        self.assertNotIn('workspace', yaml.safe_load(self.run_cli('config', 'export')[1]))  # default's: as before
        doc['connections'].append({'name': 'extra', 'db': 'sqlite', 'database': '/tmp/x.db', 'active': False})
        path = os.path.join(self.tmp.name, 'team-b.yaml')
        with open(path, 'w') as f:
            yaml.safe_dump(doc, f)
        self.assertEqual(self.run_cli('config', 'apply', path)[0], 0)  # the file says where
        self.ok('get', f'{B}/api/v1/connections/extra')
        self.ok('get', '/api/v1/connections/extra', status=404)

    def test_a_file_for_a_workspace_that_doesnt_exist_is_refused(self):
        path = os.path.join(self.tmp.name, 'nowhere.yaml')
        with open(path, 'w') as f:
            yaml.safe_dump({**FILE, 'workspace': 'no-such'}, f)
        code, _, err = self.run_cli('config', 'apply', path)
        self.assertEqual(code, 1)
        self.assertIn("no workspace named 'no-such' - create it first", err)

    def test_an_unknown_workspace_is_refused_before_anything_runs(self):
        code, _, err = self.run_cli('exports', 'list', '--workspace', 'no-such')
        self.assertEqual(code, 2)
        self.assertIn("no workspace named 'no-such'", err)
        self.assertEqual(self.client.get('/api/v1/workspaces/no-such', headers=ADMIN).status_code, 404)

    def test_against_a_running_server_a_file_goes_to_its_workspace(self):
        from unittest import mock
        seen = []

        def urlopen(request, timeout=None):
            path = request.full_url.replace('http://prod.example', '')
            seen.append(path)
            body = io.BytesIO(self.client.post(path, data=request.data, headers=dict(request.header_items())).data)
            body.__enter__ = lambda *a: body
            return body

        path = os.path.join(self.tmp.name, 'remote.yaml')
        with open(path, 'w') as f:
            yaml.safe_dump(FILE, f)
        with mock.patch('urllib.request.urlopen', urlopen), mock.patch.dict(os.environ, {'QAG_API_KEY': 'admin-key'}):
            code, _, _ = self.run_cli('config', 'apply', '--server', 'http://prod.example', path)
        self.assertEqual((code, seen), (0, ['/w/team-b/api/v1/config/apply']))
        self.ok('get', f'{B}/api/v1/connections/warehouse')

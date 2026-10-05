"""Named administrators and their admin tokens (ADR 0003, admins.py): who may sign in, what can't be lost (the last
owner, authentication itself), what the audit log records, the break-glass key, and the `queryapigate admins` CLI."""
import contextlib
import io
import logging
import os
import tempfile
import unittest
from datetime import date, timedelta
from unittest import mock

from queryapigate import admins, cli, create_app, store
from tests.helpers import create_key

ADMIN = {'X-API-Key': 'admin-key'}


class AdminTestCase(unittest.TestCase):
    shared_key = 'admin-key'

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = {'QUERYAPIGATE_HOME': self.tmp.name}
        if self.shared_key:
            env['QUERYAPIGATE_API_KEY'] = self.shared_key
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        if not self.shared_key:
            os.environ.pop('QUERYAPIGATE_API_KEY', None)
        patcher = mock.patch.dict(admins._break_glass, {'audited': None, 'owner_checked': None, 'owner': False})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = create_app().test_client()

    def token_for(self, name, role='admin', **kwargs):
        if not any(a['name'] == name for a in admins.list_admins()):
            admins.create_admin(name, role)
        return {'X-API-Key': admins.issue_token(name, **kwargs)[1]}

    def me(self, headers):
        return self.client.get('/api/v1/me', headers=headers)


class TokenTests(AdminTestCase):
    def test_a_token_signs_its_administrator_in(self):
        token = self.token_for('alice')
        self.assertTrue(token['X-API-Key'].startswith('qagadm_'))
        self.assertEqual(self.me(token).get_json()['name'], 'alice')

    def test_what_stops_a_token(self):
        cases = {
            'wrong': lambda: {'X-API-Key': 'qagadm_not-a-real-token'},
            'expired': lambda: self.token_for('old', expires_at=(date.today() - timedelta(days=1)).isoformat()),
        }
        for label, headers in cases.items():
            with self.subTest(label):
                self.assertEqual(self.me(headers()).status_code, 401)
        revoked = self.token_for('bob')
        token_id = admins.list_tokens('bob')[0]['id']
        admins.revoke_token('bob', token_id)
        self.assertEqual(self.me(revoked).status_code, 401)
        inactive = self.token_for('carol')
        admins.update_admin('carol', active=False)
        self.assertEqual(self.me(inactive).status_code, 401)
        admins.update_admin('carol', active=True)
        self.assertEqual(self.me(inactive).status_code, 200)

    def test_revoking_one_token_leaves_the_others(self):
        laptop, ci = self.token_for('dave', label='laptop'), self.token_for('dave', label='ci')
        admins.revoke_token('dave', next(t['id'] for t in admins.list_tokens('dave') if t['label'] == 'laptop'))
        self.assertEqual((self.me(laptop).status_code, self.me(ci).status_code), (401, 200))

    def test_only_the_hash_is_stored(self):
        token = self.token_for('erin')['X-API-Key']
        from queryapigate import db
        stored = db.connection().execute('SELECT * FROM admin_tokens').fetchall()
        self.assertNotIn(token, repr([tuple(row) for row in stored]))

    def test_use_is_recorded(self):
        self.me(self.token_for('frank'))
        self.assertIsNotNone(admins.get_admin('frank')['last_seen_at'])
        self.assertIsNotNone(admins.list_tokens('frank')[0]['last_used_at'])

    def test_a_deleted_administrator_takes_its_tokens_along(self):
        token = self.token_for('gina')
        admins.delete_admin('gina')
        self.assertEqual(self.me(token).status_code, 401)
        from queryapigate import db
        self.assertIsNone(db.connection().execute('SELECT 1 FROM admin_tokens').fetchone())


class NameTests(AdminTestCase):
    def test_an_administrator_and_a_key_never_share_a_name(self):
        admins.create_admin('alice', 'admin')
        res = create_key(self.client, headers=ADMIN, name='alice', connections=[])
        self.assertEqual((res.status_code, res.get_json()['code']), (409, 'name_taken'))
        create_key(self.client, headers=ADMIN, name='app', connections=[])
        with self.assertRaises(Exception) as caught:
            admins.create_admin('app', 'admin')
        self.assertEqual(caught.exception.code, 'name_taken')

    def test_reserved_and_bad_names_and_roles(self):
        for name, role, code in (('admin', 'owner', 'invalid_name'), ('cli', 'owner', 'invalid_name'),
                                 ('startup', 'owner', 'invalid_name'),
                                 ('a/b', 'owner', 'invalid_name'), ('ok', 'superuser', 'invalid_body')):
            with self.subTest(name=name, role=role), self.assertRaises(Exception) as caught:
                admins.create_admin(name, role)
            self.assertEqual(caught.exception.code, code)
        admins.create_admin('hal', 'owner')
        with self.assertRaises(Exception) as caught:
            admins.create_admin('hal', 'owner')
        self.assertEqual(caught.exception.code, 'admin_exists')


class WithoutSharedKeyTests(AdminTestCase):
    shared_key = None

    def test_authentication_stays_required_once_an_administrator_exists(self):
        self.assertEqual(self.client.get('/api/v1/me').status_code, 200)  # open: no keys, no administrators
        token = self.token_for('owner1', 'owner')
        self.assertEqual(self.client.get('/api/v1/me').status_code, 401)
        self.assertEqual(self.client.get('/catalog').status_code, 401)
        self.assertEqual(self.me(token).status_code, 200)

    def test_the_startup_warning_knows_an_owner_can_manage(self):
        from queryapigate import apikeys
        apikeys.create_key('app', connections=[])
        with self.assertLogs('queryapigate', level=logging.WARNING) as logs:
            create_app()
        self.assertTrue(any('nobody can manage connections' in line for line in logs.output), logs.output)
        admins.create_admin('owner1', 'owner')
        with self.assertNoLogs('queryapigate', level=logging.WARNING):
            create_app()

    def test_the_last_active_owner_cannot_be_lost(self):
        admins.create_admin('owner1', 'owner')
        admins.create_admin('helper', 'admin')
        for change in (lambda: admins.update_admin('owner1', role='admin'),
                       lambda: admins.update_admin('owner1', active=False),
                       lambda: admins.delete_admin('owner1')):
            with self.assertRaises(Exception) as caught:
                change()
            self.assertEqual(caught.exception.code, 'last_owner')
        self.assertEqual(admins.get_admin('owner1')['role'], 'owner')  # each refusal changed nothing
        admins.create_admin('owner2', 'owner')
        admins.update_admin('owner1', role='admin')  # another owner exists now


class AuditTests(AdminTestCase):
    def test_the_audit_log_says_who_and_how(self):
        body = {'name': 'lite', 'db': 'sqlite', 'database': ':memory:'}
        self.client.post('/api/v1/connections', headers=self.token_for('alice'), json=body)
        self.client.delete('/api/v1/connections/lite', headers=ADMIN, json={'reason': 'test'})
        entries = [(e['actor'], e['via'], e['action']) for e in store.read_audit_log()]
        self.assertEqual(entries[-2:], [('alice', 'token', 'create_connection'),
                                        ('admin', 'break-glass', 'delete_connection')])

    def test_how_they_signed_in_comes_from_the_request_not_the_name(self):
        with self.client.application.test_request_context():
            from flask import g
            g.permission = admins.permission_for({'name': 'cli', 'role': 'owner'}, 'token')
            store.record_audit('cli', 'something', 'x')
        self.assertEqual(store.read_audit_log()[-1]['via'], 'token')
        store.record_audit('cli', 'something', 'x')  # outside a request: the CLI itself
        self.assertEqual(store.read_audit_log()[-1]['via'], 'cli')


class BreakGlassTests(AdminTestCase):
    def alerts(self, headers=ADMIN):
        return [a['kind'] for a in self.client.get('/api/v1/alerts', headers=headers).get_json()['items']]

    def test_quiet_while_no_owner_exists(self):
        with self.assertNoLogs('queryapigate', level=logging.WARNING):
            self.me(ADMIN)
        self.assertNotIn('break_glass_used', [e['action'] for e in store.read_audit_log()])

    def test_a_change_merely_named_break_glass_used_raises_nothing(self):
        owner = self.token_for('owner1', 'owner')
        self.client.post('/api/v1/roles', headers=owner, json={'name': 'break_glass_used', 'connections': []})
        self.assertNotIn('break_glass_used', self.alerts(owner))

    def test_logged_audited_and_alerted_once_an_owner_exists(self):
        owner = self.token_for('owner1', 'owner')
        with self.assertLogs('queryapigate', level=logging.WARNING) as logs:
            self.me(ADMIN)
            self.me(ADMIN)
        self.assertEqual(sum('break-glass key) was used for GET /api/v1/me' in line for line in logs.output), 2)
        used = [e for e in store.read_audit_log() if e['action'] == 'break_glass_used']
        self.assertEqual([(e['actor'], e['via'], e['target']) for e in used],
                         [('admin', 'break-glass', 'GET /api/v1/me')])  # audited once per interval
        self.assertIn('break_glass_used', self.alerts(owner))


class CliTests(AdminTestCase):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_the_first_owner_from_the_command_line(self):
        code, out, _ = self.run_cli('admins', 'create', 'alice', '--role', 'owner', '--email', 'alice@corp.com',
                                    '--label', 'laptop')
        self.assertEqual(code, 0)
        token = next(line.strip() for line in out.splitlines() if line.strip().startswith('qagadm_'))
        me = self.me({'X-API-Key': token}).get_json()
        self.assertEqual((me['name'], me['role']), ('alice', 'owner'))
        expires = admins.list_tokens('alice')[0]['expires_at']
        self.assertEqual(expires, (date.today() + timedelta(days=cli.DEFAULT_TOKEN_DAYS)).isoformat())
        self.assertEqual([(e['actor'], e['via'], e['action']) for e in store.read_audit_log()],
                         [('cli', 'cli', 'create_admin'), ('cli', 'cli', 'issue_admin_token')])

    def test_another_token_and_the_list(self):
        self.run_cli('admins', 'create', 'ci-deploy', '--role', 'developer')
        code, out, _ = self.run_cli('admins', 'token', 'ci-deploy', '--expires', 'never', '--label', 'ci')
        self.assertEqual(code, 0)
        self.assertIn('never expires', out)
        code, out, _ = self.run_cli('admins', 'list')
        self.assertIn('ci-deploy  developer', out)

    def test_errors_are_reported(self):
        code, _, err = self.run_cli('admins', 'token', 'nobody')
        self.assertEqual(code, 1)
        self.assertIn("No administrator named 'nobody'", err)


if __name__ == '__main__':
    unittest.main()

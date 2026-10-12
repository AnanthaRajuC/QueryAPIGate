"""Vault (ADR 0015, BACKLOG #98), slice 2: `${vault:...}` from the KV engine, `vault-database` credentials for a
lease, the pool retiring a connection before its lease ends, the three ways of signing in, and leases revoked at exit.

Against a fake Vault always; against a real one - and a PostgreSQL it manages users in - when
QUERYAPIGATE_TEST_VAULT_ADDR is set (CI's vault job): with QUERYAPIGATE_TEST_VAULT_TOKEN (a root token),
QUERYAPIGATE_TEST_VAULT_PG (host:port of that PostgreSQL, from here) and QUERYAPIGATE_TEST_VAULT_PG_FROM_VAULT (the
same, as Vault reaches it)."""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from queryapigate import config, db, pool, secretrefs, store, vault
from queryapigate.errors import ApiError
from queryapigate.services import connections


class FakeVault(ThreadingHTTPServer):
    """Enough of Vault's API: the three sign-ins, KV v1 and v2, database credentials, lease revocation."""

    def __init__(self):
        super().__init__(('127.0.0.1', 0), FakeVaultHandler)
        self.tokens = {'root'}
        self.calls = []
        self.kv = {'secret/data/reporting/warehouse': {'data': {'data': {'password': 'v2-password'},
                                                                  'metadata': {'version': 3}}},
                   'kv1/reporting/legacy': {'data': {'password': 'v1-password'}, 'lease_duration': 600}}
        self.lease_seconds = 3600
        self.issued = 0
        self.revoked = []
        self.down = False

    @property
    def url(self):
        return f'http://127.0.0.1:{self.server_address[1]}'


class FakeVaultHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def answer(self, status, body=None):
        data = json.dumps(body or {}).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def handle_any(self, method):
        vault_ = self.server
        path = self.path[len('/v1/'):]
        body = json.loads(self.rfile.read(int(self.headers.get('Content-Length') or 0)) or b'{}')
        vault_.calls.append((method, path, self.headers.get('X-Vault-Token'), body))
        if vault_.down:
            return self.answer(503, {'errors': ['Vault is sealed']})
        if path == 'auth/kubernetes/login':
            ok = body == {'role': 'queryapigate', 'jwt': 'the-service-account-token'}
            return self.answer(200, {'auth': {'client_token': 'k8s-token', 'lease_duration': 3600}}) if ok \
                else self.answer(403, {'errors': ['permission denied']})
        if path == 'auth/approle/login':
            ok = body == {'role_id': 'rid', 'secret_id': 'sid'}
            return self.answer(200, {'auth': {'client_token': 'approle-token', 'lease_duration': 3600}}) if ok \
                else self.answer(403, {'errors': ['invalid role or secret ID']})
        if self.headers.get('X-Vault-Token') not in vault_.tokens | {'k8s-token', 'approle-token'}:
            return self.answer(403, {'errors': ['permission denied']})
        if path in vault_.kv:
            return self.answer(200, vault_.kv[path])
        if path == 'database/creds/reporting-ro':
            vault_.issued += 1
            return self.answer(200, {'lease_id': f'database/creds/reporting-ro/{vault_.issued}',
                                     'lease_duration': vault_.lease_seconds,
                                     'data': {'username': f'v-user-{vault_.issued}',
                                              'password': f'pw-{vault_.issued}'}})
        if path == 'sys/leases/revoke':
            vault_.revoked.append(body['lease_id'])
            return self.answer(204)
        return self.answer(404, {'errors': []})

    def do_GET(self):
        self.handle_any('GET')

    def do_POST(self):
        self.handle_any('POST')

    def do_PUT(self):
        self.handle_any('PUT')


class VaultTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.vault = FakeVault()
        threading.Thread(target=self.vault.serve_forever, daemon=True).start()
        self.addCleanup(self.vault.server_close)
        self.addCleanup(self.vault.shutdown)
        patcher = mock.patch.dict(os.environ, {
            'QUERYAPIGATE_HOME': self.tmp.name, 'QUERYAPIGATE_VAULT_ADDR': self.vault.url,
            'QUERYAPIGATE_VAULT_TOKEN': 'root', 'QUERYAPIGATE_VAULT_PATHS': 'secret/data/reporting/,kv1/reporting/',
            'QUERYAPIGATE_VAULT_DATABASE_ROLES': 'reporting-ro'})
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ('QUERYAPIGATE_VAULT_AUTH', 'QUERYAPIGATE_VAULT_TOKEN_FILE', 'QUERYAPIGATE_SECRET_CACHE_SECONDS'):
            os.environ.pop(name, None)
        db.init_schema()
        for reset in (secretrefs.cache.clear, vault.client.reset, secretrefs._expiries.clear):
            reset()
            self.addCleanup(reset)

    def refused(self, value):
        with self.assertRaises(ApiError) as caught:
            secretrefs.check_value(value)
        return caught.exception.message


class KvTests(VaultTestCase):
    def test_a_field_of_a_kv_secret_version_2_or_1(self):
        self.assertEqual(secretrefs.expand('${vault:secret/data/reporting/warehouse#password}'), 'v2-password')
        self.assertEqual(secretrefs.expand('${vault:kv1/reporting/legacy#password}'), 'v1-password')

    def test_kept_then_asked_again(self):
        ref = '${vault:secret/data/reporting/warehouse#password}'
        secretrefs.expand(ref)
        secretrefs.expand(ref)
        self.assertEqual(sum(1 for c in self.vault.calls if c[1].startswith('secret/')), 1)

    def test_only_what_the_deployer_allows(self):
        self.assertIn("isn't under a path QUERYAPIGATE_VAULT_PATHS allows",
                      self.refused('${vault:secret/data/payroll#password}'))
        self.assertIn('name the field after the path', self.refused('${vault:secret/data/reporting/warehouse}'))
        self.assertIn("isn't a Vault path", self.refused('${vault:secret/data/reporting/../payroll#password}'))
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_VAULT_PATHS': ''}):
            self.assertIn('QUERYAPIGATE_VAULT_PATHS is not set',
                          self.refused('${vault:secret/data/reporting/warehouse#password}'))
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_VAULT_ADDR': ''}):
            self.assertIn('QUERYAPIGATE_VAULT_ADDR is not set', self.refused('${vault:secret/data/reporting/w#p}'))
        self.assertIn("isn't under a path", self.refused('${vault:secret/data/reportingx/w#p}'))  # a prefix ends at /

    def test_a_missing_field_or_a_vault_that_answers_no(self):
        with self.assertRaises(ApiError) as caught:
            secretrefs.expand('${vault:secret/data/reporting/warehouse#username}')
        self.assertEqual(caught.exception.code, 'secret_unavailable')
        self.assertIn("has no field 'username'", caught.exception.message)
        self.vault.down = True
        with self.assertRaises(ApiError) as caught:
            secretrefs.expand('${vault:kv1/reporting/legacy#password}')
        self.assertIn('Vault answered 503 for kv1/reporting/legacy: Vault is sealed', caught.exception.message)


class SignInTests(VaultTestCase):
    def test_kubernetes_with_the_pods_service_account_token(self):
        token = os.path.join(self.tmp.name, 'token')
        with open(token, 'w') as f:
            f.write('the-service-account-token\n')
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_VAULT_AUTH': 'kubernetes', 'QUERYAPIGATE_VAULT_K8S_ROLE':
                                          'queryapigate', 'QUERYAPIGATE_VAULT_K8S_TOKEN_FILE': token}):
            config.check_settings()
            self.assertEqual(secretrefs.expand('${vault:kv1/reporting/legacy#password}'), 'v1-password')
        self.assertEqual(self.vault.calls[-1][2], 'k8s-token')

    def test_approle(self):
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_VAULT_AUTH': 'approle', 'QUERYAPIGATE_VAULT_ROLE_ID': 'rid',
                                          'QUERYAPIGATE_VAULT_SECRET_ID': 'sid'}):
            self.assertEqual(secretrefs.expand('${vault:kv1/reporting/legacy#password}'), 'v1-password')
        self.assertEqual(self.vault.calls[-1][2], 'approle-token')

    def test_a_token_file_is_read_again_when_vault_refuses_the_token(self):
        path = os.path.join(self.tmp.name, 'agent-token')
        with open(path, 'w') as f:
            f.write('old')
        self.vault.tokens = {'old'}
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_VAULT_TOKEN': '', 'QUERYAPIGATE_VAULT_TOKEN_FILE': path}):
            secretrefs.expand('${vault:kv1/reporting/legacy#password}')
            self.vault.tokens = {'new'}  # Vault Agent renewed it, and the old one no longer works
            with open(path, 'w') as f:
                f.write('new')
            secretrefs.cache.clear()
            self.assertEqual(secretrefs.expand('${vault:kv1/reporting/legacy#password}'), 'v1-password')
        self.assertEqual([c[2] for c in self.vault.calls[-2:]], ['old', 'new'])

    def test_settings_name_what_is_missing(self):
        for env, words in (({'QUERYAPIGATE_VAULT_AUTH': 'ldap'}, 'must be one of: token, kubernetes, approle'),
                           ({'QUERYAPIGATE_VAULT_AUTH': 'kubernetes'}, 'set QUERYAPIGATE_VAULT_K8S_ROLE'),
                           ({'QUERYAPIGATE_VAULT_AUTH': 'approle'}, 'set QUERYAPIGATE_VAULT_ROLE_ID'),
                           ({'QUERYAPIGATE_VAULT_TOKEN': ''}, 'set QUERYAPIGATE_VAULT_TOKEN or'),
                           ({'QUERYAPIGATE_VAULT_ADDR': 'vault:8200'}, 'must be an http:// or https:// address')):
            with self.subTest(env=env), mock.patch.dict(os.environ, env), self.assertRaises(ValueError) as caught:
                config.check_settings()
            self.assertIn(words, str(caught.exception))


class CredentialTests(VaultTestCase):
    def setUp(self):
        super().setUp()
        self.clock = [1000.0]
        for target in ('queryapigate.secretrefs.time.monotonic', 'queryapigate.pool.time.monotonic'):
            patcher = mock.patch(target, lambda: self.clock[0])
            patcher.start()
            self.addCleanup(patcher.stop)
        self.details = {'db': 'postgres', 'host': 'h', 'credentials': {'source': 'vault-database',
                                                                      'role': 'reporting-ro'}}

    def test_a_user_and_password_for_the_lease_shared_by_the_connections_opened_meanwhile(self):
        first = secretrefs.resolve_credentials(self.details)
        self.assertEqual((first['user'], first['password']), ('v-user-1', 'pw-1'))
        self.assertNotIn('credentials', first)  # a driver never sees it
        self.clock[0] += 60
        self.assertEqual(secretrefs.resolve_credentials(self.details)['user'], 'v-user-1')
        self.assertEqual(self.vault.issued, 1)

    def test_a_new_pair_before_the_lease_ends_and_the_old_never_in_its_last_moments(self):
        secretrefs.resolve_credentials(self.details)
        self.assertEqual(secretrefs.credential_expiry({'user': 'v-user-1', 'password': 'pw-1'}), 1000.0 + 3600 - 300)
        self.clock[0] += 3600 * 0.8 + 1  # past the refresh point
        self.assertEqual(secretrefs.resolve_credentials(self.details)['user'], 'v-user-2')

    def test_a_short_lease_is_renewed_before_its_margin(self):
        self.vault.lease_seconds = 12  # a 5 s margin reaches past 80% of it
        secretrefs.resolve_credentials(self.details)
        self.clock[0] += 7.5
        self.assertEqual(secretrefs.resolve_credentials(self.details)['user'], 'v-user-2')

    def test_vault_failing_is_forgiven_within_the_lease_and_never_after(self):
        secretrefs.resolve_credentials(self.details)
        self.vault.down = True
        self.clock[0] += 3000  # past the refresh point, within what's usable
        with self.assertLogs('queryapigate', 'WARNING'):
            self.assertEqual(secretrefs.resolve_credentials(self.details)['user'], 'v-user-1')
        self.clock[0] += 400  # past lease end minus its margin
        with self.assertRaises(ApiError) as caught:
            secretrefs.resolve_credentials(self.details)
        self.assertEqual((caught.exception.status, caught.exception.code), (503, 'secret_unavailable'))

    def test_the_pool_retires_a_connection_before_its_lease_ends(self):
        opened = []

        class Driver:
            def connect(self, details, read_only):
                opened.append(details['user'])
                return object()

            def close(self, session):
                pass

            def is_alive(self, session):
                return True

            def reset(self, session):
                pass

        connection_pool = pool.ConnectionPool()
        for advance in (0, 60, 3600 * 0.8 + 1):  # a new pair after the refresh point - and the old pool key retired
            self.clock[0] += advance
            details = secretrefs.resolve_credentials(self.details)
            with connection_pool.checkout(Driver(), details, True):
                pass
        self.assertEqual(opened, ['v-user-1', 'v-user-2'])
        # the same pair, its session idle in the pool, past its usable end: closed, not handed out
        self.clock[0] += 3600
        self.assertEqual(connection_pool._acquire(pool.fingerprint(details, True)), None)

    def test_checked_when_saved(self):
        def refused(**fields):
            with self.assertRaises(ApiError) as caught:
                connections.create({'name': 'c', 'db': 'postgres', 'host': 'h', **fields}, 't')
            return caught.exception.message

        self.assertIn('leave out password, user', refused(user='u', password='p', credentials=self.details[
            'credentials']))
        self.assertIn("'payroll' isn't a role QUERYAPIGATE_VAULT_DATABASE_ROLES allows",
                      refused(credentials={'source': 'vault-database', 'role': 'payroll'}))
        self.assertIn('credentials.source must be one of: vault-database',
                      refused(credentials={'source': 'ldap', 'role': 'r'}))
        self.assertIn('is {"source": ..., "role": ...}', refused(credentials={'source': 'vault-database'}))
        with self.assertRaises(ApiError) as caught:
            connections.create({'name': 'c', 'db': 'sqlite', 'database': 'x.db',
                                'credentials': self.details['credentials']}, 't')
        self.assertIn('no user and password for credentials to give', caught.exception.message)

    def test_a_connection_resolves_to_them(self):
        connections.create({'name': 'w', 'db': 'postgres', 'host': 'h', 'credentials': self.details['credentials']},
                           't')
        resolved = store.get_connection('w')
        self.assertEqual((resolved['user'], resolved['password']), ('v-user-1', 'pw-1'))
        self.assertEqual(store.read_connections()['w']['credentials'], self.details['credentials'])

    def test_leases_are_revoked_at_exit(self):
        secretrefs.resolve_credentials(self.details)
        self.clock[0] += 3600
        secretrefs.resolve_credentials(self.details)
        vault.client.revoke_leases()
        self.assertEqual(sorted(self.vault.revoked), ['database/creds/reporting-ro/1', 'database/creds/reporting-ro/2'])


# --------------------------------------------------------------------------------------
# Against a real Vault and the PostgreSQL it manages users in
# --------------------------------------------------------------------------------------

REAL = os.environ.get('QUERYAPIGATE_TEST_VAULT_ADDR')


def _vault(method, path, body=None):
    data = json.dumps(body).encode() if body else None
    request = urllib.request.Request(f"{REAL}/v1/{path}", method=method, data=data,
                                     headers={'X-Vault-Token': os.environ.get('QUERYAPIGATE_TEST_VAULT_TOKEN', 'root'),
                                              'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=10) as response:
        raw = response.read()
    return json.loads(raw) if raw else {}


@unittest.skipUnless(REAL, 'set QUERYAPIGATE_TEST_VAULT_ADDR (and the rest) to test against a real Vault')
class RealVaultTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        mounts = _vault('GET', 'sys/mounts')
        if 'database/' not in mounts.get('data', mounts):
            _vault('POST', 'sys/mounts/database', {'type': 'database'})
        from_vault = os.environ['QUERYAPIGATE_TEST_VAULT_PG_FROM_VAULT']
        _vault('POST', 'database/config/qag-test', {
            'plugin_name': 'postgresql-database-plugin', 'allowed_roles': 'qag-short',
            'connection_url': f'postgresql://{{{{username}}}}:{{{{password}}}}@{from_vault}/postgres?sslmode=disable',
            'username': 'postgres', 'password': 'pw'})
        _vault('POST', 'database/roles/qag-short', {
            'db_name': 'qag-test', 'default_ttl': '12s', 'max_ttl': '1h',
            'creation_statements': ['CREATE ROLE "{{name}}" WITH LOGIN PASSWORD \'{{password}}\' VALID UNTIL '
                                    '\'{{expiration}}\'; GRANT pg_read_all_data TO "{{name}}";']})
        _vault('POST', 'secret/data/qag-test/warehouse', {'data': {'password': 'from-real-vault'}})

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        host, port = os.environ['QUERYAPIGATE_TEST_VAULT_PG'].rsplit(':', 1)
        self.connection = {'name': 'warehouse', 'db': 'postgres', 'host': host, 'port': int(port),
                           'database': 'postgres', 'sslmode': 'disable',
                           'credentials': {'source': 'vault-database', 'role': 'qag-short'}}
        self.env = {'QUERYAPIGATE_HOME': self.tmp.name, 'QUERYAPIGATE_VAULT_ADDR': REAL,
                    'QUERYAPIGATE_VAULT_TOKEN': os.environ.get('QUERYAPIGATE_TEST_VAULT_TOKEN', 'root'),
                    'QUERYAPIGATE_VAULT_PATHS': 'secret/data/qag-test/',
                    'QUERYAPIGATE_VAULT_DATABASE_ROLES': 'qag-short'}
        patcher = mock.patch.dict(os.environ, self.env)
        patcher.start()
        self.addCleanup(patcher.stop)
        db.init_schema()
        for reset in (secretrefs.cache.clear, vault.client.reset, secretrefs._expiries.clear):
            reset()
            self.addCleanup(reset)

    def who(self):
        from queryapigate import engine
        return engine.execute_sql('SELECT current_user AS who', 'warehouse', 10, 0).rows[0][0]

    def test_a_query_runs_as_a_user_vault_made_and_moves_to_the_next_before_the_lease_ends(self):
        connections.create(self.connection, 't')
        first = self.who()
        self.assertTrue(first.startswith('v-token-qag-shor'), first)
        self.assertEqual(self.who(), first)  # the same lease, and the pooled connection
        time.sleep(7.5)  # a 12 s lease: from 7 s on (its 5 s margin) neither the pair nor its connection is used
        second = self.who()
        self.assertNotEqual(second, first)
        self.assertEqual(secretrefs.expand('${vault:secret/data/qag-test/warehouse#password}'), 'from-real-vault')

    def test_the_leases_a_process_took_are_revoked_when_it_exits(self):
        def leases():
            try:
                return set(_vault('LIST', 'sys/leases/lookup/database/creds/qag-short')['data']['keys'])
            except urllib.error.HTTPError:
                return set()

        before = leases()
        code = ('from queryapigate import db, engine, config\nfrom queryapigate.services import connections\n'
                f'config.check_settings(); db.init_schema(); connections.create({self.connection!r}, "t")\n'
                'print(engine.execute_sql("SELECT 1", "warehouse", 1, 0).rows)\n'
                'from queryapigate import vault; print(len(vault.client._leases))')
        out = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, env={**os.environ,
                                                                                              **self.env})
        self.assertEqual(out.stdout.split(), ['[[1]]', '1'], out.stderr)
        self.assertEqual(leases() - before, set())


if __name__ == '__main__':
    unittest.main()

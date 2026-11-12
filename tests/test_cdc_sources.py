"""CDC sources through the Management API (ADR 0033 §11): the Debezium Servers that tell live queries what changed, as
workspace objects - a secret generated and shown once, stored encrypted, rotated with a grace period; posts verified
against it at /ingest/cdc/<name> (/w/<workspace>/... outside `default`); QUERYAPIGATE_CDC_SOURCES still working."""
import base64
import json
import os
from unittest import mock

from cryptography.fernet import Fernet

from queryapigate import admins, cdcsources, create_app, db, live, rekey, store
from tests.test_api_v1 import ADMIN, V1TestCase
from tests.test_live import SECRET, change, signed

LIST = '/api/v1/cdc-sources'
ONE = '/api/v1/cdc-sources/{name}'
ROTATE = '/api/v1/cdc-sources/{name}/rotate'
PREVIOUS = '/api/v1/cdc-sources/{name}/previous'


class CdcSourceTestCase(V1TestCase):
    setting = {}

    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, {'QUERYAPIGATE_LIVE_QUERIES': '1',
                                               'QUERYAPIGATE_CDC_SOURCES': json.dumps(self.setting)})
        patcher.start()
        self.addCleanup(patcher.stop)
        # What ingest signals, wherever the store keeps it (the events table, or a ClickHouse run store)
        self.written = mock.patch.object(live, 'write_signal', wraps=live.write_signal).start()
        self.addCleanup(mock.patch.stopall)

    def source(self, name='shop', status=201, headers=ADMIN, prefix='', **fields):
        body = {'name': name, 'connection': 'lite', **fields}
        return self.call('post', f'{prefix}{LIST}', LIST, status, headers=headers, json=body)

    def post(self, secret, path='/ingest/cdc/shop', body=None, server='shop'):
        body = body if body is not None else json.dumps([change('film', name=server)]).encode()
        return self.client.post(path, data=body, headers=signed(body, secret))

    def signals(self):
        """(workspace, source, tables) of each signal stored, in order - each one written through the store."""
        return [(columns[-1], columns[1], json.loads(columns[7])['tables'])
                for columns in (call.args[0] for call in self.written.call_args_list)]

    def stored(self, name='shop', workspace='default'):
        row = db.connection().execute('SELECT details_json, last_received_at FROM cdc_sources '
                                      'WHERE workspace = ? AND name = ?', (workspace, name)).fetchone()
        return json.loads(row['details_json']), row['last_received_at']

    def audit(self, action):
        return [entry for entry in store.read_audit_log() if entry['action'] == action]


class LifecycleTests(CdcSourceTestCase):
    def test_created_with_a_secret_shown_once(self):
        res = self.source(server_name='shop')
        self.assertEqual(res.headers['Cache-Control'], 'no-store')
        created = res.get_json()
        self.assertTrue(created['secret'].startswith('whsec_'))
        self.assertEqual(len(base64.b64decode(created['secret'].removeprefix('whsec_'))), 32)
        self.assertEqual((created['connection'], created['server_name'], created['ingest_path'], created['managed_by']),
                         ('lite', 'shop', '/ingest/cdc/shop', None))
        self.assertIsNone(created['last_received_at'])
        shown = self.call('get', f'{LIST}/shop', ONE, 200).get_json()
        self.assertNotIn('secret', shown)
        listed = self.call('get', LIST, LIST, 200).get_json()['items']
        self.assertEqual([s['name'] for s in listed], ['shop'])
        self.assertNotIn('secret', listed[0])
        self.assertNotIn(created['secret'], json.dumps(store.read_audit_log()))
        self.assertEqual(self.audit('create_cdc_source')[0]['changes'], {'connection': 'lite', 'server_name': 'shop'})

    def test_two_sources_never_share_a_secret(self):
        self.assertNotEqual(self.source('a').get_json()['secret'], self.source('b').get_json()['secret'])

    def test_what_a_source_may_be(self):
        res = self.call('post', LIST, LIST, 400, json={'name': 'x', 'connection': 'lite', 'secret': SECRET})
        self.assertEqual(res.get_json()['code'], 'unknown_field')
        self.assertIn('generated', res.get_json()['error'])
        self.assertEqual(self.source(connection='nope', status=404).get_json()['code'], 'connection_not_found')
        self.assertEqual(self.source('has space', status=400).get_json()['code'], 'invalid_name')
        self.call('post', LIST, LIST, 400, json={'name': 'x'})
        self.call('post', LIST, LIST, 400, json={'name': 'x', 'connection': 'lite', 'server_name': 7})
        self.source()
        self.assertEqual(self.source(status=409).get_json()['code'], 'cdc_source_exists')

    def test_changed_with_if_match_and_audited(self):
        self.source()
        etag = self.call('get', f'{LIST}/shop', ONE, 200).headers['ETag']
        changed = self.call('patch', f'{LIST}/shop', ONE, 200, headers={**ADMIN, 'If-Match': etag},
                            json={'server_name': 'shop-eu'}).get_json()
        self.assertEqual(changed['server_name'], 'shop-eu')
        self.call('patch', f'{LIST}/shop', ONE, 412, headers={**ADMIN, 'If-Match': etag}, json={'server_name': None})
        self.assertIsNone(self.call('patch', f'{LIST}/shop', ONE, 200, json={'server_name': None})
                          .get_json()['server_name'])
        self.assertEqual(self.call('patch', f'{LIST}/shop', ONE, 400, json={'secret': 'x'}).get_json()['code'],
                         'unknown_field')
        self.assertEqual(self.audit('update_cdc_source')[0]['changes'],
                         {'server_name': {'from': None, 'to': 'shop-eu'}})

    def test_deleted(self):
        secret = self.source().get_json()['secret']
        self.call('delete', f'{LIST}/shop', ONE, 204)
        self.call('get', f'{LIST}/shop', ONE, 404)
        self.assertEqual(self.post(secret).status_code, 404)
        self.assertEqual(len(self.audit('delete_cdc_source')), 1)

    def test_the_secret_is_encrypted_at_rest_with_a_secret_key(self):
        key = Fernet.generate_key().decode()
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_SECRET_KEY': key}):
            secret = self.source().get_json()['secret']
            stored = self.stored()[0]['secret']
            self.assertTrue(stored.startswith('enc:'))
            self.assertNotIn(secret, json.dumps(self.stored()))
            self.assertEqual(self.post(secret).status_code, 204)

    def test_reading_needs_connections_read_and_changing_connections_write(self):
        self.source()
        admins.create_admin('ann', 'auditor')
        _, token = admins.issue_token('ann')
        auditor = {'X-API-Key': token}
        self.call('get', LIST, LIST, 200, headers=auditor)
        res = self.source('other', status=403, headers=auditor).get_json()
        self.assertEqual((res['code'], res['capability']), ('role_forbidden', 'connections.write'))
        self.call('post', f'{LIST}/shop/rotate', ROTATE, 403, headers=auditor)
        self.assertEqual(self.client.get(LIST, headers={'X-API-Key': 'nope'}).status_code, 401)

    def test_the_api_works_with_live_queries_off_and_ingest_doesnt(self):
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_LIVE_QUERIES': ''}):
            secret = self.source().get_json()['secret']
            self.assertEqual(self.post(secret).status_code, 404)
        self.assertEqual(self.post(secret).status_code, 204)


class IngestTests(CdcSourceTestCase):
    def test_a_post_signed_with_its_secret_is_a_signal(self):
        secret = self.source().get_json()['secret']
        self.assertEqual(self.post(secret).status_code, 204)
        self.assertEqual(self.signals(), [('default', 'cdc:shop', ['main.film'])])

    def test_an_unknown_source_and_a_bad_signature_answer_alike(self):
        self.source()
        wrong = self.post(SECRET)
        unknown = self.post(SECRET, '/ingest/cdc/nope')
        self.assertEqual((wrong.status_code, unknown.status_code), (404, 404))
        self.assertEqual(wrong.get_json()['code'], unknown.get_json()['code'])
        self.assertEqual(self.signals(), [])

    def test_server_name_refuses_another_server(self):
        secret = self.source(server_name='shop').get_json()['secret']
        res = self.post(secret, server='other')
        self.assertEqual((res.status_code, res.get_json()['code']), (422, 'invalid_change'))

    def test_last_received_at_moves_at_most_once_a_minute_and_only_when_verified(self):
        secret = self.source().get_json()['secret']
        self.post(SECRET)
        self.assertIsNone(self.stored()[1])
        self.post(secret, body=b'[]')  # a heartbeat-like post: nothing changed, but it's connected
        first = self.stored()[1]
        self.assertIsNotNone(first)
        self.assertEqual(self.call('get', f'{LIST}/shop', ONE, 200).get_json()['last_received_at'], first)
        with mock.patch.object(db, 'transaction', side_effect=AssertionError('no write within the minute')):
            self.assertEqual(self.post(secret, body=b'[]').status_code, 204)

    def test_a_status_write_failing_never_fails_the_post(self):
        secret = self.source().get_json()['secret']
        with mock.patch.object(cdcsources.db, 'transaction', side_effect=OSError('disk full')):
            source = cdcsources.find('shop')
            cdcsources.mark_received(source, 'shop')  # swallowed
        self.assertEqual(self.post(secret).status_code, 204)


class RotationTests(CdcSourceTestCase):
    def rotate(self, status=200, **body):
        return self.call('post', f'{LIST}/shop/rotate', ROTATE, status, json=body)

    def test_both_secrets_verify_during_the_grace_period(self):
        old = self.source().get_json()['secret']
        rotated = self.rotate().get_json()
        new = rotated['secret']
        self.assertNotEqual(new, old)
        self.assertIsNotNone(rotated['previous_expires_at'])
        self.assertEqual((self.post(old).status_code, self.post(new).status_code), (204, 204))
        self.assertNotIn(new, json.dumps(store.read_audit_log()))
        self.assertEqual(self.audit('rotate_cdc_source_secret')[0]['changes']['grace_minutes'], 60)

    def test_a_second_rotation_waits_for_the_first_s_grace_period(self):
        old = self.source().get_json()['secret']
        self.rotate()
        self.assertEqual(self.rotate(409).get_json()['code'], 'cdc_source_rotation_pending')
        ended = self.call('delete', f'{LIST}/shop/previous', PREVIOUS, 200).get_json()
        self.assertIsNone(ended['previous_expires_at'])
        self.assertEqual(self.post(old).status_code, 404)
        self.rotate()
        self.assertEqual(len(self.audit('end_cdc_source_previous_secret')), 1)

    def test_grace_zero_stops_the_old_secret_at_once(self):
        old = self.source().get_json()['secret']
        new = self.rotate(grace_minutes=0).get_json()['secret']
        self.assertEqual((self.post(old).status_code, self.post(new).status_code), (404, 204))
        self.rotate(grace_minutes=0)  # nothing pending

    def test_an_expired_grace_period_stops_the_old_secret(self):
        old = self.source().get_json()['secret']
        self.rotate()
        with db.transaction() as conn:
            details = self.stored()[0]
            details['previous_expires_at'] = '2000-01-01 00:00:00'
            conn.execute("UPDATE cdc_sources SET details_json = ? WHERE name = 'shop'", (json.dumps(details),))
        self.assertEqual(self.post(old).status_code, 404)
        self.assertIsNone(self.call('get', f'{LIST}/shop', ONE, 200).get_json()['previous_expires_at'])

    def test_grace_minutes_is_checked(self):
        self.source()
        for bad in (-1, 10081, 'soon', True):
            with self.subTest(bad=bad):
                self.rotate(400, grace_minutes=bad)
        self.rotate(400, other=1)


class SecretKeyTests(CdcSourceTestCase):
    """The secrets under QUERYAPIGATE_SECRET_KEY's rotation (ADR 0009): `rekey` moves both a source's secrets - the
    current one and the one in its grace period - and the first start with a key encrypts ones saved without it."""

    def keys(self, value):
        return mock.patch.dict(os.environ, {'QUERYAPIGATE_SECRET_KEY': value})

    def under(self, key, field):
        return Fernet(key.encode()).decrypt(self.stored()[0][field][len('enc:'):].encode()).decode()

    def test_rekey_moves_both_secrets_and_posts_still_verify(self):
        old_key, new_key = Fernet.generate_key().decode(), Fernet.generate_key().decode()
        with self.keys(old_key):
            first = self.source().get_json()['secret']
            second = self.call('post', f'{LIST}/shop/rotate', ROTATE, 200, json={}).get_json()['secret']
        with self.keys(f'{new_key},{old_key}'):
            self.assertEqual(rekey.check()['under_old_keys'], 2)
            self.assertEqual(rekey.rekey('tester')['rekeyed'], 2)
            self.assertEqual((self.under(new_key, 'secret'), self.under(new_key, 'previous_secret')),
                             (second, first))
        with self.keys(new_key):  # the old key gone: both still verify
            self.assertEqual((self.post(first).status_code, self.post(second).status_code), (204, 204))

    def test_what_no_key_decrypts_is_named_as_a_cdc_source(self):
        with self.keys(Fernet.generate_key().decode()):
            self.source()
        with self.keys(Fernet.generate_key().decode()):
            self.assertEqual(rekey.check()['undecryptable'], [{'type': 'CDC source', 'name': 'shop'}])

    def test_the_first_start_with_a_key_encrypts_a_secret_saved_without_one(self):
        secret = self.source().get_json()['secret']
        self.assertEqual(self.stored()[0]['secret'], secret)
        key = Fernet.generate_key().decode()
        with self.keys(key):
            create_app()
            self.assertEqual(self.under(key, 'secret'), secret)
            self.assertEqual(self.post(secret).status_code, 204)


class WorkspaceTests(CdcSourceTestCase):
    def setUp(self):
        super().setUp()
        self.call('post', '/api/v1/workspaces', '/api/v1/workspaces', 201, json={'name': 'acme'})
        with db.transaction() as tx:
            tx.execute("INSERT INTO connections (workspace, name, db, active, created_at, updated_at, details_json) "
                       "VALUES ('acme', 'lite', 'sqlite', 1, 'now', 'now', '{}')")

    def test_a_workspace_s_source_is_posted_to_under_its_prefix(self):
        created = self.source(prefix='/w/acme').get_json()
        self.assertEqual(created['ingest_path'], '/w/acme/ingest/cdc/shop')
        self.assertEqual(self.post(created['secret']).status_code, 404)  # not default's
        self.assertEqual(self.post(created['secret'], '/w/acme/ingest/cdc/shop').status_code, 204)
        self.assertEqual(self.signals(), [('acme', 'cdc:shop', ['main.film'])])
        self.assertEqual(self.post(created['secret'], '/w/nowhere/ingest/cdc/shop').status_code, 404)

    def test_the_same_name_in_two_workspaces_keeps_two_secrets(self):
        default = self.source().get_json()['secret']
        acme = self.source(prefix='/w/acme').get_json()['secret']
        self.assertEqual(self.post(acme).status_code, 404)
        self.assertEqual(self.post(default, '/w/acme/ingest/cdc/shop').status_code, 404)
        self.assertEqual(self.post(default).status_code, 204)
        self.assertEqual(self.post(acme, '/w/acme/ingest/cdc/shop').status_code, 204)
        self.assertEqual([w for w, _, _ in self.signals()], ['default', 'acme'])
        self.assertEqual([s['name'] for s in self.call('get', LIST, LIST, 200).get_json()['items']], ['shop'])

    def test_a_workspace_with_a_source_isnt_empty(self):
        self.source(prefix='/w/acme')
        with db.transaction() as tx:
            tx.execute("DELETE FROM connections WHERE workspace = 'acme'")
        res = self.client.delete('/api/v1/workspaces/acme', headers=ADMIN)
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.get_json()['contents'], {'CDC sources': 1})


class SettingTests(CdcSourceTestCase):
    """QUERYAPIGATE_CDC_SOURCES, the prototype's setting: still served, listed read-only, and never hidden."""
    setting = {'legacy': {'connection': 'lite', 'secret': SECRET, 'server_name': 'shop'},
               'acme_legacy': {'connection': 'lite', 'secret': SECRET, 'workspace': 'acme'}}

    def test_listed_read_only(self):
        listed = self.call('get', LIST, LIST, 200).get_json()['items']
        self.assertEqual([(s['name'], s['managed_by']) for s in listed], [('legacy', 'QUERYAPIGATE_CDC_SOURCES')])
        self.assertNotIn('secret', listed[0])
        self.assertEqual(self.call('get', f'{LIST}/legacy', ONE, 200).get_json()['ingest_path'], '/ingest/cdc/legacy')
        for method, url, path in (('patch', f'{LIST}/legacy', ONE), ('delete', f'{LIST}/legacy', ONE),
                                  ('post', f'{LIST}/legacy/rotate', ROTATE)):
            with self.subTest(method=method, url=url):
                res = self.call(method, url, path, 409, json={'server_name': None} if method == 'patch' else {})
                self.assertEqual(res.get_json()['code'], 'cdc_source_managed')

    def test_a_source_it_would_hide_cant_be_made(self):
        self.assertEqual(self.source('legacy', status=409).get_json()['code'], 'cdc_source_exists')
        self.assertEqual(self.source('acme_legacy', status=409).get_json()['code'], 'cdc_source_exists')

    def test_its_sources_answer_where_they_did(self):
        self.assertEqual(self.post(SECRET, '/ingest/cdc/legacy').status_code, 204)
        self.assertEqual(self.post(SECRET, '/ingest/cdc/acme_legacy').status_code, 204)
        self.assertEqual([w for w, _, _ in self.signals()], ['default', 'acme'])


if __name__ == '__main__':
    import unittest
    unittest.main()

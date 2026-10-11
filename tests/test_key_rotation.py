"""Reissuing an API key's secret (ADR 0009, BACKLOG #91): POST /api/v1/api-keys/{name}/rotate gives a new secret, and
the old one keeps working for a grace period - same name, same grants, the same history."""
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

from queryapigate import admins, apikeys, create_app, db, store
from tests.helpers import create_key, secret_of, write_connections

ADMIN = {'X-API-Key': 'admin-key'}


def later(minutes):
    return (datetime.now() + timedelta(minutes=minutes)).strftime('%Y-%m-%d %H:%M:%S')


class RotationTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = os.path.join(self.tmp.name, 'data.db')
        conn = sqlite3.connect(self.db_path)
        conn.execute('CREATE TABLE t (id INTEGER)')
        conn.execute('INSERT INTO t VALUES (1)')
        conn.commit()
        conn.close()
        patcher = mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': self.tmp.name, 'QUERYAPIGATE_API_KEY': 'admin-key'})
        patcher.start()
        self.addCleanup(patcher.stop)
        write_connections({'shop': {'db': 'sqlite', 'database': self.db_path, 'active': True}})
        self.client = create_app().test_client()
        apikeys._last_recorded_use.clear()
        self.old = secret_of(create_key(self.client, ADMIN, name='partner', connections=['shop'],
                                        allowed_ips=['127.0.0.1']))

    def rotate(self, status=200, headers=ADMIN, name='partner', **body):
        res = self.client.post(f'/api/v1/api-keys/{name}/rotate', json=body, headers=headers)
        self.assertEqual(res.status_code, status, res.get_data(as_text=True))
        return res

    def works(self, secret, ip='127.0.0.1'):
        res = self.client.post('/execute_sql', json={'connection_name': 'shop', 'sql': 'SELECT id FROM t'},
                               headers={'X-API-Key': secret}, environ_base={'REMOTE_ADDR': ip})
        return res.status_code == 200

    def key(self):
        return self.client.get('/api/v1/api-keys/partner', headers=ADMIN).get_json()

    def audit(self, action):
        return [e for e in store.read_audit_log() if e['action'] == action]


class RotateTests(RotationTestCase):
    def test_a_new_secret_and_the_old_one_still_works_for_the_grace_period(self):
        before = self.key()
        res = self.rotate()
        body = res.get_json()
        new = body['secret']
        self.assertNotEqual(new, self.old)
        self.assertEqual(res.headers['Cache-Control'], 'no-store')
        self.assertTrue(self.works(new))
        self.assertTrue(self.works(self.old))
        self.assertTrue(later(59) < body['previous_expires_at'] <= later(60))  # 60 minutes by default
        for field in ('name', 'connections', 'allowed_ips', 'created_at', 'expires_at', 'active'):
            self.assertEqual(body[field], before[field])
        self.assertEqual(self.audit('rotate_api_key')[0]['changes'],
                         {'grace_minutes': 60, 'previous_expires_at': body['previous_expires_at']})
        self.assertNotIn(new, repr(store.read_audit_log()))

    def test_the_old_secret_stops_the_second_its_grace_ends(self):
        until = self.rotate(grace_minutes=5).get_json()['previous_expires_at']
        stamp = '%Y-%m-%d %H:%M:%S'
        just_before = (datetime.strptime(until, stamp) - timedelta(seconds=1)).strftime(stamp)
        with mock.patch.object(store, 'now', return_value=just_before):
            self.assertTrue(self.works(self.old))
        with mock.patch.object(store, 'now', return_value=until):
            self.assertFalse(self.works(self.old))

    def test_grace_zero_stops_the_old_secret_at_once(self):
        body = self.rotate(grace_minutes=0).get_json()
        self.assertIsNone(body['previous_expires_at'])
        self.assertFalse(self.works(self.old))
        self.assertTrue(self.works(body['secret']))
        self.rotate(grace_minutes=0)  # nothing pending, so rotating again is fine

    def test_a_second_rotation_during_the_grace_is_refused_until_it_is_ended(self):
        first = self.rotate().get_json()
        res = self.rotate(409)
        self.assertEqual(res.get_json()['code'], 'key_rotation_pending')
        self.assertEqual(res.get_json()['previous_expires_at'], first['previous_expires_at'])
        self.assertTrue(self.works(first['secret']))  # the refused rotation changed nothing
        ended = self.client.delete('/api/v1/api-keys/partner/previous', headers=ADMIN)
        self.assertEqual(ended.status_code, 200)
        self.assertIsNone(ended.get_json()['previous_expires_at'])
        self.assertFalse(self.works(self.old))
        self.assertEqual(self.audit('end_previous_secret')[0]['changes'],
                         {'was_valid_until': first['previous_expires_at']})
        self.rotate()

    def test_after_the_grace_a_new_rotation_is_allowed(self):
        until = self.rotate(grace_minutes=1).get_json()['previous_expires_at']
        with mock.patch.object(store, 'now', return_value=until):
            body = self.rotate(grace_minutes=0).get_json()
        self.assertIsNone(body['previous_expires_at'])

    def test_bad_requests(self):
        for grace in (-1, 10081, 'an hour', True, 1.5):
            self.assertEqual(self.rotate(400, grace_minutes=grace).get_json()['code'], 'invalid_body')
        self.assertEqual(self.rotate(400, secret='mine').get_json()['code'], 'unknown_field')
        self.assertEqual(self.rotate(404, name='nobody').get_json()['code'], 'key_not_found')
        res = self.client.delete('/api/v1/api-keys/partner/previous', headers=ADMIN)
        self.assertEqual((res.status_code, res.get_json()['code']), (404, 'previous_secret_not_found'))
        self.rotate(grace_minutes=10080)  # a week is the most
        res = self.client.post('/api/v1/api-keys/partner/rotate', headers=ADMIN)  # no body at all: the default
        self.assertEqual(res.get_json()['code'], 'key_rotation_pending')


class BothSecretsTests(RotationTestCase):
    def test_allowed_ips_expiry_and_active_apply_to_the_old_secret_too(self):
        new = self.rotate().get_json()['secret']
        self.assertFalse(self.works(self.old, ip='10.9.9.9'))
        self.assertFalse(self.works(new, ip='10.9.9.9'))
        self.client.patch('/api/v1/api-keys/partner', json={'active': False}, headers=ADMIN)
        self.assertFalse(self.works(self.old))
        self.assertFalse(self.works(new))
        self.client.patch('/api/v1/api-keys/partner', json={'active': True, 'expires_at': '2020-01-01'},
                          headers=ADMIN)
        self.assertFalse(self.works(self.old))
        self.assertFalse(self.works(new))

    def test_one_rate_limit_for_both_secrets(self):
        self.client.patch('/api/v1/api-keys/partner', json={'rate_limit': '1/minute'}, headers=ADMIN)
        new = self.rotate().get_json()['secret']
        self.assertTrue(self.works(new))
        self.assertFalse(self.works(self.old))  # the same bucket - the old secret is no second allowance

    def test_when_each_secret_was_last_used(self):
        new = self.rotate().get_json()['secret']
        self.assertIsNone(self.key()['previous_last_used_at'])
        self.works(self.old)
        self.assertIsNotNone(self.key()['previous_last_used_at'])
        self.assertIsNone(self.key()['last_used_at'])
        self.works(new)
        self.assertIsNotNone(self.key()['last_used_at'])
        self.client.delete('/api/v1/api-keys/partner/previous', headers=ADMIN)
        self.assertIsNone(self.key()['previous_last_used_at'])  # no old secret, nothing to report

    def test_the_old_secret_is_stored_only_as_a_hash(self):
        new = self.rotate().get_json()['secret']
        row = db.connection().execute('SELECT * FROM api_keys WHERE name = ?', ('partner',)).fetchone()
        dumped = repr(tuple(row))
        self.assertNotIn(self.old, dumped)
        self.assertNotIn(new, dumped)
        self.assertEqual(row['previous_hash'], apikeys._hash(self.old))

    def test_a_key_never_rotated_answers_as_before(self):
        key = self.key()
        self.assertEqual((key['previous_expires_at'], key['previous_last_used_at']), (None, None))


class WhoMayRotateTests(RotationTestCase):
    def test_access_write_is_needed(self):
        admins.create_admin('dev', 'developer')
        admins.create_admin('ops', 'admin')
        dev = {'X-API-Key': admins.issue_token('dev')[1]}
        ops = {'X-API-Key': admins.issue_token('ops')[1]}
        self.assertEqual(self.rotate(403, headers=dev).get_json()['code'], 'role_forbidden')
        res = self.client.delete('/api/v1/api-keys/partner/previous', headers=dev)
        self.assertEqual(res.get_json()['code'], 'role_forbidden')
        self.rotate(headers=ops)
        self.assertEqual(self.audit('rotate_api_key')[0]['actor'], 'ops')

    def test_a_scoped_key_cannot_rotate_itself(self):
        self.assertEqual(self.rotate(403, headers={'X-API-Key': self.old}).get_json()['code'], 'admin_only')



class UpgradeTests(RotationTestCase):
    def test_a_schema_11_store_gains_the_columns_and_its_keys_rotate(self):
        with db.transaction() as conn:  # the store as 0.21 left it
            conn.execute('CREATE TABLE api_keys_v11 (name TEXT PRIMARY KEY, hash TEXT NOT NULL UNIQUE, '
                         'active INTEGER NOT NULL DEFAULT 1, expires_at TEXT, created_at TEXT NOT NULL, '
                         'details_json TEXT NOT NULL)')
            conn.execute('INSERT INTO api_keys_v11 SELECT name, hash, active, expires_at, created_at, details_json '
                         'FROM api_keys')
            conn.execute('DROP TABLE api_keys')
            conn.execute('ALTER TABLE api_keys_v11 RENAME TO api_keys')
            conn.execute('UPDATE schema_version SET version = 11')
        db.init_schema()
        self.assertEqual(db.connection().execute('SELECT version FROM schema_version').fetchone()[0],
                         db.SCHEMA_VERSION)
        self.assertTrue(self.works(self.old))
        self.assertIsNone(self.key()['previous_expires_at'])
        new = self.rotate().get_json()['secret']
        self.assertTrue(self.works(self.old))
        self.assertTrue(self.works(new))

if __name__ == '__main__':
    unittest.main()

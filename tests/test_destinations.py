"""Destinations (ADR 0004, destinations.py): where exports may write - validated, the secret kept like a connection
password, writes locked to the prefix by DuckDB, and only owners and admins may define one. The S3 part runs
against a real S3-compatible server when QUERYAPIGATE_TEST_S3 is set (as test_duckdb_files.py does)."""
import os
import tempfile
import unittest
from unittest import mock

import duckdb

from queryapigate import admins, create_app, db, destinations, store
from tests.test_api_v1 import V1TestCase

ADMIN = {'X-API-Key': 'admin-key'}
LIST = '/api/v1/destinations'
ONE = '/api/v1/destinations/{name}'


class DestinationApiTests(V1TestCase):
    def setUp(self):
        super().setUp()
        self.out = os.path.join(self.tmp.name, 'exports') + '/'

    def create(self, status=201, **fields):
        body = {'name': 'local', 'url': self.out, **fields}
        return self.call('post', LIST, LIST, status, json=body)

    def test_create_read_change_remove(self):
        created = self.create(user='AKIA1', password='s3cret', region='eu-west-1').get_json()
        self.assertEqual((created['url'], created['password']), (self.out, '********'))
        self.assertEqual(self.call('get', LIST, LIST, 200).get_json()['items'][0]['name'], 'local')
        self.create(status=409)
        etag = self.call('get', f'{LIST}/local', ONE, 200).headers['ETag']
        changed = self.call('patch', f'{LIST}/local', ONE, 200, json={'region': None, 'password': '********'},
                            headers={**ADMIN, 'If-Match': etag}).get_json()
        self.assertNotIn('region', changed)
        self.assertEqual(destinations.get('local')['password'], 's3cret')  # the mask echoed back kept it
        self.call('patch', f'{LIST}/local', ONE, 412, json={'user': 'x'}, headers={**ADMIN, 'If-Match': etag})
        self.call('delete', f'{LIST}/local', ONE, 204)
        self.call('get', f'{LIST}/local', ONE, 404)
        audit = [(e['action'], e['changes']) for e in store.read_audit_log()]
        self.assertEqual([a for a, _ in audit], ['create_destination', 'update_destination', 'delete_destination'])
        self.assertNotIn('s3cret', repr(audit))

    def test_a_secret_is_a_var_reference_or_encrypted(self):
        self.create(password='${DEST_SECRET}')
        self.assertEqual(self.call('get', f'{LIST}/local', ONE, 200).get_json()['password'], '${DEST_SECRET}')
        from cryptography.fernet import Fernet
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_SECRET_KEY': Fernet.generate_key().decode()}):
            self.call('patch', f'{LIST}/local', ONE, 200, json={'password': 'plain'})
            stored = db.connection().execute("SELECT details_json FROM destinations").fetchone()[0]
            self.assertNotIn('plain', stored)
            self.assertEqual(destinations._usable(destinations.get('local'))['password'], 'plain')

    def test_bad_fields_are_refused(self):
        for fields, code in (({'url': 's3://bucket/no-slash'}, 'invalid_body'),
                             ({'url': 'https://example.com/drop/'}, 'invalid_body'),
                             ({'url': 'relative/folder/'}, 'invalid_body'),
                             ({'url': 's3://bucket/../other/'}, 'invalid_body'),
                             ({'url': 's3://bucket/*/'}, 'invalid_body'),
                             ({'url': 's3:///'}, 'invalid_body'),
                             ({'url': 'gs://bucket/x/', 'storage': 's3'}, 'invalid_body'),
                             ({'url_style': 'sideways'}, 'invalid_body'),
                             ({'shoe_size': 9}, 'unknown_field'),
                             ({'name': 'has space'}, 'invalid_name')):
            with self.subTest(fields=fields):
                res = self.create(status=400, **fields)
                self.assertEqual(res.get_json()['code'], code)

    def test_the_scheme_decides_the_storage(self):
        self.create(name='gcs', url='gs://bucket/out/')
        self.assertEqual(destinations.get('gcs')['storage'], 'gcs')
        self.call('patch', f'{LIST}/gcs', ONE, 200, json={'url': 'r2://bucket/out/'})
        self.assertEqual(destinations.get('gcs')['storage'], 'r2')

    def test_the_test_button_writes_a_probe(self):
        self.create()
        res = self.call('post', f'{LIST}/local/test', '/api/v1/destinations/{name}/test', 200)
        self.assertTrue(os.path.isfile(os.path.join(self.out, destinations.PROBE_FILE)))
        self.assertEqual(res.get_json()['object'], os.path.join(self.out, destinations.PROBE_FILE))
        unsaved = os.path.join(self.tmp.name, 'unsaved') + '/'
        self.call('post', f'{LIST}/test', '/api/v1/destinations/test', 200, json={'url': unsaved})
        self.assertTrue(os.path.isfile(os.path.join(unsaved, destinations.PROBE_FILE)))

    def test_a_destination_in_use_is_kept(self):
        self.create()
        with self.assertRaises(Exception) as caught:
            destinations.delete('local', used_by=['daily'])
        self.assertEqual(caught.exception.code, 'destination_in_use')

    def test_only_owners_and_admins_define_destinations(self):
        self.create()
        for role, read, write in (('developer', 200, 403), ('auditor', 200, 403), ('admin', 200, 201)):
            with self.subTest(role=role):
                admins.create_admin(f'{role}-1', role)
                token = {'X-API-Key': admins.issue_token(f'{role}-1')[1]}
                self.assertEqual(self.client.get(LIST, headers=token).status_code, read)
                res = self.client.post(LIST, headers=token, json={'name': f'by-{role}', 'url': self.out})
                self.assertEqual(res.status_code, write, res.get_data(as_text=True))


class WriterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        create_app()
        self.out = os.path.join(self.tmp.name, 'out') + '/'
        self.elsewhere = os.path.join(self.tmp.name, 'elsewhere')
        os.makedirs(self.elsewhere)

    def test_writes_go_under_the_prefix_and_nowhere_else(self):
        conn, prefix = destinations.open_writer({'url': 'file://' + self.out})
        try:
            inside = destinations.target(prefix, 'daily/2026-10-06/orders.parquet')
            conn.execute(f"COPY (SELECT 1 AS n) TO '{inside}' (FORMAT parquet)")
            self.assertEqual(duckdb.sql(f"SELECT n FROM '{inside}'").fetchall(), [(1,)])
            for relative in ('../elsewhere/x.csv', '/etc/x.csv', 'a/../../x.csv', ''):
                with self.subTest(relative=relative), self.assertRaises(Exception) as caught:
                    destinations.target(prefix, relative)
                self.assertEqual(caught.exception.code, 'invalid_body')
            for path in (os.path.join(self.elsewhere, 'x.csv'), prefix + '../elsewhere/y.csv'):
                with self.subTest(path=path), self.assertRaises(duckdb.PermissionException):
                    conn.execute(f"COPY (SELECT 1 AS n) TO '{path}' (FORMAT csv)")
            with self.assertRaises(duckdb.Error):  # and the lock can't be lifted from SQL
                conn.execute("SET allowed_directories = ['/']")
        finally:
            conn.close()
        self.assertEqual(os.listdir(self.elsewhere), [])


@unittest.skipUnless(os.environ.get('QUERYAPIGATE_TEST_S3'), 'set QUERYAPIGATE_TEST_S3=endpoint,key,secret,bucket')
class S3DestinationTests(V1TestCase):
    def test_a_probe_written_to_a_bucket_prefix_and_no_further(self):
        endpoint, key, secret, bucket = os.environ['QUERYAPIGATE_TEST_S3'].split(',')
        with mock.patch.dict(os.environ, {'TEST_S3_SECRET': secret}):
            self.call('post', LIST, LIST, 201, json={
                'name': 'bucket', 'url': f's3://{bucket}/queryapigate-test/exports/', 'user': key,
                'password': '${TEST_S3_SECRET}', 'endpoint': endpoint, 'url_style': 'path', 'use_ssl': False,
                'region': 'us-east-1'})
            res = self.call('post', f'{LIST}/bucket/test', '/api/v1/destinations/{name}/test', 200)
            self.assertEqual(res.get_json()['object'],
                             f's3://{bucket}/queryapigate-test/exports/{destinations.PROBE_FILE}')
            conn, prefix = destinations.open_writer(destinations.get('bucket'))
            try:
                written = conn.execute(f"SELECT probe FROM read_csv('{prefix}{destinations.PROBE_FILE}')").fetchall()
                self.assertEqual(written, [('queryapigate',)])
                with self.assertRaises(duckdb.PermissionException):
                    conn.execute(f"COPY (SELECT 1 AS n) TO 's3://{bucket}/queryapigate-test/other.csv' (FORMAT csv)")
            finally:
                conn.close()


if __name__ == '__main__':
    unittest.main()

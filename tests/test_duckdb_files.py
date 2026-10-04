"""DuckDB file access (BACKLOG #75, duckfiles.py): a connection reads exactly its allowed_paths - local folders and
files, and http(s) files - and nothing else, however the SQL asks; views give files names that allowed_tables can
grant. The S3 part is checked against a real S3-compatible server only when QUERYAPIGATE_TEST_S3 is set."""
import functools
import http.server
import logging
import os
import tempfile
import threading
import unittest
from unittest import mock

import duckdb

from queryapigate import create_app
from tests.helpers import create_key, put_connections, secret_of

ADMIN = {'X-API-Key': 'admin-key'}


@functools.lru_cache(maxsize=1)
def httpfs_available():
    try:
        conn = duckdb.connect()
        try:
            conn.execute('LOAD httpfs')
        except Exception:
            conn.execute('INSTALL httpfs')
            conn.execute('LOAD httpfs')
        return True
    except Exception:
        return False


class FilesTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.public = os.path.join(self.tmp.name, 'public')
        self.private = os.path.join(self.tmp.name, 'private')
        os.makedirs(self.public)
        os.makedirs(self.private)
        duckdb.sql(f"COPY (SELECT range AS id, range * 2 AS price FROM range(10)) "
                   f"TO '{self.public}/products.parquet'")
        duckdb.sql(f"COPY (SELECT 'eu' AS region) TO '{self.public}/regions.csv'")
        duckdb.sql(f"COPY (SELECT 'top secret' AS s) TO '{self.private}/secret.csv'")
        patcher = mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': self.tmp.name, 'QUERYAPIGATE_API_KEY': 'admin-key'})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = create_app().test_client()

    def connect(self, name='files', **fields):
        res = put_connections(self.client, {name: {'db': 'duckdb', 'database': ':memory:', 'active': True, **fields}},
                              headers=ADMIN)
        self.assertIn(res.status_code, (200, 201), res.get_data(as_text=True))

    def run_sql(self, sql, connection='files', headers=ADMIN):
        return self.client.post('/execute_sql', json={'sql': sql, 'connection_name': connection}, headers=headers)

    def assert_refused(self, res, code='path_not_allowed'):
        self.assertEqual(res.status_code, 403, res.get_data(as_text=True))
        self.assertEqual(res.get_json()['code'], code)


class LocalFilesTests(FilesTestCase):
    def test_without_allowed_paths_no_file_can_be_read(self):
        self.connect()
        self.assert_refused(self.run_sql(f"SELECT * FROM read_parquet('{self.public}/products.parquet')"))
        self.assert_refused(self.run_sql("SELECT * FROM read_csv('/etc/passwd')"))
        self.assertEqual(self.run_sql('SELECT 42 AS answer').get_json(), [{'answer': 42}])

    def test_an_allowed_folder_and_nothing_outside_it(self):
        self.connect(allowed_paths=[self.public + '/'])
        res = self.run_sql(f"SELECT COUNT(*) AS n FROM read_parquet('{self.public}/*.parquet')")
        self.assertEqual(res.get_json(), [{'n': 10}])
        self.assertEqual(self.run_sql(f"SELECT COUNT(*) AS n FROM '{self.public}/products.parquet'").get_json(),
                         [{'n': 10}])
        self.assert_refused(self.run_sql(f"SELECT * FROM read_csv('{self.private}/secret.csv')"))
        self.assert_refused(self.run_sql(f"SELECT * FROM read_csv('{self.public}/../private/secret.csv')"))
        self.assert_refused(self.run_sql(f"SELECT * FROM glob('{self.tmp.name}/*')"))

    def test_an_allowed_file_is_exactly_that_file(self):
        self.connect(allowed_paths=[f'{self.public}/regions.csv'])
        self.assertEqual(self.run_sql(f"SELECT region FROM read_csv('{self.public}/regions.csv')").get_json(),
                         [{'region': 'eu'}])
        self.assert_refused(self.run_sql(f"SELECT * FROM read_parquet('{self.public}/products.parquet')"))

    def test_the_refusal_names_the_path(self):
        self.connect()
        res = self.run_sql(f"SELECT * FROM read_csv('{self.private}/secret.csv')")
        self.assertIn(f'{self.private}/secret.csv', res.get_json()['error'])
        self.assertNotIn('top secret', res.get_data(as_text=True))

    def test_a_query_cannot_unlock_the_connection(self):
        self.connect(allowed_paths=[self.public + '/'])
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_ALLOW_WRITES': '1'}):
            for sql in ('SET enable_external_access = true', "SET allowed_directories = ['/']",
                        f"ATTACH '{self.private}/other.duckdb'", 'INSTALL postgres', 'LOAD postgres_scanner',
                        f"COPY (SELECT 1) TO '{self.private}/out.csv'"):
                with self.subTest(sql=sql):
                    self.assertNotEqual(self.run_sql(sql).status_code, 200)
        self.assertFalse(os.path.exists(f'{self.private}/out.csv'))
        self.assert_refused(self.run_sql(f"SELECT * FROM read_csv('{self.private}/secret.csv')"))

    def test_a_database_file_still_works_and_stays_writable_to_itself(self):
        path = os.path.join(self.tmp.name, 'own.duckdb')
        duckdb.connect(path).close()
        self.connect(name='own', database=path, allowed_paths=[self.public + '/'])
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_ALLOW_WRITES': '1'}):
            self.assertEqual(self.run_sql(f"CREATE TABLE p AS SELECT * FROM '{self.public}/products.parquet'",
                                          'own').status_code, 200)
        self.assertEqual(self.run_sql('SELECT COUNT(*) AS n FROM p', 'own').get_json(), [{'n': 10}])


class ViewTests(FilesTestCase):
    def setUp(self):
        super().setUp()
        self.connect(allowed_paths=[self.public + '/'],
                     views={'products': f"SELECT * FROM read_parquet('{self.public}/products.parquet')"})

    def test_a_view_is_queryable_and_in_the_schema(self):
        self.assertEqual(self.run_sql('SELECT COUNT(*) AS n FROM products').get_json(), [{'n': 10}])
        tables = self.client.get('/connections/files/schema', headers=ADMIN).get_json()['tables']
        self.assertEqual([(t['name'], t['type']) for t in tables], [('products', 'view')])

    def test_a_table_restricted_key_gets_the_view_but_not_the_files_behind_it(self):
        key = {'X-API-Key': secret_of(create_key(self.client, headers=ADMIN, name='shop-app', connections=['files'],
                                                 allowed_tables=['products']))}
        self.assertEqual(self.run_sql('SELECT COUNT(*) AS n FROM products', headers=key).get_json(), [{'n': 10}])
        self.assert_refused(self.run_sql(f"SELECT * FROM read_parquet('{self.public}/products.parquet')",
                                         headers=key), code='table_not_allowed')
        self.assert_refused(self.run_sql(f"SELECT * FROM '{self.public}/products.parquet'", headers=key),
                            code='table_not_allowed')


class ValidationTests(FilesTestCase):
    def test_bad_fields_are_refused_when_saved(self):
        for fields in ({'allowed_paths': 'not-a-list'}, {'allowed_paths': ['relative/folder/']},
                       {'allowed_paths': ['https://data.example.com/public/']},
                       {'allowed_paths': [f'{self.public}/../private/']}, {'allowed_paths': [f'{self.public}/*.csv']},
                       {'views': {'bad name': 'SELECT 1'}}, {'views': ['SELECT 1']}, {'storage': 'ftp'},
                       {'use_ssl': 'no'}):
            with self.subTest(fields=fields):
                res = self.client.post('/api/v1/connections', headers=ADMIN,
                                       json={'name': 'x', 'db': 'duckdb', 'database': ':memory:', **fields})
                self.assertEqual(res.status_code, 400, res.get_data(as_text=True))
                self.assertEqual(res.get_json()['code'], 'invalid_body')


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


@unittest.skipUnless(httpfs_available(), "DuckDB's httpfs extension isn't installed and can't be downloaded here")
class HttpFilesTests(FilesTestCase):
    def setUp(self):
        super().setUp()
        handler = functools.partial(_Quiet, directory=self.tmp.name)
        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.base = f'http://127.0.0.1:{self.server.server_address[1]}'

    def test_an_allowed_web_file_and_no_other(self):
        self.connect(allowed_paths=[f'{self.base}/public/products.parquet'])
        self.assertEqual(self.run_sql(f"SELECT COUNT(*) AS n FROM '{self.base}/public/products.parquet'").get_json(),
                         [{'n': 10}])
        self.assert_refused(self.run_sql(f"SELECT * FROM read_csv('{self.base}/private/secret.csv')"))
        # the web server would resolve this one - the reason an http(s) entry can't be a prefix
        self.assert_refused(self.run_sql(f"SELECT * FROM read_csv('{self.base}/public/../private/secret.csv')"))

    def test_a_remote_source_is_flagged_experimental_at_startup(self):
        self.connect(allowed_paths=[f'{self.base}/public/products.parquet'])
        with self.assertLogs('queryapigate', level=logging.WARNING) as logs:
            create_app()
        self.assertTrue(any('Remote files through DuckDB are experimental (connection files)' in line
                            for line in logs.output), logs.output)


@unittest.skipUnless(os.environ.get('QUERYAPIGATE_TEST_S3'), 'set QUERYAPIGATE_TEST_S3=endpoint,key,secret,bucket')
class S3FilesTests(FilesTestCase):
    """Against a real S3-compatible server: QUERYAPIGATE_TEST_S3=localhost:9100,KEY,SECRET,bucket - the bucket must
    exist and be writable with that key; this test writes one object under queryapigate-test/."""

    def test_an_allowed_prefix_with_credentials(self):
        endpoint, key, secret, bucket = os.environ['QUERYAPIGATE_TEST_S3'].split(',')
        writer = duckdb.connect()
        writer.execute('LOAD httpfs')
        writer.execute(f"CREATE SECRET (TYPE s3, KEY_ID '{key}', SECRET '{secret}', ENDPOINT '{endpoint}', "
                       "URL_STYLE 'path', USE_SSL false, REGION 'us-east-1')")
        writer.execute(f"COPY (SELECT range AS id FROM range(7)) TO 's3://{bucket}/queryapigate-test/a/x.parquet'")
        writer.execute(f"COPY (SELECT 1 AS id) TO 's3://{bucket}/queryapigate-test/b/y.parquet'")
        with mock.patch.dict(os.environ, {'TEST_S3_SECRET': secret}):
            self.connect(allowed_paths=[f's3://{bucket}/queryapigate-test/a/'], user=key,
                         password='${TEST_S3_SECRET}', endpoint=endpoint, url_style='path', use_ssl=False,
                         region='us-east-1')
            res = self.run_sql(f"SELECT COUNT(*) AS n FROM read_parquet('s3://{bucket}/queryapigate-test/a/*.parquet')")
            self.assertEqual(res.get_json(), [{'n': 7}])
            self.assert_refused(self.run_sql(f"SELECT * FROM 's3://{bucket}/queryapigate-test/b/y.parquet'"))
        detail = self.client.get('/api/v1/connections/files', headers=ADMIN).get_json()
        self.assertEqual(detail['password'], '${TEST_S3_SECRET}')


if __name__ == '__main__':
    unittest.main()

"""Writing a result to a destination (ADR 0004, exporting.py): object paths from templates, Parquet/CSV/NDJSON
written by DuckDB through a connection locked to the destination, rows in their order, an incremental column's
largest value, and `queryapigate export --to`. The S3 part runs against a real S3-compatible server when
QUERYAPIGATE_TEST_S3 is set."""
import contextlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import date, datetime
from decimal import Decimal
from unittest import mock

import duckdb

from queryapigate import cli, create_app, destinations, exporting, history
from tests.helpers import save_query, write_connections

ADMIN = {'X-API-Key': 'admin-key'}
COLUMNS = ['id', 'label', 'amount', 'day']


def rows(n=5):
    return [(i, f'row {i}', Decimal(f'{i}.50'), date(2026, 10, i % 28 + 1)) for i in range(n, 0, -1)]


class PathTests(unittest.TestCase):
    def test_placeholders(self):
        now = datetime(2026, 10, 8, 9, 30, 5)
        self.assertEqual(exporting.render_path('{name}/{date}/{time}_{run}_{region}.parquet', 'orders',
                                               {'region': 'EU'}, 'abc123', now),
                         'orders/2026-10-08/093005_abc123_EU.parquet')

    def test_what_a_path_may_not_do(self):
        for template, params in (('../x.parquet', {}), ('/abs.parquet', {}), ('a/../../x', {}), ('{nope}.csv', {}),
                                 ('{region}.csv', {'region': '../../etc'}), ('{region}.csv', {'region': 'a/b'}),
                                 ('{date.parquet', {}), ('a\\b.csv', {}), ('', {})):
            with self.subTest(template=template, params=params), self.assertRaises(Exception) as caught:
                exporting.render_path(template, 'q', params)
            self.assertEqual(caught.exception.code, 'invalid_body')


class DeliverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        create_app()
        self.out = os.path.join(self.tmp.name, 'out') + '/'
        self.destination = {'url': 'file://' + self.out}

    def test_each_format_keeps_types_and_order(self):
        for fmt in exporting.FORMATS:
            with self.subTest(fmt=fmt):
                result = exporting.deliver(self.destination, f'daily/rows.{fmt}', COLUMNS, rows(), fmt, track='id')
                self.assertEqual((result['rows'], result['largest']), (5, 5))
                self.assertGreater(result['bytes'], 0)
                path = os.path.join(self.out, 'daily', f'rows.{fmt}')
                self.assertEqual(result['object'], path)
                if fmt == 'parquet':
                    types = [r[1] for r in duckdb.sql(f"DESCRIBE SELECT * FROM '{path}'").fetchall()]
                    self.assertEqual(types, ['BIGINT', 'VARCHAR', 'DECIMAL(38,2)', 'DATE'])
                    got = duckdb.sql(f"SELECT * FROM '{path}'").fetchall()
                    self.assertEqual(got, rows())
                elif fmt == 'csv':
                    with open(path) as f:
                        lines = f.read().splitlines()
                    self.assertEqual(lines[:2], ['id,label,amount,day', '5,row 5,5.50,2026-10-06'])
                else:
                    with open(path) as f:
                        first = json.loads(f.readline())
                    self.assertEqual(first, {'id': 5, 'label': 'row 5', 'amount': 5.5, 'day': '2026-10-06'})

    def test_order_survives_a_large_result(self):
        many = [(i, 'x', Decimal('1.00'), date(2026, 1, 1)) for i in range(300_000, 0, -1)]
        exporting.deliver(self.destination, 'big.parquet', COLUMNS, iter(many))
        ids = [r[0] for r in duckdb.sql(f"SELECT id FROM '{self.out}big.parquet'").fetchall()]
        self.assertEqual(ids, list(range(300_000, 0, -1)))

    def test_empty_results(self):
        self.assertEqual(exporting.deliver(self.destination, 'none.parquet', COLUMNS, [], skip_empty=True)['object'],
                         None)
        self.assertFalse(os.path.exists(os.path.join(self.out, 'none.parquet')))
        result = exporting.deliver(self.destination, 'empty.parquet', COLUMNS, [])
        self.assertEqual(duckdb.sql(f"SELECT count(*) FROM '{result['object']}'").fetchall(), [(0,)])

    def test_a_bad_incremental_column(self):
        with self.assertRaises(Exception) as caught:
            exporting.deliver(self.destination, 'x.parquet', COLUMNS, rows(), track='updated_at')
        self.assertEqual(caught.exception.code, 'invalid_watermark')
        with self.assertRaises(Exception) as caught:
            exporting.deliver(self.destination, 'x.parquet', ['v'], [(1,), ('a',)], track='v')
        self.assertEqual(caught.exception.code, 'invalid_watermark')

    def test_the_staging_file_is_always_removed(self):
        before = set(os.listdir(tempfile.gettempdir()))
        exporting.deliver(self.destination, 'ok.csv', COLUMNS, rows(), 'csv')
        with self.assertRaises(exporting.ApiError):
            exporting.deliver({'url': 's3://bucket-that-does-not-exist-qag/x/', 'endpoint': '127.0.0.1:1',
                               'use_ssl': False, 'url_style': 'path', 'user': 'k', 'password': 's'},
                              'x.csv', COLUMNS, rows(), 'csv')
        leftovers = {f for f in set(os.listdir(tempfile.gettempdir())) - before if f.startswith('qag-stage-')}
        self.assertEqual(leftovers, set())

    def test_the_writer_runs_on_one_thread(self):
        # The setting that keeps a large write at steady memory (and rows in order) - see parquet.WRITER_SETTINGS.
        conn, _ = destinations.open_writer(self.destination)
        try:
            self.assertEqual(conn.execute("SELECT current_setting('threads')").fetchone()[0], 1)
        finally:
            conn.close()


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': self.tmp.name, 'QUERYAPIGATE_API_KEY': 'admin-key',
                                               'QUERYAPIGATE_HISTORY_FLUSH_INTERVAL': '0'})
        patcher.start()
        self.addCleanup(patcher.stop)
        data = os.path.join(self.tmp.name, 'data.db')
        conn = sqlite3.connect(data)
        conn.execute('CREATE TABLE t (id INTEGER, region TEXT)')
        conn.executemany('INSERT INTO t VALUES (?, ?)', [(i, 'EU' if i % 2 else 'US') for i in range(1, 11)])
        conn.commit()
        conn.close()
        write_connections({'lite': {'db': 'sqlite', 'database': data, 'active': True}})
        client = create_app().test_client()
        save_query(client, headers=ADMIN, body={
            'filename': 'by_region', 'description': 'd', 'connection_name': 'lite',
            'sql_query': 'SELECT id, region FROM t WHERE region = :region ORDER BY id',
            'query_parameters': {'region': {'type': 'string'}}})
        self.out = os.path.join(self.tmp.name, 'drop') + '/'
        destinations.create('drop', {'url': self.out})

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_export_to_a_destination(self):
        code, out, err = self.run_cli('export', 'by_region', '--param', 'region=EU', '--to', 'drop', '--format',
                                      'parquet', '--out', '{name}/{region}/{date}.parquet')
        self.assertEqual(code, 0, err)
        path = os.path.join(self.out, 'by_region', 'EU', f'{date.today().isoformat()}.parquet')
        self.assertIn(path, out)
        self.assertEqual(duckdb.sql(f"SELECT id FROM '{path}'").fetchall(), [(1,), (3,), (5,), (7,), (9,)])
        history.flush()
        entry = [e for e in history.search(query='by_region')[0] if e.get('transport') == 'export'][0]
        self.assertEqual((entry['status'], entry['rows'], entry['destination'], entry['object']),
                         ('success', 5, 'drop', path))

    def test_refusals(self):
        for argv, message in ((('--to', 'drop', '--format', 'tsv', '--out', 'x.tsv'), '--format must be one of'),
                              (('--to', 'nowhere', '--format', 'csv', '--out', 'x.csv'), "'nowhere' not found"),
                              (('--to', 'drop', '--format', 'csv', '--out', '../x.csv'), "without '..'")):
            with self.subTest(argv=argv):
                code, _, err = self.run_cli('export', 'by_region', '--param', 'region=EU', *argv)
                self.assertEqual(code, 1)
                self.assertIn(message, err)


@unittest.skipUnless(os.environ.get('QUERYAPIGATE_TEST_S3'), 'set QUERYAPIGATE_TEST_S3=endpoint,key,secret,bucket')
class S3DeliverTests(unittest.TestCase):
    def test_each_format_to_a_bucket(self):
        endpoint, key, secret, bucket = os.environ['QUERYAPIGATE_TEST_S3'].split(',')
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': home}):
            create_app()
            destination = {'url': f's3://{bucket}/queryapigate-test/deliver/', 'storage': 's3', 'user': key,
                           'password': secret, 'endpoint': endpoint, 'url_style': 'path', 'use_ssl': False,
                           'region': 'us-east-1'}
            for fmt in exporting.FORMATS:
                with self.subTest(fmt=fmt):
                    result = exporting.deliver(destination, f'rows.{fmt}', COLUMNS, rows(), fmt)
                    self.assertEqual(result['object'], f"s3://{bucket}/queryapigate-test/deliver/rows.{fmt}")
                    reader = {'parquet': 'read_parquet', 'csv': 'read_csv', 'ndjson': 'read_json'}[fmt]
                    conn, _ = destinations.open_writer(destination)
                    try:
                        got = conn.execute(f"SELECT count(*) FROM {reader}('{result['object']}')").fetchone()[0]
                    finally:
                        conn.close()
                    self.assertEqual(got, 5)


if __name__ == '__main__':
    unittest.main()

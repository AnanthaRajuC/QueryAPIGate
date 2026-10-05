"""Parquet output (BACKLOG #81, parquet.py): `?format=parquet` on any query - paged or streamed - and
`queryapigate export --format parquet`, written by DuckDB with column types worked out from the values."""
import os
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, time, timezone
from decimal import Decimal
from unittest import mock

import duckdb

from queryapigate import cli, create_app, parquet
from tests.helpers import save_query, write_connections

ADMIN = {'X-API-Key': 'admin-key'}


def read(data_or_path):
    """(DuckDB column types, rows) of a Parquet file, given its bytes or its path."""
    path = data_or_path
    if isinstance(data_or_path, bytes):
        fd, path = tempfile.mkstemp(suffix='.parquet')
        with os.fdopen(fd, 'wb') as f:
            f.write(data_or_path)
    try:
        conn = duckdb.connect()
        types = [(r[0], r[1]) for r in conn.execute(f"DESCRIBE SELECT * FROM '{path}'").fetchall()]
        return types, conn.execute(f"SELECT * FROM '{path}'").fetchall()
    finally:
        if isinstance(data_or_path, bytes):
            os.unlink(path)


class TypesTests(unittest.TestCase):
    def test_each_kind_of_value_keeps_its_type(self):
        rows = [(1, 'a', 1.5, Decimal('10.25'), date(2026, 10, 1), datetime(2026, 10, 1, 9, 30), True, time(8, 15),
                 datetime(2026, 10, 1, 9, 30, tzinfo=timezone.utc), 2 ** 70),
                (2, 'b', 2.0, Decimal('3'), date(2026, 10, 2), datetime(2026, 10, 2, 9, 30), False, time(9, 0),
                 datetime(2026, 10, 2, 9, 30, tzinfo=timezone.utc), 5)]
        columns = ['id', 'name', 'x', 'amount', 'day', 'at', 'flag', 'clock', 'at_utc', 'big']
        types, out = read(parquet.to_bytes(columns, rows))
        self.assertEqual(types, [('id', 'BIGINT'), ('name', 'VARCHAR'), ('x', 'DOUBLE'), ('amount', 'DECIMAL(38,2)'),
                                 ('day', 'DATE'), ('at', 'TIMESTAMP'), ('flag', 'BOOLEAN'), ('clock', 'TIME'),
                                 ('at_utc', 'TIMESTAMP WITH TIME ZONE'), ('big', 'DECIMAL(38,0)')])
        self.assertEqual(out[0][:7], (1, 'a', 1.5, Decimal('10.25'), date(2026, 10, 1), datetime(2026, 10, 1, 9, 30),
                                      True))
        self.assertEqual(out[0][9], Decimal(2 ** 70))

    def test_nulls_mixes_and_odd_values(self):
        rows = [(None, 1, 'x', {'k': 1}, b'\x01'), (None, 2.5, 3, [1, 2], None)]
        types, out = read(parquet.to_bytes(['empty', 'mixed_num', 'mixed', 'obj', 'blob'], rows))
        self.assertEqual([t for _, t in types], ['VARCHAR', 'DOUBLE', 'VARCHAR', 'VARCHAR', 'VARCHAR'])
        self.assertEqual(out, [(None, 1.0, 'x', '{"k": 1}', '01'), (None, 2.5, '3', '[1, 2]', None)])

    def test_column_names_are_kept_as_they_are(self):
        types, out = read(parquet.to_bytes(['a "quoted" name', 'Ünïcode', 'with space'], [(1, 2, 3)]))
        self.assertEqual([n for n, _ in types], ['a "quoted" name', 'Ünïcode', 'with space'])
        self.assertEqual(out, [(1, 2, 3)])

    def test_an_empty_result_is_a_valid_file(self):
        types, out = read(parquet.to_bytes(['a', 'b'], []))
        self.assertEqual(([n for n, _ in types], out), (['a', 'b'], []))

    def test_without_duckdb_it_says_what_to_install(self):
        with mock.patch.object(parquet, 'available', return_value=False):
            with self.assertRaises(Exception) as caught:
                parquet.to_bytes(['a'], [(1,)])
        self.assertEqual(caught.exception.code, 'format_unavailable')


class EndpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': self.tmp.name, 'QUERYAPIGATE_API_KEY': 'admin-key'})
        patcher.start()
        self.addCleanup(patcher.stop)
        data = os.path.join(self.tmp.name, 'data.db')
        conn = sqlite3.connect(data)
        conn.execute('CREATE TABLE t (id INTEGER, label TEXT, score REAL)')
        conn.executemany('INSERT INTO t VALUES (?, ?, ?)', [(i, f'row {i}', i / 2) for i in range(1, 26)])
        conn.commit()
        conn.close()
        write_connections({'lite': {'db': 'sqlite', 'database': data, 'active': True}})
        self.client = create_app().test_client()
        save_query(self.client, headers=ADMIN, body={'filename': 'rows', 'description': 'd', 'connection_name': 'lite',
                                                     'sql_query': 'SELECT id, label, score FROM t ORDER BY id'})

    def test_a_page_as_parquet(self):
        res = self.client.get('/q/rows?format=parquet&page_size=10', headers=ADMIN)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.mimetype, 'application/vnd.apache.parquet')
        self.assertEqual(res.headers['X-Has-More'], 'true')
        types, out = read(res.get_data())
        self.assertEqual(types, [('id', 'BIGINT'), ('label', 'VARCHAR'), ('score', 'DOUBLE')])
        self.assertEqual(out[0], (1, 'row 1', 0.5))
        self.assertEqual(len(out), 10)

    def test_the_whole_result_streamed_as_parquet(self):
        res = self.client.get('/q/rows?format=parquet&stream=true', headers=ADMIN)
        self.assertEqual((res.status_code, res.mimetype), (200, 'application/vnd.apache.parquet'))
        self.assertIn('rows.parquet', res.headers['Content-Disposition'])
        self.assertEqual(len(read(res.get_data())[1]), 25)

    def test_ad_hoc_sql_as_parquet(self):
        res = self.client.post('/execute_sql?format=parquet', headers=ADMIN,
                               json={'sql': 'SELECT COUNT(*) AS n FROM t', 'connection_name': 'lite'})
        self.assertEqual(read(res.get_data()), ([('n', 'BIGINT')], [(25,)]))

    def test_the_cli_exports_parquet(self):
        out = os.path.join(self.tmp.name, '{name}.parquet')
        self.assertEqual(cli.main(['export', 'rows', '--format', 'parquet', '--out', out]), 0)
        types, rows = read(os.path.join(self.tmp.name, 'rows.parquet'))
        self.assertEqual((len(rows), types[0]), (25, ('id', 'BIGINT')))


if __name__ == '__main__':
    unittest.main()

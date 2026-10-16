"""MongoDB streaming (ADR 0022): a find() streamed through ?stream=true, `queryapigate export` and saved exports, in
every streamed format - NDJSON whole, Parquet with every field, CSV and TSV with columns decided before the first row.
pymongo.MongoClient is faked (tests/test_queryapigate._mongo_client); tests/test_integration.py runs a real one."""
import csv
import io
import json
import os
from datetime import datetime
from unittest import mock

import duckdb
from pymongo.errors import ExecutionTimeout

from queryapigate import cli, history, mongodocs, parquet
from queryapigate.errors import ApiError
from tests.helpers import put_connections, save_query
from tests.test_exports import LIST, RUNS, ExportTestCase
from tests.test_parquet import read
from tests.test_queryapigate import ApiTestCase, _mongo_cursor
from tests.test_queryapigate import _mongo_client as _one_shot_client

MG = {'mg': {'db': 'mongo', 'host': 'h', 'user': 'u', 'database': 'd', 'active': True}}
DOCS = [{'_id': 1, 'name': 'a', 'total': 10, 'address': {'city': 'Oslo'}},
        {'_id': 2, 'name': 'b', 'total': 'n/a', 'tags': ['x', 'y']},
        {'_id': 3, 'name': 'c', 'total': 30, 'address': {'city': 'Pune'}, 'late': True}]


def _mongo_client(docs=()):
    """_one_shot_client(), its cursor read afresh each time - the pool keeps the first client it's given."""
    client = _one_shot_client(docs)
    cursor = client.__getitem__.return_value.__getitem__.return_value.find.return_value
    cursor.__iter__.side_effect = lambda *_: iter(list(docs))
    return client


def _failing_cursor(docs, after):
    """A cursor that times out on the server after yielding `after` of `docs`."""
    def documents():
        yield from docs[:after]
        raise ExecutionTimeout('operation exceeded time limit')

    cursor = _mongo_cursor([])
    cursor.__iter__.return_value = documents()
    return cursor


class ShapeTests(ApiTestCase):
    def test_an_inclusion_projection_fixes_the_columns_in_its_order(self):
        for projection, columns in (
                ({'name': 1, 'total': 1}, ['_id', 'name', 'total']),
                ({'total': True, 'name': 1, '_id': 0}, ['total', 'name']),
                ({'_id': 1, 'address.city': 1}, ['_id', 'address.city']),
                (None, None),
                ({}, None),
                ({'_id': 0}, None),  # exclusion only
                ({'secret': 0}, None),
                ({'name': 1, 'tags': {'$slice': 1}}, None)):  # an expression: the documents say
            with self.subTest(projection=projection):
                self.assertEqual(mongodocs.projected_columns(projection), columns)

    def test_without_a_projection_the_first_documents_decide_and_later_fields_are_counted(self):
        shaped = mongodocs.Documents(DOCS, lookahead=2)
        rows = list(shaped)
        self.assertEqual(shaped.columns, ['_id', 'name', 'total', 'address', 'tags'])
        self.assertEqual(rows[2], [3, 'c', 30, {'city': 'Pune'}, None])  # `late` has no column
        self.assertEqual((dict(shaped.dropped), shaped.report()), ({'late': 1}, {'fields_dropped': 1}))
        everything = mongodocs.Documents(DOCS)  # the first 1,000 hold them all
        list(everything)
        self.assertEqual((everything.columns[-1], everything.report()), ('late', {}))

    def test_a_projected_nested_field_is_its_own_column(self):
        shaped = mongodocs.Documents([{'_id': 1, 'address': {'city': 'Oslo'}}, {'_id': 2}],
                                     {'address.city': 1})
        self.assertEqual((list(shaped), shaped.report()), ([[1, 'Oslo'], [2, None]], {}))

    def test_growing_columns_reach_parquet_with_earlier_rows_null(self):
        shaped = mongodocs.Documents(DOCS, grow=True)
        path = os.path.join(self.tmp.name, 'docs.parquet')
        self.assertEqual(parquet.write(shaped.columns, shaped, path), 3)
        types, rows = read(path)
        self.assertEqual(types, [('_id', 'BIGINT'), ('name', 'VARCHAR'), ('total', 'VARCHAR'), ('address', 'VARCHAR'),
                                 ('tags', 'VARCHAR'), ('late', 'BOOLEAN')])  # a number and a string: text
        self.assertEqual(rows[0], (1, 'a', '10', '{"city": "Oslo"}', None, None))
        self.assertEqual(rows[2][-1], True)

    def test_no_documents_is_still_a_file(self):
        for projection, columns in ((None, [('_id', 'VARCHAR')]),):
            shaped = mongodocs.Documents([], projection, grow=True)
            path = os.path.join(self.tmp.name, 'empty.parquet')
            self.assertEqual(parquet.write(shaped.columns, shaped, path), 0)
            self.assertEqual(read(path), (columns, []))


class MongoTestCase(ApiTestCase):
    def setUp(self):
        super().setUp()
        put_connections(self.client, MG)

    def save(self, name='orders', **fields):
        self.assertEqual(save_query(self.client, {
            'filename': name, 'author': 'a', 'description': 'd', 'query_type': 'mongo', 'mongo_collection': 'orders',
            'mongo_filter': {}, 'connection_name': 'mg', **fields}).status_code, 201)

    def get(self, url, client):
        with mock.patch('pymongo.MongoClient', return_value=client):
            res = self.client.get(url)
            body = res.get_data()  # read inside the patch: the stream runs as the body is read
        return res, body

    def runs(self, name='orders'):
        history.flush()
        return history.search(query=name)[0]


class StreamTests(MongoTestCase):
    def test_ndjson_writes_each_document_whole(self):
        self.save()
        when = datetime(2026, 10, 16, 9, 30)
        res, body = self.get('/q/orders?stream=true&format=ndjson',
                             _mongo_client([{'_id': 1, 'at': when, 'address': {'city': 'Oslo'}, 'tags': [1, 2]}]))
        self.assertEqual(res.status_code, 200, body)
        self.assertEqual([json.loads(line) for line in body.decode().splitlines()],
                         [{'_id': 1, 'at': '2026-10-16 09:30:00', 'address': {'city': 'Oslo'}, 'tags': [1, 2]}])
        self.assertIn('orders.ndjson', res.headers['Content-Disposition'])

    def test_csv_takes_its_columns_from_the_projection(self):
        self.save(mongo_projection={'total': 1, 'name': 1, '_id': 0})
        client = _mongo_client([{'name': 'a', 'total': 10}, {'total': 20}])
        res, body = self.get('/q/orders?stream=true&format=csv', client)
        self.assertEqual(res.status_code, 200, body)
        self.assertEqual(list(csv.reader(io.StringIO(body.decode()))), [['total', 'name'], ['10', 'a'], ['20', '']])
        coll = client.__getitem__.return_value.__getitem__.return_value
        self.assertEqual(coll.find.call_args.kwargs, {'batch_size': 1000})
        coll.find.return_value.max_time_ms.assert_called_once_with(30000)  # the server's default time limit
        coll.find.return_value.close.assert_called_once()
        self.assertEqual((self.runs()[0]['status'], self.runs()[0]['rows']), ('success', 2))

    def test_tsv_without_a_projection_counts_the_fields_it_left_out(self):
        self.save()
        with mock.patch.object(mongodocs, 'LOOKAHEAD', 2):
            res, body = self.get('/q/orders?stream=true&format=tsv', _mongo_client(DOCS))
        lines = list(csv.reader(io.StringIO(body.decode()), delimiter='\t'))
        self.assertEqual((res.status_code, lines[0]), (200, ['_id', 'name', 'total', 'address', 'tags']))
        self.assertEqual(lines[1][3], '{"city": "Oslo"}')  # nested: JSON text
        run = self.runs()[0]
        self.assertEqual((run['status'], run['rows'], run['fields_dropped']), ('success', 3, 1))

    def test_parquet_has_every_field(self):
        self.save()
        res, body = self.get('/q/orders?stream=true&format=parquet', _mongo_client(DOCS))
        self.assertEqual(res.status_code, 200)
        types, rows = read(body)
        self.assertEqual([name for name, _ in types], ['_id', 'name', 'total', 'address', 'tags', 'late'])
        self.assertEqual(len(rows), 3)

    def test_execute_mongo_streams_too(self):
        with mock.patch('pymongo.MongoClient', return_value=_mongo_client(DOCS)):
            res = self.client.post('/execute_mongo?stream=true&format=ndjson',
                                   json={'collection': 'orders', 'connection_name': 'mg', 'projection': {'name': 1}})
            body = res.get_data(as_text=True)
        self.assertEqual((res.status_code, len(body.splitlines())), (200, 3))
        history.flush()
        run = history.search(kind='adhoc')[0][0]
        self.assertEqual((run['mongo_collection'], run['rows'], run['status']), ('orders', 3, 'success'))

    def test_the_stream_constraints_and_grants_hold(self):
        self.save()
        for url, code in (('/q/orders?stream=true&format=json', 'invalid_stream'),
                          ('/q/orders?stream=true&format=csv&page=2', 'invalid_stream')):
            with self.subTest(url=url), mock.patch('pymongo.MongoClient') as connect:
                res = self.client.get(url)
                self.assertEqual((res.status_code, res.get_json()['code']), (400, code))
                connect.assert_not_called()

    def test_a_time_limit_on_the_first_batch_is_a_504(self):
        self.save()
        client = _mongo_client()
        client.__getitem__.return_value.__getitem__.return_value.find.return_value = _failing_cursor(DOCS, 0)
        res, _ = self.get('/q/orders?stream=true&format=csv', client)
        self.assertEqual((res.status_code, res.get_json()['code']), (504, 'query_timeout'))

    def test_a_time_limit_mid_stream_ends_it_and_is_recorded(self):
        self.save()
        client = _mongo_client()
        client.__getitem__.return_value.__getitem__.return_value.find.return_value = _failing_cursor(DOCS, 2)
        with mock.patch('pymongo.MongoClient', return_value=client):
            res = self.client.get('/q/orders?stream=true&format=ndjson')
            with self.assertRaises(ApiError):
                res.get_data()
        run = self.runs()[0]
        self.assertEqual((run['status'], run['code'], run['rows']), ('error', 'query_timeout', 2))

    def test_the_stream_row_limit_applies(self):
        self.save()
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_STREAM_MAX_ROWS': '2'}):
            res, body = self.get('/q/orders?stream=true&format=ndjson', _mongo_client(DOCS))
        self.assertEqual(len(body.decode().splitlines()), 2)


class ScopedStreamTests(ApiTestCase):
    def test_a_key_streams_only_the_collections_it_may_read(self):
        os.environ['QUERYAPIGATE_API_KEY'] = 'admin-key'
        from queryapigate import create_app
        self.client = create_app().test_client()
        admin = {'X-API-Key': 'admin-key'}
        put_connections(self.client, MG, headers=admin)
        save_query(self.client, {'filename': 'orders', 'author': 'a', 'description': 'd', 'query_type': 'mongo',
                                 'mongo_collection': 'orders', 'mongo_filter': {}, 'connection_name': 'mg'},
                   headers=admin)
        key = self.client.post('/api/v1/api-keys', headers=admin, json={
            'name': 'scoped', 'connections': ['mg'], 'allowed_tables': ['customers']}).get_json()['secret']
        with mock.patch('pymongo.MongoClient') as connect:
            res = self.client.get('/q/orders?stream=true&format=csv', headers={'X-API-Key': key})
        self.assertEqual((res.status_code, res.get_json()['code']), (403, 'table_not_allowed'))
        connect.assert_not_called()


class CliExportTests(MongoTestCase):
    def export(self, fmt, docs=DOCS):
        out = os.path.join(self.tmp.name, '{name}.' + fmt)
        with mock.patch('pymongo.MongoClient', return_value=_mongo_client(docs)):
            self.assertEqual(cli.main(['export', 'orders', '--format', fmt, '--out', out]), 0)
        return os.path.join(self.tmp.name, f'orders.{fmt}')

    def test_each_format_to_a_file(self):
        self.save(mongo_projection={'name': 1})
        with open(self.export('ndjson')) as f:
            self.assertEqual(json.loads(f.readline())['address'], {'city': 'Oslo'})  # whole: the fake ignores it
        with open(self.export('csv')) as f:
            self.assertEqual(f.readline().strip(), '_id,name')
        self.assertEqual(len(read(self.export('parquet'))[1]), 3)
        self.assertEqual([r['rows'] for r in self.runs()], [3, 3, 3])


class SavedExportTests(ExportTestCase):
    def setUp(self):
        super().setUp()
        put_connections(self.client, MG, headers={'X-API-Key': 'admin-key'})
        self.call('post', '/api/v1/queries', '/api/v1/queries', 201, json={
            'name': 'docs', 'connection_name': 'mg', 'description': 'd', 'publish': True, 'query_type': 'mongo',
            'mongo_collection': 'orders', 'mongo_filter': {}})

    def test_a_find_is_exported_whole_with_every_field(self):
        for fmt in ('parquet', 'csv', 'ndjson'):
            with self.subTest(fmt=fmt):
                self.call('post', LIST, LIST, 201, json={'name': f'docs-{fmt}', 'query': 'docs', 'destination': 'drop',
                                                         'format': fmt, 'path': f'docs/{{run}}.{fmt}'})
                with mock.patch('pymongo.MongoClient', return_value=_mongo_client(DOCS)):
                    run = self.call('post', f'{LIST}/docs-{fmt}/runs', RUNS, 201).get_json()
                self.assertEqual(run['rows'], 3)
                columns = [d[0] for d in duckdb.sql(f"SELECT * FROM '{run['object']}'").description]
                self.assertEqual(columns, ['_id', 'name', 'total', 'address', 'tags', 'late'])

    def test_an_incremental_export_of_a_find_is_refused(self):
        res = self.call('post', LIST, LIST, 400, json={
            'name': 'docs-inc', 'query': 'docs', 'destination': 'drop', 'path': 'docs/{run}.parquet',
            'incremental': {'column': 'updated_at', 'parameter': 'since', 'start': '2000-01-01'}})
        self.assertIn('incrementally', res.get_json()['error'])

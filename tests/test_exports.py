"""Saved exports (ADR 0004, exports.py): defined against a destination and a published query, run over HTTP or the
CLI, one run at a time, each incremental run delivering only what changed - and a failed run losing nothing."""
import contextlib
import io
import os
import sqlite3
import time
from unittest import mock

import duckdb

from queryapigate import admins, cli, db, exporting, exports, history, store
from tests.test_api_v1 import ADMIN, V1TestCase

LIST = '/api/v1/exports'
ONE = '/api/v1/exports/{name}'
RUNS = '/api/v1/exports/{name}/runs'


class ExportTestCase(V1TestCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, {'QUERYAPIGATE_HISTORY_FLUSH_INTERVAL': '0'})
        patcher.start()
        self.addCleanup(patcher.stop)
        conn = sqlite3.connect(self.db_path)
        conn.execute('CREATE TABLE orders (id INTEGER, region TEXT, total INTEGER, updated_at TEXT)')
        conn.executemany('INSERT INTO orders VALUES (?, ?, ?, ?)',
                         [(1, 'EU', 10, '2026-10-01 09:00:00'), (2, 'US', 20, '2026-10-01 10:00:00'),
                          (3, 'EU', 30, '2026-10-02 09:00:00')])
        conn.commit()
        conn.close()
        self.call('post', '/api/v1/queries', '/api/v1/queries', 201, json={
            'name': 'orders_since', 'connection_name': 'lite', 'description': 'd', 'publish': True,
            'sql': 'SELECT id, region, total, updated_at FROM orders WHERE region = :region AND updated_at > :since '
                   'ORDER BY updated_at',
            'parameters': {'region': {'type': 'str'}, 'since': {'type': 'str'}}})
        self.out = os.path.join(self.tmp.name, 'drop') + '/'
        self.call('post', '/api/v1/destinations', '/api/v1/destinations', 201, json={'name': 'drop', 'url': self.out})

    def define(self, status=201, **fields):
        body = {'name': 'eu-orders', 'query': 'orders_since', 'params': {'region': 'EU'}, 'destination': 'drop',
                'path': 'orders/{region}/{run}.parquet',
                'incremental': {'column': 'updated_at', 'parameter': 'since', 'start': '2000-01-01'}, **fields}
        return self.call('post', LIST, LIST, status, json=body)

    def add_order(self, id_, region, updated_at):
        conn = sqlite3.connect(self.db_path)
        conn.execute('INSERT INTO orders VALUES (?, ?, 1, ?)', (id_, region, updated_at))
        conn.commit()
        conn.close()

    def run_export(self, status=201, name='eu-orders', headers=ADMIN):
        return self.call('post', f'{LIST}/{name}/runs', RUNS, status, headers=headers)

    def ids(self, run):
        return [r[0] for r in duckdb.sql(f"SELECT id FROM '{run['object']}'").fetchall()]


class DefinitionTests(ExportTestCase):
    def test_an_export_is_checked_against_its_query_and_destination(self):
        created = self.define().get_json()
        self.assertEqual((created['format'], created['skip_empty'], created['watermark'], created['running']),
                         ('parquet', True, None, False))
        self.define(status=409)
        for fields, status, code in (
                ({'name': 'x', 'destination': 'nowhere'}, 404, 'destination_not_found'),
                ({'name': 'x', 'query': 'no_such_query'}, 404, 'query_not_found'),
                ({'name': 'x', 'format': 'xlsx'}, 400, 'invalid_body'),
                ({'name': 'x', 'path': '{nope}.parquet'}, 400, 'invalid_body'),
                ({'name': 'x', 'path': '../out.parquet'}, 400, 'invalid_body'),
                ({'name': 'x', 'incremental': {'column': 'updated_at', 'parameter': 'until', 'start': 'x'}}, 400,
                 'invalid_body'),
                ({'name': 'x', 'incremental': {'column': 'updated_at'}}, 400, 'invalid_body'),
                ({'name': 'x', 'params': {'region': 'EU', 'since': 'x'}}, 400, 'invalid_body'),
                ({'name': 'x', 'colour': 'blue'}, 400, 'unknown_field'),
                ({'name': 'has space'}, 400, 'invalid_name')):
            with self.subTest(fields=fields):
                res = self.define(status=status, **fields)
                self.assertEqual(res.get_json()['code'], code)

    def test_change_and_remove_with_if_match_and_audit(self):
        self.define()
        etag = self.call('get', f'{LIST}/eu-orders', ONE, 200).headers['ETag']
        self.call('patch', f'{LIST}/eu-orders', ONE, 200, json={'format': 'csv', 'path': 'eu/{date}.csv'},
                  headers={**ADMIN, 'If-Match': etag})
        self.call('patch', f'{LIST}/eu-orders', ONE, 412, json={'format': 'ndjson'},
                  headers={**ADMIN, 'If-Match': etag})
        self.call('delete', f'{LIST}/eu-orders', ONE, 204)
        actions = [e['action'] for e in store.read_audit_log() if e['target'] == 'eu-orders']
        self.assertEqual(actions, ['create_export', 'update_export', 'delete_export'])

    def test_a_destination_in_use_stays(self):
        self.define()
        res = self.call('delete', '/api/v1/destinations/drop', '/api/v1/destinations/{name}', 409)
        self.assertEqual(res.get_json()['code'], 'destination_in_use')


class RunTests(ExportTestCase):
    def test_each_run_delivers_only_what_changed(self):
        self.define()
        first = self.run_export().get_json()
        self.assertEqual((first['rows'], self.ids(first)), (2, [1, 3]))
        self.assertEqual(first['watermark'], {'from': None, 'to': '2026-10-02 09:00:00'})
        self.assertTrue(first['object'].startswith(os.path.join(self.out, 'orders', 'EU', '')))
        self.add_order(4, 'EU', '2026-10-03 09:00:00')
        self.add_order(5, 'US', '2026-10-04 09:00:00')
        second = self.run_export().get_json()
        self.assertEqual((second['rows'], self.ids(second)), (1, [4]))
        self.assertEqual(second['watermark'], {'from': '2026-10-02 09:00:00', 'to': '2026-10-03 09:00:00'})
        nothing = self.run_export().get_json()
        self.assertEqual((nothing['rows'], nothing['object']), (0, None))  # skip_empty: no file
        self.assertEqual(nothing['watermark'], {'from': '2026-10-03 09:00:00', 'to': '2026-10-03 09:00:00'})
        self.assertEqual(len(os.listdir(os.path.join(self.out, 'orders', 'EU'))), 2)

    def test_a_reset_watermark_delivers_everything_again(self):
        self.define()
        self.run_export()
        self.call('patch', f'{LIST}/eu-orders', ONE, 200, json={'watermark': None})
        self.assertEqual(self.ids(self.run_export().get_json()), [1, 3])
        self.call('patch', f'{LIST}/eu-orders', ONE, 200, json={'watermark': '2026-10-01 12:00:00'})
        self.assertEqual(self.ids(self.run_export().get_json()), [3])
        self.assertIn('update_export', [e['action'] for e in store.read_audit_log()])

    def test_a_failed_run_loses_nothing_and_raises_the_alert(self):
        from queryapigate import metrics
        active = metrics._active_queries
        self.define()
        self.run_export()
        self.add_order(4, 'EU', '2026-10-03 09:00:00')
        failure = exporting.ApiError("Couldn't write: bucket gone", 502, code='destination_unreachable')
        with mock.patch.object(exporting, 'deliver', side_effect=failure):
            res = self.run_export(status=502)
        self.assertEqual(res.get_json()['code'], 'destination_unreachable')
        export = self.call('get', f'{LIST}/eu-orders', ONE, 200).get_json()
        self.assertEqual((export['watermark'], export['running']), ('2026-10-02 09:00:00', False))
        self.assertEqual(metrics._active_queries, active)  # the query was ended, its connection given back
        alerts = self.call('get', '/api/v1/alerts', '/api/v1/alerts', 200).get_json()['items']
        self.assertIn('export_failing', [a['kind'] for a in alerts])
        self.assertEqual(self.ids(self.run_export().get_json()), [4])  # the next run catches up
        alerts = self.call('get', '/api/v1/alerts', '/api/v1/alerts', 200).get_json()['items']
        self.assertNotIn('export_failing', [a['kind'] for a in alerts])

    def test_a_failure_before_the_query_runs_is_recorded_too(self):
        self.define()
        self.call('delete', '/api/v1/queries/orders_since', '/api/v1/queries/{name}', 204)
        self.run_export(status=404)
        alerts = self.call('get', '/api/v1/alerts', '/api/v1/alerts', 200).get_json()['items']
        self.assertIn('export_failing', [a['kind'] for a in alerts])

    def test_one_run_at_a_time(self):
        self.define()
        with db.transaction() as conn:
            conn.execute('UPDATE exports SET lease_until = ? WHERE name = ?', (time.time() + 60, 'eu-orders'))
        res = self.run_export(status=409)
        self.assertEqual(res.get_json()['code'], 'export_running')
        self.assertGreater(int(res.headers['Retry-After']), 0)
        self.assertTrue(self.call('get', f'{LIST}/eu-orders', ONE, 200).get_json()['running'])
        with db.transaction() as conn:  # a crashed run's lease expires
            conn.execute('UPDATE exports SET lease_until = ? WHERE name = ?', (time.time() - 1, 'eu-orders'))
        self.run_export()
        self.assertFalse(self.call('get', f'{LIST}/eu-orders', ONE, 200).get_json()['running'])

    def test_runs_are_in_history(self):
        self.define()
        run = self.run_export().get_json()
        runs = self.call('get', f'{LIST}/eu-orders/runs', RUNS, 200).get_json()['items']
        self.assertEqual([(r['run_id'], r['transport'], r['status'], r['object']) for r in runs],
                         [(run['id'], 'export', 'success', run['object'])])

    def test_a_watermark_the_parameter_would_refuse_writes_nothing(self):
        # A date-only parameter, and a timestamp column: the first run would deliver, then every run after it fail.
        self.call('post', '/api/v1/queries', '/api/v1/queries', 201, json={
            'name': 'orders_by_day', 'connection_name': 'lite', 'description': 'd', 'publish': True,
            'sql': 'SELECT id, updated_at, date(updated_at) AS day FROM orders WHERE date(updated_at) > :since',
            'parameters': {'since': {'type': 'str', 'pattern': r'\d{4}-\d{2}-\d{2}'}}})
        self.define(name='by-ts', query='orders_by_day', params={}, path='orders/{run}.parquet',
                    incremental={'column': 'updated_at', 'parameter': 'since', 'start': '2000-01-01'})
        res = self.run_export(status=400, name='by-ts')
        self.assertEqual(res.get_json()['code'], 'invalid_watermark')
        self.assertIn('Nothing was written', res.get_json()['error'])
        self.assertFalse(os.path.exists(self.out + 'orders'))
        self.assertIsNone(self.call('get', f'{LIST}/by-ts', ONE, 200).get_json()['watermark'])
        self.define(name='by-day', query='orders_by_day', params={}, path='days/{run}.parquet',
                    incremental={'column': 'day', 'parameter': 'since', 'start': '2000-01-01'})
        self.assertEqual(self.run_export(name='by-day').get_json()['watermark']['to'], '2026-10-02')

    def test_a_plain_export_without_a_watermark(self):
        self.define(name='all-us', params={'region': 'US', 'since': '2000-01-01'}, incremental=None,
                    format='csv', path='{name}.csv', skip_empty=False)
        run = self.run_export(name='all-us').get_json()
        self.assertEqual((run['rows'], run['watermark']), (1, None))
        with open(run['object']) as f:
            self.assertEqual(f.readline().strip(), 'id,region,total,updated_at')


class RoleTests(ExportTestCase):
    def test_owners_and_admins_define_developers_run_auditors_read(self):
        self.define()
        for role, define, run, read in (('developer', 403, 201, 200), ('auditor', 403, 403, 200),
                                        ('admin', 201, 201, 200)):
            with self.subTest(role=role):
                admins.create_admin(f'{role}-1', role)
                token = {'X-API-Key': admins.issue_token(f'{role}-1')[1]}
                body = {'name': f'by-{role}', 'query': 'orders_since', 'destination': 'drop', 'path': 'x.parquet',
                        'params': {'region': 'EU', 'since': '2000-01-01'}}
                self.assertEqual(self.client.post(LIST, headers=token, json=body).status_code, define)
                self.assertEqual(self.client.post(f'{LIST}/eu-orders/runs', headers=token).status_code, run)
                self.assertEqual(self.client.get(f'{LIST}/eu-orders', headers=token).status_code, read)
        runs = exports.get('eu-orders')
        self.assertFalse(runs['running'])

    def test_the_run_is_recorded_under_who_ran_it(self):
        self.define()
        admins.create_admin('ci-deploy', 'developer')
        self.run_export(headers={'X-API-Key': admins.issue_token('ci-deploy')[1]})
        history.flush()
        entry = history.search(query='orders_since')[0][0]
        self.assertEqual(entry['key_name'], 'ci-deploy')


class CliTests(ExportTestCase):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_run_and_list(self):
        self.define()
        code, out, err = self.run_cli('exports', 'run', 'eu-orders')
        self.assertEqual(code, 0, err)
        self.assertIn('Wrote 2 rows to', out)
        self.assertIn('Watermark: None -> 2026-10-02 09:00:00', out)
        code, out, _ = self.run_cli('exports', 'list')
        self.assertIn('eu-orders  orders_since -> drop:orders/{region}/{run}.parquet  parquet  watermark '
                      '2026-10-02 09:00:00', out)
        code, _, err = self.run_cli('exports', 'run', 'nope')
        self.assertEqual((code, "Export 'nope' not found" in err), (1, True))

"""Tests for history.py - batched, sampled, retained run history - and GET /history. Runs on whichever metadata
backend the suite runs on (see tests/__init__.py)."""
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from unittest import mock

from queryapigate import config, create_app, db, history, metrics, store
from tests import TEST_DATABASE_URL
from tests.helpers import save_query, write_connections

HISTORY_ENV = ('QUERYAPIGATE_HISTORY_LIMIT', 'QUERYAPIGATE_HISTORY_RETENTION_DAYS', 'QUERYAPIGATE_HISTORY_SAMPLE_RATE',
               'QUERYAPIGATE_HISTORY_FLUSH_INTERVAL')


class HistoryTestCase(unittest.TestCase):
    env = {}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = os.path.join(self.tmp.name, 'data.db')
        conn = sqlite3.connect(self.data)
        conn.execute('CREATE TABLE t (id INTEGER)')
        conn.executemany('INSERT INTO t VALUES (?)', [(i,) for i in range(5)])
        conn.commit()
        conn.close()
        env = {name: '' for name in HISTORY_ENV}
        patcher = mock.patch.dict(os.environ, {**env, 'QUERYAPIGATE_HOME': self.tmp.name,
                                               'QUERYAPIGATE_API_KEY': 'admin', **self.env})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(history.flush)  # nothing of this test's left queued for the next one
        write_connections({'lite': {'db': 'sqlite', 'database': self.data, 'active': True}})
        self.client = create_app().test_client()
        self.admin = {'X-API-Key': 'admin'}
        for name in ('q', 'other'):
            self.assertEqual(save_query(self.client, headers=self.admin, body={
                'author': 'a', 'description': 'd', 'filename': name, 'connection_name': 'lite',
                'sql_query': 'SELECT id FROM t WHERE id >= :min', 'query_parameters': {'min': 'int'}}).status_code, 201)

    def run_query(self, name='q', minimum=0, headers=None):
        return self.client.get(f'/q/{name}?min={minimum}', headers=headers or self.admin)

    def stored(self):
        """Rows actually in the store - deliberately *without* flushing first."""
        return db.connection().execute('SELECT COUNT(*) FROM execution_history').fetchone()[0]

    def listed(self, name='q'):
        """The newest runs a version carries with it (store.load_versions) - bounded by QUERYAPIGATE_HISTORY_LIMIT."""
        return store.load_versions(name)['1']['execution_history']

    def seed(self, count, start, name='q', status='success', key='admin'):
        """``count`` runs one minute apart from ``start``, written straight to the store."""
        rows = [history._row(name, 1, {'executed_at': (start + timedelta(minutes=i)).strftime(history.TIME_FORMAT),
                                       'status': status, 'key_name': key, 'i': i}) for i in range(count)]
        history.write(rows, db.current_target())


class BatchingTests(HistoryTestCase):
    def test_runs_are_queued_then_written_in_one_batch(self):
        with mock.patch.object(history._Writer, '_ensure_thread'):  # no writer thread: nothing drains on its own
            for _ in range(3):
                self.assertEqual(self.run_query().status_code, 200)
            self.assertEqual((self.stored(), history.pending_count()), (0, 3))
            history._writer().flush()
        self.assertEqual((self.stored(), history.pending_count()), (3, 0))

    def test_a_process_always_reads_its_own_runs(self):
        for _ in range(3):
            self.run_query()
        self.assertEqual(len(self.listed()), 3)  # flushed by the read itself, not by waiting for the interval

    def test_a_read_waits_for_a_batch_the_writer_is_still_writing(self):
        """The writer takes a batch off the queue before writing it, so mid-write the queue is empty - a read must
        still wait for that batch to commit, or it misses the runs in it (an intermittent failure on Postgres)."""
        real_write, entered, release = history.write, threading.Event(), threading.Event()

        def slow_write(rows, target=None):
            entered.set()
            release.wait(5)
            real_write(rows, target)
        with mock.patch.object(history, 'write', side_effect=slow_write):
            self.run_query()
            history._writer().wake.set()
            self.assertTrue(entered.wait(5))  # the batch is off the queue and being written
            self.assertEqual(history.pending_count(), 0)
            done = threading.Event()
            reader = threading.Thread(target=lambda: (history.flush(), done.set()))
            reader.start()
            self.assertFalse(done.wait(0.3))  # still waiting: the batch hasn't committed yet
            release.set()
            self.assertTrue(done.wait(5))
            reader.join(5)
        self.assertEqual(self.stored(), 1)

    def test_a_flush_interval_of_zero_writes_inside_the_request(self):
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_HISTORY_FLUSH_INTERVAL': '0'}):
            self.run_query()
            self.assertEqual((self.stored(), history.pending_count()), (1, 0))

    def test_a_full_queue_drops_and_counts_instead_of_blocking(self):
        before = dict(metrics._history_runs)
        with mock.patch.object(history, '_MAX_PENDING', 2), mock.patch.object(history._Writer, '_ensure_thread'):
            for _ in range(5):
                self.assertEqual(self.run_query().status_code, 200)  # the request itself never notices
            self.assertEqual(history.pending_count(), 2)
            history._writer().flush()
        self.assertEqual(metrics._history_runs.get(('dropped',), 0) - before.get(('dropped',), 0), 3)
        self.assertEqual(self.stored(), 2)

    def test_a_run_of_a_query_deleted_meanwhile_is_skipped_not_fatal(self):
        with mock.patch.object(history._Writer, '_ensure_thread'):
            self.run_query('q')
            self.run_query('other')
            self.assertEqual(self.client.delete('/api/v1/queries/other', headers=self.admin).status_code, 204)
            history._writer().flush()
        self.assertEqual(len(self.listed('q')), 1)  # the batch's other run still landed

    def test_metrics_report_history_outcomes(self):
        self.run_query()
        history.flush()
        body = self.client.get('/metrics').get_data(as_text=True)
        self.assertIn('queryapigate_history_runs_total{outcome="recorded"}', body)
        self.assertIn('queryapigate_history_pending ', body)


class SamplingTests(HistoryTestCase):
    env = {'QUERYAPIGATE_HISTORY_SAMPLE_RATE': '0.5'}

    def test_only_the_sampled_fraction_of_successes_is_kept_but_every_failure_is(self):
        with mock.patch.object(history.random, 'random', side_effect=[0.1, 0.9, 0.3, 0.7]):
            for _ in range(4):
                self.assertEqual(self.run_query().status_code, 200)
        # failing runs - an error from the database itself, after validation passed - are never sampled out
        with mock.patch.dict('queryapigate.engine.RUNNERS', {'sqlite': mock.Mock(side_effect=RuntimeError('x'))}):
            for _ in range(3):
                self.assertEqual(self.run_query().status_code, 500)
        statuses = [run['status'] for run in self.listed()]
        self.assertEqual((statuses.count('success'), statuses.count('error')), (2, 3))


class RetentionTests(HistoryTestCase):
    def test_without_retention_each_version_keeps_its_newest_runs(self):
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_HISTORY_LIMIT': '5'}):
            self.seed(8, datetime(2026, 1, 1))
            self.assertEqual(self.stored(), 5)
            self.assertEqual([run['i'] for run in self.listed()], [3, 4, 5, 6, 7])

    def test_with_retention_every_run_is_kept_and_lists_still_show_only_the_newest(self):
        env = {'QUERYAPIGATE_HISTORY_LIMIT': '5', 'QUERYAPIGATE_HISTORY_RETENTION_DAYS': '30'}
        with mock.patch.dict(os.environ, env):
            self.seed(8, datetime(2026, 1, 1))
            self.assertEqual(self.stored(), 8)
            self.assertEqual([run['i'] for run in self.listed()], [3, 4, 5, 6, 7])  # lists stay bounded
            entries = self.client.get('/api/v1/history', headers=self.admin).get_json()['items']
            self.assertEqual(len(entries), 8)  # the whole history is still reachable

    def test_the_sweep_deletes_only_runs_older_than_the_retention_period(self):
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_HISTORY_RETENTION_DAYS': '7'}):
            now = datetime(2026, 3, 1, 12, 0, 0)
            self.seed(3, now - timedelta(days=10))
            self.seed(2, now - timedelta(days=1))
            with mock.patch.object(history, '_SWEEP_CHUNK', 2):  # several chunks, still all of them
                self.assertEqual(history.sweep(now=now), 3)
            self.assertEqual(self.stored(), 2)

    def test_no_retention_no_sweep(self):
        self.seed(3, datetime(2000, 1, 1))
        self.assertEqual(history.sweep(), 0)
        self.assertEqual(self.stored(), 3)


class HistoryEndpointTests(HistoryTestCase):
    env = {'QUERYAPIGATE_HISTORY_RETENTION_DAYS': '365'}

    def get(self, query='', headers=None):
        return self.client.get(f'/api/v1/history{query}', headers=headers or self.admin)

    def test_pages_through_everything_newest_first_without_gaps_or_repeats(self):
        start = datetime(2026, 2, 1)
        self.seed(7, start)
        self.seed(3, start, name='other')  # same timestamps: rowid breaks the tie
        seen, cursor = [], ''
        while True:
            page = self.get(f'?limit=3{cursor}').get_json()
            seen += [(e['query'], e['executed_at'], e['i']) for e in page['items']]
            if page['next_cursor'] is None:
                break
            cursor = f"&cursor={page['next_cursor']}"
        self.assertEqual(len(seen), 10)
        self.assertEqual(len(set(seen)), 10)
        self.assertEqual([s[1] for s in seen], sorted((s[1] for s in seen), reverse=True))

    def test_filters(self):
        start = datetime(2026, 2, 1)
        self.seed(4, start, status='success', key='partner')
        self.seed(2, start + timedelta(days=1), status='error', key='partner')
        self.seed(3, start, name='other', key='admin')
        count = lambda query: len(self.get(query).get_json()['items'])  # noqa: E731
        self.assertEqual(count('?status=error'), 2)
        self.assertEqual(count('?key=partner&status=success'), 4)
        self.assertEqual(count('?query=other'), 3)
        self.assertEqual(count('?query=q&version=1'), 6)
        self.assertEqual(count('?since=2026-02-02'), 2)
        self.assertEqual(count('?until=2026-02-01 00:02:00'), 4)  # exclusive: 00:00 and 00:01 of each query
        entry = self.get('?status=error&limit=1').get_json()['items'][0]
        self.assertEqual((entry['query'], entry['version'], entry['key_name']), ('q', 1, 'partner'))

    def test_live_runs_show_up_with_their_caller(self):
        self.run_query()
        entry = self.get().get_json()['items'][0]
        self.assertEqual((entry['query'], entry['status'], entry['key_name'], entry['rows']),
                         ('q', 'success', 'admin', 5))

    def test_bad_input_is_a_400(self):
        for query in ('?status=failed', '?since=2026-13-01', '?until=yesterday', '?limit=0', '?limit=1001',
                      '?cursor=not-a-cursor', '?version=one'):
            self.assertEqual(self.get(query).status_code, 400, query)

    def test_admin_only(self):
        key = self.client.post('/api/v1/api-keys', headers=self.admin, json={'name': 'scoped', 'connections': ['lite']}
                               ).get_json()['secret']
        self.assertEqual(self.get(headers={'X-API-Key': key}).status_code, 403)


class SettingsValidationTests(unittest.TestCase):
    def test_malformed_values_stop_startup(self):
        for name, value in (('QUERYAPIGATE_HISTORY_LIMIT', '0'), ('QUERYAPIGATE_HISTORY_RETENTION_DAYS', 'a week'),
                            ('QUERYAPIGATE_HISTORY_SAMPLE_RATE', '0'), ('QUERYAPIGATE_HISTORY_SAMPLE_RATE', '1.5'),
                            ('QUERYAPIGATE_HISTORY_FLUSH_INTERVAL', '-1'),
                            ('QUERYAPIGATE_HISTORY_FLUSH_INTERVAL', 'inf')):
            with mock.patch.dict(os.environ, {name: value}), self.assertRaises(ValueError, msg=f'{name}={value}'):
                config.check_settings()

    def test_good_values_are_accepted(self):
        env = {'QUERYAPIGATE_HISTORY_LIMIT': '200', 'QUERYAPIGATE_HISTORY_RETENTION_DAYS': '90',
               'QUERYAPIGATE_HISTORY_SAMPLE_RATE': '0.05', 'QUERYAPIGATE_HISTORY_FLUSH_INTERVAL': '0'}
        with mock.patch.dict(os.environ, env):
            config.check_settings()
            self.assertEqual((config.history_limit(), config.history_retention_days(), config.history_sample_rate(),
                              config.history_flush_interval()), (200, 90, 0.05, 0.0))


class SchemaUpgradeTests(unittest.TestCase):
    """A store created before execution_history had status/key_name columns (schema 2) is upgraded in place on
    first start, its existing runs backfilled from their own entry_json."""

    def test_status_and_key_name_are_added_and_backfilled(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': home}):
            self.addCleanup(db.close)
            schema = db._PG_SCHEMA if TEST_DATABASE_URL else db._SCHEMA
            old = '\n'.join(line for line in schema.splitlines()
                            if not line.strip().startswith(('status TEXT', 'key_name TEXT')))
            conn = db.connection()
            conn.executescript(old)
            conn.execute('INSERT INTO schema_version (version) VALUES (2)')
            conn.execute("INSERT INTO saved_queries (name, example) VALUES ('q', 0)")
            conn.execute("INSERT INTO saved_query_versions (query_name, version, uuid, created_at, last_modified_at, "
                         "fields_json) VALUES ('q', 1, 'u', 'now', 'now', '{}')")
            conn.execute('INSERT INTO execution_history (query_name, version, executed_at, entry_json) '
                         'VALUES (?, ?, ?, ?)', ('q', 1, '2026-01-01 00:00:00',
                                                 json.dumps({'status': 'error', 'key_name': 'partner'})))
            db.init_schema()
            db.init_schema()  # and again: every step checks for itself
            row = db.connection().execute('SELECT status, key_name FROM execution_history').fetchone()
            self.assertEqual((row[0], row[1]), ('error', 'partner'))
            self.assertEqual(db.connection().execute('SELECT version FROM schema_version').fetchone()[0],
                             db.SCHEMA_VERSION)


if __name__ == '__main__':
    unittest.main()

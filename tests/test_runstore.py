"""Tests for runstore.py (ADR 0029): run history, the event log and usage rollups in ClickHouse.

The first classes need no ClickHouse: the URL's rules, the parameters' escaping, the event payload, the read windows,
the id leases and the marker's refusals. The rest run when QUERYAPIGATE_TEST_RUN_STORE_URL names a scratch ClickHouse
(tests/__init__.py), as CI's run-store job does - and then the whole suite runs with the run store too."""
import asyncio
import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from unittest import mock

from queryapigate import (
    __version__,
    config,
    create_app,
    dataplane,
    db,
    events,
    history,
    metrics,
    runstore,
    store,
    usage,
)
from tests import TEST_RUN_STORE_URL
from tests.helpers import save_query, write_connections
from tests.test_dataplane import SECRET
from tests.test_events_server import Stream


class HomeTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        db.init_schema()


class UrlTests(unittest.TestCase):
    def check(self, url):
        with mock.patch.object(config, 'run_store_url', lambda: url):
            config.check_run_store_url()

    def test_https_or_this_machine(self):
        self.check('https://u:p@clickhouse.internal:8443/runs')
        self.check('http://u:p@localhost:8123/runs')
        self.check('http://u:p@127.0.0.1:8123')
        with self.assertRaisesRegex(ValueError, 'https://'):
            self.check('http://u:p@clickhouse.internal:8123/runs')

    def test_a_url_clickhouse_can_take(self):
        for url, why in (('clickhouse://h:9000/runs', 'HTTP interface'), ('https:///runs', 'HTTP interface'),
                         ('https://h/runs-2026', 'database name'), ('https://h:99999/runs', '(?i)port')):
            with self.subTest(url=url), self.assertRaisesRegex(ValueError, why):
                self.check(url)

    def test_where_runs_are_is_named_without_its_credentials(self):
        self.assertEqual(runstore.location('https://u:secret@ch.internal/runs'), 'ch.internal:8443/runs')
        self.assertEqual(runstore.location('http://u:secret@localhost'), 'localhost:8123/queryapigate')


class ParameterTests(unittest.TestCase):
    def test_values_reach_clickhouse_as_written(self):
        self.assertEqual(runstore._param("it's\ta\\b\nc"), "it's\\ta\\\\b\\nc")
        self.assertEqual(runstore._param(['a', "b'c"]), "['a','b\\'c']")
        self.assertEqual(runstore._param([1, 2]), '[1,2]')
        self.assertEqual(runstore._param(None), '\\N')
        self.assertEqual(runstore._param(True), '1')


class PayloadTests(unittest.TestCase):
    """A run's event is rebuilt from its columns when read: it must be byte for byte what the metadata store keeps."""

    def row(self, name, version, entry):
        return {'type': events.RUN, 'source': entry.get('transport'), 'occurred_at': entry['executed_at'],
                'query_name': name, 'version': version, 'connection_name': entry.get('connection_name'),
                'entry': json.dumps(entry, default=str), 'payload_json': ''}

    def test_saved_and_adhoc_runs(self):
        for name, version, entry in (
                ('q', 3, {'executed_at': '2026-10-28 12:00:00', 'connection_name': 'shop', 'request_id': 'ab12',
                          'key_name': 'mobile', 'transport': 'rest', 'status': 'success', 'rows': 2,
                          'duration_ms': 1, 'masked': [{'column': 'ssn', 'action': 'hash'}]}),
                (None, None, {'executed_at': '2026-10-28 12:00:01', 'transport': 'mcp', 'status': 'error',
                              'error': 'naïve "quote"', 'sql': 'SELECT 1', 'params': []})):
            with self.subTest(kind='saved' if name else 'adhoc'):
                self.assertEqual(runstore.event_payload(self.row(name, version, entry)),
                                 json.dumps(events.run_payload(name, version, entry), default=str))

    def test_any_other_event_as_written(self):
        self.assertEqual(runstore.event_payload({'type': 'key.revoked', 'payload_json': '{"x": 1}'}), '{"x": 1}')

    def test_a_line_break_in_a_stored_row_never_ends_its_frame(self):
        """Whoever has the run store's credentials writes its rows: a break in one must not start a frame of its own -
        another event, with an id that moves every client's resume point (the security review, 2026-10-28)."""
        row = self.row('q', 1, {'executed_at': '2026-10-28 12:00:00', 'status': 'success'})
        row['entry'] = '{"status": "success"}\n\nid: 9007199254740000\r\ndata: {"type": "forged"}'
        frame = events._format(1, runstore.event_payload(row)).decode()
        self.assertEqual(frame.count('\n'), 3, frame)  # its id, its data, the blank line that ends it
        self.assertEqual(frame.count('\r'), 0)
        self.assertEqual(json.loads(events._format(2, '{\n"a": 1\r\n}').decode().split('data: ')[1]), {'a': 1})


class WindowTests(unittest.TestCase):
    def test_newest_first_a_minute_an_hour_a_day_then_the_rest(self):
        spans = list(runstore._windows('2026-10-28 12:00:00', None))
        self.assertEqual(spans, [('2026-10-28 11:59:00', '2026-10-28 12:00:00'),
                                 ('2026-10-28 11:00:00', '2026-10-28 11:59:00'),
                                 ('2026-10-27 12:00:00', '2026-10-28 11:00:00'),
                                 (None, '2026-10-27 12:00:00')])

    def test_stops_at_the_floor(self):
        self.assertEqual(list(runstore._windows('2026-10-28 12:00:00', '2026-10-28 11:30:00')),
                         [('2026-10-28 11:59:00', '2026-10-28 12:00:00'),
                          ('2026-10-28 11:30:00', '2026-10-28 11:59:00')])

    def test_a_date(self):
        self.assertEqual(next(runstore._windows('2026-10-28', None)), ('2026-10-27 23:59:00', '2026-10-28'))


class ReplayTests(unittest.TestCase):
    def test_a_keys_creation_bounds_the_ids_a_replay_reads(self):
        since = '2026-10-28 12:00:00'
        for given, starts in ((since, runstore._id_span(since, since)[0] - 1), (None, 5)):
            with self.subTest(since=given), mock.patch.object(runstore, 'replay_ids', return_value=[]) as ids:
                self.assertEqual(list(runstore.replay_pages(5, lambda: runstore.MAX_ID, 'default', 'k', {}, given,
                                                            1000)), [])
                self.assertEqual(ids.call_args[0][0], starts)


class LeaseTests(HomeTestCase):
    def test_writers_get_blocks_that_dont_overlap(self):
        starts = [runstore.lease(16, f'w{i}') for i in range(3)] + [runstore.lease(256, 'dataplane')]
        self.assertEqual(starts, [0, 16, 32, 48])

    def test_a_freed_block_is_reused_and_a_lapsed_one_reclaimed(self):
        first, second = runstore.lease(16, 'a'), runstore.lease(16, 'b')
        runstore.release(first, 'a')
        self.assertEqual(runstore.lease(16, 'c'), first)
        real = time.time
        with mock.patch.object(runstore.time, 'time', lambda: real() + runstore.LEASE_SECONDS + 1):
            self.assertEqual(runstore.lease(16, 'd'), 0)  # every lease above had lapsed
        self.assertFalse(runstore.renew(second, 'b'))

    def test_leasing_leaves_the_metadata_cache_alone(self):
        from queryapigate import metacache
        with mock.patch.object(metacache, 'clear') as cleared:
            block = runstore.lease(16, 'w')
            self.assertTrue(runstore.renew(block, 'w'))
            runstore.release(block, 'w')
        cleared.assert_not_called()

    def test_every_block_taken(self):
        runstore.lease(runstore.PER_MS - 8, 'big')
        with self.assertRaisesRegex(runstore.LeaseError, 'too many processes'):
            runstore.lease(16, 'one too many')


class MintTests(HomeTestCase):
    def minter(self, size=4):
        minter = runstore.Minter(size)
        self.addCleanup(lambda: runstore.release(minter.block, minter.holder))
        return minter

    def test_increasing_unique_and_below_2_to_the_53(self):
        a, b = self.minter(), self.minter()
        with mock.patch.object(runstore, 'store_ms', lambda: 26_000_000_000):
            ids = a.mint(10) + b.mint(10)
            self.assertGreater(a.mint(1)[0], max(ids[:10]))
        self.assertEqual(len(set(ids)), 20)
        self.assertTrue(all(i < 2 ** 53 for i in ids))
        self.assertEqual(sorted(ids[:10]), ids[:10])
        self.assertEqual({i % runstore.PER_MS for i in ids[:10]}, set(range(a.block, a.block + 4)))

    def test_a_burst_moves_on_to_the_next_millisecond_never_far_ahead(self):
        minter = self.minter(size=2)
        with mock.patch.object(runstore, 'store_ms', lambda: 1000):
            ids = minter.mint(5000)
        self.assertEqual(ids[0] // runstore.PER_MS, 1000)
        self.assertEqual(ids[-1] // runstore.PER_MS, 1000 + runstore._MAX_LEAD_MS)
        self.assertEqual(len(ids), 2 * (runstore._MAX_LEAD_MS + 1))  # the rest are dropped, not stamped later

    def test_never_backwards_when_the_clock_is(self):
        minter = self.minter()
        with mock.patch.object(runstore, 'store_ms', lambda: 5000):
            first = minter.mint(1)[0]
        with mock.patch.object(runstore, 'store_ms', lambda: 4000):
            self.assertGreater(minter.mint(1)[0], first)


class MarkerTests(HomeTestCase):
    def mark(self, where):
        with db.transaction() as tx:
            tx.execute('INSERT INTO run_store (kind, location, first_id, since) VALUES (?, ?, ?, ?)',
                       ('clickhouse', where, 1, '2026-10-28 12:00:00'))

    def test_a_store_whose_runs_are_in_clickhouse_needs_the_setting(self):
        self.mark('ch.internal:8443/runs')
        with mock.patch.object(config, 'run_store_url', lambda: None), \
                self.assertRaisesRegex(ValueError, 'kept in ClickHouse at ch.internal:8443/runs'):
            runstore.check_marker()

    def test_every_instance_names_the_same_one(self):
        self.mark('ch.internal:8443/runs')
        with mock.patch.object(config, 'run_store_url', lambda: 'https://u:p@ch.other:8443/runs'), \
                self.assertRaisesRegex(ValueError, 'must name the same server and database'):
            runstore.check_marker()


@unittest.skipUnless(TEST_RUN_STORE_URL, 'set QUERYAPIGATE_TEST_RUN_STORE_URL to run')
class StoreTestCase(unittest.TestCase):
    env = {}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        data = os.path.join(self.tmp.name, 'data.db')
        conn = sqlite3.connect(data)
        conn.execute('CREATE TABLE t (id INTEGER)')
        conn.executemany('INSERT INTO t VALUES (?)', [(i,) for i in range(3)])
        conn.commit()
        conn.close()
        patcher = mock.patch.dict(os.environ, {
            'QUERYAPIGATE_HOME': self.tmp.name, 'QUERYAPIGATE_API_KEY': 'admin', 'QUERYAPIGATE_RATE_LIMIT': '',
            'QUERYAPIGATE_HISTORY_FLUSH_INTERVAL': '0', 'QUERYAPIGATE_HISTORY_SAMPLE_RATE': '',
            'QUERYAPIGATE_HISTORY_RETENTION_DAYS': '', 'QUERYAPIGATE_CORS_ORIGINS': '', **self.env})
        patcher.start()
        self.addCleanup(patcher.stop)
        write_connections({'lite': {'db': 'sqlite', 'database': data, 'active': True}})
        self.client = create_app().test_client()
        self.admin = {'X-API-Key': 'admin'}
        for name in ('q', 'other'):
            self.assertEqual(save_query(self.client, headers=self.admin, body={
                'author': 'a', 'description': 'd', 'filename': name, 'connection_name': 'lite',
                'sql_query': 'SELECT id FROM t'}).status_code, 201)

    def count(self, table, where='1', params=None):
        return runstore.query(f'SELECT count() AS n FROM {table} WHERE {where}', params)[0]['n']

    def run_query(self, name='q', headers=None):
        self.assertEqual(self.client.get(f'/q/{name}', headers=headers or self.admin).status_code, 200)


class WriteTests(StoreTestCase):
    def test_every_run_is_an_event_and_a_history_row(self):
        self.run_query()
        self.run_query('other')
        self.assertEqual(self.count('events'), 2)
        self.assertEqual(self.count('history'), 2)
        entry = json.loads(runstore.query("SELECT entry FROM history WHERE query_name = 'q'")[0]['entry'])
        self.assertEqual((entry['status'], entry['rows'], entry['key_name']), ('success', 3, 'admin'))
        self.assertEqual(self.count('run_minutes'), 2)

    def test_a_sampled_out_run_is_an_event_only(self):
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_HISTORY_SAMPLE_RATE': '0.000001'}):
            self.run_query()
        self.assertEqual((self.count('events'), self.count('history')), (1, 0))
        self.assertEqual(runstore.query('SELECT sum(runs) AS n FROM run_minutes')[0]['n'], 1)  # counted all the same

    def test_a_run_of_a_deleted_version_keeps_its_event_but_no_history_row(self):
        row = history._row('q', 1, {'executed_at': store.now(), 'status': 'success', 'key_name': 'admin'})
        self.assertEqual(self.client.delete('/api/v1/queries/q', headers=self.admin).status_code, 204)
        history.write([row])
        self.assertEqual((self.count('events', "query_name = 'q'"), self.count('history', "query_name = 'q'")),
                         (1, 0))

    def test_a_batch_goes_to_the_store_it_was_recorded_against(self):
        """The writer thread writes after the request - by then the process may be pointed at another home (as each test
        is): the batch still goes to its own metadata store's run store, checked against its own versions."""
        target = db.current_target()
        row = history._row('q', 1, {'executed_at': store.now(), 'status': 'success', 'key_name': 'admin'})
        failed = metrics.history_counts().get('failed', 0)
        elsewhere = tempfile.TemporaryDirectory()
        self.addCleanup(elsewhere.cleanup)
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': elsewhere.name}):  # a home with no store yet
            history.write([row], target)
        self.assertEqual(metrics.history_counts().get('failed', 0), failed)
        self.assertEqual((self.count('events'), self.count('history')), (1, 1))

    def test_an_id_above_2_to_the_53_is_refused(self):
        row = {'type': 'x', 'occurred_at': store.now(), 'workspace': 'default', 'in_history': False, 'rows': 0,
               'seconds': 0, 'entry': '', 'payload_json': ''}
        runstore.insert('events', [{**row, 'id': runstore.MAX_ID}])
        with self.assertRaisesRegex(runstore.RunStoreError, 'id_below_2_53'):
            runstore.insert('events', [{**row, 'id': runstore.MAX_ID + 1}])

    def test_an_ad_hoc_statement_is_kept_hashed_by_the_hour_and_as_text_only_as_long_as_the_event_log(self):
        history.write([history._row(None, None, {'executed_at': store.now(), 'transport': 'mcp', 'key_name': 'agent',
                                                 'status': 'success', 'sql': "SELECT 'a@b.example'"}, keep=False)])
        hashed = 'sha:' + hashlib.sha256("SELECT 'a@b.example'".encode()).hexdigest()  # as history.py hashes it
        self.assertEqual([row['statement'] for row in runstore.query('SELECT statement FROM run_hours')], [hashed])
        self.assertEqual([row['statement'] for row in runstore.query('SELECT statement FROM run_minutes')],
                         ["sql:SELECT 'a@b.example'"])
        ttl = runstore.query("SELECT engine_full FROM system.tables WHERE database = {db:String} AND "
                             "name = 'run_minutes'", {'db': runstore.database()})[0]['engine_full']
        self.assertIn(f'TTL minute + toIntervalHour({config.events_retention_hours()})', ttl)

    def test_ids_increase_with_every_batch(self):
        for _ in range(3):
            self.run_query()
        ids = [row['id'] for row in runstore.query('SELECT id FROM events ORDER BY id')]
        self.assertEqual(len(ids), 3)
        self.assertEqual(ids, sorted(set(ids)))
        self.assertTrue(all(runstore.id_floor(0) < i < 2 ** 53 for i in ids))


class ReadTests(StoreTestCase):
    def seed(self, count, start, name='q', key='admin', step=timedelta(minutes=1)):
        rows = [history._row(name, 1, {'executed_at': (start + step * i).strftime(history.TIME_FORMAT),
                                       'status': 'success', 'key_name': key, 'i': i}, event=True)
                for i in range(count)]
        history.write(rows)

    def test_history_pages_back_across_windows_newest_first(self):
        now = datetime.now().replace(microsecond=0)
        self.seed(10, now - timedelta(days=3), step=timedelta(hours=8))  # 3 days back to just now
        found, cursor = [], None
        while True:
            res = self.client.get('/api/v1/history', headers=self.admin,
                                  query_string={'limit': 3, **({'cursor': cursor} if cursor else {})}).get_json()
            found += [item['i'] for item in res['items']]
            cursor = res['next_cursor']
            if cursor is None:
                break
        self.assertEqual(found, list(range(9, -1, -1)))

    def test_filters_and_bounds(self):
        start = datetime(2026, 10, 1, 12, 0, 0)
        self.seed(5, start, key='alice')
        self.seed(5, start, name='other', key='bob')
        res = self.client.get('/api/v1/history', headers=self.admin, query_string={
            'key': 'alice', 'since': '2026-10-01 12:01:00', 'until': '2026-10-01 12:04:00'}).get_json()
        self.assertEqual([item['i'] for item in res['items']], [3, 2, 1])

    def test_the_console_lists_each_versions_newest(self):
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_HISTORY_LIMIT': '4'}):
            self.seed(6, datetime.now() - timedelta(days=2), step=timedelta(hours=6))
            listed = store.load_versions('q')['1']['execution_history']
        self.assertEqual([entry['i'] for entry in listed], [2, 3, 4, 5])  # oldest first, as the metadata store lists

    def test_forget_erases_a_caller_everywhere(self):
        self.seed(3, datetime.now() - timedelta(minutes=5), key='alice')
        self.seed(2, datetime.now() - timedelta(minutes=5), key='bob')
        dry = self.client.delete('/api/v1/history', headers=self.admin,
                                 query_string={'caller': 'alice', 'dry_run': 'true'}).get_json()
        self.assertEqual((dry['runs'], dry['events']), (3, 3))
        self.assertEqual(self.count('history', "key_name = 'alice'"), 3)
        done = self.client.delete('/api/v1/history', headers=self.admin, query_string={'caller': 'alice'}).get_json()
        self.assertEqual((done['runs'], done['events']), (3, 3))
        for table in ('events', 'history', 'history_counts', 'run_minutes', 'run_hours'):
            self.assertEqual(self.count(table, "key_name = 'alice'"), 0, table)
        self.assertEqual(self.count('history', "key_name = 'bob'"), 2)

    def test_forget_erases_a_run_that_lands_after_its_first_delete(self):
        """A data plane's batch, or another worker's queue, written while the erasure runs: it deletes again once
        they would have landed."""
        self.seed(2, datetime.now() - timedelta(minutes=5), key='alice')
        waited = []

        def landing(seconds):
            waited.append(seconds)
            self.seed(1, datetime.now(), key='alice')
        with mock.patch.object(runstore, 'time', mock.Mock(wraps=time, sleep=landing)):
            self.assertEqual(self.client.delete('/api/v1/history', headers=self.admin,
                                                query_string={'caller': 'alice'}).status_code, 200)
        self.assertEqual(waited, [runstore._in_flight_seconds()])
        for table in ('events', 'history', 'history_counts', 'run_minutes', 'run_hours'):
            self.assertEqual(self.count(table, "key_name = 'alice'"), 0, table)

    def test_a_cursor_this_server_never_returned_is_refused(self):
        for top, after in (('garbage', 1), ('2026-13-45 00:00:00', 1), ('2026-10-28 12:00:00', -1),
                           ('2026-10-28 12:00:00', 2 ** 64)):
            with self.subTest(top=top, after=after):
                res = self.client.get('/api/v1/history', headers=self.admin,
                                      query_string={'cursor': history._encode_cursor(top, after)})
                self.assertEqual((res.status_code, res.get_json()['code']), (400, 'invalid_filter'))

    def test_deleting_a_query_deletes_its_runs(self):
        self.seed(3, datetime.now() - timedelta(minutes=5))
        self.seed(2, datetime.now() - timedelta(minutes=5), name='other')
        self.assertEqual(self.client.delete('/api/v1/queries/q', headers=self.admin).status_code, 204)
        self.assertEqual((self.count('history', "query_name = 'q'"), self.count('history', "query_name = 'other'")),
                         (0, 2))


class UsageTests(StoreTestCase):
    def mcp_run(self, key, keep, **entry):
        row = history._row(None, None, {'executed_at': store.now(), 'transport': 'mcp', 'key_name': key,
                                        'status': 'success', 'rows': 2, 'connection_name': 'lite', **entry},
                           keep=keep)
        history.write([row])

    def test_every_run_counts_whatever_history_kept(self):
        for i in range(4):
            self.mcp_run('agent', keep=i == 0, sql=f'SELECT  {i % 2}')  # three of the four sampled out of history
        self.mcp_run('agent', keep=False, status='error', code='table_not_allowed', sql='SELECT secret')
        self.mcp_run('agent', keep=False, masked=[{'column': 'email'}], sql_sha256='ab' * 32)
        summary = usage.agents()
        self.assertEqual((summary['sample_rate'], summary['truncated']), (1.0, False))
        agent, = summary['items']
        self.assertEqual((agent['calls'], agent['rows'], agent['errors'], agent['masked_calls'], agent['adhoc_calls']),
                         (6, 12, 1, 1, 6))
        self.assertEqual(agent['refusals'], {'table_not_allowed': 1})
        self.assertEqual(agent['connections'], ['lite'])
        self.assertEqual([(s['sql'], s['count']) for s in agent['top_statements']][:2],
                         [('SELECT 0', 2), ('SELECT 1', 2)])  # whitespace normalised, as from history


    def test_whole_minutes_from_the_rollups_and_the_edges_from_events_add_up_exactly(self):
        base = datetime.now().replace(second=0, microsecond=0) - timedelta(minutes=10)
        for minutes, second, extra in ((0, 5, {}), (0, 50, {}), (2, 13, {'status': 'error', 'code': 'query_timeout'}),
                                       (3, 0, {'masked': [{'column': 'email'}]}), (5, 59, {}), (7, 31, {}),
                                       (8, 2, {'sql': 'SELECT 9'})):
            at = (base + timedelta(minutes=minutes, seconds=second)).strftime(history.TIME_FORMAT)
            row = history._row('q' if 'sql' not in extra else None, 1 if 'sql' not in extra else None,
                               {'executed_at': at, 'transport': 'mcp', 'key_name': 'agent', 'status': 'success',
                                'rows': 3, 'connection_name': 'lite', **extra})
            history.write([row])
        since = (base + timedelta(seconds=40)).strftime(history.TIME_FORMAT)  # mid-minute at both ends
        until = (base + timedelta(minutes=7, seconds=45)).strftime(history.TIME_FORMAT)
        mixed = runstore.agent_usage('default', since, until)
        with mock.patch.object(runstore, '_minute_bounds', lambda *_: None):
            counted = runstore.agent_usage('default', since, until)
        self.assertEqual(mixed, counted)
        agent, = mixed
        self.assertEqual((agent['calls'], agent['errors'], agent['masked_calls'], agent['refusals']),
                         (5, 1, 1, {'query_timeout': 1}))


class SwitchTests(StoreTestCase):
    def test_the_first_start_copies_the_event_log_with_its_ids(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': home.name}):
            db.init_schema()
            payload = json.dumps(events.run_payload('q', 1, {'executed_at': '2026-10-28 12:00:00', 'key_name': 'k',
                                                             'status': 'success', 'transport': 'rest'}))
            with db.transaction() as tx:
                tx.executemany(events.INSERT_WITH_ID, [
                    (7, events.RUN, 'rest', datetime.now().strftime(history.TIME_FORMAT), 'k', None, 'q', 'success',
                     payload, 'default')])
            runstore.check_marker()
            copied = runstore.query('SELECT id, workspace, key_name, type, query_name, version, connection_name, '
                                    'status, source, occurred_at, entry, payload_json FROM events')
            self.assertEqual([(row['id'], runstore.event_payload(row)) for row in copied], [(7, payload)])
            marker = db.connection().execute('SELECT kind, first_id FROM run_store').fetchone()
            self.assertEqual(marker[0], 'clickhouse')
            self.assertGreater(marker[1], 7)
            runstore.check_marker()  # the second start copies nothing again
            self.assertEqual(self.count('events'), 1)

    def test_history_is_copied_once_on_request(self):
        with db.transaction() as tx:
            tx.executemany('INSERT INTO execution_history (workspace, query_name, version, executed_at, entry_json, '
                           'status, key_name) VALUES (?, ?, ?, ?, ?, ?, ?)',
                           [('default', 'q', 1, f'2026-10-0{i} 12:00:00', json.dumps({'i': i}), 'success', 'k')
                            for i in range(1, 4)])
        self.assertEqual(runstore.copy_history(), 3)
        res = self.client.get('/api/v1/history', headers=self.admin, query_string={'key': 'k'}).get_json()
        self.assertEqual([item['i'] for item in res['items']], [3, 2, 1])
        with self.assertRaisesRegex(ValueError, 'already copied'):
            runstore.copy_history()

    def test_leaving_continues_the_event_ids_above_the_run_stores(self):
        self.run_query()
        newest = runstore.newest_event_id(2 ** 63 - 1)
        self.assertGreaterEqual(runstore.leave(), newest)
        self.assertIsNone(db.connection().execute('SELECT 1 FROM run_store').fetchone())
        with mock.patch.object(config, 'run_store_url', lambda: None):
            history.write([history._row('q', 1, {'executed_at': store.now(), 'status': 'success'})])
            self.assertGreater(db.connection().execute('SELECT MAX(id) FROM events').fetchone()[0], newest)

    def test_leaving_carries_no_id_from_ahead_of_the_clock(self):
        """An id no minter handed out - written with the run store's credentials - isn't where the metadata store's
        event log continues from."""
        self.run_query()
        newest = runstore.newest_event_id(runstore.MAX_ID)
        row = runstore.query(f'SELECT {runstore.EVENT_COLUMNS} FROM events WHERE id = {{id:UInt64}}', {'id': newest})[0]
        runstore.insert('events', [{**row, 'id': runstore.MAX_ID, 'in_history': False, 'rows': 0, 'seconds': 0.0}])
        left = runstore.leave()
        self.assertGreaterEqual(left, newest)
        self.assertLess(left, runstore.id_floor(runstore.store_ms() + 60_000))


class MigrationTests(StoreTestCase):
    def test_a_0_45_store_is_brought_to_version_2_once(self):
        """0.45's tables (schema 1): the hourly rollup kept statements' text, events had no id check, the minutes
        kept 7 days. The counts stay; only the text goes."""
        old_view = runstore._ROLLUP_VIEW.format(fn='toStartOfHour', grain='hour', key=runstore._ROLLUP_KEY,
                                                group=runstore._ROLLUP_KEY)
        for statement in ('ALTER TABLE run_hours_mv MODIFY QUERY ' + old_view,
                          'ALTER TABLE events DROP CONSTRAINT id_below_2_53',
                          'ALTER TABLE run_minutes MODIFY TTL minute + INTERVAL 7 DAY',
                          'TRUNCATE TABLE run_store_schema', 'INSERT INTO run_store_schema VALUES (1)'):
            runstore.execute(statement)
        for sql in ('SELECT 1', 'SELECT 1', 'SELECT 2'):
            history.write([history._row(None, None, {'executed_at': store.now(), 'transport': 'mcp',
                                                     'key_name': 'agent', 'status': 'success', 'sql': sql})])
        self.run_query()
        self.assertEqual(runstore.query("SELECT uniqExact(statement) AS n FROM run_hours "
                                        "WHERE startsWith(statement, 'sql:')")[0]['n'], 2)  # their text, as 0.45 kept

        def started():
            runstore._ready.discard((os.getpid(), runstore.database()))
            runstore.ensure_schema()
            return {row['statement']: row['n'] for row in runstore.query(
                'SELECT statement, sum(runs) AS n FROM run_hours GROUP BY statement')}
        sha = {sql: 'sha:' + hashlib.sha256(sql.encode()).hexdigest() for sql in ('SELECT 1', 'SELECT 2')}
        expected = {sha['SELECT 1']: 2, sha['SELECT 2']: 1, '': 1}
        self.assertEqual(started(), expected)
        self.assertEqual(runstore._schema_version(runstore.database()), 2)
        with self.assertRaisesRegex(runstore.RunStoreError, 'id_below_2_53'):
            runstore.insert('events', [{'id': runstore.MAX_ID + 1, 'type': 'x', 'occurred_at': store.now(),
                                        'workspace': 'default', 'in_history': False, 'rows': 0, 'seconds': 0,
                                        'entry': '', 'payload_json': ''}])
        history.write([history._row(None, None, {'executed_at': store.now(), 'transport': 'mcp', 'key_name': 'agent',
                                                 'status': 'success', 'sql': 'SELECT 2'})])
        self.assertEqual(started(), {**expected, sha['SELECT 2']: 2})  # hashed by the view now; nothing counted twice


class LeaseEndpointTests(StoreTestCase):
    env = {'QUERYAPIGATE_DATAPLANE_SECRET': SECRET}

    def lease(self, **body):
        return self.client.post('/internal/dataplane/lease', json={'holder': 'dp', **body},
                                headers={dataplane.SECRET_HEADER: SECRET, dataplane.VERSION_HEADER: __version__})

    def test_a_data_plane_from_another_release_gets_none(self):
        res = self.client.post('/internal/dataplane/lease', json={'holder': 'dp'},
                               headers={dataplane.SECRET_HEADER: SECRET, dataplane.VERSION_HEADER: '0.1.0'})
        self.assertEqual((res.status_code, res.get_json()['error']), (409, dataplane.UNPAIRED))

    def test_names_where_runs_are_and_takes_the_block_back(self):
        res = self.lease()
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()['location'], runstore.location())
        block = res.get_json()['block']
        self.assertEqual(self.lease(block=block, release=True).status_code, 204)
        self.assertFalse(runstore.renew(block, 'dp'))  # given back, not lapsing a minute later

    def test_every_block_taken_is_a_503_that_says_so(self):
        with mock.patch.object(runstore, 'lease', side_effect=runstore.LeaseError('a message of its own')):
            res = self.lease()
        self.assertEqual(res.status_code, 503)
        self.assertEqual(res.get_json()['error'], runstore.LEASES_TAKEN)


class EventServerTests(StoreTestCase):
    def setUp(self):
        super().setUp()
        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever, daemon=True)
        thread.start()
        self.server = events.EventServer('127.0.0.1', 0, poll_interval=0.05)
        asyncio.run_coroutine_threadsafe(self.server.start(), loop).result(10)

        def stop():
            asyncio.run_coroutine_threadsafe(self.server.stop(), loop).result(10)
            loop.call_soon_threadsafe(loop.stop)
            thread.join(10)
        self.addCleanup(stop)

    def stream(self, last_id=None):
        headers = {'X-API-Key': 'admin', **({'Last-Event-ID': str(last_id)} if last_id is not None else {})}
        stream = Stream(self.server.port, headers=headers)
        self.addCleanup(stream.close)
        self.assertEqual(stream.status, 200)
        return stream

    def test_runs_arrive_once_settled_in_id_order(self):
        stream = self.stream()
        self.run_query()
        self.run_query('other')
        started = time.monotonic()
        found = stream.events(2, timeout=10)
        self.assertGreaterEqual(time.monotonic() - started, runstore.SETTLE_SECONDS * 0.8)
        self.assertEqual([payload['query'] for _, payload in found], ['q', 'other'])
        self.assertLess(found[0][0], found[1][0])

    def test_a_resume_misses_nothing_and_repeats_nothing(self):
        stream = self.stream()
        self.run_query()
        first = stream.events(1, timeout=10)[0][0]
        stream.close()
        for name in ('q', 'other', 'q'):
            self.run_query(name)
        again = self.stream(last_id=first)
        found = again.events(3, timeout=10)
        self.assertEqual([payload['query'] for _, payload in found], ['q', 'other', 'q'])
        self.assertTrue(all(event_id > first for event_id, _ in found))
        self.assertTrue(again.nothing_more(wait=1.5))

    def test_a_long_resume_finds_its_ids_in_batches_and_misses_nothing(self):
        stream = self.stream()
        self.run_query()
        first = stream.events(1, timeout=10)[0][0]
        stream.close()
        for i in range(7):
            self.run_query('q' if i % 2 else 'other')
        time.sleep(runstore.SETTLE_SECONDS + 0.5)  # past the settle window: the resume, not live delivery, has them
        with mock.patch.object(runstore, 'REPLAY_IDS', 4), mock.patch.object(events, '_REPLAY_PAGE', 2), \
                mock.patch.object(runstore, 'replay_ids', wraps=runstore.replay_ids) as reads:
            again = self.stream(last_id=first)
            found = again.events(7, timeout=10)
            self.assertTrue(again.nothing_more(wait=1.5))
        ids = [event_id for event_id, _ in found]
        self.assertEqual(ids, sorted(set(ids)))
        self.assertTrue(all(event_id > first for event_id in ids))
        self.assertEqual(reads.call_count, 2)  # 4 ids, then the 3 left: two reads for four pages

    def test_a_late_event_is_still_delivered_live(self):
        stream = self.stream()
        self.run_query()
        delivered = stream.events(1, timeout=10)[0][0]
        late = delivered - runstore.PER_MS  # a millisecond older than one already delivered: inserted only now
        row = runstore.query(f'SELECT {runstore.EVENT_COLUMNS} FROM events WHERE id = {{id:UInt64}}',
                             {'id': delivered})[0]
        row.update(id=late, in_history=False, rows=0, seconds=0.0)
        runstore.insert('events', [{k: v for k, v in row.items()}])
        self.assertEqual(stream.events(1, timeout=5)[0][0], late)


if __name__ == '__main__':
    unittest.main()

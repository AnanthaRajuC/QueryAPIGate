"""Tests for structured logging, request IDs, the slow-query log and /metrics."""
import hashlib
import io
import json
import logging
import os
import re
import sqlite3
import tempfile
import unittest
import uuid
from unittest import mock

from flask import g

from queryapigate import apikeys, create_app, logging_setup, metrics
from queryapigate import app as app_module
from tests.helpers import write_connections


class AppTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        tmp = self.tmp.name
        self.db_path = os.path.join(tmp, 'test.db')
        conn = sqlite3.connect(self.db_path)
        conn.execute('CREATE TABLE t (id INTEGER)')
        conn.execute('INSERT INTO t VALUES (1)')
        conn.commit()
        conn.close()
        patcher = mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': tmp})
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ('QUERYAPIGATE_API_KEY', 'QUERYAPIGATE_JSON_LOGS', 'QUERYAPIGATE_SLOW_QUERY_THRESHOLD',
                     'QUERYAPIGATE_RATE_LIMIT'):
            os.environ.pop(name, None)
        write_connections({'lite': {'db': 'sqlite', 'database': self.db_path, 'active': True}})
        self.client = create_app().test_client()

    def run_sql(self, sql='SELECT * FROM t'):
        return self.client.post('/execute_sql', json={'sql': sql, 'connection_name': 'lite'})


class RequestIdTests(AppTestCase):
    def test_every_response_carries_a_request_id(self):
        self.assertRegex(self.client.get('/health').headers['X-Request-Id'], r'^[0-9a-f]{12}$')

    def test_request_ids_are_unique_per_request(self):
        first = self.client.get('/health').headers['X-Request-Id']
        second = self.client.get('/health').headers['X-Request-Id']
        self.assertNotEqual(first, second)

    def test_a_rejected_request_still_gets_an_id(self):
        os.environ['QUERYAPIGATE_API_KEY'] = 'k3y'
        res = self.client.get('/connections')
        self.assertEqual(res.status_code, 401)
        self.assertRegex(res.headers['X-Request-Id'], r'^[0-9a-f]{12}$')


class SuppliedRequestIdTests(AppTestCase):
    """A caller may bring its own X-Request-Id, but only in a shape that is safe to put in a log line."""

    def test_a_valid_supplied_id_is_used_everywhere(self):
        for supplied in ('trace-42', 'a' * 64, '3f2b8c1e-9d4a-4e7b-8a10-6c5d2e9f1a77', 'svc.job_7:run-1', 'X'):
            res = self.client.get('/health', headers={'X-Request-Id': supplied})
            self.assertEqual(res.headers['X-Request-Id'], supplied)

    def test_an_invalid_supplied_id_is_ignored_not_rejected(self):
        for supplied in ('a' * 65, 'has space', 'quo"te', 'semi;colon', 'ünï', 'a/b', '<x>', '%0a', ''):
            res = self.client.get('/health', headers={'X-Request-Id': supplied})
            self.assertEqual(res.status_code, 200, supplied)
            self.assertRegex(res.headers['X-Request-Id'], r'^[0-9a-f]{12}$', supplied)

    def test_a_trailing_newline_is_not_accepted(self):
        # `$` in a regex would let this through; a newline in a log line is how one is forged.
        for supplied in ('abc\n', 'abc\r\nX-Injected: 1', '\nabc'):
            self.assertRegex(app_module.new_request_id(supplied), r'^[0-9a-f]{12}$')

    def test_supplied_ids_are_not_deduplicated(self):
        first = self.client.get('/health', headers={'X-Request-Id': 'same'}).headers['X-Request-Id']
        second = self.client.get('/health', headers={'X-Request-Id': 'same'}).headers['X-Request-Id']
        self.assertEqual(first, second)

    def test_it_reaches_a_saved_querys_history(self):
        self.client.patch('/save_sql_to_file', json={'filename': 'q', 'author': 'a', 'description': 'd',
                                                     'sql_query': 'SELECT * FROM t', 'connection_name': 'lite'})
        res = self.client.get('/q/q', headers={'X-Request-Id': 'caller-trace-9'})
        self.assertEqual(res.status_code, 200)
        files = self.client.get('/list_files').get_json()['files']
        history = files[0]['versions'][0]['execution_history']
        self.assertEqual(history[-1]['request_id'], 'caller-trace-9')

    def test_it_is_the_id_the_log_filter_stamps_on_log_lines(self):
        # RequestContextFilterTests proves the filter copies g.request_id onto every record; this proves the
        # supplied ID is what ends up in g.request_id.
        with self.client:
            self.client.get('/health', headers={'X-Request-Id': 'trace-log-1'})
            self.assertEqual(g.request_id, 'trace-log-1')
            record = logging.LogRecord('queryapigate', logging.INFO, __file__, 1, 'msg', None, None)
            logging_setup._RequestContextFilter().filter(record)
            self.assertEqual(record.request_id, 'trace-log-1')

    def test_a_rejected_request_keeps_the_supplied_id(self):
        os.environ['QUERYAPIGATE_API_KEY'] = 'k3y'
        res = self.client.get('/connections', headers={'X-Request-Id': 'trace-401'})
        self.assertEqual((res.status_code, res.headers['X-Request-Id']), (401, 'trace-401'))

    def test_it_is_never_an_identity(self):
        os.environ['QUERYAPIGATE_API_KEY'] = 'k3y'
        res = self.client.get('/connections', headers={'X-Request-Id': 'k3y'})  # even a key-shaped value
        self.assertEqual(res.status_code, 401)


class RequestContextFilterTests(unittest.TestCase):
    def test_uses_g_request_id_inside_a_request_context(self):
        record = logging.LogRecord('queryapigate', logging.INFO, __file__, 1, 'msg', None, None)
        with create_app().test_request_context('/health'):
            g.request_id = 'abc123'
            self.assertTrue(logging_setup._RequestContextFilter().filter(record))
        self.assertEqual(record.request_id, 'abc123')

    def test_dash_outside_a_request_context(self):
        record = logging.LogRecord('queryapigate', logging.INFO, __file__, 1, 'msg', None, None)
        self.assertTrue(logging_setup._RequestContextFilter().filter(record))
        self.assertEqual(record.request_id, '-')
        self.assertEqual(record.key, '-')

    def test_uses_the_resolved_permissions_name(self):
        record = logging.LogRecord('queryapigate', logging.INFO, __file__, 1, 'msg', None, None)
        with create_app().test_request_context('/health'):
            g.request_id = 'abc123'
            g.permission = apikeys.Permission(name='reporting', admin=False, connections='*', allow_writes=False,
                                              queries=[], rate_limit=None, allowed_write_ops=None)
            logging_setup._RequestContextFilter().filter(record)
        self.assertEqual(record.key, 'reporting')

    def test_dash_when_permission_is_not_yet_resolved(self):
        record = logging.LogRecord('queryapigate', logging.INFO, __file__, 1, 'msg', None, None)
        with create_app().test_request_context('/health'):
            g.request_id = 'abc123'  # no g.permission set yet, e.g. a CORS preflight or a 429
            logging_setup._RequestContextFilter().filter(record)
        self.assertEqual(record.key, '-')


class LoggingFormatTests(unittest.TestCase):
    def _handler_for(self, logger_name, json_logs):
        logger = logging.getLogger(logger_name)
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_JSON_LOGS': '1' if json_logs else ''}):
            logging_setup.configure(logger)
        return logger, next(h for h in logger.handlers if getattr(h, '_queryapigate_managed', False))

    def test_plain_text_by_default(self):
        _, handler = self._handler_for('queryapigate_test_plain', json_logs=False)
        self.assertNotIsInstance(handler.formatter, logging_setup._JsonFormatter)

    def test_json_when_enabled(self):
        logger, handler = self._handler_for('queryapigate_test_json', json_logs=True)
        self.assertIsInstance(handler.formatter, logging_setup._JsonFormatter)
        buf = io.StringIO()
        handler.stream = buf
        logger.warning('boom')
        payload = json.loads(buf.getvalue().strip())
        self.assertEqual(payload['level'], 'WARNING')
        self.assertEqual(payload['message'], 'boom')
        self.assertEqual(payload['request_id'], '-')

    def test_configure_replaces_only_its_own_handler(self):
        logger = logging.getLogger('queryapigate_test_idempotent')
        other = logging.NullHandler()
        logger.addHandler(other)
        logging_setup.configure(logger)
        logging_setup.configure(logger)
        managed = [h for h in logger.handlers if getattr(h, '_queryapigate_managed', False)]
        self.assertEqual(len(managed), 1)
        self.assertIn(other, logger.handlers)

    def test_extra_fields_become_top_level_json_keys(self):
        logger, handler = self._handler_for('queryapigate_test_extra', json_logs=True)
        buf = io.StringIO()
        handler.stream = buf
        logger.info('Executing on %s', 'lite', extra={'connection': 'lite', 'duration_ms': 12.5})
        payload = json.loads(buf.getvalue().strip())
        self.assertEqual(payload['connection'], 'lite')
        self.assertEqual(payload['duration_ms'], 12.5)
        self.assertEqual(payload['message'], 'Executing on lite')

    def test_a_call_with_no_extra_has_no_stray_fields(self):
        logger, handler = self._handler_for('queryapigate_test_no_extra', json_logs=True)
        buf = io.StringIO()
        handler.stream = buf
        logger.info('plain message')
        payload = json.loads(buf.getvalue().strip())
        self.assertEqual(set(payload), {'time', 'level', 'logger', 'request_id', 'key', 'message'})

    def test_extra_cannot_override_request_id_or_key(self):
        # request_id/key come from _RequestContextFilter, not a caller's extra={} - even if a call site
        # tried to pass one of those names, the filter's value (set via the same attribute names) is what
        # a real request actually produces; this just confirms the formatter doesn't double them up oddly.
        logger, handler = self._handler_for('queryapigate_test_extra_collision', json_logs=True)
        buf = io.StringIO()
        handler.stream = buf
        logger.info('msg')
        payload = json.loads(buf.getvalue().strip())
        self.assertEqual(payload['request_id'], '-')
        self.assertEqual(payload['key'], '-')


class JsonLogFieldsTests(unittest.TestCase):
    """Integration-level: the real log.info()/log.warning() call sites in engine.py and app.py actually
    carry the extra={} fields LoggingFormatTests above proves _JsonFormatter turns into JSON keys."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        tmp = self.tmp.name
        self.db_path = os.path.join(tmp, 'test.db')
        conn = sqlite3.connect(self.db_path)
        conn.execute('CREATE TABLE t (id INTEGER)')
        conn.execute('INSERT INTO t VALUES (1)')
        conn.commit()
        conn.close()
        patcher = mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': tmp, 'QUERYAPIGATE_JSON_LOGS': '1',
                                               'QUERYAPIGATE_SLOW_QUERY_THRESHOLD': '0'})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop('QUERYAPIGATE_API_KEY', None)
        os.environ.pop('QUERYAPIGATE_RATE_LIMIT', None)
        write_connections({'lite': {'db': 'sqlite', 'database': self.db_path, 'active': True}})
        self.client = create_app().test_client()
        self.buf = io.StringIO()
        logging.getLogger('queryapigate').handlers[0].stream = self.buf

    def lines(self):
        return [json.loads(line) for line in self.buf.getvalue().splitlines()]

    def find(self, substring):
        return next(p for p in self.lines() if substring in p.get('message', ''))

    def test_execute_sql_line_carries_connection_and_dialect(self):
        self.client.post('/execute_sql', json={'sql': 'SELECT * FROM t', 'connection_name': 'lite'})
        payload = self.find('Executing on')
        self.assertEqual(payload['connection'], 'lite')
        self.assertEqual(payload['dialect'], 'sqlite')
        self.assertIn('limit', payload)
        self.assertIn('offset', payload)
        self.assertIn('timeout', payload)

    def test_streaming_line_carries_connection_and_dialect(self):
        res = self.client.post('/execute_sql?stream=true&format=csv',
                               json={'sql': 'SELECT * FROM t', 'connection_name': 'lite'})
        res.get_data()  # the streamed body is a lazy generator - drain it, same as any other streaming test
        payload = self.find('Streaming from')
        self.assertEqual(payload['connection'], 'lite')
        self.assertEqual(payload['dialect'], 'sqlite')

    def test_slow_query_line_carries_connection_dialect_and_duration(self):
        os.environ['QUERYAPIGATE_SLOW_QUERY_THRESHOLD'] = '0.000001'
        self.client.post('/execute_sql', json={'sql': 'SELECT * FROM t', 'connection_name': 'lite'})
        payload = self.find('Slow query on')
        self.assertEqual(payload['connection'], 'lite')
        self.assertEqual(payload['dialect'], 'sqlite')
        self.assertIn('duration_ms', payload)

    def test_access_log_line_carries_method_path_status_and_duration(self):
        self.client.post('/execute_sql', json={'sql': 'SELECT * FROM t', 'connection_name': 'lite'})
        payload = self.find('/execute_sql ->')
        self.assertEqual(payload['method'], 'POST')
        self.assertEqual(payload['path'], '/execute_sql')
        self.assertEqual(payload['status'], 200)
        self.assertIn('duration_ms', payload)

    def test_execute_sql_line_carries_a_full_sha256_sql_hash(self):
        sql = 'SELECT * FROM t'
        self.client.post('/execute_sql', json={'sql': sql, 'connection_name': 'lite'})
        payload = self.find('Executing on')
        self.assertEqual(payload['sql_hash'], hashlib.sha256(sql.encode('utf-8')).hexdigest())

    def test_streaming_line_carries_a_sql_hash(self):
        sql = 'SELECT * FROM t'
        res = self.client.post('/execute_sql?stream=true&format=csv', json={'sql': sql, 'connection_name': 'lite'})
        res.get_data()
        payload = self.find('Streaming from')
        self.assertEqual(payload['sql_hash'], hashlib.sha256(sql.encode('utf-8')).hexdigest())

    def test_a_paged_response_gets_the_same_sql_hash_for_the_same_query(self):
        self.client.post('/execute_sql', json={'sql': 'SELECT * FROM t', 'connection_name': 'lite'})
        self.client.post('/execute_sql', json={'sql': 'SELECT id FROM t', 'connection_name': 'lite'})
        lines = [p for p in self.lines() if p.get('message', '').startswith('Executing on')]
        self.assertEqual(lines[0]['sql_hash'], lines[0]['sql_hash'])
        self.assertNotEqual(lines[0]['sql_hash'], lines[1]['sql_hash'])

    def test_access_log_line_carries_serialization_ms_for_a_paged_response(self):
        self.client.post('/execute_sql', json={'sql': 'SELECT * FROM t', 'connection_name': 'lite'})
        payload = self.find('/execute_sql ->')
        self.assertIn('serialization_ms', payload)

    def test_access_log_line_has_no_serialization_ms_for_a_streamed_response(self):
        res = self.client.post('/execute_sql?stream=true&format=csv',
                               json={'sql': 'SELECT * FROM t', 'connection_name': 'lite'})
        res.get_data()
        payload = self.find('/execute_sql ->')
        self.assertNotIn('serialization_ms', payload)


class SlowQueryLogTests(AppTestCase):
    def test_logs_a_warning_when_a_query_exceeds_the_threshold(self):
        os.environ['QUERYAPIGATE_SLOW_QUERY_THRESHOLD'] = '0.000001'
        with mock.patch('queryapigate.engine.log.warning') as warning:
            self.run_sql()
        self.assertTrue(warning.called)
        self.assertEqual(warning.call_args[0][0], 'Slow query on %s (%s): %.1fms - %s')
        self.assertEqual(warning.call_args[0][1], 'lite')

    def test_no_warning_under_the_threshold(self):
        os.environ['QUERYAPIGATE_SLOW_QUERY_THRESHOLD'] = '60'
        with mock.patch('queryapigate.engine.log.warning') as warning:
            self.run_sql()
        warning.assert_not_called()

    def test_zero_disables_it_even_for_a_slow_query(self):
        os.environ['QUERYAPIGATE_SLOW_QUERY_THRESHOLD'] = '0'
        with mock.patch('queryapigate.engine.log.warning') as warning:
            self.run_sql()
        warning.assert_not_called()


class MetricsEndpointTests(AppTestCase):
    def test_public_even_with_an_api_key_set(self):
        os.environ['QUERYAPIGATE_API_KEY'] = 'k3y'
        self.assertEqual(self.client.get('/metrics').status_code, 200)

    def test_request_and_query_metrics_appear_after_use(self):
        self.run_sql()
        body = self.client.get('/metrics').get_data(as_text=True)
        self.assertIn(
            'queryapigate_requests_total{method="POST",endpoint="api.execute_sql_endpoint",status="200",key="-"}', body)
        self.assertIn('queryapigate_queries_total{connection="lite",dialect="sqlite",status="success",key="-"}', body)
        self.assertIn('queryapigate_request_duration_seconds_bucket', body)
        self.assertIn('queryapigate_query_duration_seconds_bucket', body)
        self.assertIn('queryapigate_pool_idle_connections', body)
        self.assertIn('queryapigate_rate_limit_rejections_total', body)

    def test_a_failed_query_is_counted_as_an_error(self):
        self.run_sql('SELECT this is not sql')
        body = self.client.get('/metrics').get_data(as_text=True)
        self.assertIn('queryapigate_queries_total{connection="lite",dialect="sqlite",status="error",key="-"}', body)

    def test_the_calling_key_is_tagged_on_the_counters(self):
        os.environ['QUERYAPIGATE_API_KEY'] = 'k3y'
        self.client.post('/execute_sql', json={'sql': 'SELECT * FROM t', 'connection_name': 'lite'},
                         headers={'X-API-Key': 'k3y'})
        body = self.client.get('/metrics', headers={'X-API-Key': 'k3y'}).get_data(as_text=True)
        self.assertIn(
            'queryapigate_queries_total{connection="lite",dialect="sqlite",status="success",key="admin"}', body)
        self.assertIn(
            'queryapigate_requests_total{method="POST",endpoint="api.execute_sql_endpoint",status="200",key="admin"}',
            body)

    def test_rate_limit_rejections_are_counted(self):
        before = self._rejections()
        os.environ['QUERYAPIGATE_RATE_LIMIT'] = '1/minute'
        client = create_app().test_client()
        client.get('/connections')  # consumes the one allowed request
        client.get('/connections')  # rejected
        self.assertEqual(self._rejections() - before, 1)

    def _rejections(self):
        body = self.client.get('/metrics').get_data(as_text=True)
        return int(re.search(r'queryapigate_rate_limit_rejections_total (\d+)', body).group(1))

    def test_cache_hit_and_miss_are_counted(self):
        self.client.patch('/save_sql_to_file', json={'author': 'a', 'description': 'd', 'filename': 'cached',
                                                      'connection_name': 'lite', 'sql_query': 'SELECT * FROM t',
                                                      'cache_ttl': 60})
        before_hits, before_misses = self._cache_counts()
        self.client.get('/q/cached')  # miss
        self.client.get('/q/cached')  # hit
        hits, misses = self._cache_counts()
        self.assertEqual(hits - before_hits, 1)
        self.assertEqual(misses - before_misses, 1)

    def test_a_request_with_no_x_cache_header_touches_neither_counter(self):
        before = self._cache_counts()
        self.run_sql()  # ad-hoc /execute_sql never carries X-Cache
        self.assertEqual(self._cache_counts(), before)

    def _cache_counts(self):
        body = self.client.get('/metrics').get_data(as_text=True)
        hits = int(re.search(r'queryapigate_cache_hits_total (\d+)', body).group(1))
        misses = int(re.search(r'queryapigate_cache_misses_total (\d+)', body).group(1))
        return hits, misses

    def _rows_returned(self):
        body = self.client.get('/metrics').get_data(as_text=True)
        match = re.search(r'queryapigate_rows_returned_total\{connection="lite",dialect="sqlite",key="-"\} (\d+)', body)
        return int(match.group(1)) if match else 0

    def test_rows_returned_reflects_a_paged_query(self):
        before = self._rows_returned()
        self.run_sql()  # AppTestCase's table t has exactly one row
        self.assertEqual(self._rows_returned() - before, 1)

    def test_rows_returned_reflects_a_streaming_export(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute('INSERT INTO t VALUES (2), (3)')
        conn.commit()
        conn.close()
        before = self._rows_returned()
        res = self.client.post('/execute_sql?stream=true&format=csv',
                               json={'sql': 'SELECT * FROM t', 'connection_name': 'lite'})
        self.assertEqual(res.status_code, 200)
        res.get_data()  # drains the generator, where the streaming row count is recorded
        self.assertEqual(self._rows_returned() - before, 3)

    def test_active_queries_returns_to_zero_once_requests_complete(self):
        self.run_sql()
        self.run_sql('SELECT this is not sql')
        body = self.client.get('/metrics').get_data(as_text=True)
        self.assertIn('queryapigate_active_queries 0', body)

    def test_a_truncated_stream_is_counted_separately_from_a_full_success(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute('INSERT INTO t VALUES (2), (3), (4)')
        conn.commit()
        conn.close()
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_STREAM_MAX_ROWS': '2'}):
            res = self.client.post('/execute_sql?stream=true&format=csv',
                                   json={'sql': 'SELECT * FROM t', 'connection_name': 'lite'})
            res.get_data()
        body = self.client.get('/metrics').get_data(as_text=True)
        self.assertIn(
            'queryapigate_stream_exports_total{connection="lite",dialect="sqlite",status="truncated",key="-"}', body)

    def test_serialization_metric_appears_after_a_paged_response(self):
        self.run_sql()
        body = self.client.get('/metrics').get_data(as_text=True)
        self.assertIn('queryapigate_serialization_duration_seconds_bucket{format="json"', body)
        self.assertIn('queryapigate_serialization_duration_seconds_count{format="json"}', body)

    def test_connections_list_carries_each_connections_live_usage(self):
        self.run_sql()
        self.run_sql('SELECT this is not sql')
        conns = self.client.get('/connections').get_json()['connections']
        usage = conns['lite']['usage']
        self.assertGreaterEqual(usage['queries'], 2)
        self.assertGreaterEqual(usage['errors'], 1)
        self.assertIsInstance(usage['avg_duration_ms'], float)

    def test_api_keys_list_carries_each_keys_live_usage(self):
        os.environ['QUERYAPIGATE_API_KEY'] = 'k3y'
        created = self.client.post('/api_keys', json={'name': 'scoped', 'connections': ['lite']},
                                   headers={'X-API-Key': 'k3y'}).get_json()
        secret = created['key']
        self.client.post('/execute_sql', json={'sql': 'SELECT * FROM t', 'connection_name': 'lite'},
                         headers={'X-API-Key': secret})
        self.client.post('/execute_sql', json={'sql': 'SELECT this is not sql', 'connection_name': 'lite'},
                         headers={'X-API-Key': secret})
        keys = self.client.get('/api_keys', headers={'X-API-Key': 'k3y'}).get_json()['keys']
        self.assertEqual(keys['scoped']['usage'], {'queries': 2, 'errors': 1, 'rows': 1})

    def test_serialization_metric_is_not_recorded_for_a_streamed_response(self):
        # Other tests elsewhere may legitimately produce format="csv" via a *paged* csv response, so this
        # checks the count doesn't move for this call specifically, not that the label never appears at all.
        before = self._serialization_count('csv')
        res = self.client.post('/execute_sql?stream=true&format=csv',
                               json={'sql': 'SELECT * FROM t', 'connection_name': 'lite'})
        res.get_data()
        self.assertEqual(self._serialization_count('csv'), before)

    def _serialization_count(self, fmt):
        body = self.client.get('/metrics').get_data(as_text=True)
        match = re.search(r'queryapigate_serialization_duration_seconds_count\{format="' + fmt + r'"\} (\d+)', body)
        return int(match.group(1)) if match else 0


class MetricsRenderTests(unittest.TestCase):
    def test_histogram_buckets_are_cumulative(self):
        endpoint = 'unit_test_histogram_' + uuid.uuid4().hex
        metrics.observe_request('GET', endpoint, '200', 0.02)
        metrics.observe_request('GET', endpoint, '200', 0.2)
        metrics.observe_request('GET', endpoint, '200', 2.0)
        body = metrics.render()
        buckets = {}
        for line in body.splitlines():
            if endpoint in line and '_bucket' in line:
                le = re.search(r'le="([^"]+)"', line).group(1)
                buckets[le] = int(line.rsplit(' ', 1)[1])
        self.assertEqual(buckets['0.025'], 1)
        self.assertEqual(buckets['0.25'], 2)
        self.assertEqual(buckets['+Inf'], 3)
        self.assertIn(f'queryapigate_requests_total{{method="GET",endpoint="{endpoint}",status="200",key="-"}} 3', body)

    def test_pool_occupancy_reflects_the_shared_pool(self):
        fake_pool = mock.MagicMock()
        fake_pool.idle_count.return_value = 7
        with mock.patch('queryapigate.metrics.pool.get_pool', return_value=fake_pool):
            self.assertIn('queryapigate_pool_idle_connections 7', metrics.render())

    def test_pool_occupancy_is_zero_when_pooling_is_disabled(self):
        with mock.patch('queryapigate.metrics.pool.get_pool', return_value=None):
            self.assertIn('queryapigate_pool_idle_connections 0', metrics.render())

    def test_cache_entries_reflects_a_live_cache_instance(self):
        from queryapigate import cache
        live = cache.ResponseCache()
        live.set(cache.ResponseCache.key(name='x'), b'body', 'text/plain', [], ttl=60)
        self.assertIn('queryapigate_cache_entries 1', metrics.render(live))

    def test_cache_entries_is_zero_without_a_cache_instance(self):
        self.assertIn('queryapigate_cache_entries 0', metrics.render(None))
        self.assertIn('queryapigate_cache_entries 0', metrics.render())

    def test_active_queries_gauge_tracks_inc_and_dec(self):
        before = int(re.search(r'queryapigate_active_queries (-?\d+)', metrics.render()).group(1))
        metrics.inc_active_query()
        metrics.inc_active_query()
        self.assertIn(f'queryapigate_active_queries {before + 2}', metrics.render())
        metrics.dec_active_query()
        self.assertIn(f'queryapigate_active_queries {before + 1}', metrics.render())
        metrics.dec_active_query()
        self.assertIn(f'queryapigate_active_queries {before}', metrics.render())

    def test_rows_returned_accumulates_across_calls(self):
        label = 'unit_test_rows_' + uuid.uuid4().hex
        metrics.observe_rows(label, 'sqlite', '-', 5)
        metrics.observe_rows(label, 'sqlite', '-', 2)
        self.assertIn(f'queryapigate_rows_returned_total{{connection="{label}",dialect="sqlite",key="-"}} 7',
                      metrics.render())

    def test_serialization_histogram_buckets_by_format(self):
        fmt = 'unit_test_format_' + uuid.uuid4().hex
        metrics.observe_serialization(fmt, 0.02)
        metrics.observe_serialization(fmt, 0.2)
        body = metrics.render()
        buckets = {}
        for line in body.splitlines():
            if fmt in line and '_bucket' in line:
                le = re.search(r'le="([^"]+)"', line).group(1)
                buckets[le] = int(line.rsplit(' ', 1)[1])
        self.assertEqual(buckets['0.025'], 1)
        self.assertEqual(buckets['0.25'], 2)
        self.assertIn(f'queryapigate_serialization_duration_seconds_count{{format="{fmt}"}} 2', body)


class UsageSummaryTests(unittest.TestCase):
    """metrics.summary_for_key()/summary_for_connection() - the aggregation #32's admin-UI usage columns
    are built on, using unique connection/key names per test so these never see another test's counters."""

    def test_summary_for_key_counts_queries_errors_and_rows_across_connections(self):
        key = 'unit_test_key_' + uuid.uuid4().hex
        conn_a = 'unit_test_conn_a_' + uuid.uuid4().hex
        conn_b = 'unit_test_conn_b_' + uuid.uuid4().hex
        metrics.observe_query(conn_a, 'sqlite', 'success', 0.01, key)
        metrics.observe_query(conn_a, 'sqlite', 'success', 0.02, key)
        metrics.observe_query(conn_b, 'postgres', 'error', 0.03, key)
        metrics.observe_query(conn_a, 'sqlite', 'success', 0.01, 'a-different-key')  # must not be counted
        metrics.observe_rows(conn_a, 'sqlite', key, 4)
        metrics.observe_rows(conn_b, 'postgres', key, 0)
        summary = metrics.summary_for_key(key)
        self.assertEqual(summary, {'queries': 3, 'errors': 1, 'rows': 4})

    def test_summary_for_key_with_no_activity_is_all_zero(self):
        key = 'unit_test_key_unused_' + uuid.uuid4().hex
        self.assertEqual(metrics.summary_for_key(key), {'queries': 0, 'errors': 0, 'rows': 0})

    def test_summary_for_connection_counts_across_keys_and_averages_latency(self):
        conn = 'unit_test_conn_' + uuid.uuid4().hex
        metrics.observe_query(conn, 'sqlite', 'success', 0.010, 'key-a')
        metrics.observe_query(conn, 'sqlite', 'success', 0.020, 'key-b')
        metrics.observe_query(conn, 'sqlite', 'error', 0.030, 'key-a')
        metrics.observe_rows(conn, 'sqlite', 'key-a', 10)
        metrics.observe_rows(conn, 'sqlite', 'key-b', 5)
        summary = metrics.summary_for_connection(conn)
        self.assertEqual(summary['queries'], 3)
        self.assertEqual(summary['errors'], 1)
        self.assertEqual(summary['rows'], 15)
        self.assertAlmostEqual(summary['avg_duration_ms'], 20.0, places=1)  # (10+20+30)/3 ms

    def test_summary_for_connection_with_no_activity_has_no_average(self):
        conn = 'unit_test_conn_unused_' + uuid.uuid4().hex
        summary = metrics.summary_for_connection(conn)
        self.assertEqual(summary, {'queries': 0, 'errors': 0, 'rows': 0, 'avg_duration_ms': None})

    def test_summaries_are_isolated_by_exact_name(self):
        # a connection and key that happen to share a name must not bleed into each other's summary
        name = 'unit_test_shared_name_' + uuid.uuid4().hex
        metrics.observe_query(name, 'sqlite', 'success', 0.01, 'some-other-key')
        self.assertEqual(metrics.summary_for_key(name), {'queries': 0, 'errors': 0, 'rows': 0})

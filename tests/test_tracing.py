"""Tracing with OpenTelemetry (ADR 0010, BACKLOG #92): spans for requests, saved queries, database calls,
serialisation, MCP tool calls and exports; a caller's traceparent continued; the trace id in run history, error bodies
and JSON logs - and never a parameter value, row, statement, header or secret in any span."""
import contextlib
import http.server
import io
import json
import logging
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest import mock

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode

from queryapigate import apikeys, cli, create_app, logging_setup, mcp_server, runners, tracing
from tests.helpers import create_key, save_query, secret_of, write_connections

ADMIN = {'X-API-Key': 'admin-key'}
PARAM, ROW, IN_SQL, HEADER, PASSWORD = ('SENTINEL-PARAM-7731', 'SENTINEL-ROW-4410', 'SENTINEL-SQL-9921',
                                        'SENTINEL-HDR-1203', 'SENTINEL-PW-5500')
TRACE_ID, PARENT_ID = '0af7651916cd43dd8448eb211c80319c', 'b7ad6b7169203331'
TRACEPARENT = f'00-{TRACE_ID}-{PARENT_ID}-01'


class TracingTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = os.path.join(self.tmp.name, 'data.db')
        conn = sqlite3.connect(self.data)
        conn.execute('CREATE TABLE people (name TEXT, note TEXT)')
        conn.execute('INSERT INTO people VALUES (?, ?)', (PARAM, ROW))
        conn.commit()
        conn.close()
        patcher = mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': self.tmp.name, 'QUERYAPIGATE_API_KEY': 'admin-key'})
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ('OTEL_EXPORTER_OTLP_ENDPOINT', 'OTEL_EXPORTER_OTLP_TRACES_ENDPOINT'):
            os.environ.pop(name, None)
        write_connections({'lite': {'db': 'sqlite', 'database': self.data, 'active': True, 'password': PASSWORD}})
        self.exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(self.exporter))
        tracing.use(provider)
        self.addCleanup(tracing.use, None)
        self.app = create_app()
        self.client = self.app.test_client()
        res = save_query(self.client, {'author': 'a', 'description': 'd', 'filename': 'people_named',
                                       'connection_name': 'lite',
                                       'sql_query': f"SELECT note /* {IN_SQL} */ FROM people WHERE name = :name",
                                       'query_parameters': {'name': {'type': 'string'}}}, headers=ADMIN)
        self.assertEqual(res.status_code, 201, res.get_data(as_text=True))
        self.secret = secret_of(create_key(self.client, ADMIN, name='partner', queries=['people_named'],
                                           connections=['lite']))
        self.exporter.clear()

    def spans(self):
        return self.exporter.get_finished_spans()

    def named(self, name):
        found = [s for s in self.spans() if s.name == name]
        self.assertTrue(found, f'no span {name!r} in {[s.name for s in self.spans()]}')
        return found[-1]


class SpanTests(TracingTestCase):
    def test_a_saved_query_request_is_a_tree_of_spans(self):
        res = self.client.get(f'/q/people_named?name={PARAM}', headers={'X-API-Key': self.secret})
        self.assertEqual(res.status_code, 200)
        server, query = self.named('GET /q/<name>'), self.named('query people_named')
        db_call, serialize = self.named('SELECT lite'), self.named('serialize json')
        self.assertEqual(server.kind, SpanKind.SERVER)
        self.assertEqual(db_call.kind, SpanKind.CLIENT)
        self.assertEqual(query.parent.span_id, server.context.span_id)
        self.assertEqual(db_call.parent.span_id, query.context.span_id)
        self.assertEqual(serialize.parent.span_id, query.context.span_id)
        self.assertEqual({k: server.attributes[k] for k in ('http.request.method', 'http.route',
                                                             'http.response.status_code', 'queryapigate.caller')},
                         {'http.request.method': 'GET', 'http.route': '/q/<name>', 'http.response.status_code': 200,
                          'queryapigate.caller': 'partner'})
        self.assertEqual(dict(query.attributes), {'queryapigate.query': 'people_named', 'queryapigate.version': 1,
                                                  'queryapigate.connection': 'lite', 'queryapigate.rows': 1,
                                                  'queryapigate.masked': False})
        self.assertEqual(dict(db_call.attributes), {'db.system.name': 'sqlite', 'db.operation.name': 'SELECT',
                                                    'queryapigate.connection': 'lite',
                                                    'db.response.returned_rows': 1})
        self.assertNotIn('db.namespace', db_call.attributes)  # SQLite's database is a file path on this server

    def test_a_callers_traceparent_is_continued(self):
        self.client.get('/health', headers={'traceparent': TRACEPARENT})
        server = self.named('GET /health')
        self.assertEqual(format(server.context.trace_id, '032x'), TRACE_ID)
        self.assertEqual(format(server.parent.span_id, '016x'), PARENT_ID)

    def test_the_trace_id_is_in_run_history_and_error_bodies(self):
        self.client.get(f'/q/people_named?name={PARAM}', headers={'traceparent': TRACEPARENT, **ADMIN})
        runs = self.client.get('/api/v1/history?query=people_named', headers=ADMIN).get_json()['items']
        self.assertEqual(runs[0]['trace_id'], TRACE_ID)
        missing = self.client.get('/q/nothing', headers={'traceparent': TRACEPARENT, **ADMIN})
        self.assertEqual((missing.status_code, missing.get_json()['trace_id']), (404, TRACE_ID))

    def test_json_log_lines_carry_the_trace(self):
        record = logging.makeLogRecord({'msg': 'hello'})
        with tracing.span('x') as span:
            line = json.loads(logging_setup._JsonFormatter().format(record))
        self.assertEqual(line['trace_id'], format(span.get_span_context().trace_id, '032x'))
        self.assertEqual(line['span_id'], format(span.get_span_context().span_id, '016x'))
        self.assertNotIn('trace_id', json.loads(logging_setup._JsonFormatter().format(record)))  # outside a span

    def test_a_client_error_keeps_its_code_and_a_server_error_fails_the_span(self):
        self.client.get('/q/nothing', headers=ADMIN)
        server = self.named('GET /q/<name>')
        self.assertEqual((server.status.status_code, server.attributes['error.type']),
                         (StatusCode.UNSET, 'query_not_found'))
        self.exporter.clear()
        with mock.patch.dict(runners.RUNNERS, {'sqlite': mock.Mock(side_effect=RuntimeError(f'boom {ROW}'))}):
            res = self.client.get(f'/q/people_named?name={PARAM}', headers=ADMIN)
        self.assertEqual(res.status_code, 500)
        db_call, server = self.named('SELECT lite'), self.named('GET /q/<name>')
        self.assertEqual((db_call.status.status_code, db_call.attributes['error.type']),
                         (StatusCode.ERROR, 'RuntimeError'))
        self.assertEqual(server.status.status_code, StatusCode.ERROR)
        self.assertFalse(any(s.events for s in self.spans()))  # no exception event: its message carries values

    def test_an_mcp_tool_call_continues_the_callers_trace(self):
        permission = apikeys.authenticate(self.secret)
        result = mcp_server.call_as(self.app, permission, 'people_named', {'name': PARAM},
                                    headers={'traceparent': TRACEPARENT})
        self.assertFalse(result.get('isError'), result)
        call, query = self.named('tools/call people_named'), self.named('query people_named')
        self.assertEqual(call.kind, SpanKind.SERVER)
        self.assertEqual(format(call.context.trace_id, '032x'), TRACE_ID)
        self.assertEqual(query.parent.span_id, call.context.span_id)
        self.assertEqual((call.attributes['gen_ai.tool.name'], call.attributes['queryapigate.caller']),
                         ('people_named', 'partner'))

    def test_a_tool_name_that_couldnt_be_one_isnt_recorded(self):
        mcp_server.call_as(self.app, apikeys.authenticate(self.secret), f'x" OR {PARAM}\n' * 20, {})
        self.assertEqual(self.named('tools/call (invalid)').attributes['gen_ai.tool.name'], '(invalid)')

    def test_a_cli_export_continues_its_schedulers_traceparent(self):
        out = os.path.join(self.tmp.name, 'people.csv')
        with mock.patch.dict(os.environ, {'TRACEPARENT': TRACEPARENT}), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(['export', 'people_named', '--param', f'name={PARAM}', '--out', out]), 0)
        export = self.named('export people_named')
        self.assertEqual(format(export.context.trace_id, '032x'), TRACE_ID)
        self.assertEqual(self.named('SELECT lite').parent.span_id, export.context.span_id)


class NeverInASpanTests(TracingTestCase):
    def test_no_value_statement_header_or_secret_reaches_any_span(self):
        partner = {'X-API-Key': self.secret, 'X-Custom': HEADER, 'Cookie': f'session={HEADER}'}
        self.client.get(f'/q/people_named?name={PARAM}', headers=partner)
        self.client.post('/q/people_named', json={'name': PARAM}, headers=partner)
        self.client.get(f'/q/people_named?name={PARAM}&format=csv', headers=partner)
        self.client.get(f'/q/people_named?name={PARAM}&stream=true&format=csv', headers=partner).get_data()
        self.client.post('/execute_sql', headers=ADMIN, json={
            'connection_name': 'lite', 'sql': f"SELECT note FROM people WHERE name = '{PARAM}' -- {IN_SQL}"})
        self.client.get(f'/q/people_named?nam={PARAM}', headers=partner)  # an error, naming the parameter
        mcp_server.call_as(self.app, apikeys.authenticate(self.secret), 'people_named', {'name': PARAM})
        mcp_server.call_as(self.app, apikeys.authenticate(self.secret), 'execute_sql',
                           {'connection_name': 'lite', 'sql': f"SELECT '{PARAM}' -- {IN_SQL}"})
        out = os.path.join(self.tmp.name, 'people.csv')
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            cli.main(['export', 'people_named', '--param', f'name={PARAM}', '--out', out])
        spans = self.spans()
        self.assertGreater(len(spans), 15)
        for span in spans:
            dumped = span.to_json()
            for secret in (PARAM, ROW, IN_SQL, HEADER, PASSWORD, self.secret, 'admin-key', 'people WHERE'):
                self.assertNotIn(secret, dumped, f'{secret} in span {span.name}')


class OffTests(TracingTestCase):
    def test_off_nothing_is_traced_and_no_trace_id_appears(self):
        tracing.use(None)
        res = self.client.get('/q/nothing', headers={'traceparent': TRACEPARENT, **ADMIN})
        self.assertNotIn('trace_id', res.get_json())
        self.assertEqual(self.spans(), ())
        self.assertIsNone(tracing.current())
        self.assertEqual(tracing.ids(), (None, None))

    def test_an_endpoint_without_the_packages_stops_the_server(self):
        with mock.patch.dict(os.environ, {'OTEL_EXPORTER_OTLP_ENDPOINT': 'http://collector:4318'}), \
                mock.patch.dict(sys.modules, {'opentelemetry.sdk.trace': None}), \
                self.assertRaisesRegex(ValueError, r'pip install "queryapigate\[otel\]"'):
            tracing.check()

    def test_otel_sdk_disabled_turns_it_off(self):
        with mock.patch.dict(os.environ, {'OTEL_EXPORTER_OTLP_ENDPOINT': 'http://collector:4318',
                                          'OTEL_SDK_DISABLED': 'true'}):
            self.assertFalse(tracing.enabled())


class _Collector(http.server.BaseHTTPRequestHandler):
    received: list = []

    def do_POST(self):  # noqa: N802 - the http.server name
        _Collector.received.append((self.path, self.rfile.read(int(self.headers['Content-Length']))))
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


class OtlpExportTests(TracingTestCase):
    def test_spans_reach_an_otlp_collector_with_the_service_named(self):
        server = http.server.HTTPServer(('127.0.0.1', 0), _Collector)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        _Collector.received = []
        tracing.use(None)
        with mock.patch.dict(os.environ, {'OTEL_EXPORTER_OTLP_ENDPOINT': f'http://127.0.0.1:{server.server_port}'}):
            self.client.get(f'/q/people_named?name={PARAM}', headers=ADMIN)
            tracing.flush()
        self.assertTrue(_Collector.received)
        path, body = _Collector.received[0]
        self.assertEqual(path, '/v1/traces')
        for expected in (b'queryapigate', b'service.instance.id', b'query people_named'):
            self.assertIn(expected, body)
        for secret in (PARAM, ROW, IN_SQL, 'admin-key'):
            self.assertNotIn(secret.encode(), b''.join(b for _, b in _Collector.received))


if __name__ == '__main__':
    unittest.main()

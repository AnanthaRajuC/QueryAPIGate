"""Where data goes (ADR 0016, BACKLOG #101), slice 1: the page that says it - kept true by these tests - the access log
without query strings, statements logged as run history keeps them, and forgetting a person."""
import hashlib
import io
import json
import os
import re
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from queryapigate import accesslog, agentwrites, cli, db, engine, history, instances, quotas, sso, store

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = os.path.join(ROOT, 'documentation', 'DATA_PROTECTION.md')

# Every setting that bounds what is kept, or where it goes - each must be on the page.
SETTINGS = ('QUERYAPIGATE_HISTORY_LIMIT', 'QUERYAPIGATE_HISTORY_ADHOC_LIMIT', 'QUERYAPIGATE_HISTORY_RETENTION_DAYS',
            'QUERYAPIGATE_HISTORY_SAMPLE_RATE', 'QUERYAPIGATE_HISTORY_ADHOC_SQL', 'QUERYAPIGATE_AUDIT_LOG_LIMIT',
            'QUERYAPIGATE_AUDIT_LOG_EXPORT_FILE', 'QUERYAPIGATE_AUDIT_SYSLOG_URL', 'QUERYAPIGATE_AUDIT_OTLP',
            'QUERYAPIGATE_SSO_SESSION_HOURS', 'QUERYAPIGATE_ACCESS_LOG_QUERY', 'QUERYAPIGATE_JSON_LOGS',
            'QUERYAPIGATE_REDIS_URL', 'OTEL_EXPORTER_OTLP_ENDPOINT', 'QUERYAPIGATE_SECRET_KEY',
            'QUERYAPIGATE_MCP_MAX_ROWS', 'QUERYAPIGATE_MASK_KEY', 'QUERYAPIGATE_HISTORY_FLUSH_INTERVAL')
# Every way data leaves the process - each must have a row under "Where data is sent".
FLOWS = ('The log (stdout)', 'The access log', 'Traces', '`/metrics`', "The audit log's copies", 'Live events',
         'AI agents (MCP)', 'Exports (destinations)', 'Secret providers')


def read_page():
    with open(PAGE) as f:
        return f.read()


class PageTests(unittest.TestCase):
    def test_every_table_of_the_store_has_a_row(self):
        page = read_page()
        missing = sorted(t for t in db.expected_columns() if f'`{t}`' not in page)
        self.assertEqual(missing, [], 'a new table: say on DATA_PROTECTION.md what it holds, for how long, for whom')

    def test_every_setting_that_bounds_it_and_every_way_out(self):
        page = read_page()
        self.assertEqual([s for s in SETTINGS if f'`{s}' not in page], [])
        sent = page.split('## Where data is sent', 1)[1].split('\n## ', 1)[0]
        self.assertEqual([f for f in FLOWS if f'| {f}' not in sent], [])

    def test_the_periods_it_states_are_the_codes(self):
        page = read_page()
        rows = {line.split('|')[1].strip(): line for line in page.splitlines() if line.startswith('| `')}
        self.assertIn(f'| {agentwrites.KEEP_DAYS} days |', rows['`write_requests`'])
        self.assertIn(f'| {quotas.KEEP_DAYS} days |', rows['`usage_counters`'])
        self.assertIn(f'| {sso.LOGIN_TTL // 60} minutes |', rows['`sso_logins`'])
        self.assertIn(f'| {int(instances.FORGET // 86400)} day after it stops |', rows['`instances`'])
        with open(os.path.join(ROOT, 'queryapigate', 'quotas.py')) as f:
            redis_days = int(re.search(r'expire\(name, (\d+) \* 86400\)', f.read()).group(1))
        self.assertIn(f"| Redis's quota counters | Calls and rows per caller per period | {redis_days} days |", page)
        self.assertIn('30 days, then deleted', page)
        self.assertEqual(agentwrites.KEEP_DAYS, 30)


class AccessLogTests(unittest.TestCase):
    def test_the_path_without_its_query_string_or_the_referrer(self):
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_ACCESS_LOG_QUERY': ''}):
            fmt = accesslog.gunicorn_format()
            self.assertEqual(accesslog.request_line('GET', '/q/orders?email=alice@example.com', 'HTTP/1.1'),
                             'GET /q/orders HTTP/1.1')
        self.assertIn('%(U)s', fmt)
        for atom in ('%(r)s', '%(q)s', '%(f)s'):
            self.assertNotIn(atom, fmt)
        self.assertIn('rid=%({x-request-id}o)s', fmt)

    def test_kept_when_the_deployer_asks(self):
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_ACCESS_LOG_QUERY': '1'}):
            self.assertIn('%(r)s', accesslog.gunicorn_format())
            self.assertEqual(accesslog.request_line('GET', '/q/x?a=1', 'HTTP/1.1'), 'GET /q/x?a=1 HTTP/1.1')
        self.assertNotIn('%(f)s', accesslog.gunicorn_format())

    def test_the_image_uses_it(self):
        with open(os.path.join(ROOT, 'Dockerfile')) as f:
            self.assertIn('"-c", "python:queryapigate.gunicorn_conf"', f.read())
        from queryapigate import gunicorn_conf
        self.assertEqual(gunicorn_conf.access_log_format, accesslog.gunicorn_format())

    def test_serve_logs_the_same(self):
        handler = cli._handler()
        line = handler.__new__(handler)
        line.command, line.path, line.request_version = 'GET', '/q/orders?email=alice@example.com', 'HTTP/1.1'
        logged = []
        line.log = lambda level, fmt, *args: logged.append(fmt % args)
        line.log_request(401, '-')
        self.assertEqual(logged, ['"GET /q/orders HTTP/1.1" 401 -'])


class StatementLogTests(unittest.TestCase):
    def test_a_statement_is_logged_as_run_history_keeps_it(self):
        sql = "SELECT * FROM people WHERE email = 'alice@example.com'"
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_HISTORY_ADHOC_SQL': ''}):
            self.assertEqual(engine.logged_sql(sql), sql)
        for mode in ('hash', 'none'):
            with mock.patch.dict(os.environ, {'QUERYAPIGATE_HISTORY_ADHOC_SQL': mode}):
                self.assertEqual(engine.logged_sql(sql),
                                 f'(text not logged; sha256 {hashlib.sha256(sql.encode()).hexdigest()})')


class ForgetTests(unittest.TestCase):
    PERSON = 'jwt:alice@example.com'

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': self.tmp.name, 'QUERYAPIGATE_API_KEY': 'owner',
                                               'QUERYAPIGATE_HISTORY_FLUSH_INTERVAL': '0'})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop('QUERYAPIGATE_REDIS_URL', None)
        db.init_schema()
        self.addCleanup(history.flush)
        store.save_version('q', {'sql_query': 'SELECT 1', 'author': 'a', 'description': 'd', 'tags': [],
                                 'query_parameters': {}})
        for caller in (self.PERSON, self.PERSON, 'jwt:bob@example.com', 'partner-key'):
            history.record('q', 1, {'executed_at': store.now(), 'key_name': caller, 'status': 'success', 'rows': 1})
        history.flush()
        quotas._counter.add(self.PERSON, 'day:2026-10-12', 3, 30)
        quotas._counter.add('jwt:bob@example.com', 'day:2026-10-12', 1, 10)
        quotas.write()
        quotas._counter.add(self.PERSON, 'month:2026-10', 1, 1)  # still unwritten

    def runs(self, caller):
        return db.connection().execute('SELECT COUNT(*) FROM execution_history WHERE key_name = ?',
                                       (caller,)).fetchone()[0]

    def periods(self, caller):
        return db.connection().execute('SELECT COUNT(*) FROM usage_counters WHERE caller = ?', (caller,)).fetchone()[0]

    def audit(self):
        return [json.loads(r[0]) for r in db.connection().execute('SELECT entry_json FROM audit_log ORDER BY id')]

    def test_a_persons_runs_and_usage_go_and_no_one_elses(self):
        # the month's count was never written: counted all the same, whether a background write came first or not
        self.assertEqual(history.forget(self.PERSON, 'owner', dry_run=True),
                         {'runs': 2, 'events': 2, 'usage_periods': 2})
        self.assertEqual(self.runs(self.PERSON), 2)  # a dry run deletes nothing
        self.assertEqual(history.forget(self.PERSON, 'owner'), {'runs': 2, 'events': 2, 'usage_periods': 2})
        self.assertEqual((self.runs(self.PERSON), self.periods(self.PERSON)), (0, 0))
        self.assertEqual((self.runs('jwt:bob@example.com'), self.runs('partner-key')), (1, 1))
        self.assertEqual(self.periods('jwt:bob@example.com'), 1)
        quotas.write()  # what was still unwritten was forgotten too
        self.assertEqual(self.periods(self.PERSON), 0)

    def test_a_write_in_flight_is_waited_for_not_missed(self):
        # CI, 2026-10-14: the background writer had taken the unwritten month's count and was writing it when forget
        # ran - forget found it in neither place, deleted the rest, and the writer then stored it after the person
        # was forgotten. Forgetting now waits for a write in flight, and deletes what it wrote.
        import threading
        writing, release, original = threading.Event(), threading.Event(), quotas._write

        def slow_write(target, counted, stamp):
            writing.set()
            release.wait(5)
            original(target, counted, stamp)
        with mock.patch.object(quotas, '_write', slow_write):
            writer = threading.Thread(target=quotas.write)
            writer.start()
            self.assertTrue(writing.wait(5))
            threading.Timer(0.3, release.set).start()
            forgotten = history.forget(self.PERSON, 'owner')
            writer.join(5)
        self.assertEqual(forgotten['usage_periods'], 2)
        self.assertEqual(self.periods(self.PERSON), 0)

    def test_the_record_of_it_keeps_nothing_of_who(self):
        history.forget(self.PERSON, 'owner')
        entry = self.audit()[-1]
        self.assertEqual((entry['action'], entry['actor'], entry['target']), ('forget_caller', 'owner', 'history'))
        self.assertEqual(entry['changes'], {'caller_sha256': hashlib.sha256(self.PERSON.encode()).hexdigest(),
                                            'runs': 2, 'events': 2, 'usage_periods': 2})
        self.assertNotIn('alice', json.dumps(self.audit()))

    def test_through_the_api_owners_only(self):
        from queryapigate import admins, create_app
        client = create_app().test_client()
        admins.create_admin('ops', 'admin')
        ops = {'X-API-Key': admins.issue_token('ops')[1]}
        refused = client.delete(f'/api/v1/history?caller={self.PERSON}', headers=ops)
        self.assertEqual((refused.status_code, refused.get_json()['code']), (403, 'role_forbidden'))
        answer = client.delete(f'/api/v1/history?caller={self.PERSON}', headers={'X-API-Key': 'owner'})
        self.assertEqual((answer.status_code, answer.get_json()), (200, {'runs': 2, 'events': 2, 'usage_periods': 2}))
        self.assertEqual(client.delete('/api/v1/history', headers={'X-API-Key': 'owner'}).get_json()['code'],
                         'invalid_body')

    def test_the_cli(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(['history', 'forget', self.PERSON])
        self.assertEqual(code, 0, err.getvalue())
        self.assertIn('Deleted 2 run(s), 2 event(s) and 2 usage period(s) of jwt:alice@example.com', out.getvalue())

    @unittest.skipUnless(os.environ.get('QUERYAPIGATE_TEST_REDIS_URL'), 'set QUERYAPIGATE_TEST_REDIS_URL')
    def test_and_what_redis_counts(self):
        import redis
        url = os.environ['QUERYAPIGATE_TEST_REDIS_URL']
        client = redis.Redis.from_url(url)
        for name in (f'qag:quota:{self.PERSON}:day:2026-10-12', 'qag:quota:jwt:bob@example.com:day:2026-10-12'):
            client.hset(name, 'calls', 1)
        self.addCleanup(lambda: client.delete('qag:quota:jwt:bob@example.com:day:2026-10-12'))
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_REDIS_URL': url}):
            history.forget(self.PERSON, 'owner')
        self.assertFalse(client.exists(f'qag:quota:{self.PERSON}:day:2026-10-12'))
        self.assertTrue(client.exists('qag:quota:jwt:bob@example.com:day:2026-10-12'))


if __name__ == '__main__':
    unittest.main()

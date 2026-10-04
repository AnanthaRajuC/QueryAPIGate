"""GET /api/v1/alerts (alerts.py): each check raises its alert when its condition holds, says what to do, points at
what to fix - and the alert goes away by itself once the condition does."""
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta
from unittest import mock

from queryapigate import alerts, config, create_app, history
from tests.helpers import create_key, save_query, write_connections

ADMIN = {'X-API-Key': 'admin-key'}


class AlertTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': self.tmp.name, 'QUERYAPIGATE_API_KEY': 'admin-key',
                                               'QUERYAPIGATE_HISTORY_FLUSH_INTERVAL': '0'})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(history.flush)
        self.addCleanup(alerts._refusals.clear)
        write_connections({'lite': {'db': 'sqlite', 'database': os.path.join(self.tmp.name, 'd.db'), 'active': True}})
        self.client = create_app().test_client()

    def alerts(self):
        res = self.client.get('/api/v1/alerts', headers=ADMIN)
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        return res.get_json()['items']

    def kinds(self):
        return {a['id']: a for a in self.alerts()}

    def record(self, query, *, status='success', code=None, duration_ms=10, connection='lite', error=None,
               executed_at=None):
        entry = {'connection_name': connection, 'status': status, 'duration_ms': duration_ms, 'key_name': 'admin',
                 'executed_at': executed_at or datetime.now().strftime(history.TIME_FORMAT)}
        if status == 'error':
            entry.update(code=code or 'query_failed', error=error or 'boom')
        if query is None:
            history.record_adhoc(entry, 'SELECT 1')
        else:
            history.record(query, 1, entry)
        history.flush()

    def save(self, name):
        save_query(self.client, headers=ADMIN, body={'filename': name, 'description': 'd', 'connection_name': 'lite',
                                                     'sql_query': 'SELECT 1'})


class ServerTests(AlertTestCase):
    def test_a_quiet_healthy_server_has_none(self):
        self.assertEqual(self.alerts(), [])

    def test_a_server_without_any_key_is_critical(self):
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_API_KEY': ''}):
            os.environ.pop('QUERYAPIGATE_API_KEY')
            [alert] = self.client.get('/api/v1/alerts').get_json()['items']
        self.assertEqual((alert['id'], alert['severity']), ('open_server:server', 'critical'))
        self.assertIn('QUERYAPIGATE_API_KEY', alert['detail'])

    def test_admin_only(self):
        secret = create_key(self.client, headers=ADMIN, name='scoped', connections=['lite']).get_json()['secret']
        res = self.client.get('/api/v1/alerts', headers={'X-API-Key': secret})
        self.assertEqual((res.status_code, res.get_json()['code']), (403, 'admin_only'))


class KeyTests(AlertTestCase):
    def test_a_key_expiring_within_a_week_and_one_expired(self):
        soon = (date.today() + timedelta(days=3)).isoformat()
        later = (date.today() + timedelta(days=30)).isoformat()
        create_key(self.client, headers=ADMIN, name='soon', connections=['lite'], expires_at=soon)
        create_key(self.client, headers=ADMIN, name='later', connections=['lite'], expires_at=later)
        found = self.kinds()
        self.assertEqual(found['key_expiring:soon']['title'], "API key 'soon' expires in 3 days")
        self.assertEqual(found['key_expiring:soon']['target'], {'type': 'key', 'name': 'soon'})
        self.assertNotIn('key_expiring:later', found)
        with mock.patch('queryapigate.alerts.datetime', wraps=datetime) as clock:
            clock.now.return_value = datetime.now() + timedelta(days=5)
            found = self.kinds()
        self.assertEqual(found['key_expired:soon']['severity'], 'warning')
        self.assertNotIn('key_expiring:soon', found)

    def test_a_revoked_key_is_not_called_out(self):
        soon = (date.today() + timedelta(days=1)).isoformat()
        create_key(self.client, headers=ADMIN, name='gone', connections=['lite'], expires_at=soon)
        self.client.patch('/api/v1/api-keys/gone', headers=ADMIN, json={'active': False})
        self.assertNotIn('key_expiring:gone', self.kinds())

    def test_a_key_unused_for_the_configured_days(self):
        create_key(self.client, headers=ADMIN, name='idle', connections=['lite'])
        self.assertNotIn('key_unused:idle', self.kinds())
        with mock.patch('queryapigate.alerts.datetime', wraps=datetime) as clock:
            clock.now.return_value = datetime.now() + timedelta(days=91)
            alert = self.kinds()['key_unused:idle']
            self.assertEqual(alert['severity'], 'info')
            self.assertIn('Never used', alert['detail'])
            with mock.patch.dict(os.environ, {'QUERYAPIGATE_ALERT_KEY_UNUSED_DAYS': '0'}):
                self.assertNotIn('key_unused:idle', self.kinds())

    def test_a_key_that_keeps_hitting_its_rate_limit(self):
        secret = create_key(self.client, headers=ADMIN, name='busy', connections=['lite'],
                            rate_limit='1/hour').get_json()['secret']
        for _ in range(11):
            self.client.get('/catalog', headers={'X-API-Key': secret})
        alert = self.kinds()['key_rate_limited:busy']
        self.assertIn('10 calls refused in the last hour', alert['detail'])

    def test_a_client_that_keeps_hitting_the_server_limit(self):
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_RATE_LIMIT': '1/hour'}):
            for _ in range(12):
                self.client.get('/catalog', headers=ADMIN)
        found = self.client.get('/api/v1/alerts', headers=ADMIN).get_json()['items']
        self.assertIn('client_rate_limited:127.0.0.1', {a['id'] for a in found})

    def test_a_few_refusals_are_normal(self):
        for _ in range(5):
            alerts.note_rate_limited('key', 'calm')
        self.assertNotIn('key_rate_limited:calm', self.kinds())


class RunTests(AlertTestCase):
    def test_a_connection_whose_newest_runs_all_fail_to_connect(self):
        self.record(None)
        for _ in range(3):
            self.record(None, status='error', code='connection_failed', error='could not connect to server')
        alert = self.kinds()['connection_failing:lite']
        self.assertEqual(alert['severity'], 'critical')
        self.assertIn('could not connect to server', alert['detail'])
        self.record(None)  # it works again: the alert is gone
        self.assertNotIn('connection_failing:lite', self.kinds())

    def test_bad_sql_does_not_make_a_connection_failing(self):
        for _ in range(4):
            self.record(None, status='error', code='query_failed')
        self.assertNotIn('connection_failing:lite', self.kinds())

    def test_a_query_with_a_high_error_rate(self):
        self.save('flaky')
        for i in range(10):
            self.record('flaky', status='error' if i < 3 else 'success')
        alert = self.kinds()['query_errors:flaky']
        self.assertEqual(alert['title'], "Query 'flaky' fails 30% of its runs")
        self.assertEqual(alert['target'], {'type': 'query', 'name': 'flaky'})
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_ALERT_ERROR_RATE': '50'}):
            self.assertNotIn('query_errors:flaky', self.kinds())

    def test_too_few_runs_say_nothing_about_a_rate(self):
        self.save('new')
        self.record('new', status='error')
        self.assertNotIn('query_errors:new', self.kinds())

    def test_a_query_that_times_out(self):
        self.save('heavy')
        self.record('heavy', status='error', code='query_timeout')
        self.assertIn('timed out 1 time in the last 24 hours', self.kinds()['query_timeouts:heavy']['title'])

    def test_a_query_that_is_typically_slow(self):
        self.save('slow')
        self.save('spiky')
        for _ in range(5):
            self.record('slow', duration_ms=2500)
        for ms in (10, 10, 10, 10, 9000):  # one slow run is not a slow query
            self.record('spiky', duration_ms=ms)
        found = self.kinds()
        self.assertIn('2.5 s', found['query_slow:slow']['detail'])
        self.assertNotIn('query_slow:spiky', found)
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_SLOW_QUERY_THRESHOLD': '3'}):
            self.assertNotIn('query_slow:slow', self.kinds())

    def test_old_runs_are_left_out(self):
        self.save('past')
        old = (datetime.now() - timedelta(days=8)).strftime(history.TIME_FORMAT)
        for _ in range(10):
            self.record('past', status='error', executed_at=old)
        self.assertNotIn('query_errors:past', self.kinds())


class OrderAndHealthTests(AlertTestCase):
    def test_most_severe_first_and_history_trouble(self):
        create_key(self.client, headers=ADMIN, name='soon', connections=['lite'],
                   expires_at=(date.today() + timedelta(days=2)).isoformat())
        with mock.patch('queryapigate.metrics.history_counts', return_value={'dropped': 4, 'failed': 1}):
            found = self.alerts()
        self.assertEqual([a['severity'] for a in found], sorted((a['severity'] for a in found),
                                                                 key=alerts.SEVERITY_ORDER.get))
        self.assertIn('history_dropped:server', {a['id'] for a in found})
        self.assertIn('4 runs dropped', next(a for a in found if a['kind'] == 'history_dropped')['detail'])


class SettingsTests(unittest.TestCase):
    def test_bad_thresholds_stop_startup(self):
        for name, value in (('QUERYAPIGATE_ALERT_ERROR_RATE', '120'), ('QUERYAPIGATE_ALERT_ERROR_RATE', 'lots'),
                            ('QUERYAPIGATE_ALERT_KEY_UNUSED_DAYS', '-1')):
            with self.subTest(name=name, value=value), mock.patch.dict(os.environ, {name: value}), \
                    self.assertRaises(ValueError):
                config.check_settings()


if __name__ == '__main__':
    unittest.main()

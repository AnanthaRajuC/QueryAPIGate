"""Usage quotas (ADR 0011, BACKLOG #95): calls and rows per calendar day or month, per caller - checked before a call,
counted after it, kept in the store through the run-history batch."""
import os
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime
from unittest import mock

import jwt

from queryapigate import alerts, apikeys, create_app, db, history, mcp_server, quotas
from tests.helpers import create_key, save_query, secret_of, write_connections

ADMIN = {'X-API-Key': 'admin-key'}
SECRET = 'a-shared-secret-that-is-long-enough-for-hs256'


class QuotaTestCase(unittest.TestCase):
    env = {}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        data = os.path.join(self.tmp.name, 'data.db')
        conn = sqlite3.connect(data)
        conn.execute('CREATE TABLE t (id INTEGER)')
        conn.executemany('INSERT INTO t VALUES (?)', [(i,) for i in range(50)])
        conn.commit()
        conn.close()
        patcher = mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': self.tmp.name, 'QUERYAPIGATE_API_KEY': 'admin-key',
                                               'QUERYAPIGATE_RATE_LIMIT': '', 'QUERYAPIGATE_JWT_SECRET': '',
                                               **self.env})
        patcher.start()
        self.addCleanup(patcher.stop)
        quotas._counter.pending.clear()
        quotas._counter.read.clear()
        self.addCleanup(history.flush)
        write_connections({'lite': {'db': 'sqlite', 'database': data, 'active': True}})
        self.app = create_app()
        self.client = self.app.test_client()
        for name, ttl in (('ids', None), ('cached', 60)):
            res = save_query(self.client, {'author': 'a', 'description': 'd', 'filename': name,
                                           'connection_name': 'lite', 'sql_query': 'SELECT id FROM t ORDER BY id',
                                           'cache_ttl': ttl}, headers=ADMIN)
            self.assertEqual(res.status_code, 201, res.get_data(as_text=True))

    def key(self, quota, name='partner'):
        return secret_of(create_key(self.client, ADMIN, name=name, queries=['ids', 'cached'], connections=['lite'],
                                    quota=quota))

    def get(self, secret, path='/q/ids'):
        return self.client.get(path, headers={'X-API-Key': secret})


class GrantTests(QuotaTestCase):
    def test_bad_quotas_are_refused(self):
        for quota in ({'calls': 10}, {'per': 'week', 'calls': 1}, {'per': 'day'}, {'per': 'day', 'calls': 0},
                      {'per': 'day', 'calls': 1.5}, {'per': 'day', 'calls': True}, {'per': 'day', 'bytes': 9},
                      'monthly'):
            res = create_key(self.client, ADMIN, name='bad', quota=quota)
            self.assertEqual((res.status_code, res.get_json()['code']), (400, 'invalid_body'), quota)

    def test_set_shown_and_cleared(self):
        self.key({'calls': 5, 'per': 'month'})
        key = self.client.get('/api/v1/api-keys/partner', headers=ADMIN).get_json()
        self.assertEqual(key['quota'], {'calls': 5, 'per': 'month'})
        usage = key['usage_this_period']
        self.assertEqual((usage['per'], usage['calls'], usage['calls_limit'], usage['rows_limit']),
                         ('month', 0, 5, None))
        self.assertEqual(usage['resets_at'], quotas.period('month')[2].strftime('%Y-%m-%d %H:%M:%S'))
        res = self.client.patch('/api/v1/api-keys/partner', headers=ADMIN, json={'quota': None})
        self.assertIsNone(res.get_json()['usage_this_period'])


class CountingTests(QuotaTestCase):
    def test_calls_are_counted_until_the_quota_is_used_up(self):
        secret = self.key({'calls': 3, 'per': 'day'})
        remaining = [self.get(secret).headers['X-Quota-Remaining-Calls'] for _ in range(3)]
        self.assertEqual(remaining, ['2', '1', '0'])
        refused = self.get(secret)
        body = refused.get_json()
        self.assertEqual((refused.status_code, body['code'], body['quota'], body['limit']),
                         (429, 'quota_exceeded', 'calls', 3))
        resets = quotas.period('day')[2]
        self.assertEqual(body['resets_at'], resets.strftime('%Y-%m-%d %H:%M:%S'))
        self.assertEqual(refused.headers['X-Quota-Reset'], body['resets_at'])
        self.assertLessEqual(int(refused.headers['Retry-After']), 86400)
        self.assertEqual(self.client.get('/q/ids', headers=ADMIN).status_code, 200)  # the admin has no quota

    def test_cached_responses_count_and_are_refused_too(self):
        secret = self.key({'calls': 2, 'per': 'day'})
        self.assertEqual(self.get(secret, '/q/cached').headers['X-Cache'], 'MISS')
        self.assertEqual(self.get(secret, '/q/cached').headers['X-Cache'], 'HIT')
        self.assertEqual(self.get(secret, '/q/cached').status_code, 429)

    def test_rows_are_counted_and_the_crossing_call_completes(self):
        secret = self.key({'rows': 15, 'per': 'day'})
        first = self.get(secret, '/q/ids?page_size=10')
        self.assertEqual((first.headers['X-Row-Count'], first.headers['X-Quota-Remaining-Rows']), ('10', '5'))
        crossing = self.get(secret, '/q/ids?page_size=10')
        self.assertEqual((crossing.status_code, crossing.headers['X-Quota-Remaining-Rows']), (200, '0'))  # 20 of 15
        self.assertEqual(self.get(secret).get_json()['quota'], 'rows')

    def test_a_streams_rows_count_as_they_leave(self):
        secret = self.key({'rows': 1000, 'per': 'day'})
        self.get(secret, '/q/ids?stream=true&format=ndjson').get_data()
        self.assertEqual(self.client.get('/api/v1/api-keys/partner', headers=ADMIN).get_json()
                         ['usage_this_period']['rows'], 50)

    def test_ad_hoc_sql_counts(self):
        secret = self.key({'calls': 1, 'rows': 100, 'per': 'day'})
        res = self.client.post('/execute_sql', headers={'X-API-Key': secret},
                               json={'connection_name': 'lite', 'sql': 'SELECT id FROM t LIMIT 4'})
        self.assertEqual((res.status_code, res.headers['X-Quota-Remaining-Rows']), (200, '96'))
        self.assertEqual(self.client.post('/execute_sql', headers={'X-API-Key': secret}, json={
            'connection_name': 'lite', 'sql': 'SELECT 1'}).status_code, 429)

    def test_refused_calls_dont_count(self):
        secret = self.key({'calls': 1, 'per': 'day'})
        self.assertEqual(self.get(secret, '/q/nothing').status_code, 404)
        self.assertEqual(self.get(secret, '/q/ids?page_size=0').status_code, 400)
        self.assertEqual(self.client.post('/execute_sql', headers={'X-API-Key': secret}, json={
            'connection_name': 'lite', 'sql': 'SELECT * FROM missing'}).status_code, 500)  # a database error
        self.assertEqual(self.get(secret).status_code, 200)

    def test_mcp_tool_calls_count_and_are_refused(self):
        secret = self.key({'calls': 2, 'per': 'day'})
        permission = apikeys.authenticate(secret)
        mcp_server.call_as(self.app, permission, 'list_tables', {'connection_name': 'lite'})  # not data
        self.assertFalse(mcp_server.call_as(self.app, permission, 'ids', {}).get('isError'))
        self.assertFalse(mcp_server.call_as(self.app, permission, 'ids', {}).get('isError'))
        refused = mcp_server.call_as(self.app, permission, 'ids', {})
        self.assertEqual((refused['isError'], refused['structuredContent']['code']), (True, 'quota_exceeded'))


class StoreTests(QuotaTestCase):
    env = {'QUERYAPIGATE_HISTORY_FLUSH_INTERVAL': '5'}

    def test_counts_reach_the_store_in_the_history_batch_and_other_instances_see_them(self):
        secret = self.key({'calls': 3, 'per': 'month'})
        self.get(secret)
        self.get(secret)
        self.assertEqual(db.connection().execute('SELECT COUNT(*) FROM usage_counters').fetchone()[0], 0)  # not yet
        history.flush()
        quotas.write()  # what the writer's batch does
        period = quotas.period('month')[0]
        row = db.connection().execute('SELECT calls, rows FROM usage_counters WHERE caller = ? AND period = ?',
                                      ('partner', period)).fetchone()
        self.assertEqual((row[0], row[1]), (2, 20))
        quotas._counter.read.clear()  # another instance: nothing of its own, reading the store
        self.assertEqual(self.get(secret).status_code, 200)
        quotas.write()
        quotas._counter.read.clear()
        self.assertEqual(self.get(secret).status_code, 429)
        listed = self.client.get('/api/v1/usage/quotas', headers=ADMIN).get_json()['items']
        self.assertEqual([(c['caller'], c['per'], c['calls'], c['rows']) for c in listed],
                         [('partner', 'month', 3, 30)])

    def test_a_failed_write_keeps_the_counts(self):
        self.key({'calls': 9, 'per': 'day'})
        quotas._counter.add('partner', quotas.period('day')[0], 2, 7)
        with mock.patch.object(quotas, '_write', side_effect=RuntimeError('store down')), \
                self.assertRaises(RuntimeError):
            quotas.write()
        quotas.write()
        row = db.connection().execute('SELECT calls, rows FROM usage_counters').fetchone()
        self.assertEqual((row[0], row[1]), (2, 7))


class ImmediateWriteTests(QuotaTestCase):
    env = {'QUERYAPIGATE_HISTORY_FLUSH_INTERVAL': '0'}

    def test_with_no_batching_each_call_is_written_at_once(self):
        secret = self.key({'calls': 5, 'per': 'day'})
        self.get(secret)
        self.assertEqual(db.connection().execute('SELECT calls FROM usage_counters').fetchone()[0], 1)


class PeriodTests(unittest.TestCase):
    def test_calendar_days_and_months(self):
        self.assertEqual(quotas.period('day', datetime(2026, 12, 31, 23, 59)),
                         ('day:2026-12-31', datetime(2026, 12, 31), datetime(2027, 1, 1)))
        self.assertEqual(quotas.period('month', datetime(2026, 12, 15, 8)),
                         ('month:2026-12', datetime(2026, 12, 1), datetime(2027, 1, 1)))
        self.assertEqual(quotas.period('month', datetime(2028, 2, 29)),
                         ('month:2028-02', datetime(2028, 2, 1), datetime(2028, 3, 1)))


class AlertTests(QuotaTestCase):
    def test_at_80_percent_and_when_used_up(self):
        secret = self.key({'calls': 5, 'per': 'month'})
        for _ in range(4):
            self.get(secret)

        def found():
            return [(a['severity'], a['title']) for a in alerts.collect() if a['kind'] == 'quota_nearly_used']

        self.assertEqual(found(), [('warning', "API key 'partner' has used 80% of its monthly quota of calls")])
        self.get(secret)
        self.assertEqual(found(), [('critical', "API key 'partner' has used all of its monthly quota of calls")])


class SignedInUserTests(QuotaTestCase):
    env = {'QUERYAPIGATE_JWT_SECRET': SECRET, 'QUERYAPIGATE_JWT_ROLE': 'mobile'}

    def test_a_roles_quota_is_each_users_own(self):
        res = self.client.post('/api/v1/roles', headers=ADMIN, json={'name': 'mobile', 'queries': ['ids'],
                                                                      'quota': {'calls': 1, 'per': 'day'}})
        self.assertEqual(res.status_code, 201, res.get_data(as_text=True))

        def call(user):
            claims = {'sub': user, 'exp': int(time.time()) + 300}
            return self.client.get('/q/ids', headers={'Authorization': f'Bearer {jwt.encode(claims, SECRET)}'})

        self.assertEqual(call('alice').status_code, 200)
        self.assertEqual(call('alice').status_code, 429)
        self.assertEqual(call('bob').status_code, 200)  # bob's own allowance


if __name__ == '__main__':
    unittest.main()

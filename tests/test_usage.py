"""What each AI agent did (BACKLOG #87, ADR 0006, usage.py): GET /api/v1/usage/agents, worked out from run history -
real MCP calls made here, through mcp_server.handle_call(), not entries written by hand - and the
agent_unusual_volume alert."""
import os
import unittest
from datetime import datetime, timedelta
from unittest import mock

from queryapigate import admins, alerts, history, mcp_server, usage
from tests import TEST_RUN_STORE_URL
from tests.test_api_v1 import ADMIN, V1TestCase

URL = '/api/v1/usage/agents'


class UsageTestCase(V1TestCase):
    def setUp(self):
        super().setUp()
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        conn.execute('CREATE TABLE customer (id INTEGER, email TEXT)')
        conn.executemany('INSERT INTO customer VALUES (?, ?)', [(i, f'c{i}@example.com') for i in range(1, 6)])
        conn.commit()
        conn.close()
        from queryapigate import masking
        masking.forget_schema()
        self.create(publish=True)  # 'films'
        self.agent = self.key('agent', connections=['lite'], allowed_tables=['film', 'customer'],
                              mask=[{'column': 'customer.email', 'action': 'hash'}])
        self.other = self.key('other', connections=['lite'])

    def key(self, name, **grants):
        res = self.client.post('/api/v1/api-keys', headers=ADMIN, json={'name': name, **grants})
        self.assertEqual(res.status_code, 201, res.get_data(as_text=True))
        return res.get_json()['secret']

    def mcp(self, secret, tool, **arguments):
        return mcp_server.handle_call(self.client.application, secret, '', '10.0.0.1', tool, arguments)

    def sql(self, secret, sql):
        return self.mcp(secret, 'execute_sql', connection_name='lite', sql=sql)

    def summary(self, **query):
        res = self.call('get', URL, URL, 200, query_string=query)
        return {item['agent']: item for item in res.get_json()['items']}, res.get_json()


class SummaryTests(UsageTestCase):
    def test_each_agent_s_calls_rows_refusals_and_statements(self):
        for _ in range(3):
            self.sql(self.agent, 'SELECT  id, email FROM customer')  # whitespace aside, one statement
        self.sql(self.agent, 'SELECT id,\n email FROM customer')
        self.sql(self.agent, 'SELECT * FROM rental')            # not in its allowed_tables
        self.sql(self.agent, 'DELETE FROM customer')            # never over MCP
        self.mcp(self.agent, 'films')
        self.mcp(self.agent, 'films')
        self.sql(self.other, 'SELECT title FROM film')
        self.client.post('/execute_sql', headers=ADMIN, json={'sql': 'SELECT 1', 'connection_name': 'lite'})  # REST
        agents, body = self.summary()
        self.assertEqual(set(agents), {'agent', 'other'})        # REST callers aren't agents
        self.assertEqual((body['sample_rate'], body['truncated']), (1.0, False))
        agent = agents['agent']
        self.assertEqual((agent['calls'], agent['adhoc_calls'], agent['errors'], agent['masked_calls']), (8, 6, 2, 4))
        self.assertEqual(agent['rows'], 4 * 5 + 2 * 2)
        self.assertEqual(agent['queries'], {'films': 2})
        self.assertEqual(agent['connections'], ['lite'])
        self.assertEqual(set(agent['refusals']), {'table_not_allowed', 'read_only'})
        top = agent['top_statements'][0]
        self.assertEqual((top['sql'], top['count'], top['rows']), ('SELECT id, email FROM customer', 4, 20))
        self.assertEqual(list(agents), ['agent', 'other'])       # busiest first

    def test_a_hash_only_history_still_groups_statements(self):
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_HISTORY_ADHOC_SQL': 'hash'}):
            self.sql(self.other, 'SELECT title FROM film')
            self.sql(self.other, 'SELECT title FROM film')
        top = self.summary()[0]['other']['top_statements'][0]
        self.assertEqual((top['sql'], len(top['sql_sha256']), top['count']), (None, 64, 2))

    def test_the_window(self):
        self.sql(self.other, 'SELECT title FROM film')
        tomorrow = (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d')
        later = (datetime.now() + timedelta(days=2)).strftime('%Y-%m-%d')
        self.assertEqual(self.summary(since=tomorrow, until=later)[0], {})
        self.assertIn('other', self.summary(since='2020-01-01', until=tomorrow)[0])
        self.assertIn('other', self.summary(hours=1)[0])
        for query in ({'since': 'yesterday'}, {'since': tomorrow, 'until': '2020-01-01'}, {'hours': 0},
                      {'hours': 'x'}, {'hours': 2, 'since': '2020-01-01'}):
            with self.subTest(query=query):
                res = self.client.get(URL, headers=ADMIN, query_string=query)
                self.assertEqual((res.status_code, res.get_json()['code']), (400, 'invalid_filter'))

    def test_every_administrator_may_look_and_api_keys_may_not(self):
        for role in ('owner', 'admin', 'developer', 'auditor'):
            admins.create_admin(f'{role}-1', role)
            token = admins.issue_token(f'{role}-1')[1]
            self.assertEqual(self.client.get(URL, headers={'X-API-Key': token}).status_code, 200, role)
        self.assertEqual(self.client.get(URL, headers={'X-API-Key': self.agent}).status_code, 403)

    def test_a_sampled_history_says_so(self):
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_HISTORY_SAMPLE_RATE': '0.5'}):
            # a run store counts every run in the event log instead (ADR 0029): not a sample
            self.assertEqual(self.summary()[1]['sample_rate'], 1.0 if TEST_RUN_STORE_URL else 0.5)


class VolumeAlertTests(UsageTestCase):
    def runs(self, *spec):
        """(agent, hours ago, rows) -> runs as the alerts read them, newest first."""
        now = datetime.now()
        out = [(None, (now - timedelta(hours=hours)).strftime(history.TIME_FORMAT),
                {'transport': 'mcp', 'key_name': agent, 'rows': rows, 'status': 'success'})
               for agent, hours, rows in spec]
        return now, sorted(out, key=lambda r: r[1], reverse=True)

    def test_a_sudden_rise_past_the_floor_fires(self):
        now, runs = self.runs(('steady', 0.5, 50_000), *[('steady', h, 40_000) for h in range(2, 48)],
                              ('spike', 0.2, 30_000), *[('spike', h, 100) for h in range(2, 48)],
                              ('quiet', 0.1, 9_000))
        found = usage.unusual_volume(runs, now, 10_000)
        self.assertEqual([name for name, _, _ in found], ['spike'])  # steady is busy every hour; quiet is under it

    def test_a_new_agent_past_the_floor_fires(self):
        now, runs = self.runs(('fresh', 0.3, 12_000))
        self.assertEqual([name for name, _, _ in usage.unusual_volume(runs, now, 10_000)], ['fresh'])
        self.assertEqual(usage.unusual_volume(runs, now, 0), [])  # 0 turns it off

    def test_the_alert_from_real_calls(self):
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_ALERT_AGENT_ROWS': '10'}):
            for _ in range(3):
                self.sql(self.agent, 'SELECT id FROM customer')
            history.flush()
            found = [a for a in alerts.collect() if a['kind'] == 'agent_unusual_volume']
            res = self.call('get', '/api/v1/alerts', '/api/v1/alerts', 200)  # and it fits the documented shape
        self.assertEqual([(a['id'], a['target']) for a in found],
                         [('agent_unusual_volume:agent', {'type': 'agent', 'name': 'agent'})])
        self.assertIn('15 rows over MCP in the last hour', found[0]['detail'])
        self.assertIn('agent_unusual_volume:agent', [a['id'] for a in res.get_json()['items']])


class ReadsTests(unittest.TestCase):
    def test_a_summary_reads_at_most_max_scan_runs(self):
        self.assertGreater(usage.MAX_SCAN, alerts.RECENT_RUNS)
        self.assertEqual(usage._statement({'sql': ' SELECT\n 1 '})[1]['sql'], 'SELECT 1')
        self.assertIsNone(usage._statement({}))


if __name__ == '__main__':
    unittest.main()

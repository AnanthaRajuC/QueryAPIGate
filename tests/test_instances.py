"""The processes sharing a store (BACKLOG #58, instances.py): each records itself, refreshes as it serves, and a
deployment that looks wrong - several instances without Redis, or on different versions - is reported in the log at
startup, in alerts and in GET /api/v1/instances. A second process is simulated by writing its row directly; the
multi-server test (test_multinode.py) has real ones."""
import logging
import os
import tempfile
import time
import unittest
from unittest import mock

from queryapigate import __version__, alerts, create_app, db, instances
from tests.helpers import create_key, secret_of

ADMIN = {'X-API-Key': 'admin-key'}


class InstancesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': self.tmp.name, 'QUERYAPIGATE_API_KEY': 'admin-key',
                                               'QUERYAPIGATE_REDIS_URL': ''})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = create_app().test_client()

    def other(self, version=__version__, shared=False, seen_ago=5, name='other-1'):
        now = time.time()
        with db.transaction() as conn:
            conn.execute('INSERT INTO instances (id, host, pid, role, version, shared_limits, started_at, last_seen) '
                         'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                         (name, 'host-b', 4242, 'serve', version, 1 if shared else 0, now - 600, now - seen_ago))

    def listing(self):
        res = self.client.get('/api/v1/instances', headers=ADMIN)
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        return res.get_json()

    def test_a_server_records_itself(self):
        [me] = self.listing()['items']
        self.assertEqual((me['role'], me['version'], me['this'], me['shared_limits'], me['pid']),
                         ('serve', __version__, True, False, os.getpid()))
        self.assertEqual(self.listing()['problems'], [])

    def test_a_second_instance_without_redis_is_reported(self):
        self.other()
        body = self.listing()
        self.assertEqual(len(body['items']), 2)
        self.assertEqual([p['kind'] for p in body['problems']], ['instances_not_shared'])
        self.assertIn('Set the same QUERYAPIGATE_REDIS_URL on every instance', body['problems'][0]['message'])
        self.assertIn('instances_not_shared', [a['kind'] for a in alerts.collect()])

    def test_instances_on_different_versions_are_reported(self):
        self.other(version='0.9.9')
        kinds = [p['kind'] for p in self.listing()['problems']]
        self.assertIn('instances_versions_differ', kinds)
        message = next(p['message'] for p in self.listing()['problems'] if p['kind'] == 'instances_versions_differ')
        self.assertIn('0.9.9 on 1', message)

    def test_one_not_seen_for_a_while_is_not_running(self):
        self.other(seen_ago=instances.ALIVE + 30)
        self.assertEqual(len(self.listing()['items']), 1)
        self.assertEqual(self.listing()['problems'], [])

    def test_one_gone_for_a_day_is_forgotten_at_the_next_start(self):
        self.other(seen_ago=instances.FORGET + 60, name='long-gone')
        create_app()
        self.assertIsNone(db.connection().execute("SELECT id FROM instances WHERE id = 'long-gone'").fetchone())

    def test_serving_refreshes_last_seen_at_most_every_heartbeat(self):
        def seen():
            return db.connection().execute('SELECT last_seen FROM instances WHERE id = ?',
                                           (instances.instance_id(),)).fetchone()[0]
        first = seen()
        time.sleep(0.01)
        self.client.get('/health')
        self.assertEqual(seen(), first)  # too soon
        with mock.patch.object(instances, '_last_beat', time.monotonic() - instances.HEARTBEAT - 1):
            self.client.get('/health')
            self.assertGreater(seen(), first)

    def test_the_startup_log_says_so(self):
        self.other()
        with self.assertLogs('queryapigate', level=logging.WARNING) as logs:
            create_app()
        self.assertTrue(any('2 processes share this store' in line for line in logs.output), logs.output)

    def test_only_the_admin_key_may_list_them(self):
        secret = secret_of(create_key(self.client, headers=ADMIN, name='scoped', connections=[]))
        self.assertEqual(self.client.get('/api/v1/instances', headers={'X-API-Key': secret}).status_code, 403)


if __name__ == '__main__':
    unittest.main()

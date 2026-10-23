"""Tests for metacache.py - the opt-in cache of the key, saved-query and connection rows every request reads - and the
request path's store reads it sits on."""
import os
import unittest
from unittest import mock

from queryapigate import config, db, metacache
from tests import TEST_DATABASE_URL
from tests.test_api_keys import AppTestCase


class RowsTests(unittest.TestCase):
    def setUp(self):
        metacache.clear()
        self.addCleanup(metacache.clear)
        self.loads = 0

    def load(self):
        self.loads += 1
        return [{'n': self.loads}]

    def test_off_by_default_reads_every_time(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('QUERYAPIGATE_METADATA_CACHE_TTL', None)
            self.assertEqual(config.metadata_cache_ttl(), 0)
            metacache.rows('k', 1, self.load)
            metacache.rows('k', 1, self.load)
        self.assertEqual(self.loads, 2)

    def test_reused_until_the_ttl_passes(self):
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_METADATA_CACHE_TTL': '5'}), \
                mock.patch('queryapigate.metacache.time.monotonic', return_value=100.0) as clock:
            self.assertEqual(metacache.rows('k', 1, self.load), [{'n': 1}])
            self.assertEqual(metacache.rows('k', 1, self.load), [{'n': 1}])
            self.assertEqual(metacache.rows('k', 2, self.load), [{'n': 2}])  # another key is its own entry
            clock.return_value = 105.0
            self.assertEqual(metacache.rows('k', 1, self.load), [{'n': 3}])

    def test_a_read_that_overlaps_a_clear_is_not_kept(self):
        def load_then_clear():
            metacache.clear()  # a write committed while this read was in flight: what it read may be stale
            return self.load()

        with mock.patch.dict(os.environ, {'QUERYAPIGATE_METADATA_CACHE_TTL': '60'}):
            metacache.rows('k', 1, load_then_clear)
            metacache.rows('k', 1, self.load)
        self.assertEqual(self.loads, 2)

    def test_bounded(self):
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_METADATA_CACHE_TTL': '60'}), \
                mock.patch.object(metacache, '_MAX_ENTRIES', 2):
            for key in range(5):
                metacache.rows('k', key, self.load)
            self.assertLessEqual(len(metacache._entries), 2)

    def test_bad_setting_means_off(self):
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_METADATA_CACHE_TTL': 'soon'}):
            self.assertEqual(config.metadata_cache_ttl(), 0)


class CachedRequestTests(AppTestCase):
    """With the cache on: what changes through this process is seen at once, and every check on a key still runs."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, {'QUERYAPIGATE_METADATA_CACHE_TTL': '60'})
        patcher.start()
        self.addCleanup(patcher.stop)
        metacache.clear()
        self.addCleanup(metacache.clear)
        self.save_query('items')
        self.secret = self.create_key('reader', queries=['items'])

    def run_items(self, **kwargs):
        return self.client.get('/q/items', headers={'X-API-Key': self.secret}, **kwargs)

    def test_revoking_a_key_here_takes_effect_at_once(self):
        self.assertEqual(self.run_items().status_code, 200)
        self.client.patch('/api/v1/api-keys/reader', json={'active': False}, headers=self.admin_headers)
        self.assertEqual(self.run_items().status_code, 401)

    def test_a_change_made_elsewhere_is_seen_within_the_ttl(self):
        with mock.patch('queryapigate.metacache.time.monotonic', return_value=100.0) as clock:
            self.assertEqual(self.run_items().status_code, 200)  # records last_used_at - a write here, so a clear
            self.assertEqual(self.run_items().status_code, 200)  # now cached
            other = db._open(db.current_target())  # another process, as far as this one can tell
            other.execute('UPDATE api_keys SET active = 0 WHERE name = ?', ('reader',))
            other.close()
            self.assertEqual(self.run_items().status_code, 200)  # the documented window
            clock.return_value = 160.0
            self.assertEqual(self.run_items().status_code, 401)

    def test_allowed_ips_is_checked_on_a_cached_row(self):
        self.client.patch('/api/v1/api-keys/reader', json={'allowed_ips': ['10.0.0.1']}, headers=self.admin_headers)
        self.assertEqual(self.run_items(environ_overrides={'REMOTE_ADDR': '10.0.0.1'}).status_code, 200)
        self.assertEqual(self.run_items(environ_overrides={'REMOTE_ADDR': '10.0.0.2'}).status_code, 401)

    def test_a_new_version_is_served_at_once(self):
        self.assertEqual(self.run_items().get_json(), [{'id': 1}])
        self.save_query('items', sql='SELECT id + 1 AS id FROM t')
        self.assertEqual(self.run_items().get_json(), [{'id': 2}])

    def test_a_deactivated_connection_is_refused_at_once(self):
        self.assertEqual(self.run_items().status_code, 200)
        self.client.patch('/api/v1/connections/a', json={'active': False}, headers=self.admin_headers)
        self.assertEqual(self.run_items().status_code, 403)

    def test_a_log_append_does_not_clear_it(self):
        metacache.rows('k', 1, lambda: [{'n': 1}])
        with db.transaction(append_only=True):
            pass
        self.assertTrue(metacache._entries)
        with db.transaction():
            pass
        self.assertFalse(metacache._entries)


class RunReadsTests(AppTestCase):
    @unittest.skipIf(TEST_DATABASE_URL, 'counts statements with sqlite3\'s trace callback - the same SQL either way')
    def test_a_run_reads_the_saved_query_once(self):
        self.save_query('items')
        statements = []
        db.connection().set_trace_callback(statements.append)
        self.addCleanup(db.connection().set_trace_callback, None)
        res = self.client.get('/q/items', headers=self.admin_headers)
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        reads = [s for s in statements if 'saved_queries' in s and s.lstrip().upper().startswith('SELECT')]
        self.assertEqual(len(reads), 1, reads)

    def test_an_unknown_query_is_still_a_404(self):
        res = self.client.get('/q/nothing_here', headers=self.admin_headers)
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.get_json()['code'], 'query_not_found')


if __name__ == '__main__':
    unittest.main()

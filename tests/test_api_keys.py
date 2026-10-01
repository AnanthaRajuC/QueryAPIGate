"""Tests for per-key API permissions: scoped keys layered on top of QUERYAPIGATE_API_KEY."""
import json
import logging
import os
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from unittest import mock

from queryapigate import apikeys, create_app, db, engine
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
        patcher = mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': tmp, 'QUERYAPIGATE_API_KEY': 'admin-key'})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop('QUERYAPIGATE_ALLOW_WRITES', None)
        write_connections({
            'a': {'db': 'sqlite', 'database': self.db_path, 'active': True},
            'b': {'db': 'sqlite', 'database': self.db_path, 'active': True},
        })
        self.client = create_app().test_client()
        self.admin_headers = {'X-API-Key': 'admin-key'}
        # apikeys._last_recorded_use is in-process, module-level state (see its "throttle last_used_at
        # writes" docstring) - it must not leak between tests that happen to reuse the same key name
        # ("reporting" and friends) across different temp homes, or a later test's first use could be
        # wrongly throttled by an earlier test's write.
        apikeys._last_recorded_use.clear()

    def create_key(self, name='scoped', connections=None, allow_writes=False, queries=None, expires_at=None,
                   allowed_ips=None, allowed_write_ops=None, allowed_tables=None):
        res = self.client.post('/api_keys', json={'name': name, 'connections': connections,
                                                   'allow_writes': allow_writes, 'queries': queries,
                                                   'expires_at': expires_at, 'allowed_ips': allowed_ips,
                                                   'allowed_write_ops': allowed_write_ops,
                                                   'allowed_tables': allowed_tables},
                               headers=self.admin_headers)
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        return res.get_json()['key']

    def save_query(self, filename, connection_name='a', sql='SELECT * FROM t'):
        res = self.client.patch('/save_sql_to_file', json={'author': 'a', 'description': 'd', 'filename': filename,
                                                            'sql_query': sql, 'connection_name': connection_name},
                                headers=self.admin_headers)
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))


class ApiKeyCrudTests(AppTestCase):
    def test_create_list_delete(self):
        key = self.create_key('reporting', connections=['a'], allow_writes=False)
        self.assertTrue(key.startswith('sk_'))
        listed = self.client.get('/api_keys', headers=self.admin_headers).get_json()['keys']
        self.assertIn('reporting', listed)
        self.assertNotIn('hash', listed['reporting'])
        self.assertEqual(listed['reporting']['connections'], ['a'])
        self.assertEqual(self.client.delete('/api_keys/reporting', headers=self.admin_headers).status_code, 200)
        self.assertNotIn('reporting', self.client.get('/api_keys', headers=self.admin_headers).get_json()['keys'])

    def test_duplicate_name_is_rejected(self):
        self.create_key('dup')
        self.assertEqual(self.client.post('/api_keys', json={'name': 'dup'}, headers=self.admin_headers)
                         .status_code, 400)

    def test_invalid_name_is_rejected(self):
        for name in ('bad/name!', '', None):
            res = self.client.post('/api_keys', json={'name': name}, headers=self.admin_headers)
            self.assertEqual(res.status_code, 400, name)

    def test_malformed_connections_is_rejected(self):
        res = self.client.post('/api_keys', json={'name': 'x', 'connections': 'a'}, headers=self.admin_headers)
        self.assertEqual(res.status_code, 400)

    def test_empty_connections_list_blocks_every_connection(self):
        key = self.create_key('locked', connections=[])
        res = self.client.post('/execute_sql', json={'sql': 'SELECT * FROM t', 'connection_name': 'a'},
                               headers={'X-API-Key': key})
        self.assertEqual(res.status_code, 403)

    def test_stored_file_never_holds_the_plaintext_key(self):
        key = self.create_key('reporting')
        row = db.connection().execute('SELECT hash, details_json FROM api_keys WHERE name = ?',
                                      ('reporting',)).fetchone()
        self.assertNotIn(key, row['hash'])
        self.assertNotIn(key, row['details_json'])
        self.assertNotEqual(row['hash'], '')

    def test_unknown_endpoints_require_admin_not_just_any_key(self):
        scoped = self.create_key('scoped', connections=['a'])
        headers = {'X-API-Key': scoped}
        cases = [
            ('get', '/api_keys', {}), ('post', '/api_keys', {'json': {'name': 'x'}}),
            ('delete', '/api_keys/scoped', {}), ('patch', '/api_keys/scoped', {'json': {'active': False}}),
            ('get', '/connections', {}), ('patch', '/connections', {'json': {'connections': {}}}),
            ('delete', '/connections/a', {}), ('get', '/list_files', {}),
            ('patch', '/save_sql_to_file', {'json': {}}),
        ]
        for method, path, kwargs in cases:
            res = getattr(self.client, method)(path, headers=headers, **kwargs)
            self.assertEqual(res.status_code, 403, f'{method} {path}')


class AuthenticateLookupTests(AppTestCase):
    def test_the_right_key_is_found_among_many(self):
        # authenticate() looks a secret up by its hash rather than scanning every key
        keys = {f'k{i}': self.create_key(f'k{i}', connections=['a'] if i % 2 else ['b']) for i in range(20)}
        for name in ('k0', 'k7', 'k19'):
            self.assertEqual(apikeys.authenticate(keys[name]).name, name)
        self.assertIsNone(apikeys.authenticate('sk_not-a-real-key'))
        self.client.patch('/api_keys/k7', json={'active': False}, headers=self.admin_headers)
        self.assertIsNone(apikeys.authenticate(keys['k7']))  # an inactive key still fails like a wrong one


class DriverErrorDetailTests(AppTestCase):
    def failing_runner(self, *args, **kwargs):
        raise RuntimeError('no such column: internal_secret_col')

    def test_saved_query_callers_other_than_admin_get_no_driver_detail(self):
        self.save_query('q')
        key = self.create_key('reporting', connections=['a'])
        with mock.patch.dict(engine.RUNNERS, {'sqlite': self.failing_runner}):
            scoped = self.client.get('/q/q', headers={'X-API-Key': key})
            admin = self.client.get('/q/q', headers=self.admin_headers)
            ad_hoc = self.client.post('/execute_sql', json={'sql': 'SELECT 1', 'connection_name': 'a'},
                                      headers={'X-API-Key': key})
        self.assertEqual((scoped.status_code, scoped.get_json()),
                         (500, {'error': 'An error occurred while executing the SQL query'}))
        self.assertEqual(admin.get_json()['detail'], 'no such column: internal_secret_col')
        # /execute_sql's caller wrote the SQL, so it still needs the database's reason
        self.assertEqual(ad_hoc.get_json()['detail'], 'no such column: internal_secret_col')


class ConnectionScopingTests(AppTestCase):
    def test_scoped_key_can_use_its_allowed_connection(self):
        key = self.create_key('reporting', connections=['a'])
        res = self.client.post('/execute_sql', json={'sql': 'SELECT * FROM t', 'connection_name': 'a'},
                               headers={'X-API-Key': key})
        self.assertEqual(res.status_code, 200)

    def test_scoped_key_is_blocked_from_other_connections(self):
        key = self.create_key('reporting', connections=['a'])
        res = self.client.post('/execute_sql', json={'sql': 'SELECT * FROM t', 'connection_name': 'b'},
                               headers={'X-API-Key': key})
        self.assertEqual(res.status_code, 403)

    def test_wildcard_connections_allows_everything(self):
        key = self.create_key('all-access', connections='*')
        for conn in ('a', 'b'):
            res = self.client.post('/execute_sql', json={'sql': 'SELECT * FROM t', 'connection_name': conn},
                                   headers={'X-API-Key': key})
            self.assertEqual(res.status_code, 200, conn)

    def test_omitted_connections_defaults_to_wildcard(self):
        key = self.create_key('default-access', connections=None)
        res = self.client.post('/execute_sql', json={'sql': 'SELECT * FROM t', 'connection_name': 'b'},
                               headers={'X-API-Key': key})
        self.assertEqual(res.status_code, 200)

    def test_saved_query_execution_is_scoped_too(self):
        self.client.patch('/save_sql_to_file', json={'author': 'a', 'description': 'd', 'filename': 'q',
                                                      'sql_query': 'SELECT * FROM t', 'connection_name': 'b'},
                          headers=self.admin_headers)
        key = self.create_key('reporting', connections=['a'])
        self.assertEqual(self.client.get('/q/q', headers={'X-API-Key': key}).status_code, 403)
        self.assertEqual(self.client.get('/q/q', headers=self.admin_headers).status_code, 200)

    def test_schema_endpoint_is_scoped(self):
        key = self.create_key('reporting', connections=['a'])
        self.assertEqual(self.client.get('/connections/a/schema', headers={'X-API-Key': key}).status_code, 200)
        self.assertEqual(self.client.get('/connections/b/schema', headers={'X-API-Key': key}).status_code, 403)


class QueryScopingTests(AppTestCase):
    """A key's `queries` grant: independent of and additive with `connections` (see apikeys.py's module
    docstring) - built for an external-client key that should only reach specific named saved queries, with
    no connection access of its own at all, rather than the coarser connection-wide grant internal keys use."""

    def test_a_query_scoped_key_can_run_its_granted_query_with_no_connection_access_at_all(self):
        self.save_query('monthly_revenue', connection_name='a')
        key = self.create_key('external', connections=[], queries=['monthly_revenue'])
        res = self.client.get('/q/monthly_revenue', headers={'X-API-Key': key})
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))

    def test_a_query_scoped_key_cannot_run_a_query_it_was_not_granted(self):
        self.save_query('monthly_revenue', connection_name='a')
        self.save_query('internal_only', connection_name='a')
        key = self.create_key('external', connections=[], queries=['monthly_revenue'])
        self.assertEqual(self.client.get('/q/internal_only', headers={'X-API-Key': key}).status_code, 403)

    def test_a_query_scoped_key_still_cannot_run_ad_hoc_sql_on_the_same_connection(self):
        self.save_query('monthly_revenue', connection_name='a')
        key = self.create_key('external', connections=[], queries=['monthly_revenue'])
        res = self.client.post('/execute_sql', json={'sql': 'SELECT * FROM t', 'connection_name': 'a'},
                               headers={'X-API-Key': key})
        self.assertEqual(res.status_code, 403)

    def test_wildcard_queries_allows_any_saved_query_but_not_ad_hoc_sql(self):
        self.save_query('q1', connection_name='a')
        self.save_query('q2', connection_name='b')
        key = self.create_key('reader', connections=[], queries='*')
        self.assertEqual(self.client.get('/q/q1', headers={'X-API-Key': key}).status_code, 200)
        self.assertEqual(self.client.get('/q/q2', headers={'X-API-Key': key}).status_code, 200)
        res = self.client.post('/execute_sql', json={'sql': 'SELECT * FROM t', 'connection_name': 'a'},
                               headers={'X-API-Key': key})
        self.assertEqual(res.status_code, 403)

    def test_queries_grant_adds_reach_on_top_of_an_existing_connections_grant(self):
        self.save_query('b_only_query', connection_name='b')
        # 'internal' already has full access to connection 'a' (and so every query on it); the queries
        # grant additionally reaches one specific query on 'b', without granting connection 'b' outright.
        key = self.create_key('internal', connections=['a'], queries=['b_only_query'])
        self.assertEqual(self.client.get('/q/b_only_query', headers={'X-API-Key': key}).status_code, 200)
        res = self.client.post('/execute_sql', json={'sql': 'SELECT * FROM t', 'connection_name': 'b'},
                               headers={'X-API-Key': key})
        self.assertEqual(res.status_code, 403)  # connection 'b' itself is still not granted

    def test_malformed_queries_is_rejected(self):
        res = self.client.post('/api_keys', json={'name': 'x', 'queries': 'not-a-list-or-wildcard'},
                               headers=self.admin_headers)
        self.assertEqual(res.status_code, 400)

    def test_a_key_created_before_this_field_existed_keeps_working_unchanged(self):
        # Simulates a key stored with no 'queries' entry at all, predating this feature.
        self.save_query('legacy_query', connection_name='a')
        key = self.create_key('legacy', connections=['a'])
        with db.transaction() as conn:
            row = conn.execute('SELECT details_json FROM api_keys WHERE name = ?', ('legacy',)).fetchone()
            details = json.loads(row['details_json'])
            del details['queries']
            conn.execute('UPDATE api_keys SET details_json = ? WHERE name = ?',
                        (json.dumps(details), 'legacy'))
        # unchanged: still works via its connection grant, and gains no extra reach from the missing field
        self.assertEqual(self.client.get('/q/legacy_query', headers={'X-API-Key': key}).status_code, 200)

    def test_openapi_catalog_shows_only_what_the_key_can_reach(self):
        self.save_query('monthly_revenue', connection_name='a')
        self.save_query('internal_only', connection_name='a')
        key = self.create_key('external', connections=[], queries=['monthly_revenue'])
        paths = self.client.get('/openapi.json', headers={'X-API-Key': key}).get_json()['paths']
        self.assertIn('/q/monthly_revenue', paths)
        self.assertNotIn('/q/internal_only', paths)
        admin_paths = self.client.get('/openapi.json', headers=self.admin_headers).get_json()['paths']
        self.assertIn('/q/monthly_revenue', admin_paths)
        self.assertIn('/q/internal_only', admin_paths)


class PerQueryWriteCurationTests(AppTestCase):
    """A `queries` entry can be `{"name": ..., "allow_writes": true}` instead of a plain name, adding write
    reach for that one query specifically - on top of, never instead of, the key's blanket allow_writes."""

    def setUp(self):
        super().setUp()
        os.environ['QUERYAPIGATE_ALLOW_WRITES'] = '1'

    def test_a_write_curated_query_can_write_with_no_blanket_write_access_at_all(self):
        self.save_query('submit_order', connection_name='a', sql='INSERT INTO t VALUES (2)')
        key = self.create_key('partner', connections=[], allow_writes=False,
                              queries=[{'name': 'submit_order', 'allow_writes': True}])
        res = self.client.get('/q/submit_order', headers={'X-API-Key': key})
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))

    def test_a_plain_string_entry_grants_no_write_access(self):
        self.save_query('submit_order', connection_name='a', sql='INSERT INTO t VALUES (2)')
        key = self.create_key('partner', connections=[], queries=['submit_order'])
        res = self.client.get('/q/submit_order', headers={'X-API-Key': key})
        self.assertEqual(res.status_code, 403)

    def test_write_curation_does_not_extend_to_a_different_query(self):
        self.save_query('submit_order', connection_name='a', sql='INSERT INTO t VALUES (2)')
        self.save_query('other_write', connection_name='a', sql='DELETE FROM t')
        key = self.create_key('partner', connections=[],
                              queries=[{'name': 'submit_order', 'allow_writes': True}, 'other_write'])
        self.assertEqual(self.client.get('/q/other_write', headers={'X-API-Key': key}).status_code, 403)

    def test_write_curation_never_grants_ad_hoc_sql(self):
        self.save_query('submit_order', connection_name='a', sql='INSERT INTO t VALUES (2)')
        key = self.create_key('partner', connections=[],
                              queries=[{'name': 'submit_order', 'allow_writes': True}])
        res = self.client.post('/execute_sql', json={'sql': 'INSERT INTO t VALUES (3)', 'connection_name': 'a'},
                               headers={'X-API-Key': key})
        self.assertEqual(res.status_code, 403)

    def test_still_capped_by_the_server_wide_setting(self):
        os.environ['QUERYAPIGATE_ALLOW_WRITES'] = ''
        self.save_query('submit_order', connection_name='a', sql='INSERT INTO t VALUES (2)')
        key = self.create_key('partner', connections=[],
                              queries=[{'name': 'submit_order', 'allow_writes': True}])
        res = self.client.get('/q/submit_order', headers={'X-API-Key': key})
        self.assertEqual(res.status_code, 403)

    def test_the_wildcard_never_carries_write_access(self):
        self.save_query('submit_order', connection_name='a', sql='INSERT INTO t VALUES (2)')
        key = self.create_key('reader', connections=[], queries='*')
        res = self.client.get('/q/submit_order', headers={'X-API-Key': key})
        self.assertEqual(res.status_code, 403)

    def test_a_blanket_write_key_can_still_write_through_a_plainly_named_query(self):
        # allow_writes on the key itself is unaffected by any of this - it's an independent, wider grant.
        self.save_query('submit_order', connection_name='a', sql='INSERT INTO t VALUES (2)')
        key = self.create_key('internal', connections=['a'], allow_writes=True, queries=['submit_order'])
        res = self.client.get('/q/submit_order', headers={'X-API-Key': key})
        self.assertEqual(res.status_code, 200)

    def test_patch_can_add_write_curation_to_an_existing_read_only_grant(self):
        self.save_query('submit_order', connection_name='a', sql='INSERT INTO t VALUES (2)')
        key = self.create_key('partner', connections=[], queries=['submit_order'])
        self.client.patch('/api_keys/partner', json={'queries': [{'name': 'submit_order', 'allow_writes': True}]},
                          headers=self.admin_headers)
        res = self.client.get('/q/submit_order', headers={'X-API-Key': key})
        self.assertEqual(res.status_code, 200)

    def test_patch_can_remove_write_curation_reverting_to_read_only(self):
        self.save_query('submit_order', connection_name='a', sql='INSERT INTO t VALUES (2)')
        key = self.create_key('partner', connections=[], queries=[{'name': 'submit_order', 'allow_writes': True}])
        self.client.patch('/api_keys/partner', json={'queries': ['submit_order']}, headers=self.admin_headers)
        res = self.client.get('/q/submit_order', headers={'X-API-Key': key})
        self.assertEqual(res.status_code, 403)

    def test_a_duplicate_query_name_is_rejected(self):
        res = self.client.post('/api_keys', json={'name': 'x', 'queries': ['a', {'name': 'a'}]},
                               headers=self.admin_headers)
        self.assertEqual(res.status_code, 400)

    def test_an_entry_missing_a_name_is_rejected(self):
        res = self.client.post('/api_keys', json={'name': 'x', 'queries': [{'allow_writes': True}]},
                               headers=self.admin_headers)
        self.assertEqual(res.status_code, 400)

    def test_an_entry_with_unexpected_fields_is_rejected(self):
        res = self.client.post('/api_keys', json={'name': 'x', 'queries': [{'name': 'a', 'extra': 1}]},
                               headers=self.admin_headers)
        self.assertEqual(res.status_code, 400)

    def test_the_listing_shows_the_object_form_only_for_write_curated_entries(self):
        self.create_key('partner', connections=[],
                        queries=['read_one', {'name': 'write_one', 'allow_writes': True}])
        listed = self.client.get('/api_keys', headers=self.admin_headers).get_json()['keys']['partner']
        self.assertEqual(listed['queries'], ['read_one', {'name': 'write_one', 'allow_writes': True}])

    def test_the_admin_key_can_write_through_any_query_regardless_of_curation(self):
        self.save_query('submit_order', connection_name='a', sql='INSERT INTO t VALUES (2)')
        res = self.client.get('/q/submit_order', headers=self.admin_headers)
        self.assertEqual(res.status_code, 200)


class KeyExpiryTests(AppTestCase):
    """A key's optional expires_at (YYYY-MM-DD, day granularity) - checked live on every authenticate()
    call, not swept by a background job (see apikeys.is_expired())."""

    def yesterday(self):
        return (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')

    def today(self):
        return datetime.now().strftime('%Y-%m-%d')

    def tomorrow(self):
        return (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d')

    def test_an_expired_key_is_rejected(self):
        key = self.create_key('expired', connections=['a'], expires_at=self.yesterday())
        res = self.client.post('/execute_sql', json={'sql': 'SELECT * FROM t', 'connection_name': 'a'},
                               headers={'X-API-Key': key})
        self.assertEqual(res.status_code, 401)

    def test_a_key_expiring_today_still_works_through_the_end_of_the_day(self):
        # expires_at is valid through 23:59:59 of that date, not from its start - "expires today" should
        # not mean "already expired".
        key = self.create_key('expires-today', connections=['a'], expires_at=self.today())
        res = self.client.post('/execute_sql', json={'sql': 'SELECT * FROM t', 'connection_name': 'a'},
                               headers={'X-API-Key': key})
        self.assertEqual(res.status_code, 200)

    def test_a_key_expiring_in_the_future_still_works(self):
        key = self.create_key('future', connections=['a'], expires_at=self.tomorrow())
        res = self.client.post('/execute_sql', json={'sql': 'SELECT * FROM t', 'connection_name': 'a'},
                               headers={'X-API-Key': key})
        self.assertEqual(res.status_code, 200)

    def test_a_key_with_no_expiry_never_expires(self):
        key = self.create_key('forever', connections=['a'])
        res = self.client.post('/execute_sql', json={'sql': 'SELECT * FROM t', 'connection_name': 'a'},
                               headers={'X-API-Key': key})
        self.assertEqual(res.status_code, 200)

    def test_malformed_expiry_is_rejected(self):
        for bad in ('not-a-date', '2026/10/15', '2026-13-40', 15):
            res = self.client.post('/api_keys', json={'name': 'x', 'expires_at': bad}, headers=self.admin_headers)
            self.assertEqual(res.status_code, 400, bad)

    def test_expires_at_appears_in_the_listing(self):
        self.create_key('reporting', connections=['a'], expires_at=self.tomorrow())
        listed = self.client.get('/api_keys', headers=self.admin_headers).get_json()['keys']['reporting']
        self.assertEqual(listed['expires_at'], self.tomorrow())

    def test_clearing_expiry_via_explicit_null_un_expires_a_key(self):
        key = self.create_key('was-expired', connections=['a'], expires_at=self.yesterday())
        self.assertEqual(self.client.patch('/api_keys/was-expired', json={'expires_at': None},
                                           headers=self.admin_headers).status_code, 200)
        res = self.client.post('/execute_sql', json={'sql': 'SELECT * FROM t', 'connection_name': 'a'},
                               headers={'X-API-Key': key})
        self.assertEqual(res.status_code, 200)

    def test_patch_without_expires_at_leaves_it_unchanged(self):
        self.create_key('reporting', connections=['a'], expires_at=self.tomorrow())
        self.client.patch('/api_keys/reporting', json={'allow_writes': True}, headers=self.admin_headers)
        listed = self.client.get('/api_keys', headers=self.admin_headers).get_json()['keys']['reporting']
        self.assertEqual(listed['expires_at'], self.tomorrow())
        self.assertTrue(listed['allow_writes'])

    def test_patch_can_set_an_expiry_on_a_key_that_had_none(self):
        key = self.create_key('reporting', connections=['a'])
        self.client.patch('/api_keys/reporting', json={'expires_at': self.yesterday()}, headers=self.admin_headers)
        res = self.client.post('/execute_sql', json={'sql': 'SELECT * FROM t', 'connection_name': 'a'},
                               headers={'X-API-Key': key})
        self.assertEqual(res.status_code, 401)


class LastUsedTests(AppTestCase):
    """A key's last_used_at, recorded by apikeys._record_use() on every authenticate() match but throttled
    to once per apikeys._USE_RECORD_INTERVAL, so a busy key doesn't turn every request into a disk write."""

    def listed(self, name='reporting'):
        return self.client.get('/api_keys', headers=self.admin_headers).get_json()['keys'][name]

    def test_a_never_used_key_has_no_last_used_at(self):
        self.create_key('reporting', connections=['a'])
        self.assertNotIn('last_used_at', self.listed())

    def test_first_use_records_last_used_at(self):
        key = self.create_key('reporting', connections=['a'])
        self.client.post('/execute_sql', json={'sql': 'SELECT * FROM t', 'connection_name': 'a'},
                         headers={'X-API-Key': key})
        self.assertIn('last_used_at', self.listed())

    def test_first_use_records_even_when_the_host_booted_seconds_ago(self):
        # time.monotonic() counts from an undefined point - on Linux, boot - and CI runners are fresh VMs, so the
        # clock can read a few seconds. A "never recorded" default of 0.0 then looks like a write a moment ago, and
        # the first use was throttled away: last_used_at never appeared. Simulate that clock without waiting.
        key = self.create_key('reporting', connections=['a'])
        fresh_boot = mock.Mock(monotonic=lambda: 5.0)
        with mock.patch.object(apikeys, 'time', fresh_boot):
            self.client.post('/execute_sql', json={'sql': 'SELECT * FROM t', 'connection_name': 'a'},
                             headers={'X-API-Key': key})
            self.assertIn('last_used_at', self.listed())
            with mock.patch.object(apikeys, '_upsert') as spy:  # and the throttle still holds on that clock
                for _ in range(3):
                    self.client.post('/execute_sql', json={'sql': 'SELECT * FROM t', 'connection_name': 'a'},
                                     headers={'X-API-Key': key})
                self.assertEqual(spy.call_count, 0)

    def test_rapid_reuse_is_throttled_to_one_write(self):
        key = self.create_key('reporting', connections=['a'])
        with mock.patch.object(apikeys, '_upsert') as spy:
            for _ in range(5):
                self.client.post('/execute_sql', json={'sql': 'SELECT * FROM t', 'connection_name': 'a'},
                                 headers={'X-API-Key': key})
            self.assertEqual(spy.call_count, 1)

    def test_a_new_write_happens_again_once_the_throttle_window_passes(self):
        key = self.create_key('reporting', connections=['a'])
        self.client.post('/execute_sql', json={'sql': 'SELECT * FROM t', 'connection_name': 'a'},
                         headers={'X-API-Key': key})
        first = self.listed()['last_used_at']
        # simulate the throttle window having elapsed, rather than sleeping the test for real
        apikeys._last_recorded_use['reporting'] = time.monotonic() - apikeys._USE_RECORD_INTERVAL - 1
        with mock.patch.object(apikeys, '_upsert', wraps=apikeys._upsert) as spy:
            self.client.post('/execute_sql', json={'sql': 'SELECT * FROM t', 'connection_name': 'a'},
                             headers={'X-API-Key': key})
            self.assertEqual(spy.call_count, 1)
        self.assertIsNotNone(first)

    def test_using_a_different_key_does_not_throttle_this_one(self):
        key_a = self.create_key('key-a', connections=['a'])
        key_b = self.create_key('key-b', connections=['a'])
        self.client.post('/execute_sql', json={'sql': 'SELECT * FROM t', 'connection_name': 'a'},
                         headers={'X-API-Key': key_a})
        with mock.patch.object(apikeys, '_upsert', wraps=apikeys._upsert) as spy:
            self.client.post('/execute_sql', json={'sql': 'SELECT * FROM t', 'connection_name': 'a'},
                             headers={'X-API-Key': key_b})
            self.assertEqual(spy.call_count, 1)

    def test_the_admin_key_has_no_stored_entry_to_record_use_against(self):
        # QUERYAPIGATE_API_KEY is an env var, not a stored key - there is nothing in api_keys.json to write to,
        # and authenticate()'s admin-key branch never calls _record_use() at all.
        self.client.get('/connections', headers=self.admin_headers)
        self.assertEqual(self.client.get('/api_keys', headers=self.admin_headers).get_json()['keys'], {})


class IpAllowlistTests(AppTestCase):
    """A key's optional allowed_ips (IP addresses or CIDR ranges) - checked in authenticate() against the
    same client address rate limiting already resolves (see apikeys._ip_allowed())."""

    def run_as(self, key, remote_addr):
        return self.client.post('/execute_sql', json={'sql': 'SELECT * FROM t', 'connection_name': 'a'},
                                headers={'X-API-Key': key}, environ_overrides={'REMOTE_ADDR': remote_addr})

    def test_a_key_with_no_restriction_works_from_any_address(self):
        key = self.create_key('open', connections=['a'])
        self.assertEqual(self.run_as(key, '198.51.100.9').status_code, 200)

    def test_a_matching_exact_address_is_allowed(self):
        key = self.create_key('pinned', connections=['a'], allowed_ips=['203.0.113.5'])
        self.assertEqual(self.run_as(key, '203.0.113.5').status_code, 200)

    def test_a_non_matching_address_is_rejected(self):
        key = self.create_key('pinned', connections=['a'], allowed_ips=['203.0.113.5'])
        res = self.run_as(key, '203.0.113.6')
        self.assertEqual(res.status_code, 401)

    def test_an_address_inside_an_allowed_cidr_range_is_allowed(self):
        key = self.create_key('pinned', connections=['a'], allowed_ips=['10.0.0.0/8'])
        self.assertEqual(self.run_as(key, '10.42.0.7').status_code, 200)

    def test_an_address_outside_an_allowed_cidr_range_is_rejected(self):
        key = self.create_key('pinned', connections=['a'], allowed_ips=['10.0.0.0/8'])
        self.assertEqual(self.run_as(key, '11.0.0.1').status_code, 401)

    def test_ipv6_addresses_are_supported(self):
        key = self.create_key('pinned', connections=['a'], allowed_ips=['2001:db8::/32'])
        self.assertEqual(self.run_as(key, '2001:db8::1').status_code, 200)
        self.assertEqual(self.run_as(key, '2001:dead::1').status_code, 401)

    def test_malformed_allowed_ips_is_rejected(self):
        for bad in ('203.0.113.5', 123, ['not-an-ip'], ['203.0.113.5/99']):
            res = self.client.post('/api_keys', json={'name': 'x', 'allowed_ips': bad}, headers=self.admin_headers)
            self.assertEqual(res.status_code, 400, bad)

    def test_allowed_ips_appears_in_the_listing(self):
        self.create_key('pinned', connections=['a'], allowed_ips=['203.0.113.5', '10.0.0.0/8'])
        listed = self.client.get('/api_keys', headers=self.admin_headers).get_json()['keys']['pinned']
        self.assertEqual(listed['allowed_ips'], ['10.0.0.0/8', '203.0.113.5'])

    def test_clearing_via_explicit_null_removes_the_restriction(self):
        key = self.create_key('pinned', connections=['a'], allowed_ips=['203.0.113.5'])
        self.assertEqual(self.client.patch('/api_keys/pinned', json={'allowed_ips': None},
                                           headers=self.admin_headers).status_code, 200)
        self.assertEqual(self.run_as(key, '198.51.100.9').status_code, 200)

    def test_patch_without_allowed_ips_leaves_it_unchanged(self):
        key = self.create_key('pinned', connections=['a'], allowed_ips=['203.0.113.5'])
        self.client.patch('/api_keys/pinned', json={'allow_writes': True}, headers=self.admin_headers)
        self.assertEqual(self.run_as(key, '203.0.113.6').status_code, 401)

    def test_patch_can_add_a_restriction_to_a_key_that_had_none(self):
        key = self.create_key('pinned', connections=['a'])
        self.client.patch('/api_keys/pinned', json={'allowed_ips': ['203.0.113.5']}, headers=self.admin_headers)
        self.assertEqual(self.run_as(key, '198.51.100.9').status_code, 401)

    def test_the_admin_key_is_never_restricted_by_a_scoped_keys_allowed_ips(self):
        self.create_key('pinned', connections=['a'], allowed_ips=['203.0.113.5'])
        res = self.client.get('/connections', headers=self.admin_headers, environ_overrides={'REMOTE_ADDR': '9.9.9.9'})
        self.assertEqual(res.status_code, 200)


class AllowedWriteOpsTests(AppTestCase):
    """A key's optional allowed_write_ops (specific write statement keywords) - narrows allow_writes rather
    than replacing it; checked in sqltools.validate_sql() alongside the existing read-only rule."""

    def run_as(self, key, sql):
        return self.client.post('/execute_sql', json={'sql': sql, 'connection_name': 'a'},
                                headers={'X-API-Key': key})

    def setUp(self):
        super().setUp()
        os.environ['QUERYAPIGATE_ALLOW_WRITES'] = '1'

    def test_a_permitted_operation_is_allowed(self):
        key = self.create_key('writer', connections=['a'], allow_writes=True,
                              allowed_write_ops=['insert', 'update'])
        self.assertEqual(self.run_as(key, 'INSERT INTO t VALUES (1)').status_code, 200)

    def test_a_non_permitted_operation_is_rejected(self):
        key = self.create_key('writer', connections=['a'], allow_writes=True,
                              allowed_write_ops=['insert', 'update'])
        res = self.run_as(key, 'DELETE FROM t')
        self.assertEqual(res.status_code, 403)
        self.assertIn('insert', res.get_json()['error'])
        self.assertIn('update', res.get_json()['error'])

    def test_read_only_statements_are_never_restricted_by_this(self):
        key = self.create_key('writer', connections=['a'], allow_writes=True, allowed_write_ops=['insert'])
        self.assertEqual(self.run_as(key, 'SELECT * FROM t').status_code, 200)

    def test_unset_means_any_write_is_permitted(self):
        key = self.create_key('writer', connections=['a'], allow_writes=True)
        self.assertEqual(self.run_as(key, 'DELETE FROM t').status_code, 200)
        self.assertEqual(self.run_as(key, 'DROP TABLE t').status_code, 200)

    def test_the_restriction_never_widens_allow_writes(self):
        key = self.create_key('reader', connections=['a'], allow_writes=False, allowed_write_ops=['insert'])
        self.assertEqual(self.run_as(key, 'INSERT INTO t VALUES (1)').status_code, 403)

    def test_keywords_are_normalised_to_lowercase(self):
        key = self.create_key('writer', connections=['a'], allow_writes=True, allowed_write_ops=['INSERT'])
        self.assertEqual(self.run_as(key, 'insert into t values (1)').status_code, 200)

    def test_malformed_allowed_write_ops_is_rejected(self):
        for bad in ('insert', 123, [1, 2], ['']):
            res = self.client.post('/api_keys', json={'name': 'x', 'allowed_write_ops': bad},
                                   headers=self.admin_headers)
            self.assertEqual(res.status_code, 400, bad)

    def test_allowed_write_ops_appears_in_the_listing(self):
        self.create_key('writer', connections=['a'], allow_writes=True, allowed_write_ops=['update', 'insert'])
        listed = self.client.get('/api_keys', headers=self.admin_headers).get_json()['keys']['writer']
        self.assertEqual(listed['allowed_write_ops'], ['insert', 'update'])

    def test_clearing_via_explicit_null_removes_the_restriction(self):
        key = self.create_key('writer', connections=['a'], allow_writes=True, allowed_write_ops=['insert'])
        self.assertEqual(self.client.patch('/api_keys/writer', json={'allowed_write_ops': None},
                                           headers=self.admin_headers).status_code, 200)
        self.assertEqual(self.run_as(key, 'DELETE FROM t').status_code, 200)

    def test_patch_without_allowed_write_ops_leaves_it_unchanged(self):
        key = self.create_key('writer', connections=['a'], allow_writes=True, allowed_write_ops=['insert'])
        self.client.patch('/api_keys/writer', json={'active': True}, headers=self.admin_headers)
        self.assertEqual(self.run_as(key, 'DELETE FROM t').status_code, 403)

    def test_the_admin_key_is_never_restricted_by_this(self):
        res = self.client.post('/execute_sql', json={'sql': 'DELETE FROM t', 'connection_name': 'a'},
                               headers=self.admin_headers)
        self.assertEqual(res.status_code, 200)


class AllowedTablesTests(AppTestCase):
    """A key's optional allowed_tables (BACKLOG #21's remaining gap) - restricts *every* statement, read or
    write, to a named set of tables; checked in sqltools.validate_sql()/tableguard.py."""

    def setUp(self):
        super().setUp()
        conn = sqlite3.connect(self.db_path)
        conn.execute('CREATE TABLE secret (id INTEGER)')
        conn.execute('INSERT INTO secret VALUES (1)')
        conn.commit()
        conn.close()
        write_connections({
            'a': {'db': 'sqlite', 'database': self.db_path, 'active': True},
            'h2conn': {'db': 'h2', 'host': 'localhost', 'database': 'unused', 'active': True},
        })

    def run_as(self, key, sql, connection_name='a'):
        return self.client.post('/execute_sql', json={'sql': sql, 'connection_name': connection_name},
                                headers={'X-API-Key': key})

    def test_a_query_touching_only_allowed_tables_is_permitted(self):
        key = self.create_key('reader', connections=['a'], allowed_tables=['t'])
        self.assertEqual(self.run_as(key, 'SELECT * FROM t').status_code, 200)

    def test_a_query_touching_a_forbidden_table_is_rejected(self):
        key = self.create_key('reader', connections=['a'], allowed_tables=['t'])
        res = self.run_as(key, 'SELECT * FROM secret')
        self.assertEqual(res.status_code, 403)
        self.assertIn('secret', res.get_json()['error'])

    def test_this_restricts_writes_too_not_just_reads(self):
        os.environ['QUERYAPIGATE_ALLOW_WRITES'] = '1'
        key = self.create_key('writer', connections=['a'], allow_writes=True, allowed_tables=['t'])
        self.assertEqual(self.run_as(key, 'DELETE FROM secret').status_code, 403)
        self.assertEqual(self.run_as(key, 'DELETE FROM t').status_code, 200)

    def test_unset_means_no_restriction(self):
        key = self.create_key('reader', connections=['a'])
        self.assertEqual(self.run_as(key, 'SELECT * FROM secret').status_code, 200)

    def test_table_names_are_normalised_to_lowercase(self):
        key = self.create_key('reader', connections=['a'], allowed_tables=['T'])
        self.assertEqual(self.run_as(key, 'SELECT * FROM t').status_code, 200)

    def test_malformed_allowed_tables_is_rejected(self):
        for bad in ('t', 123, [1, 2], ['']):
            res = self.client.post('/api_keys', json={'name': 'x', 'allowed_tables': bad},
                                   headers=self.admin_headers)
            self.assertEqual(res.status_code, 400, bad)

    def test_allowed_tables_appears_in_the_listing(self):
        self.create_key('reader', connections=['a'], allowed_tables=['b_table', 'a_table'])
        listed = self.client.get('/api_keys', headers=self.admin_headers).get_json()['keys']['reader']
        self.assertEqual(listed['allowed_tables'], ['a_table', 'b_table'])

    def test_clearing_via_explicit_null_removes_the_restriction(self):
        key = self.create_key('reader', connections=['a'], allowed_tables=['t'])
        self.assertEqual(self.client.patch('/api_keys/reader', json={'allowed_tables': None},
                                           headers=self.admin_headers).status_code, 200)
        self.assertEqual(self.run_as(key, 'SELECT * FROM secret').status_code, 200)

    def test_patch_without_allowed_tables_leaves_it_unchanged(self):
        key = self.create_key('reader', connections=['a'], allowed_tables=['t'])
        self.client.patch('/api_keys/reader', json={'active': True}, headers=self.admin_headers)
        self.assertEqual(self.run_as(key, 'SELECT * FROM secret').status_code, 403)

    def test_the_admin_key_is_never_restricted_by_this(self):
        res = self.client.post('/execute_sql', json={'sql': 'SELECT * FROM secret', 'connection_name': 'a'},
                               headers=self.admin_headers)
        self.assertEqual(res.status_code, 200)

    def test_a_restricted_key_against_an_unsupported_dialect_is_always_rejected_not_silently_open(self):
        key = self.create_key('reader', connections=['h2conn'], allowed_tables=['t'])
        res = self.run_as(key, 'SELECT 1', connection_name='h2conn')
        self.assertEqual(res.status_code, 403)
        self.assertIn('h2', res.get_json()['error'])

    def test_a_role_copies_allowed_tables_onto_a_key_created_from_it(self):
        self.client.post('/roles', json={'name': 'reader-role', 'connections': ['a'], 'allowed_tables': ['t']},
                         headers=self.admin_headers)
        secret = self.client.post('/api_keys', json={'name': 'from-role', 'role': 'reader-role'},
                                  headers=self.admin_headers).get_json()['key']
        self.assertEqual(self.run_as(secret, 'SELECT * FROM secret').status_code, 403)
        self.assertEqual(self.run_as(secret, 'SELECT * FROM t').status_code, 200)


class WritePermissionTests(AppTestCase):
    def test_scoped_key_without_allow_writes_is_forced_read_only(self):
        os.environ['QUERYAPIGATE_ALLOW_WRITES'] = '1'
        key = self.create_key('reporting', connections=['a'], allow_writes=False)
        res = self.client.post('/execute_sql', json={'sql': 'DELETE FROM t', 'connection_name': 'a'},
                               headers={'X-API-Key': key})
        self.assertEqual(res.status_code, 403)

    def test_scoped_key_with_allow_writes_can_write_when_the_server_allows_it(self):
        os.environ['QUERYAPIGATE_ALLOW_WRITES'] = '1'
        key = self.create_key('writer', connections=['a'], allow_writes=True)
        res = self.client.post('/execute_sql', json={'sql': 'DELETE FROM t', 'connection_name': 'a'},
                               headers={'X-API-Key': key})
        self.assertEqual(res.status_code, 200)

    def test_the_server_wide_flag_is_still_a_ceiling(self):
        os.environ.pop('QUERYAPIGATE_ALLOW_WRITES', None)
        key = self.create_key('writer', connections=['a'], allow_writes=True)
        res = self.client.post('/execute_sql', json={'sql': 'DELETE FROM t', 'connection_name': 'a'},
                               headers={'X-API-Key': key})
        self.assertEqual(res.status_code, 403)


class AdminKeyBackwardCompatTests(AppTestCase):
    def test_admin_key_keeps_full_access(self):
        self.assertEqual(self.client.get('/connections', headers=self.admin_headers).status_code, 200)
        res = self.client.post('/execute_sql', json={'sql': 'SELECT * FROM t', 'connection_name': 'a'},
                               headers=self.admin_headers)
        self.assertEqual(res.status_code, 200)


class UpdateKeyTests(AppTestCase):
    def test_patch_updates_permissions_without_changing_the_secret(self):
        key = self.create_key('reporting', connections=['a'], allow_writes=False)
        res = self.client.patch('/api_keys/reporting', json={'connections': ['a', 'b']}, headers=self.admin_headers)
        self.assertEqual(res.status_code, 200)
        res = self.client.post('/execute_sql', json={'sql': 'SELECT * FROM t', 'connection_name': 'b'},
                               headers={'X-API-Key': key})
        self.assertEqual(res.status_code, 200)

    def test_deactivating_a_key_blocks_it_immediately(self):
        key = self.create_key('reporting', connections=['a'])
        self.client.patch('/api_keys/reporting', json={'active': False}, headers=self.admin_headers)
        res = self.client.post('/execute_sql', json={'sql': 'SELECT * FROM t', 'connection_name': 'a'},
                               headers={'X-API-Key': key})
        self.assertEqual(res.status_code, 401)

    def test_update_of_an_unknown_key_is_404(self):
        res = self.client.patch('/api_keys/nope', json={'active': False}, headers=self.admin_headers)
        self.assertEqual(res.status_code, 404)


class OpenServerBootstrapTests(unittest.TestCase):
    """No QUERYAPIGATE_API_KEY set at all - the server starts fully open, same as before this feature existed."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop('QUERYAPIGATE_API_KEY', None)
        write_connections({'a': {'db': 'sqlite', 'database': 'x.db', 'active': True}})

    def test_a_fully_open_server_stays_admin_with_no_key_at_all(self):
        client = create_app().test_client()
        self.assertEqual(client.get('/connections').status_code, 200)

    def test_can_bootstrap_a_scoped_key_from_the_open_state(self):
        client = create_app().test_client()
        self.assertEqual(client.post('/api_keys', json={'name': 'first', 'connections': ['a']}).status_code, 200)

    def test_bootstrapping_a_key_immediately_requires_auth_for_everyone(self):
        client = create_app().test_client()
        client.post('/api_keys', json={'name': 'first', 'connections': ['a']})
        self.assertEqual(client.get('/connections').status_code, 401)

    def test_without_an_admin_key_the_scoped_key_itself_cannot_manage_the_server(self):
        client = create_app().test_client()
        key = client.post('/api_keys', json={'name': 'first', 'connections': ['a']}).get_json()['key']
        res = client.get('/connections', headers={'X-API-Key': key})
        self.assertEqual(res.status_code, 403)  # locked out - only QUERYAPIGATE_API_KEY can manage the server


class StartupWarningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop('QUERYAPIGATE_API_KEY', None)
        db.init_schema()
        with db.transaction() as conn:
            apikeys._upsert(conn, 'x', {'hash': 'x', 'connections': '*', 'allow_writes': False, 'active': True,
                                        'created_at': 'now'})

    def test_warns_when_scoped_keys_exist_without_an_admin_key(self):
        with self.assertLogs('queryapigate', level=logging.WARNING) as logs:
            create_app()
        self.assertTrue(any('QUERYAPIGATE_API_KEY' in line for line in logs.output))


class ApiKeysModuleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(db.close)
        os.environ.pop('QUERYAPIGATE_API_KEY', None)
        db.init_schema()

    def test_authenticate_returns_none_for_an_unknown_key(self):
        self.assertIsNone(apikeys.authenticate('nope'))

    def test_authenticate_returns_none_for_an_empty_key(self):
        self.assertIsNone(apikeys.authenticate(''))

    def test_authenticate_matches_the_legacy_admin_key(self):
        os.environ['QUERYAPIGATE_API_KEY'] = 'secret'
        permission = apikeys.authenticate('secret')
        self.assertTrue(permission.admin)
        self.assertEqual(permission.connections, apikeys.ALL_CONNECTIONS)

    def test_can_use(self):
        admin = apikeys.Permission(name=None, admin=True, connections=frozenset(), allow_writes=True, queries=[],
                                   rate_limit=None, allowed_write_ops=None)
        self.assertTrue(apikeys.can_use(admin, 'anything'))
        scoped = apikeys.Permission(name='k', admin=False, connections=frozenset({'a'}), allow_writes=False,
                                    queries=[], rate_limit=None, allowed_write_ops=None)
        self.assertTrue(apikeys.can_use(scoped, 'a'))
        self.assertFalse(apikeys.can_use(scoped, 'b'))
        wildcard = apikeys.Permission(name='k', admin=False, connections=apikeys.ALL_CONNECTIONS, allow_writes=False,
                                      queries=[], rate_limit=None, allowed_write_ops=None)
        self.assertTrue(apikeys.can_use(wildcard, 'anything'))

    def test_can_use_query(self):
        scoped = apikeys.Permission(name='k', admin=False, connections=frozenset(), allow_writes=False,
                                    queries=frozenset({'monthly_revenue'}), rate_limit=None, allowed_write_ops=None)
        self.assertTrue(apikeys.can_use_query(scoped, 'monthly_revenue'))
        self.assertFalse(apikeys.can_use_query(scoped, 'other_query'))
        wildcard = apikeys.Permission(name='k', admin=False, connections=frozenset(), allow_writes=False,
                                      queries=apikeys.ALL_QUERIES, rate_limit=None, allowed_write_ops=None)
        self.assertTrue(apikeys.can_use_query(wildcard, 'anything'))
        admin = apikeys.Permission(name=None, admin=True, connections=frozenset(), allow_writes=True, queries=[],
                                   rate_limit=None, allowed_write_ops=None)
        self.assertTrue(apikeys.can_use_query(admin, 'anything'))

    def test_can_write_query(self):
        curated = apikeys.Permission(name='k', admin=False, connections=frozenset(), allow_writes=False,
                                     queries={'submit_order': True, 'read_one': False}, rate_limit=None,
                                     allowed_write_ops=None)
        self.assertTrue(apikeys.can_write_query(curated, 'submit_order'))
        self.assertFalse(apikeys.can_write_query(curated, 'read_one'))
        self.assertFalse(apikeys.can_write_query(curated, 'never_granted'))
        wildcard = apikeys.Permission(name='k', admin=False, connections=frozenset(), allow_writes=False,
                                      queries=apikeys.ALL_QUERIES, rate_limit=None, allowed_write_ops=None)
        self.assertFalse(apikeys.can_write_query(wildcard, 'anything'))  # "*" never implies write - see #14
        admin = apikeys.Permission(name=None, admin=True, connections=frozenset(), allow_writes=True, queries=[],
                                   rate_limit=None, allowed_write_ops=None)
        self.assertTrue(apikeys.can_write_query(admin, 'anything'))

    def test_is_expired(self):
        yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
        tomorrow = (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d')
        today = datetime.now().strftime('%Y-%m-%d')
        self.assertTrue(apikeys.is_expired({'expires_at': yesterday}))
        self.assertFalse(apikeys.is_expired({'expires_at': tomorrow}))
        self.assertFalse(apikeys.is_expired({'expires_at': today}))  # valid through end of its own day
        self.assertFalse(apikeys.is_expired({}))
        self.assertFalse(apikeys.is_expired({'expires_at': None}))


class CatalogTests(AppTestCase):
    """GET /catalog (#26): everything a caller can reach through /q/<name>, and the terms it's offered
    under - assembled from data that already exists (cache_ttl, a key's own rate_limit, per-query write
    curation), scoped by the same reachability rule /openapi.json already uses."""

    def test_admin_sees_every_query_with_its_governance_facts(self):
        self.save_query('report', connection_name='a', sql='SELECT * FROM t')
        self.client.patch('/save_sql_to_file', json={'author': 'a', 'description': 'd', 'filename': 'report',
                                                      'sql_query': 'SELECT * FROM t', 'connection_name': 'a',
                                                      'cache_ttl': 60}, headers=self.admin_headers)
        entries = {e['name']: e for e in self.client.get('/catalog', headers=self.admin_headers)
                  .get_json()['queries']}
        self.assertEqual(entries['report']['connection_name'], 'a')
        self.assertEqual(entries['report']['cache_ttl'], 60)
        self.assertTrue(entries['report']['can_write'])  # admin can write anywhere

    def test_a_query_with_no_cache_ttl_reports_null_not_zero_or_missing(self):
        self.save_query('plain', connection_name='a')
        entries = {e['name']: e for e in self.client.get('/catalog', headers=self.admin_headers)
                  .get_json()['queries']}
        self.assertIsNone(entries['plain']['cache_ttl'])

    def test_a_key_scoped_to_specific_queries_sees_only_those(self):
        self.save_query('visible', connection_name='a')
        self.save_query('hidden', connection_name='a')
        key = self.create_key('scoped', connections=[], queries=['visible'])
        names = {e['name'] for e in self.client.get('/catalog', headers={'X-API-Key': key}).get_json()['queries']}
        self.assertEqual(names, {'visible'})

    def test_a_key_scoped_to_a_connection_sees_every_query_on_it(self):
        self.save_query('one', connection_name='a')
        self.save_query('two', connection_name='a')
        self.save_query('elsewhere', connection_name='b')
        key = self.create_key('scoped', connections=['a'])
        names = {e['name'] for e in self.client.get('/catalog', headers={'X-API-Key': key}).get_json()['queries']}
        self.assertEqual(names, {'one', 'two'})

    def test_per_query_write_curation_is_reflected_per_query(self):
        os.environ['QUERYAPIGATE_ALLOW_WRITES'] = '1'
        self.save_query('submit_order', connection_name='a', sql='INSERT INTO t VALUES (2)')
        self.save_query('read_only', connection_name='a')
        key = self.create_key('partner', connections=[],
                              queries=[{'name': 'submit_order', 'allow_writes': True}, 'read_only'])
        entries = {e['name']: e for e in self.client.get('/catalog', headers={'X-API-Key': key})
                  .get_json()['queries']}
        self.assertTrue(entries['submit_order']['can_write'])
        self.assertFalse(entries['read_only']['can_write'])

    def test_caller_reflects_the_calling_keys_own_terms(self):
        key = self.create_key('scoped', connections=['a'], allow_writes=True, allowed_write_ops=['insert'],
                              allowed_tables=['orders'])
        self.client.patch('/api_keys/scoped', json={'rate_limit': '50/minute'}, headers=self.admin_headers)
        caller = self.client.get('/catalog', headers={'X-API-Key': key}).get_json()['caller']
        self.assertEqual(caller['name'], 'scoped')
        self.assertFalse(caller['admin'])
        self.assertTrue(caller['allow_writes'])
        self.assertEqual(caller['allowed_write_ops'], ['insert'])
        self.assertEqual(caller['allowed_tables'], ['orders'])
        self.assertEqual(caller['rate_limit'], '50/minute')

    def test_caller_allowed_tables_is_null_when_the_key_has_no_restriction(self):
        key = self.create_key('scoped', connections=['a'])
        caller = self.client.get('/catalog', headers={'X-API-Key': key}).get_json()['caller']
        self.assertIsNone(caller['allowed_tables'])

    def test_caller_rate_limit_is_null_when_the_key_has_none_of_its_own(self):
        key = self.create_key('scoped', connections=['a'])
        caller = self.client.get('/catalog', headers={'X-API-Key': key}).get_json()['caller']
        self.assertIsNone(caller['rate_limit'])

    def test_server_rate_limit_reflects_the_env_setting(self):
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_RATE_LIMIT': '100/hour'}):
            caller = self.client.get('/catalog', headers=self.admin_headers).get_json()['caller']
        self.assertEqual(caller['server_rate_limit'], '100/hour')

    def test_admin_caller_has_no_key_specific_rate_limit(self):
        caller = self.client.get('/catalog', headers=self.admin_headers).get_json()['caller']
        self.assertIsNone(caller['rate_limit'])

    def test_requires_authentication_like_any_other_functional_endpoint(self):
        self.assertEqual(self.client.get('/catalog').status_code, 401)
        self.assertEqual(self.client.get('/catalog', headers={'X-API-Key': 'wrong'}).status_code, 401)
        self.assertFalse(apikeys.is_expired({'expires_at': 'garbage'}))  # malformed shouldn't lock the key out

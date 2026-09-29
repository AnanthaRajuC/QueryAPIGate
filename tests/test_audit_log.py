"""Tests for the audit log: a durable record of administrative changes (store.record_audit()),
distinct from execution_history (query runs) and the ephemeral access log."""
import json
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from queryapigate import config, create_app, db, store


class AppTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        tmp = self.tmp.name
        self.db_path = os.path.join(tmp, 'test.db')
        conn = sqlite3.connect(self.db_path)
        conn.execute('CREATE TABLE t (id INTEGER)')
        conn.commit()
        conn.close()
        patcher = mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': tmp, 'QUERYAPIGATE_API_KEY': 'admin-key'})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = create_app().test_client()
        self.admin_headers = {'X-API-Key': 'admin-key'}

    def entries(self):
        return self.client.get('/audit_log', headers=self.admin_headers).get_json()['entries']

    def create_connection(self, name='a', **fields):
        body = {'db': 'sqlite', 'database': self.db_path, 'active': True, **fields}
        res = self.client.patch('/connections', json={'connections': {name: body}}, headers=self.admin_headers)
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))

    def create_key(self, name='scoped', **fields):
        res = self.client.post('/api_keys', json={'name': name, **fields}, headers=self.admin_headers)
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        return res.get_json()['key']

    def save_query(self, filename='q', **fields):
        body = {'author': 'a', 'description': 'd', 'filename': filename, 'sql_query': 'SELECT 1', **fields}
        res = self.client.patch('/save_sql_to_file', json=body, headers=self.admin_headers)
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))


class AdminOnlyTests(AppTestCase):
    def test_a_scoped_key_cannot_read_the_audit_log(self):
        scoped = self.create_key('scoped', connections=[])
        res = self.client.get('/audit_log', headers={'X-API-Key': scoped})
        self.assertEqual(res.status_code, 403)

    def test_anonymous_is_unauthorized_once_a_key_is_configured(self):
        self.assertEqual(self.client.get('/audit_log').status_code, 401)


class ApiKeyAuditTests(AppTestCase):
    def test_create_is_recorded_with_the_new_grant(self):
        self.create_key('reporting', connections=['a'], allow_writes=False)
        entry = self.entries()[0]
        self.assertEqual((entry['action'], entry['target'], entry['actor']), ('create_key', 'reporting', 'admin'))
        self.assertEqual(entry['changes']['connections'], ['a'])
        self.assertNotIn('hash', entry['changes'])

    def test_update_is_recorded_as_a_diff_of_only_the_changed_fields(self):
        self.create_key('reporting', connections=['a'], allow_writes=False)
        self.client.patch('/api_keys/reporting', json={'allow_writes': True}, headers=self.admin_headers)
        entry = self.entries()[0]
        self.assertEqual(entry['action'], 'update_key')
        self.assertEqual(entry['changes'], {'allow_writes': {'from': False, 'to': True}})

    def test_a_no_op_update_records_nothing_new(self):
        self.create_key('reporting', connections=['a'])
        before = len(self.entries())
        self.client.patch('/api_keys/reporting', json={'connections': ['a']}, headers=self.admin_headers)
        self.assertEqual(len(self.entries()), before)

    def test_delete_is_recorded_with_what_the_key_could_still_do(self):
        self.create_key('reporting', connections=['a'], allow_writes=True)
        self.client.delete('/api_keys/reporting', headers=self.admin_headers)
        entry = self.entries()[0]
        self.assertEqual(entry['action'], 'delete_key')
        self.assertEqual(entry['changes']['connections'], ['a'])
        self.assertTrue(entry['changes']['allow_writes'])

    def test_the_scoped_keys_own_secret_never_appears(self):
        secret = self.create_key('reporting', connections=['a'])
        body = json.dumps(self.entries())
        self.assertNotIn(secret, body)


class ConnectionAuditTests(AppTestCase):
    def test_create_is_recorded_with_the_password_masked(self):
        self.create_connection('a', password='super-secret')
        entry = self.entries()[0]
        self.assertEqual((entry['action'], entry['target']), ('create_connection', 'a'))
        self.assertEqual(entry['changes']['password'], config.PASSWORD_MASK)
        self.assertNotIn('super-secret', json.dumps(entry))

    def test_a_changed_password_is_reported_as_changed_never_as_a_value(self):
        self.create_connection('a', password='first-secret')
        self.create_connection('a', password='second-secret')
        entry = self.entries()[0]
        self.assertEqual(entry['action'], 'update_connection')
        self.assertEqual(entry['changes']['password'], 'changed')
        body = json.dumps(entry)
        self.assertNotIn('first-secret', body)
        self.assertNotIn('second-secret', body)

    def test_an_unchanged_password_is_not_reported_as_changed(self):
        self.create_connection('a', password='same-secret')
        self.create_connection('a', password='same-secret', active=False)
        entry = self.entries()[0]
        self.assertEqual(entry['changes'], {'active': {'from': True, 'to': False}})

    def test_a_non_secret_field_change_shows_actual_values(self):
        self.create_connection('a', active=True)
        self.create_connection('a', active=False)
        entry = self.entries()[0]
        self.assertEqual(entry['changes'], {'active': {'from': True, 'to': False}})

    def test_delete_is_recorded_with_the_password_masked(self):
        self.create_connection('a', password='super-secret')
        self.client.delete('/connections/a', json={'reason': 'retiring this database'}, headers=self.admin_headers)
        entry = self.entries()[0]
        self.assertEqual(entry['action'], 'delete_connection')
        self.assertEqual(entry['changes']['password'], config.PASSWORD_MASK)
        self.assertEqual(entry['changes']['deleted_reason'], 'retiring this database')
        self.assertNotIn('super-secret', json.dumps(entry))

    def test_delete_without_a_reason_is_rejected_and_not_recorded(self):
        self.create_connection('a')
        res = self.client.delete('/connections/a', headers=self.admin_headers)
        self.assertEqual(res.status_code, 400)
        self.assertEqual(self.entries()[0]['action'], 'create_connection')  # no delete entry was ever added

    def test_an_env_var_password_reference_is_shown_as_written(self):
        self.create_connection('a', password='${SOME_VAR}')
        entry = self.entries()[0]
        self.assertEqual(entry['changes']['password'], '${SOME_VAR}')


class SavedQueryAuditTests(AppTestCase):
    def test_save_is_recorded_with_the_new_version(self):
        self.save_query('q1', connection_name='')
        self.save_query('q1', connection_name='')
        entries = self.entries()
        self.assertEqual([e['changes']['version'] for e in entries if e['target'] == 'q1'], [2, 1])

    def test_delete_is_recorded(self):
        self.save_query('q1', connection_name='')
        self.client.delete('/saved_sql/q1', headers=self.admin_headers)
        entry = self.entries()[0]
        self.assertEqual((entry['action'], entry['target']), ('delete_query', 'q1'))

    def test_delete_of_one_version_is_recorded_with_that_version_number(self):
        self.save_query('q1', connection_name='')
        self.save_query('q1', connection_name='')
        self.client.delete('/saved_sql/q1?version=1', headers=self.admin_headers)
        entry = self.entries()[0]
        self.assertEqual(entry['changes'], {'version': 1})


class OrderingAndCapTests(AppTestCase):
    def test_newest_entry_is_first(self):
        self.create_key('one', connections=[])
        self.create_key('two', connections=[])
        entries = self.entries()
        self.assertEqual(entries[0]['target'], 'two')
        self.assertEqual(entries[1]['target'], 'one')

    def test_the_log_is_capped(self):
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_AUDIT_LOG_LIMIT': '3'}):
            for i in range(5):
                self.create_key(f'k{i}', connections=[])
        entries = self.entries()
        self.assertEqual(len(entries), 3)
        self.assertEqual([e['target'] for e in entries], ['k4', 'k3', 'k2'])

    def test_a_write_failure_never_raises(self):
        # record_audit() is called after the action it's recording has already succeeded (see app.py's
        # route handlers) - a disk error writing the audit_log table specifically must not surface as a
        # 500, the same trade-off store.record_execution() already makes for query-run history.
        # sqlite3.Connection is a C type (its methods can't be monkeypatched directly), so this wraps it in
        # a thin proxy instead - transparent for everything except the one statement being made flaky, the
        # same technique test_collections.py's move_collection() interruption test already established.
        real_conn = db.connection()

        class FlakyConn:
            def execute(self, sql, *args, **kwargs):
                if sql.strip().startswith('INSERT INTO audit_log'):
                    raise sqlite3.OperationalError('disk full')
                return real_conn.execute(sql, *args, **kwargs)

            def __getattr__(self, attr):
                return getattr(real_conn, attr)

        with mock.patch.object(db, 'connection', return_value=FlakyConn()):
            store.record_audit('admin', 'create_key', 'reporting', {'connections': ['a']})


class ConfigurableLimitTests(unittest.TestCase):
    """config.audit_log_limit(): QUERYAPIGATE_AUDIT_LOG_LIMIT replaces the hardcoded default (#30)."""

    def test_defaults_to_the_module_constant(self):
        os.environ.pop('QUERYAPIGATE_AUDIT_LOG_LIMIT', None)
        self.assertEqual(config.audit_log_limit(), config.AUDIT_LOG_LIMIT)

    def test_an_explicit_value_overrides_the_default(self):
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_AUDIT_LOG_LIMIT': '5000'}):
            self.assertEqual(config.audit_log_limit(), 5000)

    def test_malformed_values_are_rejected_at_startup(self):
        for bad in ('0', '-1', 'many', '3.5'):
            with mock.patch.dict(os.environ, {'QUERYAPIGATE_AUDIT_LOG_LIMIT': bad}):
                with self.assertRaises(ValueError, msg=bad):
                    config.check_settings()

    def test_a_valid_value_passes_startup_validation(self):
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_AUDIT_LOG_LIMIT': '10'}):
            config.check_settings()  # must not raise


class ExportFileTests(AppTestCase):
    """QUERYAPIGATE_AUDIT_LOG_EXPORT_FILE (#30): a second, never-capped copy for retention beyond
    audit_log.json's own rolling window - independently fault-tolerant from the primary write."""

    def test_unset_by_default(self):
        os.environ.pop('QUERYAPIGATE_AUDIT_LOG_EXPORT_FILE', None)
        self.assertIsNone(config.audit_log_export_file())

    def test_every_entry_is_appended_as_one_json_object_per_line(self):
        export_path = os.path.join(self.tmp.name, 'audit-export.jsonl')
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_AUDIT_LOG_EXPORT_FILE': export_path}):
            self.create_key('one', connections=[])
            self.create_key('two', connections=[])
        with open(export_path) as f:
            lines = f.read().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual([json.loads(line)['target'] for line in lines], ['one', 'two'])

    def test_export_is_never_capped_even_when_the_main_log_is(self):
        export_path = os.path.join(self.tmp.name, 'audit-export.jsonl')
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_AUDIT_LOG_EXPORT_FILE': export_path,
                                          'QUERYAPIGATE_AUDIT_LOG_LIMIT': '2'}):
            for i in range(5):
                self.create_key(f'k{i}', connections=[])
        self.assertEqual(len(self.entries()), 2)  # the capped store
        with open(export_path) as f:
            self.assertEqual(len(f.read().splitlines()), 5)  # the export, uncapped

    def test_a_broken_export_path_does_not_stop_the_primary_write(self):
        bad_path = os.path.join(self.tmp.name, 'no-such-directory', 'export.jsonl')
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_AUDIT_LOG_EXPORT_FILE': bad_path}):
            self.create_key('reporting', connections=[])
        self.assertEqual(len(self.entries()), 1)

    def test_a_broken_primary_write_does_not_stop_the_export(self):
        export_path = os.path.join(self.tmp.name, 'audit-export.jsonl')
        real_conn = db.connection()

        class FlakyConn:
            def execute(self, sql, *args, **kwargs):
                if sql.strip().startswith('INSERT INTO audit_log'):
                    raise sqlite3.OperationalError('disk full')
                return real_conn.execute(sql, *args, **kwargs)

            def __getattr__(self, attr):
                return getattr(real_conn, attr)

        with mock.patch.dict(os.environ, {'QUERYAPIGATE_AUDIT_LOG_EXPORT_FILE': export_path}), \
             mock.patch.object(db, 'connection', return_value=FlakyConn()):
            store.record_audit('admin', 'create_key', 'reporting', {'connections': ['a']})
        with open(export_path) as f:
            self.assertEqual(len(f.read().splitlines()), 1)


if __name__ == '__main__':
    unittest.main()

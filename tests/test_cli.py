"""Tests for the CLI - particularly `queryapigate export` (#31): running a saved query and writing the full
result to a file, entirely in-process, for a cron/systemd/Kubernetes CronJob to call."""
import io
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from unittest import mock

from queryapigate import cli, config
from tests.helpers import write_connections


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = self.tmp.name
        self.db_path = os.path.join(self.home, 'test.db')
        conn = sqlite3.connect(self.db_path)
        conn.execute('CREATE TABLE t (id INTEGER, name TEXT)')
        conn.executemany('INSERT INTO t VALUES (?, ?)', [(1, 'a'), (2, 'b'), (3, 'c')])
        conn.commit()
        conn.close()
        patcher = mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': self.home})
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ('QUERYAPIGATE_STREAM_MAX_ROWS', 'QUERYAPIGATE_QUERY_TIMEOUT', 'QUERYAPIGATE_AUDIT_LOG_LIMIT'):
            os.environ.pop(name, None)
        self._write_connections({'lite': {'db': 'sqlite', 'database': self.db_path, 'active': True}})
        os.makedirs(os.path.join(self.home, 'saved_sql'))
        self.save('all_rows', 'SELECT id, name FROM t ORDER BY id', connection_name='lite')

    def _write_connections(self, connections):
        write_connections(connections)

    def save(self, filename, sql, connection_name=None, query_parameters=None):
        entry = {'1': {'sql_query': sql, 'author': 'a', 'description': 'd', 'connection_name': connection_name,
                       'query_parameters': query_parameters, 'status': 'active', 'version': 1,
                       'execution_history': []}}
        with open(os.path.join(self.home, 'saved_sql', f'{filename}.json'), 'w') as f:
            json.dump(entry, f)

    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_writes_the_full_result_as_csv(self):
        out_path = os.path.join(self.home, 'out.csv')
        code, out, err = self.run_cli(['export', 'all_rows', '--out', out_path])
        self.assertEqual(code, 0, err)
        self.assertIn('Wrote 3 rows', out)
        with open(out_path) as f:
            self.assertEqual(f.read().splitlines(), ['id,name', '1,a', '2,b', '3,c'])

    def test_date_and_name_placeholders_are_resolved(self):
        code, _, err = self.run_cli(['export', 'all_rows', '--out', os.path.join(self.home, '{name}_{date}.csv')])
        self.assertEqual(code, 0, err)
        expected = os.path.join(self.home, f"all_rows_{datetime.now().strftime('%Y-%m-%d')}.csv")
        self.assertTrue(os.path.exists(expected))

    def test_tsv_format(self):
        out_path = os.path.join(self.home, 'out.tsv')
        code, _, err = self.run_cli(['export', 'all_rows', '--format', 'tsv', '--out', out_path])
        self.assertEqual(code, 0, err)
        with open(out_path) as f:
            self.assertEqual(f.read().splitlines(), ['id\tname', '1\ta', '2\tb', '3\tc'])

    def test_ndjson_format(self):
        out_path = os.path.join(self.home, 'out.ndjson')
        code, _, err = self.run_cli(['export', 'all_rows', '--format', 'ndjson', '--out', out_path])
        self.assertEqual(code, 0, err)
        with open(out_path) as f:
            lines = [json.loads(line) for line in f.read().splitlines()]
        self.assertEqual(lines, [{'id': 1, 'name': 'a'}, {'id': 2, 'name': 'b'}, {'id': 3, 'name': 'c'}])

    def test_unknown_query_fails_cleanly(self):
        code, _, err = self.run_cli(['export', 'nope', '--out', os.path.join(self.home, 'x.csv')])
        self.assertEqual(code, 1)
        self.assertIn('queryapigate export:', err)

    def test_unknown_out_placeholder_is_rejected(self):
        code, _, err = self.run_cli(['export', 'all_rows', '--out', os.path.join(self.home, '{bogus}.csv')])
        self.assertEqual(code, 1)
        self.assertIn('unknown placeholder', err)

    def test_a_failed_run_leaves_no_partial_or_final_file(self):
        out_path = os.path.join(self.home, 'broken.csv')
        code, _, err = self.run_cli(['export', 'all_rows', '--connection', 'nope', '--out', out_path])
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists(out_path))
        self.assertFalse(any(name.endswith('.part') for name in os.listdir(self.home)))

    def test_a_missing_required_param_fails_cleanly_with_no_file_written(self):
        self.save('needs_param', 'SELECT * FROM t WHERE id = :id', connection_name='lite',
                  query_parameters={'id': {'type': 'int', 'required': True}})
        out_path = os.path.join(self.home, 'x.csv')
        code, _, err = self.run_cli(['export', 'needs_param', '--out', out_path])
        self.assertEqual(code, 1)
        self.assertIn('id is required', err)
        self.assertFalse(os.path.exists(out_path))

    def test_param_flag_supplies_a_value(self):
        self.save('needs_param', 'SELECT * FROM t WHERE id = :id', connection_name='lite',
                  query_parameters={'id': {'type': 'int', 'required': True}})
        out_path = os.path.join(self.home, 'x.csv')
        code, _, err = self.run_cli(['export', 'needs_param', '--param', 'id=2', '--out', out_path])
        self.assertEqual(code, 0, err)
        with open(out_path) as f:
            self.assertEqual(f.read().splitlines(), ['id,name', '2,b'])

    def test_malformed_param_flag_is_rejected(self):
        code, _, err = self.run_cli(['export', 'all_rows', '--param', 'novalue',
                                     '--out', os.path.join(self.home, 'x.csv')])
        self.assertEqual(code, 1)
        self.assertIn('--param must look like name=value', err)

    def test_a_malformed_server_setting_fails_at_startup_before_anything_runs(self):
        out_path = os.path.join(self.home, 'x.csv')
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_STREAM_MAX_ROWS': 'bogus'}):
            code, _, err = self.run_cli(['export', 'all_rows', '--out', out_path])
        self.assertEqual(code, 2)
        self.assertFalse(os.path.exists(out_path))

    def test_connection_flag_overrides_the_saved_querys_own_default(self):
        self._write_connections({
            'lite': {'db': 'sqlite', 'database': self.db_path, 'active': True},
            'other': {'db': 'sqlite', 'database': self.db_path, 'active': True},
        })
        self.save('no_default_conn', 'SELECT id FROM t ORDER BY id LIMIT 1', connection_name=None)
        out_path = os.path.join(self.home, 'x.csv')
        code, _, err = self.run_cli(['export', 'no_default_conn', '--connection', 'other', '--out', out_path])
        self.assertEqual(code, 0, err)

    def test_missing_connection_with_no_override_fails_cleanly(self):
        self.save('no_default_conn', 'SELECT id FROM t', connection_name=None)
        code, _, err = self.run_cli(['export', 'no_default_conn', '--out', os.path.join(self.home, 'x.csv')])
        self.assertEqual(code, 1)
        self.assertIn('Connection name is missing', err)

    def test_creates_the_output_directory_if_missing(self):
        out_path = os.path.join(self.home, 'nested', 'dir', 'out.csv')
        code, _, err = self.run_cli(['export', 'all_rows', '--out', out_path])
        self.assertEqual(code, 0, err)
        self.assertTrue(os.path.exists(out_path))

    def test_rerunning_overwrites_the_previous_export(self):
        out_path = os.path.join(self.home, 'out.csv')
        self.run_cli(['export', 'all_rows', '--out', out_path])
        conn = sqlite3.connect(self.db_path)
        conn.execute('INSERT INTO t VALUES (4, ?)', ('d',))
        conn.commit()
        conn.close()
        code, out, err = self.run_cli(['export', 'all_rows', '--out', out_path])
        self.assertEqual(code, 0, err)
        self.assertIn('Wrote 4 rows', out)
        with open(out_path) as f:
            self.assertEqual(len(f.read().splitlines()), 5)  # header + 4 rows


class LegacySettingsTests(unittest.TestCase):
    """The project used to be named SQL2API and read SQL2API_* settings. Those are no longer read, and a
    leftover one is a startup error - silently ignoring SQL2API_API_KEY would leave the server without an
    admin key, i.e. open."""

    def test_a_leftover_old_prefixed_setting_fails_startup_and_says_how_to_fix_it(self):
        with mock.patch.dict(os.environ, {'SQL2API_API_KEY': 'secret', 'SQL2API_HOME': '/tmp/x'}):
            with self.assertRaises(ValueError) as caught:
                config.check_settings()
        message = str(caught.exception)
        self.assertIn('SQL2API_API_KEY -> QUERYAPIGATE_API_KEY', message)
        self.assertIn('SQL2API_HOME -> QUERYAPIGATE_HOME', message)
        self.assertNotIn('secret', message)  # never echo the value of a setting that may be a credential

    def test_the_server_refuses_to_start_rather_than_run_without_the_key(self):
        err = io.StringIO()
        with mock.patch.dict(os.environ, {'SQL2API_API_KEY': 'secret'}), redirect_stderr(err):
            code = cli.main(['serve', '--port', '5999'])
        self.assertEqual(code, 2)
        self.assertIn('SQL2API_API_KEY -> QUERYAPIGATE_API_KEY', err.getvalue())

    def test_the_old_name_is_not_silently_read_as_a_fallback(self):
        with mock.patch.dict(os.environ, {'SQL2API_API_KEY': 'secret'}):
            self.assertIsNone(config.api_key())

    def test_new_prefixed_settings_are_unaffected(self):
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_API_KEY': 'secret'}):
            config.check_settings()  # must not raise
            self.assertEqual(config.api_key(), 'secret')


if __name__ == '__main__':
    unittest.main()

"""Tests for db.py - the SQLite connection/schema/transaction layer store.py's connections and saved-query
functions are built on (Phase 1 of the JSON-to-SQLite migration)."""
import os
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock

from queryapigate import config, db


class DbTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(db.close)

    def test_init_schema_creates_every_table(self):
        db.init_schema()
        conn = db.connection()
        names = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}
        self.assertEqual({'schema_version', 'connections', 'saved_queries',
                          'saved_query_versions', 'execution_history'} - names, set())

    def test_init_schema_is_idempotent(self):
        db.init_schema()
        db.init_schema()  # must not raise, must not duplicate the schema_version row
        conn = db.connection()
        rows = conn.execute('SELECT version FROM schema_version').fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], db.SCHEMA_VERSION)

    def test_wal_mode_is_on(self):
        db.init_schema()
        mode = db.connection().execute('PRAGMA journal_mode').fetchone()[0]
        self.assertEqual(mode.lower(), 'wal')

    def test_foreign_keys_are_enforced(self):
        db.init_schema()
        conn = db.connection()
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO saved_query_versions "
                        "(query_name, version, uuid, created_at, last_modified_at, fields_json) "
                        "VALUES ('nope', 1, 'u', 'now', 'now', '{}')")

    def test_a_second_thread_local_connection_to_the_same_file_works(self):
        db.init_schema()
        results = {}
        def other_thread():
            results['tables'] = {row[0] for row in db.connection().execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}
        t = threading.Thread(target=other_thread)
        t.start()
        t.join()
        self.assertIn('connections', results['tables'])

    def test_connection_reopens_when_home_changes(self):
        db.init_schema()
        first_path = db.connection().execute('PRAGMA database_list').fetchone()[2]
        with tempfile.TemporaryDirectory() as other:
            with mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': other}):
                db.init_schema()
                second_path = db.connection().execute('PRAGMA database_list').fetchone()[2]
        self.assertNotEqual(first_path, second_path)
        self.assertEqual(os.path.realpath(first_path), os.path.realpath(str(config.db_file())))

    def test_transaction_commits_on_success(self):
        db.init_schema()
        with db.transaction() as conn:
            conn.execute("INSERT INTO saved_queries (name, collection, example) VALUES ('q', NULL, 0)")
        row = db.connection().execute("SELECT name FROM saved_queries WHERE name = 'q'").fetchone()
        self.assertIsNotNone(row)

    def test_transaction_rolls_back_on_exception(self):
        db.init_schema()
        try:
            with db.transaction() as conn:
                conn.execute("INSERT INTO saved_queries (name, collection, example) VALUES ('q2', NULL, 0)")
                raise RuntimeError('boom')
        except RuntimeError:
            pass
        row = db.connection().execute("SELECT name FROM saved_queries WHERE name = 'q2'").fetchone()
        self.assertIsNone(row)


if __name__ == '__main__':
    unittest.main()

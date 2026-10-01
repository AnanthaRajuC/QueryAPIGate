"""Tests for db.py - the connection/schema/transaction layer store.py and apikeys.py are built on, on both of
its backends: SQLite (the default) and PostgreSQL (QUERYAPIGATE_DATABASE_URL). The Postgres-only tests run when
QUERYAPIGATE_TEST_DATABASE_URL points at a scratch database - see tests/__init__.py."""
import io
import os
import sqlite3
import tempfile
import threading
import unittest
from contextlib import redirect_stderr
from unittest import mock

from queryapigate import config, db
from tests import TEST_DATABASE_URL
from tests.helpers import write_connections

sqlite_only = unittest.skipIf(TEST_DATABASE_URL, 'inspects the SQLite file itself')
postgres_only = unittest.skipUnless(TEST_DATABASE_URL, 'set QUERYAPIGATE_TEST_DATABASE_URL to run')


class DbTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(db.close)

    @sqlite_only
    def test_init_schema_creates_every_table(self):
        db.init_schema()
        conn = db.connection()
        names = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}
        self.assertEqual({'schema_version', 'connections', 'saved_queries', 'saved_query_versions',
                          'execution_history', 'api_keys', 'roles', 'audit_log'} - names, set())

    def test_init_schema_is_idempotent(self):
        db.init_schema()
        db.init_schema()  # must not raise, must not duplicate the schema_version row
        conn = db.connection()
        rows = conn.execute('SELECT version FROM schema_version').fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], db.SCHEMA_VERSION)

    def test_init_schema_bumps_an_older_stored_version(self):
        db.init_schema()
        conn = db.connection()
        conn.execute('UPDATE schema_version SET version = 1')
        db.init_schema()
        rows = conn.execute('SELECT version FROM schema_version').fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], db.SCHEMA_VERSION)

    @sqlite_only
    def test_wal_mode_is_on(self):
        db.init_schema()
        mode = db.connection().execute('PRAGMA journal_mode').fetchone()[0]
        self.assertEqual(mode.lower(), 'wal')

    @sqlite_only
    def test_foreign_keys_are_enforced(self):
        db.init_schema()
        conn = db.connection()
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO saved_query_versions "
                        "(query_name, version, uuid, created_at, last_modified_at, fields_json) "
                        "VALUES ('nope', 1, 'u', 'now', 'now', '{}')")

    @sqlite_only
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

    @sqlite_only
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



class PgSqlTranslationTests(unittest.TestCase):
    """db._pg_sql(): the one place the callers' SQLite-flavoured SQL is adapted for Postgres. Pure string
    work, so it runs on every backend."""

    def test_placeholders_become_pyformat_outside_literals_only(self):
        self.assertEqual(db._pg_sql('SELECT * FROM t WHERE a = ? AND b = \'?\' AND "c?" = ?'),
                         'SELECT * FROM t WHERE a = %s AND b = \'?\' AND "c?" = %s')

    def test_a_literal_percent_is_escaped_for_psycopg2(self):
        self.assertEqual(db._pg_sql("SELECT * FROM t WHERE a LIKE 'x%' AND b = ?"),
                         "SELECT * FROM t WHERE a LIKE 'x%%' AND b = %s")

    def test_sqlite_only_spellings_are_rewritten(self):
        self.assertEqual(db._pg_sql('SELECT x FROM t ORDER BY x LIMIT -1 OFFSET ?'),
                         'SELECT x FROM t ORDER BY x LIMIT ALL OFFSET %s')
        self.assertEqual(db._pg_sql('INSERT OR IGNORE INTO t (a) VALUES (?)'),
                         'INSERT INTO t (a) VALUES (%s) ON CONFLICT DO NOTHING')

    def test_booleans_are_bound_as_integers(self):
        self.assertEqual(db._pg_params((True, False, 1, 'a', None)), (1, 0, 1, 'a', None))


class DatabaseUrlSettingTests(unittest.TestCase):
    def test_only_a_postgres_url_is_accepted(self):
        for url in ('mysql://u@h/d', 'sqlite:///x.db', 'h:5432/d'):
            with mock.patch.dict(os.environ, {'QUERYAPIGATE_DATABASE_URL': url}):
                with self.assertRaises(ValueError) as caught:
                    config.check_settings()
                self.assertIn('QUERYAPIGATE_DATABASE_URL', str(caught.exception))
        for url in ('postgres://u@h/d', 'postgresql://u:p@h:5432/d'):
            with mock.patch.dict(os.environ, {'QUERYAPIGATE_DATABASE_URL': url}):
                config.check_settings()  # must not raise

    @sqlite_only
    def test_unset_means_sqlite(self):
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_DATABASE_URL': ''}):
            self.assertIsNone(config.database_url())


@postgres_only
class PostgresDbTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(db.close)
        db.init_schema()

    def tables(self, conn):
        return {row[0] for row in conn.execute(
            'SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()').fetchall()}

    def test_init_schema_creates_every_table(self):
        self.assertEqual({'schema_version', 'connections', 'saved_queries', 'saved_query_versions',
                          'execution_history', 'api_keys', 'roles', 'audit_log'} - self.tables(db.connection()),
                         set())

    def test_foreign_keys_are_enforced(self):
        import psycopg2
        with self.assertRaises(psycopg2.IntegrityError):
            db.connection().execute("INSERT INTO saved_query_versions "
                                    "(query_name, version, uuid, created_at, last_modified_at, fields_json) "
                                    "VALUES ('nope', 1, 'u', 'now', 'now', '{}')")

    def test_rows_support_name_and_index_access(self):
        db.connection().execute("INSERT INTO saved_queries (name, collection, example) VALUES ('q', 'c', ?)",
                                (True,))
        row = db.connection().execute('SELECT name, example FROM saved_queries').fetchone()
        self.assertEqual((row['name'], row[0], row['example']), ('q', 'q', 1))
        self.assertEqual(dict(row), {'name': 'q', 'example': 1})

    def test_names_sort_byte_wise_like_sqlite(self):
        for name in ('b', 'B', 'a', 'A', '_x'):
            db.connection().execute('INSERT INTO saved_queries (name, example) VALUES (?, 0)', (name,))
        names = [r[0] for r in db.connection().execute('SELECT name FROM saved_queries ORDER BY name').fetchall()]
        self.assertEqual(names, sorted(names))  # Python's own sort is by code point, as SQLite's is

    def test_a_second_thread_gets_its_own_connection(self):
        results = {}

        def other_thread():
            results['conn'] = db.connection()
            results['tables'] = self.tables(results['conn'])
        t = threading.Thread(target=other_thread)
        t.start()
        t.join()
        self.assertIsNot(results['conn'], db.connection())
        self.assertIn('connections', results['tables'])

    def test_a_lost_connection_is_replaced_on_the_next_call(self):
        import psycopg2
        conn = db.connection()
        pid = conn.execute('SELECT pg_backend_pid()').fetchone()[0]
        killer = db._PgConnection(TEST_DATABASE_URL)
        killer.execute('SELECT pg_terminate_backend(?)', (pid,))
        killer.close()
        with self.assertRaises(psycopg2.Error):
            conn.execute('SELECT 1')
        self.assertEqual(db.connection().execute('SELECT 1').fetchone()[0], 1)  # a fresh connection
        self.assertIsNot(db.connection(), conn)

    def test_transactions_are_serialised_like_sqlites_write_lock(self):
        """Two concurrent read-modify-write transactions must not interleave: without the advisory lock,
        Postgres' default READ COMMITTED isolation would let both read 0 and both write 1."""
        db.connection().execute("INSERT INTO saved_queries (name, example) VALUES ('counter', 0)")
        home = os.environ['QUERYAPIGATE_HOME']
        start = threading.Barrier(8)
        errors = []

        def bump():
            try:
                with mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': home}):
                    start.wait()
                    for _ in range(10):
                        with db.transaction() as conn:
                            value = conn.execute("SELECT example FROM saved_queries WHERE name = 'counter'"
                                                 ).fetchone()[0]
                            conn.execute("UPDATE saved_queries SET example = ? WHERE name = 'counter'",
                                         (value + 1,))
                    db.close()
            except Exception as error:  # surfaced below - a thread's exception would otherwise vanish
                errors.append(error)
        threads = [threading.Thread(target=bump) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(db.connection().execute("SELECT example FROM saved_queries WHERE name = 'counter'"
                                                 ).fetchone()[0], 80)

    def test_a_forked_child_opens_its_own_connection_and_leaves_the_parents_alone(self):
        """gunicorn --preload forks workers after the parent already opened a connection."""
        import gc
        parent = db.connection()
        read, write = os.pipe()
        pid = os.fork()
        if pid == 0:  # child: must get a fresh, working connection of its own
            try:
                parent_id = id(parent)
                del parent
                child = db.connection()
                gc.collect()  # a collected inherited connection would close the parent's session
                ok = id(child) != parent_id and child.execute('SELECT 1').fetchone()[0] == 1
            except Exception:
                ok = False
            os.write(write, b'1' if ok else b'0')
            os._exit(0)
        os.waitpid(pid, 0)
        self.assertEqual(os.read(read, 1), b'1')
        os.close(read)
        os.close(write)
        self.assertIs(db.connection(), parent)
        self.assertEqual(parent.execute('SELECT 1').fetchone()[0], 1)  # the parent's is still usable

    def test_concurrent_history_writes_are_all_recorded_then_trimmed(self):
        """record_execution() skips the store-wide lock on Postgres (transaction(append_only=True)): every
        concurrent run must still land, and the per-version cap must still hold afterwards."""
        from queryapigate import store
        store.save_version('q', {'sql_query': 'SELECT 1', 'author': 'a', 'description': 'd'})
        home = os.environ['QUERYAPIGATE_HOME']
        start = threading.Barrier(8)
        errors = []

        def run(worker):
            try:
                with mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': home}):
                    start.wait()
                    for i in range(20):
                        store.record_execution('q', 1, {'executed_at': f'2026-01-01 00:00:{i:02d}',
                                                        'request_id': f'{worker}-{i}'})
                    db.close()
            except Exception as error:
                errors.append(error)
        with mock.patch.object(config, 'HISTORY_LIMIT', 1000):  # nothing trimmed: every one of the 160 runs kept
            threads = [threading.Thread(target=run, args=(w,)) for w in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(errors, [])
        count = 'SELECT COUNT(*) FROM execution_history'
        self.assertEqual(db.connection().execute(count).fetchone()[0], 160)
        store.record_execution('q', 1, {'executed_at': '2026-01-02 00:00:00'})  # the cap applies again
        self.assertEqual(db.connection().execute(count).fetchone()[0], config.HISTORY_LIMIT)

    def test_init_schema_is_safe_to_run_concurrently(self):
        """Several workers or instances start at once and each runs init_schema() against a fresh database."""
        with tempfile.TemporaryDirectory() as fresh:
            start = threading.Barrier(6)
            errors = []

            def init():
                try:
                    with mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': fresh}):
                        start.wait()
                        db.init_schema()
                        db.close()
                except Exception as error:
                    errors.append(error)
            threads = [threading.Thread(target=init) for _ in range(6)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(errors, [])


if __name__ == '__main__':
    unittest.main()


class MigrateCommandTests(unittest.TestCase):
    def test_refuses_without_a_postgres_url(self):
        with tempfile.TemporaryDirectory() as home, \
                mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': home, 'QUERYAPIGATE_DATABASE_URL': ''}), \
                mock.patch.object(config, 'database_url', return_value=None), \
                mock.patch.object(db, '_target', return_value=('sqlite', os.path.join(home, 'queryapigate.db'))):
            from queryapigate import cli
            err = io.StringIO()
            with redirect_stderr(err):
                self.assertEqual(cli.main(['migrate-to-postgres']), 2)
            db.close()
        self.assertIn('QUERYAPIGATE_DATABASE_URL is not set', err.getvalue())


@postgres_only
class MigrateToPostgresTests(unittest.TestCase):
    """db.migrate_sqlite_to_postgres(): a real queryapigate.db, built through the app's own API on the SQLite
    backend, copied into this test's (empty) Postgres schema."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {'QUERYAPIGATE_HOME': self.tmp.name, 'QUERYAPIGATE_API_KEY': 'admin'})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(db.close)
        self.sqlite_path = os.path.join(self.tmp.name, 'queryapigate.db')
        self.snapshot_sqlite = self.build_sqlite_store()

    def snapshot(self):
        from queryapigate import create_app
        client, admin = create_app().test_client(), {'X-API-Key': 'admin'}
        return {url: client.get(url, headers=admin).get_json()
                for url in ('/list_files', '/api_keys', '/roles', '/audit_log', '/connections')}

    def build_sqlite_store(self):
        from queryapigate import create_app
        with mock.patch.object(config, 'database_url', return_value=None), \
                mock.patch.object(db, '_target', return_value=('sqlite', self.sqlite_path)):
            db.close()
            write_connections({'lite': {'db': 'sqlite', 'database': self.sqlite_path, 'active': True}})
            client, admin = create_app().test_client(), {'X-API-Key': 'admin'}
            for name in ('b_query', 'a_query'):
                client.patch('/save_sql_to_file', headers=admin, json={
                    'author': 'a', 'description': 'd', 'filename': name, 'connection_name': 'lite',
                    'sql_query': 'SELECT name FROM roles ORDER BY name', 'collection': 'reports'})
            client.post('/api_keys', headers=admin, json={'name': 'partner', 'connections': ['lite']})
            client.post('/roles', headers=admin, json={'name': 'reader', 'connections': ['lite']})
            for _ in range(3):
                client.get('/q/a_query', headers=admin)
            snapshot = self.snapshot()
            db.close()
        return snapshot

    def test_copies_everything_and_the_api_sees_the_same_store(self):
        before = os.path.getmtime(self.sqlite_path), os.path.getsize(self.sqlite_path)
        copied = db.migrate_sqlite_to_postgres(self.sqlite_path)
        self.assertEqual((copied['saved_queries'], copied['api_keys'], copied['roles'], copied['execution_history']),
                         (2, 1, 1, 3))
        self.assertEqual(self.snapshot(), self.snapshot_sqlite)  # history order, audit order, keys - all alike
        self.assertEqual((os.path.getmtime(self.sqlite_path), os.path.getsize(self.sqlite_path)), before)

    def test_refuses_to_fill_a_store_that_already_has_data(self):
        db.init_schema()
        db.connection().execute("INSERT INTO roles (name, created_at, details_json) VALUES ('mine', 'now', '{}')")
        with self.assertRaises(ValueError) as caught:
            db.migrate_sqlite_to_postgres(self.sqlite_path)
        self.assertIn('already has data (roles)', str(caught.exception))
        self.assertEqual(db.connection().execute('SELECT COUNT(*) FROM saved_queries').fetchone()[0], 0)

    def test_refuses_a_missing_source(self):
        with self.assertRaises(ValueError):
            db.migrate_sqlite_to_postgres(os.path.join(self.tmp.name, 'nope.db'))

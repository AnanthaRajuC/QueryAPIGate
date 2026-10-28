"""Test package setup.

Set QUERYAPIGATE_TEST_DATABASE_URL (a postgres:// URL to a scratch database) to run the whole suite against the
PostgreSQL metadata backend instead of SQLite. Every test already gets a fresh QUERYAPIGATE_HOME, so each home is
mapped to its own Postgres schema: the same per-test isolation the SQLite backend gets from a fresh
queryapigate.db file, with no test needing to know which backend it runs on.

Set QUERYAPIGATE_TEST_RUN_STORE_URL (an http://user:password@host:port URL to a scratch ClickHouse) to run it with
run history and the event log in a run store (runstore.py, ADR 0029) instead: each home gets its own ClickHouse
database the same way, dropped with the home.
"""
import hashlib
import os
from pathlib import Path

TEST_DATABASE_URL = os.environ.get('QUERYAPIGATE_TEST_DATABASE_URL', '').strip() or None
TEST_RUN_STORE_URL = os.environ.get('QUERYAPIGATE_TEST_RUN_STORE_URL', '').strip() or None
_SCHEMA_PREFIX = 'qag_test_'
_SCHEMA_LOCK_ID = 0x7E57  # distinct from db._PG_LOCK_ID


def _use_postgres(url):
    from queryapigate import config, db

    def schema_for(home):
        return _SCHEMA_PREFIX + hashlib.sha1(home.encode('utf-8')).hexdigest()[:16]

    def target():
        return ('postgres', str(config.home()))

    sqlite_open = db._open

    def open_target(target):
        if target[0] == 'sqlite':  # a test that deliberately switches to SQLite (e.g. to build a migration source)
            return sqlite_open(target)
        conn = db._PgConnection(url)
        schema = schema_for(target[1])
        # Session-level lock: several threads of one test (e.g. a concurrency test) may open the same schema at
        # once, and CREATE SCHEMA IF NOT EXISTS itself is not safe against that race.
        # Bounded, like db.transaction()'s lock: a stuck holder fails the test with a lock-timeout error naming
        # this spot, rather than hanging the whole run.
        conn.execute("SET lock_timeout = '30s'")
        conn.execute('SELECT pg_advisory_lock(?)', (_SCHEMA_LOCK_ID,))
        try:
            conn.execute(f'CREATE SCHEMA IF NOT EXISTS {schema}')
        finally:
            conn.execute('SELECT pg_advisory_unlock(?)', (_SCHEMA_LOCK_ID,))
        conn.execute(f'SET search_path TO {schema}')
        return conn

    # Drop what an earlier run left behind, so schemas don't accumulate across runs.
    conn = db._PgConnection(url)
    for (schema,) in conn.execute('SELECT nspname FROM pg_namespace WHERE nspname LIKE ?',
                                  (_SCHEMA_PREFIX + '%',)).fetchall():
        conn.execute(f'DROP SCHEMA {schema} CASCADE')
    conn.close()

    config.database_url = lambda: url
    db._target = target
    db._open = open_target


def _run_store_database(home):
    return _SCHEMA_PREFIX + hashlib.sha1(str(home).encode('utf-8')).hexdigest()[:16]


def _use_run_store(url):
    from queryapigate import config, runstore

    config.run_store_url = lambda: url

    def database(target=None):  # the home a batch's metadata store is in (db.current_target()), or the current one
        if target is None:
            return _run_store_database(config.home())
        home = target[1] if target[0] == 'postgres' else os.path.dirname(target[1])  # _use_postgres maps homes so
        return _run_store_database(Path(home).resolve())
    runstore.database = database
    marker = runstore.check_marker

    def check_marker():
        if os.environ.get('QUERYAPIGATE_HOME'):  # never the working directory's own store: a developer's
            marker()
    runstore.check_marker = check_marker
    # Drop what an earlier run left behind, so databases don't accumulate across runs.
    for row in runstore.query("SELECT name FROM system.databases WHERE startsWith(name, {p:String})",
                              {'p': _SCHEMA_PREFIX}, db_name='default'):
        runstore.execute(f"DROP DATABASE IF EXISTS {row['name']} SYNC", db_name='default')


def _flush_history_before_removing_homes():
    """Run history is written in batches by a background thread (history.py). A test's last runs can still be
    queued when its temporary QUERYAPIGATE_HOME is removed; written mid-removal, they recreate queryapigate.db there,
    and the removal fails with "Directory not empty" - intermittently, depending on timing. So every temporary
    directory waits for this process's queued runs to be written before it is removed, whatever test made it."""
    import tempfile

    from queryapigate import history

    remove = tempfile.TemporaryDirectory.cleanup

    def cleanup(self):
        history.flush()
        remove(self)
        if TEST_RUN_STORE_URL:  # the home's own run store database goes with it
            from pathlib import Path

            from queryapigate import runstore
            try:
                runstore.execute(f'DROP DATABASE IF EXISTS {_run_store_database(Path(self.name).resolve())} SYNC',
                                 db_name='default')
            except runstore.RunStoreError:
                pass

    tempfile.TemporaryDirectory.cleanup = cleanup  # type: ignore[method-assign]


_flush_history_before_removing_homes()

if TEST_DATABASE_URL:
    _use_postgres(TEST_DATABASE_URL)
if TEST_RUN_STORE_URL:
    _use_run_store(TEST_RUN_STORE_URL)

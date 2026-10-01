"""Test package setup.

Set QUERYAPIGATE_TEST_DATABASE_URL (a postgres:// URL to a scratch database) to run the whole suite against the
PostgreSQL metadata backend instead of SQLite. Every test already gets a fresh QUERYAPIGATE_HOME, so each home is
mapped to its own Postgres schema: the same per-test isolation the SQLite backend gets from a fresh
queryapigate.db file, with no test needing to know which backend it runs on.
"""
import hashlib
import os

TEST_DATABASE_URL = os.environ.get('QUERYAPIGATE_TEST_DATABASE_URL', '').strip() or None
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


if TEST_DATABASE_URL:
    _use_postgres(TEST_DATABASE_URL)

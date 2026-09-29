"""queryapigate.db - the SQLite store for connections and saved queries (Phase 1 of moving off the JSON
files store.py used to own alone; see BACKLOG for the roadmap. api_keys.json/roles.json/audit_log.json are
untouched by this module and stay JSON-backed for a later phase).

One sqlite3.Connection per thread (threading.local), in WAL mode with a busy_timeout - SQLite's own file
locking is what actually makes this safe across threads *and* processes, replacing what store.py's
`threading.RLock` could only ever guarantee within a single process. Every compound "read, mutate, write"
that used to be `with lock: write_json_atomic(...)` is now a real `transaction()` - BEGIN IMMEDIATE acquires
the write lock up front (so a conflicting writer fails fast at the start, not partway through), COMMIT on a
clean exit, ROLLBACK on any exception.
"""
import contextlib
import sqlite3
import threading

from . import config

SCHEMA_VERSION = 1

_local = threading.local()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS connections (
  name TEXT PRIMARY KEY,
  db TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  details_json TEXT NOT NULL  -- everything else: password, host, port, user, database, example, ...
);

CREATE TABLE IF NOT EXISTS saved_queries (
  name TEXT PRIMARY KEY,
  collection TEXT,
  example INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS saved_query_versions (
  query_name TEXT NOT NULL REFERENCES saved_queries(name) ON DELETE CASCADE,
  version INTEGER NOT NULL,
  uuid TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL,
  last_modified_at TEXT NOT NULL,
  fields_json TEXT NOT NULL,
  PRIMARY KEY (query_name, version)
);

CREATE TABLE IF NOT EXISTS execution_history (
  query_name TEXT NOT NULL,
  version INTEGER NOT NULL,
  executed_at TEXT NOT NULL,
  connection_name TEXT,
  request_id TEXT,
  key_name TEXT,
  status TEXT NOT NULL,
  rows INTEGER,
  duration_ms REAL,
  serialization_ms REAL,
  error TEXT,
  FOREIGN KEY (query_name, version) REFERENCES saved_query_versions(query_name, version) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_execution_history_qv ON execution_history(query_name, version, executed_at);
"""


def _connect(path):
    conn = sqlite3.connect(path, isolation_level=None, timeout=5)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA foreign_keys=ON')
    conn.execute('PRAGMA busy_timeout=5000')
    conn.row_factory = sqlite3.Row
    return conn


def connection():
    """This thread's connection to the *current* config.db_file() - reopened automatically if
    QUERYAPIGATE_HOME (and so the target path) has changed since the last call, which is exactly what
    every test does between cases (a fresh temp home per test, same worker thread throughout the suite)."""
    path = str(config.db_file())
    if getattr(_local, 'path', None) != path:
        old = getattr(_local, 'conn', None)
        if old is not None:
            old.close()
        _local.conn = _connect(path)
        _local.path = path
    return _local.conn


def close():
    """Close and forget this thread's connection, if any - mainly for tests that want a clean slate without
    relying on the path-change auto-reconnect above."""
    conn = getattr(_local, 'conn', None)
    if conn is not None:
        conn.close()
        _local.conn = None
        _local.path = None


def init_schema():
    """Create every table (IF NOT EXISTS) and record the schema version - idempotent, safe to call from
    `queryapigate init`, `queryapigate migrate-to-sqlite`, and (as a no-op after the first time) app
    startup."""
    conn = connection()
    conn.executescript(_SCHEMA)
    row = conn.execute('SELECT version FROM schema_version').fetchone()
    if row is None:
        conn.execute('INSERT INTO schema_version (version) VALUES (?)', (SCHEMA_VERSION,))


@contextlib.contextmanager
def transaction():
    """BEGIN IMMEDIATE / COMMIT / ROLLBACK around a block of statements - the replacement for store.py's
    `with lock: write_json_atomic(...)` for everything this module owns."""
    conn = connection()
    conn.execute('BEGIN IMMEDIATE')
    try:
        yield conn
        conn.execute('COMMIT')
    except BaseException:
        conn.execute('ROLLBACK')
        raise

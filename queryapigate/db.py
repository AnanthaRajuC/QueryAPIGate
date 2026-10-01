"""queryapigate.db - the SQLite store for every persistent thing this app owns: connections and saved
queries (Phase 1), and API keys, roles and the audit log (Phase 2) - see BACKLOG #53 for the roadmap.

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

SCHEMA_VERSION = 2

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

-- One row per run, `entry_json` holding the whole entry verbatim (connection_name/request_id/key_name/
-- status/rows/duration_ms/serialization_ms/error - whichever keys the caller actually gave, exactly as
-- given) rather than structured columns: several UI features (History, Metrics, the "requests per day"
-- chart) distinguish a key being *absent* from being present-but-null, which a JSON blob preserves for
-- free and per-column NULLs cannot. `executed_at` is pulled out as a real column since it's what every
-- ordering/capping query needs.
CREATE TABLE IF NOT EXISTS execution_history (
  query_name TEXT NOT NULL,
  version INTEGER NOT NULL,
  executed_at TEXT NOT NULL,
  entry_json TEXT NOT NULL,
  FOREIGN KEY (query_name, version) REFERENCES saved_query_versions(query_name, version) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_execution_history_qv ON execution_history(query_name, version, executed_at);

CREATE TABLE IF NOT EXISTS api_keys (
  name TEXT PRIMARY KEY,
  hash TEXT NOT NULL UNIQUE,  -- authenticate() (apikeys.py) looks a supplied secret up by this column;
                              -- UNIQUE gives it the index that keeps that a single lookup at any key count
  active INTEGER NOT NULL DEFAULT 1,
  expires_at TEXT,           -- checked in Python (apikeys.is_expired()), kept a real column anyway to match
                              -- active/created_at as a plausible future "keys expiring soon" filter target
  created_at TEXT NOT NULL,
  details_json TEXT NOT NULL  -- connections, allow_writes, queries, collections, rate_limit, allowed_ips,
                               -- allowed_write_ops, created_from_role, last_used_at - everything else
);

CREATE TABLE IF NOT EXISTS roles (
  name TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  details_json TEXT NOT NULL  -- connections, allow_writes, queries, collections, rate_limit, allowed_ips,
                               -- allowed_write_ops, example - a role has no hash/active/expires_at/
                               -- created_from_role/last_used_at, so none of those become columns
);

-- Mirrors execution_history's own shape: entry_json holds actor/action/target/changes verbatim, for the
-- same "absent vs. present-but-null" reason execution_history documents. `timestamp` is a real column
-- since it's what every ordering/capping query needs.
CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  timestamp TEXT NOT NULL,
  entry_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_log_timestamp ON audit_log(timestamp);
"""


def _connect(path):
    conn = sqlite3.connect(path, isolation_level=None, timeout=5)
    conn.execute('PRAGMA journal_mode=WAL')
    # NORMAL is SQLite's recommended setting under WAL: still never corrupts the file, but commits no longer
    # fsync - which matters because every saved-query run commits a history row. The trade-off is that a
    # power loss (not a process crash) can lose the last few commits.
    conn.execute('PRAGMA synchronous=NORMAL')
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
    elif row[0] != SCHEMA_VERSION:
        conn.execute('UPDATE schema_version SET version = ?', (SCHEMA_VERSION,))


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

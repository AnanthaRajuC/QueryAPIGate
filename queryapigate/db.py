"""queryapigate.db - the store for every persistent thing this app owns: connections, saved queries and their
run history, API keys, roles and the audit log - see BACKLOG #53 for the roadmap.

Two backends behind one interface, picked once per process by config.database_url():

- **SQLite** (the default): queryapigate.db in QUERYAPIGATE_HOME. One sqlite3.Connection per thread
  (threading.local), in WAL mode with a busy_timeout - SQLite's own file locking is what actually makes this
  safe across threads *and* processes on one machine.
- **PostgreSQL** (QUERYAPIGATE_DATABASE_URL): one psycopg2 connection per thread, wrapped by _PgConnection so
  every caller keeps writing the same SQL - `?` placeholders, `row['column']` / `row[0]` access, `rowcount`.
  The few SQLite-only spellings callers use are rewritten once per statement (see _pg_sql()), and the schema
  names a `rowid` column wherever callers order or trim by SQLite's implicit one. This is what lets several
  instances behind a load balancer share one store.

Every compound "read, mutate, write" is a real `transaction()`. SQLite's BEGIN IMMEDIATE takes the database's
single write lock up front, so writers never interleave; on Postgres the same guarantee comes from a
transaction-scoped advisory lock, so both backends give callers identical semantics - a read inside a
transaction can't be invalidated by another writer before that transaction commits.
"""
import contextlib
import functools
import os
import re
import sqlite3
import threading

from . import config

SCHEMA_VERSION = 7  # 3: execution_history.status/key_name; 4: saved_queries.published_version;
#                    5: ad-hoc runs in execution_history (query_name/version nullable); 6: instances;
#                    7: administrators and admin_tokens - see _upgrade()

_local = threading.local()
_inherited: list[object] = []  # connections a forked child must neither use nor close - see connection()

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
  example INTEGER NOT NULL DEFAULT 0,
  published_version INTEGER  -- the version /q/<name> serves; NULL = not published (schema 4; see _upgrade())
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
  query_name TEXT,   -- with version: the saved query that ran; both NULL for an ad-hoc run (/execute_sql, MCP's
  version INTEGER,   -- execute_sql), whose SQL and connection are in entry_json (schema 5)
  executed_at TEXT NOT NULL,
  entry_json TEXT NOT NULL,
  status TEXT,    -- copies of entry_json's own status/key_name, as real columns so GET /api/v1/history can filter on
  key_name TEXT,  -- them the same way on both backends (schema 3; see _upgrade())
  FOREIGN KEY (query_name, version) REFERENCES saved_query_versions(query_name, version) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_execution_history_qv ON execution_history(query_name, version, executed_at);
CREATE INDEX IF NOT EXISTS idx_execution_history_time ON execution_history(executed_at);

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

-- Every running process using this store, refreshed as it serves requests (instances.py): which are alive, on what
-- version, and whether they share rate limits through Redis. Rows of processes gone for a day are removed.
CREATE TABLE IF NOT EXISTS instances (
  id TEXT PRIMARY KEY,
  host TEXT NOT NULL,
  pid INTEGER NOT NULL,
  role TEXT NOT NULL,
  version TEXT NOT NULL,
  shared_limits INTEGER NOT NULL,
  started_at REAL NOT NULL,
  last_seen REAL NOT NULL
);
-- Named administrators (admins.py, ADR 0003): the people and pipelines that manage this server, each with a role.
CREATE TABLE IF NOT EXISTS administrators (
  name TEXT PRIMARY KEY,
  role TEXT NOT NULL,          -- owner, admin, developer or auditor (adminroles.py)
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  details_json TEXT NOT NULL   -- email, created_by, last_seen_at
);

-- An administrator's personal admin tokens - only each one's SHA-256 hash, looked up like an API key's.
CREATE TABLE IF NOT EXISTS admin_tokens (
  id TEXT PRIMARY KEY,         -- public, shown in lists; never the secret
  admin_name TEXT NOT NULL,
  hash TEXT NOT NULL UNIQUE,
  expires_at TEXT,
  created_at TEXT NOT NULL,
  details_json TEXT NOT NULL   -- label, last_used_at
);
CREATE INDEX IF NOT EXISTS idx_admin_tokens_admin ON admin_tokens(admin_name);
"""


# The SQLite execution_history table as schema 5 defines it - what _upgrade() rebuilds an older one into.
_SQLITE_HISTORY_V5 = _SCHEMA[_SCHEMA.index('CREATE TABLE IF NOT EXISTS execution_history ('):]
_SQLITE_HISTORY_V5 = _SQLITE_HISTORY_V5[:_SQLITE_HISTORY_V5.index(');') + 2]

_PG_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS connections (
  name TEXT COLLATE "C" PRIMARY KEY,
  db TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  details_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS saved_queries (
  name TEXT COLLATE "C" PRIMARY KEY,
  collection TEXT COLLATE "C",
  example INTEGER NOT NULL DEFAULT 0,
  published_version INTEGER
);

CREATE TABLE IF NOT EXISTS saved_query_versions (
  query_name TEXT COLLATE "C" NOT NULL REFERENCES saved_queries(name) ON DELETE CASCADE,
  version INTEGER NOT NULL,
  uuid TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL,
  last_modified_at TEXT NOT NULL,
  fields_json TEXT NOT NULL,
  PRIMARY KEY (query_name, version)
);

-- `rowid` stands in for SQLite's implicit column of the same name: store.py breaks executed_at ties and trims
-- the oldest runs by it, so the SQL that does so runs unchanged on both backends.
CREATE TABLE IF NOT EXISTS execution_history (
  rowid BIGSERIAL PRIMARY KEY,
  query_name TEXT COLLATE "C",  -- NULL, with version, for an ad-hoc run (schema 5)
  version INTEGER,
  executed_at TEXT NOT NULL,
  entry_json TEXT NOT NULL,
  status TEXT,
  key_name TEXT,
  FOREIGN KEY (query_name, version) REFERENCES saved_query_versions(query_name, version) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_execution_history_qv ON execution_history(query_name, version, executed_at);
CREATE INDEX IF NOT EXISTS idx_execution_history_time ON execution_history(executed_at);

CREATE TABLE IF NOT EXISTS api_keys (
  name TEXT COLLATE "C" PRIMARY KEY,
  hash TEXT NOT NULL UNIQUE,
  active INTEGER NOT NULL DEFAULT 1,
  expires_at TEXT,
  created_at TEXT NOT NULL,
  details_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS roles (
  name TEXT COLLATE "C" PRIMARY KEY,
  created_at TEXT NOT NULL,
  details_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
  id BIGSERIAL PRIMARY KEY,
  rowid BIGINT GENERATED ALWAYS AS (id) STORED,  -- see execution_history's own rowid
  timestamp TEXT NOT NULL,
  entry_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_log_timestamp ON audit_log(timestamp);

CREATE TABLE IF NOT EXISTS instances (
  id TEXT COLLATE "C" PRIMARY KEY,
  host TEXT NOT NULL,
  pid INTEGER NOT NULL,
  role TEXT NOT NULL,
  version TEXT NOT NULL,
  shared_limits INTEGER NOT NULL,
  started_at DOUBLE PRECISION NOT NULL,
  last_seen DOUBLE PRECISION NOT NULL
);
-- Named administrators (admins.py, ADR 0003): the people and pipelines that manage this server, each with a role.
CREATE TABLE IF NOT EXISTS administrators (
  name TEXT COLLATE "C" PRIMARY KEY,
  role TEXT NOT NULL,          -- owner, admin, developer or auditor (adminroles.py)
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  details_json TEXT NOT NULL   -- email, created_by, last_seen_at
);

-- An administrator's personal admin tokens - only each one's SHA-256 hash, looked up like an API key's.
CREATE TABLE IF NOT EXISTS admin_tokens (
  id TEXT COLLATE "C" PRIMARY KEY,         -- public, shown in lists; never the secret
  admin_name TEXT NOT NULL,
  hash TEXT NOT NULL UNIQUE,
  expires_at TEXT,
  created_at TEXT NOT NULL,
  details_json TEXT NOT NULL   -- label, last_used_at
);
CREATE INDEX IF NOT EXISTS idx_admin_tokens_admin ON admin_tokens(admin_name);
"""
# Text columns use the "C" collation so ORDER BY name sorts byte-wise, exactly as SQLite does - a database's
# default locale collation would otherwise reorder every list the API and UI return.

# Serialises every transaction() on Postgres, as SQLite's single write lock does - see the module docstring.
_PG_LOCK_TIMEOUT = '30s'  # longest a transaction() waits for that lock (or a row lock inside it)
_PG_LOCK_ID = 0x51A6  # "QAG": an arbitrary, fixed advisory-lock key this app owns in its database

_INSERT_OR_IGNORE_RE = re.compile(r'^\s*INSERT\s+OR\s+IGNORE\s+INTO\b', re.I)
_LIMIT_ALL_RE = re.compile(r'\bLIMIT\s+-1\b', re.I)
_PG_TOKEN_RE = re.compile(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|--[^\n]*|\?|%")


def is_postgres():
    return config.database_url() is not None


def describe():
    """Where the store lives, for log lines and CLI messages - never with a password in it."""
    url = config.database_url()
    return f'PostgreSQL {config.redact_url(url)}' if url else str(config.db_file())


@functools.lru_cache(maxsize=512)
def _pg_sql(sql):
    """One caller's SQLite-flavoured statement, as Postgres wants it: `?` placeholders become `%s` (outside
    string literals and identifiers only), a literal `%` is doubled for psycopg2, `INSERT OR IGNORE` becomes
    `ON CONFLICT DO NOTHING` and `LIMIT -1` (SQLite's "no limit") becomes `LIMIT ALL`. Callers only ever pass
    this module's own fixed SQL text, so the cache stays small."""
    def token(match):
        text = match.group(0)
        if text == '?':
            return '%s'
        return text.replace('%', '%%')
    out = _PG_TOKEN_RE.sub(token, sql)
    out = _LIMIT_ALL_RE.sub('LIMIT ALL', out)
    if _INSERT_OR_IGNORE_RE.match(out):
        out = _INSERT_OR_IGNORE_RE.sub('INSERT INTO', out, count=1).rstrip().rstrip(';') + ' ON CONFLICT DO NOTHING'
    return out


def _pg_params(params):
    # SQLite stores a Python bool in an INTEGER column as 0/1; Postgres refuses a boolean there.
    return tuple(int(p) if isinstance(p, bool) else p for p in params)


class _PgConnection:
    """The slice of sqlite3.Connection's interface this app's callers use - execute() returning a cursor whose
    rows support both row['column'] and row[0], and executescript() - over a psycopg2 connection in
    autocommit mode (the counterpart of sqlite3's isolation_level=None: no implicit transactions, only the
    explicit ones transaction() opens)."""

    def __init__(self, url):
        import psycopg2
        import psycopg2.extras
        self.raw = psycopg2.connect(url, connect_timeout=config.CONNECT_TIMEOUT)
        self.raw.autocommit = True
        self._cursor_factory = psycopg2.extras.DictCursor

    @property
    def closed(self):
        return bool(self.raw.closed)

    def execute(self, sql, params=()):
        cursor = self.raw.cursor(cursor_factory=self._cursor_factory)
        cursor.execute(_pg_sql(sql), _pg_params(params))
        return cursor

    def executescript(self, script):
        with self.raw.cursor() as cursor:
            cursor.execute(script)

    def close(self):
        self.raw.close()


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


def _target():
    """What this thread's connection must point at: the Postgres URL when one is configured, else the SQLite
    file - re-read on every call so a changed QUERYAPIGATE_HOME/QUERYAPIGATE_DATABASE_URL (as every test makes
    between cases) is followed rather than served by a stale connection."""
    url = config.database_url()
    return ('postgres', url) if url else ('sqlite', str(config.db_file()))


def _open(target):
    kind, where = target
    return _PgConnection(where) if kind == 'postgres' else _connect(where)


def current_target():
    """The store this thread would use right now - captured by history.py when it queues a run, so the batch
    is later written to that same store even if the configuration has moved on by then."""
    return _target()


def connection(target=None):
    """This thread's connection to the current store (or to ``target``, a current_target() value) - reopened
    automatically if the target has changed since the last call (see _target()), or if a Postgres connection
    was lost (a server restart, a dropped network link): the next call simply connects again instead of
    failing every later request on a dead socket."""
    target = target or _target()
    conn = getattr(_local, 'conn', None)
    if conn is not None and getattr(_local, 'pid', None) != os.getpid():
        # Inherited across a fork (e.g. gunicorn --preload): the parent still owns that socket or file handle.
        # Closing it - or letting it be garbage-collected, which closes it too (psycopg2 sends the server a
        # terminate message on the shared socket) - would end the parent's session, so it is parked, never
        # closed, and replaced with a connection of this process's own.
        _inherited.append(conn)
        conn = _local.conn = None
    if conn is None or getattr(_local, 'target', None) != target or getattr(conn, 'closed', False):
        if conn is not None:
            try:
                conn.close()
            except Exception:  # already gone - nothing to release
                pass
        _local.conn = _open(target)
        _local.target = target
        _local.pid = os.getpid()
    return _local.conn


def close():
    """Close and forget this thread's connection, if any - mainly for tests that want a clean slate without
    relying on the target-change auto-reconnect above."""
    conn = getattr(_local, 'conn', None)
    if conn is not None:
        conn.close()
        _local.conn = None
        _local.target = None


def init_schema():
    """Create every table (IF NOT EXISTS) and record the schema version - idempotent, safe to call from
    `queryapigate init` and (as a no-op after the first time) app startup. On Postgres it runs inside
    transaction(), so several instances or workers starting at once can't race each other's CREATE TABLE."""
    if is_postgres():
        with transaction() as conn:
            conn.executescript(_PG_SCHEMA)
            _refuse_newer(conn)
            _upgrade(conn, postgres=True)
            check_shape(conn, postgres=True)
            _record_schema_version(conn)
        return
    connection().executescript(_SCHEMA)
    with transaction() as conn:  # BEGIN IMMEDIATE: another process starting at once can't upgrade alongside
        _refuse_newer(conn)
        _upgrade(conn, postgres=False)
        check_shape(conn, postgres=False)
        _record_schema_version(conn)


def backup_sqlite(destination):
    """Copy the SQLite store to `destination` while the server keeps running: SQLite's online backup API, so the
    copy is one consistent point in time and includes what is still in the write-ahead log, which copying the
    file alone can miss. Returns the copy's size in bytes."""
    source = sqlite3.connect(str(config.db_file()))
    target = sqlite3.connect(str(destination))
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    return os.path.getsize(destination)


def postgres_schema():
    """The schema the PostgreSQL store's tables are in - public, or the one ?options=-csearch_path names."""
    return connection().execute('SELECT current_schema()').fetchone()[0]


def _columns(conn, table, postgres):
    if postgres:
        return {row[0] for row in conn.execute(
            'SELECT column_name FROM information_schema.columns WHERE table_schema = current_schema() '
            'AND table_name = ?', (table,)).fetchall()}
    return {row[1] for row in conn.execute(f'PRAGMA table_info({table})').fetchall()}


def _upgrade(conn, postgres):
    """Bring a store an older version of this app created up to SCHEMA_VERSION. CREATE TABLE IF NOT EXISTS
    never alters a table that already exists, so a column added since has to be added here - each step checks
    for itself rather than trusting the recorded version, so it is safe to re-run.

    2: execution_history as one entry_json per run. 0.10 created a columnar placeholder it never wrote to (its
       runs still lived in saved_sql/*.json, imported from there), and no release replaced it - see
       _replace_placeholder_history().
    3: execution_history.status/key_name, backfilled from each row's own entry_json.
    4: saved_queries.published_version. Before it, the newest version was always the one served, so every
       existing query is published at its newest version: an upgrade changes nothing a caller can see.
    5: execution_history.query_name/version nullable, for ad-hoc runs. SQLite can't relax NOT NULL in place, so
       the table is rebuilt - keeping every row's rowid, which is also its live event's id (events.py), so a
       client resuming with Last-Event-ID still finds its place.
    6: the instances table - new, so CREATE TABLE IF NOT EXISTS makes it; nothing to change in place. Additive, as
       every change within a minor line must be (see DEPLOYMENT.md, Rolling upgrades): a 0.14 process still
       running against the upgraded store keeps working.
    7: administrators and admin_tokens (ADR 0003) - new tables, made by CREATE TABLE IF NOT EXISTS; additive."""
    if 'entry_json' not in _columns(conn, 'execution_history', postgres):
        _replace_placeholder_history(conn, postgres)
    if 'status' not in _columns(conn, 'execution_history', postgres):
        conn.execute('ALTER TABLE execution_history ADD COLUMN status TEXT')
        conn.execute('ALTER TABLE execution_history ADD COLUMN key_name TEXT')
        if postgres:
            conn.execute("UPDATE execution_history SET status = entry_json::json->>'status', "
                         "key_name = entry_json::json->>'key_name'")
        else:
            conn.execute("UPDATE execution_history SET status = json_extract(entry_json, '$.status'), "
                         "key_name = json_extract(entry_json, '$.key_name')")
    if 'published_version' not in _columns(conn, 'saved_queries', postgres):
        conn.execute('ALTER TABLE saved_queries ADD COLUMN published_version INTEGER')
        conn.execute('UPDATE saved_queries SET published_version = (SELECT MAX(version) FROM saved_query_versions '
                     'WHERE saved_query_versions.query_name = saved_queries.name)')
    if _history_requires_a_query(conn, postgres):
        if postgres:
            conn.execute('ALTER TABLE execution_history ALTER COLUMN query_name DROP NOT NULL')
            conn.execute('ALTER TABLE execution_history ALTER COLUMN version DROP NOT NULL')
        else:
            conn.execute(_SQLITE_HISTORY_V5.replace('execution_history (', 'execution_history_v5 (', 1))
            conn.execute('INSERT INTO execution_history_v5 (rowid, query_name, version, executed_at, entry_json, '
                         'status, key_name) SELECT rowid, query_name, version, executed_at, entry_json, status, '
                         'key_name FROM execution_history')
            conn.execute('DROP TABLE execution_history')
            conn.execute('ALTER TABLE execution_history_v5 RENAME TO execution_history')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_execution_history_qv '
                         'ON execution_history(query_name, version, executed_at)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_execution_history_time ON execution_history(executed_at)')


def _refuse_newer(conn):
    """A store a newer release has upgraded is not this version's to run on: its tables may mean things this code
    doesn't know, and recording this version's number over that release's would hide the mismatch from it."""
    row = conn.execute('SELECT version FROM schema_version').fetchone()
    if row is not None and row[0] > SCHEMA_VERSION:
        raise ValueError(f'the metadata store {describe()} is at schema {row[0]}, from a newer release of '
                         f'QueryAPIGate than this one (schema {SCHEMA_VERSION}). Run that release or a later one; '
                         'downgrading in place is not supported - restore a backup taken before the upgrade instead.')


def _replace_placeholder_history(conn, postgres):
    """Swap 0.10's never-used, columnar execution_history for the current table. Only an empty one is replaced: a
    table with rows in it is not the placeholder, and dropping it would lose them, so startup refuses instead."""
    if conn.execute('SELECT COUNT(*) FROM execution_history').fetchone()[0]:
        raise ValueError(f'the execution_history table in {describe()} has rows but not the shape any release of '
                         'QueryAPIGate wrote (no entry_json column), so it was not upgraded. Back up the store, '
                         'then move that table aside (ALTER TABLE execution_history RENAME TO '
                         'execution_history_old) and start again: a new, empty run history is created.')
    conn.execute('DROP TABLE execution_history')
    if postgres:
        conn.executescript(_PG_SCHEMA)
    else:
        conn.execute(_SQLITE_HISTORY_V5)
        conn.execute('CREATE INDEX IF NOT EXISTS idx_execution_history_qv '
                     'ON execution_history(query_name, version, executed_at)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_execution_history_time ON execution_history(executed_at)')


@functools.lru_cache(maxsize=1)
def expected_columns():
    """{table: columns} as this version's schema defines them - what check_shape() holds a store to."""
    conn = sqlite3.connect(':memory:')
    try:
        conn.executescript(_SCHEMA)
        tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' "
                                                       "AND name NOT LIKE 'sqlite_%'")]
        return {table: {row[1] for row in conn.execute(f'PRAGMA table_info({table})')} for table in tables}
    finally:
        conn.close()


def check_shape(conn, postgres):
    """Refuse to start on a store whose tables, after _upgrade(), still lack a column this version reads or writes -
    one some other tool altered, or a release newer than this one rebuilt - rather than start and then fail on the
    first request that touches it (BACKLOG #65). Extra columns are fine: a newer release's additions are ignored."""
    problems = []
    for table, wanted in sorted(expected_columns().items()):
        missing = wanted - _columns(conn, table, postgres)
        if missing:
            problems.append(f'{table} is missing {", ".join(sorted(missing))}')
    if problems:
        raise ValueError(f'the metadata store {describe()} does not have the shape this version of QueryAPIGate '
                         f'(schema {SCHEMA_VERSION}) needs: {"; ".join(problems)}. Restore it from a backup taken '
                         'before it was changed, or start the release that last ran on it and export what you need '
                         '(`queryapigate collection export`) before moving to a new QUERYAPIGATE_HOME.')


def _history_requires_a_query(conn, postgres):
    """Whether execution_history still predates schema 5 (query_name NOT NULL)."""
    if postgres:
        row = conn.execute("SELECT is_nullable FROM information_schema.columns WHERE table_schema = current_schema() "
                           "AND table_name = 'execution_history' AND column_name = 'query_name'").fetchone()
        return row is not None and row[0] == 'NO'
    return any(row[1] == 'query_name' and row[3] for row in
               conn.execute('PRAGMA table_info(execution_history)').fetchall())


def _record_schema_version(conn):
    row = conn.execute('SELECT version FROM schema_version').fetchone()
    if row is None:
        conn.execute('INSERT INTO schema_version (version) VALUES (?)', (SCHEMA_VERSION,))
    elif row[0] != SCHEMA_VERSION:
        conn.execute('UPDATE schema_version SET version = ?', (SCHEMA_VERSION,))


@contextlib.contextmanager
def transaction(append_only=False, target=None):
    """BEGIN / COMMIT / ROLLBACK around a block of statements, holding the store's write lock for the whole
    block - SQLite's own via BEGIN IMMEDIATE, an advisory lock on Postgres (see the module docstring).

    ``append_only=True`` is for a block that only adds a log row (store.record_execution()): nothing it reads
    can be invalidated by a concurrent writer, so on Postgres it skips the advisory lock - otherwise every
    query run in every instance would queue on that one lock - and commits without waiting for the WAL flush
    (synchronous_commit off), the same durability SQLite's synchronous=NORMAL gives: a crash can lose the last
    few runs' history, never corrupt anything. On SQLite it changes nothing.

    ``target`` (a current_target() value) runs the block against that store instead of the current one."""
    conn = connection(target) if target else connection()
    postgres = (target[0] == 'postgres') if target else is_postgres()
    conn.execute('BEGIN' if postgres else 'BEGIN IMMEDIATE')
    try:
        if postgres and append_only:
            conn.execute('SET LOCAL synchronous_commit TO OFF')
        elif postgres:
            # Bounded: should a session ever hold the lock and stall, writers fail after this long with a clear
            # "lock timeout" error instead of every one of them hanging, and holding a request thread, forever.
            conn.execute(f"SET LOCAL lock_timeout = '{_PG_LOCK_TIMEOUT}'")
            conn.execute('SELECT pg_advisory_xact_lock(?)', (_PG_LOCK_ID,))
        yield conn
        conn.execute('COMMIT')
    except BaseException:
        try:
            conn.execute('ROLLBACK')
        except Exception:  # a lost connection has nothing left to roll back; the original error matters more
            if not postgres:
                raise
        raise


def _error_types():
    types = [sqlite3.Error]
    try:
        import psycopg2
        types.append(psycopg2.Error)
    except ImportError:
        pass
    return tuple(types)


# What a failed statement raises on either backend - for the "never let a history/audit write fail the request"
# callers in store.py that used to catch sqlite3.Error alone.
Error = _error_types()


# Every table migrate_sqlite_to_postgres() copies, parents before children (foreign keys), with the columns it
# copies - execution_history's and audit_log's own row ids are left for Postgres to assign, in source order.
_MIGRATED_TABLES = (
    ('connections', ('name', 'db', 'active', 'created_at', 'updated_at', 'details_json'), 'name'),
    ('saved_queries', ('name', 'collection', 'example', 'published_version'), 'name'),
    ('saved_query_versions', ('query_name', 'version', 'uuid', 'status', 'created_at', 'last_modified_at',
                              'fields_json'), 'query_name, version'),
    ('execution_history', ('query_name', 'version', 'executed_at', 'entry_json', 'status', 'key_name'), 'rowid'),
    ('api_keys', ('name', 'hash', 'active', 'expires_at', 'created_at', 'details_json'), 'name'),
    ('roles', ('name', 'created_at', 'details_json'), 'name'),
    ('audit_log', ('timestamp', 'entry_json'), 'id'),
    ('administrators', ('name', 'role', 'active', 'created_at', 'details_json'), 'name'),
    ('admin_tokens', ('id', 'admin_name', 'hash', 'expires_at', 'created_at', 'details_json'), 'id'),
)


def migrate_sqlite_to_postgres(sqlite_path):
    """Copy every row of a queryapigate.db into the (empty) Postgres store QUERYAPIGATE_DATABASE_URL names, in
    one transaction: either all of it arrives or none does. Refuses - changing nothing - when the source is
    missing or on an older schema, or when the target already holds any data, so it can never merge two
    stores or overwrite one. The SQLite file is opened read-only and left exactly as it was.
    Returns {table: rows copied}."""
    if not is_postgres():
        raise ValueError('QUERYAPIGATE_DATABASE_URL is not set - there is no PostgreSQL database to migrate to')
    if not os.path.isfile(sqlite_path):
        raise ValueError(f'{sqlite_path} does not exist - nothing to migrate')
    source = sqlite3.connect(f'file:{sqlite_path}?mode=ro', uri=True)
    try:
        source.row_factory = sqlite3.Row
        try:
            version = source.execute('SELECT version FROM schema_version').fetchone()
        except sqlite3.Error:
            version = None
        if version is None or version[0] != SCHEMA_VERSION:
            raise ValueError(f'{sqlite_path} is not a queryapigate.db at schema version {SCHEMA_VERSION} - '
                             'start this version of QueryAPIGate against it once (without '
                             'QUERYAPIGATE_DATABASE_URL) to upgrade it, then migrate')
        init_schema()
        copied = {}
        with transaction() as conn:
            occupied = [table for table, _, _ in _MIGRATED_TABLES
                        if conn.execute(f'SELECT 1 FROM {table} LIMIT 1').fetchone() is not None]
            if occupied:
                raise ValueError(f'the PostgreSQL database already has data ({", ".join(occupied)}) - migrating '
                                 'only ever fills an empty one, so nothing was copied')
            from psycopg2.extras import execute_values
            for table, columns, order in _MIGRATED_TABLES:
                rows = [tuple(row) for row in source.execute(
                    f'SELECT {", ".join(columns)} FROM {table} ORDER BY {order}')]
                if rows:
                    with conn.raw.cursor() as cursor:
                        execute_values(cursor, f'INSERT INTO {table} ({", ".join(columns)}) VALUES %s', rows,
                                       page_size=1000)
                copied[table] = len(rows)
        return copied
    finally:
        source.close()

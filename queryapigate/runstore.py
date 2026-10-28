"""The run store (ADR 0029, experimental): run history, the event log and usage rollups in ClickHouse.

With QUERYAPIGATE_RUN_STORE_URL set, history.py's writer thread sends its batches here instead of to the metadata
store, and everything that reads runs or events - GET /api/v1/history, the Console's runs per version, usage
reports, alerts, `forget`, `queryapigate events` - reads them from here. Keys, queries, the audit log and the rest
stay in the metadata store (db.py).

**One insert per batch.** A batch goes into `events`. ClickHouse fills `history` (the runs history keeps -
`in_history`, false when the sample rate sampled a run out) and the per-minute and per-hour rollups from it, through
materialized views. Retention is each table's TTL: whole partitions are dropped, and history.py's sweeps don't run.
`events.written_at` is ClickHouse's own time at the insert: against the time in an event's id, it shows how late any
writer's insert was, the data plane's included.

**Speaking to ClickHouse.** Its HTTP interface, with the standard library: inserts as JSONEachRow, and every value a
read compares against as a typed query parameter (`{key:String}`), never as SQL text. The data plane (ADR 0028)
speaks the same interface from Rust.

**Event ids** (ADR 0002, ADR 0029 §2): milliseconds since 2026-01-01 UTC x 4096, plus a number within that
millisecond from a block this process leases in the metadata store (run_writer_leases), so no two writers mint the
same one. They're stamped on ClickHouse's clock, as this process estimates it, just before a batch is sent. They stay
below 2^53, and far above any id the metadata store's event log handed out, so a client holding one resumes after it.
`queryapigate events` delivers only events older than SETTLE_SECONDS: an insert acknowledged within that window is
never behind an id already delivered, so resuming after any id a client was given misses nothing.
"""
import atexit
import http.client
import json
import logging
import os
import socket
import ssl
import threading
import time
import uuid
from datetime import datetime, timedelta
from urllib.parse import unquote, urlencode, urlsplit

from . import config, db, metrics

log = logging.getLogger('queryapigate')

EPOCH_MS = 1767225600000         # 2026-01-01 00:00:00 UTC: where an id's milliseconds count from
PER_MS = 4096                    # ids in each millisecond, shared out in leased blocks
PYTHON_BLOCK = 16                # ids a Python process leases per millisecond: 16,000 runs a second
DATAPLANE_BLOCK = 256            # a data plane's (ADR 0028): 256,000 a second
SETTLE_SECONDS = 1.0             # how old an event must be before `queryapigate events` delivers it
LEASE_SECONDS = 60.0             # a lease lapses this long after its last renewal
_RENEW_SECONDS = 10.0            # how often a writer renews its lease
_MINT_WITHIN = 45.0              # a writer whose last renewal is older than this renews before minting again
_MAX_LEAD_MS = 1000              # how far a writer's ids may run ahead of the clock in a burst
_CLOCK_SECONDS = 60.0            # how often the offset to ClickHouse's clock is measured again
_TIMEOUT = 30.0                  # seconds for one request to ClickHouse
_SCHEMA_VERSION = 1              # of the run store's tables: run_store_schema records it
TIME_FORMAT = '%Y-%m-%d %H:%M:%S'


class RunStoreError(Exception):
    """ClickHouse refused a request, or couldn't be reached. ``sent`` is False when the request never reached it (a
    connection refused) and None when it can't be told whether it did (a timeout, a connection lost mid-response)."""

    def __init__(self, message, sent=True):
        super().__init__(message)
        self.sent = sent


def enabled():
    """Whether runs are kept here rather than in the metadata store."""
    return config.run_store_url() is not None


def database(target=None):
    """The ClickHouse database the run store's tables are in: the URL's path, `queryapigate` without one. ``target``
    (a db.current_target()) is the metadata store a batch belongs to - one database serves every target in a server,
    but the test package maps each test's home to a database of its own, as it maps each to a PostgreSQL schema."""
    return urlsplit(config.run_store_url()).path.strip('/') or 'queryapigate'


def location(url=None):
    """host:port/database - what the metadata store records as where runs are, never the URL's credentials."""
    parts = urlsplit(url or config.run_store_url())
    port = parts.port or (8443 if parts.scheme == 'https' else 8123)
    return f'{parts.hostname}:{port}/{parts.path.strip("/") or "queryapigate"}'


# ---- the HTTP interface ----

def _param(value):
    """A typed query parameter's text, as ClickHouse parses it: strings in its escaped format, arrays and tuples
    with quoted strings inside."""
    if value is None:
        return '\\N'
    if isinstance(value, bool):
        return '1' if value else '0'
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (list, tuple)):
        inner = ','.join(_quoted(item) for item in value)
        return f'[{inner}]' if isinstance(value, list) else f'({inner})'
    return str(value).replace('\\', '\\\\').replace('\t', '\\t').replace('\n', '\\n').replace('\r', '\\r')


def _quoted(value):
    if isinstance(value, str):
        return "'" + value.replace('\\', '\\\\').replace("'", "\\'") + "'"
    return _param(value)


class _Http:
    """One keep-alive connection per thread to ClickHouse's HTTP interface."""

    def __init__(self, url):
        parts = urlsplit(url)
        self.url = url
        self.https = parts.scheme == 'https'
        self.host = parts.hostname
        self.port = parts.port or (8443 if self.https else 8123)
        self.user = unquote(parts.username or 'default')
        self.password = unquote(parts.password or '')
        self.local = threading.local()

    def _connection(self):
        conn = getattr(self.local, 'conn', None)
        if conn is None or self.local.pid != os.getpid():  # never a connection a parent process opened
            if self.https:
                conn = http.client.HTTPSConnection(self.host, self.port, timeout=_TIMEOUT,
                                                   context=ssl.create_default_context())
            else:
                conn = http.client.HTTPConnection(self.host, self.port, timeout=_TIMEOUT)
            self.local.conn, self.local.pid = conn, os.getpid()
        return conn

    def _drop(self):
        conn = getattr(self.local, 'conn', None)
        if conn is not None:
            conn.close()
        self.local.conn = None

    def request(self, params, body):
        headers = {'X-ClickHouse-User': self.user, 'X-ClickHouse-Key': self.password,
                   'Content-Type': 'text/plain; charset=utf-8'}
        path = '/?' + urlencode(params)
        for attempt in (1, 2):
            conn = self._connection()
            sent = False
            try:
                conn.request('POST', path, body=body, headers=headers)
                sent = None
                response = conn.getresponse()
                data = response.read()
            except (http.client.RemoteDisconnected, BrokenPipeError, ConnectionResetError) as error:
                self._drop()
                # A keep-alive connection ClickHouse had closed: try once more on a new one. Every insert carries a
                # deduplication token, so one that did arrive isn't written twice.
                if attempt == 1:
                    continue
                raise RunStoreError(f'ClickHouse at {location(self.url)}: {error}', sent=sent) from None
            except (OSError, http.client.HTTPException) as error:
                self._drop()
                if isinstance(error, (ConnectionRefusedError, socket.gaierror)):
                    sent = False
                raise RunStoreError(f'ClickHouse at {location(self.url)}: {error}', sent=sent) from None
            if response.status != 200:
                message = data.decode('utf-8', 'replace').strip().splitlines()
                raise RunStoreError(f'ClickHouse at {location(self.url)}: {message[0] if message else response.status}')
            return data
        raise AssertionError('unreachable')


_http = None
_http_guard = threading.Lock()


def _client():
    global _http
    url = config.run_store_url()
    if _http is None or _http.url != url:
        with _http_guard:
            if _http is None or _http.url != url:
                _http = _Http(url)
    return _http


def query(sql, params=None, db_name=None):
    """Rows of a read, as dicts. ``params``: the values of its {name:Type} placeholders."""
    qp = {'database': db_name or database(), 'default_format': 'JSONEachRow',
          'output_format_json_quote_64bit_integers': 0}
    for name, value in (params or {}).items():
        qp[f'param_{name}'] = _param(value)
    data = _client().request(qp, sql.encode())
    return [json.loads(line) for line in data.splitlines() if line]


def execute(sql, params=None, db_name=None, settings=None):
    qp = {'database': db_name or database(), **(settings or {})}
    for name, value in (params or {}).items():
        qp[f'param_{name}'] = _param(value)
    _client().request(qp, sql.encode())


def insert(table, rows, token=None, db_name=None):
    """Insert ``rows`` (dicts) into ``table`` as one block, acknowledged once they're readable. ``token``: two inserts
    with the same one are written once (non_replicated_deduplication_window)."""
    if not rows:
        return
    qp = {'database': db_name or database(), 'query': f'INSERT INTO {table} FORMAT JSONEachRow'}
    if token:
        qp['insert_deduplication_token'] = token
    body = '\n'.join(json.dumps(row, separators=(',', ':'), default=str) for row in rows).encode()
    _client().request(qp, body)


# ---- the tables ----

_STAMPED = "toDateTime(intDiv(id, 4096000) + 1767225600, 'UTC')"  # an id's millisecond, to the second
_RUN_COLUMNS = """
  workspace LowCardinality(String),
  query_name LowCardinality(Nullable(String)),
  version Nullable(UInt32),
  key_name LowCardinality(Nullable(String)),
  connection_name LowCardinality(Nullable(String)),
  status LowCardinality(Nullable(String)),"""
_ROLLUP_DIMENSIONS = 'key_name, query_name, version, connection_name, source, status, code, statement'
_ROLLUP_KEY = 'workspace, ' + _ROLLUP_DIMENSIONS


def _tables(events_hours, history_days):
    """The run store's DDL, with today's retention settings."""
    rollup = """(
  {grain} DateTime('UTC'),
  workspace LowCardinality(String),
  key_name LowCardinality(Nullable(String)),
  query_name LowCardinality(Nullable(String)),
  version Nullable(UInt32),
  connection_name LowCardinality(Nullable(String)),
  source LowCardinality(Nullable(String)),
  status LowCardinality(Nullable(String)),
  code LowCardinality(String),
  statement String CODEC(ZSTD(3)),
  runs SimpleAggregateFunction(sum, UInt64),
  rows SimpleAggregateFunction(sum, UInt64),
  masked SimpleAggregateFunction(sum, UInt64),
  first SimpleAggregateFunction(min, String),
  last SimpleAggregateFunction(max, String),
  seconds AggregateFunction(quantilesTDigest(0.5, 0.95, 0.99), Float32)
) ENGINE = AggregatingMergeTree PARTITION BY toYYYYMM({grain})
ORDER BY (workspace, {grain}, {dimensions}) TTL {grain} + INTERVAL {days} DAY
SETTINGS allow_nullable_key = 1"""
    rollup_view = """AS SELECT {fn}(parseDateTimeBestEffortOrZero(occurred_at, 'UTC')) AS {grain}, {key},
  count() AS runs, sum(rows) AS rows, sum(toUInt64(masked)) AS masked, min(occurred_at) AS first,
  max(occurred_at) AS last, quantilesTDigestState(0.5, 0.95, 0.99)(seconds) AS seconds
FROM events WHERE type = 'query.run' GROUP BY {grain}, {key}"""
    return [
        'CREATE TABLE IF NOT EXISTS run_store_schema (version UInt32) ENGINE = TinyLog',
        f"""CREATE TABLE IF NOT EXISTS events (
  id UInt64 CODEC(Delta, ZSTD(1)),
  type LowCardinality(String),
  source LowCardinality(Nullable(String)),
  occurred_at String CODEC(ZSTD(1)),{_RUN_COLUMNS}
  in_history Bool,
  rows UInt64 CODEC(T64, ZSTD(1)),
  seconds Float32 CODEC(ZSTD(1)),
  code LowCardinality(String) DEFAULT '',
  masked Bool DEFAULT false,
  statement String DEFAULT '' CODEC(ZSTD(3)),
  entry String CODEC(ZSTD(3)),
  payload_json String CODEC(ZSTD(3)),
  stamped_at DateTime('UTC') DEFAULT {_STAMPED},
  written_at DateTime64(3, 'UTC') DEFAULT now64(3) CODEC(Delta, ZSTD(1))
) ENGINE = MergeTree PARTITION BY toStartOfHour(stamped_at) ORDER BY id
TTL stamped_at + INTERVAL {events_hours} HOUR
SETTINGS ttl_only_drop_parts = 1, non_replicated_deduplication_window = 1000""",
        f"""CREATE TABLE IF NOT EXISTS history (
  id UInt64,
  executed_at String CODEC(ZSTD(1)),{_RUN_COLUMNS.replace('  connection_name LowCardinality(Nullable(String)),', '')}
  entry String CODEC(ZSTD(3)),
  stamped_at DateTime('UTC') DEFAULT {_STAMPED}
) ENGINE = MergeTree PARTITION BY toYYYYMMDD(stamped_at) ORDER BY (workspace, executed_at, id)
TTL stamped_at + INTERVAL {history_days} DAY
SETTINGS ttl_only_drop_parts = 1, non_replicated_deduplication_window = 1000""",
        f"""CREATE TABLE IF NOT EXISTS history_counts (
  workspace LowCardinality(String),
  query_name LowCardinality(Nullable(String)),
  version Nullable(UInt32),
  key_name LowCardinality(Nullable(String)),
  day Date,
  runs SimpleAggregateFunction(sum, UInt64),
  last SimpleAggregateFunction(max, String)
) ENGINE = AggregatingMergeTree PARTITION BY day ORDER BY (workspace, query_name, version, key_name, day)
TTL day + INTERVAL {history_days} DAY SETTINGS allow_nullable_key = 1""",
        """CREATE MATERIALIZED VIEW IF NOT EXISTS history_counts_mv TO history_counts
DEFINER = CURRENT_USER SQL SECURITY DEFINER
AS SELECT workspace, query_name, version, key_name, toDate(stamped_at) AS day, count() AS runs, max(executed_at) AS last
FROM history GROUP BY workspace, query_name, version, key_name, day""",
        'CREATE TABLE IF NOT EXISTS run_minutes ' + rollup.format(grain='minute', dimensions=_ROLLUP_DIMENSIONS,
                                                                  days=7),
        'CREATE TABLE IF NOT EXISTS run_hours ' + rollup.format(grain='hour', dimensions=_ROLLUP_DIMENSIONS, days=400),
        """CREATE MATERIALIZED VIEW IF NOT EXISTS history_mv TO history
DEFINER = CURRENT_USER SQL SECURITY DEFINER
AS SELECT id, occurred_at AS executed_at, workspace, query_name, version, key_name, status, entry
FROM events WHERE type = 'query.run' AND in_history""",
        'CREATE MATERIALIZED VIEW IF NOT EXISTS run_minutes_mv TO run_minutes DEFINER = CURRENT_USER SQL SECURITY '
        'DEFINER ' + rollup_view.format(fn='toStartOfMinute', grain='minute', key=_ROLLUP_KEY),
        'CREATE MATERIALIZED VIEW IF NOT EXISTS run_hours_mv TO run_hours DEFINER = CURRENT_USER SQL SECURITY '
        'DEFINER ' + rollup_view.format(fn='toStartOfHour', grain='hour', key=_ROLLUP_KEY),
    ]


_ready: set[tuple[int, str]] = set()  # (pid, database) whose tables this process has made sure of
_ready_guard = threading.Lock()


def ensure_schema(target=None):
    """Create the run store's tables if they don't exist, and bring their retention in line with the settings - once
    per process and database (for ``target``: see database())."""
    name = database(target)
    if (os.getpid(), name) in _ready:
        return
    with _ready_guard:
        if (os.getpid(), name) in _ready:
            return
        if not query('SELECT 1 FROM system.databases WHERE name = {n:String}', {'n': name}, db_name='default'):
            execute(f'CREATE DATABASE IF NOT EXISTS {name}', db_name='default')  # a user who may create it, then
        hours, days = config.events_retention_hours(), config.history_retention_days()
        for statement in _tables(hours, days):
            execute(statement, db_name=name)
        _retain('events', f'stamped_at + toIntervalHour({hours})', name)  # as ClickHouse writes them in engine_full
        _retain('history', f'stamped_at + toIntervalDay({days})', name)
        _retain('history_counts', f'day + toIntervalDay({days})', name)
        if not query('SELECT version FROM run_store_schema', db_name=name):
            execute(f'INSERT INTO run_store_schema VALUES ({_SCHEMA_VERSION})', db_name=name)
        _ready.add((os.getpid(), name))


def _retain(table, ttl, name):
    """Apply a changed retention setting to an existing table, without rewriting what it holds: rows already past the
    new limit go as their partitions are next merged or dropped."""
    found = query('SELECT engine_full FROM system.tables WHERE database = {db:String} AND name = {t:String}',
                  {'db': name, 't': table}, db_name=name)
    if found and f'TTL {ttl}' not in found[0]['engine_full']:
        execute(f'ALTER TABLE {table} MODIFY TTL {ttl}', db_name=name, settings={'materialize_ttl_after_modify': 0})


# ---- the clock and the ids ----

class _Clock:
    """ClickHouse's clock, as this process estimates it: its own, plus an offset measured every _CLOCK_SECONDS from
    `now64(3)` and half the round trip."""

    def __init__(self):
        self.offset_ms = 0.0
        self.measured = None
        self.lock = threading.Lock()

    def now_ms(self):
        if self.measured is None or time.monotonic() - self.measured > _CLOCK_SECONDS:
            with self.lock:
                if self.measured is None or time.monotonic() - self.measured > _CLOCK_SECONDS:
                    self._measure()
        return time.time() * 1000 + self.offset_ms

    def _measure(self):
        before = time.time()
        try:
            server = query('SELECT toUnixTimestamp64Milli(now64(3)) AS t', db_name='default')[0]['t']
        except RunStoreError:
            log.warning('Run store: could not read ClickHouse\'s clock; keeping the last offset', exc_info=True)
            self.measured = time.monotonic()
            return
        after = time.time()
        self.offset_ms = server - (before + after) / 2 * 1000
        self.measured = time.monotonic()


clock = _Clock()


def store_ms():
    """Milliseconds since EPOCH_MS on ClickHouse's clock."""
    return int(clock.now_ms()) - EPOCH_MS


def id_floor(ms):
    """The smallest id minted in millisecond ``ms`` (since EPOCH_MS)."""
    return ms * PER_MS


def settled_id():
    """The newest id `queryapigate events` may deliver now: every id minted SETTLE_SECONDS ago or earlier."""
    return id_floor(store_ms() - int(SETTLE_SECONDS * 1000)) + PER_MS - 1


class LeaseError(Exception):
    pass


_LEASE_LOCK_ID = 0x51A9  # next to db's and history's: the PostgreSQL advisory lock leasing takes


def lease(size, holder, target=None):
    """Lease ``size`` of each millisecond's PER_MS ids for ``holder``: the first free block, after lapsed leases are
    cleared. Returns its first number. Not the store's own write lock, and not a commit that clears the metadata cache
    (a lease is no key, query or connection): a lock of its own on PostgreSQL - two writers choosing at once could
    otherwise each see a block free - and a commit that waits for the WAL, as a lease a writer mints from must survive
    a crash. SQLite's BEGIN IMMEDIATE already serialises it."""
    now = time.time()
    postgres = target[0] == 'postgres' if target else db.is_postgres()
    with db.transaction(append_only=True, target=target) as tx:
        if postgres:
            tx.execute('SET LOCAL synchronous_commit TO ON')
            tx.execute('SELECT pg_advisory_xact_lock(?)', (_LEASE_LOCK_ID,))
        tx.execute('DELETE FROM run_writer_leases WHERE expires_at < ?', (now,))
        taken = sorted((row[0], row[1]) for row in
                       tx.execute('SELECT block_start, size FROM run_writer_leases').fetchall())
        start = 0
        for block_start, block_size in taken:
            if block_start - start >= size:
                break
            start = max(start, block_start + block_size)
        if start + size > PER_MS:
            raise LeaseError(f'every one of the {PER_MS} event ids in each millisecond is leased to a running writer - '
                             'too many processes are recording runs at once')
        tx.execute('INSERT INTO run_writer_leases (block_start, size, holder, expires_at) VALUES (?, ?, ?, ?)',
                   (start, size, holder, now + LEASE_SECONDS))
    return start


def renew(block_start, holder, target=None):
    """Extend a lease. False when it had lapsed and is gone (or another writer holds the block now). Append-only: one
    row of this writer's own, so no lock, and the metadata cache isn't cleared every _RENEW_SECONDS (metacache.py)."""
    with db.transaction(append_only=True, target=target) as tx:
        return tx.execute('UPDATE run_writer_leases SET expires_at = ? WHERE block_start = ? AND holder = ?',
                          (time.time() + LEASE_SECONDS, block_start, holder)).rowcount == 1


def release(block_start, holder, target=None):
    try:
        with db.transaction(append_only=True, target=target) as tx:  # its own row, as renew()
            tx.execute('DELETE FROM run_writer_leases WHERE block_start = ? AND holder = ?', (block_start, holder))
    except (OSError, *db.Error):
        pass  # it lapses by itself


class Minter:
    """This process's ids: its leased block of each millisecond, on ClickHouse's clock, never going backwards. A burst
    that fills its block for one millisecond moves on to the next, never more than _MAX_LEAD_MS ahead of the clock."""

    def __init__(self, size=PYTHON_BLOCK, target=None):
        self.size = size
        self.target = target  # the metadata store its lease is in (a db.current_target()), or the current one
        self.holder = f'{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}'
        self.block = None
        self.renewed = 0.0
        self.last_ms = -1
        self.seq = 0
        self.lock = threading.Lock()

    def _ensure_lease(self):
        age = time.monotonic() - self.renewed
        if self.block is not None and age < _RENEW_SECONDS:
            return
        if self.block is not None and age < _MINT_WITHIN and renew(self.block, self.holder, self.target):
            self.renewed = time.monotonic()
            return
        if self.block is not None:
            release(self.block, self.holder, self.target)
        self.block = lease(self.size, self.holder, self.target)
        self.renewed = time.monotonic()
        self.last_ms, self.seq = -1, 0  # a new block: its numbers start again
        if not getattr(self, '_registered', False):
            atexit.register(lambda: self.block is not None and release(self.block, self.holder, self.target))
            self._registered = True

    def mint(self, count):
        """Up to ``count`` new ids, in increasing order - fewer when a burst would run more than _MAX_LEAD_MS ahead."""
        with self.lock:
            self._ensure_lease()
            now = store_ms()
            ms, seq = (self.last_ms, self.seq) if self.last_ms >= now else (now, 0)
            ids = []
            while len(ids) < count:
                if seq >= self.size:
                    ms, seq = ms + 1, 0
                    if ms - now > _MAX_LEAD_MS:
                        break
                ids.append(ms * PER_MS + self.block + seq)
                seq += 1
            self.last_ms, self.seq = ms, seq
            return ids


_minters: dict[tuple, "Minter"] = {}


def minter(target=None):
    """This process's minter for ``target``'s metadata store (where its lease is): one in a server, whose store doesn't
    change; one per store when a process writes to several, as the test suite does."""
    key = (os.getpid(), target)
    if key not in _minters:
        _minters[key] = Minter(target=target)
    return _minters[key]


# ---- writing ----

def _existing_versions(saved, target=None):
    """Which of ``saved`` - (workspace, query, version) - still exist: a run whose version was deleted before its batch
    was written has no history row, as the metadata store's foreign key has it (ADR 0020 §2)."""
    found = set()
    for workspace in {ws for ws, _, _ in saved}:
        names = sorted({name for ws, name, _ in saved if ws == workspace})
        marks = ', '.join('?' * len(names))
        found.update((workspace, row[0], row[1]) for row in db.connection(target).execute(
            f'SELECT query_name, version FROM saved_query_versions WHERE workspace = ? AND query_name IN ({marks})',
            (workspace, *names)).fetchall())
    return found


def _numbers(entry_json):
    """(rows, seconds, columns) of a run's entry: what the rollups and usage reports count by, as columns of their
    own so a report over millions of runs never parses an entry."""
    try:
        entry = json.loads(entry_json)
    except ValueError:
        entry = {}
    rows, duration = entry.get('rows'), entry.get('duration_ms')
    sql = entry.get('sql')
    statement = 'sql:' + sql if isinstance(sql, str) else (
        'sha:' + entry['sql_sha256'] if isinstance(entry.get('sql_sha256'), str) else '')
    code = entry.get('code')  # a run's error code, not one raised here (tests/test_errors.py reads dict literals)
    columns = dict(code=code if isinstance(code, str) else '', masked=bool(entry.get('masked')), statement=statement)
    return (rows if isinstance(rows, int) and not isinstance(rows, bool) and rows >= 0 else 0,
            duration / 1000 if isinstance(duration, (int, float)) and duration >= 0 else 0.0, columns)


def write(rows, target=None):
    """history.write() for the run store: ``rows`` (history._row()'s tuples) as one insert into `events`, and runs that
    are history only (the examples' seeded runs) into `history`. ``target``: the metadata store they were recorded
    against - the writer thread writes after the request, so never "whichever is current" (db.current_target()).
    Never raises: returns how many runs history kept, or None when the batch couldn't be written."""
    try:
        ensure_schema(target)
        saved = {(row[8], row[0], row[1]) for row in rows if row[6] and row[0] is not None}
        existing = _existing_versions(saved, target) if saved else set()
        events_rows, history_rows = [], []
        for name, version, executed_at, entry_json, status, key_name, keep, event, workspace in rows:
            keep = keep and (name is None or (workspace, name, version) in existing)
            if event is None:
                if keep:
                    history_rows.append({'executed_at': executed_at, 'workspace': workspace, 'query_name': name,
                                         'version': version, 'key_name': key_name, 'status': status,
                                         'entry': entry_json})
                continue
            count, seconds, columns = _numbers(entry_json)
            events_rows.append({'type': event[0], 'source': event[1], 'occurred_at': event[2], 'workspace': workspace,
                                'query_name': name, 'version': version, 'key_name': event[3],
                                'connection_name': event[4], 'status': event[6], 'in_history': keep, 'rows': count,
                                'seconds': seconds, **columns, 'entry': entry_json, 'payload_json': ''})
        _send('events', events_rows, target)
        _send('history', history_rows, target)
    except (RunStoreError, LeaseError, OSError, *db.Error):
        log.warning('Could not record %d history entries in the run store', len(rows), exc_info=True)
        metrics.inc_history('failed', len(rows))
        return None
    return sum(1 for row in events_rows if row['in_history']) + len(history_rows)


def _send(table, rows, target=None):
    """Stamp ``rows`` with fresh ids and insert them. A retry is stamped again - rows that were never inserted were
    never visible - unless the first attempt's outcome is unknown and its rows turn out to be there."""
    if not rows:
        return
    name = database(target)
    for attempt in (1, 2):
        ids = minter(target).mint(len(rows))
        if len(ids) < len(rows):
            metrics.inc_history('dropped', len(rows) - len(ids))  # a burst past what the lease allows
            rows = rows[:len(ids)]
        for row, event_id in zip(rows, ids, strict=True):
            row['id'] = event_id
        token = f'{minter(target).holder}:{ids[0]}'
        try:
            insert(table, rows, token=token, db_name=name)
        except RunStoreError as error:
            if attempt == 2 or error.sent is False:
                raise
            if error.sent is None and query(f'SELECT count() AS n FROM {table} WHERE id = {{id:UInt64}}',
                                            {'id': ids[0]}, db_name=name)[0]['n']:
                break  # it did arrive
            continue
        break
    late = store_ms() - ids[-1] // PER_MS - int(SETTLE_SECONDS * 1000)
    if table == 'events' and late > 0:
        metrics.inc_events_late(len(rows))


# ---- reading runs ----

_WINDOWS = (timedelta(minutes=1), timedelta(hours=1), timedelta(days=1))


def _windows(top, floor):
    """Newest-first [low, high) spans of executed_at down to ``floor`` (None: the oldest kept): a minute, an hour and a
    day back from ``top`` (None: now), then everything older - so a read of the newest runs scans only the newest."""
    anchor = datetime.strptime(top, TIME_FORMAT if len(top) > 10 else '%Y-%m-%d') if top else datetime.now()
    high = top
    for span in _WINDOWS:
        low = (anchor - span).strftime(TIME_FORMAT)
        if floor is not None and low <= floor:
            break
        yield low, high
        high = low
    yield floor, high


def newest_first(where, params, limit, top=None, floor=None, columns='*', after=None):
    """Up to ``limit`` history rows matching ``where`` (SQL over history's columns, with {placeholders}), newest
    first: executed_at DESC, id DESC. ``top``: executed_at below this (or, with ``after`` - an id - at it and below that
    id); ``floor``: executed_at at or above it."""
    found = []
    for low, high in _windows(top, floor):
        clauses = [where]
        values = dict(params)
        if low is not None:
            clauses.append('executed_at >= {low:String}')
            values['low'] = low
        if high is not None:
            if after is not None and high == top:
                clauses.append('(executed_at < {high:String} OR (executed_at = {high:String} AND id < {after:UInt64}))')
                values['after'] = after
            else:
                clauses.append('executed_at < {high:String}')
            values['high'] = high
        found += query(f'SELECT {columns} FROM history WHERE {" AND ".join(clauses)} '
                       f'ORDER BY executed_at DESC, id DESC LIMIT {limit - len(found)}', values)
        if len(found) >= limit:
            break
    return found


def history_filter(workspace, query_name=None, version=None, status=None, key=None, kind=None, since=None,
                   until=None):
    """(where, params) for history's filters - GET /api/v1/history's."""
    clauses, params = ['workspace = {ws:String}'], {'ws': workspace}
    for column, value, kind_of in (('query_name', query_name, 'String'), ('version', version, 'UInt32'),
                                   ('status', status, 'String'), ('key_name', key, 'String')):
        if value not in (None, ''):
            clauses.append(f'{column} = {{{column}:{kind_of}}}')
            params[column] = value
    if kind == 'saved':
        clauses.append('query_name IS NOT NULL')
    elif kind == 'adhoc':
        clauses.append('query_name IS NULL')
    if until:
        clauses.append('executed_at < {until:String}')
        params['until'] = until
    return ' AND '.join(clauses), params


def per_version(workspace, name, versions, limit):
    """{version: [entry_json, ...]} - each version's newest ``limit`` runs, oldest first, as the Console lists them."""
    wanted = set(versions)
    found = {version: [] for version in wanted}
    for low, high in _windows(None, None):
        short = sorted(v for v in wanted if len(found[v]) < limit)
        if not short:
            break
        clauses = ['workspace = {ws:String}', 'query_name = {q:String}', 'version IN {vs:Array(UInt32)}']
        values = {'ws': workspace, 'q': name, 'vs': short}
        if low is not None:
            clauses.append('executed_at >= {low:String}')
            values['low'] = low
        if high is not None:
            clauses.append('executed_at < {high:String}')
            values['high'] = high
        for row in query(f'SELECT version, entry FROM history WHERE {" AND ".join(clauses)} '
                         f'ORDER BY executed_at DESC, id DESC LIMIT {limit} BY version', values):
            if len(found[row['version']]) < limit:
                found[row['version']].append(row['entry'])
    return {version: entries[::-1] for version, entries in found.items()}


def forget(caller, dry_run=False):
    """Erase every run and event by ``caller``, and their rollups, in every workspace (ADR 0016) - hidden at once,
    purged from disk once ClickHouse has rewritten their parts. Returns (runs, events) counted before."""
    ensure_schema()
    params = {'c': caller}
    runs = query('SELECT count() AS n FROM history WHERE key_name = {c:String}', params)[0]['n']
    found = query('SELECT count() AS n FROM events WHERE key_name = {c:String}', params)[0]['n']
    if not dry_run:
        for table in ('events', 'history', 'history_counts', 'run_minutes', 'run_hours'):
            execute(f'DELETE FROM {table} WHERE key_name = {{c:String}}', params)
            execute(f'ALTER TABLE {table} APPLY DELETED MASK')
    return runs, found


def delete_runs(workspace, name=None, version=None):
    """Delete a deleted query's (or version's, or workspace's) runs from history - what the metadata store's ON DELETE
    CASCADE does - now, and again once a batch written meanwhile has landed."""
    if not enabled():
        return
    clauses, params = ['workspace = {ws:String}'], {'ws': workspace}
    if name is not None:
        clauses.append('query_name = {q:String}')
        params['q'] = name
    if version is not None:
        clauses.append('version = {v:UInt32}')
        params['v'] = version
    statements = [f'DELETE FROM {table} WHERE {" AND ".join(clauses)}' for table in ('history', 'history_counts')]
    where = database()  # now: the repeat runs on a timer thread, after this request

    def run():
        try:
            for statement in statements:
                execute(statement, params, db_name=where)
        except RunStoreError:
            log.warning('Run store: could not delete the runs of %s', name or workspace, exc_info=True)

    ensure_schema()
    run()
    delay = (config.history_flush_interval() or config.DEFAULT_HISTORY_FLUSH_INTERVAL) + SETTLE_SECONDS
    timer = threading.Timer(delay, run)
    timer.daemon = True
    timer.start()


def _minute_bounds(since, until):
    """The whole minutes within [since, until) - (first, end), or None - which run_minutes counts exactly."""
    def parsed(value):  # a time, or a date (GET /api/v1/usage/agents takes either)
        return datetime.strptime(value, TIME_FORMAT if len(value) > 10 else '%Y-%m-%d')
    first = parsed(since)
    if first.second:
        first = first.replace(second=0) + timedelta(minutes=1)
    end = parsed(until).replace(second=0)
    return (first.strftime(TIME_FORMAT), end.strftime(TIME_FORMAT)) if first < end else None


_LEGACY_IDS = 1 << 40  # ids below this came from the metadata store's event log (_copy_event_log()), not minted


def _id_span(since, until):
    """(low, high) ids that can hold a run that ran in [since, until): an id is stamped when its batch is sent, at or
    after the run - a minute's slack below for clocks, ten minutes above for a queue a slow store left behind. A
    bound for the primary key, to read only what's near the window; the occurred_at filter beside it decides."""
    def ms(value):
        return int(time.mktime(datetime.strptime(value, TIME_FORMAT if len(value) > 10 else '%Y-%m-%d').timetuple())
                   * 1000) - EPOCH_MS
    return id_floor(max(ms(since) - 60_000, 0)), id_floor(ms(until) + 600_000) + PER_MS - 1


def agent_usage(workspace, since, until):
    """usage.summarise()'s items for MCP runs in [since, until), from every run - not history's sample - aggregated
    here rather than read and counted in Python: exact, and never truncated. The window's whole minutes come from
    run_minutes, a few rows a minute whatever the traffic; its partial minutes at either end, and ad-hoc statements,
    from events. The caller makes sure the window is within the event log's retention and normalises statements
    (usage._statement())."""
    ensure_schema()
    params = {'ws': workspace, 'since': since, 'until': until}
    agent = "ifNull(key_name, '-')"
    columns = f"""{agent} AS agent, {{count}} AS calls, sum(rows) AS rows_read, {{errors}} AS errors,
          {{masked}} AS masked_calls, min({{first}}) AS first_seen, max({{last}}) AS last_seen,
          arraySort(groupUniqArray(assumeNotNull(connection_name))) AS connections,
          {{adhoc}} AS adhoc_calls,
          sumMapIf([assumeNotNull(query_name)], [toUInt64({{one}})], query_name IS NOT NULL) AS queries,
          sumMapIf([if(code = '', 'internal_error', code)], [toUInt64({{one}})], status = 'error') AS refusals"""
    from_events = columns.format(count='count()', errors="countIf(status = 'error')", masked='countIf(masked)',
                                 first='occurred_at', last='occurred_at', adhoc='countIf(query_name IS NULL)', one='1')
    from_minutes = columns.format(count='sum(runs)', errors="sumIf(runs, status = 'error')", masked='sum(masked)',
                                  first='first', last='last', adhoc='sumIf(runs, query_name IS NULL)', one='runs')
    run = "type = 'query.run' AND source = 'mcp' AND workspace = {ws:String}"
    near = '((id >= {a{n}:UInt64} AND id <= {b{n}:UInt64}) OR id < {legacy:UInt64})'
    params['legacy'] = _LEGACY_IDS

    def ids(n, low, high):  # the primary-key bound for one span of the window (_id_span())
        params[f'a{n}'], params[f'b{n}'] = _id_span(low, high)
        return near.replace('{n}', str(n))
    minutes = _minute_bounds(since, until)
    events_where, minutes_where = [], None
    if minutes is None:
        events_where.append(f"{ids(0, since, until)} AND occurred_at >= {{since:String}} AND "
                            "occurred_at < {until:String}")
    else:
        params.update(lo=minutes[0], hi=minutes[1])
        events_where.append(f"{ids(0, since, minutes[0])} AND occurred_at >= {{since:String}} AND "
                            "occurred_at < {lo:String}")
        events_where.append(f"{ids(1, minutes[1], until)} AND occurred_at >= {{hi:String}} AND "
                            "occurred_at < {until:String}")
        minutes_where = ("source = 'mcp' AND workspace = {ws:String} AND minute >= toDateTime({lo:String}, 'UTC') "
                         "AND minute < toDateTime({hi:String}, 'UTC')")
    parts = [f"SELECT {from_events} FROM events WHERE {run} AND ({' OR '.join(f'({w})' for w in events_where)}) "
             "GROUP BY agent"]
    if minutes_where:
        parts.append(f"SELECT {from_minutes} FROM run_minutes WHERE {minutes_where} GROUP BY agent")
    found = {}
    for sql in parts:
        for row in query(sql, params):
            item = found.setdefault(row['agent'], {
                'agent': row['agent'], 'calls': 0, 'rows': 0, 'errors': 0, 'masked_calls': 0, 'first_seen': None,
                'last_seen': None, 'connections': [], 'queries': {}, 'adhoc_calls': 0, 'refusals': {},
                'statements': []})
            for field, value in (('calls', 'calls'), ('rows', 'rows_read'), ('errors', 'errors'),
                                 ('masked_calls', 'masked_calls'), ('adhoc_calls', 'adhoc_calls')):
                item[field] += row[value]
            item['first_seen'] = min(filter(None, (item['first_seen'], row['first_seen'])))
            item['last_seen'] = max(filter(None, (item['last_seen'], row['last_seen'])))
            item['connections'] = sorted({*item['connections'], *(c for c in row['connections'] if c)})
            for name, counted in (('queries', row['queries']), ('refusals', row['refusals'])):
                for key, n in zip(*counted, strict=True):
                    item[name][key] = item[name].get(key, 0) + n
    statements = [f"""SELECT {agent} AS agent, statement, count() AS n, sum(rows) AS rows_read,
          countIf(status = 'error') AS errors FROM events
        WHERE {run} AND ({' OR '.join(f'({w})' for w in events_where)}) AND query_name IS NULL AND statement != ''
        GROUP BY agent, statement"""]
    if minutes_where:
        statements.append(f"""SELECT {agent} AS agent, statement, sum(runs) AS n, sum(rows) AS rows_read,
          sumIf(runs, status = 'error') AS errors FROM run_minutes
        WHERE {minutes_where} AND query_name IS NULL AND statement != '' GROUP BY agent, statement""")
    counted = {}
    for sql in statements:
        for row in query(sql, params):
            seen = counted.setdefault((row['agent'], row['statement']), {**row, 'n': 0, 'rows_read': 0, 'errors': 0})
            for field in ('n', 'rows_read', 'errors'):
                seen[field] += row[field]
    for (name, _), row in counted.items():
        found[name]['statements'].append(row)
    return list(found.values())


# ---- the event log ----

def event_payload(row):
    """An event's data, exactly as the metadata store's event log keeps it: a run's rebuilt from its columns
    (events.run_payload()'s shape); any other event's, and one copied from the metadata store, as written."""
    if row['payload_json']:
        return row['payload_json']
    head = json.dumps({'type': row['type'], 'source': row['source'], 'occurred_at': row['occurred_at'],
                       'kind': 'saved' if row['query_name'] is not None else 'adhoc', 'query': row['query_name'],
                       'version': row['version'], 'connection_name': row['connection_name']})
    return f'{head[:-1]}, "entry": {row["entry"]}}}'


EVENT_COLUMNS = ('id, workspace, key_name, type, query_name, version, connection_name, status, source, occurred_at, '
                 'entry, payload_json')


def newest_event_id(upto):
    ensure_schema()
    rows = query('SELECT max(id) AS newest FROM events WHERE id <= {upto:UInt64}', {'upto': upto})
    return rows[0]['newest'] if rows else 0


def events_between(after, upto, limit):
    """Events with ids in (after, upto], oldest first."""
    ensure_schema()
    return query(f'SELECT {EVENT_COLUMNS} FROM events WHERE id > {{after:UInt64}} AND id <= {{upto:UInt64}} '
                 f'ORDER BY id LIMIT {int(limit)}', {'after': after, 'upto': upto})


def counts_per_second(after, upto):
    """{second (an id // (PER_MS * 1000)): events} for ids in (after, upto] - what the events server compares with what
    it delivered, to find a late one."""
    ensure_schema()
    return {row['s']: row['n'] for row in query(
        'SELECT intDiv(id, 4096000) AS s, count() AS n FROM events WHERE id > {after:UInt64} AND id <= {upto:UInt64} '
        'GROUP BY s', {'after': after, 'upto': upto})}


def ids_in_second(second):
    ensure_schema()
    return [row['id'] for row in query(
        'SELECT id FROM events WHERE id >= {lo:UInt64} AND id < {hi:UInt64} ORDER BY id',
        {'lo': second * PER_MS * 1000, 'hi': (second + 1) * PER_MS * 1000})]


def events_by_id(ids):
    ensure_schema()
    return query(f'SELECT {EVENT_COLUMNS} FROM events WHERE id IN {{ids:Array(UInt64)}} ORDER BY id', {'ids': ids})


def replay_page(after, upto, workspace, key_name, filters, since, limit):
    """One page of a resume: the events after ``after`` (up to ``upto``) in ``workspace`` that ``key_name`` may see
    (None: all) and ``filters`` (events.parse_filters()) ask for, from ``since`` on, oldest first, as (id, payload)."""
    ensure_schema()
    clauses = ['id > {after:UInt64}', 'id <= {upto:UInt64}', 'workspace = {ws:String}']
    params = {'after': after, 'upto': upto, 'ws': workspace}
    if key_name is not None:
        clauses.append('key_name = {key:String}')
        params['key'] = key_name
    if since is not None:
        clauses.append('occurred_at >= {since:String}')
        params['since'] = since
    for name, value in filters.items():
        if name == 'kind':
            clauses.append('query_name IS NOT NULL' if value == 'saved' else 'query_name IS NULL')
        else:
            column = {'type': 'type', 'query': 'query_name', 'connection': 'connection_name', 'status': 'status'}[name]
            clauses.append(f'{column} = {{f_{name}:String}}')
            params[f'f_{name}'] = value
    # The page's ids first, from the columns it filters on, then those rows: a rare match is in nearly every granule,
    # and reading `entry` and `payload_json` for all of them is most of what one read would cost
    found = [row['id'] for row in query(
        f'SELECT id FROM events WHERE {" AND ".join(clauses)} ORDER BY id LIMIT {int(limit)}', params)]
    return [(row['id'], event_payload(row)) for row in events_by_id(found)] if found else []


# ---- which store holds runs ----

def check_marker():
    """At startup: the metadata store says where runs are kept (run_store), and this process's setting must agree -
    two instances recording runs in different places would split history and the event log. On first start with
    QUERYAPIGATE_RUN_STORE_URL set, the marker is written and the event log's retention window copied across with its
    ids, so a client that disconnected just before resumes. Raises ValueError, saying why, when they disagree or the
    run store can't be reached."""
    try:
        _check_marker()
    except RunStoreError as error:
        raise ValueError(f'the run store (QUERYAPIGATE_RUN_STORE_URL) could not be used: {error}') from None


def _check_marker():
    row = db.connection().execute('SELECT kind, location, first_id FROM run_store').fetchone()
    if not enabled():
        if row is not None:
            raise ValueError(f'this store\'s runs and events are kept in ClickHouse at {row["location"]} (ADR 0029) - '
                             'set QUERYAPIGATE_RUN_STORE_URL to it, or run `queryapigate runs leave-clickhouse` to '
                             'keep them here again')
        return
    here = location()
    if row is not None:
        if row['location'] != here:
            raise ValueError(f"this store's runs are kept in ClickHouse at {row['location']}, not {here} - "
                             'QUERYAPIGATE_RUN_STORE_URL must name the same server and database on every instance')
        ensure_schema()
        return
    ensure_schema()
    first = id_floor(store_ms())
    copied = _copy_event_log()
    with db.transaction() as tx:
        if tx.execute('SELECT 1 FROM run_store').fetchone() is None:
            tx.execute('INSERT INTO run_store (kind, location, first_id, since) VALUES (?, ?, ?, ?)',
                       ('clickhouse', here, first, datetime.now().strftime(TIME_FORMAT)))
    log.info('Run store: runs, events and usage are kept in ClickHouse at %s from now on (%d events copied)', here,
             copied)


def _copy_event_log():
    """The metadata store's event log, still within its retention, into the run store with the ids clients hold."""
    rows = db.connection().execute(
        'SELECT id, type, source, occurred_at, key_name, connection_name, query_name, status, payload_json, workspace '
        'FROM events ORDER BY id').fetchall()
    batch = []
    for row in rows:
        try:
            payload = json.loads(row['payload_json'])
        except ValueError:
            continue
        is_run = row['type'] == 'query.run'
        count, seconds, columns = _numbers(json.dumps(payload.get('entry'), default=str)) if is_run else (0, 0.0, {})
        # Its old id stands for no time at all: retention counts from when it happened instead
        batch.append({'id': row['id'], 'stamped_at': row['occurred_at'], 'type': row['type'], 'source': row['source'],
                      'occurred_at': row['occurred_at'], 'workspace': row['workspace'],
                      'query_name': row['query_name'], 'version': payload.get('version') if is_run else None,
                      'key_name': row['key_name'], 'connection_name': row['connection_name'], 'status': row['status'],
                      'in_history': False, 'rows': count, 'seconds': seconds, **columns, 'entry': '',
                      'payload_json': row['payload_json']})
    for start in range(0, len(batch), 10_000):
        insert('events', batch[start:start + 10_000], token=f'copy:{batch[start]["id"]}')
    return len(batch)


def copy_history():
    """`queryapigate runs copy-to-clickhouse`: the metadata store's run history into the run store, once - each run
    with a new id, kept for history's retention from when it ran. Returns how many were copied."""
    ensure_schema()
    marker = db.connection().execute('SELECT history_copied FROM run_store').fetchone()
    if marker is None:
        raise ValueError('runs are kept in the metadata store - start a server with QUERYAPIGATE_RUN_STORE_URL set '
                         'first, so the run store is in use')
    if marker[0] is not None:
        raise ValueError(f'this store\'s history was already copied to the run store, at {marker[0]} - copying it '
                         'again would list every run twice')
    rows = db.connection().execute('SELECT workspace, query_name, version, executed_at, entry_json, status, key_name '
                                   'FROM execution_history ORDER BY rowid').fetchall()
    for start in range(0, len(rows), 10_000):
        chunk = rows[start:start + 10_000]
        ids = minter().mint(len(chunk))
        while len(ids) < len(chunk):  # a big copy outruns one lease's ids for a millisecond: wait for the clock
            time.sleep(_MAX_LEAD_MS / 1000)
            ids += minter().mint(len(chunk) - len(ids))
        insert('history', [{'id': event_id, 'stamped_at': row['executed_at'], 'executed_at': row['executed_at'],
                            'workspace': row['workspace'], 'query_name': row['query_name'], 'version': row['version'],
                            'key_name': row['key_name'], 'status': row['status'], 'entry': row['entry_json']}
                           for row, event_id in zip(chunk, ids, strict=True)], token=f'copy-history:{ids[0]}')
    with db.transaction() as tx:
        tx.execute('UPDATE run_store SET history_copied = ?', (datetime.now().strftime(TIME_FORMAT),))
    return len(rows)


def leave():
    """`queryapigate runs leave-clickhouse`: keep runs in the metadata store again. Its event ids continue above the
    newest the run store handed out, so a client holding one still resumes after it; the run store's history and events
    stay where they are, not copied back. Returns the newest id. Start without QUERYAPIGATE_RUN_STORE_URL next."""
    if db.connection().execute('SELECT 1 FROM run_store').fetchone() is None:
        raise ValueError('runs are already kept in the metadata store')
    ensure_schema()
    newest = max(newest_event_id(2 ** 63 - 1), id_floor(store_ms()) + PER_MS)  # ids minted this very millisecond too
    with db.transaction() as tx:
        db._set_events_sequence(tx, db.is_postgres(), newest)
        tx.execute('DELETE FROM run_store')
        tx.execute('DELETE FROM run_writer_leases')
    return newest


def run_counts(workspace, name):
    """{version: runs history holds} for one query - from history_counts, which keeps them as rows arrive and expire."""
    ensure_schema()
    return {row['version']: row['n'] for row in query(
        'SELECT version, sum(runs) AS n FROM history_counts WHERE workspace = {ws:String} AND query_name = {q:String} '
        'GROUP BY version HAVING n > 0', {'ws': workspace, 'q': name})}


def last_run_times(workspace, name=None):
    """{query: when it last ran} over the runs history holds - for one query, or every one."""
    ensure_schema()
    where, params = ('AND query_name = {q:String}', {'q': name}) if name is not None else ('', {})
    return {row['query_name']: row['last'] for row in query(
        f'SELECT query_name, max(last) AS last FROM history_counts WHERE workspace = {{ws:String}} {where} '
        'GROUP BY query_name HAVING sum(runs) > 0', {'ws': workspace, **params})}


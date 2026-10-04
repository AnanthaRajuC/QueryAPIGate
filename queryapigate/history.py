"""Run history: how a saved query's runs reach execution_history, how long they stay, and how they are read back.

Writes are batched. record() never touches the database itself: it appends the run to an in-memory queue and
returns, and a background thread writes everything queued in one transaction every
config.history_flush_interval() seconds (or sooner once a batch fills up). A request therefore never waits on a
history write, and a busy server makes one write per batch instead of one per run. The trade-offs, all bounded:

- **Delay.** Another process (another worker, another instance) sees a run up to one interval later. This
  process never does: every read through store.load_versions() or GET /api/v1/history calls flush() first, so a
  caller always sees its own runs.
- **Loss on a hard kill.** Whatever is still queued when a process is killed outright (SIGKILL, power loss) is
  gone - at most one interval's worth. A normal shutdown flushes at exit.
- **Back-pressure.** The queue holds at most _MAX_PENDING runs. If the store can't keep up (or is down), newer
  runs are dropped and counted (queryapigate_history_runs_total{outcome="dropped"}) rather than ever slowing
  a request or growing memory without bound.

What is kept:

- **Sampling** (config.history_sample_rate()): only that fraction of *successful* runs is recorded. A failed run
  always is - those are the ones worth looking into.
- **Retention**: by default each version keeps its newest config.history_limit() runs, trimmed as each batch is
  written. With config.history_retention_days() set, every run is kept for that many days instead, and a
  periodic sweep deletes older ones - meant for a PostgreSQL store (db.py), where months of runs are fine.

A flush interval of 0 writes each run inside its own request instead, exactly as before batching existed.
"""
import atexit
import base64
import hashlib
import json
import logging
import os
import random
import re
import threading
import time
from datetime import datetime, timedelta

from . import config, db, metrics
from .errors import ApiError

log = logging.getLogger('queryapigate')

_MAX_PENDING = 10_000      # queued runs per process before new ones are dropped
_BATCH_SIZE = 500          # a batch this full is written straight away instead of waiting for the interval
_SWEEP_INTERVAL = 600.0    # seconds between retention sweeps
_SWEEP_CHUNK = 5_000       # rows deleted per sweep transaction, so a big backlog never holds one long transaction
NOTIFY_CHANNEL = 'queryapigate_history'  # Postgres LISTEN/NOTIFY channel: "new runs were recorded"
TIME_FORMAT = '%Y-%m-%d %H:%M:%S'  # executed_at's format (store.now()) - a string compare orders it correctly


def _row(name, version, entry):
    return (name, version, entry.get('executed_at') or datetime.now().strftime(TIME_FORMAT),
            json.dumps(entry, default=str), entry.get('status'), entry.get('key_name'))


class _Writer:
    """One per process (see _writer()): the queue and the one thread that writes it. Only that thread ever writes
    a batch, on its own database connection - a request thread asking for read-your-writes (flush()) waits for
    it rather than writing itself, so a flush can never disturb a request thread's connection or the
    transaction it may be in the middle of."""

    def __init__(self):
        self.pid = os.getpid()
        self.pending = []           # [(target, row)] in arrival order
        self.cond = threading.Condition()  # guards pending/queued/written
        self.queued = 0             # runs ever queued
        self.written = 0            # runs ever taken off the queue and handled (written, or failed)
        self.wake = threading.Event()
        self.thread = None
        self.last_sweep = 0.0

    def add(self, target, row):
        with self.cond:
            if len(self.pending) >= _MAX_PENDING:
                metrics.inc_history('dropped')
                return
            self.pending.append((target, row))
            self.queued += 1
            full = len(self.pending) >= _BATCH_SIZE
        self._ensure_thread()
        if full:
            self.wake.set()

    def unwritten(self):
        """Runs queued but not yet handled - including a batch the writer thread is in the middle of writing."""
        with self.cond:
            return self.queued - self.written

    def wait_until_written(self, timeout=10.0):
        """Block until every run queued so far has been handled by the writer thread - or, if the store is so slow
        that ``timeout`` passes first, go ahead (logged): a history read then may not show the newest runs yet,
        which is better than a request hanging on a struggling store."""
        with self.cond:
            goal = self.queued
            if self.written >= goal:
                return
        self._ensure_thread()
        self.wake.set()
        with self.cond:
            if not self.cond.wait_for(lambda: self.written >= goal, timeout=timeout):
                log.warning('History writer is %d runs behind after %.0fs; reading history without them',
                            goal - self.written, timeout)

    def _ensure_thread(self):
        if self.thread is None or not self.thread.is_alive():
            with self.cond:
                if self.thread is None or not self.thread.is_alive():
                    self.thread = threading.Thread(target=self._run, name='queryapigate-history', daemon=True)
                    self.thread.start()

    def _run(self):
        while True:
            self.wake.wait(timeout=config.history_flush_interval() or config.DEFAULT_HISTORY_FLUSH_INTERVAL)
            self.wake.clear()
            try:
                self.flush()
                self.maybe_sweep()
            except Exception:  # never let the writer thread die - the next round simply tries again
                log.exception('History writer failed')

    def flush(self):
        """Write everything queued - only ever called on the writer thread (or at exit, once it is gone)."""
        with self.cond:
            batch, self.pending = self.pending, []
        by_target = {}
        for target, row in batch:
            by_target.setdefault(target, []).append(row)
        try:
            for target, rows in by_target.items():
                write(rows, target)
        finally:
            with self.cond:
                self.written += len(batch)
                self.cond.notify_all()

    def maybe_sweep(self):
        if config.history_retention_days() is None or time.monotonic() - self.last_sweep < _SWEEP_INTERVAL:
            return
        self.last_sweep = time.monotonic()
        sweep()


_writer_instance = None
_writer_guard = threading.Lock()


def _writer():
    """This process's writer - a fresh one after a fork: the parent's queue (copied into the child) belongs to the
    parent, which will write it, and its thread didn't survive the fork anyway."""
    global _writer_instance
    writer = _writer_instance
    if writer is None or writer.pid != os.getpid():
        with _writer_guard:
            if _writer_instance is None or _writer_instance.pid != os.getpid():
                _writer_instance = _Writer()
            writer = _writer_instance
    return writer


def record(name, version, entry, sample=True):
    """Queue one run of saved query ``name`` at ``version`` for its history. Never raises and never blocks on the
    database (except with a flush interval of 0). ``sample=False`` records it whatever the sample rate - for
    entries that aren't live traffic, such as the examples' seeded history."""
    if sample and entry.get('status') != 'error':
        rate = config.history_sample_rate()
        if rate < 1 and random.random() >= rate:
            metrics.inc_history('sampled_out')
            return
    row = _row(name, version, entry)
    target = db.current_target()
    if config.history_flush_interval() == 0:
        write([row], target)
    else:
        _writer().add(target, row)


def record_adhoc(entry, sql, params=None, sample=True):
    """Queue one ad-hoc run (/execute_sql, /execute_mongo, MCP's execute_sql) for history, like record(): sampled the
    same way (failures always kept), written in the same batches. Keeps its SQL as QUERYAPIGATE_HISTORY_ADHOC_SQL
    says, and the names of its parameters - never their values. Returns the entry as recorded."""
    mode = config.history_adhoc_sql()
    entry = dict(entry)
    if isinstance(sql, str) and mode == 'text':
        entry['sql'] = sql[:config.HISTORY_ADHOC_SQL_MAX]
        if len(sql) > config.HISTORY_ADHOC_SQL_MAX:
            entry['sql_truncated'] = True
    elif isinstance(sql, str) and mode == 'hash':
        entry['sql_sha256'] = hashlib.sha256(sql.encode()).hexdigest()
    entry['params'] = sorted(params or ())
    record(None, None, entry, sample=sample)
    return entry


def flush():
    """Have everything this process has queued written, now - called before every history read, so a process
    always sees its own runs (read-your-writes). The writer thread does the writing; this only waits for it.

    "Queued but not yet written" is queued > written, not "the queue isn't empty": the writer thread takes a batch
    off the queue before it writes it, so a batch still being written leaves the queue empty - checking that
    alone let a read go ahead before the batch's transaction committed, and miss the runs in it."""
    writer = _writer_instance
    if writer is not None and writer.pid == os.getpid() and writer.unwritten():
        writer.wait_until_written()


@atexit.register
def _flush_at_exit():
    writer = _writer_instance
    if writer is None or writer.pid != os.getpid() or not writer.unwritten():
        return
    if writer.thread is not None and writer.thread.is_alive():
        writer.wait_until_written()
    else:
        writer.flush()  # no writer thread left to ask: nothing else can be using this thread's connection now


def pending_count():
    writer = _writer_instance
    return len(writer.pending) if writer is not None and writer.pid == os.getpid() else 0


def write(rows, target=None):
    """Insert ``rows`` (see _row()) in one transaction, then trim each version they touch back to its limit
    (unless a retention period applies instead). A run whose saved query or version was deleted meanwhile is
    skipped, not an error. Never raises: a history write must never fail the work it records."""
    try:
        with db.transaction(append_only=True, target=target) as conn:
            for name, version, executed_at, entry_json, status, key_name in rows:
                if name is None:  # an ad-hoc run: no saved query to belong to
                    conn.execute('INSERT INTO execution_history (query_name, version, executed_at, entry_json, '
                                 'status, key_name) VALUES (NULL, NULL, ?, ?, ?, ?)',
                                 (executed_at, entry_json, status, key_name))
                    continue
                conn.execute(
                    'INSERT INTO execution_history (query_name, version, executed_at, entry_json, status, key_name) '
                    'SELECT ?, ?, ?, ?, ?, ? WHERE EXISTS '
                    '(SELECT 1 FROM saved_query_versions WHERE query_name = ? AND version = ?)',
                    (name, version, executed_at, entry_json, status, key_name, name, version))
            if (target or db.current_target())[0] == 'postgres':
                # Wakes `queryapigate events` (events.py) in every instance. Sent inside the transaction, so
                # Postgres delivers it only once these rows are committed and visible to a listener's query.
                conn.execute(f'NOTIFY {NOTIFY_CHANNEL}')
    except (OSError, *db.Error):
        log.warning('Could not record %d history entries', len(rows), exc_info=True)
        metrics.inc_history('failed', len(rows))
        return
    metrics.inc_history('recorded', len(rows))
    if config.history_retention_days() is None:
        for name, version in dict.fromkeys((row[0], row[1]) for row in rows):
            _trim(name, version, target)


def _trim(name, version, target):
    """Keep only the newest history_limit() rows of one version - or, for ad-hoc runs (name None), the newest
    history_adhoc_limit() of them all. Its own transaction, after the runs are safely
    recorded: on Postgres two concurrent trims of the same version can contend for the same rows, and losing
    that race must never cost a run its history row. A trim that loses is harmless - the next one catches up."""
    try:
        with db.transaction(append_only=True, target=target) as conn:
            if name is None:
                conn.execute("""
                    DELETE FROM execution_history WHERE rowid IN (
                        SELECT rowid FROM execution_history WHERE query_name IS NULL
                        ORDER BY executed_at DESC, rowid DESC LIMIT -1 OFFSET ?
                    )
                """, (config.history_adhoc_limit(),))
                return
            conn.execute("""
                DELETE FROM execution_history WHERE rowid IN (
                    SELECT rowid FROM execution_history WHERE query_name = ? AND version = ?
                    ORDER BY executed_at DESC, rowid DESC LIMIT -1 OFFSET ?
                )
            """, (name, version, config.history_limit()))
    except (OSError, *db.Error):
        log.debug('History trim for %s v%s lost a race or failed; the next one catches up', name, version,
                  exc_info=True)


def sweep(target=None, now=None):
    """Delete runs older than history_retention_days(), in chunks. Returns how many were deleted. Run by each
    process's writer thread every _SWEEP_INTERVAL; several processes sweeping at once is harmless."""
    days = config.history_retention_days()
    if days is None:
        return 0
    cutoff = ((now or datetime.now()) - timedelta(days=days)).strftime(TIME_FORMAT)
    deleted = 0
    while True:
        try:
            with db.transaction(append_only=True, target=target) as conn:
                count = conn.execute(
                    'DELETE FROM execution_history WHERE rowid IN (SELECT rowid FROM execution_history '
                    'WHERE executed_at < ? LIMIT ?)', (cutoff, _SWEEP_CHUNK)).rowcount
        except (OSError, *db.Error):
            log.warning('History retention sweep failed; the next one retries', exc_info=True)
            return deleted
        deleted += count
        if count < _SWEEP_CHUNK:
            return deleted


_DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}( \d{2}:\d{2}:\d{2})?$')
MAX_PAGE = 1000


def _time_bound(value, label):
    if value is None or value == '':
        return None
    value = value.replace('T', ' ')
    if not _DATE_RE.match(value):
        raise ApiError(f"{label} must be a date (YYYY-MM-DD) or a time (YYYY-MM-DD HH:MM:SS)")
    try:
        datetime.strptime(value, TIME_FORMAT if ' ' in value else '%Y-%m-%d')
    except ValueError:
        raise ApiError(f'{label} is not a real date or time') from None
    return value


def _encode_cursor(executed_at, rowid):
    return base64.urlsafe_b64encode(json.dumps([executed_at, rowid]).encode()).decode().rstrip('=')


def _decode_cursor(cursor):
    try:
        executed_at, rowid = json.loads(base64.urlsafe_b64decode(cursor + '=' * (-len(cursor) % 4)))
        if isinstance(executed_at, str) and isinstance(rowid, int):
            return executed_at, rowid
    except (ValueError, TypeError):
        pass
    raise ApiError('cursor is not one this server returned')


def search(query=None, version=None, status=None, key=None, since=None, until=None, limit=100, cursor=None,
           kind=None):
    """One page of every stored run matching the filters, newest first, as (entries, next_cursor) - next_cursor
    is None on the last page. ``since`` is inclusive and ``until`` exclusive; either takes a date or a time
    (executed_at's own format, in this server's local time). Each entry is the run's own record plus the
    saved query and version it belongs to. Backs GET /api/v1/history - the way to look past the newest
    history_limit() runs per version that lists show, e.g. across a retention period's worth of runs."""
    if status not in (None, '', 'success', 'error'):
        raise ApiError("status must be 'success' or 'error'")
    if kind not in (None, '', 'saved', 'adhoc'):
        raise ApiError("kind must be 'saved' or 'adhoc'")
    if not 1 <= limit <= MAX_PAGE:
        raise ApiError(f'limit must be between 1 and {MAX_PAGE}')
    since, until = _time_bound(since, 'since'), _time_bound(until, 'until')
    clauses, params = [], []
    for column, value in (('query_name', query), ('version', version), ('status', status), ('key_name', key)):
        if value not in (None, ''):
            clauses.append(f'{column} = ?')
            params.append(value)
    if kind == 'saved':
        clauses.append('query_name IS NOT NULL')
    elif kind == 'adhoc':
        clauses.append('query_name IS NULL')
    if since:
        clauses.append('executed_at >= ?')
        params.append(since)
    if until:
        clauses.append('executed_at < ?')
        params.append(until)
    if cursor:
        executed_at, rowid = _decode_cursor(cursor)
        clauses.append('(executed_at < ? OR (executed_at = ? AND rowid < ?))')
        params += [executed_at, executed_at, rowid]
    flush()
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ''
    rows = db.connection().execute(
        f'SELECT query_name, version, executed_at, rowid, entry_json FROM execution_history {where} '
        'ORDER BY executed_at DESC, rowid DESC LIMIT ?', (*params, limit + 1)).fetchall()
    entries = [{'query': row['query_name'], 'version': row['version'],
                'kind': 'saved' if row['query_name'] is not None else 'adhoc', **json.loads(row['entry_json'])}
               for row in rows[:limit]]
    last = rows[limit - 1] if len(rows) > limit else None
    return entries, (_encode_cursor(last['executed_at'], last['rowid']) if last is not None else None)


def last_run_times(name=None):
    """{query name: when it last ran} over every stored run - for one query, or (name None) for all of them in
    one grouped read rather than one per query."""
    flush()
    where, params = ('WHERE query_name = ?', (name,)) if name is not None else ('', ())
    rows = db.connection().execute(
        f'SELECT query_name, MAX(executed_at) AS last FROM execution_history {where} GROUP BY query_name',
        params).fetchall()
    return {row['query_name']: row['last'] for row in rows}


def run_counts(name):
    """{version: how many runs are stored} for one query."""
    flush()
    rows = db.connection().execute(
        'SELECT version, COUNT(*) AS n FROM execution_history WHERE query_name = ? GROUP BY version',
        (name,)).fetchall()
    return {row['version']: row['n'] for row in rows}

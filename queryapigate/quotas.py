"""Usage quotas: calls and rows per day or month (ADR 0011, BACKLOG #95) - stable since 0.30 (ADR 0017).

A key or role can carry `"quota": {"calls": 100000, "rows": 10000000, "per": "month"}`. Each caller has its own
counter - a key's, or for signed-in users one per user (`jwt:<subject>`), so a role's quota is each user's allowance.
Periods are calendar days or months in the server's time zone. Counted: successful calls that return data and the
rows they return - saved queries, ad-hoc SQL, MCP tool calls, cached responses. Refused calls aren't.

Checked before a call, counted after it. Kept in the store (usage_counters), so counts survive restarts and every
instance reads them - but not a write per call: they gather in this process and the run-history writer's batch
(history.py) writes them, every QUERYAPIGATE_HISTORY_FLUSH_INTERVAL.

What a check compares against:
- **With QUERYAPIGATE_REDIS_URL** (several instances): a counter in Redis, added to atomically by every instance as
  each call is counted, and seeded from the store the first time a period is used there - so a Redis restart loses
  nothing the store has. A caller can overshoot only by the calls it has in flight at once: checked before, counted
  after.
- **Without it** (or while Redis is failing): the store as this process last read it - at most an interval old - plus
  this process's own unwritten counts. One instance counts exactly; with several, each sees the others' counts only
  once written and re-read, so a burst shorter than that can get up to a quota's worth through each instance.

Either way, the call that crosses the row quota completes: its rows aren't known before it runs (max_rows bounds it).
"""
import threading
import time
from datetime import datetime, timedelta

from . import config, db
from .errors import ApiError

PERIODS = ('day', 'month')
ADJECTIVE = {'day': 'daily', 'month': 'monthly'}
MAX_COUNT = 10**12
KEEP_DAYS = 400  # counters older than this are removed: a year of monthly periods, and some
_TIME = '%Y-%m-%d %H:%M:%S'


def validate(quota):
    """None for none, or {calls?, rows?, per} - at least one count, each a whole number."""
    if quota is None:
        return None
    if not isinstance(quota, dict) or set(quota) - {'calls', 'rows', 'per'}:
        raise ApiError('quota is an object with calls and/or rows, and per ("day" or "month"), or null for none',
                       code='invalid_body')
    if quota.get('per') not in PERIODS:
        raise ApiError('quota.per must be "day" or "month"', code='invalid_body')
    counts = {k: quota[k] for k in ('calls', 'rows') if quota.get(k) is not None}
    if not counts:
        raise ApiError('quota needs calls, rows or both', code='invalid_body')
    for name, value in counts.items():
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_COUNT:
            raise ApiError(f'quota.{name} must be a whole number from 1 to {MAX_COUNT:,}', code='invalid_body')
    return {**counts, 'per': quota['per']}


def period(per, now=None):
    """(period key, its start, when it resets) - calendar periods in the server's time zone."""
    now = now or datetime.now()
    if per == 'day':
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return f'day:{start:%Y-%m-%d}', start, start + timedelta(days=1)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    following = (start + timedelta(days=32)).replace(day=1)
    return f'month:{start:%Y-%m}', start, following


class _Counter:
    """This process's view: what it has counted but not written yet, and what it last read from the store."""

    def __init__(self):
        self.lock = threading.Lock()
        self.pending = {}  # (store, caller, period) -> [calls, rows] not written yet
        self.read = {}     # (caller, period) -> (calls, rows, monotonic time read)

    def add(self, caller, key, calls, rows):
        with self.lock:
            counts = self.pending.setdefault((db.current_target(), caller, key), [0, 0])
            counts[0] += calls
            counts[1] += rows

    def take(self):
        with self.lock:
            taken, self.pending = self.pending, {}
        return taken

    def put_back(self, taken):
        with self.lock:
            for slot, (calls, rows) in taken.items():
                counts = self.pending.setdefault(slot, [0, 0])
                counts[0] += calls
                counts[1] += rows

    def used(self, caller, key):
        """(calls, rows) used in the period: the store, re-read at most once a flush interval, plus unwritten."""
        fresh_for = config.history_flush_interval() or config.DEFAULT_HISTORY_FLUSH_INTERVAL
        with self.lock:
            cached = self.read.get((caller, key))
            pending = list(self.pending.get((db.current_target(), caller, key), (0, 0)))
        if cached is None or time.monotonic() - cached[2] >= fresh_for:
            row = db.connection().execute('SELECT calls, rows FROM usage_counters WHERE caller = ? AND period = ?',
                                          (caller, key)).fetchone()
            cached = (row[0], row[1], time.monotonic()) if row else (0, 0, time.monotonic())
            with self.lock:
                self.read[(caller, key)] = cached
        return cached[0] + pending[0], cached[1] + pending[1]

    def forget_reads(self):
        with self.lock:
            self.read.clear()


_counter = _Counter()
# Held from taking the unwritten counts to storing them, and by forget(): a forget that ran while a write was in flight
# found the counts in neither place - taken from _counter, not yet in the store - and the write then stored them after
# the person was forgotten (CI, 2026-10-14). Reentrant: forget() writes first, under it.
_writing = threading.RLock()


class _Shared:
    """The period's counters in Redis (QUERYAPIGATE_REDIS_URL): one count for every instance. None from used() means
    Redis can't answer now - the caller falls back to the store's view."""

    RETRY_AFTER_FAILURE = 5.0

    def __init__(self, url):
        import redis
        self._error = redis.RedisError
        self._client = redis.Redis.from_url(url, socket_timeout=0.5, socket_connect_timeout=0.5)
        self._skip_until = 0.0

    def _down(self, error):
        from . import ratelimit
        self._skip_until = time.monotonic() + self.RETRY_AFTER_FAILURE
        ratelimit._note_failure(error)

    def _key(self, caller, key):
        return f'qag:quota:{caller}:{key}'

    def used(self, caller, key, seed):
        if time.monotonic() < self._skip_until:
            return None
        name = self._key(caller, key)
        try:
            calls, rows = self._client.hmget(name, 'calls', 'rows')
            if calls is None:  # first use of this period in Redis: start from what the store has
                stored_calls, stored_rows = seed()
                pipe = self._client.pipeline()
                pipe.hsetnx(name, 'calls', stored_calls)
                pipe.hsetnx(name, 'rows', stored_rows)
                pipe.expire(name, 40 * 86400)
                pipe.hmget(name, 'calls', 'rows')
                calls, rows = pipe.execute()[-1]
            return int(calls or 0), int(rows or 0)
        except self._error as error:
            self._down(error)
            return None

    def current(self, *keys):
        """{(caller, period): (calls, rows)} for these periods, as Redis has them - {} if it can't answer."""
        found = {}
        try:
            for key in keys:
                for name in self._client.scan_iter(match=f'qag:quota:*:{key}', count=500):
                    caller = name.decode()[len('qag:quota:'):-len(key) - 1]
                    calls, rows = self._client.hmget(name, 'calls', 'rows')
                    found[(caller, key)] = (int(calls or 0), int(rows or 0))
        except self._error as error:
            self._down(error)
            return {}
        return found

    def add(self, caller, key, calls, rows):
        if time.monotonic() < self._skip_until:
            return
        name = self._key(caller, key)
        try:
            if not self._client.exists(name):
                return  # not seeded yet: the next check seeds it from the store, which these counts will reach
            pipe = self._client.pipeline()
            pipe.hincrby(name, 'calls', calls)
            pipe.hincrby(name, 'rows', rows)
            pipe.execute()
        except self._error as error:
            self._down(error)


_shared: dict = {}  # QUERYAPIGATE_REDIS_URL -> its counters


def _redis():
    url = config.redis_url()
    if not url:
        return None
    if url not in _shared:
        _shared[url] = _Shared(url)
    return _shared[url]


def _used(caller, key):
    """(calls, rows) this period, from Redis when it's set and answering, else the store's view (_Counter)."""
    shared = _redis()
    if shared is not None:
        found = shared.used(caller, key, lambda: _stored(caller, key))
        if found is not None:
            return found
    return _counter.used(caller, key)


def _stored(caller, key):
    """What the store has for the period, with this process's unwritten counts - Redis's starting point."""
    return _counter.used(caller, key)


def write():
    """Add what's been counted to the store(s) it was counted against - the history writer's batch calls this (and
    tests). A failed write puts the counts back, to be written next time rather than lost."""
    with _writing:
        taken = _counter.take()
        if not taken:
            return
        by_target = {}
        for (target, caller, key), counts in taken.items():
            by_target.setdefault(target, {})[(caller, key)] = counts
        stamp = datetime.now().strftime(_TIME)
        try:
            for target, counted in by_target.items():
                _write(target, counted, stamp)
        except Exception:
            _counter.put_back(taken)
            raise
        _counter.forget_reads()


def _write(target, counted, stamp):
    with db.transaction(target=target) as tx:
        for (caller, key), (calls, rows) in counted.items():
            tx.execute('INSERT INTO usage_counters (caller, period, calls, rows, updated_at) VALUES (?, ?, ?, ?, ?) '
                           'ON CONFLICT (caller, period) DO UPDATE SET calls = usage_counters.calls + excluded.calls, '
                           'rows = usage_counters.rows + excluded.rows, updated_at = excluded.updated_at',
                       (caller, key, calls, rows, stamp))
        cutoff = (datetime.now() - timedelta(days=KEEP_DAYS)).strftime(_TIME)
        tx.execute('DELETE FROM usage_counters WHERE updated_at < ?', (cutoff,))


def _ensure_written():
    """Counts are written by the history writer's next batch (it's started if it isn't running) - or now, with a
    flush interval of 0."""
    from . import history
    if config.history_flush_interval() == 0:
        write()
    else:
        history.ensure_writer()


def forget(caller):
    """Delete a caller's usage counts - unwritten, stored and in Redis (ADR 0016: forgetting a person). Their quota's
    count for the period starts again. Returns how many periods there were: what this process hadn't written yet is
    written first, so the answer doesn't depend on whether a background write happened to come first - or was in
    flight: that one is waited for (_writing)."""
    with _writing:
        write()
        with _counter.lock:
            for slot in [s for s in _counter.read if s[0] == caller]:
                del _counter.read[slot]
        with db.transaction() as tx:
            stored = tx.execute('DELETE FROM usage_counters WHERE caller = ?', (caller,)).rowcount
    shared = _redis()
    if shared is not None:
        try:
            names = list(shared._client.scan_iter(match=f'qag:quota:{_glob_escape(caller)}:*', count=500))
            if names:
                shared._client.delete(*names)
        except shared._error as error:
            shared._down(error)
            raise ApiError(f'Redis could not be reached to forget the counts it holds: {error}', 503,
                           code='redis_unavailable') from None
    return stored


def _glob_escape(text):
    """`text` matched literally by Redis's SCAN MATCH: its special characters each escaped with a backslash."""
    return ''.join('\\' + c if c in '*?[]\\' else c for c in text)


def check(permission):
    """Refuse a call - 429 quota_exceeded - once the caller's period is used up. None (no quota) passes."""
    quota = getattr(permission, 'quota', None)
    if not quota:
        return
    key, _start, resets = period(quota['per'])
    calls, rows = _used(permission.name, key)
    for name, used in (('calls', calls), ('rows', rows)):
        if quota.get(name) is not None and used >= quota[name]:
            raise ApiError(f"This caller's {ADJECTIVE[quota['per']]} quota of {quota[name]:,} {name} is used up - it "
                           f'resets at {resets:{_TIME}}', 429, code='quota_exceeded', quota=name, limit=quota[name],
                           resets_at=resets.strftime(_TIME),
                           retry_after=max(1, int((resets - datetime.now()).total_seconds())))


def count(permission, rows):
    """One successful call that returned `rows` rows, against the caller's quota (if it has one)."""
    quota = getattr(permission, 'quota', None)
    if not quota:
        return
    key, rows = period(quota['per'])[0], max(0, int(rows or 0))
    _counter.add(permission.name, key, 1, rows)
    shared = _redis()
    if shared is not None:
        shared.add(permission.name, key, 1, rows)
    _ensure_written()


def count_rows(permission, rows):
    """More rows for a call already counted - a stream's, known only once it has finished."""
    quota = getattr(permission, 'quota', None)
    if not quota or not rows:
        return
    key = period(quota['per'])[0]
    _counter.add(permission.name, key, 0, int(rows))
    shared = _redis()
    if shared is not None:
        shared.add(permission.name, key, 0, int(rows))
    _ensure_written()


def headers(permission):
    """X-Quota-Remaining-Calls/-Rows and X-Quota-Reset for a caller with a quota - so it can slow down first."""
    quota = getattr(permission, 'quota', None)
    if not quota:
        return {}
    key, _start, resets = period(quota['per'])
    calls, rows = _used(permission.name, key)
    out = {'X-Quota-Reset': resets.strftime(_TIME)}
    if quota.get('calls') is not None:
        out['X-Quota-Remaining-Calls'] = str(max(0, quota['calls'] - calls))
    if quota.get('rows') is not None:
        out['X-Quota-Remaining-Rows'] = str(max(0, quota['rows'] - rows))
    return out


def usage(caller, quota):
    """A caller's use of its quota this period, for /api/v1/api-keys - or None without one."""
    if not quota:
        return None
    key, start, resets = period(quota['per'])
    calls, rows = _used(caller, key)
    return {'per': quota['per'], 'period_start': start.strftime(_TIME), 'resets_at': resets.strftime(_TIME),
            'calls': calls, 'rows': rows, 'calls_limit': quota.get('calls'), 'rows_limit': quota.get('rows')}


def current_counters():
    """Every caller's counters for the current day and month, from the store plus this process's unwritten counts -
    GET /api/v1/usage/quotas, for the deployer's own metering."""
    day, month = period('day')[0], period('month')[0]
    found = {(r['caller'], r['period']): [r['calls'], r['rows'], r['updated_at']] for r in db.connection().execute(
        'SELECT caller, period, calls, rows, updated_at FROM usage_counters WHERE period IN (?, ?)',
        (day, month)).fetchall()}
    with _counter.lock:
        for (target, caller, key), (calls, rows) in _counter.pending.items():
            if key in (day, month) and target == db.current_target():
                entry = found.setdefault((caller, key), [0, 0, None])
                entry[0] += calls
                entry[1] += rows
    shared = _redis()
    if shared is not None:  # every instance's counts as of now, not only the ones written so far
        for (caller, key), (calls, rows) in shared.current(day, month).items():
            entry = found.setdefault((caller, key), [0, 0, None])
            entry[0], entry[1] = calls, rows
    return [{'caller': caller, 'per': key.split(':')[0], 'period': key.split(':', 1)[1], 'calls': calls,
             'rows': rows, 'updated_at': updated} for (caller, key), (calls, rows, updated) in sorted(found.items())]

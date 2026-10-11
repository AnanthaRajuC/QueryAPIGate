"""A caller's own limits on concurrency, time and rows (ADR 0011, BACKLOG #94).

A key - or the role a signed-in user's token maps to - can carry `max_concurrent`, `query_timeout` and `max_rows`.
Each narrows the server's own limit, never widens it: the effective limit is the smaller of the two. They're applied
in engine.py, where every front door's queries meet - REST, ad-hoc SQL, MCP tools - so none can skip them. The caller
is the request's (`g.permission`); outside a request (the CLI, a saved export run from the shell) there is none, and
administrators have no limits of their own.

`max_concurrent` counts queries running at once, per caller: from the moment one is sent until its last row is read,
so a stream holds its slot for as long as it streams. With QUERYAPIGATE_REDIS_URL the count is in Redis - one for
every instance - and each slot has a lease (its time limit plus a margin), renewed while a stream runs, so a slot held
by a process that crashed is freed when its lease runs out. Without Redis, or while Redis is failing, each process
counts its own, as rate limits do (ratelimit.degraded()).
"""
import secrets
import threading
import time

from . import config, ratelimit
from .errors import ApiError

LEASE_MARGIN = 30.0  # seconds a slot outlives its query's time limit, for a process that dies holding it
LEASE_WITHOUT_TIMEOUT = 3600.0  # a query with no time limit: renewed while it streams
RENEW_EVERY = 30.0

_ACQUIRE = """
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[1]) then
  return 0
end
redis.call('ZADD', KEYS[1], now + tonumber(ARGV[3]), ARGV[2])
redis.call('EXPIRE', KEYS[1], math.ceil(tonumber(ARGV[3])) + 60)
return 1
"""
_RENEW = """
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
redis.call('ZADD', KEYS[1], 'XX', now + tonumber(ARGV[2]), ARGV[1])
redis.call('EXPIRE', KEYS[1], math.ceil(tonumber(ARGV[2])) + 60)
return 1
"""
_PREFIX = 'qag:running:'


def caller():
    """The current request's Permission, or None (no request, or one not yet authenticated)."""
    from flask import g, has_request_context
    return g.get('permission') if has_request_context() else None


def timeout(requested):
    """The time limit to enforce: the smaller of what was asked for (already within the server's) and the caller's."""
    own = getattr(caller(), 'query_timeout', None)
    if own is None:
        return requested
    return own if requested is None else min(requested, own)


def rows(limit):
    """How many rows one call may return: the smaller of `limit` (the server's, for this kind of call) and the
    caller's own max_rows. `limit` None - no server limit - leaves the caller's."""
    own = getattr(caller(), 'max_rows', None)
    if own is None:
        return limit
    return own if limit is None else min(limit, own)


class _Local:
    """Running queries per caller, in this process - each a lease, as in Redis, so a slot never released (a stream
    abandoned without being closed) is freed when its lease runs out rather than never."""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._running = {}  # caller -> {token: lease end}

    def _live(self, name):
        now = self._clock()
        slots = {t: end for t, end in self._running.get(name, {}).items() if end > now}
        if slots:
            self._running[name] = slots
        else:
            self._running.pop(name, None)
        return slots

    def acquire(self, name, maximum, token, lease):
        with self._lock:
            slots = self._live(name)
            if len(slots) >= maximum:
                return False
            self._running[name] = {**slots, token: self._clock() + lease}
            return True

    def release(self, name, token):
        with self._lock:
            self._running.get(name, {}).pop(token, None)
            self._live(name)

    def renew(self, name, token, lease):
        with self._lock:
            if token in self._running.get(name, {}):
                self._running[name][token] = self._clock() + lease

    def running(self, name):
        with self._lock:
            return len(self._live(name))


class _Shared:
    """Running queries per caller, in Redis: a sorted set of slot tokens scored by when each lease ends."""

    RETRY_AFTER_FAILURE = 5.0

    def __init__(self, url, client=None):
        import redis
        self._error = redis.RedisError
        self._client = client or redis.Redis.from_url(url, socket_timeout=0.5, socket_connect_timeout=0.5)
        self._acquire = self._client.register_script(_ACQUIRE)
        self._renew = self._client.register_script(_RENEW)
        self._fallback = _Local()
        self._skip_until = 0.0
        self._local_tokens = set()  # slots the fallback holds, so their release goes there too

    def _down(self, error):
        self._skip_until = time.monotonic() + self.RETRY_AFTER_FAILURE
        ratelimit._note_failure(error)

    def acquire(self, name, maximum, token, lease):
        if time.monotonic() >= self._skip_until:
            try:
                return bool(self._acquire(keys=[_PREFIX + name], args=[maximum, token, lease]))
            except self._error as error:
                self._down(error)
        else:
            ratelimit._note_failure(None)
        self._local_tokens.add(token)
        return self._fallback.acquire(name, maximum, token, lease)

    def release(self, name, token):
        if token in self._local_tokens:
            self._local_tokens.discard(token)
            return self._fallback.release(name, token)
        try:
            self._client.zrem(_PREFIX + name, token)
        except self._error as error:  # the lease frees it
            self._down(error)

    def renew(self, name, token, lease):
        if token in self._local_tokens:
            return
        try:
            self._renew(keys=[_PREFIX + name], args=[token, lease])
        except self._error as error:
            self._down(error)

    def running(self, name):
        try:
            self._client.zremrangebyscore(_PREFIX + name, '-inf', time.time())
            return int(self._client.zcard(_PREFIX + name))
        except self._error:
            return self._fallback.running(name)


_counters: dict = {}  # QUERYAPIGATE_REDIS_URL (or None) -> its counter
_counters_lock = threading.Lock()


def counter():
    """The running-query counter for this process: shared through Redis when QUERYAPIGATE_REDIS_URL is set."""
    url = config.redis_url()
    with _counters_lock:
        if url not in _counters:
            _counters[url] = _Shared(url) if url else _Local()
        return _counters[url]


class Slot:
    """One running query's place in its caller's max_concurrent - released exactly once, however the query ends."""

    def __init__(self, name, token, lease, store):
        self._name, self._token, self._lease, self._store = name, token, lease, store
        self._renewed = time.monotonic()
        self._released = False

    def renew(self):
        """Keep the lease alive while a stream runs - at most once every RENEW_EVERY seconds."""
        if not self._released and time.monotonic() - self._renewed >= RENEW_EVERY:
            self._renewed = time.monotonic()
            self._store.renew(self._name, self._token, self._lease)

    def release(self):
        if not self._released:
            self._released = True
            self._store.release(self._name, self._token)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.release()


class _NoSlot:
    def renew(self):
        pass

    def release(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass


NO_SLOT = _NoSlot()


def acquire(query_timeout=None):
    """A slot in the caller's max_concurrent for one query - or 429 too_many_concurrent when they're all taken. A
    caller without max_concurrent gets NO_SLOT, which costs nothing. Use as a context manager, or release() it."""
    permission = caller()
    maximum = getattr(permission, 'max_concurrent', None)
    if maximum is None:
        return NO_SLOT
    lease = (query_timeout or LEASE_WITHOUT_TIMEOUT) + LEASE_MARGIN
    token = secrets.token_hex(8)
    store = counter()
    if not store.acquire(permission.name, maximum, token, lease):
        raise ApiError(f'This caller already has {maximum} queries running, its max_concurrent - retry when one '
                       'finishes', 429, code='too_many_concurrent', retry_after=1, max_concurrent=maximum)
    return Slot(permission.name, token, lease, store)


def effective(grants):
    """What a key's or role's limits (its stored grants) come to against the server's now - for /api/v1/api-keys."""
    def smaller(own, server):
        return server if own is None else own if server is None else min(own, server)

    own_rows = grants.get('max_rows')
    return {'max_concurrent': grants.get('max_concurrent'),
            'query_timeout': smaller(grants.get('query_timeout'), config.query_timeout()),
            'max_rows': {'page': smaller(own_rows, config.max_page_size()),
                         'stream': smaller(own_rows, config.stream_max_rows()),
                         'mcp': smaller(own_rows, config.mcp_max_rows())}}

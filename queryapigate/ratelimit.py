"""A small in-memory token-bucket rate limiter, off unless QUERYAPIGATE_RATE_LIMIT is set (e.g. ``60/minute``).

Each client (by IP address) has a bucket holding up to the full quota, refilled continuously, so short bursts are
allowed but the sustained rate is capped. State lives in this process: with several worker processes each has its
own buckets, so the effective limit is multiplied by the number of workers.

``RateLimiter`` handles the server-wide, IP-keyed limit above. ``KeyRateLimiters`` (below) is the same
mechanism applied per API key instead, for a key's own optional ``rate_limit`` grant (see ``apikeys.py``) -
both are checked, not one instead of the other, so a per-key limit can never be used to escape the
server-wide IP-based one.
"""
import logging
import math
import threading
import time
from collections import OrderedDict


class RateLimiter:
    MAX_CLIENTS = 100_000  # bounds memory; the least recently seen clients are forgotten first

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._buckets = OrderedDict()  # client -> [tokens, last_seen]; least recently seen first
        self._spec = None
        self._last_sweep = clock()

    def hit(self, client, count, period):
        """Record a request. Returns (allowed, remaining, retry_after_seconds)."""
        now = self._clock()
        with self._lock:
            if self._spec != (count, period):  # limit changed: start again rather than mix old and new rules
                self._buckets.clear()
                self._spec = (count, period)
            self._sweep(now, period)
            tokens, seen = self._buckets.pop(client, (float(count), now))
            tokens = min(float(count), tokens + (now - seen) * count / period)
            allowed = tokens >= 1
            if allowed:
                tokens -= 1
            self._buckets[client] = (tokens, now)  # re-inserted last: most recently seen
            if len(self._buckets) > self.MAX_CLIENTS:
                self._buckets.popitem(last=False)
            # round first: 20.000000000000004 seconds must not become a 21 second wait
            retry_after = 0 if allowed else max(1, math.ceil(round((1 - tokens) * period / count, 6)))
            return allowed, int(tokens), retry_after

    def _sweep(self, now, period):
        """Forget clients idle for a full period: their bucket has refilled, so they look brand new anyway."""
        if now - self._last_sweep < period:
            return
        self._last_sweep = now
        while self._buckets:
            client, (_, seen) = next(iter(self._buckets.items()))
            if now - seen < period:
                break
            del self._buckets[client]

    def size(self):
        with self._lock:
            return len(self._buckets)


class KeyRateLimiters:
    """Per-API-key rate limiting needs a genuinely separate RateLimiter per distinct (count, period) spec,
    not just a different `client` identifier fed into one shared instance: a single RateLimiter enforces
    exactly one spec at a time and wipes every client's bucket the moment a *different* spec arrives (see
    its own `hit()` - by design, so an admin changing QUERYAPIGATE_RATE_LIMIT doesn't mix old and new rules for
    the IP-based limiter). Two keys configured with different `rate_limit` values are exactly that "two
    different specs" case, so they need their own RateLimiter instances; two keys that happen to share the
    same configured limit correctly share one instance instead (and so still get independent per-key
    buckets within it, keyed by name) - there is no reason to multiply instances beyond one per distinct
    spec actually in use.
    """

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._limiters = {}  # (count, period) -> RateLimiter

    def hit(self, key_name, count, period):
        spec = (count, period)
        with self._lock:
            limiter = self._limiters.get(spec)
            if limiter is None:
                limiter = RateLimiter(self._clock)
                self._limiters[spec] = limiter
        return limiter.hit(key_name, count, period)


# ---- Shared across instances: Redis (BACKLOG #55) ----

# The same token bucket as RateLimiter.hit(), run atomically inside Redis so every instance and process sharing it
# counts against one bucket. Time is Redis's own (TIME), so instances with drifting clocks still agree.
_TOKEN_BUCKET = """
local count = tonumber(ARGV[1])
local period = tonumber(ARGV[2])
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
local state = redis.call('HMGET', KEYS[1], 'tokens', 'seen')
local tokens = tonumber(state[1])
local seen = tonumber(state[2])
if tokens == nil or seen == nil then
  tokens = count
  seen = now
end
tokens = math.min(count, tokens + math.max(0, now - seen) * count / period)
local allowed = 0
if tokens >= 1 then
  tokens = tokens - 1
  allowed = 1
end
redis.call('HSET', KEYS[1], 'tokens', tostring(tokens), 'seen', tostring(now))
redis.call('EXPIRE', KEYS[1], math.ceil(period) + 1)
local retry = 0
if allowed == 0 then
  retry = math.max(1, math.ceil((1 - tokens) * period / count - 0.000001))
end
return {allowed, math.floor(tokens), retry}
"""
_PREFIX = 'qag:ratelimit:'
_DEGRADED_FOR = 600  # seconds a Redis failure keeps the "limits are per instance right now" alert up
_degraded_at = None
_last_warned = 0.0
_state_lock = threading.Lock()


def degraded():
    """When Redis last failed a rate-limit check, if within the last ten minutes (time.time()), else None."""
    with _state_lock:
        return _degraded_at if _degraded_at is not None and time.time() - _degraded_at < _DEGRADED_FOR else None


def _note_failure(error):
    global _degraded_at, _last_warned
    from . import metrics
    metrics.inc_rate_limit_fallback()
    now = time.time()
    with _state_lock:
        _degraded_at = now
        warn = error is not None and now - _last_warned >= 60
        if warn:
            _last_warned = now
    if warn:  # at most once a minute - an outage under load would otherwise flood the log
        logging.getLogger('queryapigate').warning(
            'Rate limits fall back to per-instance counting - Redis is unreachable: %s', error)


class SharedRateLimiter:
    """``hit()`` as RateLimiter/KeyRateLimiters have it, counted in Redis under ``scope`` - one bucket for every
    instance. The spec is part of the Redis key, so a changed limit starts a fresh bucket, as the in-process limiters
    do. If Redis fails, ``fallback`` (an in-process limiter) answers instead: limits stay enforced, per instance,
    rather than lifted or turned into refusals - see degraded()."""

    def __init__(self, url, scope, fallback, client=None):
        import redis
        self._error = redis.RedisError
        self._client = client or redis.Redis.from_url(url, socket_timeout=0.5, socket_connect_timeout=0.5)
        self._script = self._client.register_script(_TOKEN_BUCKET)
        self._scope = scope
        self._fallback = fallback
        self._skip_until = 0.0  # after a failure, Redis isn't tried again for RETRY_AFTER_FAILURE seconds

    RETRY_AFTER_FAILURE = 5.0  # so an outage costs one timeout every few seconds, not one per request

    def hit(self, client, count, period):
        if time.monotonic() < self._skip_until:
            _note_failure(None)
            return self._fallback.hit(client, count, period)
        key = f'{_PREFIX}{self._scope}:{count}/{period:g}:{client}'
        try:
            allowed, remaining, retry_after = self._script(keys=[key], args=[count, period])
        except self._error as error:
            self._skip_until = time.monotonic() + self.RETRY_AFTER_FAILURE
            _note_failure(error)
            return self._fallback.hit(client, count, period)
        return bool(allowed), int(remaining), int(retry_after)


def client_limiter(redis_url=None):
    """The server-wide limiter by client address: shared through Redis when QUERYAPIGATE_REDIS_URL is set."""
    local = RateLimiter()
    return SharedRateLimiter(redis_url, 'client', local) if redis_url else local


def key_limiters(redis_url=None):
    """Per-key (and per signed-in user) limiters: shared through Redis when QUERYAPIGATE_REDIS_URL is set."""
    local = KeyRateLimiters()
    return SharedRateLimiter(redis_url, 'key', local) if redis_url else local

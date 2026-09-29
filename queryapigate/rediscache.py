"""A Redis-backed response cache - same interface as cache.ResponseCache, swapped in by create_app() when
QUERYAPIGATE_REDIS_URL is set. Where the in-process cache is wiped by a restart and duplicated per worker
process (see cache.py's own docstring), this one survives a restart and is shared across however many
instances point at the same Redis - the two backends are otherwise interchangeable to every caller.

A cache failure (Redis unreachable, timeout, ...) is always treated as a miss on get() and a silent no-op
on set() - serving a live query result from the database is always safe; refusing to serve it because the
cache happens to be down is not a tradeoff a response cache should make.
"""
import hashlib
import json
import logging

log = logging.getLogger('queryapigate')

_PREFIX = 'qag:cache:'


class RedisResponseCache:
    def __init__(self, url, client=None):
        if client is not None:
            self._client = client
            import redis
            self._redis_error = redis.RedisError
        else:
            import redis  # imported lazily - only needed once QUERYAPIGATE_REDIS_URL is actually set
            self._client = redis.Redis.from_url(url, decode_responses=False)
            self._redis_error = redis.RedisError

    def get(self, key):
        """(body, content_type, headers, etag) for a live entry, or None if missing, expired or Redis
        itself is unreachable right now."""
        try:
            raw = self._client.hgetall(_PREFIX + key)
        except self._redis_error as e:
            log.warning('Redis cache read failed, treating as a miss: %s', e)
            return None
        if not raw:
            return None
        return raw[b'body'], raw[b'content_type'].decode(), json.loads(raw[b'headers']), raw[b'etag'].decode()

    def set(self, key, body, content_type, headers, ttl):
        """Store a response under `key` for `ttl` seconds and return its ETag - always, even if the write
        to Redis itself fails, since ETag is just a hash of the body the caller already has in hand."""
        etag = hashlib.sha256(body).hexdigest()
        try:
            full_key = _PREFIX + key
            with self._client.pipeline() as pipe:
                pipe.hset(full_key, mapping={'body': body, 'content_type': content_type, 'etag': etag,
                                             'headers': json.dumps(headers)})
                pipe.expire(full_key, ttl)
                pipe.execute()
        except self._redis_error as e:
            log.warning('Redis cache write failed, response was not cached: %s', e)
        return etag

    def size(self):
        try:
            return sum(1 for _ in self._client.scan_iter(_PREFIX + '*'))
        except self._redis_error:
            return 0

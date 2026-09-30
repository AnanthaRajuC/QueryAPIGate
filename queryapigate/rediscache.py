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

    def set(self, key, body, content_type, headers, ttl, meta=None):
        """Store a response under `key` for `ttl` seconds and return its ETag - always, even if the write
        to Redis itself fails, since ETag is just a hash of the body the caller already has in hand.
        ``meta`` is stored alongside for the admin UI's cache browser only - see cache.py's ResponseCache.set()
        docstring; never consulted to serve a hit."""
        etag = hashlib.sha256(body).hexdigest()
        try:
            full_key = _PREFIX + key
            with self._client.pipeline() as pipe:
                pipe.hset(full_key, mapping={'body': body, 'content_type': content_type, 'etag': etag,
                                             'headers': json.dumps(headers), 'meta': json.dumps(meta or {})})
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

    def list_entries(self):
        """Every live entry's metadata for the admin UI's cache browser - never the body itself, so listing
        stays cheap even with large cached responses (a separate get_body() call fetches one in full)."""
        out = []
        try:
            for full_key in self._client.scan_iter(_PREFIX + '*'):
                try:
                    raw = self._client.hmget(full_key, 'meta', 'content_type', 'body')
                    ttl = self._client.ttl(full_key)
                except self._redis_error:
                    continue
                if raw[0] is None or ttl is None or ttl < 0:
                    continue  # gone between the scan and this read, or has no TTL (shouldn't happen - skip)
                meta = json.loads(raw[0]) if raw[0] else {}
                content_type = raw[1].decode() if raw[1] else ''
                size_bytes = len(raw[2]) if raw[2] else 0
                out.append({'key': full_key.decode()[len(_PREFIX):], 'meta': meta, 'content_type': content_type,
                           'size_bytes': size_bytes, 'ttl_remaining_s': float(ttl)})
        except self._redis_error as e:
            log.warning('Redis cache listing failed: %s', e)
        return out

    def get_body(self, key):
        """(body, content_type) for one entry, or None if missing, expired or Redis is unreachable."""
        hit = self.get(key)
        if hit is None:
            return None
        body, content_type, _headers, _etag = hit
        return body, content_type

    def delete(self, key):
        try:
            self._client.delete(_PREFIX + key)
        except self._redis_error as e:
            log.warning('Redis cache delete failed: %s', e)

    def clear(self):
        try:
            keys = list(self._client.scan_iter(_PREFIX + '*'))
            if keys:
                self._client.delete(*keys)
        except self._redis_error as e:
            log.warning('Redis cache clear failed: %s', e)

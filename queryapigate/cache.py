"""Opt-in, in-process TTL response cache for saved queries.

Never applies to ad-hoc ``/execute_sql`` (there is no stable name to hang a TTL off), and the caller
(``run_saved`` in ``app.py``) only ever consults this cache for a saved query whose SQL is read-only -
serving a cached response in place of a write would silently skip that write. Cache keys already include
the resolved parameter values, connection, format and page, so entries are shared across API keys that can
both use the same connection - that reveals nothing a scoped key could not already see by running the
query itself.

Like ``ratelimit.RateLimiter``, one instance lives per Flask app (``app.extensions['queryapigate_cache']``) so
tests get a clean cache instead of leaking entries between them; a multi-process deployment would need a
shared backing store instead of this one process's memory.
"""
import hashlib
import json
import threading
import time
from collections import OrderedDict

MAX_ENTRIES = 10_000  # bounds memory; the least recently used entry is evicted first


class ResponseCache:
    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._entries = OrderedDict()  # key -> (body, content_type, headers, etag, expires_at, meta)

    @staticmethod
    def key(**parts):
        """A stable cache key from whatever makes the response unique (name, version, connection, resolved
        parameter values, format, page, page_size - never raw, unresolved input)."""
        blob = json.dumps(parts, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode('utf-8')).hexdigest()

    def get(self, key):
        """(body, content_type, headers, etag) for a live entry, or None if missing or expired."""
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            body, content_type, headers, etag, expires_at, _meta = entry
            if self._clock() >= expires_at:
                del self._entries[key]
                return None
            self._entries.move_to_end(key)  # most recently used
            return body, content_type, headers, etag

    def set(self, key, body, content_type, headers, ttl, meta=None):
        """Store a response and return its ETag (a hash of the body, so it changes only when content does).
        ``meta`` (name/version/connection/format/page - see app.py's cache_store()) is never used to serve
        a hit; it exists purely so the admin UI's cache browser (BACKLOG: real-time cache entries) can show
        something more useful than an opaque hash - see list_entries()."""
        etag = hashlib.sha256(body).hexdigest()
        with self._lock:
            self._entries[key] = (body, content_type, headers, etag, self._clock() + ttl, meta or {})
            self._entries.move_to_end(key)
            while len(self._entries) > MAX_ENTRIES:
                self._entries.popitem(last=False)
        return etag

    def size(self):
        with self._lock:
            return len(self._entries)

    def list_entries(self):
        """Every live entry's metadata for the admin UI's cache browser - never the body itself (that's a
        separate, explicit get_body() call, so listing stays cheap even with large cached responses)."""
        now = self._clock()
        with self._lock:
            items = list(self._entries.items())
        out = []
        for key, (body, content_type, _headers, _etag, expires_at, meta) in items:
            ttl_remaining = expires_at - now
            if ttl_remaining <= 0:
                continue  # expired since the snapshot above - about to be evicted on its next get()
            out.append({'key': key, 'meta': meta, 'content_type': content_type, 'size_bytes': len(body),
                       'ttl_remaining_s': round(ttl_remaining, 1)})
        return out

    def get_body(self, key):
        """(body, content_type) for one entry regardless of freshness bookkeeping elsewhere - the admin UI's
        "preview this entry" action. None if the key doesn't exist or has expired."""
        hit = self.get(key)
        if hit is None:
            return None
        body, content_type, _headers, _etag = hit
        return body, content_type

    def delete(self, key):
        with self._lock:
            self._entries.pop(key, None)

    def clear(self):
        with self._lock:
            self._entries.clear()

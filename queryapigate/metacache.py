"""A short-lived, in-process cache of the store reads every request makes: the API key a secret belongs to, the saved
query it runs and the connection that query names (QUERYAPIGATE_METADATA_CACHE_TTL, off by default).

What is cached is the rows themselves, never a decision: whether a key is active, expired, used from an allowed address
or still inside a rotation's grace period is checked on every request against the cached row, exactly as against a
fresh one. So a key that expires does so on time. What a TTL can delay is a *change* to a row:

- **Made through this process** (the API, the Console): seen at once. Every store transaction that isn't a log append
  (db.transaction()) clears the whole cache when it commits.
- **Made anywhere else** (another instance, another gunicorn worker, the CLI, an edit straight to the database): seen up
  to one TTL later. A revoked key keeps working on the other instances for that long.

Rows are shared between threads and must be treated as read-only; callers build fresh objects from them, as they do
from a query's result.
"""
import threading
import time

from . import config, db

_MAX_ENTRIES = 4096  # past this the cache starts over, so many distinct keys or queries can't grow it without bound

_lock = threading.Lock()
_entries: dict[tuple, tuple] = {}  # (kind, store target, key) -> (expires at, rows)
_generation = 0  # bumped by clear(): a read that started before a clear never stores what it read


def rows(kind, key, load):
    """``load()``'s rows, as dicts, from the cache when it holds a fresh copy for ``(kind, key)``."""
    ttl = config.metadata_cache_ttl()
    if ttl <= 0:
        return [dict(row) for row in load()]  # the same shape either way: rows read by column name only
    full = (kind, db.current_target(), key)
    now = time.monotonic()
    with _lock:
        hit = _entries.get(full)
        if hit is not None and hit[0] > now:
            return hit[1]
        generation = _generation
    value = [dict(row) for row in load()]
    with _lock:
        if generation == _generation:
            if len(_entries) >= _MAX_ENTRIES:
                _entries.clear()
            _entries[full] = (now + ttl, value)
    return value


def clear():
    global _generation
    with _lock:
        _entries.clear()
        _generation += 1
    for hook in _hooks:
        hook()


_hooks: list = []  # other per-process copies of store rows, dropped whenever this cache is - see on_clear()


def on_clear(hook):
    """Call ``hook()`` whenever the cache is cleared, i.e. after every store change this process commits - for another
    module's copy of store rows that must not outlive a change to them (masking.py's hash key)."""
    _hooks.append(hook)

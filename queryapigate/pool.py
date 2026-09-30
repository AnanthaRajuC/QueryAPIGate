"""A small thread-safe pool of live database connections.

Only *idle* connections are pooled: a connection is checked out exclusively by one request, and on the way
back it is reset (its transaction ended) and kept for reuse - unless the request failed, in which case its
state is unknown and it is closed instead. The number of concurrent connections is not limited here; the
pool only bounds how many idle ones are kept around.
"""
import atexit
import hashlib
import json
import logging
import threading
import time
from collections import deque
from contextlib import contextmanager

from . import config

log = logging.getLogger('queryapigate')

# A connection that has been idle for less than this is trusted without a round trip to check it.
VALIDATE_AFTER = 5.0  # seconds


class Session:
    """A connection plus what the driver wants to remember about it (e.g. limits already applied)."""

    def __init__(self, conn, driver):
        self.conn = conn
        self.driver = driver
        self.state = {}
        self.idle_since = time.monotonic()

    def close(self):
        try:
            self.driver.close(self)
        except Exception:  # a connection that cannot be closed cleanly is gone anyway
            log.debug('Error while closing a connection', exc_info=True)


def fingerprint(details, read_only):
    """Pool key: connections are interchangeable when their settings and read-only mode are identical."""
    blob = json.dumps(details, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode('utf-8')).hexdigest(), bool(read_only)


class ConnectionPool:
    def __init__(self):
        self._idle = {}  # key -> deque of Sessions, least recently used on the left
        self._active = 0  # checked out right now (not capped - see module docstring), for observability only
        self._lock = threading.Lock()

    @contextmanager
    def checkout(self, driver, details, read_only):
        key = fingerprint(details, read_only)
        session = self._acquire(key)
        if session is None:
            session = Session(driver.connect(details, read_only), driver)
        with self._lock:
            self._active += 1
        try:
            yield session
        except BaseException:
            session.close()  # after an error its state is unknown - never reuse it
            raise
        finally:
            with self._lock:
                self._active -= 1
        self._release(key, session)

    # ---- internals ------------------------------------------------------------------

    def _acquire(self, key):
        while True:
            with self._lock:
                queue = self._idle.get(key)
                if not queue:
                    return None
                session = queue.pop()  # most recently used first, so spare connections age out
                if not queue:
                    del self._idle[key]
            idle_for = time.monotonic() - session.idle_since
            if idle_for > config.pool_idle_timeout():
                session.close()
                continue
            if idle_for > VALIDATE_AFTER:
                try:
                    alive = session.driver.is_alive(session)
                except Exception:
                    alive = False
                if not alive:
                    log.info('Discarding a dead pooled connection')
                    session.close()
                    continue
            return session

    def _release(self, key, session):
        try:
            session.driver.reset(session)  # ends the transaction so the next user sees fresh data
        except Exception:
            log.debug('Could not reset a connection; closing it', exc_info=True)
            session.close()
            return
        session.idle_since = time.monotonic()
        surplus = []
        with self._lock:
            queue = self._idle.setdefault(key, deque())
            queue.append(session)
            while len(queue) > config.pool_size():
                surplus.append(queue.popleft())
            surplus.extend(self._expired_locked())
        for old in surplus:
            old.close()

    def _expired_locked(self):
        """Remove and return idle sessions past the idle timeout, across every key (caller holds the lock)."""
        cutoff = time.monotonic() - config.pool_idle_timeout()
        expired = []
        for key in list(self._idle):
            queue = self._idle[key]
            while queue and queue[0].idle_since < cutoff:
                expired.append(queue.popleft())
            if not queue:
                del self._idle[key]
        return expired

    # ---- housekeeping ---------------------------------------------------------------

    def idle_count(self):
        with self._lock:
            return sum(len(q) for q in self._idle.values())

    def active_count(self):
        """Connections checked out right now - across every distinct connection, like idle_count(). Not a
        capacity figure (nothing here is capped - see module docstring): purely for observability."""
        with self._lock:
            return self._active

    def close_all(self):
        with self._lock:
            sessions = [s for queue in self._idle.values() for s in queue]
            self._idle.clear()
        for session in sessions:
            session.close()


_default = ConnectionPool()
atexit.register(_default.close_all)


def close_pooled_connections():
    """Close every idle connection in the shared pool (also runs at interpreter exit)."""
    _default.close_all()


def get_pool():
    """The shared pool, or None when pooling is disabled (QUERYAPIGATE_POOL_SIZE=0)."""
    return _default if config.pool_size() > 0 else None

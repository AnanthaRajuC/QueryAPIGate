"""In-process pub/sub for live UI updates (BACKLOG #43) - one Broadcaster instance per Flask app
(app.extensions['queryapigate_broadcaster'], the same per-app-instance-for-clean-tests reasoning
cache.ResponseCache already documents, not a module-level singleton).

A subscriber is a bounded queue.Queue; publish() never blocks the publishing request - a full queue (a
stuck/slow client) just drops the event rather than back-pressuring whoever is recording it, the same
"never block the real work" trade-off store.record_execution()/record_audit() already make for their own
failures.

A future Redis-pub/sub-backed variant (a RedisBroadcaster, mirroring rediscache.RedisResponseCache) would
implement the same subscribe()/unsubscribe()/publish() shape so multiple *instances* behind a load balancer
could share events - not built yet; this module only covers the single-process case, correct for the
documented single-worker(-process), multi-threaded deployment (see the Dockerfile's --worker-class gthread).
"""
import queue
import threading

_QUEUE_SIZE = 100  # events buffered per slow subscriber before new ones are dropped


class Broadcaster:
    def __init__(self):
        self._lock = threading.Lock()
        self._subscribers: set[queue.Queue] = set()

    def subscribe(self):
        q = queue.Queue(maxsize=_QUEUE_SIZE)
        with self._lock:
            self._subscribers.add(q)
        return q

    def unsubscribe(self, q):
        with self._lock:
            self._subscribers.discard(q)

    def publish(self, event):
        with self._lock:
            subscribers = list(self._subscribers)
        for q in subscribers:
            try:
                q.put_nowait(event)
            except queue.Full:
                pass  # a stuck/slow client misses an event rather than blocking the publisher

    def subscriber_count(self):
        with self._lock:
            return len(self._subscribers)

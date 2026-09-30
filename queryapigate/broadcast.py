"""In-process pub/sub for live UI updates (BACKLOG #43) - one Broadcaster instance per Flask app
(app.extensions['queryapigate_broadcaster'], the same per-app-instance-for-clean-tests reasoning
cache.ResponseCache already documents, not a module-level singleton).

A subscriber is a bounded queue.Queue, registered with an optional ``key_name`` filter: ``None`` means "see
every event" (the admin key's own subscription - it keeps its original, unrestricted view), any other value
means "only events published under this exact key name" - a scoped key's own personal activity feed, not a
shared one. Every publish() call must say which key's activity this event is (see app.py's
_record_and_broadcast()) - there is no "unowned" event a filtered subscriber would see by accident.
publish() never blocks the publishing request - a full queue (a stuck/slow client) just drops the event
rather than back-pressuring whoever is recording it, the same "never block the real work" trade-off
store.record_execution()/record_audit() already make for their own failures.

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
        self._subscribers: dict[queue.Queue, str] = {}  # queue -> key_name filter (None = no filter)

    def subscribe(self, key_name=None):
        q = queue.Queue(maxsize=_QUEUE_SIZE)
        with self._lock:
            self._subscribers[q] = key_name
        return q

    def unsubscribe(self, q):
        with self._lock:
            self._subscribers.pop(q, None)

    def publish(self, event, key_name):
        """``key_name`` is whichever API key's activity this event reports ('admin', a scoped key's own
        name, or '-' for an unauthenticated/open-mode call) - required, not inferred from ``event`` itself,
        so this module never has to know the shape of what it's carrying. A subscriber registered with a
        filter only receives events whose key_name matches exactly; a subscriber with no filter (the admin
        key's own subscription) receives everything, unchanged from before per-subscriber filtering
        existed."""
        with self._lock:
            subscribers = list(self._subscribers.items())
        for q, filter_key in subscribers:
            if filter_key is not None and filter_key != key_name:
                continue
            try:
                q.put_nowait(event)
            except queue.Full:
                pass  # a stuck/slow client misses an event rather than blocking the publisher

    def subscriber_count(self):
        with self._lock:
            return len(self._subscribers)

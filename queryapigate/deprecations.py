"""What is deprecated (BACKLOG #67): still working, with a successor, on its way to removal on the terms of
CHANGELOG.md's "Deprecations" policy.

This is the one list, as experimental.py is for experimental features. Everything that announces a deprecation reads
it: a deprecated route's `Deprecation` and `Link` headers (app.py) and `deprecated: true` in /openapi.json
(openapi.py); a warning in the log, once per process, the first time anything here is used; and the tests that check
the changelog and the docs say so. Removing an entry is removing what it names - only on the policy's terms, and
under **Breaking** in the changelog.
"""
import logging
import threading
from datetime import datetime, timezone

log = logging.getLogger('queryapigate')

# Routes, by URL rule: since (the release that deprecated it), its date (for the Deprecation header), the successor,
# and the earliest release that may remove it.
ROUTES: dict[str, dict[str, str]] = {}  # none today: the 0.14 ones were removed in 0.15

# Everything else that can be deprecated - a CLI command or flag, an environment variable, a behaviour - by key.
OTHER: dict[str, dict[str, str]] = {}  # none today: the pre-SQLite JSON import was removed in 0.15


_warned: set = set()
_lock = threading.Lock()


def header_value(route):
    """RFC 9745's Deprecation header: when it was deprecated, as @<unix seconds>."""
    when = datetime.strptime(ROUTES[route]['date'], '%Y-%m-%d').replace(tzinfo=timezone.utc)
    return f'@{int(when.timestamp())}'


def warn(key):
    """Log, once per process, that a deprecated route (by URL rule) or other thing (by OTHER's key) was used."""
    with _lock:
        if key in _warned:
            return
        _warned.add(key)
    if key in ROUTES:
        entry = ROUTES[key]
        log.warning('%s is deprecated since %s and may be removed in %s: use %s instead (%s).',
                    key, entry['since'], entry['removal'], entry['successor'], entry['why'])
    else:
        entry = OTHER[key]
        log.warning('%s is deprecated since %s and may be removed in %s: %s (%s).',
                    entry['name'], entry['since'], entry['removal'], entry['successor'], entry['why'])

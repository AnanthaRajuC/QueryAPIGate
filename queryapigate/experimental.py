"""What is experimental (BACKLOG #64): outside the compatibility promise in CHANGELOG.md's "Versioning and
compatibility" - it may change or be removed in any minor release, always noted in the changelog.

This is the one list. Everything that marks a feature experimental reads it: the OpenAPI document (`x-experimental`
on its operations, openapi.py), the Settings rows for its environment variables (config.describe_settings()), a
warning logged at startup while one is in use (create_app(), `queryapigate events`), and the tests that check the
docs say so at each feature's own section. Graduating a feature is deleting its entry here, and its docs callout.
"""
from typing import Any

FEATURES: dict[str, dict[str, Any]] = {
    'live_events': {
        'name': 'Live events',
        'why': 'its event ids and payload change when the dedicated event log ships (ADR 0002)',
        'operations': (('get', '/events'),),
        'settings': ('QUERYAPIGATE_EVENTS_PORT', 'QUERYAPIGATE_EVENTS_MAX_CONNECTIONS',
                     'QUERYAPIGATE_EVENTS_POLL_INTERVAL', 'QUERYAPIGATE_EVENTS_MAX_STREAMS'),
        'docs': ('documentation/API.md', 'Live events (Server-Sent Events)'),
    },
    'connection_types': {
        'name': 'H2, JDBC and MongoDB connections',
        'why': 'they are tested less than the other types, and not every feature works on them (allowed_tables, '
               'schema browsing, streaming)',
        'db_types': ('h2', 'jdbc', 'mongo'),
        'docs': ('documentation/DATABASE_CONNECTION_CONFIGURATION.md', 'Experimental: h2, jdbc and mongo'),
    },
    'alerts': {
        'name': 'Alerts',
        'why': 'new in 0.13 - its checks, thresholds and alert shape may change as it is used',
        'operations': (('get', '/api/v1/alerts'),),
        'settings': ('QUERYAPIGATE_ALERT_ERROR_RATE', 'QUERYAPIGATE_ALERT_KEY_UNUSED_DAYS'),
        'docs': ('documentation/API.md', 'Alerts'),
    },
}

DB_TYPES = frozenset(t for f in FEATURES.values() for t in f.get('db_types', ()))
SETTINGS = frozenset(s for f in FEATURES.values() for s in f.get('settings', ()))
OPERATIONS = frozenset(op for f in FEATURES.values() for op in f.get('operations', ()))


def in_use(environ, connections):
    """The experimental features this server is using - an environment variable of theirs set, or a connection of an
    experimental type configured - as {feature id: what shows it}."""
    used = {}
    for key, feature in FEATURES.items():
        set_here = sorted(s for s in feature.get('settings', ()) if environ.get(s, '').strip())
        typed = sorted(name for name, details in connections.items()
                       if isinstance(details, dict) and details.get('db') in feature.get('db_types', ()))
        if set_here:
            used[key] = ', '.join(set_here)
        elif typed:
            used[key] = 'connection ' + ', '.join(typed)
    return used


def warn_in_use(log, environ, connections, also=()):
    """One warning per experimental feature in use (and per id in `also`, e.g. 'live_events' when the events server
    itself starts), so nobody comes to depend on one without having been told."""
    used = in_use(environ, connections)
    for key in also:
        used.setdefault(key, 'started')
    for key, evidence in sorted(used.items()):
        feature = FEATURES[key]
        log.warning('%s are experimental (%s): they may change in any minor release, noted in the changelog - %s.',
                    feature['name'], evidence, feature['why'])

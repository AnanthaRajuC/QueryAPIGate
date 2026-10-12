"""What is experimental (BACKLOG #64): outside the compatibility promise in CHANGELOG.md's "Versioning and
compatibility" - it may change or be removed in any minor release, always noted in the changelog.

This is the one list. Everything that marks a feature experimental reads it: the OpenAPI document (`x-experimental`
on its operations, openapi.py), the Settings rows for its environment variables (config.describe_settings()), a
warning logged at startup while one is in use (create_app(), `queryapigate events`), and the tests that check the
docs say so at each feature's own section.

A feature graduates - its entry deleted here, its callout replaced by a note that it's stable and what may still be
added - when it meets every one of GRADUATION. Alerts, remote files through DuckDB, exports and column masking did in
0.20. The connection types have their own terms (databases.GRADUATION).
"""
from typing import Any

GRADUATION = (
    'it is tested for real in CI - against the real database, store or provider it works with, not only in tests that '
    'run when someone opts in;',
    'if it guards access or data, it has had a security review;',
    'its shape has not changed for a release, and no change to it is planned;',
    'its known gaps are closed, or written down as limits where its documentation describes it;',
    'the versions of what it depends on that pyproject.toml allows are the ones tested (CI tests the oldest);',
    'its documentation says what may still be added - fields, kinds, options - since nothing is removed or renamed '
    'except through the deprecation process.',
)

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
    'mcp_oauth': {
        'name': 'OAuth for the MCP server',
        'why': 'new in 0.18 - its settings, the scope and the answers to refused tokens may change as AI clients use '
               'it (ADR 0006)',
        'settings': ('QUERYAPIGATE_MCP_RESOURCE_URL', 'QUERYAPIGATE_MCP_AUTH_SERVER', 'QUERYAPIGATE_MCP_SCOPE',
                     'QUERYAPIGATE_MCP_ROLE', 'QUERYAPIGATE_MCP_ROLE_CLAIM'),
        'docs': ('documentation/MCP.md', 'Sign-in for AI clients (OAuth)'),
    },
    'descriptions': {
        'name': 'Descriptions',
        'why': 'new in 0.18 - their shape, and where they are served to agents, may change as they are used (ADR 0006)',
        'operations': (('get', '/api/v1/connections/{name}/descriptions'),
                       ('patch', '/api/v1/connections/{name}/descriptions'),
                       ('post', '/api/v1/connections/{name}/descriptions/import')),
        'docs': ('documentation/API.md', 'Descriptions'),
    },
    'agent_usage': {
        'name': 'Agent usage',
        'why': 'new in 0.18 - what is summarised per agent may change as it is used (ADR 0006)',
        'operations': (('get', '/api/v1/usage/agents'),),
        'docs': ('documentation/API.md', 'Agent usage'),
    },
    'agent_writes': {
        'name': 'Write tools over MCP',
        'why': 'new in 0.21 - how agents ask for writes, and how people approve them, may change as it is used '
               '(ADR 0008)',
        'operations': (('get', '/api/v1/write-requests'), ('get', '/api/v1/write-requests/{id}'),
                       ('post', '/api/v1/write-requests/{id}/approve'),
                       ('post', '/api/v1/write-requests/{id}/reject')),
        'settings': ('QUERYAPIGATE_WRITE_APPROVAL_MINUTES', 'QUERYAPIGATE_CONSOLE_URL'),
        'docs': ('documentation/MCP.md', 'Write tools: agents that write, approved by a person'),
    },
    'quotas': {
        'name': 'Usage quotas',
        'why': 'new in 0.24 - what counts as a call, and how precisely, may change once quotas meet real contracts '
               '(ADR 0011)',
        'operations': (('get', '/api/v1/usage/quotas'),),
        'docs': ('documentation/API.md', 'Usage quotas'),
    },
    'audit_stream': {
        'name': 'The audit log streamed to a SIEM',
        'why': 'new in 0.25 - its settings and message format may change as it meets real SIEMs (ADR 0012)',
        'settings': ('QUERYAPIGATE_AUDIT_SYSLOG_URL', 'QUERYAPIGATE_AUDIT_SYSLOG_CA', 'QUERYAPIGATE_AUDIT_SYSLOG_CERT',
                     'QUERYAPIGATE_AUDIT_SYSLOG_KEY', 'QUERYAPIGATE_AUDIT_OTLP'),
        'operations': (('get', '/api/v1/audit/delivery'),),
        'docs': ('documentation/API.md', 'Streaming the audit log to a SIEM'),
    },
    'tracing': {
        'name': 'Tracing with OpenTelemetry',
        'why': 'new in 0.23 - span names and attributes may change as the OpenTelemetry conventions for databases and '
               'AI agents settle (ADR 0010); what never goes into a span will not',
        'settings': ('OTEL_EXPORTER_OTLP_ENDPOINT', 'OTEL_EXPORTER_OTLP_TRACES_ENDPOINT'),
        'docs': ('documentation/API.md', 'Tracing (OpenTelemetry)'),
    },
    'sso': {
        'name': 'Console sign-in through SSO',
        'why': 'new in 0.19 - its settings and the session may change as it is used (ADR 0007)',
        'settings': ('QUERYAPIGATE_SSO_ISSUER', 'QUERYAPIGATE_SSO_CLIENT_ID', 'QUERYAPIGATE_SSO_CLIENT_SECRET',
                     'QUERYAPIGATE_SSO_REDIRECT_URL', 'QUERYAPIGATE_SSO_ROLES', 'QUERYAPIGATE_SSO_GROUPS_CLAIM',
                     'QUERYAPIGATE_SSO_SESSION_HOURS', 'QUERYAPIGATE_SSO_ONLY', 'QUERYAPIGATE_SSO_DEFAULT_ROLE',
                     'QUERYAPIGATE_SSO_ALLOWED_DOMAINS'),
        'docs': ('documentation/API.md', 'Console sign-in through SSO'),
    },
}

DB_TYPES = frozenset(t for f in FEATURES.values() for t in f.get('db_types', ()))
SETTINGS = frozenset(s for f in FEATURES.values() for s in f.get('settings', ()))
OPERATIONS = frozenset(op for f in FEATURES.values() for op in f.get('operations', ()))


def in_use(environ, connections):
    """The experimental features this server is using - an environment variable of theirs set, or a connection of an
    experimental type active - as {feature id: what shows it}. An inactive connection isn't in use: `queryapigate init`
    seeds an inactive template of every type, H2 included, and that alone shouldn't warn."""
    used = {}
    for key, feature in FEATURES.items():
        set_here = sorted(s for s in feature.get('settings', ()) if environ.get(s, '').strip())
        typed = sorted(name for name, details in connections.items()
                       if isinstance(details, dict) and details.get('active', True)
                       and (details.get('db') in feature.get('db_types', ())
                            or ('detect' in feature and feature['detect'](details))))
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
        log.warning('Experimental: %s (%s) - may change in any minor release, noted in the changelog - %s.',
                    feature['name'], evidence, feature['why'])

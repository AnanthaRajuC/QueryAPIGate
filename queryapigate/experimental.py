"""What is experimental (BACKLOG #64): outside the compatibility promise in CHANGELOG.md's "Versioning and
compatibility" - it may change or be removed in any minor release, always noted in the changelog.

This is the one list. Everything that marks a feature experimental reads it: the OpenAPI document (`x-experimental`
on its operations, openapi.py), the Settings rows for its environment variables (config.describe_settings()), a
warning logged at startup while one is in use (create_app(), `queryapigate events`), and the tests that check the
docs say so at each feature's own section.

A feature graduates - its entry deleted here, its callout replaced by a note that it's stable and what may still be
added - when it meets every one of GRADUATION. Alerts, remote files through DuckDB, exports and column masking did in
0.20; descriptions, agent usage, usage quotas, configuration as code, OAuth for the MCP server, write tools and
Console sign-in through SSO in 0.30 (ADR 0017), with the Helm chart; the audit log streamed to a SIEM in 0.32, once
tested against real receivers (ADR 0019); live events, and secrets from files and Vault, in 0.34 (ADR 0021) - AWS's
stay, until tested against AWS. The connection types have their own terms (databases.GRADUATION): MongoDB met them
in 0.37, with streaming exports (ADR 0022).
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
    'connection_types': {
        'name': 'H2, JDBC and Trino connections',
        'why': 'not every feature works on them - allowed_tables on either H2 or JDBC, schema browsing and the query '
               'time limit on JDBC; Trino is fully governed and security-reviewed (ADR 0026) and awaits a release of '
               'stable shape and real use (the support matrix in DATABASE_CONNECTION_CONFIGURATION.md)',
        'db_types': ('h2', 'jdbc', 'trino'),
        'docs': ('documentation/DATABASE_CONNECTION_CONFIGURATION.md', 'Experimental: h2, jdbc and trino'),
    },
    'workspaces': {
        'name': 'Workspaces',
        'why': 'new in 0.38 (ADR 0023) - the routes, memberships and `workspace` fields may change once a real '
               'multi-team deployment uses them; a server with only `default` behaves exactly as before',
        'operations': (('get', '/api/v1/workspaces'), ('post', '/api/v1/workspaces'),
                       ('get', '/api/v1/workspaces/{name}'), ('patch', '/api/v1/workspaces/{name}'),
                       ('delete', '/api/v1/workspaces/{name}'), ('get', '/api/v1/workspaces/{name}/members'),
                       ('put', '/api/v1/workspaces/{name}/members/{admin}'),
                       ('delete', '/api/v1/workspaces/{name}/members/{admin}')),
        'docs': ('documentation/API.md', 'Workspaces'),
    },
    'tracing': {
        'name': 'Tracing with OpenTelemetry',
        'why': 'new in 0.23 - span names and attributes may change as the OpenTelemetry conventions for databases and '
               'AI agents settle (ADR 0010); what never goes into a span will not',
        'settings': ('OTEL_EXPORTER_OTLP_ENDPOINT', 'OTEL_EXPORTER_OTLP_TRACES_ENDPOINT'),
        'docs': ('documentation/API.md', 'Tracing (OpenTelemetry)'),
    },
    'embeddings': {
        'name': 'Semantic search (embed parameters)',
        'why': 'new (ADR 0025) - the embeddings-endpoint settings and the `embed` parameter rule may change as '
               'they meet more vector stores; Postgres/pgvector only for now, and off unless an endpoint is set',
        'settings': ('QUERYAPIGATE_EMBEDDING_URL', 'QUERYAPIGATE_EMBEDDING_MODEL', 'QUERYAPIGATE_EMBEDDING_API_KEY'),
        'docs': ('documentation/API.md', 'Semantic search'),
    },
    'aws_secrets': {
        'name': 'Secrets and credentials from AWS',
        'why': 'tested against a Secrets Manager stand-in, not AWS, and RDS IAM authentication against neither - the '
               'reference forms and settings may change as they meet real AWS (ADR 0021)',
        'settings': ('QUERYAPIGATE_AWS_REGION', 'QUERYAPIGATE_AWS_SECRET_PREFIXES', 'QUERYAPIGATE_AWS_RDS_HOSTS'),
        'docs': ('documentation/API.md', 'Secrets and credentials from AWS'),
    },
    'dataplane': {
        'name': 'The Rust data plane in front of /q/',
        'why': 'decided go in 0.44 (ADR 0028) and shipped in the image and the chart (ADR 0030); what it serves, its '
               'settings and its headers may change before it graduates',
        'settings': ('QUERYAPIGATE_DATAPLANE_SECRET',),
        'docs': ('documentation/DEPLOYMENT.md', 'A Rust data plane in front of /q/'),
    },
    'run_store': {
        'name': 'A ClickHouse run store for history, the event log and usage',
        'why': 'a prototype (ADR 0029, decided go): its tables, its event ids and how it is switched on may change '
               'before it graduates',
        'settings': ('QUERYAPIGATE_RUN_STORE_URL',),
        'docs': ('documentation/DEPLOYMENT.md', 'A ClickHouse run store (prototype)'),
    },
}

DB_TYPES = frozenset(t for f in FEATURES.values() for t in f.get('db_types', ()))
SETTINGS = frozenset(s for f in FEATURES.values() for s in f.get('settings', ()))
OPERATIONS = frozenset(op for f in FEATURES.values() for op in f.get('operations', ()))


def in_use(environ, connections, workspaces=()):
    """The experimental features this server is using - an environment variable of theirs set, a connection of an
    experimental type active, or a workspace besides `default` (`workspaces`: their names) - as {feature id: what shows
    it}. An inactive connection isn't in use: `queryapigate init` seeds an inactive template of every type, H2
    included, and that alone shouldn't warn."""
    used = {}
    others = sorted(name for name in workspaces if name != 'default')
    if others:
        used['workspaces'] = 'workspace ' + ', '.join(others)
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


def warn_in_use(log, environ, connections, workspaces=()):
    """One warning per experimental feature in use, so nobody comes to depend on one without having been told."""
    used = in_use(environ, connections, workspaces)
    for key, evidence in sorted(used.items()):
        feature = FEATURES[key]
        log.warning('Experimental: %s (%s) - may change in any minor release, noted in the changelog - %s.',
                    feature['name'], evidence, feature['why'])

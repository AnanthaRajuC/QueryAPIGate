"""OpenAPI description of the API, served at /openapi.json and rendered at /docs."""
import re
from urllib.parse import quote

from . import config, deprecations, experimental, schema, v1_spec
from .params import json_schema

_FORMAT_PARAM = {'name': 'format', 'in': 'query', 'schema': {
    'type': 'string', 'enum': ['json', 'ndjson', 'csv', 'tsv', 'xml', 'yaml', 'xlsx', 'parquet'], 'default': 'json'}}
_PAGE_PARAMS = [
    {'name': 'page', 'in': 'query', 'schema': {'type': 'integer', 'minimum': 1, 'default': 1}},
    {'name': 'page_size', 'in': 'query', 'schema': {'type': 'integer', 'minimum': 1, 'default': 10},
     'description': 'Upper limit is QUERYAPIGATE_MAX_PAGE_SIZE (default 1000).'},
    {'name': 'timeout', 'in': 'query', 'schema': {'type': 'number', 'minimum': 0, 'exclusiveMinimum': True},
     'description': 'Seconds the query may run before it is cancelled (504). Can lower, never raise, the server '
                    'limit QUERYAPIGATE_QUERY_TIMEOUT (default 30; 0 disables the server limit).'},
]
_ROWS = {'description': 'The requested page. Header X-Has-More says whether another page follows.',
         'headers': {'X-Page': {'schema': {'type': 'integer'}}, 'X-Page-Size': {'schema': {'type': 'integer'}},
                     'X-Has-More': {'schema': {'type': 'string', 'enum': ['true', 'false']}}},
         'content': {'application/json': {'schema': {'type': 'array', 'items': {'type': 'object'}}}}}
_ERRORS = {c: {'$ref': '#/components/responses/Error'} for c in ('400', '401', '403', '404', '429', '500', '504')}
_QUERY_ENTRY_SCHEMA = {'oneOf': [
    {'type': 'string', 'description': 'A saved-query name - read access only.'},
    {'type': 'object', 'description': 'Read access, plus write access to this one query specifically.',
     'properties': {'name': {'type': 'string'}, 'allow_writes': {'type': 'boolean'}}, 'required': ['name']}]}


def _object_schema(properties, required=()):
    """An object schema; OpenAPI 3.0 forbids an empty ``required`` list, so it is only included when non-empty."""
    schema = {'type': 'object', 'properties': properties}
    if required:
        schema['required'] = list(required)
    return schema


def _body(properties, required):
    return {'required': True, 'content': {'application/json': {'schema': _object_schema(properties, required)}}}


_EXEC_PROPS = {
    'connection_name': {'type': 'string'},
    'format': {'type': 'string'},
    'placeholders': {'type': 'object', 'description': 'Values for :name (bound) and {name} (text) parameters.'},
    'params': {'type': 'object', 'description': 'Alias of placeholders.'},
    'version': {'type': 'integer', 'description': 'Saved version to run (default: latest).'},
}


def _saved_query_paths(queries):
    """One documented endpoint per saved query, with its declared parameters and rules."""
    paths, seen = {}, set()
    for query in queries:
        name = query['name']
        has_default_connection = bool(query.get('connection_name'))
        properties, required, query_params = {}, [], []
        for param, spec in query['parameters'].items():
            schema = json_schema(spec)
            entry = {'name': param, 'in': 'query', 'required': spec['required'], 'schema': schema}
            if spec['description']:
                entry['description'] = spec['description']
            query_params.append(entry)
            properties[param] = {**schema, **({'description': spec['description']} if spec['description'] else {})}
            if spec['required']:
                required.append(param)
        connection = {'name': 'connection_name', 'in': 'query', 'required': not has_default_connection,
                      'schema': {'type': 'string', **({'default': query['connection_name']}
                                                      if has_default_connection else {})}}
        common = [connection, {'name': 'version', 'in': 'query', 'schema': {'type': 'integer'},
                               'description': f"Saved version to run (default: latest, currently {query['version']})."},
                  _FORMAT_PARAM, *_PAGE_PARAMS]
        summary = query.get('description') or f'Run the saved query {name}'
        tags = query.get('tags')
        detail = f"Runs version {query['version']} of the saved query '{name}'."
        if tags:
            detail += ' Tags: ' + (tags if isinstance(tags, str) else ', '.join(map(str, tags))) + '.'
        operation_id = 'run_' + re.sub(r'\W', '_', name)
        while operation_id in seen:
            operation_id += '_'
        seen.add(operation_id)
        body_properties = {'params': _object_schema(properties, required),
                           'connection_name': connection['schema'], 'version': {'type': 'integer'},
                           'format': {'type': 'string'}, 'timeout': {'type': 'number'}}
        paths['/q/' + quote(name, safe='')] = {
            'get': {'summary': summary, 'description': detail, 'tags': ['Saved queries (live)'],
                    'operationId': operation_id, 'parameters': [*query_params, *common],
                    'responses': {'200': _ROWS, **_ERRORS}},
            'post': {'summary': summary + ' (parameters in a JSON body)', 'description': detail,
                     'tags': ['Saved queries (live)'], 'operationId': operation_id + '_post',
                     'parameters': [_FORMAT_PARAM, *_PAGE_PARAMS],
                     'requestBody': {'required': bool(required), 'content': {'application/json': {
                         'schema': _object_schema(body_properties)}}},
                     'responses': {'200': _ROWS, **_ERRORS}},
        }
    return paths


def build_spec(version, saved_queries=None, jwt=None):
    """`jwt` (default: whether JWT is configured) adds the BearerAuth scheme - pinned by dump() so the committed
    copy never depends on the environment it was generated in."""
    if jwt is None:
        jwt = config.jwt_enabled()
    spec = {
        'openapi': '3.0.3',
        'info': {'title': 'QueryAPIGate', 'version': version,
                 'description': 'Run SQL against configured databases and get the results back over HTTP.'},
        'components': {
            'securitySchemes': {'ApiKey': {'type': 'apiKey', 'in': 'header', 'name': 'X-API-Key'},
                                **({'BearerAuth': {'type': 'http', 'scheme': 'bearer', 'bearerFormat': 'JWT',
                                                   'description': "A signed-in user's token (see jwtauth.py)"}}
                                   if jwt else {})},
            'responses': {'Error': {'description': 'Error', 'content': {'application/json': {'schema': {
                '$ref': '#/components/schemas/V1Error'}}}}},  # one error shape everywhere (errors.py)
        },
        'security': [{}, {'ApiKey': []}, *([{'BearerAuth': []}] if jwt else [])],
        'paths': {
            '/execute_sql': {'post': {
                'summary': 'Execute SQL', 'tags': ['Query'],
                'parameters': [_FORMAT_PARAM, *_PAGE_PARAMS],
                'requestBody': _body({'sql': {'type': 'string'}, 'connection_name': {'type': 'string'},
                                      'params': {'type': 'object', 'additionalProperties': True,
                                                 'description': 'Values for :name bound parameters.'},
                                      'database': {'type': 'string', 'description':
                                          'Run against a different database on the same server than this '
                                          "connection's own configured one (admin only - 403 for a scoped "
                                          'key, even one already granted this connection).'}},
                                     ['sql', 'connection_name']),
                'responses': {'200': _ROWS, **_ERRORS}}},
            '/execute_mongo': {'post': {
                'summary': 'Run a Mongo find() query - read-only (BACKLOG #36: find-only for now)',
                'tags': ['Query'],
                'parameters': [_FORMAT_PARAM, *_PAGE_PARAMS],
                'requestBody': _body({'collection': {'type': 'string'}, 'connection_name': {'type': 'string'},
                                      'filter': {'type': 'object', 'description':
                                          'A find() filter document. ":name" string leaves are bound '
                                          'parameters, filled from params.'},
                                      'projection': {'type': 'object'}, 'sort': {'type': 'object'},
                                      'params': {'type': 'object',
                                                 'description': 'Values for :name bound parameters.'}},
                                     ['collection', 'connection_name']),
                'responses': {'200': _ROWS, **_ERRORS}}},
            '/q/{name}': {
                'parameters': [{'name': 'name', 'in': 'path', 'required': True, 'schema': {'type': 'string'}}],
                'get': {'summary': 'Run a saved query; extra query-string arguments become parameters',
                        'tags': ['Saved queries'],
                        'parameters': [_FORMAT_PARAM, *_PAGE_PARAMS,
                                       {'name': 'connection_name', 'in': 'query', 'schema': {'type': 'string'}},
                                       {'name': 'version', 'in': 'query', 'schema': {'type': 'integer'}}],
                        'responses': {'200': _ROWS, **_ERRORS}},
                'post': {'summary': 'Run a saved query with a JSON body of parameters', 'tags': ['Saved queries'],
                         'parameters': [_FORMAT_PARAM, *_PAGE_PARAMS],
                         'requestBody': _body(_EXEC_PROPS, []),
                         'responses': {'200': _ROWS, **_ERRORS}}},
            '/events': {'get': {
                'summary': 'Live saved-query execution events (Server-Sent Events)', 'tags': ['Saved queries'],
                'description': 'One `data: {...}` line per saved-query execution as it happens - what keeps '
                    'the admin UI\'s Home screen live. Any authenticated key may '
                    'connect: the admin key sees every execution; a scoped key sees only executions it '
                    'triggered itself (its own personal activity feed, filtered by API key name).',
                'responses': {'200': {'description': 'text/event-stream of execution events',
                                      'content': {'text/event-stream': {'schema': {'type': 'string'}}}},
                             **_ERRORS}}},
            '/connections/{name}/schema': {'get': {
                'summary': "List a connection's tables/views and their columns", 'tags': ['Connections'],
                'description': "'database' browses a different database on the same server than the "
                    "connection's own configured one (admin only; a scoped key gets 403 on it, even one "
                    "granted this connection) - Run SQL's own database picker, not a new grant.",
                'parameters': [{'name': 'name', 'in': 'path', 'required': True, 'schema': {'type': 'string'}},
                               {'name': 'database', 'in': 'query', 'required': False, 'schema': {'type': 'string'}}],
                'responses': {'200': {'description': 'Tables and columns', 'content': {'application/json': {
                    'schema': {'type': 'object', 'properties': {
                        'tables': {'type': 'array', 'items': {'type': 'object', 'properties': {
                            'name': {'type': 'string'}, 'type': {'type': 'string', 'enum': ['table', 'view']},
                            'columns': {'type': 'array', 'items': {'type': 'object', 'properties': {
                                'name': {'type': 'string'}, 'type': {'type': 'string'},
                                'nullable': {'type': 'boolean'}, 'position': {'type': 'integer'}}}}}}},
                        'truncated': {'type': 'boolean', 'description':
                            f'True if the schema has more than {schema.ROW_CAP} columns and was cut off.'}}}}}},
                    **_ERRORS}}},
            '/connections/{name}/table_ddl': {'get': {
                'summary': "A table's real CREATE TABLE text", 'tags': ['Connections'],
                'description': "Only 'mysql', 'sqlite' and 'clickhouse' connections support this - "
                    "Postgres has no single-statement DDL dump, H2/DuckDB are unverified here, and Mongo "
                    "has no DDL at all. 'database' is the same admin-only database-override as the schema "
                    "endpoint above.",
                'parameters': [{'name': 'name', 'in': 'path', 'required': True, 'schema': {'type': 'string'}},
                               {'name': 'table', 'in': 'query', 'required': True, 'schema': {'type': 'string'}},
                               {'name': 'database', 'in': 'query', 'required': False, 'schema': {'type': 'string'}}],
                'responses': {'200': {'description': 'The real CREATE TABLE text', 'content': {'application/json': {
                    'schema': {'type': 'object', 'properties': {'ddl': {'type': 'string'}}}}}},
                    **_ERRORS}}},
            '/catalog': {'get': {
                'summary': 'Every saved query this caller can reach, and the terms it is offered under',
                'tags': ['Saved queries'],
                'description': 'Not just what /openapi.json already documents (parameters, description, '
                    'connection) but the governance half it has no field for: whether a response can be '
                    'cached and for how long, whether this specific caller can write through this query '
                    '(may differ per query - see per-query write curation under Authentication and '
                    'permissions), and this caller\'s own rate limit alongside the server-wide one. Scoped '
                    'the same way /openapi.json already scopes its saved-query list - a query this caller '
                    'cannot reach through /q/<name> is never listed here either. Requires authentication '
                    'like any other functional endpoint (unlike /openapi.json, this is never public), since '
                    'the whole point is answering "what can *I* use."',
                'responses': {'200': {'description': 'Reachable queries and this caller\'s own terms',
                    'content': {'application/json': {'schema': {'type': 'object', 'properties': {
                        'queries': {'type': 'array', 'items': {'type': 'object', 'properties': {
                            'name': {'type': 'string'}, 'version': {'type': 'integer'},
                            'description': {'type': 'string'}, 'tags': {'type': 'array', 'items':
                                {'type': 'string'}}, 'connection_name': {'type': 'string'},
                            'parameters': {'type': 'object'},
                            'cache_ttl': {'type': 'integer', 'nullable': True, 'description':
                                'Seconds a response may be served from cache, or null when uncached.'},
                            'can_write': {'type': 'boolean', 'description':
                                'Whether this caller specifically can write through this query.'},
                            'masking': {'type': 'object', 'description':
                                'Only for a caller with column-masking rules: the result columns it gets masked, and '
                                'how - or, in refused, why its rules would refuse this query (experimental).',
                                'properties': {
                                    'masked': {'type': 'array', 'items': {'type': 'object', 'properties': {
                                        'column': {'type': 'string'}, 'action': {'type': 'string'}}}},
                                    'refused': {'type': 'string', 'nullable': True}}}}}},
                        'caller': {'type': 'object', 'properties': {
                            'name': {'type': 'string', 'nullable': True},
                            'admin': {'type': 'boolean'}, 'allow_writes': {'type': 'boolean'},
                            'allowed_write_ops': {'type': 'array', 'items': {'type': 'string'}, 'nullable': True},
                            'mask': {'type': 'array', 'nullable': True, 'items': {'type': 'object'},
                                     'description': "This caller's column-masking rules, or null for none."},
                            'rate_limit': {'type': 'string', 'nullable': True, 'description':
                                'This caller\'s own additional limit, e.g. "100/minute", or null when it has '
                                'none of its own.'},
                            'server_rate_limit': {'type': 'string', 'nullable': True, 'description':
                                'QUERYAPIGATE_RATE_LIMIT, checked in addition to rate_limit above, or null when '
                                'the server has no limit configured.'}}}}}}}}, **_ERRORS}}},
            '/health': {'get': {'summary': 'Liveness check', 'tags': ['Service'],
                                'responses': {'200': {'description': 'OK', 'content': {'application/json': {
                                    'schema': {'type': 'object', 'required': ['status', 'version'], 'properties': {
                                        'status': {'type': 'string', 'enum': ['ok']},
                                        'version': {'type': 'string', 'description': 'Server version.'},
                                        'time_zone': {'type': 'string', 'nullable': True, 'description':
                                                      "IANA name of the zone the server's timestamps are in, where "
                                                      'it can tell (e.g. Asia/Kolkata).'},
                                        'utc_offset': {'type': 'string', 'description':
                                                       "That zone's current UTC offset, e.g. +05:30."}}}}}}}}},
            '/auth/sso/login': {'get': {
                'summary': "Console sign-in through SSO: redirects to the identity provider (ADR 0007)",
                'tags': ['Console sign-in'], 'security': [],
                'parameters': [{'name': 'next', 'in': 'query', 'required': False, 'schema': {'type': 'string'},
                                'description': 'The Console page to land on - only ever a /console/ path.'}],
                'responses': {'302': {'description': "To the provider's sign-in"},
                              '404': {'description': 'SSO is not configured'}}}},
            '/auth/sso/callback': {'get': {
                'summary': 'Where the identity provider sends the person back: starts a Console session',
                'tags': ['Console sign-in'], 'security': [],
                'responses': {'302': {'description': 'To the Console - signed in, or with sso_error saying why not'},
                              '404': {'description': 'SSO is not configured'}}}},
            '/auth/session': {'get': {
                'summary': "Whether SSO is on, and the Console session's administrator and CSRF token",
                'tags': ['Console sign-in'], 'security': [],
                'responses': {'200': {'description': 'OK', 'content': {'application/json': {'schema': {
                    'type': 'object', 'required': ['sso', 'signed_in', 'sso_only'], 'properties': {
                        'sso': {'type': 'boolean'}, 'signed_in': {'type': 'boolean'}, 'sso_only': {'type': 'boolean'},
                        'admin': {'type': 'string'}, 'role': {'type': 'string'},
                        'email': {'type': 'string', 'nullable': True}, 'expires_at': {'type': 'string'},
                        'csrf': {'type': 'string', 'description': 'Send as X-QAG-CSRF with every request the '
                                                                  'session cookie should authenticate.'}}}}}}}}},
            '/auth/sso/logout': {'post': {
                'summary': 'Ends the Console session (needs X-QAG-CSRF)', 'tags': ['Console sign-in'],
                'security': [],
                'responses': {'200': {'description': "Signed out; provider_sign_out is the provider's own sign-out, "
                                                     'where it has one', 'content': {'application/json': {'schema': {
                                                         'type': 'object', 'required': ['signed_out'],
                                                         'properties': {'signed_out': {'type': 'boolean'},
                                                                        'provider_sign_out': {'type': 'string',
                                                                                              'nullable': True}}}}}},
                              '401': {'description': 'Not signed in'}}}},
            '/metrics': {'get': {
                'summary': 'Prometheus text-format metrics: request/query counts and latencies, pool occupancy, '
                           'rate-limit rejections', 'tags': ['Service'],
                'responses': {'200': {'description': 'Metrics', 'content': {'text/plain': {'schema': {
                    'type': 'string'}}}}}}},
        },
    }
    # The Management API (/api/v1, ADR 0001) - fully described, responses included.
    spec['components']['schemas'] = dict(v1_spec.SCHEMAS)
    spec['paths'].update(v1_spec.PATHS)
    # The legacy saved-query management routes /api/v1/queries replaces: still working, marked deprecated.
    for path, entry in deprecations.ROUTES.items():  # on their way out (deprecations.py)
        for method, operation in list(spec['paths'][path].items()):
            spec['paths'][path] = {**spec['paths'][path], method: {
                **operation, 'deprecated': True,
                'description': (f"**Deprecated** since {entry['since']} - use `{entry['successor']}`; may be removed "
                                f"in {entry['removal']}. " + operation.get('description', '')).strip()}}
    # Outside the compatibility promise (experimental.py, BACKLOG #64): flagged for tools, and said for people.
    for method, path in experimental.OPERATIONS:
        # Copies: the operations are module-level dicts (v1_spec.PATHS) shared by every build of this document
        operation = dict(spec['paths'][path][method])
        operation['x-experimental'] = True
        operation['description'] = ('**Experimental** - may change in any minor release, noted in the changelog. '
                                    + operation.get('description', '')).strip()
        spec['paths'][path] = {**spec['paths'][path], method: operation}
    if saved_queries:
        spec['paths'].update(_saved_query_paths(saved_queries))
        spec['tags'] = [{'name': 'Saved queries (live)',
                         'description': 'One endpoint per saved query, generated from its published version: its '
                                        'declared parameters and their rules, and its default connection.'}]
    return spec


# /docs is where people type their API key, so what it loads is pinned (ADR 0010): one exact Swagger UI release, each
# file with its Subresource Integrity hash - a changed file is refused by the browser - and a CSP naming exactly those.
# Change the version and the hashes together; tests/test_security_headers.py checks the hashes against the CDN.
# 5.33.0's files are identical to the npm package's (checked when pinned, 2026-10-11).
SWAGGER_UI_VERSION = '5.33.0'
SWAGGER_UI_BASE = f'https://cdn.jsdelivr.net/npm/swagger-ui-dist@{SWAGGER_UI_VERSION}/'
SWAGGER_UI_FILES = {
    'swagger-ui.css': 'sha384-Ov4/wv3j2bmct8cDc5X4ngJZohVPzEmc6uDPH8WeljUxO5vtoykvMEfbu9Vh6RaW',
    'swagger-ui-bundle.js': 'sha384-YDALVcy8kj8yltLBVi1vBiBAUqdxvus673gM8XKwiy6aDUJFXivF/KCufekjYbVf',
}

_DOCS_SCRIPT = """
  var stored = '';
  try { stored = sessionStorage.getItem('queryapigate-key') || ''; } catch (e) {}
  document.getElementById('key').value = stored;
  document.getElementById('save-key').onclick = function () {
    try { sessionStorage.setItem('queryapigate-key', document.getElementById('key').value); } catch (e) {}
    location.reload();
  };
  SwaggerUIBundle({
    url: 'openapi.json', dom_id: '#ui', validatorUrl: null,
    requestInterceptor: function (req) { if (stored) { req.headers['X-API-Key'] = stored; } return req; }
  });
"""


def _sri(name):
    return f'integrity="{SWAGGER_UI_FILES[name]}" crossorigin="anonymous"'


def _script_hash(text):
    import base64
    import hashlib
    return "'sha256-" + base64.b64encode(hashlib.sha256(text.encode()).digest()).decode() + "'"


# Swagger UI sets inline style attributes, so styles need 'unsafe-inline'; scripts are only the pinned bundle and
# this page's own script, by hash. Requests ("Try it out", openapi.json) go to this server only.
DOCS_CSP = (f"default-src 'none'; script-src {SWAGGER_UI_BASE}swagger-ui-bundle.js {_script_hash(_DOCS_SCRIPT)}; "
            f"style-src {SWAGGER_UI_BASE}swagger-ui.css 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")

DOCS_HTML = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>QueryAPIGate docs</title>
<link rel="stylesheet" href="{SWAGGER_UI_BASE}swagger-ui.css" {_sri('swagger-ui.css')}>
<style>
  body {{ margin: 0; font-family: sans-serif; }}
  #key-bar {{ padding: 8px 16px; background: #f4f4f4; border-bottom: 1px solid #ddd; font-size: 14px; }}
  #key-bar input {{ width: 260px; padding: 4px; }}
</style></head>
<body>
<div id="key-bar">
  <a href="console/">Admin UI</a> &middot;
  API key (only needed if the server sets QUERYAPIGATE_API_KEY; it reveals your saved queries below and is
  sent with "Try it out" requests; kept for this browser tab only):
  <input id="key" type="password" autocomplete="off" placeholder="X-API-Key">
  <button id="save-key">Apply</button>
</div>
<div id="ui"></div>
<script src="{SWAGGER_UI_BASE}swagger-ui-bundle.js" {_sri('swagger-ui-bundle.js')}></script>
<script>{_DOCS_SCRIPT}</script></body></html>
"""


def dump():
    """The static API description (no saved queries, no environment-dependent parts, version pinned to "dev"),
    as stable, sorted JSON. frontend/openapi.json is this output, committed: the Console's TypeScript types are
    generated from it, so the frontend build needs no Python, and a test fails when the two drift apart.
    Regenerate it with:  python frontend/scripts/dump_openapi.py"""
    import json
    return json.dumps(build_spec('dev', jwt=False), indent=2, sort_keys=True) + '\n'

"""OpenAPI description of the API, served at /openapi.json and rendered at /docs."""
import re
from urllib.parse import quote

from . import config, schema, v1_spec
from .params import json_schema

_FORMAT_PARAM = {'name': 'format', 'in': 'query', 'schema': {
    'type': 'string', 'enum': ['json', 'ndjson', 'csv', 'tsv', 'xml', 'yaml', 'xlsx'], 'default': 'json'}}
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
    saved = {'filepath': {'type': 'string', 'description': 'Saved query name or path inside saved_sql/.'},
             **_EXEC_PROPS}
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
                'type': 'object', 'properties': {'error': {'type': 'string'}, 'detail': {'type': 'string'}}}}}}},
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
            '/execute_sql_from_file': {'post': {
                'summary': 'Run the latest version of a saved query', 'tags': ['Saved queries'],
                'parameters': [_FORMAT_PARAM, *_PAGE_PARAMS],
                'requestBody': _body(saved, ['filepath']), 'responses': {'200': _ROWS, **_ERRORS}}},
            '/execute_sql_with_parameters_from_file': {'post': {
                'summary': 'Run a saved query with parameter values', 'tags': ['Saved queries'],
                'parameters': [_FORMAT_PARAM, *_PAGE_PARAMS],
                'requestBody': _body(saved, ['filepath']), 'responses': {'200': _ROWS, **_ERRORS}}},
            '/save_sql_to_file': {'patch': {
                'summary': 'Save a query (creates the next version)', 'tags': ['Saved queries'],
                'requestBody': _body({
                    'filename': {'type': 'string'},
                    'query_type': {'type': 'string', 'enum': ['sql', 'mongo'], 'default': 'sql', 'description':
                        "'sql' (the default) needs sql_query; 'mongo' needs mongo_collection and mongo_filter."},
                    'sql_query': {'type': 'string', 'description': 'Required when query_type is sql.'},
                    'mongo_collection': {'type': 'string', 'description': 'Required when query_type is mongo.'},
                    'mongo_filter': {'type': 'object', 'description':
                        'A find() filter document. ":name" string leaves are bound parameters, the JSON-'
                        'document equivalent of :name in sql_query.'},
                    'mongo_projection': {'type': 'object'}, 'mongo_sort': {'type': 'object'},
                    'author': {'type': 'string'},
                    'description': {'type': 'string'}, 'tags': {'type': 'array', 'items': {'type': 'string'}},
                    'query_parameters': {'type': 'object', 'example': {'actor_id': 'int'}},
                    'connection_name': {'type': 'string', 'description': 'Default connection for /q/{name}.'},
                    'cache_ttl': {'type': 'integer', 'minimum': 0, 'description':
                        'Seconds to cache a response for; never used for a query that writes, and not yet '
                        'supported for query_type mongo.'},
                    'collection': {'type': 'string', 'nullable': True, 'description':
                        'The collection to file this query under (lowercase letters, digits, ".", "_", "-"). '
                        'Belongs to the query, not the version: omitted keeps the current one, null removes '
                        'it. A key granted that collection reaches this query - see PUT '
                        '/saved_sql/{name}/collection.'}},
                    ['filename', 'author', 'description']),
                'responses': {'200': {'description': 'Saved'}, **_ERRORS}}},
            '/list_files': {'get': {
                'summary': 'List saved queries', 'tags': ['Saved queries'],
                'parameters': [
                    {'name': 'sort_by', 'in': 'query', 'schema': {'type': 'string', 'enum': ['name', 'modified']}},
                    {'name': 'sort_order', 'in': 'query', 'schema': {'type': 'string', 'enum': ['asc', 'desc']}}],
                'responses': {'200': {'description': 'Saved queries with version metadata'}, **_ERRORS}}},
            '/events': {'get': {
                'summary': 'Live saved-query execution events (Server-Sent Events)', 'tags': ['Saved queries'],
                'description': 'One `data: {...}` line per saved-query execution as it happens - feeds the '
                    'admin UI\'s Home tab instead of it polling /list_files. Any authenticated key may '
                    'connect: the admin key sees every execution; a scoped key sees only executions it '
                    'triggered itself (its own personal activity feed, filtered by API key name).',
                'responses': {'200': {'description': 'text/event-stream of execution events',
                                      'content': {'text/event-stream': {'schema': {'type': 'string'}}}},
                             **_ERRORS}}},
            '/view_file_content': {'get': {
                'summary': 'Raw content of a saved query file', 'tags': ['Saved queries'],
                'parameters': [{'name': 'filename', 'in': 'query', 'required': True, 'schema': {'type': 'string'}}],
                'responses': {'200': {'description': 'File content'}, **_ERRORS}}},
            '/query_flow': {'get': {
                'summary': "Best-effort tables/joins a saved query's SQL touches", 'tags': ['Saved queries'],
                'description': 'Real SQL parsing (via sqlglot) for the Access tab\'s query-flow panel; not '
                    'available for h2, jdbc or mongo connections, or a query sqlglot cannot parse - those '
                    'come back with an explanatory "error" and empty tables/joins rather than failing.',
                'parameters': [{'name': 'filename', 'in': 'query', 'required': True, 'schema': {'type': 'string'}},
                               {'name': 'version', 'in': 'query', 'schema': {'type': 'integer'}}],
                'responses': {'200': {'description': 'Detected tables and joins, or an explanatory error'},
                             **_ERRORS}}},
            '/saved_sql/{name}': {'delete': {
                'summary': 'Delete a saved query, or one version with ?version=', 'tags': ['Saved queries'],
                'parameters': [{'name': 'name', 'in': 'path', 'required': True, 'schema': {'type': 'string'}},
                               {'name': 'version', 'in': 'query', 'schema': {'type': 'integer'}}],
                'responses': {'200': {'description': 'Deleted'}, **_ERRORS}}},
            '/saved_sql/{name}/collection': {'put': {
                'summary': 'Move a saved query into a collection, or out of any (admin only)',
                'tags': ['Saved queries'],
                'description': 'Not a new version. A key\'s collection grant is live, so this can change what '
                    'other keys reach: the response and the audit log entry list exactly which keys and roles '
                    'gain or lose access to this query.',
                'parameters': [{'name': 'name', 'in': 'path', 'required': True, 'schema': {'type': 'string'}}],
                'requestBody': _body({'collection': {'type': 'string', 'nullable': True}}, ['collection']),
                'responses': {'200': {'description': 'Moved; includes from, to and the access gained or lost'},
                              **_ERRORS}}},
            '/saved_sql/{name}/cache_ttl': {'put': {
                'summary': "Set or clear one version's cache_ttl in place (admin only)",
                'tags': ['Saved queries'],
                'description': 'Not a new version - operational metadata, not the query definition. '
                    '?version= targets a specific version; omitted means the latest. 0 or null turns '
                    'caching off for that version.',
                'parameters': [{'name': 'name', 'in': 'path', 'required': True, 'schema': {'type': 'string'}},
                               {'name': 'version', 'in': 'query', 'schema': {'type': 'integer'}}],
                'requestBody': _body({'cache_ttl': {'type': 'integer', 'nullable': True}}, ['cache_ttl']),
                'responses': {'200': {'description': 'Updated; includes the resolved version and cache_ttl'},
                              **_ERRORS}}},
            '/collections': {'get': {
                'summary': 'Every collection: its queries, and the keys and roles that reach it (admin only)',
                'tags': ['Saved queries'],
                'description': 'Also lists a collection that only a grant still names (no queries left), and '
                    'the queries in no collection, so an inert grant is visible rather than silent.',
                'responses': {'200': {'description': 'collections and uncollected'}, **_ERRORS}}},
            '/examples': {
                'get': {'summary': 'Which example APIs are installed (admin only)', 'tags': ['Saved queries'],
                        'responses': {'200': {'description': 'loaded, partial, and the example queries, roles and '
                                                             'collections present'}, **_ERRORS}},
                'post': {'summary': 'Install the example APIs (admin only)', 'tags': ['Saved queries'],
                         'description': 'Four worked scenarios - reporting, dashboard, export, partner - as saved '
                             'queries in collections, matching roles and a small generated SQLite connection '
                             'named "examples". Everything is marked as an example. Idempotent; 409 and nothing '
                             'changed if something that is not an example already holds one of their names. '
                             'No API key is created.',
                         'responses': {'200': {'description': 'What was added, and the resulting status'},
                                       **_ERRORS}},
                'delete': {'summary': 'Remove the example APIs (admin only)', 'tags': ['Saved queries'],
                           'description': 'Removes exactly what is marked as an example and nothing else, and '
                               'lists any keys that were granted an example collection (now inert).',
                           'responses': {'200': {'description': 'What was removed'}, **_ERRORS}}},
            '/collections/{name}/postman': {'get': {
                'summary': 'Download a collection as a Postman Collection v2.1 file (admin only)',
                'tags': ['Saved queries'],
                'description': 'One GET /q/{name} request per query (latest version) with its parameters filled '
                    'in from their own rules, authenticating from a {{apiKey}} variable left empty - the file '
                    'never holds a key. A snapshot: export again after queries change.',
                'parameters': [{'name': 'name', 'in': 'path', 'required': True, 'schema': {'type': 'string'}}],
                'responses': {'200': {'description': 'A Postman Collection v2.1 JSON document'}, **_ERRORS}}},
            '/collections/{name}': {'patch': {
                'summary': 'Rename a collection, carrying every key and role grant with it (admin only)',
                'tags': ['Saved queries'],
                'description': 'Resumable and never narrows access part-way. An existing target needs '
                    'merge=true - which is also how to finish a rename that was interrupted.',
                'parameters': [{'name': 'name', 'in': 'path', 'required': True, 'schema': {'type': 'string'}}],
                'requestBody': _body({'name': {'type': 'string'}, 'merge': {'type': 'boolean', 'default': False}},
                                     ['name']),
                'responses': {'200': {'description': 'Renamed; lists the queries, keys and roles changed'},
                              **_ERRORS}}},
            '/connections': {
                'get': {'summary': 'List connections (passwords masked)', 'tags': ['Connections'],
                        'responses': {'200': {'description': 'Connections'}, **_ERRORS}},
                'patch': {'summary': 'Add or update connections', 'tags': ['Connections'],
                          'requestBody': _body({'connections': {
                              'type': 'object', 'description': 'Map of name to connection details.',
                              'additionalProperties': {'type': 'object', 'properties': {
                                  'db': {'type': 'string', 'enum': list(config.SUPPORTED_DB_TYPES)},
                                  'host': {'type': 'string'}, 'port': {'type': 'integer'},
                                  'user': {'type': 'string'}, 'password': {'type': 'string'},
                                  'database': {'type': 'string'}, 'active': {'type': 'boolean'},
                                  'jar': {'type': 'string', 'description':
                                      "db: 'jdbc' only - path to the vendor's JDBC driver .jar."},
                                  'driver_class': {'type': 'string', 'description':
                                      "db: 'jdbc' only - the driver's fully-qualified Java class name."},
                                  'jdbc_url': {'type': 'string', 'description':
                                      "db: 'jdbc' only - the full JDBC connection URL."}}}}},
                              ['connections']),
                          'responses': {'200': {'description': 'Updated'}, **_ERRORS}}},
            '/connections/test': {'post': {
                'summary': "Try connecting with the given fields before saving them (admin only)",
                'tags': ['Connections'],
                'description': "The same fields PATCH /connections takes for one connection, but tried "
                    "immediately rather than saved - so a typo'd host or a firewalled port is found out "
                    "before it becomes a saved connection. A password equal to the masked placeholder "
                    "GET /connections returns is resolved to that connection's real stored password first, "
                    "by 'name' if one is given.",
                'requestBody': _body({
                    'name': {'type': 'string',
                             'description': 'Only to resolve a masked password; not required otherwise.'},
                    'db': {'type': 'string', 'enum': list(config.SUPPORTED_DB_TYPES)},
                    'host': {'type': 'string'}, 'port': {'type': 'integer'},
                    'user': {'type': 'string'}, 'password': {'type': 'string'}, 'database': {'type': 'string'},
                    'jar': {'type': 'string'}, 'driver_class': {'type': 'string'}, 'jdbc_url': {'type': 'string'}},
                    ['db']),
                'responses': {'200': {'description': 'Connected'}, **_ERRORS}}},
            '/connections/databases': {'post': {
                'summary': "List every database on a connection's server (admin only)", 'tags': ['Connections'],
                'description': "Give 'name' alone for an already-saved connection - Run SQL's own database "
                    "picker. Give the connection's own fields, as POST /connections/test does, to probe one "
                    "that isn't saved yet - the New/Edit connection form's 'Default database' dropdown - or "
                    "to try changed fields before saving them. Only supported for mysql, postgres and "
                    "clickhouse connections; sqlite and duckdb are a single file, and h2/jdbc have no one "
                    "catalogue query that works across every database reachable that way.",
                'requestBody': _body({
                    'name': {'type': 'string', 'description': 'An already-saved connection to list databases for.'},
                    'db': {'type': 'string', 'enum': list(config.SUPPORTED_DB_TYPES)},
                    'host': {'type': 'string'}, 'port': {'type': 'integer'},
                    'user': {'type': 'string'}, 'password': {'type': 'string'}, 'database': {'type': 'string'}}, []),
                'responses': {'200': {'description': 'Database names'}, **_ERRORS}}},
            '/connections/{name}': {'delete': {
                'summary': 'Delete a connection', 'tags': ['Connections'],
                'description': 'A reason is required - deleting a connection breaks every saved query that '
                    "used it, so it is not left to guesswork afterward. Recorded in the audit log's "
                    "'deleted_reason' field; there is no separate deleted-connections store.",
                'parameters': [{'name': 'name', 'in': 'path', 'required': True, 'schema': {'type': 'string'}}],
                'requestBody': _body({'reason': {'type': 'string'}}, ['reason']),
                'responses': {'200': {'description': 'Deleted'}, **_ERRORS}}},
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
            '/api_keys': {
                'get': {'summary': 'List API keys (admin only; never the key itself)', 'tags': ['API keys'],
                        'responses': {'200': {'description': 'API keys'}, **_ERRORS}},
                'post': {'summary': 'Create a scoped API key (admin only)', 'tags': ['API keys'],
                         'requestBody': _body({
                             'name': {'type': 'string'},
                             'role': {'type': 'string', 'description':
                                 'Create this key from a named role (see POST /roles) instead of specifying '
                                 'grants directly - copies the role\'s connections/allow_writes/queries/collections/'
                                 'rate_limit/allowed_ips/allowed_write_ops onto this key once, at creation. '
                                 'Cannot be combined with any of those fields in the same request (400); '
                                 'create the key from the role, then PATCH it afterward to customize. '
                                 'expires_at may still be set alongside a role.'},
                             'connections': {'description': 'Allowed connection names, or omitted/"*" for all.',
                                            'oneOf': [{'type': 'array', 'items': {'type': 'string'}},
                                                     {'type': 'string', 'enum': ['*']}]},
                             'allow_writes': {'type': 'boolean', 'default': False, 'description':
                                 'Never wider than the server-wide QUERYAPIGATE_ALLOW_WRITES.'},
                             'queries': {'description': 'Saved-query names this key may run regardless of '
                                 '"connections" - additive, not a narrower version of it; omitted/[] grants '
                                 'none beyond what "connections" already allows, "*" grants every saved query '
                                 'by name (but never ad-hoc SQL, and never write access - see below). A list '
                                 'entry is either a plain name (read access) or {"name": ..., '
                                 '"allow_writes": true} (write access to that one query specifically, on top '
                                 'of whatever allow_writes already grants); "*" can never be combined with a '
                                 'write entry - wanting write access to a specific query means enumerating '
                                 'the whole list explicitly.',
                                        'oneOf': [{'type': 'array', 'items': _QUERY_ENTRY_SCHEMA},
                                                 {'type': 'string', 'enum': ['*']}]},
                             'collections': {'type': 'array', 'items': {'type': 'string'}, 'description':
                                 'Collection names this key may run every saved query in - read access only, '
                                 'never ad-hoc SQL, and no "*" wildcard. LIVE: it reaches whatever is in the '
                                 'collection now, including queries moved in later (see PUT '
                                 '/saved_sql/{name}/collection). Each newly granted name must be an existing '
                                 'collection (400 otherwise, listing them). Omitted means none.'},
                             'expires_at': {'type': 'string', 'format': 'date', 'description':
                                 'YYYY-MM-DD; the key stops authenticating after the end of this date. '
                                 'Omitted/null means no expiry.'},
                             'rate_limit': {'type': 'string', 'description':
                                 'This key\'s own limit, e.g. "100/minute" - same grammar as '
                                 'QUERYAPIGATE_RATE_LIMIT. Checked in addition to the server-wide limit, never '
                                 'instead of it. Omitted/null means no limit of its own.'},
                             'allowed_ips': {'type': 'array', 'items': {'type': 'string'}, 'description':
                                 'IP addresses or CIDR ranges this key may be used from, e.g. '
                                 '["203.0.113.5", "10.0.0.0/8"]. Omitted/null means no restriction.'},
                             'allowed_write_ops': {'type': 'array', 'items': {'type': 'string'}, 'description':
                                 'Write statement keywords this key may perform, e.g. ["insert", "update"] '
                                 '- never widens allow_writes, only narrows which write statements are '
                                 'permitted once it is on. Omitted/null means every write keyword is '
                                 'equally permitted (today\'s default behaviour).'}},
                             ['name']),
                         'responses': {'200': {'description':
                             'The plaintext key, shown once - only its hash is stored.'}, **_ERRORS}}},
            '/api_keys/{name}': {
                'parameters': [{'name': 'name', 'in': 'path', 'required': True, 'schema': {'type': 'string'}}],
                'patch': {'summary': "Update a key's permissions (admin only; the secret itself never changes)",
                         'tags': ['API keys'],
                         'requestBody': _body({
                             'connections': {'oneOf': [{'type': 'array', 'items': {'type': 'string'}},
                                                       {'type': 'string', 'enum': ['*']}]},
                             'allow_writes': {'type': 'boolean'}, 'active': {'type': 'boolean'},
                             'queries': {'description': 'Same shape as in POST /api_keys above. Omitted '
                                 'leaves it unchanged.',
                                        'oneOf': [{'type': 'array', 'items': _QUERY_ENTRY_SCHEMA},
                                                 {'type': 'string', 'enum': ['*']}]},
                             'collections': {'type': 'array', 'items': {'type': 'string'}, 'description':
                                 'Collection names this key may run every saved query in - read access only, '
                                 'never ad-hoc SQL, and no "*" wildcard. LIVE: it reaches whatever is in the '
                                 'collection now, including queries moved in later (see PUT '
                                 '/saved_sql/{name}/collection). Each newly granted name must be an existing '
                                 'collection (400 otherwise, listing them). Omitted leaves it unchanged; a list '
                                 'replaces it.'},
                             'expires_at': {'type': 'string', 'format': 'date', 'description':
                                 'YYYY-MM-DD, or explicit null to clear an existing expiry. Omitted leaves '
                                 'it unchanged.'},
                             'rate_limit': {'type': 'string', 'description':
                                 'e.g. "100/minute", or explicit null to clear it. Omitted leaves it '
                                 'unchanged.'},
                             'allowed_ips': {'type': 'array', 'items': {'type': 'string'}, 'description':
                                 'IP addresses or CIDR ranges, or explicit null to clear the restriction. '
                                 'Omitted leaves it unchanged.'},
                             'allowed_write_ops': {'type': 'array', 'items': {'type': 'string'}, 'description':
                                 'Write statement keywords, or explicit null to clear the restriction. '
                                 'Omitted leaves it unchanged.'}}, []),
                         'responses': {'200': {'description': 'Updated'}, **_ERRORS}},
                'delete': {'summary': 'Revoke an API key (admin only)', 'tags': ['API keys'],
                          'responses': {'200': {'description': 'Deleted'}, **_ERRORS}}},
            '/roles': {
                'get': {'summary': 'List permission roles (admin only)', 'tags': ['Roles'],
                        'responses': {'200': {'description': 'Roles'}, **_ERRORS}},
                'post': {'summary': 'Create a named permission role (admin only)', 'tags': ['Roles'],
                         'description': 'A role is a reusable *template*: create_key with a "role" copies '
                             'these fields onto the new key once, at creation time. Editing or deleting the '
                             'role afterward never changes a key already created from it - the key is the '
                             'sole source of truth for every authentication after that.',
                         'requestBody': _body({
                             'name': {'type': 'string'},
                             'connections': {'description': 'Same shape as in POST /api_keys.',
                                            'oneOf': [{'type': 'array', 'items': {'type': 'string'}},
                                                     {'type': 'string', 'enum': ['*']}]},
                             'allow_writes': {'type': 'boolean', 'default': False},
                             'queries': {'description': 'Same shape as in POST /api_keys.',
                                        'oneOf': [{'type': 'array', 'items': _QUERY_ENTRY_SCHEMA},
                                                 {'type': 'string', 'enum': ['*']}]},
                             'collections': {'type': 'array', 'items': {'type': 'string'}, 'description':
                                 'Same as in POST /api_keys. Copied onto a key at creation like every other '
                                 'field - the key\'s own grant is then live, the role is not.'},
                             'rate_limit': {'type': 'string', 'description':
                                 'e.g. "100/minute" - same grammar as QUERYAPIGATE_RATE_LIMIT. Omitted/null means '
                                 'no limit of its own.'},
                             'allowed_ips': {'type': 'array', 'items': {'type': 'string'}, 'description':
                                 'IP addresses or CIDR ranges. Omitted/null means no restriction.'},
                             'allowed_write_ops': {'type': 'array', 'items': {'type': 'string'}, 'description':
                                 'Write statement keywords, e.g. ["insert", "update"]. Omitted/null means '
                                 'every write keyword is equally permitted.'}},
                             ['name']),
                         'responses': {'200': {'description': 'Created'}, **_ERRORS}}},
            '/roles/{name}': {
                'parameters': [{'name': 'name', 'in': 'path', 'required': True, 'schema': {'type': 'string'}}],
                'patch': {'summary': 'Update a role (admin only; never affects a key already created from '
                             'it)', 'tags': ['Roles'],
                         'requestBody': _body({
                             'connections': {'oneOf': [{'type': 'array', 'items': {'type': 'string'}},
                                                       {'type': 'string', 'enum': ['*']}]},
                             'allow_writes': {'type': 'boolean'},
                             'queries': {'oneOf': [{'type': 'array', 'items': _QUERY_ENTRY_SCHEMA},
                                                 {'type': 'string', 'enum': ['*']}]},
                             'collections': {'type': 'array', 'items': {'type': 'string'}, 'description':
                                 'Collection names; a list replaces the current one. Omitted leaves it '
                                 'unchanged.'},
                             'rate_limit': {'type': 'string', 'description':
                                 'e.g. "100/minute", or explicit null to clear it. Omitted leaves it '
                                 'unchanged.'},
                             'allowed_ips': {'type': 'array', 'items': {'type': 'string'}, 'description':
                                 'Or explicit null to clear the restriction. Omitted leaves it unchanged.'},
                             'allowed_write_ops': {'type': 'array', 'items': {'type': 'string'}, 'description':
                                 'Or explicit null to clear the restriction. Omitted leaves it unchanged.'}},
                             []),
                         'responses': {'200': {'description': 'Updated'}, **_ERRORS}},
                'delete': {'summary': 'Delete a role (admin only; keys already created from it are '
                             'unaffected)', 'tags': ['Roles'],
                          'responses': {'200': {'description': 'Deleted'}, **_ERRORS}}},
            '/audit_log': {'get': {
                'summary': 'A durable record of administrative changes - API keys, roles, connections and '
                           'saved queries created, changed or removed (admin only)', 'tags': ['API keys'],
                'responses': {'200': {'description': 'Newest entry first, capped at 500 entries'}, **_ERRORS}}},
            '/history': {'get': {
                'summary': 'Every stored saved-query run, newest first and paged - for looking past the newest '
                           'runs per version that /list_files shows (admin only)', 'tags': ['Saved queries'],
                'parameters': [
                    {'name': 'query', 'in': 'query', 'schema': {'type': 'string'}, 'description': 'Saved query name'},
                    {'name': 'version', 'in': 'query', 'schema': {'type': 'integer'}},
                    {'name': 'status', 'in': 'query', 'schema': {'type': 'string', 'enum': ['success', 'error']}},
                    {'name': 'key', 'in': 'query', 'schema': {'type': 'string'},
                     'description': 'API key name (admin for the admin key)'},
                    {'name': 'since', 'in': 'query', 'schema': {'type': 'string'},
                     'description': 'YYYY-MM-DD or YYYY-MM-DD HH:MM:SS, inclusive'},
                    {'name': 'until', 'in': 'query', 'schema': {'type': 'string'},
                     'description': 'YYYY-MM-DD or YYYY-MM-DD HH:MM:SS, exclusive'},
                    {'name': 'limit', 'in': 'query',
                     'schema': {'type': 'integer', 'minimum': 1, 'maximum': 1000, 'default': 100}},
                    {'name': 'cursor', 'in': 'query', 'schema': {'type': 'string'},
                     'description': "The previous page's next"}],
                'responses': {'200': {'description': 'One page of runs, and the cursor for the next (null on the '
                                                     'last page)'}, **_ERRORS}}},
            '/settings': {'get': {
                'summary': "The server's own configuration: each setting's effective value and whether it comes "
                           'from the environment or is the default (admin only, read-only; secrets are reported '
                           'as configured or not, never returned)', 'tags': ['Service'],
                'responses': {'200': {'description': 'Settings grouped into sections'}, **_ERRORS}}},
            '/settings/mcp_status': {'get': {
                'summary': 'Whether the separate `queryapigate mcp` process is currently reachable on its '
                           'configured port (admin only; a plain TCP connect attempt, not a full MCP '
                           'handshake) - on demand only, never checked automatically', 'tags': ['Service'],
                'responses': {'200': {'description': 'Reachable or not, and the port checked'}, **_ERRORS}}},
            '/settings/mcp_tools': {'get': {
                'summary': 'What `tools/list` currently returns for an unrestricted MCP caller - computed '
                           "in-process, so this works whether or not `queryapigate mcp` is actually running "
                           '(admin only)', 'tags': ['Service'],
                'responses': {'200': {'description': 'Every MCP tool an admin key would see'}, **_ERRORS}}},
            '/cache/entries': {
                'get': {'summary': 'Every live response-cache entry\'s metadata - name, version, connection, '
                           'format, content type, size and TTL remaining (admin only; never the cached body '
                           'itself - see GET /cache/entries/{key} for that)', 'tags': ['Service'],
                       'responses': {'200': {'description': 'Live entries, soonest-expiring first'}, **_ERRORS}},
                'delete': {'summary': 'Evict every cache entry (admin only; not audited - a miss just '
                             're-populates from a real query)', 'tags': ['Service'],
                          'responses': {'200': {'description': 'Cleared'}, **_ERRORS}}},
            '/cache/entries/{key}': {
                'parameters': [{'name': 'key', 'in': 'path', 'required': True, 'schema': {'type': 'string'}}],
                'get': {'summary': 'One cache entry\'s cached response body, served with its real content '
                           'type - exactly as a caller would have received it on a hit (admin only)',
                       'tags': ['Service'],
                       'responses': {'200': {'description': 'The cached body'},
                                    '404': {'description': 'Missing, expired, or already evicted'}, **_ERRORS}},
                'delete': {'summary': 'Evict one cache entry early (admin only; not audited)',
                          'tags': ['Service'],
                          'responses': {'200': {'description': 'Deleted (also 200 if it was already gone)'},
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
                                'Whether this caller specifically can write through this query.'}}}},
                        'caller': {'type': 'object', 'properties': {
                            'name': {'type': 'string', 'nullable': True},
                            'admin': {'type': 'boolean'}, 'allow_writes': {'type': 'boolean'},
                            'allowed_write_ops': {'type': 'array', 'items': {'type': 'string'}, 'nullable': True},
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
                                        'version': {'type': 'string', 'description': 'Server version.'}}}}}}}}},
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
    for path, method in _DEPRECATED_OPERATIONS:
        spec['paths'][path][method]['deprecated'] = True
    if saved_queries:
        spec['paths'].update(_saved_query_paths(saved_queries))
        spec['tags'] = [{'name': 'Saved queries (live)',
                         'description': 'One endpoint per saved query, generated from its published version: its '
                                        'declared parameters and their rules, and its default connection.'}]
    return spec


# Kept in step with app.DEPRECATED_ENDPOINTS (the routes that send a Deprecation header) - tested.
_DEPRECATED_OPERATIONS = (('/list_files', 'get'), ('/view_file_content', 'get'), ('/save_sql_to_file', 'patch'),
                          ('/saved_sql/{name}', 'delete'), ('/saved_sql/{name}/collection', 'put'),
                          ('/saved_sql/{name}/cache_ttl', 'put'), ('/query_flow', 'get'))


DOCS_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>QueryAPIGate docs</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css">
<style>
  body { margin: 0; font-family: sans-serif; }
  #key-bar { padding: 8px 16px; background: #f4f4f4; border-bottom: 1px solid #ddd; font-size: 14px; }
  #key-bar input { width: 260px; padding: 4px; }
</style></head>
<body>
<div id="key-bar">
  <a href="ui">Admin UI</a> &middot;
  API key (only needed if the server sets QUERYAPIGATE_API_KEY; it reveals your saved queries below and is
  sent with "Try it out" requests; kept for this browser tab only):
  <input id="key" type="password" autocomplete="off" placeholder="X-API-Key">
  <button id="save-key">Apply</button>
</div>
<div id="ui"></div>
<script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
<script>
  var stored = '';
  try { stored = sessionStorage.getItem('queryapigate-key') || ''; } catch (e) {}
  document.getElementById('key').value = stored;
  document.getElementById('save-key').onclick = function () {
    try { sessionStorage.setItem('queryapigate-key', document.getElementById('key').value); } catch (e) {}
    location.reload();
  };
  SwaggerUIBundle({
    url: 'openapi.json', dom_id: '#ui',
    requestInterceptor: function (req) { if (stored) { req.headers['X-API-Key'] = stored; } return req; }
  });
</script></body></html>
"""


def dump():
    """The static API description (no saved queries, no environment-dependent parts, version pinned to "dev"),
    as stable, sorted JSON. frontend/openapi.json is this output, committed: the Console's TypeScript types are
    generated from it, so the frontend build needs no Python, and a test fails when the two drift apart.
    Regenerate it with:  python frontend/scripts/dump_openapi.py"""
    import json
    return json.dumps(build_spec('dev', jwt=False), indent=2, sort_keys=True) + '\n'

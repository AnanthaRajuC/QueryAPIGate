"""An MCP (Model Context Protocol) server exposing saved queries as tools (BACKLOG #42), plus two ad-hoc
tools - ``list_tables`` (schema discovery) and ``execute_sql`` (read-only ad-hoc SQL) - gated by a key's
existing ``connections`` grant, the same permission REST's own /connections/<name>/schema and /execute_sql
already check. Both ad-hoc tools are forced read-only regardless of the key's own ``allow_writes`` grant:
read-only saved queries only in this first version (see is_read_only() below); a write-capable slice - for
saved queries and for ad-hoc SQL alike - is real, separately-scoped follow-up work needing a
destructiveHint classification and a confirmation-flow decision this codebase doesn't have yet.

A separate process on its own port (``queryapigate mcp``, config.mcp_port()), not bridged into the Flask/
gunicorn REST server: MCP's Streamable HTTP transport is ASGI-native, this app is WSGI, and bridging the two
into one process would mean either an ASGI-capable worker for the whole app (risking the just-tuned
``--worker-class gthread`` setup from BACKLOG #43) or a WSGI/ASGI bridge with its own streaming edge cases.
What actually matters for correctness - reusing apikeys.py/store.py/app.run_saved() by direct Python import,
not an HTTP round-trip back to the REST API, so caching/history/auditing/scoping stay the single source of
truth - holds regardless of which process each server runs in.

Every ``mcp`` package import is deferred into run()/build_server(), so this module (and its pure helpers,
unit-tested without the optional ``queryapigate[mcp]`` extra installed) can be imported even when that
package isn't."""
import json
import urllib.parse

from . import apikeys, config, definitions, engine, params, schema, sqltools, store
from .errors import ApiError

# A saved query can never be named these - list_tools_for() skips a colliding saved query rather than
# hiding one of these two fixed tools, since a caller relies on list_tables/execute_sql always meaning the
# same thing.
RESERVED_TOOL_NAMES = frozenset({'list_tables', 'execute_sql'})

# Generic result-envelope outputSchemas, deliberately not per-query/per-column: a saved query's actual result
# columns are only known once it runs, not from its stored definition, so declaring real per-column types
# here would need a genuinely new admin-authored field (BACKLOG #42's own still-open note) - out of scope for
# this slice. What every row-returning tool (a saved query, and execute_sql) *can* honestly promise ahead of
# time is its envelope shape, which is exactly what a client needs to read structuredContent without parsing
# the text block. Every one of these tools' results, structured or not, carries the same two fields.
ROWS_OUTPUT_SCHEMA = {
    'type': 'object',
    'properties': {
        'rows': {'type': 'array', 'items': {'type': 'object'}},
        'truncated': {'type': 'boolean'},
    },
    'required': ['rows', 'truncated'],
}
TABLES_OUTPUT_SCHEMA = {
    'type': 'object',
    'properties': {
        'tables': {'type': 'array', 'items': {'type': 'object'}},
        'truncated': {'type': 'boolean'},
    },
    'required': ['tables', 'truncated'],
}


def _is_runnable(data):
    """Same test app._is_runnable() applies for GET /catalog - kept as a private copy here rather than an
    import from app.py, since app.py pulls in Flask app construction this module's pure helpers should stay
    free of for testability without the mcp extra."""
    return isinstance(data.get('sql_query'), str) or (data.get('query_type') == 'mongo'
                                                       and isinstance(data.get('mongo_collection'), str))


def is_read_only(data):
    """Whether a saved query's own SQL is read-only - True unconditionally for a mongo find() (always
    read-only; see app.run_saved_mongo()'s own docstring). The v1 tools/list filter: only a read-only query
    becomes an MCP tool at all."""
    if data.get('query_type') == 'mongo':
        return True
    return sqltools.first_keyword(data.get('sql_query') or '') in sqltools.READ_ONLY_STATEMENTS


def input_schema(data):
    """A saved query's query_parameters as an MCP tool's inputSchema - the same JSON Schema
    params.json_schema() already produces for /openapi.json's request-body properties, reused verbatim."""
    properties, required = {}, []
    for name, spec in definitions.effective_parameters(data).items():
        properties[name] = params.json_schema(spec)
        if spec['required']:
            required.append(name)
    return {'type': 'object', 'properties': properties, 'required': required}


def list_tools_for(permission):
    """Every read-only saved query ``permission`` may run, as plain dicts shaped like an MCP Tool - the same
    apikeys.can_run_saved() scoping GET /catalog already enforces (its own docstring: "the single rule...
    the run path, /openapi.json and /catalog all call this, so a new kind of grant cannot be honoured by one
    and missed by another" - this is that same call, so MCP tool listing is no exception either). Computed
    fresh on every call, never cached: two different API keys reach different queries."""
    tools = []
    for name, _number, data, collection in store.live_versions():
        if name in RESERVED_TOOL_NAMES or not _is_runnable(data) or not is_read_only(data):
            continue
        connection_name = data.get('connection_name')
        if not apikeys.can_run_saved(permission, name, collection, connection_name):
            continue
        tools.append({
            'name': name,
            'description': data.get('description') or name,
            'inputSchema': input_schema(data),
            'outputSchema': ROWS_OUTPUT_SCHEMA,
            'readOnlyHint': True,
            'idempotentHint': True,
        })
    return tools + fixed_tools_for(permission)


def fixed_tools_for(permission):
    """The two ad-hoc tools, not tied to any saved query - schema discovery and read-only SQL, each gated by
    the same ``connections`` grant REST's own /connections/<name>/schema and /execute_sql already check.
    Listed only when this caller has any connection-level access at all (admin, "*", or a non-empty
    ``connections`` list) - a query/collection-only key would never pass either tool's own permission check,
    so listing them anyway would just be a tool guaranteed to error, the same reasoning list_tools_for()
    already applies to a saved query the caller can't reach."""
    if not (permission.admin or permission.connections):
        return []
    max_rows = config.mcp_max_rows()
    return [
        {
            'name': 'list_tables',
            'description': "List the tables/views (or Mongo collections) and their columns on a connection "
                          "this key can use.",
            'inputSchema': {
                'type': 'object',
                'properties': {
                    'connection_name': {'type': 'string',
                                        'description': 'A connection name this key is permitted to use.'},
                    'database': {'type': 'string',
                                'description': 'Browse a different database on the same server - admin key only.'},
                },
                'required': ['connection_name'],
            },
            'outputSchema': TABLES_OUTPUT_SCHEMA,
            'readOnlyHint': True,
            'idempotentHint': True,
        },
        {
            'name': 'execute_sql',
            'description': "Run a single read-only SQL statement against a connection this key can use. "
                          "Always read-only over MCP, regardless of the key's own write permission.",
            'inputSchema': {
                'type': 'object',
                'properties': {
                    'connection_name': {'type': 'string',
                                        'description': 'A connection name this key is permitted to use.'},
                    'sql': {'type': 'string',
                           'description': 'A single SELECT/WITH/SHOW/DESCRIBE/EXPLAIN statement.'},
                    'params': {'type': 'object', 'description': 'Values for any :name bound parameters in the SQL.'},
                    'page_size': {'type': 'integer', 'description': f'Row cap, up to {max_rows} (the server default).'},
                    'database': {'type': 'string',
                                'description': 'Query a different database on the same server - admin key only.'},
                },
                'required': ['connection_name', 'sql'],
            },
            'outputSchema': ROWS_OUTPUT_SCHEMA,
            'readOnlyHint': True,
            'idempotentHint': True,
        },
    ]


def call_tool_for(flask_app, permission, name, arguments):
    """Runs a saved query through app.run_saved() - the exact function GET/POST /q/<name> already uses - via
    a synthetic Flask request context (test_request_context) rather than a parallel execution path. Every
    side effect run_saved() has (the connection-grant check, param resolution, cache_ttl lookup/store,
    execution-history recording, the live Home-tab broadcast from BACKLOG #43) fires exactly as it does over
    REST. ``format`` is forced to json (a tool result is text, not a CSV/XLSX blob); ``page_size`` is capped
    to config.mcp_max_rows() regardless of what the REST API's own QUERYAPIGATE_MAX_PAGE_SIZE default allows,
    since an LLM's context can't hold a large result the way a human paging through the admin UI can - a
    caller-supplied page_size smaller than the cap is still honoured, never a larger one.

    Returns a plain dict shaped like an MCP tools/call result ({"content": [...], "structuredContent": {...}},
    or {"isError": True, "content": [...]} for a failed run - structuredContent is only ever present on
    success, matching ROWS_OUTPUT_SCHEMA) - translation into the mcp package's own result types happens at
    the transport boundary in run(), not here, so this function stays importable/testable without that
    package."""
    from flask import g  # Flask itself is a core dependency (unlike the mcp package) - fine at call time

    arguments = dict(arguments or {})
    requested_page_size = arguments.pop('page_size', None)
    try:
        page_size = min(int(requested_page_size), config.mcp_max_rows()) if requested_page_size is not None \
            else config.mcp_max_rows()
    except (TypeError, ValueError):
        page_size = config.mcp_max_rows()
    query_string = urllib.parse.urlencode({**arguments, 'format': 'json', 'page_size': page_size})

    with flask_app.test_request_context(f'/q/{name}', method='GET', query_string=query_string):
        from . import app as app_module  # deferred: app.py imports Flask, a heavier import than this
        g.request_id = app_module.new_request_id(None)                       # module's pure helpers need
        g.permission = permission
        try:
            response = app_module.run_saved(name, {}, arguments)
        except ApiError as error:
            return {'isError': True, 'content': [{'type': 'text', 'text': error.message}]}
        structured = {'rows': response.get_json(), 'truncated': response.headers.get('X-Has-More') == 'true'}
        return {'content': [{'type': 'text', 'text': json.dumps(structured)}], 'structuredContent': structured}


def _connection_error(permission, connection_name, database):
    """The shared connection-grant/database-override checks list_tables and execute_sql both need, as an
    error result (or None if the call may proceed) - the same two rules /connections/<name>/schema and
    /execute_sql already enforce over REST (require_connection(), and "only the admin key may browse/query a
    different database on this connection"), reused here rather than re-derived."""
    if not connection_name:
        return {'isError': True, 'content': [{'type': 'text', 'text': 'connection_name is required'}]}
    if not apikeys.can_use(permission, connection_name):
        return {'isError': True, 'content': [{'type': 'text',
                'text': f"This API key is not permitted to use the connection '{connection_name}'"}]}
    if database and not permission.admin:
        return {'isError': True, 'content': [{'type': 'text',
                'text': 'Only the admin key may browse or query a different database on this connection'}]}
    return None


def list_tables_tool(permission, arguments):
    """schema.fetch_schema() is plain Python with no Flask ``g`` dependency (unlike call_tool_for()'s saved-
    query path, which needs a synthetic request context because run_saved() touches caching/audit/history),
    so this needs nothing beyond the permission check itself."""
    arguments = arguments or {}
    connection_name = arguments.get('connection_name')
    database = arguments.get('database') or None
    error = _connection_error(permission, connection_name, database)
    if error is not None:
        return error
    try:
        result = schema.fetch_schema(connection_name, database=database)
    except ApiError as error:
        return {'isError': True, 'content': [{'type': 'text', 'text': error.message}]}
    return {'content': [{'type': 'text', 'text': json.dumps(result)}], 'structuredContent': result}


def execute_sql_tool(permission, arguments):
    """engine.execute_sql() is likewise plain Python, no Flask ``g`` dependency - no synthetic request
    context needed here either. ``allow_writes=False`` is hard-coded, not taken from ``permission`` - ad-hoc
    SQL over MCP is read-only in this version regardless of what the key would otherwise be allowed to do
    over REST (see module docstring). ``allowed_tables`` is still honoured, so a table-restricted key can't
    use this tool to reach a table its REST access already refuses it."""
    arguments = dict(arguments or {})
    connection_name = arguments.pop('connection_name', None)
    sql = arguments.pop('sql', None)
    database = arguments.pop('database', None) or None
    error = _connection_error(permission, connection_name, database)
    if error is not None:
        return error
    if not sql:
        return {'isError': True, 'content': [{'type': 'text', 'text': 'sql is required'}]}
    query_params = arguments.pop('params', None) or {}
    requested_page_size = arguments.pop('page_size', None)
    try:
        page_size = min(int(requested_page_size), config.mcp_max_rows()) if requested_page_size is not None \
            else config.mcp_max_rows()
    except (TypeError, ValueError):
        page_size = config.mcp_max_rows()
    try:
        result = engine.execute_sql(sql, connection_name, page_size, 0, query_params, None, allow_writes=False,
                                    key_name=permission.name or '-', database=database,
                                    allowed_tables=permission.allowed_tables)
    except ApiError as error:
        return {'isError': True, 'content': [{'type': 'text', 'text': error.message}]}
    structured = {'rows': [dict(zip(result.columns, row)) for row in result.rows], 'truncated': result.has_more}
    return {'content': [{'type': 'text', 'text': json.dumps(structured)}], 'structuredContent': structured}


def dispatch_tool_call(flask_app, permission, name, arguments):
    """Routes to a fixed tool's own handler, or falls back to call_tool_for() for a saved-query tool -
    RESERVED_TOOL_NAMES guarantees these two names are never also a saved query's."""
    if name == 'list_tables':
        return list_tables_tool(permission, arguments)
    if name == 'execute_sql':
        return execute_sql_tool(permission, arguments)
    return call_tool_for(flask_app, permission, name, arguments)


def _permission_for(request):
    """The same X-API-Key -> Permission resolution app.resolve_permission() does for the REST API
    (apikeys.authenticate(), falling back to apikeys.OPEN when no server key is configured at all) - against
    a Starlette Request instead of a Flask one, since that's what the MCP transport hands a tool handler."""
    header = request.headers.get('x-api-key', '') if request is not None else ''
    client_ip = request.client.host if request is not None and request.client else None
    permission = apikeys.authenticate(header, client_ip=client_ip)
    if permission is not None:
        return permission
    return None if apikeys.auth_required() else apikeys.OPEN


def build_server(flask_app):
    """The mcp.server.lowlevel.Server, wired to this module's helpers - low-level, not the higher-level
    FastMCP decorator API, because tool listing must be computed fresh per request (scoped to that
    request's own caller) rather than registered once at server construction time; FastMCP's @mcp.tool()
    only supports the latter."""
    import mcp.types as types
    from mcp.server.lowlevel import Server

    server = Server('queryapigate')

    @server.list_tools()
    async def list_tools():
        ctx = server.request_context
        permission = _permission_for(ctx.request)
        if permission is None:
            return []  # an unauthenticated client sees no tools, the same way an unauthenticated REST
        return [types.Tool(name=t['name'], description=t['description'], inputSchema=t['inputSchema'],
                           outputSchema=t.get('outputSchema'),
                           annotations=types.ToolAnnotations(readOnlyHint=t['readOnlyHint'],
                                                             idempotentHint=t['idempotentHint']))
                for t in list_tools_for(permission)]                          # caller sees an empty /catalog

    @server.call_tool()
    async def call_tool(name, arguments):
        ctx = server.request_context
        permission = _permission_for(ctx.request)
        if permission is None:
            return types.CallToolResult(
                content=[types.TextContent(type='text', text='Unauthorized: missing or invalid X-API-Key')],
                isError=True)
        result = dispatch_tool_call(flask_app, permission, name, arguments)
        return types.CallToolResult(content=[types.TextContent(**item) for item in result['content']],
                                    structuredContent=result.get('structuredContent'),
                                    isError=result.get('isError', False))

    return server


def run(flask_app, host, port):
    """Serves the MCP server over Streamable HTTP (stateless - no server-side session tied to one API key
    across calls, matching this server's own per-request auth model) until interrupted."""
    import contextlib

    import uvicorn
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from starlette.applications import Starlette
    from starlette.routing import Mount

    server = build_server(flask_app)
    session_manager = StreamableHTTPSessionManager(app=server, stateless=True)

    @contextlib.asynccontextmanager
    async def lifespan(_starlette_app):
        async with session_manager.run():
            yield

    asgi_app = Starlette(routes=[Mount('/mcp', app=session_manager.handle_request)], lifespan=lifespan)
    uvicorn.run(asgi_app, host=host, port=port)

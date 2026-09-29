"""An MCP (Model Context Protocol) server exposing saved queries as tools (BACKLOG #42) - read-only saved
queries only in this first version (see is_read_only() below); a write-capable slice is real, separately-
scoped follow-up work needing a destructiveHint classification and a confirmation-flow decision this
codebase doesn't have yet.

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

from . import apikeys, config, definitions, params, sqltools, store
from .errors import ApiError


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
    for name, _number, data, collection in store.latest_versions():
        if not _is_runnable(data) or not is_read_only(data):
            continue
        connection_name = data.get('connection_name')
        if not apikeys.can_run_saved(permission, name, collection, connection_name):
            continue
        tools.append({
            'name': name,
            'description': data.get('description') or name,
            'inputSchema': input_schema(data),
            'readOnlyHint': True,
            'idempotentHint': True,
        })
    return tools


def call_tool_for(flask_app, permission, name, arguments):
    """Runs a saved query through app.run_saved() - the exact function GET/POST /q/<name> already uses - via
    a synthetic Flask request context (test_request_context) rather than a parallel execution path. Every
    side effect run_saved() has (the connection-grant check, param resolution, cache_ttl lookup/store,
    execution-history recording, the live Home-tab broadcast from BACKLOG #43) fires exactly as it does over
    REST. ``format`` is forced to json (a tool result is text, not a CSV/XLSX blob); ``page_size`` is capped
    to config.mcp_max_rows() regardless of what the REST API's own QUERYAPIGATE_MAX_PAGE_SIZE default allows,
    since an LLM's context can't hold a large result the way a human paging through the admin UI can - a
    caller-supplied page_size smaller than the cap is still honoured, never a larger one.

    Returns a plain dict shaped like an MCP tools/call result ({"content": [...]}, or {"isError": True,
    "content": [...]} for a failed run) - translation into the mcp package's own result types happens at the
    transport boundary in run(), not here, so this function stays importable/testable without that package."""
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
        rows = response.get_json()
        text = json.dumps(rows)
        if response.headers.get('X-Has-More') == 'true':
            text += f'\n\n(truncated to {page_size} rows)'
        return {'content': [{'type': 'text', 'text': text}]}


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
        result = call_tool_for(flask_app, permission, name, arguments)
        return types.CallToolResult(content=[types.TextContent(**item) for item in result['content']],
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

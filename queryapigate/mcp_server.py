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
import functools
import json
import logging
import re
import time
import urllib.parse

from . import (
    apikeys,
    config,
    definitions,
    descriptions,
    engine,
    history,
    metrics,
    params,
    schema,
    scope,
    sqltools,
    store,
)
from .errors import ApiError, code_for


def _in_callers_workspace(position):
    """Run the function in the workspace of the Permission it is given (argument `position`): the MCP server answers
    outside Flask's request hooks, so nothing else has settled it (scope.py). A key's tools, resources and calls are
    its own workspace's (ADR 0023)."""
    def wrap(function):
        @functools.wraps(function)
        def inner(*args, **kwargs):
            permission = args[position] if len(args) > position else kwargs.get('permission')
            with scope.using(getattr(permission, 'workspace', None)):
                return function(*args, **kwargs)
        return inner
    return wrap

# A saved query can never be named these - list_tools_for() skips a colliding saved query rather than
# hiding one of these two fixed tools, since a caller relies on list_tables/execute_sql always meaning the
# same thing.
RESERVED_TOOL_NAMES = frozenset({'list_tables', 'execute_sql', 'describe_table'})
log = logging.getLogger('queryapigate')

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
        'note': {'type': 'string',
                 'description': 'Present when the result was capped: how to get the rest, or a summary instead.'},
    },
    'required': ['rows', 'truncated'],
}
# A write tool's answer (ADR 0008): approval_required - with where a person approves it - or done, with what it changed.
WRITE_OUTPUT_SCHEMA = {
    'type': 'object',
    'properties': {
        'status': {'type': 'string', 'enum': ['approval_required', 'done']},
        'request': {'type': 'string', 'description': 'The write request - pass it back as `approval` once approved.'},
        'approve_at': {'type': 'string', 'description': 'Where a person approves it.'},
        'expires_at': {'type': 'string'},
        'message': {'type': 'string'},
        'rows_affected': {'type': 'integer'},
        'rows': {'type': 'array', 'items': {'type': 'object'}, 'description': "A RETURNING clause's rows."},
        'truncated': {'type': 'boolean'},
    },
    'required': ['status', 'request', 'rows', 'truncated'],
}
TABLES_OUTPUT_SCHEMA = {
    'type': 'object',
    'properties': {
        'tables': {'type': 'array', 'items': {'type': 'object'}},
        'truncated': {'type': 'boolean'},
    },
    'required': ['tables', 'truncated'],
}
TABLE_OUTPUT_SCHEMA = {
    'type': 'object',
    'properties': {
        'table': {'type': 'object', 'description': 'One table: its columns, types, keys and (masking-aware) flags.'},
    },
    'required': ['table'],
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
    return sqltools.is_read_only(data.get('sql_query') or '')


def input_schema(data):
    """A saved query's query_parameters as an MCP tool's inputSchema - the same JSON Schema
    params.json_schema() already produces for /openapi.json's request-body properties, reused verbatim."""
    properties, required = {}, []
    for name, spec in definitions.effective_parameters(data).items():
        properties[name] = params.json_schema(spec)
        if spec['required']:
            required.append(name)
    return {'type': 'object', 'properties': properties, 'required': required}


def output_schema(data):
    """ROWS_OUTPUT_SCHEMA, with each row's columns described when the saved query describes them
    (`column_descriptions`, BACKLOG #86) - what an agent reads to know what `amt` means."""
    described = data.get('column_descriptions') or {}
    if not described:
        return ROWS_OUTPUT_SCHEMA
    row = {'type': 'object', 'properties': {column: {'description': text} for column, text in described.items()}}
    return {**ROWS_OUTPUT_SCHEMA,
            'properties': {**ROWS_OUTPUT_SCHEMA['properties'], 'rows': {'type': 'array', 'items': row}}}


@_in_callers_workspace(0)
def list_tools_for(permission):
    """Every read-only saved query ``permission`` may run, as plain dicts shaped like an MCP Tool - the same
    apikeys.can_run_saved() scoping GET /catalog already enforces (its own docstring: "the single rule...
    the run path, /openapi.json and /catalog all call this, so a new kind of grant cannot be honoured by one
    and missed by another" - this is that same call, so MCP tool listing is no exception either). Computed
    fresh on every call, never cached: two different API keys reach different queries."""
    tools = []
    for name, _number, data, collection in store.live_versions():
        if name in RESERVED_TOOL_NAMES or not _is_runnable(data):
            continue
        if data.get('mcp') is False:  # exposure turned off for this query (BACKLOG #54); default is on
            continue
        connection_name = data.get('connection_name')
        if not apikeys.can_run_saved(permission, name, collection, connection_name, data.get('query_parameters')):
            continue
        if is_read_only(data):
            tools.append({
                'name': name,
                'description': data.get('description') or name,
                'inputSchema': input_schema(data),
                'outputSchema': output_schema(data),
                'readOnlyHint': True,
                'idempotentHint': True,
            })
        elif agent_may_write(permission, name, data):
            tools.append(write_tool(name, data))
    return tools + fixed_tools_for(permission)


def agent_may_write(permission, name, data):
    """Whether this caller may write through this saved query over MCP (ADR 0008): its author marked it
    (`agent_writes`), the server allows writes, the caller's grant allows this one - as over REST - and its
    connection can run it in a transaction and count the rows it changed."""
    if not data.get('agent_writes') or not config.allow_writes():
        return False
    if not (permission.allow_writes or apikeys.can_write_query(permission, name)):
        return False
    try:
        dialect = store.get_connection(data.get('connection_name'))['db']
    except ApiError:
        return False
    return dialect in definitions.AGENT_WRITE_DIALECTS


APPROVAL_ARGUMENT = 'approval'
IDEMPOTENCY_ARGUMENT = 'idempotency_key'


def write_tool(name, data):
    """A write tool: its parameters, plus `approval` (the request a person approved) or, for an insert-only one,
    `idempotency_key`; annotated as writing, destructive unless it only inserts."""
    insert_only = data.get('agent_writes') == 'insert'
    schema = input_schema(data)
    if insert_only:
        schema['properties'][IDEMPOTENCY_ARGUMENT] = {
            'type': 'string', 'description': 'Optional: the same key on a retry returns the first call\'s result '
                                             'instead of inserting again (kept a day).'}
        how = 'It only adds rows, and runs without an approval.'
    else:
        schema['properties'][APPROVAL_ARGUMENT] = {
            'type': 'string', 'description': 'The write request a person approved. Leave it out the first time: '
                                             'the answer says where they approve it.'}
        how = ('A person approves each call before it runs: the first call answers with where they approve it; call '
               'again with the same arguments and approval set to the request it named.')
    ceiling = data.get('max_affected_rows') or 1
    return {
        'name': name,
        'description': f"{data.get('description') or name}\n\nThis tool writes. {how} It changes at most "
                       f"{ceiling} row{'' if ceiling == 1 else 's'}.",
        'inputSchema': schema,
        'outputSchema': WRITE_OUTPUT_SCHEMA,
        'readOnlyHint': False,
        'destructiveHint': not insert_only,
        'idempotentHint': False,
    }


@_in_callers_workspace(0)
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
        {
            'name': 'describe_table',
            'description': "Describe one table on a connection this key can use: its columns, types, primary and "
                          "foreign keys, and any descriptions - masking-aware, so a masked or dropped column is "
                          "flagged, not shown as readable. Use it before writing SQL with execute_sql.",
            'inputSchema': {
                'type': 'object',
                'properties': {
                    'connection_name': {'type': 'string',
                                        'description': 'A connection name this key is permitted to use.'},
                    'table': {'type': 'string', 'description': 'The table (or view) name to describe.'},
                    'database': {'type': 'string',
                                'description': 'A different database on the same server - admin key only.'},
                },
                'required': ['connection_name', 'table'],
            },
            'outputSchema': TABLE_OUTPUT_SCHEMA,
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
    # A write tool's own arguments (ADR 0008) - what a person approved, or an insert's idempotency key - are not the
    # query's parameters, and never reach its SQL.
    write_args = {key: arguments.pop(key) for key in (APPROVAL_ARGUMENT, IDEMPOTENCY_ARGUMENT) if key in arguments}
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
        g.transport = 'mcp'  # recorded on the run's history entry
        g.agent_write_args = write_args
        try:
            response = app_module.run_saved(name, {}, arguments)
        except ApiError as error:
            return _error(error.message, error.status, error.code)
        if g.get('agent_write_result') is not None:  # a write tool's answer - see app.run_agent_write()
            structured = g.agent_write_result
            return {'content': [{'type': 'text', 'text': json.dumps(structured, default=str)}],
                    'structuredContent': structured}
        structured = {'rows': response.get_json(), 'truncated': response.headers.get('X-Has-More') == 'true'}
        return _rows_result(structured)


def _rows_result(structured):
    """A rows/truncated tool result. When the result was capped (``truncated``), add an agent-facing ``note`` -
    both as a field in structuredContent and as a second text block - so a model adds a filter or asks for a
    summary instead of assuming the rows it got are the whole answer. The first content block stays pure JSON,
    so a client parsing it (or structuredContent) is unaffected."""
    content = [{'type': 'text', 'text': json.dumps(structured)}]
    if structured.get('truncated'):
        shown = len(structured.get('rows') or [])
        note = (f'Showing the first {shown} rows; more rows match. Add a WHERE filter to narrow the result, '
                f'or ask for a summary with an aggregate (COUNT, SUM, GROUP BY) to get the answer itself '
                f'instead of raw rows.')
        structured = {**structured, 'note': note}
        content = [{'type': 'text', 'text': json.dumps(structured)}, {'type': 'text', 'text': note}]
    return {'content': content, 'structuredContent': structured}


def _error(message, status, code=None, **extra):
    """A tool error result: the message as text, as always, and in structuredContent the same `error` and stable
    `code` a REST error body carries (errors.py, BACKLOG #69), so an agent can branch on the code. `_status` (the HTTP
    status REST would answer) is for handle_call's metrics and never leaves this module."""
    return {'isError': True, 'content': [{'type': 'text', 'text': message}],
            'structuredContent': {'error': message, 'code': code_for(status, code), **extra}, '_status': status}


def _connection_error(permission, connection_name, database):
    """The shared connection-grant/database-override checks list_tables and execute_sql both need, as an
    error result (or None if the call may proceed) - the same two rules /connections/<name>/schema and
    /execute_sql already enforce over REST (require_connection(), and "only the admin key may browse/query a
    different database on this connection"), reused here rather than re-derived."""
    if not connection_name:
        return _error('connection_name is required', 400, 'connection_required')
    if not apikeys.can_use(permission, connection_name):
        return _error(f"This API key is not permitted to use the connection '{connection_name}'", 403,
                      'connection_forbidden')
    if database and not permission.admin:
        return _error('Only the admin key may browse or query a different database on this connection', 403,
                      'admin_only')
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
        result = schema.fetch_schema(connection_name, database=database, allowed_tables=permission.allowed_tables)
    except ApiError as error:
        return _error(error.message, error.status, error.code)
    if not database:  # descriptions are the connection's own database's
        result = descriptions.annotate(result, connection_name, mask=permission.mask,
                                        allowed_tables=permission.allowed_tables)
    return {'content': [{'type': 'text', 'text': json.dumps(result)}], 'structuredContent': result}


def describe_table_tool(permission, arguments):
    """One table's columns/types/keys on a connection this key can use, masking-aware (reusing the same
    descriptions.annotate() list_tables uses, so a masked or dropped column is flagged). A table the key can't
    reach answers exactly as a missing one (table_not_found), so a table-restricted key can't probe for it -
    the same rule fetch_table_ddl() enforces."""
    arguments = arguments or {}
    connection_name = arguments.get('connection_name')
    table_name = arguments.get('table')
    database = arguments.get('database') or None
    error = _connection_error(permission, connection_name, database)
    if error is not None:
        return error
    if not table_name:
        return _error('table is required', 400, 'table_required')
    try:
        result = schema.fetch_schema(connection_name, database=database, allowed_tables=permission.allowed_tables)
    except ApiError as error:
        return _error(error.message, error.status, error.code)
    if not database:
        result = descriptions.annotate(result, connection_name, mask=permission.mask,
                                        allowed_tables=permission.allowed_tables)
    table = next((t for t in result['tables'] if t['name'] == table_name), None)
    if table is None:
        return _error(f"'{table_name}' is not a table on '{connection_name}'", 404, 'table_not_found')
    structured = {'table': table}
    return {'content': [{'type': 'text', 'text': json.dumps(structured)}], 'structuredContent': structured}


DICTIONARY_URI = 'queryapigate://connections/{}/dictionary'


@_in_callers_workspace(0)
def list_resources_for(permission):
    """One data dictionary per active connection the caller may use: its notes, tables, columns and what they mean,
    as one Markdown document (BACKLOG #86). Callers with saved queries only have none."""
    if not (permission.admin or permission.connections):
        return []
    found = []
    for name, details in sorted(store.read_connections().items()):
        if not details.get('active', True) or not apikeys.can_use(permission, name):
            continue
        found.append({'uri': DICTIONARY_URI.format(urllib.parse.quote(name, safe='')),
                      'name': f'{name} data dictionary',
                      'description': f"What the tables and columns on '{name}' mean - read before writing SQL.",
                      'mimeType': 'text/markdown'})
    return found


@_in_callers_workspace(0)
def read_resource_for(permission, uri):
    """A data dictionary's text, for a caller who may use that connection - its reachable tables only, with what it
    gets masked. Raises ApiError otherwise, never saying whether the connection exists."""
    prefix, _, rest = str(uri).partition('queryapigate://connections/')
    name = urllib.parse.unquote(rest[:-len('/dictionary')]) if not prefix and rest.endswith('/dictionary') else None
    if not name or not apikeys.can_use(permission, name):
        raise ApiError(f'No such resource: {uri}', 404, code='not_found')
    result = schema.fetch_schema(name, allowed_tables=permission.allowed_tables)
    return descriptions.dictionary(name, descriptions.annotate(result, name, mask=permission.mask,
                                                                      allowed_tables=permission.allowed_tables))


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
        return _error('sql is required', 400, 'sql_required')
    query_params = arguments.pop('params', None) or {}
    requested_page_size = arguments.pop('page_size', None)
    try:
        page_size = min(int(requested_page_size), config.mcp_max_rows()) if requested_page_size is not None \
            else config.mcp_max_rows()
    except (TypeError, ValueError):
        page_size = config.mcp_max_rows()

    entry = {'executed_at': store.now(), 'connection_name': connection_name, 'request_id': None,
             'key_name': permission.name or '-', 'transport': 'mcp'}
    if database:
        entry['database'] = database
    try:
        result, elapsed_ms = engine.timed(engine.execute_sql, sql, connection_name, page_size, 0, query_params, None,
                                          allow_writes=False, key_name=permission.name or '-', database=database,
                                          allowed_tables=permission.allowed_tables, mask=permission.mask)
    except ApiError as error:
        history.record_adhoc({**entry, 'status': 'error', 'error': error.message, 'code': error.code}, sql,
                             query_params)
        return _error(error.message, error.status, error.code)
    history.record_adhoc({**entry, 'status': 'success', 'rows': len(result.rows), 'duration_ms': elapsed_ms,
                          **({'masked': result.masked} if result.masked else {})}, sql, query_params)
    rows = [dict(zip(result.columns, row, strict=False)) for row in result.rows]
    structured = {'rows': rows, 'truncated': result.has_more}
    return _rows_result(structured)


def dispatch_tool_call(flask_app, permission, name, arguments):
    """Routes to a fixed tool's own handler, or falls back to call_tool_for() for a saved-query tool -
    RESERVED_TOOL_NAMES guarantees these two names are never also a saved query's."""
    if name == 'list_tables':
        return list_tables_tool(permission, arguments)
    if name == 'describe_table':
        return describe_table_tool(permission, arguments)
    if name == 'execute_sql':
        return execute_sql_tool(permission, arguments)
    return call_tool_for(flask_app, permission, name, arguments)


def _tool_kind(name):
    return 'mcp.' + (name if name in RESERVED_TOOL_NAMES else 'saved_query')


def handle_call(flask_app, api_key, authorization, client_ip, name, arguments):
    """One tools/call, from the caller's credentials to its result, as a plain function so it can be tested without
    the SDK. It applies exactly what REST's request hooks apply (governance.py): the client's rate limit,
    authentication by API key or bearer token, then call_as(). Over HTTP the first two happen once per request, in
    build_asgi_app()'s gate, which hands the SDK handler the caller it let in."""
    from . import governance, instances
    instances.heartbeat()
    started = time.monotonic()
    verdict = governance.check_client_limit(flask_app, client_ip)
    if verdict is not None and not verdict.allowed:
        return _finish(name, None, _error(f'Rate limit exceeded - retry after {verdict.retry_after}s', 429,
                                          retry_after=verdict.retry_after), started)
    permission, refusal = governance.authenticate_mcp(api_key, authorization, client_ip)
    if permission is None:
        return _finish(name, None, _error(refusal.message, refusal.status, refusal.code), started)
    return call_as(flask_app, permission, name, arguments, started)


_TOOL_NAME_RE = re.compile(r'[\w .\-]{1,100}')


@_in_callers_workspace(1)
def call_as(flask_app, permission, name, arguments, started=None, headers=None):
    """One tools/call by an authenticated caller: its own rate limit, the tool, and a request in /metrics (method
    MCP, endpoint mcp.<tool kind>) - in a span (tracing.py) continuing the trace a `traceparent` in `headers` names."""
    from . import governance, instances, quotas, tracing
    if started is None:
        instances.heartbeat()
        started = time.monotonic()
    # The tool name is whatever the client sent - any string, for a tool that may not exist - so a span records it
    # only in the shape a saved query's name can have.
    shown = name if isinstance(name, str) and _TOOL_NAME_RE.fullmatch(name) else '(invalid)'
    with tracing.span(f'tools/call {shown}', 'server', {
            'mcp.method.name': 'tools/call', 'gen_ai.tool.name': shown, 'gen_ai.operation.name': 'execute_tool',
            'queryapigate.caller': permission.name or '-'}, parent=tracing.extract(headers)) as span:
        verdict = governance.check_key_limit(flask_app, permission)
        if verdict is not None and not verdict.allowed:
            result = _error(f'Rate limit exceeded for this API key - retry after {verdict.retry_after}s', 429,
                            retry_after=verdict.retry_after)
        else:
            try:
                quotas.check(permission)  # usage quotas (ADR 0011): an agent's calls count like any other
            except ApiError as error:
                result = _error(error.message, error.status, error.code, **error.extra)
            else:
                result = dispatch_tool_call(flask_app, permission, name, arguments)
                if not result.get('isError') and name != 'list_tables':  # table names aren't data delivered
                    quotas.count(permission, len((result.get('structuredContent') or {}).get('rows') or []))
        if span is not None and result.get('isError'):
            code = (result.get('structuredContent') or {}).get('code')
            span.set_attribute('error.type', code if isinstance(code, str) else 'tool_error')
            if result.get('_status', 200) >= 500:
                tracing.mark_error(span, code if isinstance(code, str) else 'tool_error')
    return _finish(name, permission, result, started)


def _finish(name, permission, result, started):
    from . import governance
    status = result.pop('_status', 200)
    elapsed = time.monotonic() - started
    governance.observe('MCP', _tool_kind(name), status, elapsed, metrics.label_for(permission))
    log.info('MCP tools/call %s -> %s in %.1fms', name, status, elapsed * 1000,
             extra={'method': 'MCP', 'path': f'tools/call {name}', 'status': status,
                    'duration_ms': round(elapsed * 1000, 1)})
    return result


def permission_for_listing(flask_app, api_key, authorization, client_ip):
    """Who is asking for tools/list - authenticated like a call (API key or bearer token) and counted against the
    client's rate limit; None when the caller may see no tools."""
    from . import governance
    verdict = governance.check_client_limit(flask_app, client_ip)
    if verdict is not None and not verdict.allowed:
        return None
    return governance.authenticate_mcp(api_key, authorization, client_ip)[0]


PERMISSION_STATE = 'queryapigate_permission'  # where the gate leaves the caller, in the ASGI scope's state


def resource_metadata_url():
    """Where this server's Protected Resource Metadata is, per RFC 9728: /.well-known/oauth-protected-resource
    inserted before the resource URL's path."""
    parts = urllib.parse.urlsplit(config.mcp_resource_url())
    return f'{parts.scheme}://{parts.netloc}/.well-known/oauth-protected-resource{parts.path.rstrip("/")}'


def resource_metadata():
    """RFC 9728's metadata for the MCP server: what it is, who issues its tokens, and the scope they need."""
    return {'resource': config.mcp_resource_url(), 'authorization_servers': [config.mcp_auth_server()],
            'scopes_supported': [config.mcp_scope()], 'bearer_methods_supported': ['header'],
            'resource_name': 'QueryAPIGate'}


def www_authenticate(refusal, presented=True):
    """The WWW-Authenticate challenge for a refused request (`presented`: whether it carried a credential at all).
    With OAuth on it points the client at the metadata and names the scope (MCP authorization, 2025-11-25), so an AI
    client can find where to sign the user in."""
    if not config.mcp_oauth_enabled():
        return 'Bearer realm="queryapigate"'
    parts = [f'resource_metadata="{resource_metadata_url()}"', f'scope="{config.mcp_scope()}"']
    if refusal.code == 'insufficient_scope':
        parts.insert(0, 'error="insufficient_scope"')
    elif refusal.status == 401 and presented:
        parts.insert(0, 'error="invalid_token"')
    return 'Bearer ' + ', '.join(parts)


def gate(flask_app, inner):
    """The ASGI app in front of /mcp: the client's rate limit and authentication once per HTTP request, answering
    429, 401 or 403 at the HTTP level - which is where an MCP client looks for them, and an OAuth client's cue to
    sign the user in - and otherwise passing the request on with the caller in the scope's state."""
    from starlette.datastructures import Headers
    from starlette.responses import JSONResponse

    from . import governance, instances

    async def app(scope, receive, send):
        if scope['type'] != 'http':
            return await inner(scope, receive, send)
        instances.heartbeat()
        started = time.monotonic()
        headers = Headers(scope=scope)
        client_ip = scope['client'][0] if scope.get('client') else None
        from .app import new_request_id
        request_id = new_request_id(headers.get('x-request-id'))
        verdict = governance.check_client_limit(flask_app, client_ip)
        if verdict is not None and not verdict.allowed:
            response = JSONResponse({'error': f'Rate limit exceeded - retry after {verdict.retry_after}s',
                                     'code': 'rate_limited', 'request_id': request_id}, status_code=429,
                                    headers={'Retry-After': str(verdict.retry_after), 'X-Request-Id': request_id})
        else:
            api_key, authorization = headers.get('x-api-key', ''), headers.get('authorization', '')
            permission, refusal = governance.authenticate_mcp(api_key, authorization, client_ip)
            if permission is not None:
                scope.setdefault('state', {})[PERMISSION_STATE] = permission
                return await inner(scope, receive, send)
            challenge = www_authenticate(refusal, presented=bool(api_key or authorization))
            response = JSONResponse({'error': refusal.message, 'code': refusal.code, 'request_id': request_id},
                                    status_code=refusal.status, headers={'WWW-Authenticate': challenge,
                                                                         'X-Request-Id': request_id})
        governance.observe('MCP', 'mcp.http', response.status_code, time.monotonic() - started, '-')
        await response(scope, receive, send)

    return app


def build_server(flask_app):
    """The mcp.server.lowlevel.Server, wired to this module's helpers - low-level, not the higher-level
    FastMCP decorator API, because tool listing must be computed fresh per request (scoped to that
    request's own caller) rather than registered once at server construction time; FastMCP's @mcp.tool()
    only supports the latter."""
    import mcp.types as types
    from mcp.server.lowlevel import Server

    server = Server('queryapigate')

    def caller():
        """The caller build_asgi_app()'s gate let in - every request reaching a handler has one."""
        request = server.request_context.request
        return getattr(request.state, PERMISSION_STATE, None) if request is not None else None

    @server.list_tools()
    async def list_tools():
        permission = caller()
        if permission is None:
            return []  # an unauthenticated client sees no tools, the same way an unauthenticated REST
        return [types.Tool(name=t['name'], description=t['description'], inputSchema=t['inputSchema'],
                           outputSchema=t.get('outputSchema'),
                           annotations=types.ToolAnnotations(readOnlyHint=t['readOnlyHint'],
                                                             destructiveHint=t.get('destructiveHint'),
                                                             idempotentHint=t['idempotentHint']))
                for t in list_tools_for(permission)]                          # caller sees an empty /catalog

    @server.call_tool()
    async def call_tool(name, arguments):
        permission = caller()
        if permission is None:  # not through the gate: never served
            result = _error('Unauthorized: missing or invalid X-API-Key or bearer token', 401)
            result.pop('_status')
        else:
            request = server.request_context.request
            result = call_as(flask_app, permission, name, arguments,
                             headers=dict(request.headers) if request is not None else None)
        return types.CallToolResult(content=[types.TextContent(**item) for item in result['content']],
                                    structuredContent=result.get('structuredContent'),
                                    isError=result.get('isError', False))

    @server.list_resources()
    async def list_resources():
        permission = caller()
        if permission is None:
            return []
        return [types.Resource(uri=r['uri'], name=r['name'], description=r['description'], mimeType=r['mimeType'])
                for r in list_resources_for(permission)]

    @server.read_resource()
    async def read_resource(uri):
        from mcp.server.lowlevel.helper_types import ReadResourceContents
        permission = caller()
        if permission is None:
            raise ValueError('Unauthorized')
        try:
            text = read_resource_for(permission, uri)
        except ApiError as error:
            raise ValueError(error.message) from None
        return [ReadResourceContents(content=text, mime_type='text/markdown')]

    return server


def build_asgi_app(flask_app):
    """The MCP server over Streamable HTTP at /mcp (stateless - no server-side session tied to one API key across
    calls, matching this server's own per-request auth model), plus this process's own /metrics and /health. Like
    REST's, those two need no key: MCP calls are counted here, in this process, so REST's /metrics can't show them
    (BACKLOG #61)."""
    import contextlib

    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse, PlainTextResponse
    from starlette.routing import Mount, Route

    from . import __version__

    server = build_server(flask_app)
    session_manager = StreamableHTTPSessionManager(app=server, stateless=True)

    @contextlib.asynccontextmanager
    async def lifespan(_starlette_app):
        async with session_manager.run():
            yield

    async def metrics_endpoint(request):
        if not metrics.authorized(request.headers.get('authorization')):
            return JSONResponse({'error': metrics.UNAUTHORIZED, 'code': 'unauthorized'}, status_code=401,
                                headers={'WWW-Authenticate': 'Bearer realm="metrics"'})
        return PlainTextResponse(metrics.render(flask_app.extensions.get('queryapigate_cache')),
                                 media_type='text/plain; version=0.0.4; charset=utf-8')

    async def health(_request):
        return JSONResponse({'status': 'ok', 'version': __version__})

    routes = [Mount('/mcp', app=gate(flask_app, session_manager.handle_request)),
              Route('/metrics', metrics_endpoint), Route('/health', health)]
    if config.mcp_oauth_enabled():
        async def protected_resource(_request):
            # Public by design, and read by browser-based clients too.
            return JSONResponse(resource_metadata(), headers={'Access-Control-Allow-Origin': '*',
                                                              'Cache-Control': 'max-age=3600'})
        paths = {'/.well-known/oauth-protected-resource',
                 urllib.parse.urlsplit(resource_metadata_url()).path}
        routes += [Route(path, protected_resource, methods=['GET']) for path in sorted(paths)]
    from . import secheaders
    return secheaders.asgi(Starlette(routes=routes, lifespan=lifespan))


def run(flask_app, host, port):
    """Serves build_asgi_app() until interrupted."""
    import uvicorn

    uvicorn.run(build_asgi_app(flask_app), host=host, port=port, server_header=False)  # no version banner

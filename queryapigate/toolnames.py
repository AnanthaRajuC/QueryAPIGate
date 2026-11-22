"""The names a saved query can't take (rc1 review, B4 and B17), fixed before 1.0 so that adding a control argument or a
built-in MCP tool later never takes over a name someone already uses.

- **Parameters.** A saved query's parameters arrive as the query string (REST) or a tool's arguments (MCP), next to the
  arguments that control the call - `page`, `timeout`, `stream`... A parameter named like one could never be sent over
  REST, and over MCP it was taken as the control. Those names are refused, and so is every name starting with `_`,
  kept for controls added later.
- **MCP tools.** A saved query is listed over MCP as a tool named for it. Common LLM APIs accept only
  `^[a-zA-Z0-9_-]{1,64}$`, and a query name may hold spaces, dots and letters beyond ASCII, so the tool name is the
  query name with every other character as `_`, cut to 64 (tool_name()). A new query whose tool name is a built-in
  tool's, one kept for later, or another query's is refused when it's created.

Definitions saved before these rules still load and run. A stored query with a reserved tool name, or one whose tool
name another has, isn't listed over MCP (mcp_server.list_tools_for() keeps the first, by name).
"""
import re

from .errors import ApiError

# The MCP tools every caller with a connection grant sees (mcp_server.fixed_tools_for()).
BUILTIN_TOOLS = frozenset({'list_tables', 'execute_sql', 'describe_table'})
# Kept for built-in tools to come: a saved query can't be listed under one of these either.
RESERVED_TOOLS = BUILTIN_TOOLS | frozenset({
    'list_queries', 'describe_query', 'search_queries', 'run_query', 'list_connections', 'list_databases',
    'list_collections', 'explain_sql', 'sample_table', 'list_resources', 'read_resource'})
TOOL_NAME_MAX = 64

# What controls a call rather than supplying a parameter: REST's query string (app.RESERVED_ARGS, and the data plane's
# RESERVED) and a write tool's own arguments over MCP (ADR 0008).
CONTROL_ARGUMENTS = frozenset({'format', 'page', 'page_size', 'connection_name', 'version', 'timeout', 'stream',
                               'approval', 'idempotency_key'})

_NOT_TOOL_CHARACTER = re.compile(r'[^A-Za-z0-9_-]')


def tool_name(query_name):
    """The MCP tool a saved query is listed as: its name with each character outside [A-Za-z0-9_-] as `_`, at most 64.
    `monthly revenue` is `monthly_revenue`, `sales.by-region` is `sales_by-region`."""
    return _NOT_TOOL_CHARACTER.sub('_', query_name)[:TOOL_NAME_MAX]


def reserved_parameter(name):
    return name in CONTROL_ARGUMENTS or name.startswith('_')


def check_parameters(names):
    """Refuse a parameter named like a control argument, or starting with `_` (invalid_body, 400)."""
    taken = sorted(name for name in set(names) if reserved_parameter(name))
    if taken:
        raise ApiError(f"{', '.join(taken)} can't be a parameter's name: {'it is' if len(taken) == 1 else 'they are'} "
                       f"kept for the arguments that control a call ({', '.join(sorted(CONTROL_ARGUMENTS))}, and every "
                       "name starting with _) - rename it in the query", code='invalid_body')


def check_new_query(name, others):
    """Refuse a new saved query whose MCP tool name is a built-in or reserved tool's, or that of one of ``others`` (the
    workspace's other saved queries) - invalid_name, 400."""
    tool = tool_name(name)
    if tool in RESERVED_TOOLS:
        raise ApiError(f"'{name}' is kept for a built-in MCP tool ({tool}) - choose another name", code='invalid_name')
    for other in others:
        if other != name and tool_name(other) == tool:
            raise ApiError(f"'{name}' would be listed over MCP as {tool}, as the saved query '{other}' is - choose a "
                           'name that differs in its letters, digits, _ or -', code='invalid_name')

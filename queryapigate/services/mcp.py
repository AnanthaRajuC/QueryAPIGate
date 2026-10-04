"""The MCP server as the Management API presents it (/api/v1/mcp/status, /api/v1/mcp/tools - BACKLOG #72).

`queryapigate mcp` is a separate process, so this module never talks MCP to it: status() is a plain TCP connect to
its configured port, run only when asked (never alongside a settings load), and tools() computes what `tools/list`
returns for an unrestricted caller in-process, with the same function the MCP server itself calls - so it works
whether or not that process is running.
"""
import socket

from .. import apikeys, config, mcp_server


def status():
    port = config.mcp_port()
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=1.5):
            reachable = True
    except OSError:
        reachable = False
    return {'reachable': reachable, 'port': port}


def tools():
    return [{
        'name': t['name'],
        'description': t['description'],
        'kind': 'ad-hoc' if t['name'] in mcp_server.RESERVED_TOOL_NAMES else 'saved query',
        'params': sorted(t['inputSchema'].get('properties', {}).keys()),
        'read_only': t['readOnlyHint'],
    } for t in mcp_server.list_tools_for(apikeys.OPEN)]

"""The lakehouse example (deploy/lakehouse, how-to 66), checked as the how-to says it behaves - against the running
Compose project: Iceberg rows through Trino, the cache, masking through a federated join, qualified allowed_tables,
read-only, a partner's narrow key, and the saved queries as MCP tools.

    docker compose -f deploy/lakehouse/docker-compose.yml up --build -d --wait
    python .github/scripts/lakehouse_smoke.py        # needs the `mcp` package for the last check
"""
import asyncio
import json
import sys
import urllib.error
import urllib.request

BASE = 'http://127.0.0.1:5000'
ADMIN, AGENT, PARTNER = 'lakehouse-admin', 'lakehouse-agent-key', 'lakehouse-partner-key'
failures = []


def call(path, key, body=None):
    """(status, headers, parsed JSON) of one call."""
    request = urllib.request.Request(BASE + path, headers={'X-API-Key': key, 'Content-Type': 'application/json'},
                                     data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, response.headers, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, error.headers, json.load(error)


def check(what, condition, detail=''):
    print(('ok    ' if condition else 'FAIL  ') + what + (f' - {detail}' if detail and not condition else ''))
    if not condition:
        failures.append(what)


status, headers, rows = call('/q/revenue_by_month?year=1995&page_size=12', ADMIN)
check('an Iceberg table through Trino: a month per row', status == 200 and len(rows) == 12, (status, rows))
months = [r for y in range(1992, 1999) for r in call(f'/q/revenue_by_month?year={y}&page_size=12', ADMIN)[2]]
check('15,000 TPC-H orders in all', sum(r['orders'] for r in months) == 15000)
check('a repeat is answered from the cache',
      call('/q/revenue_by_month?year=1995&page_size=12', ADMIN)[1].get('X-Cache') == 'HIT')

status, _, agent_rows = call('/q/top_accounts?year=1995&page_size=5', AGENT)
_, _, admin_rows = call('/q/top_accounts?year=1995&page_size=5', ADMIN)
check('the federated join runs for the agent', status == 200 and len(agent_rows) == 5, (status, agent_rows))
check("the agent's contact emails are hashed", all('@' not in r['contact_email'] for r in agent_rows), agent_rows)
check("the admin's are not", all('@' in r['contact_email'] for r in admin_rows), admin_rows)
check('the same rows either way', [r['customer'] for r in agent_rows] == [r['customer'] for r in admin_rows])

status, _, body = call('/execute_sql', AGENT, {'connection_name': 'lakehouse',
                                               'sql': 'SELECT count(*) AS n FROM tpch.tiny.lineitem'})
check('a table outside the agent\'s list is refused', status == 403 and body.get('code') == 'table_not_allowed', body)
status, _, body = call('/execute_sql', AGENT, {'connection_name': 'lakehouse', 'sql': 'DELETE FROM orders'})
check('a write is refused', body.get('code') == 'read_only', body)
check("the partner's one query works", call('/q/revenue_by_month?year=1996', PARTNER)[0] == 200)
check('and nothing else', call('/q/top_accounts', PARTNER)[2].get('code') == 'connection_forbidden')


async def tools():
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
    async with streamablehttp_client('http://127.0.0.1:5001/mcp', headers={'X-API-Key': AGENT}) as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()
            names = [t.name for t in (await session.list_tools()).tools]
            result = await session.call_tool('revenue_by_month', {'year': 1997, 'page_size': 12})
            return names, result.structuredContent['rows']

names, mcp_rows = asyncio.run(tools())
check('the saved queries are MCP tools', {'revenue_by_month', 'top_accounts', 'revenue_by_region'} <= set(names), names)
check('and answer over MCP', len(mcp_rows) == 12, mcp_rows)

sys.exit(f'{len(failures)} check(s) failed: {failures}' if failures else 0)

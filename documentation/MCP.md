# MCP server

`queryapigate mcp` exposes your saved queries as [MCP](https://modelcontextprotocol.io/) tools, so an AI
agent can list and call them directly - the same way `/catalog`/`/openapi.json` already let a human or a
REST client discover the same thing - instead of shelling out to `curl` against the REST API. Two more
tools, not tied to any saved query, let an agent work with a connection directly: `list_tables` (schema
discovery) and `execute_sql` (read-only ad-hoc SQL) - see [Ad-hoc tools](#ad-hoc-tools-list_tables-and-execute_sql)
below.

## What's exposed

Only **read-only** saved queries (`SELECT`/`WITH`/`SHOW`/`DESCRIBE`/`EXPLAIN`, and any Mongo `find()` query -
the same classification the REST API's own write guard already uses) become MCP tools. A write-capable saved
query stays reachable over REST as always, just not over MCP yet - calling a write through an MCP tool needs
a considered decision on confirming an LLM-initiated write before it executes, which this first version
doesn't make for you. The same is true of ad-hoc SQL: `execute_sql` (below) always runs read-only over MCP,
regardless of what the calling key would otherwise be allowed to do over REST.

Tool listing (`tools/list`) honors the exact same per-key scoping `GET /catalog` does -
`connections`/`queries`/`collections` grants, `apikeys.can_run_saved()` - so an agent's key never sees or
calls more than the identical REST key could. A tool's `inputSchema` is generated from the saved query's own
declared parameters, the same JSON Schema translation `/openapi.json` already uses.

Calling a tool (`tools/call`) runs the query through the exact same code path `GET /q/<name>` does: the same
connection-grant check, parameter validation, `cache_ttl` caching, `execution_history` recording and audit
trail. A run triggered over MCP shows up in the admin UI's History tab and the Home tab's live feed exactly
like a REST call would - there is no separate, parallel execution path to keep in sync.

## Structured results (`outputSchema`)

Every tool - a saved query, `list_tables`, or `execute_sql` - declares an `outputSchema`, and every
successful call returns a matching `structuredContent` alongside the usual text content (the text is a
serialized copy of the same object, kept for clients that don't read `structuredContent`). A row-returning
tool (a saved query or `execute_sql`) always returns `{"rows": [...], "truncated": <bool>}`; `list_tables`
returns `{"tables": [...], "truncated": <bool>}`. This is deliberately a generic envelope, not a per-column
schema - a saved query's actual result columns are only known once it runs, not from its stored definition,
so declaring real per-column types would need a new admin-authored field this version doesn't add.

**Breaking, if you already have an MCP integration**: before this, a row-returning tool's text content was
a bare JSON array of rows with an ad-hoc `"(truncated to N rows)"` suffix string when capped. It is now
always the wrapped `{"rows": [...], "truncated": <bool>}` object - read `structuredContent` directly rather
than parsing the text block, or parse the text block as this new object shape instead of a bare array.

## Ad-hoc tools: `list_tables` and `execute_sql`

Not every question an agent needs to answer has a saved query behind it yet. Two fixed tools - not tied to
any saved query - are listed for a key whenever it has any connection-level access at all (the same
`connections` grant `/execute_sql` and `/connections/<name>/schema` already check over REST; a key scoped
only to specific queries or collections never sees these two, the same way it can't reach those REST
endpoints either):

- **`list_tables`** - `{"connection_name": "...", "database": "..."}` (`database` is admin-key only, same
  as REST). Returns the same shape `GET /connections/<name>/schema` does: each table/view (or Mongo
  collection) with its columns, types, nullability, and primary/foreign keys where the dialect supports it.
- **`execute_sql`** - `{"connection_name": "...", "sql": "...", "params": {...}, "page_size": N,
  "database": "..."}`. Runs a single read-only statement - **always read-only over MCP**, regardless of the
  calling key's own `allow_writes` grant; a key's `allowed_tables` restriction, if it has one, is still
  enforced. Results are capped to `QUERYAPIGATE_MCP_MAX_ROWS` the same way a saved-query tool call is.

Both reuse the exact permission checks and execution code the REST API already has - there is no separate
ad-hoc-SQL path for MCP to drift out of sync with REST's own. An `execute_sql` call is recorded in run history
like a `POST /execute_sql` one, with `"transport": "mcp"` - see
[Ad-hoc runs](API.md#ad-hoc-runs).

## Running it

```bash
pip install "queryapigate[mcp]"
queryapigate mcp
```

It listens on its own port (`QUERYAPIGATE_MCP_PORT`, default `5001`) - separate from `queryapigate serve`'s
port, so both can run side by side against the same `QUERYAPIGATE_HOME`. The MCP endpoint is
`http://<host>:<port>/mcp`, speaking Streamable HTTP; `/metrics` and `/health` on the same port report on this
process.

It's a **separate process** from the REST server, not a mode of it: MCP's HTTP transport is
[ASGI](https://asgi.readthedocs.io/)-native, while QueryAPIGate's REST API is WSGI (Flask on gunicorn). What
actually matters for correctness - reusing this project's own connection-grant checks, caching and audit
logging by direct Python import, not an HTTP round-trip back to the REST API - holds regardless of which
process each server runs in.

## Authentication

Same as the REST API: an `X-API-Key` header, checked against the same `QUERYAPIGATE_API_KEY`/scoped keys - or,
when [signed-in users](API.md#signed-in-users-jwt) are enabled, an `Authorization: Bearer` token, so an agent
acting for a user gets that user's role and its `from_claim` row filters. A call carrying an API key is judged on
that key alone; a wrong key never falls back to the token. Send either as a request header, the way any
HTTP-capable MCP client configuration allows - never as a URL parameter, which would leak it into logs. An unset
server key means open access, same as the REST API.

## Governance: the same rules as REST

Every tool call passes through the same checks a REST request does, in the same order, by calling the same code
(`queryapigate/governance.py`):

1. **`QUERYAPIGATE_RATE_LIMIT`**, per client address, before authentication - so guessing keys is throttled too.
2. **Authentication**, as above.
3. **The caller's own `rate_limit` grant** (a key's, or a signed-in user's role's).
4. **Grants** - connections, queries, collections, `allowed_tables`, IP allowlists - then the tool itself.

A refused call is a tool error (`isError: true`) whose text says why, and whose `structuredContent` carries the
same `{"error", "code"}` a REST error body does - `rate_limited` (with `retry_after`), `unauthorized`,
`table_not_allowed`, `read_only`, `param_invalid` and the rest listed under [Errors](API.md#errors) - so an agent
can branch on the code instead of parsing the message. Every call is counted in `/metrics`'
request counters and latency histograms with method `MCP` and endpoint `mcp.saved_query`, `mcp.execute_sql` or
`mcp.list_tables`, so agent traffic can be told apart from REST's. Saved-query and ad-hoc runs are recorded in
run history (and live events) with `"transport": "mcp"`.

**Limits are shared through Redis.** With `QUERYAPIGATE_REDIS_URL` set (the same value for both processes), a key's
`rate_limit` of 100/minute is 100 calls a minute over REST and MCP together. Without it, each process counts on its
own: 100 over REST *and* 100 over MCP, as two instances would. Metrics are per process either way: `queryapigate mcp` serves its own
`GET /metrics` (and `GET /health`) on its port, beside `/mcp`, with no key needed - the same as the REST server's.
Scrape both.

**The CLI is a local operator, not a caller.** `queryapigate export` reads the store directly with no key, so no
grant or rate limit applies - whoever can run it can already read `QUERYAPIGATE_HOME`. Its runs are still recorded
in run history, with `key_name` `cli`.

## Result size

A tool call caps its result at `QUERYAPIGATE_MCP_MAX_ROWS` (default 200) rows, independent of
`QUERYAPIGATE_MAX_PAGE_SIZE` - an LLM's context window can't hold a result the way a human paging through the
admin UI can. A truncated result says so in its own text. A caller-supplied `page_size` smaller than the cap
is still honored.

## Configuration

| Variable | Default | Effect |
|----------|---------|--------|
| `QUERYAPIGATE_MCP_PORT` | `5001` | Bind port for `queryapigate mcp`. |
| `QUERYAPIGATE_MCP_MAX_ROWS` | `200` | Row cap for a tool call's result. Always a positive count; a malformed value stops startup. |

Both show up as a read-only "MCP server" section on the admin UI's Settings screen (BACKLOG #54) - the
effective value and whether it's the default, same as every other setting there. Below them, **Check now** tries
a TCP connection to that port on `127.0.0.1` (only when asked - `GET /api/v1/mcp/status`), and **Tools** lists what
`tools/list` returns for the admin key (`GET /api/v1/mcp/tools`). Both are worked out by the REST server: the check
only says whether *something* listens on that port on the same host, and the settings shown are the REST server's -
an MCP process started with different variables isn't reflected. See
[Check the MCP server from the Console](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/how-to/27-check-the-mcp-server-from-the-console.md).

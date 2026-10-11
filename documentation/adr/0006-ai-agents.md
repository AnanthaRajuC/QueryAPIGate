# ADR 0006: AI agents - OAuth for the MCP server, descriptions agents read, and a per-agent usage view

- **Status:** Accepted
- **Date:** 2026-10-11
- **Backlog:** #85 (OAuth 2.1 for the MCP server), #86 (descriptions for agents), #87 (a per-agent usage view).
  Builds on #63 column masking ([ADR 0005](0005-column-masking.md)) and #84 admin identity ([ADR 0003](0003-named-administrators.md)).

## Context

QueryAPIGate's MCP server (`queryapigate mcp`, Streamable HTTP at `/mcp`) exposes saved queries as tools, plus
`list_tables` and `execute_sql`, to any agent with an API key or a signed-in user's JWT in a request header. Since 0.17
those callers can carry column-masking rules. Three gaps stand between that and "let our agents near our data":

1. **Hosted AI clients can't connect.** A connector in a hosted assistant doesn't take an API key in a header; it
   follows the MCP specification's authorization: OAuth 2.1, discovered from the server. And today a missing or wrong
   credential comes back as a *tool error* inside a successful HTTP response, which no OAuth client recognises.
2. **Agents guess what columns mean.** They see `orders.amt` and `cust_tier`, not "net revenue in EUR, refunds
   excluded". Their answers are only as good as what they're told.
3. **Nobody can see what an agent did.** Run history has every MCP call, but nothing summarises it per agent - which
   questions, how many rows, what was refused or masked.

The MCP authorization specification (2025-11-25, the version the installed SDK speaks) settles the shape of the first:

- An MCP server is an **OAuth 2.1 resource server**. It **must** publish Protected Resource Metadata (RFC 9728) naming
  its authorization server(s), answer `401` with `WWW-Authenticate: Bearer resource_metadata="..."` (and should name
  the `scope` needed), and answer `403` with `error="insufficient_scope"` when a token lacks one.
- It **must** accept only tokens issued for itself - the token's audience must be its own canonical URI (RFC 8707,
  sent by the client as `resource`) - and **must not** pass a token through to anything else.
- The **authorization server** is a separate role: it may be the organisation's own identity provider. Clients find it
  through the metadata, and register with it by - in the spec's order of preference - pre-registration, **Client ID
  Metadata Documents** (an HTTPS URL as `client_id`; the spec's *should*), or Dynamic Client Registration (a *may*, kept
  for older clients). PKCE with S256 is mandatory.

What exists to build on: `jwtauth.py` already verifies JWTs against an identity provider's published keys (JWKS), with
`iss`/`aud`/`exp` checks and fixed algorithms; roles map claims to grants, including `mask` rules and `from_claim`
parameters; the MCP Python SDK (1.30) ships a token-verifier interface, the bearer middleware and the metadata routes.

## Decision

### 1. The MCP server as an OAuth 2.1 resource server, with the organisation's identity provider as its authorization server (#85)

QueryAPIGate **does not become an authorization server** in this release. It validates tokens; the organisation's
identity provider issues them, signs users in, runs consent, and registers clients. That keeps passwords, sessions,
MFA and client registration where they already are, and it is all the MCP specification requires of a server.

Configuration (the first two and a role together, or none - checked at startup; plus `QUERYAPIGATE_MCP_SCOPE`,
default `queryapigate.mcp`):

| Setting | Meaning |
|---|---|
| `QUERYAPIGATE_MCP_RESOURCE_URL` | The MCP server's canonical URI, e.g. `https://data.example.com/mcp` - its own identity, and the audience every token must name |
| `QUERYAPIGATE_MCP_AUTH_SERVER` | The issuer URL of the identity provider; its keys are found through its metadata (RFC 8414 or OpenID Connect discovery) |
| `QUERYAPIGATE_MCP_ROLE_CLAIM` (optional) | The claim naming the caller's role, as `QUERYAPIGATE_JWT_ROLE_CLAIM` does for REST; `QUERYAPIGATE_MCP_ROLE` for a fixed one |

What changes on `/mcp`:

- **Protected Resource Metadata** at `/.well-known/oauth-protected-resource` and
  `/.well-known/oauth-protected-resource/mcp`: `resource`, `authorization_servers`, `scopes_supported`
  (`queryapigate.mcp`), `bearer_methods_supported: ["header"]`.
- **HTTP 401, not a tool error,** for a missing, invalid or expired credential - with `resource_metadata` and
  `scope="queryapigate.mcp"` when OAuth is configured. This is the one visible change for existing clients: one that
  sent a wrong key used to get a tool error in a `200`; now it gets `401`. A valid key or token behaves as before.
- **Token validation:** the signature against the identity provider's keys; `iss` its issuer; `aud` must contain
  `QUERYAPIGATE_MCP_RESOURCE_URL` - **a token issued for REST, or for any other service, is refused**, and REST's own
  JWT setting never accepts an MCP token either; `exp` required; the `queryapigate.mcp` scope required, else `403
  insufficient_scope`. JWT access tokens only in this release; opaque tokens through introspection (RFC 7662) later if
  asked for.
- **What the agent may do** comes from the role the token names - connections, saved queries, `allowed_tables`,
  rate limit, and **mask rules** - and `from_claim` parameters bind to the token's claims, so an agent acting for a
  person sees only that person's rows, with that role's columns masked.
- **API keys keep working** on `/mcp` for SDKs and automation. REST-style JWTs keep working there only while OAuth
  for the MCP server is off: once it's on, a bearer token on `/mcp` must be one issued for it, as the specification
  requires. No token is ever passed on: the MCP server talks to databases with connection credentials, never with the
  caller's token.

**Which identity providers work with hosted clients** depends on the provider, not on QueryAPIGate: a hosted client
needs to register itself - via Client ID Metadata Documents or Dynamic Client Registration - or be pre-registered by
an administrator. The guide will say how to check, and how to pre-register where neither is available.

**Rejected for now: a built-in authorization server** (QueryAPIGate issuing its own tokens, brokering sign-in to the
organisation's identity provider, and supporting Client ID Metadata Documents itself). It would make QueryAPIGate work
with providers that support neither registration method - but it means a consent screen, authorization codes, token
signing keys and their rotation, refresh-token rotation, and the SSRF and redirect-URI risks of fetching client
metadata: a security product of its own. Revisit after #84 Phase 2 (Console sign-in through OIDC), which needs much of
the same machinery, if providers without either registration method turn out to block real use.

### 2. Descriptions agents read (#86)

- **Table and column descriptions,** stored per connection (a new `descriptions` table, schema 9), edited in the
  Console's schema browser and through `/api/v1/connections/{name}/descriptions`, and **imported** on request from
  where many teams already keep them: the database's own comments (`COMMENT ON` in PostgreSQL, column comments in
  MySQL, ClickHouse and DuckDB).
- **Saved-query column descriptions** in the query's definition (`columns: {"amt": "Net revenue in EUR, refunds
  excluded"}`), carried into the tool's output schema.
- **Served where agents look:** MCP's `list_tables` returns each table's and column's description; each saved-query
  tool's description and output schema carry its own. An MCP *resource* per connection - the data dictionary - holds
  them all, with approved joins and metric definitions written as text.
- **No more reach than the caller has:** descriptions are shown only for the tables the caller can reach
  (`allowed_tables`), and a masked column's description says it is masked - an agent shouldn't learn that a table
  exists through its description, or plan a query around a value it will never see.
- Who may write them: `connections.write` for connection descriptions (owners, admins); a saved query's travel with
  it (`queries.write`).

### 3. A per-agent usage view (#87)

- `GET /api/v1/usage/agents?since=...&until=...` (capability `observe`): per caller on MCP - calls, tools used, rows
  read, refusals by code (`mask_unresolvable`, `table_not_allowed`, `read_only`, ...), how often masking applied, and
  the most frequent ad-hoc statements (by SQL hash). Computed from run history; nothing new is recorded.
- A Console screen, **Agents**, under Observability.
- An alert, `agent_unusual_volume`: an agent's rows read in the last hour well above its own recent hourly typical
  (a plain comparison with its own history, as BACKLOG's anomaly-detection entry allows), with a floor so a quiet
  agent's first busy hour doesn't fire.

## Consequences

- **New:** three MCP settings; the metadata routes; HTTP 401/403 on `/mcp`; schema 9 (`descriptions`); the
  descriptions API and import; `/api/v1/usage/agents`; the Agents screen; one alert. All **experimental** in their
  first release.
- **Changed:** `/mcp` answers a bad credential with HTTP 401 instead of a tool error - noted under Upgrading.
- **Security posture:** audience binding means a leaked REST token can't be replayed against `/mcp`, nor an MCP token
  against REST. The residual risk sits with the identity provider's configuration (who may get a token naming the MCP
  resource, with which role), which the guide covers.
- **Not in scope:** a built-in authorization server (above), opaque-token introspection, write tools over MCP (#88,
  which needs a confirmation design of its own), and descriptions generated by a model.

## Delivery

About two weeks, in slices:

1. `/mcp` as a resource server: metadata routes, HTTP 401/403 with `WWW-Authenticate`, a token verifier on
   `jwtauth.py`'s verification with MCP's own audience and scope, roles from claims; tests against a real
   OIDC provider in a container (Keycloak), including audience-confusion and expired/foreign-token cases.
2. A guide: connect a hosted AI client to QueryAPIGate through your identity provider - registration options, roles,
   masking, and checking it from the client's side.
3. Descriptions: schema 9, the API, import from database comments, `list_tables`, tool schemas, the dictionary
   resource, the Console's schema browser.
4. The usage API, the Agents screen and the alert.
5. Docs: threat model (tokens, audience, the identity provider's part), changelog.

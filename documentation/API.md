# API reference

Base URL when running locally: `http://127.0.0.1:5000`. The same information is available interactively at `/docs`
and as an OpenAPI document at `/openapi.json`.

If the server has any key configured - `QUERYAPIGATE_API_KEY` or a scoped key created through `/api/v1/api-keys` - send it
with every request as `X-API-Key: <key>` (`/health`, `/docs`, `/console`, `/openapi.json` and `/metrics` are always
public; `/openapi.json`'s saved-query section still varies with who's asking). See
[Authentication and permissions](#authentication-and-permissions).

| Endpoint | Method | Purpose |
|----------|--------|---------|
| [`/execute_sql`](#execute-sql) | POST | Run ad-hoc SQL |
| [`/q/<name>`](#run-a-saved-query) | GET, POST | Run a saved query as an endpoint |
| [`/connections/<name>/schema`](#connections) | GET | List a connection's tables/views and their columns |
| [`/catalog`](#the-api-catalogue) | GET | The saved queries this caller can reach, and the terms they're offered under |
| [`/events`](#live-events-server-sent-events) | GET | Live saved-query runs (Server-Sent Events) |
| [`/api/v1/...`](#management-api-v1) | GET, POST, PATCH, DELETE | The Management API: saved queries, connections, API keys, roles, collections, history, audit, settings and more (admin only) |
| `/health` | GET | `{"status": "ok", "version": "...", "time_zone": "Asia/Kolkata", "utc_offset": "+05:30"}` - the zone every timestamp the server returns is in (`time_zone` is `null` when the server can't name it) |
| [`/metrics`](#observability) | GET | Prometheus text-format metrics |

## Common query parameters

These apply to every endpoint that returns rows.

| Parameter | Default | Description |
|-----------|---------|-------------|
| `format` | `json` | `json`, `ndjson`, `csv`, `tsv`, `xml`, `yaml`, `xlsx` or `parquet`. For the POST endpoints it may also be given in the JSON body. |
| `page` | `1` | 1-based page number. |
| `page_size` | `10` | Rows per page, at most `QUERYAPIGATE_MAX_PAGE_SIZE` (default 1000). |
| `timeout` | server limit | Seconds the query may run before it is cancelled with a 504. It can lower the server limit (`QUERYAPIGATE_QUERY_TIMEOUT`, default 30; `0` disables it) but never raise it. For the POST endpoints it may also be given in the JSON body. |

Paging happens *within* the statement's own row window. A query that ends in `LIMIT 3` returns at most 3 rows
whatever `page_size` is, and `LIMIT 25` with `page_size=10` gives pages of 10, 10 and 5, then `X-Has-More: false`.
Every form is understood: `LIMIT n [OFFSET m]`, `LIMIT m, n`, `LIMIT ALL`, `OFFSET m` alone, and
`[OFFSET m ROWS] FETCH FIRST n ROWS ONLY`. Each number may be a bound `:parameter`, whose value must then be a
non-negative whole number. A `LIMIT` inside a subquery, a string or a comment is left alone. Responses carry
`X-Page`, `X-Page-Size` and `X-Has-More` (`true` when another page exists). A query that returns no rows answers
`{"message": "No results returned"}`.

## Execute SQL

`POST /execute_sql?format=json&page=1&page_size=5`

~~~json
{
    "sql": "SELECT * FROM film WHERE film_id > :min AND title LIKE :name",
    "params": {"min": 10, "name": "C%"},
    "connection_name": "examples"
}
~~~

- `:name` markers are **bound parameters**: values are sent to the database separately from the SQL. Markers inside
  string literals, comments and Postgres `::` casts are ignored. Every marker needs a value in `params`.
- Unless the server sets `QUERYAPIGATE_ALLOW_WRITES=1`, only a single `SELECT`, `WITH`, `SHOW`, `DESCRIBE`, `EXPLAIN` or
  `VALUES` statement is accepted (403 otherwise; 400 for several statements).

## Streaming exports

`page_size` is capped (`QUERYAPIGATE_MAX_PAGE_SIZE`, default 1000) - there is normally no way to pull a full result
larger than that in one call. `?stream=true` lifts that cap: the whole result is streamed straight from the
database cursor as it comes in, rather than built up in memory first, so an export far larger than fits in memory
can still be downloaded. It works on both `POST /execute_sql` and `GET`/`POST /q/<name>`:

~~~bash
curl -X POST 'http://127.0.0.1:5000/execute_sql?stream=true&format=csv' \
     -H 'Content-Type: application/json' \
     -d '{"sql": "SELECT * FROM film", "connection_name": "examples"}' -o film.csv
~~~

- Only `format=csv`, `tsv`, `ndjson` or `parquet` are streamable (400 for `json`/`xml`/`yaml`/`xlsx` - those formats all
  need the whole document structure in memory to write correctly, so paging still applies to them normally). Parquet is
  written to a temporary file by DuckDB first - its footer comes last - and then sent, so its first byte arrives once
  the query has finished; memory stays within DuckDB's own, which spills to disk.

### Parquet

`?format=parquet` (paged or streamed) and `queryapigate export --format parquet` work on every database type. The rows
are written by DuckDB - the `duckdb` extra, included in the Docker image; without it the request answers `500
format_unavailable`. Column types come from the values: integers `BIGINT` (`DECIMAL(38,0)` past 64 bits), floats
`DOUBLE`, decimals `DECIMAL(38, scale)` - exact, not converted to floats - booleans, `DATE`, `TIMESTAMP`
(`TIMESTAMPTZ` with a time zone) and `TIME`; anything else, or a mix, `VARCHAR`. A database that returns dates as text
(SQLite) gives text columns, as its driver does. Content type `application/vnd.apache.parquet`.
- `page`/`page_size` are rejected together with `stream=true` (400) - the whole point is that there is no page.
- **Always read-only**, regardless of `QUERYAPIGATE_ALLOW_WRITES` or the calling key's own write permission - a large
  export has no business mutating data. A write statement gets the usual 403 from the SQL guard.
- The response carries `Content-Disposition: attachment` with a filename, so a browser hitting the URL directly
  downloads it rather than navigating in-page.
- For a saved query, the run is still appended to `execution_history` once the stream finishes, but `cache_ttl` is
  ignored - caching would mean building the whole body in memory first, exactly what streaming avoids.
- If the connection or SQL itself is invalid, that surfaces as the usual JSON error before any data is sent. A
  failure *partway through* an already-started stream cannot change the response's status or body shape any more,
  though - the client just sees the download end early; check the server log for what actually happened.
- How much this bounds the *server's* memory, not just removing the cap, varies by database - MySQL, PostgreSQL and
  ClickHouse stream from the server without buffering the whole result client-side first; SQLite, H2, the generic
  `jdbc` type and DuckDB still bound this project's own memory to one batch at a time, but the underlying engine or
  driver may materialise more than that internally - see [Connection pooling](DATABASE_CONNECTION_CONFIGURATION.md#connection-pooling)
  for the per-dialect detail and what it means for how long a pooled connection stays checked out.
- `QUERYAPIGATE_STREAM_MAX_ROWS`, if set, caps how many rows a single export returns - unlike `page_size`, nothing
  bounds `stream=true` by default, since the whole point is not buffering the result to know its size up front. A
  malformed value is rejected at startup, the same as `QUERYAPIGATE_RATE_LIMIT`. Past the cap, the export ends early
  (whatever HTTP headers and rows already went out stand; there's no way to retroactively mark an in-progress `200`
  as partial) and a `WARNING` is logged naming the connection and the limit hit. `GET /metrics`'s
  `queryapigate_stream_exports_total` counts a capped export under `status="truncated"`, distinct from `"success"`; a
  saved query's `execution_history` still records it as `"success"` with the truncated row count - it did succeed,
  just not to completion.

## Save a query

`POST /api/v1/queries` creates a saved query with its first version; `POST /api/v1/queries/{name}/versions` adds the
next version (see [Management API (v1)](#management-api-v1)):

~~~json
{
    "name": "film_by_id",
    "sql": "SELECT * FROM film WHERE film_id = :id",
    "parameters": {"id": "int"},
    "connection_name": "examples",
    "author": "anantha",
    "description": "Look up a film",
    "tags": ["example"],
    "publish": true
}
~~~

- `name` may contain letters, digits, spaces, `.`, `_` and `-`. A name already taken is a 409; add a version instead.
- `parameters` declares the query's parameters and their [rules](#parameter-rules). The definitions are checked when you save (400 with an `errors` map if any is invalid), and every parameter declared must be used in `sql`.
- `connection_name` (optional) is the default connection when a run does not name one.
- `cache_ttl` (optional, seconds) caches a response - see [Response caching](#response-caching).
- `collection` (optional, on create) files the query under a [collection](#collections). It belongs to the query, not to a version: change it later with `PATCH /api/v1/queries/{name}`.
- `publish` (default false): a new version is a draft until published - see [Drafts and publishing](#drafts-and-publishing).
- A Mongo query sets `"query_type": "mongo"` and `mongo_collection`, `mongo_filter`, `mongo_projection`, `mongo_sort` instead of `sql`.
- Response: `201` with the query and its versions, and an `ETag`.

## Run a saved query

`GET /q/film_by_id?id=7&format=csv` - query-string arguments other than `format`, `page`, `page_size`,
`connection_name` and `version` become parameters.

`POST /q/film_by_id` - the JSON body may contain `params`, `connection_name`, `version` and `format`:

~~~json
{"params": {"id": 7}, "connection_name": "examples", "version": 1}
~~~

- The **published** version runs unless `version` is given (see [Drafts and publishing](#drafts-and-publishing)).
- `connection_name` from the request wins over the saved default.
- Each run is recorded in that version's `execution_history` (time, connection, status, rows, duration, plus
  `request_id`/`key_name` - see [Observability](#observability)). Lists show a version's newest 50 runs
  (`QUERYAPIGATE_HISTORY_LIMIT`); how many are kept, and for how long, is configurable - see
  [Run history](#run-history).

### Drafts and publishing

Every saved query has at most one **published** version: the one `/q/<name>` runs, and the one the catalog,
`/openapi.json`, MCP tools, Postman and bundle exports describe. A version newer than the published one is a
**draft**, and so is every version of a query with nothing published. A query with nothing published isn't served
at all: `/q/<name>` answers 404.

- **Drafts are invisible to callers.** Only the admin key can run one, and only by asking for it by number
  (`?version=3`), to test it before publishing. For any other key, a draft's number answers the same 404 as a
  version that never existed.
- **Older versions stay runnable** with `?version=`, as before: each was live once.
- **A new version is a draft unless saved with `"publish": true`.** The Management API also publishes,
  unpublishes and rolls back to any earlier version. `queryapigate collection import` and `examples load` publish
  what they save.
- **Deleting the published version** falls back to the newest version older than it, never to a draft. With none
  older, the query is left unpublished.
- `GET /api/v1/queries` shows each query's `published_version` (`null` when nothing is published).

Stores created before this (schema 3 and older) are upgraded on first start with every query published at its
newest version, so nothing a caller sees changes.

`POST /execute_sql_from_file` and `POST /execute_sql_with_parameters_from_file`, which ran a saved query named in
the body, were deprecated in 0.14 and removed in 0.15 - use `/q/{name}`.

## Response caching

Opt-in, per saved query: set `cache_ttl` (seconds) when [saving it](#save-a-query), or turn it on/off or retune it
for an existing version in place with `PATCH /api/v1/queries/<name>/versions/<n>` (admin only,
`{"cache_ttl": <seconds>}`, `0` or `null` turns it off) - not a new version,
so it never bumps `execution_history` or the query's version number, the same way moving a query's collection
doesn't. A cached response is only ever served for that exact name, version, connection, resolved parameter
values, `format` and page - anything else is a separate entry. It is **never** used for a query whose SQL is a
write (`INSERT`/`UPDATE`/`DELETE`/DDL), regardless of `cache_ttl`: caching such a query would silently skip the
write on every call after the first.

A fresh response carries `ETag`, `Cache-Control: max-age=<cache_ttl>` and `X-Cache: MISS`. A request within the TTL
gets the same body with `X-Cache: HIT`; send back `If-None-Match: <ETag>` to get `304 Not Modified` with no body
instead. A cache hit is not appended to `execution_history` - nothing ran against the database. The cache is kept
in this one process's memory by default, or shared across restarts/instances when `QUERYAPIGATE_REDIS_URL` is set
(see [Installation & Setup](INSTALLATION_AND_SETUP.md#shared-response-cache-redis) and the Caching tab in the admin
UI) - either way it is shared across every API key that can use the connection, since it stores nothing an
authorized caller could not already see by running the query itself.

## Parameter rules

`query_parameters` maps each parameter name to its type, or to an object of rules:

~~~json
{
    "rating":     {"type": "str", "enum": ["G", "PG", "R"], "default": "PG", "description": "MPAA rating"},
    "max_length": {"type": "int", "min": 1, "max": 600, "default": 120},
    "title":      {"type": "str", "required": false, "min_length": 2, "pattern": "[A-Za-z ]+"},
    "id":         "int"
}
~~~

| Rule | Applies to | Meaning |
|------|-----------|---------|
| `type` | any | `int`, `float`, `str` or `bool` (aliases `integer`, `number`, `string`, `boolean`). Query-string text is converted; JSON values must already have the right type. Untyped parameters accept any single value. |
| `required` | any | Defaults to `true`, or to `false` when a `default` is given. An optional parameter with no value and no default is bound as `NULL`, so `(:title IS NULL OR title LIKE :title)` works. |
| `default` | any | Used when the request supplies nothing. It must itself satisfy the other rules. Cannot be combined with `"required": true`. |
| `enum` | any | The value must be one of these. |
| `min`, `max` | numbers | Inclusive bounds. |
| `min_length`, `max_length` | text | Length bounds. |
| `pattern` | text | A regular expression the whole value must match (at most 500 characters). |
| `description` | any | Shown in `/docs`. |
| `from_claim` | any | Take the value from a claim of the caller's [sign-in token](#signed-in-users-jwt) - e.g. `"sub"` - never from the request. Always required; no `default`. |

Requests that break a rule are rejected with **400** before anything reaches the database, with every problem listed:

~~~json
{
    "error": "Invalid parameters: rating must be one of: G, PG, R; max_length must be at most 600",
    "errors": {"rating": "must be one of: G, PG, R", "max_length": "must be at most 600"}
}
~~~

Parameters used in the SQL but not declared still work: they are required and passed through as supplied.
Requests rejected this way are not recorded in the query's `execution_history`.

### Text placeholders (legacy)

Saved SQL may also contain `{name}` placeholders, which are substituted **as text** before the query runs. Because the
value becomes part of the SQL it must be a number, a boolean, or a string made only of letters, digits, whitespace and
`. , : @ % + / -`; anything else is rejected. Prefer bound `:name` parameters.

## List, change and delete saved queries

Through the [Management API](#management-api-v1): `GET /api/v1/queries` lists them (filter by `search`,
`collection`, `connection` or `status`), `GET /api/v1/queries/{name}` shows one with every version and its SQL, and
`DELETE /api/v1/queries/{name}` (or `.../versions/{n}`, one version - the query goes with its last) removes it.
Deleting the published version publishes the newest older one, if any - see
[Drafts and publishing](#drafts-and-publishing).

## Management API (v1)

`/api/v1/...` is the versioned interface to QueryAPIGate's own configuration, built one resource at a time
([ADR 0001](adr/0001-console-and-management-api.md)). The Console at `/console` uses it, and so can scripts,
Terraform or GitOps tooling. Only [administrators](#administrators-and-admin-roles) may call it, each operation as
their role allows. Every request and response is described in full in `/openapi.json`, under the "Management API v1"
tag.

**Conventions, the same on every resource:**
- **Errors** are `{"error": "...", "code": "...", "request_id": "..."}`. Branch on `code`, which is stable (for
  example `query_not_found`, `query_exists`, `version_not_found`, `precondition_failed`, `unknown_field`,
  `invalid_request`, `unauthorized`, `forbidden`). `error` is for people and its wording can change. `request_id`
  matches the `X-Request-Id` header and the server log.
- **Optimistic concurrency.** Reading a query returns an `ETag`. Send it back as `If-Match` on a change and the
  change is refused with `412 precondition_failed` if the query changed in the meantime, through any route.
- **Unknown fields are refused** (`unknown_field`), so a typo never silently does nothing.

### Queries

| Method and path | What it does |
|---|---|
| `GET /api/v1/queries` | Every saved query's summary, including `created_at` (its first version), `last_used_at` (its newest stored run) and `cache_ttl`. Filters: `search` (name, description or tag), `collection`, `connection`, `status` (`published`, `unpublished`, `draft`). |
| `POST /api/v1/queries` | Create a query with its first version: `name`, `description`, `sql`, `connection_name`, `parameters`, `tags`, `cache_ttl`, `collection`. A draft unless `"publish": true`. 409 `query_exists` if the name is taken. |
| `GET /api/v1/queries/{name}` | The query with every version, each with its `status` (`published`, `draft` or `previous`) and `run_count` (its runs stored in history). |
| `PATCH /api/v1/queries/{name}` | Change its `collection` (`null` removes it). |
| `DELETE /api/v1/queries/{name}` | Delete it and every version. |
| `POST /api/v1/queries/{name}/versions` | Add a version: a draft unless `"publish": true`. The published version keeps serving. |
| `GET /api/v1/queries/{name}/versions/{n}` | One version. |
| `PATCH /api/v1/queries/{name}/versions/{n}` | Change its `cache_ttl` in place. |
| `DELETE /api/v1/queries/{name}/versions/{n}` | Delete one version; see [Drafts and publishing](#drafts-and-publishing) for what happens to the published one. |
| `POST /api/v1/queries/{name}/publish` | `{"version": n}`: publish a draft, or an older version to roll back. |
| `POST /api/v1/queries/{name}/unpublish` | Stop serving it; every version is kept. |
| `GET /api/v1/queries/{name}/versions/{version}/flow` | The tables and joins the version's SQL touches, and the SQL formatted. Best effort: a query that can't be analyzed (Mongo, an unsupported dialect, a parse failure) has an `error` and no tables. |
| `GET /api/v1/queries/{name}/history` | Its runs, newest first. Paged with `limit` and `cursor` (`next_cursor` in the response); filters `version`, `status`, `key`, `since`, `until`. |
| `POST /api/v1/queries/validate` | Check a definition without saving it. Returns every problem found, the parameters the SQL uses and the tables it reads. |

v1 names fields for what they are: `sql` (not `sql_query`), `parameters` (not `query_parameters`), and no
`filename`, because `name` is the identity. A parameter rule is the same object as in
[Parameter rules](#parameter-rules), with type names `integer`, `number`, `string` and `boolean`. `author`
defaults to the calling key's name.

~~~bash
curl -X POST http://127.0.0.1:5000/api/v1/queries -H 'X-API-Key: <admin key>' -H 'Content-Type: application/json' \
     -d '{"name": "film_by_id", "description": "One film", "connection_name": "examples",
          "sql": "SELECT * FROM film WHERE film_id = :id", "parameters": {"id": {"type": "integer"}}}'
# a draft: /q/film_by_id answers 404 until it is published
curl -X POST http://127.0.0.1:5000/api/v1/queries/film_by_id/publish -H 'X-API-Key: <admin key>' \
     -H 'Content-Type: application/json' -d '{"version": 1}'
~~~

### Connections

| Method and path | What it does |
|---|---|
| `GET /api/v1/connections` | Every connection's `name`, `db`, `active`, `host`, `port`, `database`, `user`, `example`, timestamps and live `usage` (queries, errors, rows, average latency since the process started). Never credentials. |
| `POST /api/v1/connections` | Create one: `name`, `db` (required), `host`, `port`, `user`, `password`, `database`, `active` (default true), and `options` for driver settings (`sslmode`, `jdbc_url`, ...). 409 `connection_exists` if the name is taken. |
| `GET /api/v1/connections/{name}` | One connection, with its `options`. The password is masked as `********` unless it is a `${ENV_VAR}` reference. |
| `PATCH /api/v1/connections/{name}` | Change some fields; everything not mentioned is kept, driver options included. Send the mask (or leave `password` out) to keep the stored password; `null` removes an optional field. Honours `If-Match`. |
| `DELETE /api/v1/connections/{name}` | Body `{"reason": "..."}`, required (`reason_required`), and kept in the audit log. Every saved query using it stops working. |
| `GET /api/v1/connections/deleted` | Deleted connections, newest first, with who deleted them, when and why. |
| `POST /api/v1/connections/test` | Try to connect with the given fields (an unsaved form), or a saved connection's own by `name`. Nothing is saved. A connection that can't be reached is a 502 `connection_failed` with the driver's message in `detail` (any password in it is redacted). |
| `POST /api/v1/connections/databases` | The databases on that server, for the same two kinds of body. |
| `GET /api/v1/connections/{name}/schema` | Its tables and columns; `?database=` reads another database on the same server, for the types that have more than one (MySQL, PostgreSQL, ClickHouse, MongoDB). |

Every change is audited exactly as the legacy routes audit theirs. A password appears in an audit entry only as
`"changed"`, never its value.

### API keys and roles

| Method and path | What it does |
|---|---|
| `GET /api/v1/api-keys` | Every key's grants (`connections`, `queries`, `collections`, `allow_writes`, `allowed_write_ops`, `allowed_tables`, `rate_limit`, `allowed_ips`), `active`, `expires_at` and `expired`, `created_at`, `created_from_role`, `last_used_at` and live `usage`. Never secrets. |
| `POST /api/v1/api-keys` | Create one, from explicit grants or from a `role` (whose grants are copied once; no grant fields then). The response's `secret` is the only time it is ever shown, and is sent with `Cache-Control: no-store`. 409 `key_exists`, 404 `role_not_found`. |
| `GET /api/v1/api-keys/{name}` | One key. |
| `PATCH /api/v1/api-keys/{name}` | Change some grants, `expires_at` or `active` (`false` revokes it at once); `null` clears an optional field. Honours `If-Match`. |
| `DELETE /api/v1/api-keys/{name}` | Revoke and remove it: anything still using it stops working immediately. |
| `GET /api/v1/roles` | Every role's grants, with `keys_created`: the keys created from it. |
| `POST /api/v1/roles` | Create one. 409 `role_exists`. |
| `GET`, `PATCH`, `DELETE /api/v1/roles/{name}` | As for keys. A role is a template: changing or deleting it never touches a key already created from it. |

A name is 1-100 letters, digits, spaces, `.`, `_` or `-` (`invalid_name`). Every change is audited, never with a
secret.

### Administrators

| Method and path | What it does |
|---|---|
| `GET /api/v1/me` | Who you are: `name`, `role`, `via` (`token`, `break-glass`, `open`), your role's `capabilities`, and `data_access` (whether you may run SQL). Any administrator. |
| `GET /api/v1/administrators` | Every administrator: `name`, `role`, `email`, `active`, `created_at`, `created_by`, `last_seen_at`, `tokens` (how many). Owners only. |
| `POST /api/v1/administrators` | Create one: `{"name": "alice", "role": "developer", "email": "alice@corp.com"}`. 409 `admin_exists`, or `name_taken` for an API key's name. Owners only. |
| `GET`, `PATCH`, `DELETE /api/v1/administrators/{name}` | One administrator; change `role`, `email` or `active` (`false` stops all their tokens at once); remove them with their tokens. Honours `If-Match`. 409 `last_owner` when the change would leave no active owner and `QUERYAPIGATE_API_KEY` isn't set. |
| `GET /api/v1/administrators/{name}/tokens` | Their tokens: `id`, `label`, `created_at`, `expires_at`, `expired`, `last_used_at` - never the secret. |
| `POST /api/v1/administrators/{name}/tokens` | Issue one: `{"label": "laptop", "expires_at": "2027-01-01"}` (both optional; no `expires_at`, no expiry). The response's `secret` (`qagadm_...`) is the only time it is shown, sent with `Cache-Control: no-store`. |
| `DELETE /api/v1/administrators/{name}/tokens/{id}` | Revoke one; their other tokens keep working. |

Tokens: every administrator may list, issue and revoke **their own**, whatever their role; anyone else's take an
owner.

### History and audit

| Method and path | What it does |
|---|---|
| `GET /api/v1/history` | Every saved query's runs, newest first, as `{items, next_cursor}`. Filters: `query`, `version`, `status` (`success` or `error`), `key`, `since` (inclusive) and `until` (exclusive), each a date or a time; `limit` (default 100) and `cursor`. One query's runs alone: `GET /api/v1/queries/{name}/history`. |
| `GET /api/v1/audit` | Administrative changes, newest first: each entry's `timestamp`, `actor`, `action`, `target` and `changes`. Filters: `action`, `actor`, `target`, and `q` (matches the time, actor or target). The response also carries `total` (entries stored, before filtering), `actions` (every action in the log) and `retention` (how many entries the log keeps, `QUERYAPIGATE_AUDIT_LOG_LIMIT`). |

### Collections and the example APIs

| Method and path | What it does |
|---|---|
| `GET /api/v1/collections` | Every collection by `name`, with its `queries` and the `keys` and `roles` whose grants reach it; `uncollected` lists the queries in none. File a query under one with `PATCH /api/v1/queries/{name}`. |
| `PATCH /api/v1/collections/{name}` | Rename it: `{"name": "new"}`. Grants follow, and nobody's access narrows part-way. Into an existing collection only with `"merge": true` (409 `collection_exists` otherwise), which also finishes a rename that was interrupted. |
| `GET /api/v1/collections/{name}/postman` | The collection as a Postman Collection v2.1 file. It holds no key. |
| `GET /api/v1/examples` | Whether the [example APIs](EXAMPLES.md) are installed: `loaded`, or `partial` after an interrupted load, and what they are. |
| `POST /api/v1/examples` | Install them (idempotent; completes an interrupted load). The example keys' secrets are in the response, shown this once. 409 `examples_conflict`, changing nothing, if something that isn't an example holds one of their names. |
| `DELETE /api/v1/examples` | Remove exactly what is marked as an example. `keys_still_granted` names other keys granted an example collection, whose grant now reaches nothing. |

### The response cache

| Method and path | What it does |
|---|---|
| `GET /api/v1/cache/entries` | What is cached right now (in-process or Redis), soonest to expire first: each entry's `key`, `content_type`, `size_bytes`, `ttl_remaining_s` and `meta` (query `name`, `version`, `connection`, `format`). Never the body. |
| `DELETE /api/v1/cache/entries` | Evict everything: the next call to each query runs it for real. |
| `GET /api/v1/cache/entries/{key}` | The cached body, with its real content type - what a caller receives on a hit. 404 `cache_entry_not_found` once it has expired or been evicted. |
| `DELETE /api/v1/cache/entries/{key}` | Evict one entry early. |

Clearing the cache is housekeeping, not a change, so it is not audited.

### Settings and MCP

| Method and path | What it does |
|---|---|
| `GET /api/v1/settings` | The server's effective configuration, as sections (`id`, `title`, `description`, `rows`). Each row has the `env` variable, its displayed `value`, its `source` (`env` or `default`) and `env_value`, the raw value for a `.env` export. A secret's `value` only says whether it is configured, and its `env_value` is always null. Read-only: change settings in the environment and restart. |
| `GET /api/v1/mcp/status` | `{reachable, port}`: whether something answers on the MCP server's port. A plain TCP connect, run only when you ask. |
| `GET /api/v1/mcp/tools` | What `tools/list` returns for an unrestricted caller: each tool's `name`, `description`, `kind` (`ad-hoc` or `saved query`), `params` and `read_only`. Computed in this process, so it works whether or not `queryapigate mcp` is running. |

### Removed routes

Before `/api/v1` existed, QueryAPIGate was managed through unversioned routes. Each was deprecated when its v1
successor shipped, and all were removed together before 1.0, so they are not part of the 1.x contract:

| Removed | Use instead |
|---|---|
| `GET /list_files`, `GET /view_file_content`, `PATCH /save_sql_to_file`, `DELETE /saved_sql/{name}`, `PUT /saved_sql/{name}/collection`, `PUT /saved_sql/{name}/cache_ttl`, `GET /query_flow` | `/api/v1/queries` (the flow is `/api/v1/queries/{name}/versions/{n}/flow`) |
| `GET`/`PATCH /connections`, `DELETE /connections/{name}`, `POST /connections/test`, `POST /connections/databases` | `/api/v1/connections` |
| `/api_keys`, `/roles` | `/api/v1/api-keys`, `/api/v1/roles` |
| `GET /history`, `GET /audit_log` | `/api/v1/history`, `/api/v1/audit` |
| `GET /settings`, `/settings/mcp_status`, `/settings/mcp_tools` | `/api/v1/settings`, `/api/v1/mcp/status`, `/api/v1/mcp/tools` |
| `/cache/entries` | `/api/v1/cache/entries` |
| `/collections`, `/examples` | `/api/v1/collections`, `/api/v1/examples` |

`GET /connections/{name}/schema` and `GET /connections/{name}/table_ddl` stay: a scoped key may browse the schema of
a connection it is granted. A deprecated route carries a `Deprecation` header (RFC 9745) and a
`Link: <successor>; rel="successor-version"` header, and `/openapi.json` marks it, for as long as the changelog's
deprecation policy says it keeps working.

## Collections

A **collection** is one named group a saved query belongs to - at most one, unlike `tags`, which are free-form
and multi-valued. It exists for three things: browsing (the admin UI groups the API Repository list by
collection), access (a key can be granted a whole collection instead of a hand-maintained list of names) and
moving queries around as a unit ([export and import](#exporting-and-importing-a-collection)).

**Where it lives.** On the query's own file, as a top-level `"collection"` next to the version numbers
(`{"collection": "reporting", "1": {...}}`) - there is no separate registry to fall out of step with the queries.
Deleting a query removes its membership; a collection exists exactly while at least one query is in it; moving a
query is one atomic file write and **not** a new version, since the SQL did not change. A hand-edited value that
is not a valid name is read as *no collection*, so a grant can never reach a query through a spelling the API
would have refused.

**Names** are 1-63 characters of lowercase letters, digits, `.`, `_` and `-`, starting with a letter or digit.
Anything else - including `Reporting` - is rejected with `400`, never silently case-folded, so two spellings of
one collection cannot coexist.

**Moving a query.** `PATCH /api/v1/queries/<name>` (admin only) with `{"collection": "reporting"}`, or
`{"collection": null}` to take it out of any. A new query can also be created into one with `collection` in
[`POST /api/v1/queries`](#save-a-query). The audit log records exactly who is affected - a `move_query` entry with
the keys that gained and lost access - and the admin UI shows the same before you confirm a move:

~~~json
{"action": "move_query", "target": "top_rented_films",
 "changes": {"collection": {"from": null, "to": "reporting"}, "keys_gaining_access": ["acme-corp"],
             "keys_losing_access": []}}
~~~

**Listing.** `GET /api/v1/collections` (admin only) returns every collection with its queries and the keys and roles
granted it, plus the queries in no collection. A collection that only a grant still names (its queries have all
moved away) is listed with `"queries": []`, so a grant that has gone inert is visible instead of hiding:

~~~json
{"items": [{"name": "reporting", "queries": ["active_rentals", "top_rented_films"], "keys": ["acme-corp"],
            "roles": ["partner"]}],
 "uncollected": ["loose_query"]}
~~~

`GET /api/v1/queries` and `GET /catalog` also carry each query's `collection`.

### Granting access to a collection

A key (or role) takes a `collections` list of names:

~~~json
{"name": "acme-corp", "connections": [], "collections": ["reporting"]}
~~~

It reaches every query **currently** in those collections, on any connection, and works like `queries`: additive
(it only ever adds reach), read-only, and never ad-hoc SQL. Deliberately:

- **No `"*"` wildcard.** "Every query in any collection" would widen itself with each new collection.
- **No write access through a collection.** A write grant stays spelled out per query in `queries`.
- **Names must exist when granted** (`400`, listing the real ones - a typo would otherwise be a grant that
  reaches nothing and looks exactly like a working one). Names a key already holds are exempt, so re-saving a key
  whose collection has since emptied still works.
- **The grant is live**, unlike a role, which is copied once. That is the point - nobody maintains a list - and
  its cost, since filing a query into a collection changes what every key granted it can run. So every move is
  written to the [audit log](#audit-log) with the keys that gained or lost access, and `GET /api/v1/collections`
  shows who reaches each collection *before* you move anything.

A role's `collections` are copied onto a key at creation like every other role field.

`/openapi.json`, `/catalog` and the run path all decide reachability through one function, so a collection grant
is honoured (or not) identically by all three - a test compares them across every kind of grant.

### Renaming a collection

`PATCH /api/v1/collections/<name>` (admin only) with `{"name": "new-name"}` renames it, carrying every key and role grant
with it. It is ordered so that no key loses reach at any moment: grants are widened to hold both names, the
queries are re-filed, and only then is the old name dropped. If the process dies part-way, access is never
narrower than intended and running the same rename again completes it - which is why an already-existing target
requires `"merge": true` (409 `collection_exists` otherwise - a half-finished rename looks exactly like one). The
response lists the queries, keys and roles changed; the audit log records a `rename_collection` entry.

### Exporting a collection to Postman

`GET /api/v1/collections/<name>/postman` (admin only) downloads the collection as a **Postman Collection v2.1** file,
ready for Postman's *Import*; the CLI does the same with `queryapigate collection export <name> --format postman
[--base-url https://api.example.com] [--out file]`, and the admin UI has a **Postman** button on each collection's
header. Each query becomes a folder-free list of `GET {{baseUrl}}/q/<name>` requests:

- **Parameters** are the query string, named and documented from the query's own rules (type, `enum`, bounds,
  description). A required parameter is on, with an example that satisfies its rules (its default, the first `enum`
  value, the lower bound, `true`, ...); an optional one is present but switched off, so enabling it is one click.
  `format`, `page` and `page_size` are always offered, off.
- A `pattern` cannot be turned into an example, so that parameter is left empty and its description says to fill
  it in. A query with no default connection gets a `connection_name` entry to complete.
- **No credential is ever written.** Authentication is a collection-level `X-API-Key` header from the `{{apiKey}}`
  variable, which is empty: set it (and check `{{baseUrl}}`, which defaults to the server the file came from, or
  `http://127.0.0.1:5000` from the CLI) after importing.
- It is a **snapshot of each query's latest version** - export again after queries or their parameters change. It
  is a file rather than a live link because Postman cannot send an `X-API-Key` header when importing from a URL,
  and `/openapi.json` shows an anonymous caller none of the saved queries. Export only: there is no import from
  Postman, since that would mean guessing the SQL - use the [bundle](#exporting-and-importing-a-collection) to move
  queries between servers.

### Exporting and importing a collection

~~~sh
queryapigate collection export reporting --out reporting.json      # or stdout without --out
queryapigate collection import reporting.json --dry-run
queryapigate collection import reporting.json --on-conflict skip
~~~

A bundle (`"format": "queryapigate-collection"`, `"format_version": 1`) holds the **latest version of each
query's definition** - SQL, description, tags, declared parameters, default connection name, `cache_ttl` - and
nothing else: no execution history, no API keys or roles, no connection details (a connection is referenced by
name and must exist where you import; missing ones are reported as a warning).

Import is held to exactly the rules saving a query is, and is all-or-nothing up to the point of writing: the
whole bundle is validated and checked for conflicts first, so a problem in the tenth query stops the import
before the first is written. An unknown field in an entry is an error rather than silently dropped. When a name
already exists, `--on-conflict` decides: `fail` (default - import nothing), `skip` (leave the existing query
alone), or `new-version` (add the bundle's as the next version). `new-version` never moves a query out of a
*different* collection - that would silently change which keys can reach it - so that case is a conflict too.
Each query is written atomically and audited as a `save_query` with `"source": "import"`; if the process dies
part-way, re-run with `--on-conflict skip` to finish. `--collection NAME` imports into a different collection
than the bundle's. Like `queryapigate export`, it runs in-process against `QUERYAPIGATE_HOME` with the same reach
as the admin key - no server needs to be running.

## Example APIs

Four worked scenarios (reporting, dashboard, export, partner) ship with the package and can be loaded into a home
folder and removed again - see [Example APIs](EXAMPLES.md) for the walkthrough. Over HTTP (admin only):

- `GET /api/v1/examples` - `{"loaded": true, "partial": false, "connection": "examples", "queries": [...],
  "roles": [...], "keys": [...], "collections": [...]}`. `partial` means an interrupted load: some of it is installed.
- `POST /api/v1/examples` - install them. Idempotent; the response lists what was `added`, with the example keys'
  secrets (`key_secrets`), shown this once. `409 examples_conflict` and nothing changed if a query, role, connection
  or file that is *not* an example already holds one of their names.
- `DELETE /api/v1/examples` - remove exactly what is marked `example`, and report any keys still granted an example
  collection (`keys_still_granted`), whose grant now reaches nothing.

The same as `queryapigate examples load|unload|status`, and `QUERYAPIGATE_LOAD_EXAMPLES=yes` loads them at startup
(a problem there - a name conflict, a read-only home - is a logged warning, never a startup failure; a malformed value
is rejected). Loading records one `load_examples` audit entry, not one per query; removal records `unload_examples`.
`GET /api/v1/queries` marks each query with `"example": true/false`.

## Connections

`GET /api/v1/connections` lists connections, and `GET /api/v1/connections/{name}` shows one; stored passwords are
shown as `********` (values that are `${ENV_VAR}` references are shown as written). Each entry also carries a live
`usage` object -
`{"queries": ..., "errors": ..., "rows": ..., "avg_duration_ms": ...}` - aggregated from the same in-process
counters `/metrics` renders (see [Observability](#observability)), so the admin UI's Connections tab can show
how much a connection has actually been used without a separate Prometheus query. `avg_duration_ms` is
`null` until at least one query has run against it since this process started; the numbers reset on restart
- they are a live view of *this process*, not a durable history (see a saved query's own
[`execution_history`](#run-a-saved-query) for that).

`POST /api/v1/connections` adds one; `PATCH /api/v1/connections/{name}` changes some of its fields and keeps the
rest (`null` removes an optional one). Sending the `********` mask back, or leaving `password` out, keeps the stored
password. Driver settings (`sslmode`, `jdbc_url`, ...) go in `options`.

~~~json
{
    "name": "reporting",
    "db": "postgres",
    "host": "db.internal",
    "port": 5432,
    "database": "reports",
    "user": "readonly",
    "password": "${REPORTING_PASSWORD}",
    "active": true
}
~~~

`DELETE /api/v1/connections/reporting` with `{"reason": "..."}` removes one (the reason is required, and kept in the
audit log). `POST /api/v1/connections/test` tries a connection without saving it. See [DATABASE_CONNECTION_CONFIGURATION.md](DATABASE_CONNECTION_CONFIGURATION.md)
for the connection fields.

A `${VAR}` password reference is expanded from the environment at connection time and never written to disk as
plaintext. A literal password is encrypted at rest when `QUERYAPIGATE_SECRET_KEY` is set (see
[Encryption at rest](#encryption-at-rest-for-connection-passwords) below); without that variable it is stored
as given, in `queryapigate.db` on disk, not just masked in API responses. The server logs a startup
warning naming any connection whose password is still a literal string with no protection at all, so a
deployment that hasn't adopted either convention finds out - nothing blocks it, this is a nudge, not an
enforcement.

### Encryption at rest for connection passwords

Set `QUERYAPIGATE_SECRET_KEY` to a Fernet key (`python -c "from cryptography.fernet import Fernet;
print(Fernet.generate_key().decode())"`) and every literal connection password - existing ones immediately at
startup, new ones the moment they're saved - is encrypted before it touches disk, decrypted only in memory at
the instant a connection is actually opened. Needs the `cryptography` package
(`pip install "queryapigate[encryption]"`, included in `[all]`); a clear error at startup names the missing package
if `QUERYAPIGATE_SECRET_KEY` is set without it. A `${VAR}` reference is untouched either way - it was never a
secret stored in the file to begin with.

An encrypted password is masked the same as a literal one in `GET /api/v1/connections/{name}` and the audit log
(`********`) - the stored ciphertext itself is never returned to a client. Losing or rotating
`QUERYAPIGATE_SECRET_KEY` fails clearly rather than quietly: a connection whose password can't be decrypted
returns a `500` naming the problem, and the server logs a startup warning if encrypted passwords exist on
disk but no key is configured to read them - the same "fail closed, say why" precedent `expires_at` and
`rate_limit` already follow for a key's own malformed data. There is no way to recover an encrypted password
without the key that encrypted it; keep `QUERYAPIGATE_SECRET_KEY` itself somewhere safe, outside
`QUERYAPIGATE_HOME` and outside version control, the same way you would any other credential.

`GET /connections/reporting/schema` lists its tables and views for self-service query writing:

~~~json
{
    "tables": [
        {"name": "orders", "type": "table", "columns": [
            {"name": "id", "type": "integer", "nullable": false, "position": 1},
            {"name": "customer_id", "type": "integer", "nullable": false, "position": 2}
        ]}
    ],
    "truncated": false
}
~~~

`truncated` is `true` only if the connection has more than 5000 columns across all its tables and views combined,
in which case the list was cut off.

## Administrators and admin roles

The people and pipelines that manage QueryAPIGate are **administrators** ([ADR 0003](adr/0003-named-administrators.md)),
each with a role, signing in with their own **admin tokens** - `qagadm_...`, sent as `X-API-Key` like any key, stored
only as hashes, each with an optional expiry. An administrator may hold several (a laptop, a CI job) and revoke each
alone; deactivating the administrator stops them all.

| Role | May |
|---|---|
| `owner` | everything, including administrators |
| `admin` | everything but administrators: connections, API keys and roles, saved queries, cache, examples, settings |
| `developer` | saved queries and collections; read connections, history, the audit log and alerts; run SQL - but no API keys, no connection changes, and no moves that give a key or role new reach |
| `auditor` | read only: queries, connections, API keys and roles, the audit log, history, alerts, instances, settings. No SQL at all |

Owners, admins and developers run SQL and saved queries on every connection, as `QUERYAPIGATE_API_KEY` does. Each
`/api/v1` operation needs one capability (`queries.write`, `access.read`, ...; `GET /api/v1/me` lists yours); a role
without it gets `403 role_forbidden`, naming the `capability`. Any other caller - a scoped key, a signed-in app user -
gets `403 admin_only`.

**The first owner** is created on the server, with no running server or key needed:

~~~bash
queryapigate admins create alice --role owner --email alice@corp.com --label laptop
# Created administrator alice (owner).
# Admin token tok_3f2a9c1b04de for alice (expires 2027-01-03) - store it now, it cannot be shown again:
#   qagadm_...
~~~

`--expires YYYY-MM-DD|never` (default: 90 days). `queryapigate admins token NAME` issues another (for one who lost
theirs); `queryapigate admins list` lists them. Or create administrators in the Console, or through
`/api/v1/administrators`, with the shared key.

**`QUERYAPIGATE_API_KEY` is the break-glass key.** It keeps working, as an owner, so nothing breaks on upgrade. Once
an active owner exists, each use is logged as a warning and recorded in the audit log (`break_glass_used`, at most
every 10 minutes per process), which raises the `break_glass_used` alert for 24 hours. When everyone signs in with
their own token, remove it from the environment: **authentication stays required while any administrator exists**,
and the last active owner can't be demoted, deactivated or removed while the shared key isn't set (`409 last_owner`).

Administrator names and API key names never overlap (`409 name_taken`), so a name in the logs, `/metrics` and run
history means one caller; `admin` and `cli` are reserved.

## Authentication and permissions

`QUERYAPIGATE_API_KEY`, if set, is a full-access **admin** key - the break-glass key once named administrators
exist (above) - unrestricted, exactly as before this section
existed. Scoped keys are additive, managed through `/api/v1/api-keys` (admin only), and can only run queries: a
list of connection names they may use (or every connection), and whether they may write at all. A scoped
key can never do more than the server-wide settings already allow - `allow_writes` on a key can only narrow
`QUERYAPIGATE_ALLOW_WRITES`, never widen it - and can never manage connections, saved queries or other API keys;
only the admin key can. Creating your first scoped key turns on authentication for the whole server
immediately, even without `QUERYAPIGATE_API_KEY` set - and since only the admin key can manage the server, doing
that without also setting `QUERYAPIGATE_API_KEY` locks configuration changes out until you do (the server logs a
warning at startup in that state).

A key's secret is never stored - only its SHA-256 hash, in `queryapigate.db` (`QUERYAPIGATE_HOME`). It is generated
by the server and returned exactly once, when the key is created; there is no way to recover it afterwards,
only to revoke it (`DELETE /api/v1/api-keys/<name>`) and create a new one.

`POST /api/v1/api-keys` creates a key:

~~~json
{"name": "reporting", "connections": ["reporting-db"], "allow_writes": false}
~~~

The `201` response is the key's grants and state plus its `secret` (`sk_...`), shown this once and sent with
`Cache-Control: no-store`.

`connections` may be omitted (or `"*"`) for every connection, or an empty list to block all of them.
`GET /api/v1/api-keys` lists keys (name, grants, active, expiry, created_at, `created_from_role`, `last_used_at` -
never the hash or secret). Each entry also carries a live `usage` object -
`{"queries": ..., "errors": ..., "rows": ...}` - the same live, in-process aggregation `GET /api/v1/connections`
carries (see above), giving a key's activity alongside its grants; unlike a connection's, a key's `usage`
never includes `avg_duration_ms` - the underlying latency histograms aren't split by key, to keep `/metrics`'
bucketed output from growing with the number of keys (see [Observability](#observability)).
`PATCH /api/v1/api-keys/reporting` changes any grant, `expires_at` or `active` (`false`
revokes it immediately) without rotating the secret. `DELETE /api/v1/api-keys/reporting` removes it outright.
Creating several keys with the same grants repeatedly? See [Permission roles](#permission-roles-templates)
below for a reusable template - `POST /api/v1/api-keys` with `"role": "<name>"` instead of these fields.

### Per-saved-query access (external clients)

`connections` grants a key everything on a connection - every saved query on it, plus ad-hoc SQL if
`allow_writes` and the server allow it. That fits an internal caller, but not an external one who should
only ever reach a specific, curated list of saved queries and nothing else on the connection behind them.

A key's `queries` grant covers that case: a list of saved-query names it may run **regardless of
`connections`**, independent of and additive with whatever `connections` already allows - never a narrower
version of it. A key can have `connections: []` (no connection access at all) and still run every query
named in `queries`, but it can never reach ad-hoc SQL through this grant, since `queries` only ever
authorizes the specific named saved query, not the connection behind it - and only on **that query's own
connection**: a request that names a different one with `?connection_name=` (or that supplies one for a query with
no default) is `403` unless the key also holds a real grant on that connection. This applies equally to a
[collection grant](#granting-access-to-a-collection).

~~~json
{"name": "acme-corp", "connections": [], "queries": ["monthly_revenue", "active_users"], "allow_writes": false}
~~~

`queries` may also be `"*"` for every saved query by name (still never ad-hoc SQL) - a middle tier between a
single-connection key and a full-access one, for a caller that should see the whole curated catalogue but
never write raw SQL. Omitted (or `[]`) grants nothing extra beyond `connections`, unchanged from before this
field existed. `/openapi.json` and `/docs` reflect a key's actual reach: a `queries`-scoped key sees only
its own approved queries in the catalogue, not the full internal list.

To grant a whole group instead of naming each query, see [Granting access to a collection](#granting-access-to-a-collection).

### Per-query write curation

A `queries` entry can be an object instead of a plain name, adding write access to that one query
specifically - on top of, never instead of, whatever `allow_writes` already grants:

~~~json
{"name": "partner", "connections": [], "allow_writes": false,
 "queries": ["read_orders", {"name": "submit_order", "allow_writes": true}]}
~~~

Here `partner` can run `read_orders` read-only and `submit_order` (a write) - a single curated write
endpoint - with no blanket write access, no connection access, and no ad-hoc SQL of any kind. A plain string
entry stays exactly what it always was: read access only. `"*"` can never carry write access, by design - a
key wanting write access to a specific query must enumerate its `queries` list explicitly rather than hiding
a write grant behind a wildcard picked for unrelated read access. Still subject to the usual ceiling: never
wider than server-wide `QUERYAPIGATE_ALLOW_WRITES`, and the query's SQL still has to pass the normal guard (a
single statement, actually a write, and - if the key has [`allowed_write_ops`](#write-operation-granularity)
- one of the permitted keywords).

### Key expiry

A key can carry an optional `expires_at` (`YYYY-MM-DD`) for time-boxed access - a trial integration, a
partner engagement with a known end date - that stops authenticating on its own once the date passes,
without anyone having to remember to come back and revoke it:

~~~json
{"name": "trial-partner", "connections": ["reporting-db"], "expires_at": "2026-12-31"}
~~~

Valid through the *end* of that date (23:59:59), not from its start. Checked live on every request, the
same way `active` already is - there is no background sweep, so nothing to schedule or fail silently. Omit
it (or leave it unset) for a key that never expires; `PATCH /api/v1/api-keys/<name>` with `{"expires_at": null}`
clears an existing expiry without rotating the secret, and `PATCH` without the field at all leaves whatever
expiry (or lack of one) the key already had untouched.

### Last used

`GET /api/v1/api-keys` reports `last_used_at` for a key once it has authenticated at least one request - useful
for noticing a stale key nobody has called in months (a candidate to revoke) or confirming a newly-issued
one actually got wired up on the other end. Updated at most once a minute per key regardless of how often
it's actually used, so a busy key doesn't turn every request into a disk write - read it as "roughly how
recently," not an exact timestamp. A key that has never been used has no `last_used_at` field at all.

### Per-key rate limiting

`QUERYAPIGATE_RATE_LIMIT` (see [Rate limiting and CORS](#rate-limiting-and-cors)) applies server-wide, by client
IP, shared by every caller. Hand scoped keys to several external clients and they all draw from the same
budget - one noisy integration can exhaust it for everyone else. A key's own `rate_limit` gives it an
individual quota instead:

~~~json
{"name": "acme-corp", "connections": [], "queries": ["monthly_revenue"], "rate_limit": "100/minute"}
~~~

Same `N/period` grammar as `QUERYAPIGATE_RATE_LIMIT` (`second`, `minute`, `hour` or `day`). Checked **in
addition to** the server-wide limit, never instead of it - a key can never use its own quota to exceed the
ceiling every caller already sits under, and a per-key limit still applies even when
`QUERYAPIGATE_RATE_LIMIT` is unset entirely, since throttling one specific external caller is a reasonable ask
on its own. Omitted (or `null`) means no limit of this key's own - `PATCH /api/v1/api-keys/<name>` with an
explicit `{"rate_limit": null}` clears an existing one, the same pattern `expires_at` uses.

### IP allowlisting

A key can also be pinned to `allowed_ips`, a list of IP addresses or CIDR ranges (IPv4 or IPv6, mixed
freely) it may authenticate from - real defense in depth for a key handed to an external party with known,
stable infrastructure, since even a leaked key then only works from an expected address:

~~~json
{"name": "trial-partner", "connections": ["reporting-db"], "allowed_ips": ["203.0.113.5", "198.51.100.0/24"]}
~~~

Checked against the same client address `QUERYAPIGATE_TRUST_PROXY`/`ProxyFix` already establish as trustworthy
for [rate limiting](#rate-limiting-and-cors), not re-derived here - set `QUERYAPIGATE_TRUST_PROXY` correctly
behind a reverse proxy, or every caller looks like the proxy's own address. This restricts *who* may use a
key at all, independent of [per-key rate limiting](#per-key-rate-limiting) above, which restricts *how
much* a caller who is already allowed may do. A request from an address outside the list fails exactly like
a wrong key (`401`), not a distinct error - a caller learns nothing about *why* a key didn't work. Omitted
(or `null`) means no restriction - the admin key is never restricted by this at all. `PATCH
/api/v1/api-keys/<name>` with an explicit `{"allowed_ips": null}` clears an existing restriction, the same pattern
`expires_at` and `rate_limit` use.

### Write operation granularity

A key with `allow_writes` on can be narrowed further with `allowed_write_ops`, a list of the specific SQL
statement keywords it may actually perform - `INSERT` but not `DELETE`/`DROP`, for example - rather than
every write keyword being equally permitted once writes are on at all:

~~~json
{"name": "ingest-bot", "connections": ["events-db"], "allow_writes": true, "allowed_write_ops": ["insert"]}
~~~

Only ever narrows write access, never widens it, and never restricts a read-only statement - a key with no
`allow_writes` still can't write regardless of this list. Checked in `sqltools.validate_sql()` against the
statement's own leading keyword (case-insensitive); a rejected statement gets `403` naming the operations
the key *is* permitted. Omitted (or `null`) means every write keyword is equally permitted, exactly today's
behaviour. `PATCH /api/v1/api-keys/<name>` with an explicit `{"allowed_write_ops": null}` clears an existing
restriction, the same pattern `expires_at`/`rate_limit`/`allowed_ips` use.

### Table access restrictions

A key can be narrowed to a specific set of tables it may query, `allowed_tables` - unlike
`allowed_write_ops`, this restricts *every* statement, read or write, since a table a caller shouldn't see is
forbidden regardless of what's being done to it:

~~~json
{"name": "reporting-narrow", "connections": ["reporting-db"], "allowed_tables": ["orders", "customers"]}
~~~

Needs the `sqlglot` package to parse SQL (`pip install "queryapigate[flow]"` - the same optional extra the
Access tab's "Query flow" diagram already uses; a query is rejected with `500` if it's missing while a key
has `allowed_tables` set, rather than silently letting the query through unchecked). Checked against every
table a statement actually touches - joins, subqueries and CTEs are all resolved correctly (a CTE's own name
is never mistaken for a real table), including the target table of a bare `DELETE`/`UPDATE`/`INSERT`. Only
supported for `mysql`, `postgres`, `clickhouse`, `sqlite` and `duckdb`
connections, the dialects QueryAPIGate can actually parse for this - **a table-restricted key used against an
`h2`, `jdbc` or `mongo` connection is refused on every query, with a `403` naming the unsupported connection
type, never silently left unrestricted.** Table names are matched case-insensitively and bare (not
schema-qualified), the same simplification `allowed_write_ops`'s keyword list already makes; a table-valued
function (e.g. ClickHouse's `numbers(10)`) touches no real table, so this can't meaningfully restrict one.
The key's view of the schema follows the same list: `GET /connections/<name>/schema` and MCP's `list_tables` leave
other tables out (a foreign key pointing at one shows as `null`), and `table_ddl` answers a forbidden table with the
same `404` as a table that doesn't exist.

Omitted (or `null`) means no restriction, exactly today's behaviour. `PATCH /api/v1/api-keys/<name>` with an
explicit `{"allowed_tables": null}` clears an existing restriction, the same pattern the other grant fields
use.

### Permission roles (templates)

Creating several keys with the same shape of grants - the same connections, the same curated queries, the
same rate limit - means repeating that shape by hand each time. A named role, managed through `/api/v1/roles`
(admin only, stored separately from keys), is a reusable *template* for exactly that: `connections`,
`allow_writes`, `queries`, `collections`, `rate_limit`, `allowed_ips`, `allowed_write_ops` and
`allowed_tables`, the same fields a key itself carries (deliberately excluding `expires_at`, which is
inherently per-key, not something a shared template should dictate).

~~~json
{"name": "reporting", "connections": ["reporting-db"], "allow_writes": false, "rate_limit": "200/hour"}
~~~

`POST /api/v1/api-keys` with `"role": "reporting"` instead of specifying grants directly copies that role's fields
onto the new key **once, at creation time**:

~~~json
{"name": "acme-corp", "role": "reporting"}
~~~

This is a template, not a live link - a key created from a role is a fully independent copy from that moment
on. `authenticate()` reads only the key's own stored entry on every request; the role is never consulted
again. **Editing or deleting a role afterward has no effect whatsoever on a key already created from it** -
there is no blast radius to updating a role once keys already exist from it, and no dangling reference to
worry about when deleting one. A key still records which role (if any) it was created from, in
`created_from_role` - purely informational, visible in `GET /api/v1/api-keys`, never consulted by any permission
check.

`role` cannot be combined with any explicit grant field (`connections`, `allow_writes`, `queries`,
`collections`, `rate_limit`, `allowed_ips`, `allowed_write_ops` or `allowed_tables`) in the same
`POST /api/v1/api-keys` request - that combination is rejected with `400`, naming the conflicting fields. Create the
key from the role, then `PATCH` it afterward to customize it away from the template. `expires_at` is the one
field that *can* still be set alongside `role`, since it's per-key by nature rather than part of the shared
template.

`GET /api/v1/roles` lists roles; `PATCH /api/v1/roles/<name>` updates one (the same explicit-null-to-clear convention as
`PATCH /api/v1/api-keys/<name>` for `rate_limit`, `allowed_ips`, `allowed_write_ops` and `allowed_tables`);
`DELETE /api/v1/roles/<name>` removes it - again, with zero effect on any key already created from it.

### Signed-in users (JWT)

An API key identifies an application. For an app whose *users* each sign in - a mobile app, a customer portal -
QueryAPIGate can accept the token your app already has instead, so every user calls the API as themselves and
nobody has to issue them keys:

~~~http
GET /q/my_orders
Authorization: Bearer eyJhbGciOiJSUzI1NiIs...
~~~

Turn it on with **one** way to verify tokens, and a role saying what signed-in users may do:

| Variable | Meaning |
|----------|---------|
| `QUERYAPIGATE_JWT_JWKS_URL` | Your identity provider's signing keys (Auth0, Cognito, Firebase, Keycloak, Azure AD, ...): `https://.../.well-known/jwks.json`. Default algorithm RS256. Requires `QUERYAPIGATE_JWT_ISSUER` and `QUERYAPIGATE_JWT_AUDIENCE`. |
| `QUERYAPIGATE_JWT_SECRET` | Or: a shared secret, for tokens your own backend signs (HS256 by default; at least 32 bytes). |
| `QUERYAPIGATE_JWT_ISSUER` | The `iss` every token must carry. |
| `QUERYAPIGATE_JWT_AUDIENCE` | The `aud` value(s) accepted - this API's identifier at your provider (comma-separated for several). |
| `QUERYAPIGATE_JWT_ALGORITHMS` | Signature algorithms accepted, if not the default. |
| `QUERYAPIGATE_JWT_ROLE` | The [role](#permission-roles-templates) every signed-in user gets. |
| `QUERYAPIGATE_JWT_ROLE_CLAIM` | Or: a claim naming each user's role (a string or a list - the first existing role wins; dotted paths such as `app_metadata.roles` work). Falls back to `QUERYAPIGATE_JWT_ROLE`. |
| `QUERYAPIGATE_JWT_USER_CLAIM` | The claim identifying the user (default `sub`). |

Needs `pip install "queryapigate[jwt]"` (included in the Docker image). Settings that would make checking unsafe
stop startup: both or neither of a secret and a JWKS URL, a JWKS URL without issuer and audience (a provider
signs tokens for every app it serves) or over plain `http://`, a short secret, mixing HMAC and public-key
algorithms (the classic algorithm-confusion attack), or no role.

**What is checked.** The signature; `exp` (required) and `nbf`/`iat`, with 30 seconds of leeway for clock skew;
`iss` and `aud` when set; and only the configured algorithms - never `none`. A failure is a plain `401`, exactly
like a wrong API key. Signing keys are fetched from the JWKS URL once and cached, and fetched again only for a key
id not seen before - a provider rotating its keys just works.

**What a user may do** is their role's grants - connections, queries, collections, writes, tables, `allowed_ips`,
and `rate_limit`, counted per user. Unlike a key created from a role, which copies the role once, signed-in users
use the role *live*: editing or deleting it applies on their next request. A token whose role doesn't exist is
refused. When JWT is on, the server never runs in [open-access mode](#authentication-and-permissions): a request
with neither a key nor a token is refused even if no API key is configured.

**Who they are.** The caller's name is `jwt:<user claim>` - in logs, [run history](#run-history) (filter with
`GET /api/v1/history?key=jwt:alice`) and [live events](#live-events-server-sent-events), so each user's event stream
carries only their own runs. (`/metrics` counts all signed-in users under one `jwt` label, so a large user base
can't multiply its series.) A request carrying `X-API-Key` is judged on that key alone.

**Their own rows, guaranteed.** A parameter with `from_claim` takes its value from the verified token, so a query
can only ever see the caller's data - there is nothing in the request to change:

~~~json
{"filename": "my_orders",
 "sql_query": "SELECT * FROM orders WHERE customer_id = :customer_id ORDER BY created_at DESC",
 "query_parameters": {"customer_id": {"type": "str", "from_claim": "sub"}}}
~~~

- A signed-in user who sends `customer_id` anyway gets `400`; a token without the claim gets `403`.
- The admin key has no token and supplies it like any other parameter (for testing). Any other API key - and any
  MCP client using one - gets `403`: it has no token to take the value from.
- It is left out of `/openapi.json`, `/catalog`, Postman exports and MCP tool schemas: callers never send it.

A JWT can't be revoked before it expires, so keep token lifetimes short (minutes, refreshed by your app) - as
identity providers do by default. Disabling a user's access at once means taking the query or the role away.

## Observability

Every response carries `X-Request-Id` (12 hex characters by default); log lines written while handling that request
carry the same ID and the name of the API key that made it (`admin` for `QUERYAPIGATE_API_KEY`, a scoped key's
own name, or `-` when no key is configured at all), so a request - and who made it - can be traced through
the logs even under concurrent traffic. A caller can supply its own ID by sending `X-Request-Id` - 1 to 64
characters of letters, digits, `.`, `_`, `:` and `-` (a UUID qualifies) - and that ID is then used everywhere
instead: the response header, the log lines and a saved query's run history, so a run can be tied to a trace in the
caller's own system. Anything else (too long, spaces, quotes, non-ASCII) is ignored rather than rejected, and the
response header shows the ID actually used. It is a correlation aid only - nothing authorises by it, and two
requests may share one if the caller sends the same value twice. Browsers can send and read it cross-origin
(`X-Request-Id` is in the CORS allowed and exposed headers). Plain text by default; `QUERYAPIGATE_JSON_LOGS=1` switches to one JSON
object per line (`time`, `level`, `logger`, `request_id`, `key`, `message`). In JSON mode, several log lines
also carry extra structured fields alongside `message` rather than only inside it - the per-query line
(`connection`, `dialect`, `limit`, `offset`, `timeout`, `sql_hash`), the streaming-start line (`connection`,
`dialect`, `sql_hash`), the slow-query warning (`connection`, `dialect`, `duration_ms`), and the per-request
access log line (`method`, `path`, `status`, `duration_ms`, and `serialization_ms` when the response went
through the paged JSON/CSV/TSV/XML/YAML/XLSX/Parquet formatter) - so a log aggregator can filter or aggregate on
those directly instead of parsing the message text. `sql_hash` is a full SHA-256 hex digest of the SQL
text, logged *alongside* the full text (never instead of it) - useful for spotting "did this same query run
elsewhere/before" without a log aggregator having to store or search the SQL itself.

A query that takes at least `QUERYAPIGATE_SLOW_QUERY_THRESHOLD` seconds (default 1; `0` disables it) is logged
as a `WARNING` with the connection, dialect and elapsed time.

A saved query's `execution_history` entries (see [Save a query](#save-a-query)) also carry `request_id`,
`key_name` and `serialization_ms`, so a slow or failed run visible in the admin UI's History tab can be
traced back to the exact structured log line (and caller) that produced it, and so response-formatting time
can be told apart from `duration_ms` (query execution time) - useful for XLSX or other large-page exports,
where encoding cost can rival query time but was previously invisible, folded into "whatever's left over"
between total request latency and query latency.

`GET /metrics` (always public, like `/health`) serves [Prometheus text exposition
format](https://prometheus.io/docs/instrumenting/exposition_formats/):

- `queryapigate_requests_total` - HTTP requests by method, endpoint, status and the calling key's name.
  `queryapigate_request_duration_seconds` - the same latency, by method and endpoint only (not by key, to keep
  the bucketed output from growing with the number of keys).
- `queryapigate_queries_total` - SQL queries by connection, dialect, status (`success`/`error`) and the calling
  key's name. `queryapigate_query_duration_seconds` - the same latency, by connection and dialect only.
- `queryapigate_rows_returned_total` - total rows actually returned, by connection, dialect and the calling
  key's name: the trimmed page for a paged query, or however many rows made it out of a streaming export
  before it finished or failed partway through (a partial count on failure is still counted - that data
  already left the server).
- `queryapigate_active_queries` - a gauge of SQL queries currently executing right now, paged or mid-stream. For
  a streaming export this stays incremented for as long as the client keeps reading, not just for the
  initial query dispatch, since the underlying connection stays checked out the whole time.
- `queryapigate_serialization_duration_seconds` - response body serialization latency (JSON/CSV/TSV/XML/YAML/
  XLSX encoding), by output format, for paged responses only - streaming formats row by row as it goes, so
  there's no equivalent single span to measure there.
- `queryapigate_pool_idle_connections` - idle pooled database connections currently held.
- `queryapigate_rate_limit_rejections_total` - requests rejected by the rate limiter.
- `queryapigate_rate_limit_fallbacks_total` - rate-limit checks counted in this process instead of Redis, because
  Redis failed (see [Rate limiting and CORS](#rate-limiting-and-cors)).
- `queryapigate_cache_hits_total` / `queryapigate_cache_misses_total` - responses served from, or missed in,
  the `cache_ttl` response cache (in-process by default, Redis-backed when `QUERYAPIGATE_REDIS_URL` is set -
  see `/api/v1/settings`). Counted from the same `X-Cache: HIT`/`MISS` header a cacheable response already carries.
- `queryapigate_cache_entries` - responses currently held in the response cache.

Metrics are kept in memory for this one process - the standard Prometheus model: with several instances, scrape each
one and aggregate in queries (the bundled dashboard sums them). That's also why the image runs one gunicorn worker per
container: a scraper reaching a container with several workers would get one of them at random. Scale out with more
containers ([Scaling out](DEPLOYMENT.md#scaling-out)).

### Seeing it: a built-in view, or a real dashboard

The admin UI's **Metrics** tab reads `/metrics` itself and renders it as stat tiles, a couple of bar charts
(requests by status, queries by connection) and a per-connection table (queries, errors, average latency,
rows) - a zero-setup live view for a deployment with no Prometheus/Grafana stack in front of it at all.
It's deliberately a *snapshot*, not a dashboard: the numbers are this process's own totals since it started,
with no history and no trends, the same limits [above](#observability) already describe. It never needs an
API key - `/metrics` is public - and reading it again just re-fetches the current numbers; there's no
polling or auto-refresh.

For real history, trends and alerting, scrape `/metrics` with Prometheus and import
[`documentation/grafana-dashboard.json`](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/documentation/grafana-dashboard.json)
into Grafana (*Dashboards → New → Import*, then upload the file or paste its contents) - it builds on exactly
the metric names listed above, with panels for request/query rate and latency (p50/p95/p99), error rate,
rows returned, active queries, pool occupancy and rate-limit rejections. Each QueryAPIGate process needs its own
scrape target, and the dashboard sums across them.

## Live events (Server-Sent Events)

> **Experimental** - may change in any minor release, always noted in the changelog
> ([what that means](../CHANGELOG.md#versioning-and-compatibility)): event ids and the payload change when the dedicated event log ships
> ([ADR 0002](adr/0002-event-ids.md)).

Every recorded run - of a saved query, or of ad-hoc SQL - can be streamed to clients as it happens - a mobile app showing a user their own requests
completing, a dashboard, the admin UI's Home tab. There are two ways to serve that stream, with the same format:

| | `GET /events` on the main server | `queryapigate events` |
|---|---|---|
| For | the admin UI, a few clients | apps and phones - many clients |
| Open streams | `QUERYAPIGATE_EVENTS_MAX_STREAMS` (default 4) - each holds a request thread | thousands per process (`QUERYAPIGATE_EVENTS_MAX_CONNECTIONS`, default 10,000) |
| Sees runs from | this process only | every worker and instance sharing the store |
| Event ids and resume (`Last-Event-ID`) | no | yes |
| Delay | immediate | up to `QUERYAPIGATE_HISTORY_FLUSH_INTERVAL` (default 1 s), plus `QUERYAPIGATE_EVENTS_POLL_INTERVAL` on SQLite |

**Each key gets its own view, not a shared firehose.** Either way, a real API key is required (unless the server
runs with no keys at all). The admin key sees every run; any other key sees only the runs made with that same
key - its own personal activity feed:

```
GET /events
X-API-Key: <a scoped key's own secret>

id: 4812
data: {"type": "execution", "filename": "monthly_revenue", "version": 3, "connection_name": "warehouse",
       "entry": {"executed_at": "2026-09-30 12:00:00", "status": "success", "rows": 42,
                 "duration_ms": 118.4, "key_name": "mobile-alice", "request_id": "a1b2c3d4e5f6"}}
```

An [ad-hoc run](#ad-hoc-runs) arrives as `"type": "adhoc_execution"`, with `connection_name` and no `filename`
or `version`; its `entry` carries the SQL as `QUERYAPIGATE_HISTORY_ADHOC_SQL` keeps it, and `transport`.

**Event ids** are opaque integers that only increase: store the last one and send it back, but don't read
anything else into it. Today an id is the run's history row id; a future dedicated event log keeps the sequence
going rather than restarting it, so an id held across that upgrade still resumes
([ADR 0002](adr/0002-event-ids.md)). Live events are expected to be marked experimental for 1.0 until then.

A connection with nothing to say sends a `: keepalive` comment line every 15 seconds so a proxy or client library
doesn't time it out as idle - not an event, safe to ignore.

**Send the key in `X-API-Key` - or a signed-in user's token as `Authorization: Bearer` - never in the URL** (where
it would leak into access logs and browser history). `queryapigate events` closes a stream once its token expires
(checked every minute): reconnect with a fresh token and `Last-Event-ID`. A browser's `EventSource` can't set
request headers, so in a browser read the stream with `fetch()` and a streamed
response body; every mobile platform's own HTTP client can read a streamed response the same way.

### `queryapigate events`

A separate process, run beside `queryapigate serve` against the same `QUERYAPIGATE_HOME` or
`QUERYAPIGATE_DATABASE_URL`, serving `GET /events` (and `GET /health`) on its own port:

~~~bash
queryapigate events --host 0.0.0.0 --port 5002
~~~

Put it behind the same reverse proxy as the main server, routing `/events` to it - see
[DEPLOYMENT.md](DEPLOYMENT.md#9-live-events-for-many-clients-optional) - so clients keep one base URL.

- **Every instance's runs.** Events come from run history in the store, not from one process's memory: each run
  recorded there is an event, and its history id is the event's `id:`. On PostgreSQL the server is notified the
  moment a batch is written; on SQLite it checks every `QUERYAPIGATE_EVENTS_POLL_INTERVAL` seconds (default 1). So
  delivery takes up to one `QUERYAPIGATE_HISTORY_FLUSH_INTERVAL` - lower that (e.g. `0.2`) for snappier events.
- **Resume.** Reconnect with a `Last-Event-ID` header (or `?last_event_id=` if the client can't set headers) and
  the stream first replays every run after that id that the key may see - up to 1,000 - then continues live,
  with nothing missed or repeated in between. `EventSource` sends the header by itself; with `fetch()`, remember
  the last `id:` you read and send it when you reconnect.
- **What isn't an event.** Runs history doesn't keep: those sampled out by `QUERYAPIGATE_HISTORY_SAMPLE_RATE` or
  dropped under back-pressure. A replay can only reach runs still in history - a version keeps its newest
  `QUERYAPIGATE_HISTORY_LIMIT` runs unless `QUERYAPIGATE_HISTORY_RETENTION_DAYS` is set.
- **Limits.** A stream whose key is revoked, deactivated or expires is closed within about a minute (at most 75
  seconds: the key is checked again every 60, at the stream's next write). A client that stops
  reading is disconnected once 1,000 events are waiting for it - it reconnects with `Last-Event-ID` and catches up.
  Beyond `QUERYAPIGATE_EVENTS_MAX_CONNECTIONS` open streams, new ones get `503` with `Retry-After`;
  `QUERYAPIGATE_RATE_LIMIT` applies to connection attempts; `QUERYAPIGATE_CORS_ORIGINS` and
  `QUERYAPIGATE_TRUST_PROXY` mean the same as on the main server.
- **Scale.** Measured on a 4-core machine shared with PostgreSQL and the load generator: 9,000 concurrent streams
  in one process (about 170 MB), every event reaching all 9,000 within 0.75 s of the request, with the main
  server's own latency unaffected.

### `GET /events` on the main server

Immediate and needs no extra process, but each open stream occupies one of the main server's request threads for
as long as it is open. So at most `QUERYAPIGATE_EVENTS_MAX_STREAMS` (default 4, of the Docker image's 8 threads)
are held at once; beyond that it answers `503` with `Retry-After`, rather than leave no threads for requests
(`0` turns it off). It only sees runs handled by its own process, and has no event ids or resume. A client that
disconnects frees its slot at the next keepalive, within 15 seconds.

## Alerts

> **Experimental** - may change in any minor release, always noted in the changelog
> ([what that means](../CHANGELOG.md#versioning-and-compatibility)): new in 0.13, its checks, thresholds and alert shape may change as it is
> used.

`GET /api/v1/alerts` (admin only) lists what needs attention now, most severe first. The Console shows the same
list on its Alerts screen, as a count on the bell in its header, and in Home's System health. Every alert is a live
condition, worked out afresh on each request - nothing is stored, and an alert disappears by itself once its cause
does (a key extended, a connection fixed, a query sped up).

~~~json
{"items": [{"id": "key_expiring:partner", "severity": "warning", "kind": "key_expiring",
            "title": "API key 'partner' expires in 3 days",
            "detail": "On 2026-10-07. Calls with it will be refused from then on - extend it, or give its callers a new key first.",
            "since": "2026-10-07", "target": {"type": "key", "name": "partner"}}],
 "checked_at": "2026-10-04 12:47:00"}
~~~

`id` (kind and subject) stays the same while the condition holds, so a client can remember which alerts it has
already reported or dismissed. `target` names what to fix: a `key`, `query`, `connection`, or a `settings` section.

| Kind | Severity | When |
|------|----------|------|
| `open_server` | critical | No API key is configured: anyone can use and change the server |
| `connection_failing` | critical | A connection's last 3 runs in 24 hours all failed for a connection reason (`connection_failed`, `driver_missing`, ...), not bad SQL |
| `key_expired` | warning | An active key's `expires_at` has passed |
| `key_expiring` | warning | An active key expires within 7 days |
| `query_errors` | warning | At least `QUERYAPIGATE_ALERT_ERROR_RATE`% (default 20) of a saved query's last 50 runs in 7 days failed, with 10 runs or more |
| `query_timeouts` | warning | A saved query timed out (`query_timeout`) in the last 24 hours |
| `query_slow` | warning | A saved query's median successful run (of its last 50 in 7 days, 5 or more) is over `QUERYAPIGATE_SLOW_QUERY_THRESHOLD` - typically slow, not slow once |
| `key_rate_limited` | warning | A key's own `rate_limit` refused it 10 times or more in the last hour |
| `client_rate_limited` | warning | `QUERYAPIGATE_RATE_LIMIT` refused one client address 10 times or more in the last hour |
| `history_failed`, `history_dropped` | warning | Runs could not be recorded, or were dropped because the store could not keep up |
| `instances_not_shared` | warning | Several processes share this store, and some of them without Redis - their rate limits and cache are per process |
| `instances_versions_differ` | warning | Processes sharing this store run different versions - expected only during a rolling upgrade |
| `rate_limits_not_shared` | warning | Redis failed a rate-limit check in the last ten minutes, so this process is counting limits on its own |
| `key_unused` | info | An active key unused for `QUERYAPIGATE_ALERT_KEY_UNUSED_DAYS` (default 90) |

| Variable | Default | Effect |
|----------|---------|--------|
| `QUERYAPIGATE_ALERT_ERROR_RATE` | `20` | Percentage of a saved query's recent runs that may fail before `query_errors`. `0` turns it off. |
| `QUERYAPIGATE_ALERT_KEY_UNUSED_DAYS` | `90` | Days an active key may go unused before `key_unused`. `0` turns it off. |

The history checks read the newest 5,000 runs, so their cost doesn't grow with how much history is kept. Rate-limit
and history-write counts are this process's own, since it started (like `/metrics`): with several instances, each
reports its share. For production paging, alert on `/metrics` from Prometheus too - this list is for the people
running QueryAPIGate day to day, not a replacement for an on-call alerting system.

## Instances

`GET /api/v1/instances` (admin only) lists the processes using this store now - every `queryapigate serve` worker,
`mcp` and `events` process seen in the last 90 seconds - and what looks wrong about them:

~~~json
{"items": [{"id": "3f9c2a1b7d4e-4242", "host": "api-1", "pid": 4242, "role": "serve", "version": "0.15.0",
            "shared_limits": true, "started_at": "2026-10-04 09:00:12", "last_seen": "2026-10-04 12:47:03",
            "this": true}],
 "problems": []}
~~~

Each process records itself at startup and refreshes `last_seen` as it serves requests (at most every 30 seconds), so
the health checks a deployment already runs keep an idle one listed. `problems` holds `instances_not_shared` and
`instances_versions_differ`, the same as the alerts of those names; both are also logged at startup.

## Run history

Every saved-query run is recorded: when, against which connection, by which key, through which front door
(`transport`: `rest` or `mcp`), with what status, row count and duration. So is every [ad-hoc run](#ad-hoc-runs).
How that history is written and kept is configurable:

| Variable | Default | Effect |
|----------|---------|--------|
| `QUERYAPIGATE_HISTORY_LIMIT` | `50` | Runs kept per saved-query version, unless a retention period is set. |
| `QUERYAPIGATE_HISTORY_RETENTION_DAYS` | unset | Keep **every** run for this many days instead of a per-version count; a sweep every 10 minutes deletes older ones. Best with a [PostgreSQL metadata store](INSTALLATION_AND_SETUP.md#shared-metadata-store-postgresql). |
| `QUERYAPIGATE_HISTORY_SAMPLE_RATE` | `1` | Fraction of *successful* runs recorded, e.g. `0.1` for one in ten on a very busy query. Failed runs are always recorded. |
| `QUERYAPIGATE_HISTORY_FLUSH_INTERVAL` | `1` | Seconds between batched history writes. `0` writes each run inside its own request. |
| `QUERYAPIGATE_HISTORY_ADHOC_LIMIT` | `1000` | Ad-hoc runs kept in all, newest first, unless a retention period is set. |
| `QUERYAPIGATE_HISTORY_ADHOC_SQL` | `text` | What an ad-hoc run's entry keeps of its SQL: `text` (the first 4,000 characters, with `sql_truncated` when cut), `hash` (`sql_sha256` only - which statements repeat, never their text) or `none`. SQL can embed literal values; choose `hash` or `none` if those may be sensitive. |

**Batched writes.** A request never waits on its history entry: the run is queued in memory and a background
thread writes everything queued in one transaction every `QUERYAPIGATE_HISTORY_FLUSH_INTERVAL` seconds. Reads in
the same process always include its own queued runs (they are written first), so a history read right after a run
shows it; another worker or instance sees it within one interval. A normal shutdown writes what is queued; a
process killed outright loses at most one interval's worth. If the store can't keep up, at most 10,000 runs are
queued per process and newer ones are dropped rather than slowing requests - counted in `/metrics` as
`queryapigate_history_runs_total{outcome="dropped"}`, alongside `recorded`, `sampled_out` and `failed`, with
`queryapigate_history_pending` for the current queue.

### Ad-hoc runs

`POST /execute_sql`, `POST /execute_mongo` (including streamed exports) and MCP's `execute_sql` tool are recorded
too: the questions agents and people ask with no saved query behind them are the ones most worth being able to
look back on. An ad-hoc entry has `"kind": "adhoc"`, `query` and `version` `null`, and:

- the caller (`key_name`), `transport`, `connection_name`, status, rows, duration, and for a failure its `error`
  and [`code`](#errors) - as for a saved run;
- the SQL, as `QUERYAPIGATE_HISTORY_ADHOC_SQL` says;
- `params`: the names of its bound parameters, **never their values**.

Ad-hoc runs are sampled like saved ones (`QUERYAPIGATE_HISTORY_SAMPLE_RATE`; failures always kept) and kept to
`QUERYAPIGATE_HISTORY_ADHOC_LIMIT` in all, or the retention period if one is set. `queryapigate export` records its
runs as saved-query runs by `key_name` `cli`.

~~~json
{"kind": "adhoc", "query": null, "version": null, "executed_at": "2026-10-04 09:12:40", "status": "success",
 "rows": 12, "duration_ms": 9, "key_name": "agent", "transport": "mcp", "connection_name": "warehouse",
 "sql": "SELECT region, SUM(total) FROM orders WHERE placed_at >= :since GROUP BY region", "params": ["since"],
 "request_id": "4be1f07c9d22"}
~~~

### `GET /api/v1/history`

Pages through every stored run, saved and ad-hoc, newest first - across a retention period, for example. Admin
only (it shows every key's activity). `GET /api/v1/queries/{name}/history` is the same for one query.

| Parameter | Meaning |
|-----------|---------|
| `kind` | `saved` or `adhoc`. Each item says which it is. |
| `query`, `version` | Only this saved query (and version). |
| `status` | `success` or `error`. |
| `key` | Only runs made with this API key (`admin` for the admin key). |
| `since`, `until` | `YYYY-MM-DD` or `YYYY-MM-DD HH:MM:SS`, in the server's local time; `since` inclusive, `until` exclusive. |
| `limit` | Runs per page, 1-1000 (default 100). |
| `cursor` | The previous page's `next_cursor`. |

~~~bash
curl -H 'X-API-Key: admin-key' 'localhost:5000/api/v1/history?key=partner&status=error&since=2026-10-01'
~~~

~~~json
{"items": [{"query": "film_by_id", "version": 3, "executed_at": "2026-10-01 14:02:11", "status": "error",
              "error": "An error occurred while executing the SQL query", "key_name": "partner",
              "connection_name": "pg", "request_id": "9f2c41d07a1b"}],
 "next_cursor": "WyIyMDI2LTEwLTAxIDE0OjAyOjExIiwgNDgxMl0"}
~~~

`next_cursor` is `null` on the last page.

## Audit log

`GET /api/v1/audit` (admin only) is a durable record of administrative changes - distinct from
[Observability](#observability) above, which covers live request/query traffic, not configuration changes.
Every create, update or delete of an API key, role, connection or saved query appends one entry, newest
first (moving a query between [collections](#collections) is `move_query`, listing the keys that gained or lost
access; renaming one is `rename_collection`):

~~~json
{
  "items": [
    {"timestamp": "2026-09-24 10:03:11", "actor": "alice", "via": "token", "action": "update_key",
     "target": "acme-corp", "changes": {"allow_writes": {"from": false, "to": true}}},
    {"timestamp": "2026-09-24 10:01:47", "actor": "admin", "via": "break-glass", "action": "create_connection",
     "target": "reporting",
     "changes": {"db": "postgres", "host": "db.internal", "password": "********", "active": true}}
  ],
  "total": 2, "actions": ["create_connection", "update_key"], "retention": 500
}
~~~

`actor` is the administrator's name (`admin` for the shared key); `via` says how they authenticated - `token`
(their own admin token), `break-glass` (`QUERYAPIGATE_API_KEY`), `open` (no authentication configured), `cli` (a
command on the server) or `startup`. Entries written before 0.16 have no `via`. Administrators and their tokens are
audited too: `create_admin`, `update_admin`, `delete_admin`, `issue_admin_token`, `revoke_admin_token` - never a
token's secret.

An update's `changes` is a diff of only the fields that actually changed (`{"field": {"from": ..., "to":
...}}`); a create or delete records a full snapshot of the entry instead, since there's no prior or
remaining state to diff against. A connection's `password` is never included as a value in either form -
masked as `********` in a snapshot (the same mask `GET /api/v1/connections/{name}` already uses) and reported only
as the literal string `"changed"` in a diff, so the audit log itself never becomes a second place a real password
leaks from. An API key's entry never includes its secret or hash, the same fields `GET /api/v1/api-keys` already
omits. Capped at 500 most recent entries by default; older ones roll off, the same way a saved query's
`execution_history` is capped per version. Set `QUERYAPIGATE_AUDIT_LOG_LIMIT` to raise or lower that cap for a
busier server or a longer compliance-driven retention window - validated at startup, so a malformed value
fails loudly rather than silently keeping the default. Unlike the streaming row cap, this one is always a
positive count: each audit event trims the table back down to the cap in the same transaction as its own
insert, so letting it grow without bound would mean an ever-larger table and index, not just more disk.

For retention a cap can never satisfy - keeping every entry indefinitely rather than a rolling window of
however many - set `QUERYAPIGATE_AUDIT_LOG_EXPORT_FILE` to a path; every entry is also appended there, one JSON
object per line, and that file is never capped or rewritten. The two writes are independent, so a problem
with one (the export path's directory missing, say) never blocks the other.

The admin UI's Audit Log tab filters this client-side over what it already fetched - an action dropdown
(populated from whatever actions actually appear) and a search box matching actor, target or timestamp -
and renders a snapshot's `changes` (a create/delete) with any unset field (`null`, `""`, `[]`) omitted rather
than always listing every field, so a key or connection with few grants set doesn't read as a long, mostly
empty list. A diff (an update) is unaffected by this - it never had unset fields in it to begin with, only
whatever actually changed.

## Rate limiting and CORS

Both are off unless the server enables them (`QUERYAPIGATE_RATE_LIMIT`, `QUERYAPIGATE_CORS_ORIGINS`).

- **Rate limit.** When enabled, every response carries `X-RateLimit-Limit` (the quota) and `X-RateLimit-Remaining`. A
  client over its limit receives **429** with a `Retry-After` header (seconds) and
  `{"error": "Rate limit exceeded", "code": "rate_limited", "retry_after": 12, "request_id": "..."}`. `/health` is never limited.
- **Per-key rate limit.** An API key can also carry its own `rate_limit` (same grammar, e.g. `"100/minute"` - see
  [Per-key rate limiting](#per-key-rate-limiting)), checked *in addition to* the server-wide limit above, never
  instead of it - a response carries both header pairs when both apply (`X-RateLimit-Limit`/`X-RateLimit-Remaining`
  for the server-wide one, `X-RateLimit-Key-Limit`/`X-RateLimit-Key-Remaining` for the key's own), and a rejection
  from the key's own limit reads `{"error": "Rate limit exceeded for this API key", ...}` - distinguishable from
  the server-wide rejection's plain `"Rate limit exceeded"`.
- **Shared across instances.** With `QUERYAPIGATE_REDIS_URL` set, both limits are counted in Redis - one budget per
  client address and per key across every instance and process. If Redis fails, each process counts on its own until
  it's reachable again (`queryapigate_rate_limit_fallbacks_total`, and the `rate_limits_not_shared` alert).
- **CORS.** For listed origins the server answers preflight (`OPTIONS`) requests and adds
  `Access-Control-Allow-Origin` to responses, exposing `X-Page`, `X-Page-Size`, `X-Has-More`, `X-RateLimit-*`
  (server-wide and per-key), `Retry-After`, `X-Request-Id`, `X-Cache`, `ETag`, `Deprecation` and `Link` to the page's
  JavaScript. Allowed methods are `GET, POST, PATCH, DELETE, OPTIONS`; allowed request headers are `Content-Type`,
  `X-API-Key`, `Authorization` and `X-Request-Id`. Credentials (cookies) are not used.

## Admin UI

The admin UI - the QueryAPIGate Console - is at `/console` ([ADR 0001](adr/0001-console-and-management-api.md)).
It covers the whole workflow: connections, the API Designer (a SQL editor with completion that knows your tables and
columns, a schema browser, Explain, parameterize, run and save), the API Repository (versions, drafts and publishing,
history, curl and CLI snippets, cache, metrics, who can reach each query), API keys and roles, the Access map, caching,
metrics, the audit log, settings and help.

It is a client of the [Management API](#management-api-v1) and the runtime routes only - it adds no server-side
logic of its own. Loading the page needs no API key; the requests it makes are gated exactly like any other client's,
so a scoped (non-admin) key sees "only the admin key" messages wherever it reaches past its grants. It shares its
API-key storage with `/docs` (the same browser-tab-only `sessionStorage` entry), so entering the key on one page covers
both. Interface preferences (theme, table density, default result format) are kept in the browser.

The Console is built with React and TypeScript and ships prebuilt in the wheel and the Docker image - running it needs
no Node.js. `/ui`, where an earlier, hand-written admin page used to be, redirects here.

## Interactive documentation

`/docs` (Swagger UI, backed by `/openapi.json`) documents the generic API and also lists **every saved query as its own
endpoint**, generated from its latest version: its parameters with types, defaults, ranges and descriptions, and
whether a connection must be named. The SQL text is never included.

The generic part is public. When the server sets `QUERYAPIGATE_API_KEY`, the saved-query part is only included for
requests that carry the key - paste it into the box at the top of `/docs` (kept in that browser tab only) or send
`X-API-Key` to `/openapi.json`.

Since `/openapi.json` is a standard OpenAPI 3.0 document, Postman and Insomnia can both import it directly by
URL (Postman: *Import → Link*) to get a ready-made collection of every endpoint, including saved queries once
you've supplied a key - no separate export step.

### The API catalogue

`GET /catalog` answers a different question than `/openapi.json`: not just *how* to call a saved query
(parameters, types, connection) but *under what terms* - whether its response can be cached, whether the
calling key specifically can write through it, and what rate limit governs the calling key itself. That
information already exists (`cache_ttl` on the saved query, a key's own `rate_limit`, per-query write
curation - see [Per-query write curation](#per-query-write-curation)) but was otherwise only visible on
admin-only screens a scoped key can never reach:

~~~json
{
  "queries": [
    {"name": "top_rented_films", "version": 1, "description": "Films ranked by number of rentals",
     "tags": ["reporting", "films"], "collection": "reporting", "connection_name": "rental_db",
     "parameters": {}, "cache_ttl": 60, "can_write": false}
  ],
  "caller": {
    "name": "acme-corp", "admin": false, "allow_writes": false, "allowed_write_ops": null,
    "allowed_tables": null, "rate_limit": "200/hour", "server_rate_limit": null
  }
}
~~~

`queries` is scoped exactly like `/openapi.json`'s saved-query list - a query this caller cannot reach
through `/q/<name>` is never listed here either, so the catalogue never shows a caller something it can't
actually use. `cache_ttl` is `null` when the query isn't cached; `can_write` reflects this specific caller
(a query with no per-query write curation for them still reads `false` even if their key has blanket
`allow_writes`, since blanket access is already visible on their own key). `caller.rate_limit` is this key's
own additional limit (`null` if it has none of its own); `caller.server_rate_limit` is `QUERYAPIGATE_RATE_LIMIT`,
checked in addition to it, never instead of it. Unlike `/openapi.json`, `/catalog` is never public - it
requires authentication like any other functional endpoint, since the whole point is answering "what can *I*
use," which needs a resolved caller to mean anything.

## Errors

Every error - from `/api/v1`, the runtime routes (`/q/<name>`, `/execute_sql`, `/catalog`, ...), `queryapigate
events` and MCP tool calls - has the same shape:

~~~json
{"error": "This API key may only query these tables: film. ...", "code": "table_not_allowed",
 "request_id": "9f2c41d07a1b"}
~~~

- **`code`** is stable: branch on it. It is part of the compatibility promise - an existing code is never renamed or
  given a different meaning, though new ones may be added (treat an unknown code by its HTTP status).
- **`error`** is for people. Its wording may change in any release.
- **`request_id`** is also the response's `X-Request-Id` and appears on the server's log lines for the request, so
  a support conversation can find them. (`queryapigate events` errors have no `request_id`.)
- Some errors add fields: `errors` (a map of parameter name to problem, for `param_invalid`, `param_required` and
  invalid query definitions), `retry_after` (seconds, for `rate_limited`, also sent as `Retry-After`), `timeout`
  (for `query_timeout`) and `detail`.
- **`detail`** carries the database's own message when a query fails - except for a saved query (`/q/<name>`)
  called with a scoped key, which gets only the generic error, since the database's text
  can reveal schema details to a caller who didn't write the SQL. The full message is always in the server log.
- **MCP:** a tool error is `isError: true` with the message as text, as before, and `{"error", "code"}` (plus
  `retry_after` when rate limited) in `structuredContent`.

An error with no more specific code gets the one for its status: `invalid_request` (400), `unauthorized` (401),
`forbidden` (403), `not_found` (404), `method_not_allowed` (405), `conflict` (409), `precondition_failed` (412),
`payload_too_large` (413), `unsupported_media_type` (415), `rate_limited` (429), `internal_error` (500),
`upstream_failed` (502), `unavailable` (503), `query_timeout` (504).

| Code | Status | Meaning |
|------|--------|---------|
| `invalid_body` | 400 | The request body isn't valid JSON, isn't an object, or has a field with a wrong value |
| `unknown_field` | 400 | The body has a field this route doesn't take |
| `invalid_name` | 400 | A query, key, role, collection or connection name with characters it can't have |
| `invalid_paging` | 400 | `page`/`page_size` not positive, or `page_size` over `QUERYAPIGATE_MAX_PAGE_SIZE` |
| `invalid_format` | 400 | An unsupported `format` |
| `invalid_timeout` | 400 | `timeout` isn't a positive number of seconds |
| `invalid_stream` | 400 | `stream=true` with a format it can't stream, or with `page`/`page_size` |
| `invalid_filter` | 400 | A bad history/audit filter (`status`, `kind`, `since`, `until`, `limit`, `cursor`) or Mongo filter |
| `invalid_bundle` | 400 | A collection bundle that isn't one, or is malformed |
| `sql_required` | 400 | No SQL given |
| `connection_required` | 400 | No connection named, and the saved query has none |
| `collection_required` | 400 | No collection named |
| `table_required` | 400 | `/table_ddl` without `table` |
| `multiple_statements` | 400 | More than one SQL statement |
| `param_required` | 400 | A required parameter (or placeholder) has no value; `errors` names each |
| `param_invalid` | 400 | A parameter's value breaks its rules; `errors` says how |
| `param_from_claim` | 400 | The call sent a parameter the query takes from the caller's sign-in token |
| `reason_required` | 400 | Deleting a connection without a `reason` |
| `stream_unsupported` | 400 | Streaming a Mongo query |
| `unsupported_database` | 400 | A database type QueryAPIGate doesn't support |
| `unsupported_operation` | 400 | The operation isn't available for this database type (listing databases, schema, DDL) |
| `wrong_connection_type` | 400 | A Mongo call on a SQL connection |
| `unauthorized` | 401 | Missing or wrong `X-API-Key` or bearer token |
| `admin_only` | 403 | Only an administrator may do this (manage the server, query or browse another database) |
| `role_forbidden` | 403 | The administrator's role can't do this; `capability` names what it needed, `role` the caller's role. For a move or merge that would widen access, `gaining` lists the keys and roles |
| `connection_forbidden` | 403 | This key may not use this connection |
| `connection_inactive` | 403 | The connection is switched off |
| `read_only` | 403 | A write statement where writes aren't allowed (always, over MCP) |
| `write_op_not_allowed` | 403 | A write this key's `allowed_write_ops` doesn't include |
| `table_not_allowed` | 403 | A table outside this key's `allowed_tables`, or a table function (`read_parquet(...)`) for such a key |
| `format_unavailable` | 500 | `format=parquet` on a server without DuckDB (`pip install "queryapigate[duckdb]"`) |
| `path_not_allowed` | 403 | A DuckDB connection was asked to read a file or URL outside its `allowed_paths` |
| `table_check_unsupported` | 403 | `allowed_tables` can't be enforced on this database type, so the query is refused |
| `table_check_failed` | 403 | The SQL couldn't be analysed to enforce `allowed_tables`, so it is refused |
| `mongo_operator_forbidden` | 403 | A Mongo operator that runs server-side JavaScript |
| `sign_in_required` | 403 | The query takes values from a sign-in token; call it with one |
| `claim_missing` | 403 | The sign-in token lacks a claim the query needs |
| `forbidden` | 403 | Any other refusal |
| `query_not_found` | 404 | No such saved query |
| `version_not_found` | 404 | No such version - or a draft, to a key that can't see drafts |
| `not_published` | 404 | The query has no published version |
| `connection_not_found` | 404 | No such connection |
| `key_not_found`, `role_not_found` | 404 | No such API key or role |
| `admin_not_found`, `token_not_found` | 404 | No such administrator, or no such token of theirs |
| `collection_not_found` | 404 | No such collection (or it is empty) |
| `cache_entry_not_found` | 404 | No such cached response |
| `table_not_found` | 404 | No such table on the connection |
| `database_file_not_found` | 404 | A SQLite/DuckDB connection's file doesn't exist |
| `query_exists`, `connection_exists`, `key_exists`, `role_exists`, `collection_exists` | 409 | The name is taken |
| `admin_exists` | 409 | An administrator by that name exists |
| `name_taken` | 409 | An administrator and an API key can't share a name |
| `last_owner` | 409 | The change would leave no active owner while `QUERYAPIGATE_API_KEY` isn't set |
| `examples_conflict` | 409 | Loading the examples would overwrite things that aren't examples |
| `precondition_failed` | 412 | `If-Match` names a version of the resource that is no longer current |
| `rate_limited` | 429 | Over `QUERYAPIGATE_RATE_LIMIT` or the key's own `rate_limit`; retry after `retry_after` seconds |
| `query_failed` | 500 | The database rejected the query (`detail` has why, where shown) |
| `driver_missing` | 500 | The database driver isn't installed on the server |
| `connection_misconfigured` | 400, 500 | The connection's settings are incomplete (no database file, a missing environment variable, JDBC fields) |
| `secret_key_required`, `password_undecryptable` | 500 | An encrypted connection password can't be decrypted: `QUERYAPIGATE_SECRET_KEY` is unset or has changed |
| `table_check_unavailable` | 500 | `allowed_tables` needs the `sqlglot` package, which isn't installed |
| `internal_error` | 500 | Anything unexpected; the log has the traceback under `request_id` |
| `connection_failed` | 502 | The database couldn't be reached |
| `too_many_streams` | 503 | Every live-event stream slot is taken; retry shortly |
| `query_timeout` | 504 | The query ran past its time limit and was cancelled |

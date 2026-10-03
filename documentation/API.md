# API reference

Base URL when running locally: `http://127.0.0.1:5000`. The same information is available interactively at `/docs`
and as an OpenAPI document at `/openapi.json`.

If the server has any key configured - `QUERYAPIGATE_API_KEY` or a scoped key created through `/api_keys` - send it
with every request as `X-API-Key: <key>` (`/health`, `/docs`, `/ui`, `/openapi.json` and `/metrics` are always
public; `/openapi.json`'s saved-query section still varies with who's asking). See
[Authentication and permissions](#authentication-and-permissions).

| Endpoint | Method | Purpose |
|----------|--------|---------|
| [`/execute_sql`](#execute-sql) | POST | Run ad-hoc SQL |
| [`/q/<name>`](#run-a-saved-query) | GET, POST | Run a saved query as an endpoint |
| [`/execute_sql_from_file`](#run-a-saved-query) | POST | Run a saved query by file path |
| [`/execute_sql_with_parameters_from_file`](#run-a-saved-query) | POST | Same as above (kept for compatibility) |
| [`/save_sql_to_file`](#save-a-query) | PATCH | Save a query / add a version |
| [`/list_files`](#list-saved-queries) | GET | List saved queries |
| [`/saved_sql/<name>`](#delete-a-saved-query) | DELETE | Delete a saved query or one version |
| [`/view_file_content`](#view-a-saved-query-file) | GET | Raw saved-query file |
| [`/saved_sql/<name>/collection`](#collections) | PUT | Move a saved query into a collection, or out of any |
| [`/saved_sql/<name>/cache_ttl`](#save-a-query) | PUT | Set or clear one version's cache_ttl in place |
| [`/collections`](#collections) | GET | Every collection, its queries, and the keys and roles that reach it |
| [`/collections/<name>`](#collections) | PATCH | Rename a collection, carrying every grant with it |
| [`/collections/<name>/postman`](#exporting-a-collection-to-postman) | GET | Download a collection as a Postman Collection file |
| [`/examples`](#example-apis) | GET, POST, DELETE | Status of / load / remove the bundled example APIs |
| [`/connections`](#connections) | GET, PATCH | List / add / update connections |
| [`/connections/<name>`](#connections) | DELETE | Delete a connection |
| [`/connections/<name>/schema`](#connections) | GET | List a connection's tables/views and their columns |
| [`/api_keys`](#authentication-and-permissions) | GET, POST | List / create scoped API keys |
| [`/api_keys/<name>`](#authentication-and-permissions) | PATCH, DELETE | Update / revoke a scoped API key |
| `/health` | GET | `{"status": "ok", "version": "..."}` |
| [`/metrics`](#observability) | GET | Prometheus text-format metrics |

## Common query parameters

These apply to every endpoint that returns rows.

| Parameter | Default | Description |
|-----------|---------|-------------|
| `format` | `json` | `json`, `ndjson`, `csv`, `tsv`, `xml`, `yaml` or `xlsx`. For the POST endpoints it may also be given in the JSON body. |
| `page` | `1` | 1-based page number. |
| `page_size` | `10` | Rows per page, at most `QUERYAPIGATE_MAX_PAGE_SIZE` (default 1000). |
| `timeout` | server limit | Seconds the query may run before it is cancelled with a 504. It can lower the server limit (`QUERYAPIGATE_QUERY_TIMEOUT`, default 30; `0` disables it) but never raise it. For the POST endpoints it may also be given in the JSON body. |

Any trailing `LIMIT`/`OFFSET` in the SQL is replaced by the requested page. Responses carry
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

- Only `format=csv`, `tsv` or `ndjson` are streamable (400 for `json`/`xml`/`yaml`/`xlsx` - those formats all need
  the whole document structure in memory to write correctly, so paging still applies to them normally).
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

`PATCH /save_sql_to_file`

~~~json
{
    "filename": "film_by_id",
    "sql_query": "SELECT * FROM film WHERE film_id = :id",
    "query_parameters": {"id": "int"},
    "connection_name": "examples",
    "author": "anantha",
    "description": "Look up a film",
    "tags": ["example"]
}
~~~

- `filename` may contain letters, digits, spaces, `.`, `_` and `-`. Saving to an existing name creates the next version.
- `query_parameters` declares the query's parameters and their [rules](#parameter-rules). The definitions are checked when you save (400 with an `errors` map if any is invalid), and every parameter declared must be used in `sql_query`.
- `connection_name` (optional) is the default connection when a run does not name one.
- `cache_ttl` (optional, seconds) caches a response - see [Response caching](#response-caching).
- `collection` (optional) files the query under a [collection](#collections). It belongs to the query, not to a version: omit it to keep the current one, send `null` to remove it.
- Response: `{"message": "...", "filename": "film_by_id", "uuid": "...", "version": 2}`.

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
- **Saving through `PATCH /save_sql_to_file`, `queryapigate collection import` or `examples load` publishes the
  new version at once,** which is exactly how those always behaved. Drafts are created through the Management API
  (`/api/v1`, ADR 0001), which also publishes, unpublishes and rolls back to any earlier version.
- **Deleting the published version** falls back to the newest version older than it, never to a draft. With none
  older, the query is left unpublished.
- `GET /list_files` shows each query's `published_version` (`null` when nothing is published).

Stores created before this (schema 3 and older) are upgraded on first start with every query published at its
newest version, so nothing a caller sees changes.

The older endpoints `POST /execute_sql_from_file` and `POST /execute_sql_with_parameters_from_file` do the same thing
with the query named in the body:

~~~json
{
    "filepath": "saved_sql/film_by_id.json",
    "connection_name": "examples",
    "placeholders": {"id": 7},
    "format": "csv"
}
~~~

`filepath` may be a bare name (`film_by_id`), a `saved_sql/`-qualified path, or an absolute path ending the
same way - accepted forms kept for compatibility with how a saved query used to be referenced as a real
file; every form resolves to the same saved query by name.

## Response caching

Opt-in, per saved query: set `cache_ttl` (seconds) when [saving it](#save-a-query), or turn it on/off or retune it
for an existing version in place with `PUT /saved_sql/<name>/cache_ttl` (admin only, `{"cache_ttl": <seconds>}`,
`0` or `null` turns it off; `?version=` targets a specific version, defaulting to the latest) - not a new version,
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

## List saved queries

`GET /list_files?sort_by=name&sort_order=desc` - `sort_by` is `name` (default) or `modified`, `sort_order` is `asc`
(default) or `desc`. Returns every query with its versions' metadata (not the SQL text) and its `published_version`.

## Delete a saved query

`DELETE /saved_sql/film_by_id` removes the whole query; `DELETE /saved_sql/film_by_id?version=2` removes one
version (the query goes with its last version). Deleting the published version publishes the newest older one, if
any - see [Drafts and publishing](#drafts-and-publishing).

## View a saved query's raw data

`GET /view_file_content?filename=film_by_id` returns `{"content": "<JSON text>"}` - the same shape a saved
query always looked like as a file (see [Query metadata](QUERY_METADATA_MANAGEMENT.md)), for the admin UI's
"Show raw file" view. Only an existing saved query's own name/reference can be read (see `filepath` above
for accepted forms).

## Management API (v1)

`/api/v1/...` is the versioned interface to QueryAPIGate's own configuration, built one resource at a time
([ADR 0001](adr/0001-console-and-management-api.md)). The Console at `/console` uses it, and so can scripts,
Terraform or GitOps tooling. It is admin-only, like the routes it replaces. Every request and response is described in
full in `/openapi.json`, under the "Management API v1" tag.

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
| `GET /api/v1/queries` | Every saved query's summary. Filters: `search` (name, description or tag), `collection`, `connection`, `status` (`published`, `unpublished`, `draft`). |
| `POST /api/v1/queries` | Create a query with its first version: `name`, `description`, `sql`, `connection_name`, `parameters`, `tags`, `cache_ttl`, `collection`. A draft unless `"publish": true`. 409 `query_exists` if the name is taken. |
| `GET /api/v1/queries/{name}` | The query with every version, each with its `status`: `published`, `draft` or `previous`. |
| `PATCH /api/v1/queries/{name}` | Change its `collection` (`null` removes it). |
| `DELETE /api/v1/queries/{name}` | Delete it and every version. |
| `POST /api/v1/queries/{name}/versions` | Add a version: a draft unless `"publish": true`. The published version keeps serving. |
| `GET /api/v1/queries/{name}/versions/{n}` | One version. |
| `PATCH /api/v1/queries/{name}/versions/{n}` | Change its `cache_ttl` in place. |
| `DELETE /api/v1/queries/{name}/versions/{n}` | Delete one version; see [Drafts and publishing](#drafts-and-publishing) for what happens to the published one. |
| `POST /api/v1/queries/{name}/publish` | `{"version": n}`: publish a draft, or an older version to roll back. |
| `POST /api/v1/queries/{name}/unpublish` | Stop serving it; every version is kept. |
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

### Connections (read side)

`GET /api/v1/connections` lists each connection's `name`, `db` and `active`. `GET /api/v1/connections/{name}/schema`
returns its tables and columns. The rest of the connection resource arrives with the Console's Connections screen.

### Deprecated routes

The saved-query management routes replaced by `/api/v1/queries` keep working, unchanged. Their responses carry
`Deprecation: true` and `Link: </api/v1/queries>; rel="successor-version"`, and `/openapi.json` marks them deprecated:
`GET /list_files`, `GET /view_file_content`, `PATCH /save_sql_to_file`, `DELETE /saved_sql/{name}`,
`PUT /saved_sql/{name}/collection`, `PUT /saved_sql/{name}/cache_ttl` and `GET /query_flow`. Runtime routes
(`/q/<name>`, `/execute_sql`, `/catalog`, `/events`, `/history`) are not deprecated.

## Collections

A **collection** is one named group a saved query belongs to - at most one, unlike `tags`, which are free-form
and multi-valued. It exists for three things: browsing (the admin UI groups the Saved Queries list by
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

**Moving a query.** `PUT /saved_sql/<name>/collection` (admin only) with `{"collection": "reporting"}`, or
`{"collection": null}` to take it out of any. A new query can also be created into one with `collection` in
[`PATCH /save_sql_to_file`](#save-a-query). The response says exactly who is affected:

~~~json
{"message": "'top_rented_films' moved", "filename": "top_rented_films", "from": null, "to": "reporting",
 "access": {"keys": {"gain": ["acme-corp"], "lose": []}, "roles": {"gain": ["partner"], "lose": []}}}
~~~

**Listing.** `GET /collections` (admin only) returns every collection with its queries and the keys and roles
granted it, plus the queries in no collection. A collection that only a grant still names (its queries have all
moved away) is listed with `"queries": []`, so a grant that has gone inert is visible instead of hiding:

~~~json
{"collections": {"reporting": {"queries": ["active_rentals", "top_rented_films"], "keys": ["acme-corp"], "roles": ["partner"]}},
 "uncollected": ["loose_query"]}
~~~

`GET /list_files` and `GET /catalog` also carry each query's `collection`.

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
  written to the [audit log](#audit-log) with the keys that gained or lost access, the response says the same,
  and `GET /collections` shows who reaches each collection *before* you move anything.

A role's `collections` are copied onto a key at creation like every other role field.

`/openapi.json`, `/catalog` and the run path all decide reachability through one function, so a collection grant
is honoured (or not) identically by all three - a test compares them across every kind of grant.

### Renaming a collection

`PATCH /collections/<name>` (admin only) with `{"name": "new-name"}` renames it, carrying every key and role grant
with it. It is ordered so that no key loses reach at any moment: grants are widened to hold both names, the
queries are re-filed, and only then is the old name dropped. If the process dies part-way, access is never
narrower than intended and running the same rename again completes it - which is why an already-existing target
requires `"merge": true` (a half-finished rename looks exactly like one). The response lists the queries, keys and
roles changed; the audit log records a `rename_collection` entry.

### Exporting a collection to Postman

`GET /collections/<name>/postman` (admin only) downloads the collection as a **Postman Collection v2.1** file,
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

- `GET /examples` - `{"loaded": true, "partial": false, "connection": "examples", "queries": [...], "roles": [...],
  "collections": [...]}`. `partial` means an interrupted load: some of it is installed.
- `POST /examples` - install them. Idempotent; the response lists what was added. `409` and nothing changed if a
  query, role, connection or file that is *not* an example already holds one of their names.
- `DELETE /examples` - remove exactly what is marked `example`, and report any keys still granted an example
  collection (`keys_still_granted`), whose grant now reaches nothing.

The same as `queryapigate examples load|unload|status`, and `QUERYAPIGATE_LOAD_EXAMPLES=yes` loads them at startup
(a problem there - a name conflict, a read-only home - is a logged warning, never a startup failure; a malformed value
is rejected). Loading records one `load_examples` audit entry, not one per query; removal records `unload_examples`.
`GET /list_files` marks each query with `"example": true/false`.

## Connections

`GET /connections` lists connections; stored passwords are shown as `********` (values that are `${ENV_VAR}`
references are shown as written). Each entry also carries a live `usage` object -
`{"queries": ..., "errors": ..., "rows": ..., "avg_duration_ms": ...}` - aggregated from the same in-process
counters `/metrics` renders (see [Observability](#observability)), so the admin UI's Connections tab can show
how much a connection has actually been used without a separate Prometheus query. `avg_duration_ms` is
`null` until at least one query has run against it since this process started; the numbers reset on restart
- they are a live view of *this process*, not a durable history (see a saved query's own
[`execution_history`](#run-a-saved-query) for that).

`PATCH /connections` adds or replaces connections. Sending the `********` mask back for an existing connection keeps
its stored password.

~~~json
{
    "connections": {
        "reporting": {
            "db": "postgres",
            "host": "db.internal",
            "port": 5432,
            "database": "reports",
            "user": "readonly",
            "password": "${REPORTING_PASSWORD}",
            "active": true
        }
    }
}
~~~

`DELETE /connections/reporting` removes one. See [DATABASE_CONNECTION_CONFIGURATION.md](DATABASE_CONNECTION_CONFIGURATION.md)
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

An encrypted password is masked the same as a literal one in `GET /connections` and the audit log
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

## Authentication and permissions

`QUERYAPIGATE_API_KEY`, if set, is a full-access **admin** key - unrestricted, exactly as before this section
existed. Scoped keys are additive, managed through `/api_keys` (admin only), and can only run queries: a
list of connection names they may use (or every connection), and whether they may write at all. A scoped
key can never do more than the server-wide settings already allow - `allow_writes` on a key can only narrow
`QUERYAPIGATE_ALLOW_WRITES`, never widen it - and can never manage connections, saved queries or other API keys;
only the admin key can. Creating your first scoped key turns on authentication for the whole server
immediately, even without `QUERYAPIGATE_API_KEY` set - and since only the admin key can manage the server, doing
that without also setting `QUERYAPIGATE_API_KEY` locks configuration changes out until you do (the server logs a
warning at startup in that state).

A key's secret is never stored - only its SHA-256 hash, in `queryapigate.db` (`QUERYAPIGATE_HOME`). It is generated
by the server and returned exactly once, when the key is created; there is no way to recover it afterwards,
only to revoke it (`DELETE /api_keys/<name>`) and create a new one.

`POST /api_keys` creates a key:

~~~json
{"name": "reporting", "connections": ["reporting-db"], "allow_writes": false}
~~~

~~~json
{"name": "reporting", "key": "sk_...", "message": "Store this key now - it can't be shown again."}
~~~

`connections` may be omitted (or `"*"`) for every connection, or an empty list to block all of them.
`GET /api_keys` lists keys (name, connections, allow_writes, active, created_at, `created_from_role` - never
the hash or secret). Each entry also carries a live `usage` object -
`{"queries": ..., "errors": ..., "rows": ...}` - the same live, in-process aggregation `GET /connections`
carries (see above), giving a key's activity alongside its grants; unlike a connection's, a key's `usage`
never includes `avg_duration_ms` - the underlying latency histograms aren't split by key, to keep `/metrics`'
bucketed output from growing with the number of keys (see [Observability](#observability)).
`PATCH /api_keys/reporting` changes `connections`, `allow_writes` or `active` (`false`
revokes it immediately) without rotating the secret. `DELETE /api_keys/reporting` removes it outright.
Creating several keys with the same grants repeatedly? See [Permission roles](#permission-roles-templates)
below for a reusable template - `POST /api_keys` with `"role": "<name>"` instead of these fields.

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
it (or leave it unset) for a key that never expires; `PATCH /api_keys/<name>` with `{"expires_at": null}`
clears an existing expiry without rotating the secret, and `PATCH` without the field at all leaves whatever
expiry (or lack of one) the key already had untouched.

### Last used

`GET /api_keys` reports `last_used_at` for a key once it has authenticated at least one request - useful
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
on its own. Omitted (or `null`) means no limit of this key's own - `PATCH /api_keys/<name>` with an
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
/api_keys/<name>` with an explicit `{"allowed_ips": null}` clears an existing restriction, the same pattern
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
behaviour. `PATCH /api_keys/<name>` with an explicit `{"allowed_write_ops": null}` clears an existing
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

Omitted (or `null`) means no restriction, exactly today's behaviour. `PATCH /api_keys/<name>` with an
explicit `{"allowed_tables": null}` clears an existing restriction, the same pattern the other grant fields
use.

### Permission roles (templates)

Creating several keys with the same shape of grants - the same connections, the same curated queries, the
same rate limit - means repeating that shape by hand each time. A named role, managed through `/roles`
(admin only, stored separately from keys), is a reusable *template* for exactly that: `connections`,
`allow_writes`, `queries`, `collections`, `rate_limit`, `allowed_ips`, `allowed_write_ops` and
`allowed_tables`, the same fields a key itself carries (deliberately excluding `expires_at`, which is
inherently per-key, not something a shared template should dictate).

~~~json
{"name": "reporting", "connections": ["reporting-db"], "allow_writes": false, "rate_limit": "200/hour"}
~~~

`POST /api_keys` with `"role": "reporting"` instead of specifying grants directly copies that role's fields
onto the new key **once, at creation time**:

~~~json
{"name": "acme-corp", "role": "reporting"}
~~~

This is a template, not a live link - a key created from a role is a fully independent copy from that moment
on. `authenticate()` reads only the key's own stored entry on every request; the role is never consulted
again. **Editing or deleting a role afterward has no effect whatsoever on a key already created from it** -
there is no blast radius to updating a role once keys already exist from it, and no dangling reference to
worry about when deleting one. A key still records which role (if any) it was created from, in
`created_from_role` - purely informational, visible in `GET /api_keys`, never consulted by any permission
check.

`role` cannot be combined with any explicit grant field (`connections`, `allow_writes`, `queries`,
`collections`, `rate_limit`, `allowed_ips`, `allowed_write_ops` or `allowed_tables`) in the same
`POST /api_keys` request - that combination is rejected with `400`, naming the conflicting fields. Create the
key from the role, then `PATCH` it afterward to customize it away from the template. `expires_at` is the one
field that *can* still be set alongside `role`, since it's per-key by nature rather than part of the shared
template.

`GET /roles` lists roles; `PATCH /roles/<name>` updates one (the same explicit-null-to-clear convention as
`PATCH /api_keys/<name>` for `rate_limit`, `allowed_ips`, `allowed_write_ops` and `allowed_tables`);
`DELETE /roles/<name>` removes it - again, with zero effect on any key already created from it.

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
`GET /history?key=jwt:alice`) and [live events](#live-events-server-sent-events), so each user's event stream
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
through the paged JSON/CSV/TSV/XML/YAML/XLSX formatter) - so a log aggregator can filter or aggregate on
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
- `queryapigate_cache_hits_total` / `queryapigate_cache_misses_total` - responses served from, or missed in,
  the `cache_ttl` response cache (in-process by default, Redis-backed when `QUERYAPIGATE_REDIS_URL` is set -
  see `/settings`). Counted from the same `X-Cache: HIT`/`MISS` header a cacheable response already carries.
- `queryapigate_cache_entries` - responses currently held in the response cache.

Metrics are kept in memory for this one process. This is correct for the image this project ships (a
single gunicorn worker - see the comment next to `--workers 1` in the Dockerfile); running several worker
processes would need a shared backing store instead, which nothing here provides.

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
scrape target; the dashboard doesn't aggregate across instances, matching the in-memory, per-process nature
of the metrics themselves.

## Live events (Server-Sent Events)

Every saved-query run can be streamed to clients as it happens - a mobile app showing a user their own requests
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

## Run history

Every saved-query run is recorded: when, against which connection, by which key, with what status, row count
and duration. How that history is written and kept is configurable:

| Variable | Default | Effect |
|----------|---------|--------|
| `QUERYAPIGATE_HISTORY_LIMIT` | `50` | Runs kept per saved-query version, and how many of a version's newest runs `/list_files` and the admin UI show. |
| `QUERYAPIGATE_HISTORY_RETENTION_DAYS` | unset | Keep **every** run for this many days instead of a per-version count; a sweep every 10 minutes deletes older ones. Best with a [PostgreSQL metadata store](INSTALLATION_AND_SETUP.md#shared-metadata-store-postgresql). |
| `QUERYAPIGATE_HISTORY_SAMPLE_RATE` | `1` | Fraction of *successful* runs recorded, e.g. `0.1` for one in ten on a very busy query. Failed runs are always recorded. |
| `QUERYAPIGATE_HISTORY_FLUSH_INTERVAL` | `1` | Seconds between batched history writes. `0` writes each run inside its own request. |

**Batched writes.** A request never waits on its history entry: the run is queued in memory and a background
thread writes everything queued in one transaction every `QUERYAPIGATE_HISTORY_FLUSH_INTERVAL` seconds. Reads in
the same process always include its own queued runs (they are written first), so `/list_files` right after a run
shows it; another worker or instance sees it within one interval. A normal shutdown writes what is queued; a
process killed outright loses at most one interval's worth. If the store can't keep up, at most 10,000 runs are
queued per process and newer ones are dropped rather than slowing requests - counted in `/metrics` as
`queryapigate_history_runs_total{outcome="dropped"}`, alongside `recorded`, `sampled_out` and `failed`, with
`queryapigate_history_pending` for the current queue.

### `GET /history`

Pages through every stored run of every saved query, newest first - the way to look beyond the newest runs a
list shows, for example across a retention period. Admin only (it shows every key's activity).

| Parameter | Meaning |
|-----------|---------|
| `query`, `version` | Only this saved query (and version). |
| `status` | `success` or `error`. |
| `key` | Only runs made with this API key (`admin` for the admin key). |
| `since`, `until` | `YYYY-MM-DD` or `YYYY-MM-DD HH:MM:SS`, in the server's local time; `since` inclusive, `until` exclusive. |
| `limit` | Runs per page, 1-1000 (default 100). |
| `cursor` | The previous page's `next`. |

~~~bash
curl -H 'X-API-Key: admin-key' 'localhost:5000/history?key=partner&status=error&since=2026-10-01'
~~~

~~~json
{"entries": [{"query": "film_by_id", "version": 3, "executed_at": "2026-10-01 14:02:11", "status": "error",
              "error": "An error occurred while executing the SQL query", "key_name": "partner",
              "connection_name": "pg", "request_id": "9f2c41d07a1b"}],
 "next": "WyIyMDI2LTEwLTAxIDE0OjAyOjExIiwgNDgxMl0"}
~~~

`next` is `null` on the last page.

## Audit log

`GET /audit_log` (admin only) is a durable record of administrative changes - distinct from
[Observability](#observability) above, which covers live request/query traffic, not configuration changes.
Every create, update or delete of an API key, role, connection or saved query appends one entry, newest
first (moving a query between [collections](#collections) is `move_query`, listing the keys that gained or lost
access; renaming one is `rename_collection`):

~~~json
{
  "entries": [
    {"timestamp": "2026-09-24 10:03:11", "actor": "admin", "action": "update_key", "target": "acme-corp",
     "changes": {"allow_writes": {"from": false, "to": true}}},
    {"timestamp": "2026-09-24 10:01:47", "actor": "admin", "action": "create_connection", "target": "reporting",
     "changes": {"db": "postgres", "host": "db.internal", "password": "********", "active": true}}
  ]
}
~~~

An update's `changes` is a diff of only the fields that actually changed (`{"field": {"from": ..., "to":
...}}`); a create or delete records a full snapshot of the entry instead, since there's no prior or
remaining state to diff against. A connection's `password` is never included as a value in either form -
masked as `********` in a snapshot (the same mask `GET /connections` already uses) and reported only as the
literal string `"changed"` in a diff, so the audit log itself never becomes a second place a real password
leaks from. An API key's entry never includes its secret or hash, the same fields `GET /api_keys` already
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
  `{"error": "Rate limit exceeded", "retry_after": 12}`. `/health` is never limited.
- **Per-key rate limit.** An API key can also carry its own `rate_limit` (same grammar, e.g. `"100/minute"` - see
  [Per-key rate limiting](#per-key-rate-limiting)), checked *in addition to* the server-wide limit above, never
  instead of it - a response carries both header pairs when both apply (`X-RateLimit-Limit`/`X-RateLimit-Remaining`
  for the server-wide one, `X-RateLimit-Key-Limit`/`X-RateLimit-Key-Remaining` for the key's own), and a rejection
  from the key's own limit reads `{"error": "Rate limit exceeded for this API key", ...}` - distinguishable from
  the server-wide rejection's plain `"Rate limit exceeded"`.
- **CORS.** For listed origins the server answers preflight (`OPTIONS`) requests and adds
  `Access-Control-Allow-Origin` to responses, exposing `X-Page`, `X-Page-Size`, `X-Has-More`, `X-RateLimit-*` and
  `Retry-After` to the page's JavaScript. Allowed methods are `GET, POST, PATCH, DELETE, OPTIONS`; allowed request
  headers are `Content-Type` and `X-API-Key`. Credentials (cookies) are not used.

## Admin UI

`/ui` is a small, self-contained admin page (no build step, no external dependency) for managing
connections, saved queries and API keys, running ad-hoc SQL, reviewing the audit log, and viewing a live
snapshot of `/metrics` - a client of the API above, adding no server-side logic of its own. Loading the page
needs no API key; the requests it makes are gated exactly like any other client, so a scoped (non-admin) key
sees the same "only the admin key" message on the API Keys and Audit Log tabs as it does on Connections and
Saved Queries (the Metrics tab is the one exception - `/metrics` is public, so it works with no key at all).
It shares its API-key storage with `/docs` (the same browser-tab-only `sessionStorage` entry), so entering
the key on one page covers both.

Both the Connections and API Keys tabs have a "Usage" column showing each row's live activity (queries run,
failures, and - for a connection - average latency), sourced from the `usage` object both endpoints now
carry; a row with no activity since the server started reads "No activity yet" rather than showing zeros.

The API Keys tab creates, edits and revokes [scoped keys](#authentication-and-permissions): its "All
connections" checkbox toggles between the `"*"` wildcard and a specific set of connections, and a freshly
created key's secret is shown once, in place, with a copy button - the same one-time-only reveal the API
itself enforces, since the page never receives the secret again after that response. Its "Create from" field
lists any [permission roles](#permission-roles-templates) that exist; picking one dims the grant fields below
(they'll be copied from the role instead) and creates the key with `{"name": ..., "role": ...}` - only
`expires_at` stays editable alongside it.

The Roles tab manages roles themselves - the same connections/queries/write-access/rate-limit/allowed-IPs
form as the API Keys tab, minus `expires_at` and `active`, which don't apply to a template. Its "New key from
this" button jumps straight to the API Keys tab's "New API key" drawer with that role pre-selected.

The Saved Queries tab groups the list by [collection](#collections) once any exists (collapsible; filtering by a
collection's name shows its queries), and each group header shows how many keys are granted it, a **Postman**
download and a Rename button. **New collection** (next to New saved query) asks for a name and the queries to
file under it - a collection exists only while a query is in it, so it cannot be created empty - and previews
who gains or loses access first. A query's detail panel has a **Move…** button: it picks an existing collection, a new name, or none, and
shows - before anything changes - which keys will gain or lose access to the query and which roles will include
it, in the same terms the audit log records afterward. The New saved query drawer has an optional Collection
field that previews the same thing; changing an *existing* query's collection goes through Move… only, so there
is one path that shows the impact. The API Keys and Roles forms have a Collections field, and the tables show a
key's or role's collection grants as dashed `name/` tags beside its query names.

A saved query's History tab shows `execution_history` with a "Caller" and "Request ID" column alongside the
existing time/connection/rows/duration/error ones, and a status filter (all/success/failed) plus a search box
over connection, caller, request ID and error text - useful once a version has accumulated more than a
handful of runs and you're looking for the failures, or everything one particular key did. Filtering happens
client-side, over the run history the page already has, so it applies instantly with no extra request.

Any JSON result with at least one numeric column gets a "Chart" toggle next to "Copy as TSV" in the Run SQL
and saved-query Run tabs: a quick bar chart, off by default, of the columns on screen. It charts the
*current page only* - up to 50 rows of it - with a visible reminder that it isn't the full result, since a
chart of page 3 of 50 can look complete without being one. Label and value columns are pickable from
dropdowns (value defaults to the first all-numeric column, label to the first column that isn't); rendered
as inline SVG with no charting library, the same no-dependency approach the SQL editor's own syntax
highlighting already uses.

Both the Run SQL tab and the New saved query drawer have a **Schema** panel next to their SQL editor,
backed by [`GET /connections/<name>/schema`](#connections): it lists the selected connection's tables,
expands to show a table's columns (with type and nullability as a tooltip), and clicking a table or column
inserts its name at the cursor. It updates automatically when the connection changes, and a connection
whose schema isn't available (a `jdbc` connection, or one the caller isn't permitted to use) shows that
message in the panel itself rather than the page's error banner, since browsing the schema is optional, not
the action the user took.

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

Errors are returned as `{"error": "..."}`; failed queries also include `"detail"` with the database's message -
except a saved query (`/q/<name>`, `/execute_sql_from_file`) called with a scoped key, which gets only the generic
error, since the database's text can reveal schema details to a caller who didn't write the SQL. That full message
is always in the server log, under the response's `X-Request-Id`.

| Status | Meaning |
|--------|---------|
| 400 | Missing or invalid input (SQL, paging, format, filename, parameters, several statements). Parameter-rule violations add an `errors` map keyed by parameter name. |
| 401 | Missing or wrong `X-API-Key` (only when a key is configured) |
| 429 | Rate limit exceeded; wait `Retry-After` seconds |
| 403 | Inactive connection, write statement while writes are disabled, an invalid saved-query reference, a connection this API key isn't scoped to, or a management action a scoped (non-admin) key can't perform |
| 404 | Unknown connection, saved query, version or file |
| 500 | The database rejected the query or could not be reached |
| 504 | The query exceeded its time limit and was cancelled (the response includes `"timeout"`) |

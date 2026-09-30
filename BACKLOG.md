# Backlog

Work that has been discussed but not built, in priority order (highest first). This is a planning
document, not a promise - items move, merge, or drop as the project's needs become clearer. See
[CHANGELOG.md](CHANGELOG.md) for what has already shipped.

Each item has an **Impact** (why it matters for a production-grade, multi-user deployment) and, where the
sizing isn't obvious, a **Notes** line on approach and what it can reuse from the existing code.

## 1. Per-key API permissions

**Status: shipped** (first version - connections + write access; see below for what's still open).
`QUERYAPIGATE_API_KEY` stays a full-access admin key, unchanged. Scoped keys (`POST /api_keys`, admin only) are
each limited to a list of connection names (or every connection) and can be denied write access even when
the server otherwise allows it (`allow_writes` per key narrows `QUERYAPIGATE_ALLOW_WRITES`, never widens it).
Only the admin key manages connections, saved queries or other API keys - a scoped key can only run
queries. A key's secret is never stored, only its SHA-256 hash; the server generates and shows it once.
Verified: connection scoping, the write-access ceiling in both directions, admin-only management, the
open-server bootstrap path (and its startup warning when it would lock configuration out), key
update/revoke, and that the stored file never contains a plaintext secret.
Managing keys from `/ui` (create/edit/revoke, with the one-time secret reveal) shipped too - see #7.
**Per-saved-query access also shipped**: a key's `queries` grant (a list of saved-query names, or `"*"`) is
independent of and additive with `connections` - it needed to be additive, not a narrower filter inside
`connections`, once the actual use case turned out to be external clients with *no* connection access at
all, just a curated handful of named queries (`monthly_revenue` and similar), never ad-hoc SQL. Also closed
a pre-existing gap this surfaced: `/openapi.json`/`/docs` previously showed every saved query's name,
description and parameters to any authenticated key regardless of its own scope - the catalogue now
reflects what a key can actually reach. See [Per-saved-query access](documentation/API.md#per-saved-query-access-external-clients).
Further permission-granularity ideas that build on this - per-query write curation, per-key rate limits,
key expiry, IP allowlisting, per-key usage visibility - are their own items, #14-#18 below.

## 2. Observability: structured logs, request IDs, slow-query log, `/metrics`

**Status: shipped.** Every response carries `X-Request-Id`; log lines from handling that request carry the
same ID and the calling key's name (`admin`, a scoped key's own name, or `-`). `QUERYAPIGATE_JSON_LOGS=1`
switches to structured (one JSON object per line) logs. Queries at or past `QUERYAPIGATE_SLOW_QUERY_THRESHOLD`
(default 1s) are logged as warnings. `GET /metrics` serves Prometheus text-format request/query counts
(also by the calling key) and latency histograms (by endpoint/status and by connection/dialect, not by key,
so the number of keys never multiplies the bucketed output), idle pool occupancy, and rate-limit rejections
- in-process counters, correct for the single-worker image this project ships, documented as not carrying
over to a multi-process deployment.

## 3. Streaming exports

**Status: shipped.** `?stream=true` on `POST /execute_sql` and `GET`/`POST /q/<name>` (csv/tsv/ndjson),
always read-only. Went further than "stream from the cursor" alone: MySQL (unbuffered cursor), PostgreSQL
(named/server-side cursor) and ClickHouse (`execute_iter`) get real constant-memory streaming from the
database itself, not just a cap removed - verified against real servers with 1M rows and the actual server
process's memory sampled throughout the download (flat, not growing). SQLite/H2/generic `jdbc`/DuckDB still
bound *this project's* memory per batch even though their own engine/driver may hold more internally
(documented per-dialect from what was actually measured, not assumed - see `runners.py`). The "care around
query-timeout and connection-pooling" this item called out for was real: a PostgreSQL named cursor's
`execute()` turns out to be a `DECLARE CURSOR` that doesn't run the query at all, so column info and
`statement_timeout` cancellation are only available after the first fetch (the reverse of every other
driver); and the pool already holds a connection for a request's full duration, which for a streamed export
can now be a long time - documented as an operational consideration, not a new mechanism, since the
existing checkout/release design already handles it correctly once a generator's lifetime is tied to it.
See [Streaming exports](documentation/API.md#streaming-exports).

## 4. Generalise database support via JDBC

**Status: shipped.** `db: "jdbc"` reaches any JDBC-compliant database - Oracle, SQL Server, DB2, Snowflake
and others - by generalising `_H2`'s embedded-JVM pattern: `jar`, `driver_class` and `jdbc_url` replace
`host`/`port`/`database`, and pooling, the SQL guard, parameter binding and formats are unchanged. Two
things turned out not to be quite "only connect() differs", both handled and documented rather than
papered over: pagination (`LIMIT`/`OFFSET` is not portable SQL - a generic jdbc connection now paginates
client-side instead, correct for any vendor, just not server-side-optimal) and the JVM's classpath, which
is fixed at first startup - every jar from every currently-configured H2/jdbc connection is loaded on
whichever one connects first, so only adding a *new* jdbc connection to an already-running server needs a
restart. Two further limits are inherent to one JVM per process and documented, not fixed: no native
query-timeout cancellation, and no schema introspection (`GET /connections/<name>/schema` answers 400 for
this type). Verified end-to-end against a real H2 server reached through the *generic* driver (not the
dedicated `h2` one) - proving the driver itself, not just the idea of it, works against a real JDBC
database - plus unit tests for the classpath-union logic with distinct fake vendor jars.

## 5. Schema browser endpoint

**Status: shipped.** `GET /connections/<name>/schema` lists a connection's tables and views with their
columns (name, type, nullability, position), for every supported database. Verified against real MySQL,
PostgreSQL, ClickHouse, SQLite and H2 servers, including views and an empty (table-free) database.

## 6. Response caching

**Status: shipped.** Opt-in `cache_ttl` (seconds) per saved query; a hit answers with `X-Cache: HIT`, the
identical body, `ETag` and `Cache-Control: max-age=<cache_ttl>`, and honours `If-None-Match` with a bodyless
`304`. Interacts safely with both open questions from the original note: never used for a query whose SQL
is a write, no matter `cache_ttl` (would otherwise silently skip that write on every call after the first -
verified against a real MySQL server), and a cache hit is not recorded in `execution_history`, since
nothing ran. State lives in one process's memory (`app.extensions['queryapigate_cache']`, one per Flask app, like
the rate limiter) - a multi-process deployment would need a shared backing store instead.

## 7. Admin UI

**Status: shipped**, including every follow-up originally listed here - connections CRUD (with a proper
password-mask round trip), saved-query CRUD, per-version delete, an execution-history view per version, a
hand-rolled (no-dependency) syntax-highlighted SQL editor, an ad-hoc SQL runner with page-size presets and
Next/Previous, an API Keys tab (create/edit/revoke scoped keys, with the one-time secret reveal), and a
Schema panel next to both SQL editors (list tables, expand for columns, click to insert into the editor,
backed by #5's endpoint) - all as a thin client of the existing JSON API with no new backend logic. Verified
with real-browser (Playwright) tests: 39 assertions covering connections/queries/runner plus an XSS-payload
check (unauthenticated and with an API key set); a pass covering the API Keys tab end-to-end (create, secret
reveal, edit, revoke, the admin-only empty state a scoped key sees); and a pass covering the schema panel in
both mount points (expand/collapse, click-to-insert for tables and columns, connection-switch reactivity,
the unsupported-dialect message shown inline, the refresh button) - zero uncaught JS errors across all
three.

## 8. Client SDKs generated from the OpenAPI spec

**Status: shipped**, as documentation rather than a new pipeline - no generated code is committed, since
`/openapi.json` is already enough. README's "Client SDKs" section documents running it through
`openapi-generator-cli`, verified by actually generating Java (okhttp-gson) and TypeScript (fetch) clients
against a live server. Specifically **no duplication of the SQL guard**: the actual SQL execution still
happens in this one service.

## 9. Type checking and CI hardening

**Status: shipped.** `mypy` runs in CI in gradual-typing mode (catches real static errors without demanding
annotations across an until-now-untyped codebase) - it found and fixed two genuine, if low-impact, issues:
an untyped base-class attribute that made every dialect subclass a type error, and unannotated module-level
counters in `metrics.py`. It also surfaced a real cross-version mypy quirk on the first actual CI run: mypy
2.x can't run on Python 3.9 at all, so that job resolves an old 1.x release, which - unlike 2.x - doesn't
treat `ignore_missing_imports` as covering a stub-less-but-installed module (PyYAML); fixed with an explicit
`disable_error_code`, verified against both mypy generations. Coverage is measured and uploaded to Codecov
(badge in the README), currently 93% from the unit suite alone. Python 3.14 added to the CI matrix.
Still open (needs the next real CI run to confirm): whether every C-extension dependency
(`psycopg2-binary`, `JPype1`, etc.) already has 3.14 wheels, and whether Codecov's tokenless upload works
for this public repo on the first real run - if not, add a `CODECOV_TOKEN` repository secret from
codecov.io.

## 10. Docs site (MkDocs on GitHub Pages)

**Status: shipped and live** at <https://AnanthaRajuC.github.io/QueryAPIGate/>. `mkdocs.yml` (Material theme) and
`.github/workflows/docs.yml` build and deploy the site on every push to `main` that touches a doc file.
`docs/` holds no real content - every file there is a one-line `pymdownx.snippets` include of the actual
document (this README, `documentation/*.md`, `CONTRIBUTING.md`, etc.), mirroring their repo-relative paths
so the links between them keep working unchanged on the built site; there is exactly one place to edit each
document, same as before this existed.

## 11. DuckDB: query flat files directly, and as a general embedded database

**Status: shipped.** `db: "duckdb"` - own `_Driver` subclass in `runners.py`, opt-in `queryapigate[duckdb]` extra,
schema introspection, and a `database: <path>` shape matching SQLite's, resolved the open question below in
favour of a persistent `.db` file with no new connection fields for the file-querying case (`SELECT * FROM
read_csv(:path)` from a saved query's own SQL). Two things only found by testing against a real DuckDB
database, not guessable from the docs:

- DuckDB refuses a second connection to the same file with a different read-only setting than one already
  open on it in-process - fatal for this project's pool, which keys read-only and read-write connections to
  the same details separately. Every pooled connection is opened read-write regardless of the caller's
  read-only flag, and the guarantee rests on `validate_sql()` alone, same as `h2`/`jdbc`.
- Unlike `h2`/`jdbc`, DuckDB's Python client *does* support cancelling a running statement
  (`Connection.interrupt()`), so - unlike those two - the query time limit is genuinely enforced here, via a
  background timer.

Also worth remembering: a relative path inside `read_csv()`/`read_parquet()` resolves against the *server
process's* working directory, not `QUERYAPIGATE_HOME` (unlike the connection's own `database` field) - see
[DuckDB connections](documentation/DATABASE_CONNECTION_CONFIGURATION.md#duckdb-connections). And the same
mypy/Python-3.9 cross-version gap documented under item #9 recurred here too: `duckdb` only gained
`sleep_ms()` in its 1.5 release, and CI's Python 3.9 job resolves the newest 3.9-compatible one, 1.4.5 - the
integration test's "slow query" fixture uses a CPU-bound recursive CTE instead, verified interruptible on
both 1.4.5 and 1.5.5. Verified end-to-end: a real DuckDB integration test class (`tests/test_integration.py`,
runs unconditionally - no service container needed, unlike the network databases), and a real server/curl
session for the flat-file path (aggregation, filtering, and the file path itself passed as a bound
parameter).

## 12. Basic data visualization in the admin UI

**Status: shipped.** A "Chart" toggle button next to "Copy as TSV" (Run SQL and saved-query Run tabs,
`ui.py`'s shared `renderResponse()`/`buildChartPanel()`) appears whenever a JSON result has at least one
numeric column, off by default so it never competes with the table/raw view. Resolved the module's stated
risk directly: the panel's own toolbar carries a visible "Chart of this page only (N rows) - not the full
result" note (dynamic row count, and "N of M" when the page itself was further truncated to the chart's own
50-bar cap) rather than implying a full-dataset view the API was never asked to compute. Label and value
columns are pickable from dropdowns - value defaults to the first column that's numeric-or-null in every
row, label to the first column that isn't - and repaint live on change. Rendered as hand-rolled inline SVG
(`barChartSvg()`, raw `document.createElementNS`, since the existing `h()` DOM builder is HTML-only) with
no charting library, matching the SQL editor's own no-dependency syntax highlighting; handles negative
values correctly (a zero baseline, bars extending up or down from it). Scoped to a bar chart only for this
first version, not line/scatter too, to keep a single hand-rolled renderer rather than three. No backend
changes - purely a `/ui` addition. Verified with a real server and real headless Chrome: the button appears
only when a numeric column exists (and is absent for an all-text result), the panel is hidden by default
and toggles correctly, the row count and truncation note are accurate, the label/value dropdowns default
correctly and repaint on change, and negative values render below the zero line.

## 13. Admin UI quick wins, lowest effort first

**Impact:** ten small, UI-only additions (no backend changes for any of them) gathered from a few different
tools - a SQL IDE (DataGrip/DBeaver), an API client (Postman), a BI tool (Superset), and everyday UX
conventions (password managers, JSON viewers, spreadsheets). Each reuses infrastructure this project already
shipped (the schema browser, the run/execute path, response data already in scope) rather than adding a new
endpoint. Listed lowest effort first, so it doubles as a pick-up-in-order list, not just an idea dump.

1. **"Import into Postman" tip. Shipped.** `/openapi.json` already exists, and Postman/Insomnia can import an
   OpenAPI spec directly by URL - a one-line documentation callout in
   [API.md](documentation/API.md#interactive-documentation), no code at all.
2. **Status + timing badge for successful responses, not just errors. Shipped.** The Run SQL tab's stat bar
   now leads with the HTTP status code and status text (Postman's "200 OK · 1 row · json · 8 ms"),
   accent-colored to read as success at a glance. Verified with a real headless-Chrome test: the badge
   appears first, is visually distinct from the plain stat numbers, and the error path (its own `.eb-code`
   badge) is unaffected.
3. **Show/hide toggle on the connection form's password field. Shipped.** A "Show"/"Hide" button next to
   the field, working in both create and edit mode without disturbing the existing password-mask
   round-trip. Verified with a real headless-Chrome test.
4. **"Explain" button. Shipped.** `EXPLAIN` is already an allowed read-only statement - the guard accepts it
   today. A button next to Run runs `EXPLAIN <current query>` through the existing execute path and shows
   the plan (DataGrip/DBeaver-style). Verified with a real headless-Chrome test against a live server.
5. **Response headers panel. Shipped.** A collapsible "Headers" section under the results, listing
   everything the response actually carries - `X-Page`, `X-Request-Id`, etc. - using data `renderResponse()`
   already has in `res.headers` (Postman-style). Verified with a real headless-Chrome test that the panel
   starts hidden and lists real response headers once toggled open.
6. **"Copy as curl." Shipped.** A button next to Run that gives the exact `curl` command for the request
   just made, built from data already on hand (sql, connection, params, format). Deliberately scoped to the
   Run SQL tab's ad-hoc queries, not saved-query runs. The API key, if any, is written as a `YOUR_KEY_HERE`
   placeholder rather than the real value, since this text is meant to be copied out of the browser and the
   key typed into the page shouldn't ride along by default. Verified with a real headless-Chrome test
   (including reading the actual clipboard contents).
7. **"Copy as TSV." Shipped.** Paste-ready for Excel/Sheets, from the rows already sitting in memory from
   the last query - a pure client-side transform, no extra round-trip. Verified with a real headless-Chrome
   test that the clipboard contains a correct header row and data rows.
8. **Client-side ad-hoc query history. Shipped.** A `sessionStorage`-backed "last N queries" list under the
   Run SQL tab (click to restore into the editor), the same pattern already used for the API key and the
   last-active tab (Superset SQL Lab-style). Verified with a real headless-Chrome test, including that
   history survives a page reload within the same tab.
9. **Table preview from the schema browser. Shipped.** A small "preview" affordance next to a table builds
   `SELECT * FROM <table>` and runs it through the existing execute path, from both the Run SQL tab's own
   schema panel and the query drawer's (which also closes the drawer and switches to the Run SQL tab).
   Verified with real headless-Chrome tests covering both entry points.
10. **Collapsible JSON tree for non-tabular responses, instead of today's flat `<pre>` dump. Shipped.** What
    browser devtools or `jq` give for free - an expand/collapse tree for any non-array JSON response (for
    example the `{"message": "No results returned"}` shape). Verified with a real headless-Chrome test that
    the tree renders and the toggle collapses/expands it.

Explicitly **not** worth doing here: per-table row counts in the schema browser (needs an extra `COUNT(*)`
per table - not actually cheap), a SQL auto-formatter (needs a real formatter), a "collections" concept
(saved queries already group and tag queries), and pre/post-request scripting (real scope creep for an
admin UI).

## 14. Per-query write curation for API keys

**Status: shipped.** `allow_writes` used to be a blanket flag on the whole key - if a key could write at
all, it could write through *any* connection or saved query it was scoped to, not just a specific one. Now
a `queries` entry can be `{"name": "submit_order", "allow_writes": true}` instead of a plain name, adding
write access to that one query specifically, on top of (never instead of) the key's blanket `allow_writes` -
the shape the external-client `queries` grant (#1) was originally built for but couldn't quite express: a
key with `connections: []` and `allow_writes: false` can still write through one curated endpoint and
nothing else. Resolved the open question in favour of the safer option: `_normalize_queries()` never lets
`"*"` carry a write grant, structurally - "*" is a plain string, never a list, so there is no way to attach
a per-entry write flag to it at all; wanting write access to a specific query means enumerating the whole
`queries` list explicitly, the same explicit opt-in shape `allow_writes` already has everywhere else.
`Permission.queries` changed from a `frozenset` of names to a `{name: allow_writes}` dict for a scoped key
(a plain-string entry reads back as `allow_writes=False`, so an existing key's stored file needs no
migration) - dict membership behaves identically to the frozenset it replaces for the existing
`can_use_query()` check, so that and `describe_saved_queries()`'s catalogue filtering needed no changes at
all; only `run_saved()` gained a new `apikeys.can_write_query()` OR'd with the key's blanket `allow_writes`
before calling `engine.execute_sql()`. Still subject to the existing ceiling (never wider than server-wide
`QUERYAPIGATE_ALLOW_WRITES`) and the normal SQL guard, including a key's own `allowed_write_ops` (#21) if it has
one - both restrictions apply together, independently. Admin UI's saved-query checkbox grid gained a
per-query "write" checkbox, disabled and cleared automatically when its own query checkbox is unchecked.
Verified: write through a curated query with zero other access of any kind, a plain-string entry granting
no write access, write curation not leaking to a different query, ad-hoc SQL still fully blocked, the
server-wide ceiling still capping a query-curated grant, the wildcard never carrying write access, a
blanket-write key unaffected, add/remove via `PATCH`, duplicate-name and malformed-entry rejection, the
admin key unaffected, and a real headless-Chrome UI test (per-query write checkbox, disabled state tracking
its own read checkbox, edit-form prefill for both plain and write-curated entries, table display
distinguishing `name` from `name (write)`).

## 15. Per-key rate limiting

**Status: shipped.** An optional `rate_limit` (e.g. `"100/minute"`, `config.parse_rate_limit`'s grammar
reused via a new `label` parameter so a malformed value's error message correctly names `rate_limit`, not
`QUERYAPIGATE_RATE_LIMIT`) on a key's stored permissions. Open question resolved in favour of "applies in
addition to" the IP-based limit, not instead of it - the more defensible option, and the one that actually
needed the extra work the question called out. Checked after `g.permission` is resolved (the IP-based check
stays exactly where it was, still ahead of authentication, so "guessing keys is throttled too" is
unaffected), via a distinct `check_key_rate_limit()` and its own `X-RateLimit-Key-*` headers and
`"...for this API key"` error text, so the two limits are never conflated in a response. Needed a real
architectural piece, not just a new field: `RateLimiter` only supports one spec per instance (wipes every
bucket when a different one arrives - by design, for `QUERYAPIGATE_RATE_LIMIT` itself), so two keys with
different limits can't share one; `ratelimit.KeyRateLimiters` routes each distinct spec to its own
`RateLimiter`, lazily created, with keys sharing a spec correctly sharing an instance (and still getting
independent buckets within it). Verified: over-limit rejection with the right error/headers, a key with no
`rate_limit` never throttled by this mechanism, two keys with the same limit having independent budgets,
both limits genuinely required together (tight per-key limit bites while a generous server-wide one stays
comfortable), the admin key exempt, health/metrics exempt, malformed input rejected with the correct field
name, explicit-null clearing, and a real headless-Chrome UI test - plus a low-level unit test of
`KeyRateLimiters` proving different specs don't clobber each other's state (the actual bug this design
avoids, not just an assumption). See [Per-key rate limiting](documentation/API.md#per-key-rate-limiting).

## 16. API key expiry (TTL)

**Status: shipped.** An optional `expires_at` (`YYYY-MM-DD`, day granularity - not a full timestamp, since
the use case only ever needs day precision and a bare date is what a plain HTML date input gives you with
no timezone-conversion surprises). Checked live in `apikeys.authenticate()` alongside `active`, valid
through the *end* of the given date (23:59:59), not its start - "expires today" still works today. No
background sweep, exactly as scoped: an expired key just stops matching, the same way flipping `active` to
`false` already does. Both open questions resolved: an expired key still lists (the admin UI shows it
visually distinguished, "(expired)" in red, same row style as a revoked one, not hidden), and an expiry can
be extended or cleared without rotating the secret - `PATCH /api_keys/<name>` needed a real presence check
(`'expires_at' in body`, not `.get()`) to distinguish an explicit `{"expires_at": null}` (clear it) from
the field simply being absent (leave it alone), since both look identical through `.get()`. Verified: an
expired key rejected (401), a not-yet-expired one still works, a key expiring today still works, malformed
dates rejected, listing/table display, explicit-null clearing re-enabling a key, and that an unrelated
`PATCH` leaves an existing expiry untouched - via real unit tests and a real headless-Chrome UI test.
See [Key expiry](documentation/API.md#key-expiry).

## 17. IP allowlisting per API key

**Status: shipped.** An optional `allowed_ips` field (a list of IP addresses or CIDR ranges, IPv4 or IPv6,
mixed freely) alongside `connections`/`queries` on a key's stored permissions, checked in
`apikeys.authenticate()` (a new `_ip_allowed()` helper) against `request.remote_addr` passed in from
`app.resolve_permission()` - the same address `QUERYAPIGATE_TRUST_PROXY`/`ProxyFix` already establish as
trustworthy for rate limiting, reused rather than re-derived. Uses the stdlib `ipaddress` module for parsing
and CIDR containment, exactly as scoped - no new dependency. A request from outside the list fails the same
way a wrong key does (`401`), not a distinct error, consistent with how an expired or inactive key already
behaves; the admin key (an env var, not a stored entry) is never restricted by it. `PATCH /api_keys/<name>`
needed the same real presence check as `expires_at`/`rate_limit` (`'allowed_ips' in body`, not `.get()`) so
an explicit `{"allowed_ips": null}` (clear the restriction) is distinguishable from the field being absent
(leave it alone). Admin UI gained a comma-separated "Allowed IPs" form field and a compact "IPs" table
column (a count with the full list in a tooltip, to avoid widening an already nine-column table). Verified:
exact-address and CIDR-range matching for both IPv4 and IPv6, a non-matching address rejected, an unset key
working from any address, malformed input rejected, listing display, explicit-null clearing, an unrelated
`PATCH` leaving an existing restriction untouched, and that the admin key is exempt - via real unit tests
and a real headless-Chrome UI test (create with an allowlist, table shows the count and correct tooltip,
edit form is pre-filled, clearing it in the form restores "any"). See
[IP allowlisting](documentation/API.md#ip-allowlisting).

**Explicitly flagged, not dismissed:** real operational friction comes with this one - dynamic IPs, NAT,
cloud provider IP churn, and a client behind a CDN or corporate proxy can all make "the expected IP" a
moving target that generates support burden more often than it stops an attacker. Worth having as an
opt-in for the callers who actually have stable infrastructure (most external B2B integrations do), not
something to default new keys into.

## 18. Per-key usage visibility (last used, recent activity)

**Status: shipped.** `last_used_at` recorded on a key's stored entry in `api_keys.json`, on every
`authenticate()` match, throttled to once per 60 seconds per key (`apikeys._record_use()`,
`_USE_RECORD_INTERVAL`) - resolving the open question in favour of the "once-per-minute-ish" end of the
range, since that already answers "is this key alive" without meaningfully misleading anyone. Re-reads the
stored entry fresh under `store.lock` immediately before writing, rather than reusing the copy
`authenticate()` already had in hand, so a concurrent admin change (revoke, connections update) is never
clobbered with stale data - same discipline `update_key()`/`delete_key()` already use. `GET /api_keys`
surfaces it automatically (the existing `list_keys()` already passes through every stored field except the
hash, so no separate change was needed there), and the admin UI's table gained a "Last used" column,
showing "never" for a key that hasn't been. The admin key (`QUERYAPIGATE_API_KEY`) has no stored entry to
record use against - it's an env var, not a row in `api_keys.json` - so `authenticate()`'s admin-key branch
never calls this at all, unaffected. Verified: first use recorded, rapid re-use throttled to exactly one
disk write (`_write` call-count asserted directly, not just "the timestamp looks similar" - coarse
timestamp resolution alone wouldn't have proven the throttle actually fired), a write happens again once
the throttle window passes, two different keys don't throttle each other, and a real headless-Chrome UI
test end to end.

## 19. Structured per-query observability fields (query correlation, SQL hash, serialization time)

**Status: shipped.** Observability used to be split across three places that didn't line up with each
other - structured JSON logs (`logging_setup.py`), Prometheus counters/histograms (`metrics.py`), and a
saved query's `execution_history` (`store.py`) - and none of the three carried all of the fields an
operator actually wants when tracing one specific slow or failed call. Landed in two slices:
- **Query correlation.** `execution_history` now carries `request_id` and `key_name` (captured in
  `app.run_saved()`, and for a streamed saved query in `app.stream_sql_response()` specifically - not
  inside the lazily-run `_record_stream_history()` generator itself, whose body only executes once the
  response is actually streaming out, by which point the request/app context that produces `g` and
  `caller_key_name()` is already gone; no `flask.stream_with_context()` is used here. Getting this wrong
  was caught immediately by the test suite as `RuntimeError: Working outside of application context` the
  moment a streamed request was exercised). JSON logs are no longer only partially structured either:
  `_JsonFormatter` now promotes any attribute passed via a call's `extra={...}` to its own top-level JSON
  key, computed generically (diffed against a vanilla `LogRecord`'s own attributes, no fixed field list to
  maintain) - wired up at the per-query line (`connection`, `dialect`, `limit`, `offset`, `timeout`), the
  streaming-start line (`connection`, `dialect`), the slow-query warning (`connection`, `dialect`,
  `duration_ms`), and the per-request access log line (`method`, `path`, `status`, `duration_ms`).
- **SQL hash and serialization time.** The per-query and streaming-start log lines now also carry a full
  SHA-256 `sql_hash` (`engine._sql_hash()`, same full-length-hex convention `pool.py`/`cache.py` already
  use) alongside the existing full SQL text, not instead of it. Response serialization (JSON/CSV/TSV/XML/
  YAML/XLSX encoding) is now timed inside `app.render()` itself and surfaced three ways: a new
  `queryapigate_serialization_duration_seconds` metric by output format, a `serialization_ms` field on the
  access log line (only present when a paged response actually went through `render()`), and a
  `serialization_ms` field on a saved query's `execution_history` entries alongside the existing
  `duration_ms` (query time) - required moving `store.record_execution()` to *after* `render()` in
  `run_saved()` so both timings land in the same entry rather than two. Deliberately never measured for
  streaming responses, which format row by row interleaved with network I/O - there's no single
  "serialization happened here" span to measure the way there is for a page built in memory up front.
  `database_time` vs. `execution_time` stays a non-gap, as originally assessed: one measurement is genuinely
  enough absent a concrete need to split pool-checkout time from driver time specifically.
Verified: a direct unit test of `_JsonFormatter` (extra fields appear, no stray fields when none are
passed, no collision with `request_id`/`key`), an integration test per log call site asserting real field
values (including that `sql_hash` is the actual SHA-256 of the actual SQL and differs between different
queries) in real JSON output, `execution_history` correlation for both paged and streamed saved queries,
the serialization metric appearing for a paged response and *not* moving for a streamed one, and a
low-level histogram-bucketing test for the new metric.

## 20. Two missing `/metrics` series: rows returned and in-flight queries

**Status: shipped.** Two new series, plain module-level state in `metrics.py` matching every other
counter/gauge there - no change to the existing "in-memory, single gunicorn worker" limitation documented
at the top of that file. `queryapigate_rows_returned_total{connection,dialect,key}` sums rows actually returned:
`engine.execute_sql()` records the trimmed page's length (the same `rows[:limit]` already computed for
`has_more`, so the exact figure sent to the caller, not the possibly-larger raw fetch used to detect
another page); `engine._drain()` counts streamed rows as they pass through (an explicit loop replacing the
old `yield from rows`, mirroring the technique `app._record_stream_history()` already used for
`execution_history`) and records the count on every exit path - success, a mid-stream error, or a client
disconnect (`GeneratorExit`) - since a partial count is still data that left the server, not nothing.
`queryapigate_active_queries` is an unlabeled gauge, incremented before a query starts and decremented once it's
done. For a paged query that's a single `engine.execute_sql()` call; for a streaming export the connection
stays checked out for as long as the client keeps reading, so the increment happens in `stream_sql()` but
the decrement is deferred to wherever the query actually stops being active - `stream_sql()`'s own
`finally` if it never got to `_drain()` (a connection/driver error before draining ever starts), or
`_drain()` itself on every one of its exit paths otherwise. A related question - whether errors deserve
their own counter name rather than living as a `status="error"` label on `queryapigate_queries_total` - turned
out to already be a reasonable, common Prometheus convention as-is, not a gap; left unchanged. Verified:
row counts for both a paged query and a full streaming export (via real HTTP requests through the test
client, asserting the delta against the live `/metrics` output, not a mocked count), a low-level test that
`_drain()` settles the active-query gauge back to its prior value after a client disconnect
(`GeneratorExit`) specifically - not just the happy path - and a real concurrency check outside the test
suite (a slow query run on a background thread while polling `/metrics` from the main one, confirming the
gauge reads 1 mid-flight and returns to 0 once the request completes). See
[Observability](documentation/API.md#observability).

## 21. Finer-grained query governance: table allow-lists, operation-type granularity, a real streaming ceiling

**Status: shipped, three of three gaps**, table allow-listing (the long-deferred remainder) now included -
for `mysql`/`postgres`/`clickhouse`/`sqlite`/`duckdb` connections; `h2`/`jdbc`/`mongo` remain a deliberately
deferred, explicitly-scoped-out remainder of *this* gap (see below), not silently unsupported. `timeout`,
`cache_ttl` and `rate_limit` were already solid, and `allowed_users` was already covered (a key's
`connections`/`queries` grants, just modeled the other way round - not a gap).
**Shipped:**
- **`allowed_write_ops`.** A key with `allow_writes` on can now be narrowed to specific write keywords
  (`["insert", "update"]`, say) rather than every write keyword being equally permitted once writes are on
  at all. New `apikeys._validate_allowed_write_ops()` (no fixed enum - the write-keyword space is
  open-ended, so a name is accepted as given), a seventh `Permission` field, and a new `elif` branch in
  `sqltools.validate_sql()` right next to the existing read-only check - never restricts a read-only
  statement, only narrows which *write* ones a key may perform, checked against the statement's own leading
  keyword via the already-existing `first_keyword()`. No parser needed, exactly as scoped. Admin UI gained
  an "Allowed write operations" field and a write-op count on the existing Access column (no new table
  column, to keep the table's width unchanged).
- **`QUERYAPIGATE_STREAM_MAX_ROWS`.** A `?stream=true` export was completely unbounded regardless of
  `QUERYAPIGATE_MAX_PAGE_SIZE`; this caps it, validated at startup like `QUERYAPIGATE_RATE_LIMIT` (a typo shouldn't
  silently leave a requested safety cap unbounded, unlike `QUERYAPIGATE_MAX_PAGE_SIZE`'s own lenient fallback).
  Enforced in `engine._drain()`, which already counted rows as they passed through: past the cap, it stops
  yielding and calls `rows.close()` on the still-open inner generator to release the connection immediately
  (the same `GeneratorExit`-based cleanup a client disconnecting mid-stream already triggers -
  `runners._make_stream_runner`'s `finally: session.close()`) rather than waiting on garbage collection. A
  `break` inside the `for` loop still reaches the wrapping `try`'s own `else:` normally (only a `for`'s
  *own* `else` is skipped by `break`, not an enclosing `try`'s), so the usual success bookkeeping still
  runs. `queryapigate_stream_exports_total` counts a truncated export under a new `status="truncated"`, distinct
  from `"success"`; a saved query's `execution_history` still records `"success"` with the truncated row
  count, since it did succeed, just not to completion. Scoped server-wide only, not per-key, to keep this
  slice cheap - a per-key override would need its own `Permission` field and create/update-key API surface,
  deferred as a possible future refinement rather than blocking this.
Verified: a permitted write allowed, a non-permitted one rejected naming the allowed set, read-only never
restricted, unset meaning any write, the restriction never widening `allow_writes`, keyword
case-normalisation, malformed input rejected, listing/clearing/leave-unchanged for `allowed_write_ops`, the
admin key exempt from both restrictions, a stream truncated at exactly the cap (not the cap minus or plus
one), an under-cap result unaffected, startup rejection of a malformed cap, `execution_history` reflecting
the truncated count, the new `"truncated"` metric status, and a real headless-Chrome UI test (write-op
count and tooltip, edit-form prefill, table width unchanged at 1238px).
- **`allowed_tables`.** Scoped with the user up front to `mysql`/`postgres`/`clickhouse`/`sqlite`/`duckdb`
  only - the same five dialects `sqlflow.py` (#40) already parses for the Access tab's "Query flow" diagram;
  `h2`/`jdbc`/`mongo` have no `sqlglot` dialect to parse against and are deliberately deferred, not silently
  unsupported (see the fail-closed behavior below). Real SQL parsing was needed, not `sqltools.py`'s
  lightweight regex guard - `sqlglot` (already an optional dependency via the `flow` extra) turned out to
  have exactly the right tool, verified directly rather than assumed: a naive `find_all(exp.Table)` walk
  (what `sqlflow.extract_flow()` already does for its visualization) wrongly treats a CTE's own name as a
  real table; `sqlglot.optimizer.scope.build_scope()`, the "correct" CTE-aware tool, was tested and found to
  return `None` entirely for `DELETE`/`UPDATE`/`INSERT` - it's optimizer machinery built for `SELECT`-shaped
  queries only, and would have silently missed every write statement's own target table, a real hole for a
  key that also restricts writes via `allowed_write_ops`. The actual fix, verified against CTEs, subqueries,
  self-joins, `UNION`, and bare or CTE'd `DELETE`/`UPDATE`/`INSERT`: `find_all(exp.Table)` minus the names of
  any `exp.CTE` in the same parse tree - simpler than the scope-builder, and unlike it, correct for every
  statement type this guard needs to check. New `queryapigate/tableguard.py` (deliberately not folded into
  `sqlflow.py`, whose own docstring commits to "never raises" for its visualization-only job - a security
  check needs the opposite contract: an unverifiable query must be rejected, never quietly skipped).
  `sqltools.validate_sql()` gained one more parameter, threaded through `engine.execute_sql()`/`stream_sql()`
  to the same `app.py` call sites `allowed_write_ops` already reaches; `apikeys.py` got the same
  create/update-key/role sweep `allowed_write_ops` already went through (`_validate_allowed_tables()`, a
  ninth `Permission` field, role-copy-onto-key support). Unlike `allowed_write_ops`, this restricts *every*
  statement, read or write, since a forbidden table is forbidden regardless of what's being done to it. A
  table-restricted key used against an `h2`/`jdbc`/`mongo` connection is refused on *every* query with a
  clear `403` naming the unsupported dialect - a deliberate, visible failure mode, not a silent bypass; an
  admin who also grants such a connection to a table-restricted key will find it unusable until the
  restriction is narrowed or this dialect gap is separately picked up. Admin UI gained an "Allowed tables"
  field on both the API Keys and Roles forms, and a `"N tables"` badge on the Access column, alongside (not
  replacing) the existing write-ops badge, matching #21's own established "no new table column" precedent.
  Known, accepted limitations (verified, not guessed): a table name is extracted bare
  (`information_schema.tables` -> `tables`), not schema-qualified, matching `allowed_write_ops`'s own
  bare-keyword-list style but unable to disambiguate two identically-named tables in different schemas; a
  table-valued function (e.g. ClickHouse's `numbers(10)`) touches no real table, so this can't meaningfully
  restrict one.

Verified (this shipment): every `tableguard.extract_tables()` case above codified as a permanent unit test,
`sqltools.validate_sql()`'s own `allowed_tables` enforcement unit-tested directly (unset means unrestricted,
a forbidden table rejected, a join pulling in one forbidden table rejected even with an allowed one present,
an unsupported dialect always rejected, an existing `allowed_write_ops` violation still reported first), a
full `AllowedTablesTests` integration class mirroring `AllowedWriteOpsTests` one-for-one (permitted/forbidden
queries, restricting writes too, case-normalisation, malformed input, listing, clear-via-null,
leave-unchanged, the admin key exempt, the h2 fail-closed case, a role correctly copying the restriction onto
a key created from it), and a real headless-Chrome check (the "Allowed tables" field on both forms, the
table-count badge with its tooltip, and real enforcement proven via direct `curl` calls against a key created
entirely through the UI - a permitted table's query returning `200`, a forbidden one `403`).

## 22. Named permission roles/templates (reusable RBAC on top of per-key ACLs)

**Status: shipped**, as a **template**, resolving the open question below in favor of the simpler of the two
designs. `apikeys.create_role()`/`update_role()`/`delete_role()`/`list_roles()` manage roles in their own
`roles.json`, reusing every one of `apikeys.py`'s existing field validators
(`_normalize_connections`/`_normalize_queries`/`_validate_rate_limit`/`_validate_allowed_ips`/
`_validate_allowed_write_ops`) so there is exactly one definition of each field's validity, never duplicated
for roles. `POST /api_keys` with `"role": "<name>"` copies that role's `connections`, `allow_writes`,
`queries`, `rate_limit`, `allowed_ips` and `allowed_write_ops` onto the new key **once, at creation** -
`authenticate()` only ever reads the key's own stored entry afterward, never the role, so editing or
deleting a role has **zero effect** on a key already created from it: no accidental blast radius, and
nothing to keep in sync. A key records which role it came from in `created_from_role` - purely informational
metadata, never consulted by any permission check. `role` is rejected (`400`, naming the conflicting fields)
if combined with an explicit grant field in the same request - create the key from the role, then `PATCH` it
afterward to customize; `expires_at` is the one field that can still be set alongside `role`, since it's
inherently per-key rather than part of a shared template. All four role-management routes are admin-only and
audited (`create_role`/`update_role`/`delete_role`, see #23). Admin UI gained a Roles tab (create/edit/delete,
plus a "New key from this" shortcut) and a "Create from" picker in the New API key form that dims the grant
fields below it while a role is selected. Verified: role CRUD (including duplicate-name and malformed-field
rejection), `create_key(role=...)` field resolution, the conflict-rejection `400`, an unknown-role `404`, the
core template guarantee (editing/deleting a role after key creation never changes that key, confirmed both
in `tests/test_roles.py` and via a live request against a running server), `created_from_role` in `GET
/api_keys`, audit-log entries for all three role actions, and a real headless-Chrome run through the Roles
tab and the New API key form's role picker end to end. See
[Permission roles](documentation/API.md#permission-roles-templates).

*Original open question, for context:* a **template** (copied into a key at creation time, then independent
- no accidental blast radius when the template later changes, matching how a `PATCH` to one key today never
affects another) is materially simpler than a **live-linked role** (a key stores `role: "reporting"` and its
effective permissions are resolved from the role at `authenticate()` time, so editing the role instantly
changes every key referencing it - fewer places to update, but now a role edit is itself a sensitive,
blast-radius action). The template was chosen as the shipped design.

## 23. Audit logging for administrative actions

**Status: shipped.** A durable, append-only record (`store.record_audit()`/`read_audit_log()`, plain JSON
in `audit_log.json`, `store.lock`-serialised, capped at `config.AUDIT_LOG_LIMIT` (500) the same way
`execution_history` is capped per query version) of every create/update/delete across API keys,
connections and saved queries: `{timestamp, actor, action, target, changes}`. An update (`update_key`,
`update_connection`) records a diff of only the fields that actually changed (`app._dict_diff()`); a create
or delete records a full snapshot instead, since there's no prior or remaining state to diff against.
Resolved the module's one real design risk before shipping: a connection's `password` is never a value in
either form - `app._connection_audit_changes()` reports it only as the literal string `"changed"` in a
diff, and snapshots (create/delete) go through the existing `store.mask_passwords()` - so the audit log
never becomes a second place a real password leaks from, and an API key's entry never includes its secret
or hash (`apikeys.list_keys()` already strips it). Exposed as `GET /audit_log` (admin only, newest first)
and an "Audit Log" tab in `/ui`. Deliberately not a replacement for `QUERYAPIGATE_JSON_LOGS` + an external log
aggregator, which remains the right tool for full request-level traffic analysis - this is specifically for
the smaller, higher-stakes set of configuration-changing actions, durable inside `QUERYAPIGATE_HOME` itself
rather than depending on external log retention being configured correctly. Verified: every action type
records the expected entry shape, a no-op update records nothing new, a connection's password never
appears anywhere in a recorded entry (as a value) whether created, changed or deleted, an API key's secret
never appears, non-admin callers get 403, ordering and the cap, that a write failure never breaks the
action it's recording, and a real headless-Chrome UI test confirming the tab renders diffs and snapshots
correctly with no leaked secret in the DOM. See [Audit log](documentation/API.md#audit-log).

## 24. Credential encryption at rest and secret-manager integration

**Status: shipped.** A password given as a literal string (rather than a `${VAR}` reference) in
`db_connections.json` used to be stored **in plaintext on disk** - `mask_passwords()` only hid it in API
*responses* (`GET /connections`), never encrypted the stored file itself. Landed in two slices:
- **Startup warning (the cheap first slice).** `store.plaintext_password_connections()` names every
  connection whose password is a non-empty literal with no protection at all, in one warning line at
  `app.create_app()` - a nudge, not an enforcement.
- **Encryption at rest (the real fix).** `QUERYAPIGATE_SECRET_KEY` (a Fernet key, validated at startup) turns
  every literal password into ciphertext before `write_json_atomic()` writes it - existing connections
  immediately, via a new `store.encrypt_plaintext_passwords_in_place()` run once at startup rather than
  waiting for a manual migration or a connection's next `PATCH`; new ones the moment they're saved, in
  `update_connections()`. Decrypted only in `get_connection()`, at the exact moment a connection is opened -
  never held decrypted anywhere else. Stored with an `enc:` prefix so a literal, a `${VAR}` reference and an
  encrypted value are always distinguishable at a glance, and so `mask_passwords()` and the startup warning
  above could be taught the difference: an encrypted value now masks the same as a literal in API responses
  and the audit log (neither is ever a value a client should see), but no longer trips the "literal
  password" warning once it's actually protected. `cryptography` is a new dependency, but an *optional* one
  (`queryapigate[encryption]`, bundled in `[all]`) imported lazily only when `QUERYAPIGATE_SECRET_KEY` is actually set -
  a deployment that never opts in installs nothing extra, matching how the DB driver extras already work; a
  clear startup error names the missing package rather than a raw `ImportError` if the key is set without it.
  Both open questions resolved as scoped: a missing or rotated key fails a request clearly (`500`, naming the
  problem - "fail closed, say why", the same precedent `expires_at`/`rate_limit` already set) rather than
  passing ciphertext to the driver or silently falling back to plaintext, and a startup warning fires if
  encrypted passwords exist on disk with no key configured to read them at all (a rotated-out or removed key,
  most likely). Third-party secret-manager integration (Vault, AWS/GCP Secrets Manager) stays explicitly out
  of scope, per the original assessment - `${VAR}` already covers pulling a secret from any of them via an
  operator's own startup tooling.
Verified: startup migration encrypting a literal password (and leaving a `${VAR}` reference or an
already-encrypted value untouched, and an empty password alone), a query working end-to-end through an
encrypted password, `GET /connections` masking it, echoing the mask back keeping the stored ciphertext
unchanged (never re-encrypted), no encryption at all without a configured key (today's behaviour, unaffected),
a missing key both warning at startup and failing clearly on use, a rotated key failing clearly with its own
distinct message, a malformed key rejected at startup, a missing `cryptography` package rejected at startup
with an actionable message, neither the plaintext nor the ciphertext ever appearing in the audit log, and a
real headless-Chrome UI check confirming the connection edit form shows the mask (never the ciphertext) and a
query against an encrypted connection still runs successfully end to end through the UI.

## 25. Benchmark coverage gaps: general API latency, connection pooling, cache performance

**Status: shipped**, as three new scripts (`benchmarks/latency.py`, `pooling.py`, `caching.py`) alongside the
existing `run.py`, sharing a new `benchmarks/common.py` (the `Server`/`Sampler`/native-connect plumbing
`run.py` used to own alone). Real runs committed for MySQL and ClickHouse (disposable Docker containers,
matching CI's own images/credentials, torn down after) - see `benchmarks/README.md` for the full results and
methodology per script. One real finding worth flagging here since it changed the pooling benchmark's own
design partway through: `pool.py` only bounds *idle* connections kept around, not concurrent ones, so
"wait time when the pool is exhausted" - the framing this entry originally used below - does not apply to
this codebase's pool at all. `pooling.py` measures what's actually real instead: throughput/latency and the
idle-pool hit rate (`queryapigate_pool_idle_connections`, sampled during the run) as concurrency increases
relative to `QUERYAPIGATE_POOL_SIZE`. Postgres/SQLite/H2/DuckDB runs are left as a follow-up - the harness
handles every dialect `run.py` already does, just not yet run and committed for the other four.

**Impact (as originally scoped):** `benchmarks/` (see [benchmarks/README.md](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/benchmarks/README.md))
used to answer exactly one
question - buffered vs. streamed memory and latency for one large flat `SELECT`, per dialect - and answered
it well. It did not answer three other questions a production-grade deployment would reasonably want
evidence for:
- **General API latency.** The existing suite only measures latency at the 1M-row large-export scale; there
  is no benchmark for the common case - a typical small `/execute_sql` or `GET /q/<name>` call - so there's
  no p50/p95/p99 figure for "how much overhead does QueryAPIGate itself add" independent of query execution time
  (parameter binding, the SQL guard, rate-limit/permission checks, response serialization).
- **Connection pooling under concurrency.** `queryapigate_pool_idle_connections` exists as a live gauge
  (`GET /metrics`), but nothing exercises it under load - no benchmark shows pool hit rate, wait time for a
  connection when the pool is exhausted, or throughput as concurrent callers increase relative to
  `QUERYAPIGATE_POOL_SIZE`.
- **Cache performance.** Opt-in saved-query response caching (`cache_ttl`, see [Response
  caching](documentation/API.md#response-caching)) has no benchmark showing the actual hit-vs-miss latency
  delta or effectiveness under a realistic repeated-call pattern - only correctness is tested today
  (`tests/test_queryapigate.py`), not the performance claim caching exists to deliver.

**Notes:** same manual-not-CI methodology as the existing suite applies here too (a timing/memory benchmark
on a shared CI runner is inherently flaky, and none of these need checking on every push) - real subprocess
over real HTTP, not the Flask test client, sampled the same way `benchmarks/run.py` already does. Connection
pooling and cache benchmarks both need a *concurrent* client (multiple in-flight requests), which
`benchmarks/run.py`'s current single-request-at-a-time harness doesn't yet support and would need extending
for (e.g. a thread pool of clients hitting the same server, varying concurrency level as the independent
variable rather than row count/width). General API latency could reuse the existing harness almost as-is,
just against a small, fixed-size dataset instead of the current 1M-row one.

## 26. An API catalogue: one place showing what's exposed, and under what terms

**Status: shipped**, as an endpoint rather than an admin-UI tab - see the note below on why. `GET /catalog`
(`app.py`) joins, per saved query the caller can reach, exactly the governance facts OpenAPI has no field
for: `cache_ttl`, and `can_write` - whether *this specific caller* can write through this query
(`apikeys.can_write_query()`; per-query write curation means this can differ query to query even for the
same key). Alongside the query list, a `caller` object surfaces the calling key's own terms: `allow_writes`,
`allowed_write_ops`, its own `rate_limit`, and the server-wide `server_rate_limit` - both rendered back into
human form (`"100/minute"`) by a new `config.format_rate_limit()`, the inverse of the existing
`parse_rate_limit()`. `queries` is scoped by the exact same reachability rule `describe_saved_queries()`
already applies for `/openapi.json` (kept as its own loop rather than shared - the two describe different
things to different consumers, and OpenAPI's shape is a stable contract other tooling parses, not something
to risk changing by threading new fields through it). Unlike `/openapi.json`, `/catalog` is never public -
it requires a resolved caller like any other functional endpoint, since "what can *I* use" is meaningless
without one. Verified: reachability scoping (a query-scoped key sees only its queries, a connection-scoped
key sees every query on its connections), `cache_ttl` reporting `null` when unset rather than `0` or being
omitted, per-query write curation reflected correctly per query for the same key, the `caller` object's
fields (including that the admin key and a key with no rate limit of its own both report `rate_limit: null`
correctly), and that unauthenticated access is rejected the same way any other functional endpoint rejects
it. See [The API catalogue](documentation/API.md#the-api-catalogue).

**Why an endpoint, not an admin-UI tab:** the admin UI already lets an *admin* piece this same information
together today, query by query on the Saved Queries tab and key by key on the API Keys tab - there's no gap
for that audience. The actual gap `/catalog` closes is for a *scoped* key's own caller, who cannot reach
either of those admin-only tabs at all (see "the same 'only the admin key' message" noted under Admin UI) -
for them, the endpoint itself, consumable directly via `curl` or a script, *is* the catalogue. A UI tab
would only have helped the audience that already had this information.

## 27. Real-world example APIs under `examples/`

**Status: shipped**, in a different shape than sketched below - see the note at the end.

**Impact:** `examples/` currently ships two general-purpose sample databases (sakila, chinook) and two
generic saved queries (`actor_by_id`, `films_by_rating`) - enough to prove the mechanics work, but not
enough to show *why* someone would reach for this project over hand-rolling a controller. A newcomer
evaluating QueryAPIGate benefits more from seeing a named, complete scenario ("here's a reporting API," "here's
what a partner-integration key looks like") than from a single actor-lookup query.
**Notes:** add a handful of fully worked scenarios reusing the existing sakila/chinook data, each pairing a
saved query with the API-key shape that would realistically front it - e.g. a **reporting API** (a
read-only, rate-limited key scoped to a small set of aggregate queries, mirroring the `reporting` role
example already in [documentation/API.md](documentation/API.md#permission-roles-templates)), a **dashboard
data API** (several small saved queries meant to be polled), a **data export API** (a `?stream=true` query
against a large table, tying into [Streaming exports](documentation/API.md#streaming-exports)), and a
**partner/integration API** (a `queries`-scoped external-client key with an expiry, per [Per-saved-query
access](documentation/API.md#per-saved-query-access-external-clients)). Each should be a short, runnable
walkthrough (seed data already exists; add the saved-query JSON and the exact `curl`/admin-UI steps to wire
up its key) rather than a new dataset - the goal is showing intent, not multiplying fixtures.

**What shipped, and how it differs from the sketch above:** the four scenarios (reporting, dashboard, export,
partner) are real and runnable, but they are *loadable into any home* rather than files under the repo's `examples/`
folder - `queryapigate examples load | unload | status`, `QUERYAPIGATE_LOAD_EXAMPLES=yes` for containers,
`GET/POST/DELETE /examples` and a control in the admin UI (the model is Superset's: opt-in load, an environment
variable for containers, easy removal). That needed them to work from a `pip install`, which the repo's Sakila and
Chinook files cannot: those are not in the wheel and their licences were never resolved for redistribution. So the
examples run against a small SQLite database *generated locally* (~2 MB, 60 films / 200 customers / 20,000 rentals,
dated relative to the day it is generated) - a new fixture, which the sketch wanted to avoid, in exchange for nothing
third-party being shipped. The repo's `examples/` folder is unchanged and still the manual route. Each scenario is a
collection plus a role shaped like the key that fronts it (built from the collections work in #35); the walkthrough is
`documentation/EXAMPLES.md`.

**Strengthened afterward:** a fifth role, `example-executive` (`EXTRA_ROLES` in `examples.py`, deliberately
separate from the four `SCENARIOS` - nothing iterates it 1:1 with a collection), read-only and granted both
`examples-reporting` and `examples-dashboard` at once - the one access-model capability the four scenarios
never demonstrated on their own (a role/key spanning more than one collection).

The example database also grew from 3 tables to 8, at explicit follow-up request: `category`, `store`,
`staff`, `address` and `payment` (each with a real foreign key, except `category` - a deliberate plain
lookup table, joined into nothing, a contrast case), added purely additively so `film`/`customer`/`rental`'s
own rows and the `rng` sequence that produces them are byte-for-byte unchanged. `example_all_rentals` (the
export scenario) now joins six tables - `rental`, `film`, `customer`, `payment`, `staff`, `store` -
`payment` is exactly 1:1 with `rental` by construction, so its row count is still exactly `RENTAL_COUNT`,
unchanged. Still deliberately left out: a Mongo scenario (needs a real running server, unlike the generated
SQLite database, so it can't be a `load()`-and-it-just-works fixture the way the rest is); a second version
of an existing query (would need to change `load()`'s name-only idempotency tracking, real risk to a
load-bearing invariant `tests/test_examples.py` heavily exercises, for a cosmetic History-tab demo).
`documentation/EXAMPLES.md` points at the newer schema/parsing features using this real data: PK/FK badges
throughout the new tables, the Access tab's Query flow diagram on the new 6-table join, table-scoped
autocomplete, and real SQL pretty-printing.

**Safety properties, each guarded by a test that was confirmed to fail when broken:** everything installed is marked
`example` and removal deletes exactly that (never a query of yours that shares a name); loading refuses, changing
nothing, if something unmarked holds an example's name (a query, role, key, connection *or* the database file);
loading is idempotent (no extra versions, no extra history, no new keys on re-run) and completes an interrupted
load, reporting `partial` meanwhile. A startup problem under `QUERYAPIGATE_LOAD_EXAMPLES` is a logged warning,
never a startup failure, and a malformed value is rejected.

**Reversed afterward, deliberately: real example API keys and seeded request history, at explicit follow-up
request.** `load()` now creates one real API key per role (`role=<name>`, the same "create key from role" path
the admin UI uses) - this is a deliberate reversal of the "no key is created" property above, confirmed with the
user up front rather than an oversight: the moment these keys exist, `apikeys.auth_required()` flips true and
every request needs a key, everywhere, not just the example endpoints. Weighed and accepted: a demo an admin can
actually call immediately, with real per-scenario keys to copy into a client, was judged worth losing the
"stays open" default. Each key's secret is shown exactly once - printed by `queryapigate examples load`,
returned in `POST /examples`'s response, or logged once at server startup for `QUERYAPIGATE_LOAD_EXAMPLES`
(the only channel available there, with no interactive terminal watching) - and is genuinely unrecoverable after
that, the same rule every API key already has; a second `load()` creates no new keys and returns no new secrets.
A real security bug caught and fixed *before* it shipped, not after: the first draft passed `load()`'s full
return value (including the plaintext secrets) straight into `store.record_audit()`, which would have persisted
every secret into the audit log forever, visible on the Audit Log tab to anyone who can view it - fixed with a
`redact_for_audit()` helper that replaces the secrets with just the sorted key names before anything is
persisted, mirroring how a connection's password is already masked before an audit entry is ever written.

`load()` also seeds each freshly-created query's `execution_history` with 8-20 realistic-looking synthetic runs,
spread over the last 7 days, through `store.record_execution()` - the exact function a real request already
uses - so the admin UI's History tab, Home tab and per-query requests-per-day chart show something immediately
instead of staying empty until someone actually calls a query; a second `load()` never adds more, since only
queries created *this* call are ever seeded. Deliberately *not* extended to the in-memory `/metrics` counters or
the API Keys/Connections "Usage" columns - confirmed with the user up front - since those are documented,
genuinely live-traffic counters that reset on every restart; synthesizing them would only last until the next
one while quietly changing what they honestly mean. Verified directly, not assumed: a fresh restart immediately
after loading shows `/metrics` and every key's usage at zero, while the seeded `execution_history` (real SQLite
rows, unaffected by a restart) is still there in full - proving the two are genuinely independent, not a partial
leak in either direction.

**Deliberately not done:** hiding examples while keeping them installed (a filter across every listing surface is
exactly the kind of thing that drifts; removal is one explicit, exact operation instead), refreshing examples in place
when a newer release changes them (`unload` then `load`), and setting `QUERYAPIGATE_LOAD_EXAMPLES=no` to *remove* them
(a startup setting that deletes data would be a surprise).

*Follow-up:* the repo's own `examples/` folder (the Sakila and Chinook files, two saved queries) has since been removed
in favour of `queryapigate examples load`, so the repository carries no third-party data (0.7.0).

## 28. Package metadata improvements for PyPI discoverability

**Status: shipped.** `[project.urls]` gained `Documentation` (the MkDocs site) and `Repository` - PyPI's
project-page sidebar recognizes both as well-known keys and renders them as clickable links; previously only
`Homepage`, `Issues` and `Changelog` were set, so a visitor had no direct link to the docs site or a second
link to the source without going through the homepage first. Classifiers gained `Environment :: Web
Environment`, `Operating System :: OS Independent`, and per-version `Programming Language :: Python :: 3.9`
through `3.14` matching the CI test matrix exactly (PyPI's "Programming Language" filter facet uses these).
Verified with `python -m build` and `twine check dist/*` (both pass) and by inspecting the built wheel's own
`METADATA` file directly for the expected `Project-URL`/`Classifier` lines. Building the package surfaced an
unrelated deprecation warning (the `License :: OSI Approved :: MIT License` classifier), fixed alongside this
by moving to a PEP 639 SPDX `license = "MIT"` expression in place of the old `license = { text = "MIT" }`
table form and the now-redundant classifier - the build now emits `License-Expression: MIT` and bundles
`LICENSE` under the wheel's standard `licenses/` directory automatically.

**Deliberately left out:** a `Typing :: Typed` classifier (or a `py.typed` marker, PEP 561's mechanism for
actually shipping it). The project's own `[tool.mypy]` config says why this doesn't apply yet - "the
codebase has no type hints yet" and mypy's job here is catching structural errors like a subclass assigning
the wrong type to a base class's annotated attribute, not full annotation coverage. `Typing :: Typed` is a
promise to *consumers* that they get useful inline types when they import this package and run their own
type checker against it - not true today, so the classifier would be actively misleading rather than merely
optimistic. Worth revisiting once the codebase is more thoroughly annotated, not before.

## 29. A pre-built Grafana dashboard for `/metrics`, and a live in-app Metrics tab

**Status: shipped**, in a broader shape than originally framed - see the correction below. Two independent
pieces:
- **`documentation/grafana-dashboard.json`** - a ready-to-import Grafana dashboard against a Prometheus
  scrape of `/metrics`: request/query rate and latency (p50/p95/p99), error rate, rows returned, active
  queries, pool occupancy and rate-limit rejections. For deployments that already run Prometheus/Grafana,
  closing the gap between "the data exists" and "someone can actually see it" without every deployment
  building the same panels from scratch.
- **A "Metrics" tab in the admin UI** (`ui.py`) - stat tiles, two bar charts (requests by status, queries by
  connection) and a per-connection latency/error table, built by parsing `/metrics`' own Prometheus text
  client-side (`parseMetricsText()` and a handful of small aggregation helpers) - no new backend endpoint,
  no new state. For a deployment with no Prometheus/Grafana stack at all, this is the only visibility it has
  by default; needs no API key, since `/metrics` is already public.

**Correction to this item's original framing:** it said "deliberately *not* an in-app dashboard" and ruled
one out for duplicating Grafana and lacking the persistence a real dashboard needs. That reasoning holds for
a *historical* dashboard - trends over time, per-consumer breakdowns as their own analytics product - which
still correctly belongs to Grafana alone, not this project (see the "not recommended" entry on in-app
usage-analytics dashboards). A *live snapshot* - today's totals, no time axis, refreshed on demand - is a
different, smaller thing, already legitimized by #32's Usage columns: reading the same live, in-memory,
current-process numbers `/metrics` already has and formatting them for a human, once per tab load, is not
the same category of risk as building a second time-series dashboard. The tab's own subtitle says this
explicitly - "Live totals since this process started - no history, no trends" - so nobody mistakes it for a
replacement for the Grafana dashboard above, which remains the right answer for real observability.

## 30. Configurable, exportable long-term audit history

**Status: shipped.** `config.audit_log_limit()` (`QUERYAPIGATE_AUDIT_LOG_LIMIT`, default 500) replaces the
hardcoded `AUDIT_LOG_LIMIT` constant `store.record_audit()` used directly before - validated at startup
(`check_settings()`), the same pattern `QUERYAPIGATE_STREAM_MAX_ROWS` already uses, but always a positive count,
never "unbounded" the way the streaming cap can be: `audit_log.json` is read and rewritten in full on every
single audit event, so letting it grow without bound would make every administrative action progressively
slower, not just use more disk. `config.audit_log_export_file()` (`QUERYAPIGATE_AUDIT_LOG_EXPORT_FILE`), when
set, also appends every entry to a separate, append-only file - one JSON object per line, never capped or
rewritten - for retention a rolling cap can never satisfy regardless of how it's sized. The two writes in
`record_audit()` are independently wrapped (each still "never raises," the same trade-off it already made
for the primary write), so a problem with one - the export path's directory missing, say - never blocks the
other. A webhook-delivery option (also proposed in this item's original framing) was left out: it needs
network calls, retries and failure handling inside what has to stay a fire-and-forget, never-raising
function - a materially bigger feature than "write another line to a file," not the "both cheap" this item
was scoped as. Verified: the cap using the env var default and an explicit override, malformed values
rejected at startup, the export file accumulating every entry uncapped even while the primary log is capped
tightly, and each write's failure (a broken export path, a primary-write disk error) confirmed independent
of the other.

## 31. A CLI helper for cron-driven exports to a local file

**Status: shipped.** `queryapigate export <query> --format csv|tsv|ndjson --connection <name> --out
/path/{name}_{date}.csv` (`queryapigate/cli.py`). Runs entirely in-process against `QUERYAPIGATE_HOME` - no server
needs to be running, no HTTP round trip, no API key, since this is a trusted local operator with the same
reach the admin key already has. `{date}`/`{name}` placeholders in `--out` are resolved (and a typo'd
placeholder is rejected with a clear message rather than silently writing a literal `{bogus}` into the
filename); `--param name=value` (repeatable) supplies a required parameter, resolved through the exact same
`params.resolve()` validation a real request gets. Calls `engine.stream_sql()` directly - the same function
`stream_sql_response()` already calls internally for `?stream=true` - rather than shelling out to `curl`
against a running server, via a newly-factored-out `formats.iter_stream_chunks()` shared by both the HTTP
response path and this one. Writes to a `.part` temp file in the target directory and renames into place
only once the write completes, so a failed run - a missing query, a bad connection, a missing required
parameter, a malformed startup setting - never leaves a partial or stale file at the final path; exits `1`
for a data/query error and `2` for a startup-configuration error (matching `queryapigate serve`'s existing use of
that code), so cron's own failure handling (mail, whatever alerting the operator already has) works
unmodified. Local filesystem only, no new credential storage, no new dependency, no new daemon - exactly the
scope this item was framed for; see the "not recommended" entry on a full multi-destination scheduler for
why the fuller version doesn't fit this project. Verified: CSV/TSV/NDJSON output, placeholder resolution,
`--connection` overriding the saved query's own default, `--param` supplying a required value, and every
failure path (unknown query, bad connection, missing required parameter, malformed `--out` placeholder,
malformed server setting) confirmed to leave no partial or final file behind.

## 32. Surface each API key's and connection's own live usage in the admin UI

**Status: shipped.** Prompted by a screen-by-screen comparison against Langfuse's admin UI - its
Users/Sessions tables pair every identity with usage stats right next to their config, where QueryAPIGate's API
Keys and Connections screens were config/inventory-only (a key's row showed grants and, at best, a single
`last_used_at` timestamp). `metrics.summary_for_key(name)`/`metrics.summary_for_connection(name)`
(`metrics.py`) aggregate the same in-process counters `/metrics` already renders - queries run, of those how
many failed, and rows returned, plus an average query latency for a connection - and `GET /api_keys`/
`GET /connections` (`app.py`) now attach that as a `usage` object on each entry. The admin UI's Connections
and API Keys tables both gained a "Usage" column rendering it (`usageCell()` in `ui.py`), reading "No
activity yet" when a row has no activity since this process started rather than showing zeros. Deliberately
not a new dashboard or historical view (see the "not recommended" entries on in-app analytics dashboards) -
these are the same live, in-memory, current-process numbers `/metrics` already has, just read once per
admin-UI table load instead of via a separate Prometheus scrape; no new persistence, no time series, no
charting.

Two scope reductions from how this was originally framed, both driven by what `metrics.py`'s existing data
shape actually supports rather than invented afterward: a key's `usage` has no average latency, because the
request/query duration histograms are deliberately not split by key (to keep `/metrics`' bucketed output
from growing with the number of keys - see `observe_request()`'s docstring); a connection's `usage` has no
pool-occupancy figure, because `pool.py`'s idle-connection tracking is keyed by a fingerprint of connection
*details*, not by connection *name*, so today's `idle_count()` is necessarily pool-wide, not
per-connection - breaking that out would be a real `pool.py` change, not just an aggregation function, and
was left out of this pass. Verified: seven new tests (`tests/test_observability.py`) covering both
aggregation functions directly (multi-key/multi-connection isolation, zero-activity defaults, latency
averaging) and both list endpoints end-to-end through the Flask test client; a live headless-Chrome check
against a running server with real query traffic confirmed the rendered "N queries · N failed · Nms avg"
text matches actual activity and "No activity yet" shows correctly for untouched rows.

## 33. Close the gaps in the saved-query History tab: missing columns, no filtering, no drill-down

**Status: shipped**, in a narrower shape than originally framed - see the correction below. `renderHistoryTab()`
(`ui.py`) now shows "Caller" (`key_name`) and "Request ID" columns - `execution_history` already recorded
both (added for the log-correlation work closing #19), but the admin UI never rendered either, so that
traceability was invisible unless someone read the underlying JSON file directly. A status filter
(all/success/failed) and a search box (matching connection, caller, request ID and error text) sit above the
table, filtering client-side over the run history the page already has - no backend change, consistent with
`config.HISTORY_LIMIT` capping this at 50 entries per version to begin with.

**Correction to this item's original framing:** it claimed a failed run's error was only "truncated" to fit
a table cell, and that a drill-down would surface it in full "along with the bound parameters that produced
it." Neither survived contact with the actual code. The error message was never truncated - `x.error` was
already rendered as complete text in the DOM, just prone to forcing horizontal scroll on a long message
(fixed here with proper word-wrapping, not a new drill-down). The bound parameters, meanwhile, were never
captured *at all*: `run_saved()` computes them locally but never includes them in the dict passed to
`store.record_execution()`. Adding that was deliberately left out of this pass rather than done quietly - a
saved query's parameters can carry arbitrary caller-supplied values (a customer ID, an email address, ...),
and there is no existing precedent in this codebase for deciding which of those are safe to persist into a
capped-but-still-durable history file versus needing the same masking treatment `store.mask_passwords()`
gives connection passwords. That is a real design question, not a rendering gap, and belongs in its own
backlog item if it's wanted - not folded silently into a UI-only change.

## 34. Make the audit log scannable at volume: filtering and compact rows

**Status: shipped**, in a narrower shape than originally framed - see the correction below.
`renderAuditLog()`/`renderAuditChanges()` (`ui.py`) gained: an action-type filter (`<select>`, its options
built from whatever actions actually appear in the loaded entries, not a hardcoded list) and a search box
matching actor, target or timestamp, both filtering client-side over the entries the tab already fetched -
no backend change, `GET /audit_log` already returns everything needed. A create/delete snapshot's `changes`
now omits fields whose value is unset (`null`/`""`/`[]`) rather than always listing every field on the
entity - `create_key`'s audit entry, for example, no longer lists `allowed_ips: none`,
`allowed_write_ops: none`, `connections: none` and `created_from_role: none` when a key was created with
none of those set. An update's diff is unaffected, since `_dict_diff()` only ever includes a field that
actually changed - there was nothing to thin out there to begin with.

**Correction to this item's original framing:** its Impact example listed `allow_writes: false` alongside
genuinely-unset fields as something worth hiding. It isn't hidden, deliberately - `false` is a real,
meaningful value (confirming write access is off), not an absence of one, and the thinning rule implemented
here only ever drops a field that is actually empty/unset. The other proposed shape, a one-line "N fields
changed" summary that expands to the full diff on click, was left out in favor of the simpler
unset-field-omission alone: after hiding empty fields, a snapshot entry is already short enough (typically
3-5 lines) that a separate expand/collapse interaction wasn't adding much. The "date range" half of the
originally proposed filter bar was also folded into the plain search box (it already matches against each
entry's timestamp substring, e.g. typing "2026-09-25" narrows to that day) rather than building a dedicated
date-range picker for what is, even before #30's higher cap, a capped-at-500-entries list.

## 35. Collections: group saved queries, grant access to a whole group, move and share them as a unit

**Status: shipped.**
**Impact:** a key's `queries` grant is a literal list of names, so giving a partner access to 40 queries means
maintaining 40 names by hand and editing the key every time a query is added; the Saved Queries list is flat, so
finding and managing queries gets harder as their number grows; and there is no unit to move or share. A
**collection** is one deliberately named group a query belongs to (at most one - unlike tags, which are
multi-valued and cross-cutting), and the unit for grants, bulk changes and export/import.

**Design decisions - each chosen so two sources of truth can never disagree:**
- **Stored on the query file itself** (a top-level `"collection"` next to the version numbers), not in a separate
  registry: deleting the query deletes its membership, a move is one atomic file write, and there is no orphaned
  or out-of-sync mapping to reconcile. A collection exists exactly while at least one query is in it. A move does
  **not** create a new version (a collection belongs to the query, not to a version).
- **One canonical name form**, lowercase `[a-z0-9._-]`, *rejected* rather than silently case-folded - so
  `Reporting` and `reporting` can never both exist.
- **A key's `collections` grant is additive and read-only**, like `queries`: it reaches every query currently in
  those collections and never ad-hoc SQL, and there is deliberately **no** `"*"` wildcard and no write access via a
  collection (a write grant must stay spelled out per query). Names are validated against existing collections
  when granted, so a typo is an error, not a silent grant of nothing.
- **Reachability is one function** (`apikeys.can_run_saved()`), used by the run path, `/openapi.json` and
  `/catalog` - previously the same rule was written out three times, which is exactly how a new grant type drifts
  out of one of them. A test asserts all three surfaces agree across every grant type.
- **Grants are live** (unlike roles, which are copied once): that is the point of the feature, so its cost is
  made visible instead - moving a query in or out is audited with the keys that gain or lose access, and
  `GET /collections` lists which keys and roles reach each collection.
- **Rename is resumable, never destructive**: grants are widened first (old and new), queries moved, old removed;
  an interrupted rename loses no access and re-running it completes it.
- **Export/import** (`queryapigate collection export|import`) validates through the same code as saving a query
  and preflights the whole bundle before writing anything; a bundle carries definitions only - never execution
  history, keys or connection credentials.

**Verified:** membership/grant behaviour end to end (live grants, no wildcard, no writes, no ad-hoc SQL, typo
rejection, role copy semantics, keys stored before the field existed); the run path, `/openapi.json` and
`/catalog` agreeing for every grant type; every route being in the OpenAPI spec; rename interrupted before and
during the move loses no access and completes on re-run; import atomicity and each conflict policy. The tests
that guard against drift were confirmed to fail when the property they protect is deliberately broken.

**A pre-existing hole found while building this, fixed here:** a key granted a saved query by name could add
`?connection_name=` and run that query's SQL against *any* active connection, with no connection access at all -
the `queries` grant was checked, the connection it ran on was not. The new collection grant would have inherited
it, so both now authorise the query only on its own connection (see the CHANGELOG `Fixed` entry).

**Postman export and "New collection" (added after the first cut):** `GET /collections/<name>/postman`, the CLI's
`--format postman` and a UI button generate a Postman Collection v2.1 file. Its examples are checked against the
server's own parameter validation - a first version of that test passed vacuously (it handed `resolve()`
already-normalised definitions, which silently disable every rule) and was only caught by deliberately breaking
the generator and seeing the test *not* fail; it now asserts values come back typed and a companion test proves
the check can reject. Not verified inside Postman itself. **New collection** in the UI takes a name plus the first
queries to file, since an empty collection cannot exist by design.

**Deliberately not done:** hierarchical (nested) collections, a collection registry with its own lifecycle,
write access via a collection, an HTTP export/import (the CLI is in-process like `queryapigate export`), and
exporting more than each query's latest version. Each is a larger change than the problem needed.

## Not recommended without a specific hard requirement

### Parameter-value-level restrictions per API key

Something like "this key may call `customer_lookup`, but only with `customer_id=42`" - row/value-level
security scoped to a specific key. A fundamentally different, much bigger problem than #1/#14-#18 (which
all restrict *which* connections/queries/writes a key can reach, never *which rows or values* within an
allowed one) - it needs per-parameter, per-key rule evaluation on every call, a rule language or UI to
define it, and gets complicated fast once a query has several parameters or the restriction needs to be
"any of these values" or "less than N" rather than one fixed value. Out of scope unless a concrete need
for it shows up; the existing per-query and per-connection granularity already covers the common real
case ("this client should only ever see their own data") by simply saving a query that hard-codes the
tenant/customer identifier server-side rather than accepting it as a caller-supplied parameter at all.

### A native JVM server port

Rewriting the whole service (connection handling, pooling, rate limiting, OpenAPI generation, and -
hardest of all - the SQL guard) as a second, independent implementation on the JVM. Only worth it for a
concrete constraint like "must run with zero Python in the environment" - nothing on this list is
currently that constraint.

If this is ever pursued, **do not re-derive the SQL guard from scratch.** The dialect-aware backslash vs.
doubling-only string-literal parsing in `queryapigate/sqltools.py` took real access to live MySQL/ClickHouse
servers to get right (see the "Fixed" entry under Unreleased in [CHANGELOG.md](CHANGELOG.md)) - a second,
independently-written parser is exactly the kind of thing likely to quietly reintroduce that class of bug.
Export the cases in `tests/test_sql_guard_fuzz.py` (including the real-server-verified regression) as a
language-agnostic `{sql, dialect, expected}` conformance file, and require any new implementation to pass
it before it's trusted - the same technique used for things like Unicode normalization tables or TLS test
vectors.

### In-app *historical* usage-analytics dashboards (trends over time, per-consumer breakdowns as their own product)

`/metrics` already labels request/query counts and latencies by calling key name, so a per-consumer
breakdown already exists as a Prometheus label query - see [Observability](documentation/API.md#observability).
Building a second, in-app analytics UI with real history and trend lines on top of that would duplicate
Grafana rather than complement it, and would need real persistence this project deliberately doesn't have
(metrics are in-memory, single-process - see the note at the end of the Observability section). This is
distinct from what #29 ships - a live, no-history *snapshot* view (today's totals only, explicitly labeled
as such) is a much smaller thing than a historical dashboard, and doesn't need the persistence this rejection
is actually about; see #29's own "correction" note for exactly where that line sits. #29's Grafana dashboard
remains the right answer for real trends, alerting and per-consumer history - without turning QueryAPIGate itself
into a dashboard product.

### Cost/usage reporting

There is no generic way to attribute a dollar cost to a query: this is a database-agnostic gateway, and
"cost" depends entirely on what's behind a given connection - a free local SQLite file, an on-prem MySQL
box, or a cloud warehouse billed by credits or IOPS. Any cost model built in here would be wrong for most
deployments and right for none in particular. Belongs in the surrounding cloud/database billing tooling,
which already has the pricing data this project has no way to know.

### Alerting

Exactly what Prometheus Alertmanager (or Grafana's own alerting) already does, and `/metrics` is exposed in
the standard exposition format specifically so that stack can consume it. Building thresholds, notification
channels (email/Slack/webhook) and silencing into a single-process gateway is a large, mature product
category on its own, orthogonal to what QueryAPIGate is for - the same shape of "not recommended" as the native
JVM port above.

### Anomaly detection

Needs a statistical baseline of normal traffic built up over time, which needs real persistence across
restarts and workers - the in-memory, single-worker metrics this project deliberately keeps (see the note at
the end of [Observability](documentation/API.md#observability)) are the wrong foundation for it, and getting
it wrong has a real cost (false positives train people to ignore alerts; false negatives miss the thing that
mattered). Better served by dedicated APM/observability tooling that specializes in this problem.

### A built-in multi-destination scheduler (cron/timezone config, S3/SFTP/FTP/WebDAV delivery, retry and
retention policies, an execution-history/monitoring UI)

Conflicts directly with this project's core architecture, not just its current scope: every design decision
so far - metrics kept in-memory for one process, production guidance is explicitly `gunicorn --workers 1`
"because saved-query and connection files are protected by an in-process lock," the audit log and rate
limiter both intentionally non-persistent beyond a JSON file - assumes there is no durable job store and no
long-lived background process outside of handling an HTTP request. A scheduler that has to survive restarts
without double-firing, retry failures, and keep durable execution history is a fundamentally different
runtime shape, needing exactly the persistence and coordination this project has deliberately avoided
everywhere else. It also duplicates mature tooling that already solves "run this reliably, retry it, alert on
failure" well - cron, systemd timers, Kubernetes CronJob, Airflow - the same shape of "not recommended" as
Alerting above. Each additional destination (S3, SFTP, FTP, WebDAV) is also a new credential type needing the
same encryption-at-rest treatment #24 just gave connection passwords, and Parquet output needs `pyarrow`, a
heavy new dependency for a single format. #31 above gets the actual common case - a file dropped on a
schedule - without any of this: local filesystem only, no new daemon, scheduling/retry/alerting left to tools
that already do it well.

## 36. Broader database backend support

**Status: MongoDB shipped as a find-only slice; MSSQL/Oracle/Redis/Snowflake/BigQuery not started.** Raised
as "support MySQL Workbench, DBeaver, etc." - worth being precise about what that actually means: those are
GUI *clients*, not protocols, and they already work today, because they talk to a real MySQL/PostgreSQL/etc.
server over that database's own wire protocol - the same server a QueryAPIGate connection of that type points
at. QueryAPIGate is never in that path at all: it is the thing being queried *through* (a REST API in front
of a database), not a database a desktop client opens a session with. So there is nothing to add for those
tools by name; the real, useful version of this request is **more of the already-supported dialects'
cousins**: `SUPPORTED_DB_TYPES` today is `mysql, postgres, clickhouse, sqlite, h2, jdbc, duckdb, mongo`
(`queryapigate/config.py`) - MariaDB rides in under `mysql` and any JDBC-reachable database already rides in
under `jdbc`, but there is no native driver (its own runner in `queryapigate/runners.py`, its own
`schema.py` catalogue query, its own connection-form fields) for **MSSQL/SQL Server, Oracle, Redis,
Snowflake or BigQuery** - each a real, commonly-requested backend on its own merits.

**Impact:** the project's pitch is "expose *your* database as a governed REST API" - every dialect it can't
speak is a team it can't help at all, not a team that gets a worse experience.

**MongoDB (shipped, find-only):** a `mongo` connection type, ad-hoc and saved `find()` queries (`POST
/execute_mongo`, saved queries with `query_type: 'mongo'`), collection listing in the Schema tab (as
`{'type': 'collection', 'columns': []}` - no field-level introspection, since collections are schemaless),
and database listing/switching (native to Mongo, unlike the SQL dialects' catalogue-query approach). See
`queryapigate/mongotools.py` (JSON-document placeholder substitution and the `$where`/`$function`/
`$accumulator` guard - the non-SQL sibling of `sqltools.py`) and `engine.execute_mongo()`. Deliberately not
included, each its own separately-scoped follow-up: aggregation pipelines, insert/update/delete (there is no
write path at all yet, not even behind `QUERYAPIGATE_ALLOW_WRITES` - see `engine.execute_mongo()`'s
docstring), per-field schema sampling, `cache_ttl` for a saved Mongo query, streaming (`?stream=true` 400s
for one), a dedicated query-builder UI (Run SQL and the saved-query form currently use one JSON textarea -
`{"collection", "filter", "projection", "sort"}` - rather than separate form fields), and Postman export
(a saved Mongo query is silently excluded from `queryapigate/postman.py`'s output, same as it always was for
anything without a `sql_query`).

**Notes (remaining dialects):** each is a real, separately-scoped unit of work, not one ticket - a new
`_Driver` subclass in `runners.py` (`connect`/`query`/`stream`, its own parameter style and literal-quoting
rules for `sqltools.py`'s guard), a schema-introspection query in `schema.py` (MSSQL/Oracle have a normal
`information_schema`-shaped or close-enough catalogue, same shape MongoDB's `fetch_schema()` branch proves
out for a non-relational one; Redis has no schema concept at all - "list tables/columns" would need to be a
no-op or something else entirely), a new admin-UI form field set if the dialect needs something the current
form doesn't ask for, and a new optional dependency group in `pyproject.toml` (mirroring how
`mysql-connector-python`, `psycopg2`, `pymongo` etc. are already optional extras, so installing QueryAPIGate
never pulls in every driver for a dialect nobody uses). MSSQL and Oracle are the most natural next two (both
relational); Redis (non-relational, no query language to speak of) and Snowflake/BigQuery (cloud warehouses
with their own auth models - key pairs, service accounts) are each a bigger, more different-shaped addition
and worth scoping separately rather than bundled into "add a driver."

## 37. Primary/foreign key markers in the schema browser's Columns tab

**Status: shipped for `mysql`/`postgres`/`sqlite`/`duckdb`.** The Columns tab (`schema.py`'s `fetch_schema()`,
rendered by `ui.py`'s `schemaBrowser()`) now shows a "PK" badge and a "FK → table.column" badge next to a
column's type - exactly the two facts that matter most when deciding how to `JOIN` against a table you're
seeing for the first time.

A new `_KEY_QUERIES` per dialect (`schema.py`), run as a second, additive query and merged onto the columns
the base `_QUERIES` already built - wrapped in a `try/except` so a permissions error or an unexpected
catalogue shape degrades to "no badges" for that connection, never breaks schema browsing that already
worked (the same discipline `sqlflow.py` established). SQLite folds straight into its *existing* query
instead (`pragma_table_info()` already returns a `pk` flag, `pragma_foreign_key_list()` joins cleanly) -
verified for real against `sqlite3`. DuckDB is also verified for real, against an actual `duckdb` file
through this project's own `engine.execute_sql` pipeline - worth noting since a first attempt exposed a real
gotcha: `duckdb_constraints()`'s `LIST`-typed columns get stringified by this project's pipeline rather than
passed through as Python lists, so the query indexes the first array element in SQL itself
(`constraint_column_names[1]`) rather than post-processing a string. MySQL and Postgres use standard,
well-documented `information_schema` patterns but are **not independently verified against a live server in
this environment** (no embedded/pure-Python path the way SQLite/DuckDB have) - worth a real smoke test
before leaning on them hard.

**Deferred, explicit gaps, not bugs:** **H2** is left out entirely - its `information_schema` constraint
shape isn't verifiable here (no JVM/JDBC access), and a guessed query risks a silently-wrong badge, a worse
failure than no badge. **ClickHouse** still reports neither - no real foreign-key concept, and its primary
key is an informational `ORDER BY`-style table clause, not a per-column constraint the others report the
same way. **Composite (multi-column) keys** report only their first column (Postgres's `constraint_column_usage`
join and DuckDB's `constraint_column_names[1]` both have this limitation) - a composite key still gets a
badge, just not a fully accurate multi-column one.

## 38. A "show CREATE TABLE" icon in the schema browser

**Status: shipped for `mysql`/`sqlite`/`clickhouse`**, exactly the split this entry originally scoped -
Postgres/H2/DuckDB/Mongo remain the documented, deliberate remainder below. A new ⌸ icon next to the
existing copy-starter-query (⧉) and preview (👁) icons in Run SQL's schema browser shows a table's real
`CREATE TABLE` text (`GET /connections/{name}/table_ddl`, `schema.fetch_table_ddl()`).

SQLite is free - `sqlite_master.sql` already *is* the original DDL text, fetched with this project's own
`:name` bound-parameter convention (`table_name` is a genuine bound value, never interpolated). MySQL and
ClickHouse each have a single `SHOW CREATE TABLE` statement, verified as real syntax but **not
independently tested against a live server in this environment** (no embedded path for either, the same
honesty note #37's MySQL/Postgres key-query work already carries). Since no driver supports parameter-
binding for an identifier the way it does for a value, the real safety mechanism is an existence check:
`table_name` (arriving on the request) must already be a table `fetch_schema()` itself reported before any
DDL query is ever built - a plain identifier-character regex runs as defense-in-depth after that, not
instead of it. Verified directly: a real SQLite table's DDL comes back correctly, a nonexistent table name
and a SQL-injection attempt both get rejected as 404 before reaching a SQL string (confirmed the target
table is untouched afterward), and an unsupported dialect gets a clear 400, not a crash.

Only wired into Run SQL's own schema browser, not the saved-query form's embedded one - that one already
lives inside the shared drawer this feature also needs, and opening it there would clobber the very form
it's shown inside.

**Deferred, not a gap:** PostgreSQL has no built-in single-statement equivalent - real DDL needs
reconstruction from `pg_catalog`/`information_schema` (columns, defaults, indexes, constraints each their
own query), the well-known reason third-party tools like `pg_get_tabledef` exist - a real follow-up, not
this slice. H2/DuckDB are left out the same way they were for #37 - unverified completeness in this
environment. Mongo has no DDL at all (schemaless) - the icon is already hidden for it, along with every
other unsupported dialect.

## 39. Autocomplete columns while typing in the SQL editor

**Status: shipped as its table-scoped slice only.** Typing `alias.` or `table.` (a table already named in a
`FROM`/`JOIN`) in either SQL editor (`$('run-sql')` in Run SQL, `$('q-sql')` in the saved-query form) now
pops up that table's columns, filtered as more letters are typed, accepted via Enter/Tab/click, dismissed
via Escape/click-away. `extractTableAliases()`/`attachColumnAutocomplete()` (`ui.py`) resolve the alias with
a lightweight `FROM`/`JOIN` regex - not `sqlflow.py`'s real parser, which is scoped to finished, saved SQL
and would fail constantly against text that's routinely mid-keystroke - filtered against `SQL_KW` so a bare
`FROM t WHERE` doesn't misread `WHERE` as an alias. Columns come from the existing `schemaCache` (no new
backend work); caret placement uses the classic hidden-mirror-div technique; the popup's keydown handler
runs in the capture phase so it can `stopImmediatePropagation()` and safely pre-empt `bindEditor()`'s
existing Tab-inserts-two-spaces behaviour while it's open. No popup at all for a Mongo connection's JSON
editor, and no popup when the alias doesn't resolve (never falls back to an unscoped "every column" dump).

**Deferred:** the "dumb" flat suggestion (every column across every table, suggested anywhere while typing,
not just after `alias.`) - still a distinct, separately-scoped trigger design, and still blocked for Mongo
specifically on the same schema-sampling gap BACKLOG #36 already notes (v1's Mongo schema is collection
names only).

## 40. Real SQL parsing for the Access tab's "Query flow" panel

**Status: shipped as a table/join-extraction v1 only.** Until now the only "which tables does this query
touch" signal anywhere in the app was `computeTableMatches()` (`ui.py`), a client-side word-boundary text
search over the raw SQL - explicitly documented in its own comment as "not real parsing," with known false-
positive/negative risk from aliases, comments and string literals. The user wanted a detailed per-query
visualization on the saved-query detail view's Access tab (shipped in 0.9.0); that need was the forcing
function for finally adding a real parser rather than building more UI on top of the heuristic.

New `queryapigate/sqlflow.py` parses a saved query's SQL with `sqlglot` (new `flow` extra) and extracts its
tables and joins; `GET /query_flow` serves it. Never touches `sqltools.validate_sql` or any execution path -
a parse failure just shows an explanatory line, never blocks or raises.

The Access tab's Query flow panel shows this as a real node-link diagram - tables (with join edges between
them) flowing into the query, flowing out to the keys/roles that can call it (`queryReach()`, already shared
with `accessBox()`/the API Keys/Roles tabs, so this can never disagree with them) - the detailed per-query
visualization originally asked for. Still no new charting/graph-rendering dependency, per the user's explicit
choice: `renderFlowDiagram()` (`ui.py`) renders the node boxes as ordinary HTML (reusing `.tag`/`accessPill()`
exactly, so styling matches everywhere else) with a single thin SVG layer just for the connecting lines,
positioned from each node's real `getBoundingClientRect()` after layout rather than hand-rolled position math
- matches this project's stance of bar rows over charts elsewhere. Capped at 10 nodes per column with a
"+N more" hint pointing at the full lists already on this same tab, rather than an unbounded diagram.

The Access map screen's own "which queries reference this table" filter now also uses real parsing where
the connection's dialect supports it (the same `PARSEABLE_DIALECTS` list, client-side in `ui.py`):
`computeTableMatches()` calls `GET /query_flow` per candidate query and matches against its real `tables`
list, falling back to the original text-search heuristic per-query when parsing isn't available for the
dialect or fails on that query's SQL - so h2/jdbc/mongo connections, and any unparseable query, keep working
exactly as before. Shares `queryFlowCache` with the Access tab's own panel, so the two can never disagree.

`formatSql()` (the SQL tab and the Access map's query info popup, used whenever a saved query's stored SQL
has no line breaks of its own) now also prefers a real `sqlglot` pretty-print, via the same `GET
/query_flow`'s new `formatted` field, when the dialect supports it and the query has no legacy `{name}`
brace placeholder (`sqlglot` has no notion of that convention and misparses it) - `formatSql()`'s original
token-based reflow remains the fallback for everything else. `sqlglot` parses this project's `:name` bound
parameters natively, so they round-trip unchanged in the pretty-printed output.

**Deferred, each its own separately-scoped unit of work:** column-level lineage; real write-target detection
(the #21 table-allow-list note above still applies - this is read-only best-effort, not a security guard).
(Table-scoped autocomplete, once flagged here as a possible use of this parser, shipped instead via a
lightweight regex - see #39 - not this module; `sqlflow.py` stays scoped to finished, saved SQL.) **H2/JDBC
connections are permanently out of scope for real parsing**, not a gap to fill later - those can point at
any vendor's SQL over a generic bridge, and there is no "generic JDBC" sqlglot dialect to guess at; both
keep using the text-search fallback everywhere it applies.

## 41. A Home tab for the admin UI

**Status: shipped.** Opening the admin UI dropped straight onto the Connections table (or wherever a
returning session last was, via `sessionStorage`) with no at-a-glance sense of what's on the server or what
happened recently. A new first tab, "Home" - stat tiles (connections active/total, saved queries, API keys,
roles, requests, error rate), the 5 most recent audit log entries (click one to jump to its likely tab, via
a plain substring match on the action name - `homeActivityTarget()`), and quick actions (Run SQL, New saved
query, New connection, Help).

No new backend endpoint: `refreshAll()` already loads connections, saved queries, API keys, roles, the
audit log and metrics on every page load regardless of active tab, so `renderHome()` (`ui.py`) is purely a
new view over caches those loaders already populate, plus one small addition - `loadMetrics()` now keeps
its parsed series in a module-level `metricsSeries` rather than handing it straight to `renderMetrics()`
and discarding it, so Home's requests/error-rate tiles cost no second fetch. `renderHome()` is called from
the tail of each of the six loaders it depends on (the same cross-cutting-refresh pattern `loadAuditLog()`
already used to keep the Connections screen's Deleted tab in sync), so it can never show stale numbers
regardless of which loader finishes first. Becomes the default landing tab for a fresh session; a returning
user's remembered last tab still takes over exactly as before.

**Strengthened afterward:** a "Recent API requests" panel, auto-refreshing every 5 seconds while Home is
the visible tab (`pollRecentRequests()`, a lightweight `GET /list_files` poll - not the full `loadQueries()`
pipeline, which also drives the Saved Queries screen's own state/DOM). Built entirely from
`execution_history`, already returned in full by `GET /list_files` (`store.list_saved()`) and already used
by the per-query History tab - `aggregateRecentExecutions()` just flattens it across every saved query and
version. There is no true in-flight-request tracking anywhere in this app (metrics only exposes an
aggregate *count*, `queryapigate_active_queries`, not per-request identity), so this is deliberately a
recent-**completed**-requests tail, not a live in-flight view - and only saved-query runs through
`/q/<name>` are recorded this way, not ad-hoc `/execute_sql` calls from Run SQL, which have no saved query
to attach a history entry to.

## 42. An MCP server exposing saved queries as tools

**Status: shipped, for read-only saved queries only** - `queryapigate mcp` (needs
`pip install "queryapigate[mcp]"`, and Python >= 3.10, the `mcp` package's own floor, higher than this
project's own `>=3.9`) starts an MCP server, on its own port (`QUERYAPIGATE_MCP_PORT`, default 5001),
separate from `queryapigate serve` - MCP's Streamable HTTP transport is ASGI-native, this app is WSGI (Flask
on gunicorn, `--worker-class gthread` per #43), and bridging the two into one process would have meant
either an ASGI-capable worker for the *whole* app or a WSGI/ASGI bridge with its own streaming edge cases;
a separate process is the same shape "run gunicorn for the API" already is, not a change to how the REST
API is served. What actually matters for correctness - reusing `apikeys.py`/`store.py`/`app.run_saved()` by
direct Python import, not an HTTP round-trip back to the REST API - holds regardless of the process split.

Built exactly as this entry's own notes scoped it: `tools/list` (`mcp_server.list_tools_for()`) reuses the
exact same `apikeys.can_run_saved()` scoping `GET /catalog` already enforces, computed fresh on every call
(never cached - two different API keys reach different queries, proven by a test asserting `tools/list` and
`/catalog` always agree, not by hand-duplicating an expected set); `inputSchema` reuses `params.json_schema()`
verbatim, the same translation `/openapi.json` already uses. `tools/call` (`mcp_server.call_tool_for()`) runs
a query through `app.run_saved()` itself - the exact function `GET /q/<name>` uses - via a synthetic Flask
`test_request_context()` rather than a parallel execution path, so the connection-grant check, param
resolution, `cache_ttl` caching, `execution_history` recording and #43's live Home-tab broadcast all fire
exactly as they do over REST (verified: a `cache_ttl`-carrying query's second identical call is provably
served from cache - `execution_history` gains exactly one entry across both calls, not two - and a query run
via MCP shows up in the REST server's own History tab immediately, checked against two servers actually
running side by side against the same `QUERYAPIGATE_HOME`).

**Only read-only saved queries become tools** (`sqltools.first_keyword() in READ_ONLY_STATEMENTS`, plus any
Mongo `find()`, always read-only) - a write-capable query stays reachable over REST as before, per the scope
decision made with the user up front: exposing a write over MCP needs a `destructiveHint` classification this
codebase has no existing constant for (see the notes below, unchanged) and a considered decision on
confirming an LLM-initiated write before it executes - real, separately-scoped follow-up work, not bundled
into this first slice. A tool call's result is capped at `QUERYAPIGATE_MCP_MAX_ROWS` (default 200 rows),
independent of the REST API's own `QUERYAPIGATE_MAX_PAGE_SIZE` default, since an LLM's context can't hold a
large result the way a human paging through the admin UI can; a caller-supplied smaller `page_size` is still
honored. A failed call is reported as a normal MCP result with `isError: true` (translating `ApiError` at
the one MCP boundary, per this entry's own "structured tool errors" note below), not a transport-level error.

Verified against the full test suite (840 tests, including a real end-to-end test with the actual `mcp` SDK's
own client talking to a real running server - not just `tools/list`/`tools/call` shapes checked in isolation),
ruff/mypy, and a real CLI run with `queryapigate mcp` and `queryapigate serve` running side by side against
one `QUERYAPIGATE_HOME`, called from a real MCP client.

**Two ad-hoc tools shipped as a follow-up**: `list_tables` (schema discovery) and `execute_sql` (read-only
ad-hoc SQL), not tied to any saved query - the gap between "an agent can call queries a human already
saved" and "an agent can work with a connection directly." Both gated by the same `connections` grant
REST's `/connections/<name>/schema` and `/execute_sql` already check, reusing those exact permission checks
and execution functions (`schema.fetch_schema()`, `engine.execute_sql()`) rather than a parallel path.
`execute_sql` is hard-forced read-only regardless of the calling key's own `allow_writes` grant - the same
read-only-over-MCP rule saved queries already follow, now applied to ad-hoc SQL too - and still honors a
key's `allowed_tables` restriction if it has one. See
[documentation/MCP.md](documentation/MCP.md#ad-hoc-tools-list_tables-and-execute_sql).

**Making a saved query a genuinely first-class MCP tool, not just a REST endpoint wrapped in MCP's shape:**
real MCP tool concepts, and what each needs from a saved query's current definition - shipped/open status
per item, now that v1 is built:

- **`description`** - **shipped as-is.** Reused verbatim from the saved query's own `description` field for
  v1 - still written for admins, not the model, but no new field invented for this slice.
- **`inputSchema`** - **shipped.** `query_parameters` -> JSON Schema via `params.json_schema()`, the same
  translation `/openapi.json` already used. Zero new data, as scoped.
- **`outputSchema`** - **shipped, as a generic result-envelope schema, not per-column.** Every row-returning
  tool (a saved query, `execute_sql`) declares `{"rows": [...], "truncated": <bool>}`; `list_tables`
  declares `{"tables": [...], "truncated": <bool>}`. Every successful call now also returns a matching
  `structuredContent`, verified round-tripping through the real `mcp` SDK's own client, not just this
  module's own dict shape. **Breaking change** to the text-content shape (a bare JSON array with an
  ad-hoc truncation suffix string, before this) - flagged under this project's own versioning policy (see
  CHANGELOG.md's "Versioning and compatibility" section). Real per-column types remain un-started design
  work if ever wanted - a saved query's result columns are still only known once it runs, not from its
  stored definition, and declaring them accurately would need a genuinely new admin-authored field.
- **Annotations (`readOnlyHint`/`destructiveHint`/`idempotentHint`)** - **`readOnlyHint`/`idempotentHint`
  shipped** (both always `True` for the read-only-only v1 tool set). **`destructiveHint` still not built** -
  moot for now since nothing destructive is exposed at all; still no `DESTRUCTIVE_STATEMENTS` constant to
  reuse if/when write-capable tools are added.
- **Structured tool errors** - **shipped.** `ApiError` is translated to `isError: true` at the one MCP
  boundary (`mcp_server.call_tool_for()`), not left as a transport-level HTTP status.
- **Result size** - **shipped.** `QUERYAPIGATE_MCP_MAX_ROWS` (default 200), independent of the REST API's own
  page-size default, with a truncation note in the result text.
- **Elicitation for writes** - **still open**, since no write-capable query reaches MCP at all in this
  version - the confirmation-flow decision this note flags stays exactly as open as it was, deferred to
  whatever follow-up adds write support.

Net, updated: everything scoped as "close-to-free reuse" shipped, plus the two ad-hoc tools and a generic
`outputSchema`/`structuredContent`. `destructiveHint` and elicitation for writes remain the real new design
decisions this entry always said they'd be, both still open because both are only meaningful once
write-capable tools exist - not needed for a read-only-only version.

## 43. Server-Sent Events (SSE) for live updates, instead of polling

**Status: shipped**, for exactly the one consumer this entry's own note flagged as the place to start: Home's
"Recent API requests"/"Slowest queries" panels, which used to poll `GET /list_files` (the whole saved-queries
catalog) every 5 seconds. A new `GET /events` (admin only) streams one `data: {...}` line per saved-query
execution as `store.record_execution()` records it (see the new `app._record_and_broadcast()` wrapper around
every one of its call sites - `run_saved`, `run_saved_mongo`, and the streamed-export history recorder), and
the client patches the change straight into its already-loaded `filesCache` instead of refetching - a new row
now appears in well under a second instead of up to 5. Falls back to the old poll if the stream can't be
reached or drops (fail-open, matching this app's usual treatment of a side channel failing).

Built exactly per this entry's own prior correction: a new in-process `Broadcaster`
(`queryapigate/broadcast.py`, one instance per Flask app in `app.extensions`, a bounded `queue.Queue` per
subscriber that drops rather than blocks a stuck client) - correct for the documented single-process
deployment, no Redis needed for correctness. Given the same swappable-backend shape `cache.py`/`rediscache.py`
already established for the response cache (`subscribe()`/`unsubscribe()`/`publish()`), so a future
Redis-pub/sub variant is a clean addition if QueryAPIGate is ever scaled to several *instances* behind a load
balancer - not built now, since nothing needs it yet.

One real blocking bug found and fixed along the way, not previously known: the Dockerfile's gunicorn command
was `--workers 1 --threads 8` with no `--worker-class` set, so it silently ran gunicorn's default `sync`
class - `--threads` only takes effect under `gthread` (or another threaded/async class), so the existing
`--threads 8` flag had never actually been doing anything. Under `sync`, a long-lived SSE connection would
have blocked every other request to the single worker for as long as that one client stayed connected -
`--worker-class gthread` was added to close this before shipping SSE at all, verified against a real
container (a held-open `/events` connection no longer delays a concurrent `/health` request).

The browser's native `EventSource` API cannot set the `X-API-Key` header every other admin request already
uses (and this app has no cookie-based auth to fall back on), so the client reads the stream with `fetch()`'s
streamed response body instead - same wire format, just read manually, so header-based auth keeps working
unchanged rather than putting the key in the URL. Every other item this entry originally listed as a future
consumer (the example dashboard's KPI queries, Metrics' manual refresh) is still poll/manual-refresh-based,
not converted - the event's own shape (`{'type': 'execution', ...}`) leaves room for a future `'type': 'audit'`
or similar to reuse the same connection, but nothing beyond saved-query executions is wired up yet.

## 44. Home tab: surface real health signals, not just counts

**Status: shipped**, exactly the cheap slice this entry scoped - see below for what's still deferred.
Home (#41) showed counts (connections, saved queries, keys, roles, requests, error rate) but nothing that
flagged "something needs attention" - the whole point of a health-focused landing tab. Every signal below
was already computed or loaded somewhere else in the app; this only needed reusing it on Home.

- **Three more stat tiles, zero new fetch.** `renderHome()` already parsed `metricsSeries` for its
  Requests/Error rate tiles - `queryapigate_active_queries` (queries genuinely running right now, the one
  truly live signal available), `queryapigate_pool_idle_connections` and
  `queryapigate_rate_limit_rejections_total` were already computed by `renderMetrics()` for the Metrics tab
  from that same series, just not read by Home yet.
- **A "System health" panel**, placed right after the stat tiles (before Recent activity/Quick actions) -
  flags expired API keys (`isKeyExpired()`) and ones expiring within 7 days (new `isKeyExpiringSoon()`,
  same shape), plus any connection with `usage.errors > 0` (`connectionsCache`, already populated by
  `metrics.summary_for_connection`). Always renders something, even "No issues detected" when clean -
  reassurance is a health signal too, not just alarms. Each issue links straight to the relevant tab.
- **Deferred, heavier:** a true live reachability check per connection (an actual round-trip via
  `/connections/<name>/test` on every Home load, not a usage-history heuristic) - more honest but a real
  per-connection cost paid on every visit, worth scoping deliberately rather than bundling in; p95/avg
  query latency from the existing `queryapigate_query_duration_seconds`/`_request_duration_seconds`
  histograms - real value, but needs histogram bucket math client-side that the three stat tiles above
  don't. Not started.

## 45. More zero-cost improvements: data already loaded, just not reused yet

**Status: shipped**, except one item that turned out not to apply - see below. A handful of small wins,
each surfacing data the app already fetches/tracks somewhere, just not in the place it would actually be
useful - no new endpoint, no new metric, no new dependency for any of them.

- **FK hints in table-scoped autocomplete.** The autocomplete popup (#39) shows a `→ table` hint (the
  column's `foreign_key.table`, full `table.column` detail on hover) next to a foreign-key column, the
  same schema data the schema browser's own badges (#37) already carry - no new fetch.
- **Keys expiring soon, not just already-expired.** The API Keys table now highlights (in `var(--warn)`,
  "(expires soon)") a key expiring within 7 days, alongside the existing already-expired highlighting -
  reuses `isKeyExpiringSoon()` (added for Home's health panel, #44), same `expires_at` field.
- **Slowest queries.** A new Home panel, ranking the exact same `execution_history` data the Recent API
  requests panel (#41) already aggregates - `aggregateSlowestExecutions()` just sorts by `duration_ms`
  instead of `executed_at` - refreshed on the same 5s poll, no separate fetch.
- **Unused roles.** `renderRoles()` already computed a per-role "Keys created from this role" count
  (`keysFrom[name]`) - it just rendered a plain, easy-to-miss "0" for a role nobody's used yet. Now dimmed
  (`mono dim`, the same convention this file already uses for an empty value elsewhere), in both the Roles
  table and a saved query's own Roles tab. No new logic - the count already existed, only its visibility
  changed.
- **FK-aware "copy starter query."** Since #37, a table's columns already carry `foreign_key` in the
  schema cache the browser already holds - `buildSqlSelect()` now generates a real `JOIN`, with real table
  aliases (collision-safe even for a table referencing itself), when a table has foreign keys, instead of
  always a bare `SELECT * FROM table`. A table with no foreign keys produces byte-for-byte the same output
  as before.
- **"Empty collections" - doesn't apply, not shipped.** Checking before building it: a collection isn't a
  standalone entity in this app, it's a derived property of the queries filed under it
  (`openNewCollectionForm()` says so directly - "a collection exists only while a query is in it"). The
  moment a collection's last query leaves it, the collection itself stops existing - there is no empty-
  collection state to flag.

## 46. A per-table reach indicator in Run SQL's Schema browser

**Status: shipped.** The Access map already answers "which queries touch this table, and which keys/roles
reach them" - but only one table at a time, via its own connection+table filter
(`computeTableMatches()`/`queryTouchesTable()`), and only if you already knew to go looking there. Each
table row in Run SQL's Schema browser now shows the same Q/C/W badge inline: green with the real reach
codes when at least one key/role can reach a query touching that table, a muted dot when the table is
touched but only the admin key can run anything against it, and no badge at all when no saved query
touches the table - nothing to say.

Reuses the exact reach machinery already built for the Access map and the Access tab, not a new
computation: `getQueryFlow()` (real per-query SQL parsing, cached), the same text-match fallback for
unparseable dialects/queries, and `queryReach()` (which keys/roles reach a query and how). The one new
piece is `buildTableUsageIndex()` - a single pass over the connection's saved queries building a full
table→queries index at once (each parseable query's real touched-tables already comes back in one
`GET /query_flow` call), instead of the Access map's own one-table-at-a-time lookup repeated per row -
so badging every table costs about what checking one table already cost, computed once per Schema-tab
paint and shared across every row. `tableReachSummary()` unions `queryReach()`'s answer across every
query a table is touched by, so a table reachable via more than one query still shows one combined badge.

Clicking a badge opens a panel (query list + `accessPill()` reach, the same shape `accessBox()` already
uses for one query) with a "View in Access map" link that reuses the exact already-computed matched-query
set instead of recomputing it - the two screens can never disagree about which queries touch a table.
Scoped to Run SQL's own schema browser only, same reason as the DDL icon (#38): the saved-query form's
embedded schema browser already lives inside the shared drawer this feature also needs, and opening it
there would clobber the form it's shown inside.

The panel shows each touching query's actual SQL, not just its name - fetched and formatted the same way
the SQL tab already does (`getContent()` + `queryDisplayText()` + `prettySql()`, real `sqlglot` pretty-
printing when the dialect supports it), rendered in the same read-only `codeBox()` used everywhere else in
the app. The query name stays a clickable heading (still jumps to Saved Queries); the drawer opts into a
new `.drawer.wide` modifier (reset on every `openDrawer()` call so it never leaks into a narrower drawer
used next) to fit the SQL comfortably. Fixed a real race discovered while building this: `buildTableUsageIndex()`
reads the saved-queries list (`filesCache`) synchronously, but the Schema browser's first paint can run
before the initial `list_files` load finishes populating it, permanently caching an empty usage index for
that connection until a manual refresh. A `whenFilesCacheReady()` promise, resolved once by `loadQueries()`'s
first successful load, is now awaited at the top of `buildTableUsageIndex()` so the very first computation
waits for real data instead of racing it.

## 47. A shared, Redis-backed response cache (QUERYAPIGATE_REDIS_URL)

**Status: shipped.** The daily production pain this closes: an OLTP query getting hit hundreds of times a
second. `cache_ttl` on a saved query already existed to absorb that, but `cache.ResponseCache` is
in-process memory only - wiped by every restart, and never shared if the deployment is ever scaled to more
than one instance behind a load balancer. Setting `QUERYAPIGATE_REDIS_URL` swaps in `rediscache.py`'s
`RedisResponseCache` instead, with the exact same three-method interface (`get`/`set`/`size`) `app.py`'s
`cache_lookup()`/`cache_store()`/`run_saved()` already call - nothing downstream changed at all.

One Redis hash per cache entry (`qag:cache:<key>`, `HSET` + `EXPIRE` in a pipeline, plain `HGETALL` to
read), storing `body` as raw bytes (a saved query can render `xlsx`, which is binary - the client stays in
bytes mode, no base64 needed since Redis strings/hash fields are binary-safe), `content_type`, `etag` and a
JSON-encoded `headers` list. Redis's own `EXPIRE` replaces `ResponseCache`'s manual clock-check and
`MAX_ENTRIES` LRU eviction. A cache failure (Redis unreachable, timeout) is always a miss on read and a
silent no-op on write, never a request failure - the etag is still computed and returned from the body hash
either way, so `cache_store()` needs no special-casing. `redis` is imported lazily, only inside
`RedisResponseCache.__init__`, so a user who never sets the var never needs the package - mirrors how every
DB driver in `runners.py` is imported lazily too. `config.check_settings()` validates the URL scheme and
that `redis` is actually importable at startup (not connectivity - consistent with how database connections
themselves are validated lazily, on first use); a new `redis` extra (`pip install "queryapigate[redis]"`) is
standalone, not folded into `all`/`server`. Fully unit-tested against a small in-memory fake client (no real
Redis server, no new test dependency), matching how `runners.py`'s own DB drivers are tested via mocked
cursors, plus one test class that swaps the fake-backed `RedisResponseCache` into a real `create_app()` and
re-runs the miss/hit/304 flow through actual HTTP requests to prove backend parity.

**Explicitly out of scope, by the user's own call when this was scoped:** making the file-based store
(connections/saved queries/API keys, `store.py`) safe for more than one worker process - that stays a
separate, much larger project (real cross-process locking or a datastore migration touching 10+ locked
sections throughout `store.py`). `--workers 1` remains the documented recommendation regardless of this
cache. SSE (#43) also stays separate, not built here - see the correction added to #43's own entry.

## 48. A Caching screen showing response-cache status and stats

**Status: shipped.** #47 shipped the Redis-backed cache but left no way to see it working - `/settings`
reported the backend but nothing about hit rate, entries, or which queries were actually being cached.
A new "Caching" tab (beside Metrics) closes that gap, built entirely from data the app already computes:

- Two new `/metrics` counters (`queryapigate_cache_hits_total`/`queryapigate_cache_misses_total`) and a
  `queryapigate_cache_entries` gauge, added to `metrics.py`'s existing render pipeline the same way
  `queryapigate_rate_limit_rejections_total`/`queryapigate_pool_idle_connections` already work. Hits/misses
  are counted in `app.py`'s existing `after_request` hook from the `X-Cache` header a cacheable response
  already carries - zero changes to `cache_lookup()`/`cache_store()`/`run_saved()`. Entries comes from
  `.size()` on whichever cache backend is live (`app.extensions['queryapigate_cache']`, passed into
  `metrics.render(cache=...)` by the `/metrics` route, since the cache lives per-Flask-app rather than as
  a module-level singleton the way `pool.py`'s shared pool does).
- The UI tab reuses `settingsData` (already loaded, `#47`'s "Response cache" section) for the backend
  identity, and the already-parsed `metricsSeries` for hits/misses/entries/hit-rate - no dedicated endpoint.
- The one genuinely new piece: `store.list_saved()` didn't expose a version's `cache_ttl` at all (only
  `/catalog` did, which the admin UI never fetches) - added it to `/list_files`' existing payload so the
  Caching tab's "which queries are actually cached" table costs no new fetch either, same as everything
  else on this tab. That same field addition also let the Saved queries detail view grow a "Cached · Ns"
  chip alongside its existing version/latest/status ones (`renderDetail()`) - zero new fetch there either.
- Found and fixed the same race BACKLOG #46 already ran into once: `renderCaching()` was only re-triggered
  from `loadMetrics()`/`loadSettings()`, not `loadQueries()` - so the cache_ttl table's very first paint
  could run before `filesCache` was populated and never repaint. Now called from all three loaders, same
  "tolerate whichever independent fetch lands last" pattern `renderSettings()` already uses.

## 49. A per-query "Cache" tab - view and edit cache_ttl in place

**Status: shipped.** `cache_ttl` could previously only be set when saving a new version - no quick way to
turn caching on/off or retune the TTL for an existing version. A new "Cache" tab (saved-query detail view,
right after Access) closes that gap:

- Editing `cache_ttl` is treated exactly like moving a query's collection already is: **not a new version**.
  `store.set_collection()`'s own docstring already states the precedent ("Not a new version - the SQL did
  not change - and nothing else in the file is touched") - a new `store.set_cache_ttl(ref, version, ttl)`
  mutates the target version's `cache_ttl` in place (truthy → stored, falsy → removed, mirroring how
  `save_version()` itself only ever writes the key when it's truthy) under the same file lock/atomic write
  `set_collection()` uses, rather than going through `save_version()`'s always-`max+1` path.
- Scoped to one specific version, not "the query" - `cache_ttl` is per-version data today (`run_saved()`
  reads it off whichever version actually ran), so the tab edits `v`, the version currently selected via
  the existing version chips, exactly like the History/Curl/SQL tabs already are.
- New `PUT /saved_sql/<name>/cache_ttl` (admin only, `?version=` defaulting to latest), modeled directly on
  `move_query()`'s `PUT /saved_sql/<name>/collection` - same admin-only/audited/in-place shape. The
  validation rule itself (non-negative integer) was extracted out of `definitions.py`'s
  `validate_definition()` into a shared `validate_cache_ttl()` so saving a new version and editing an
  existing one's cache_ttl can never disagree about what's valid.
- "On/off" is just `cache_ttl` being positive vs. `0`/absent - no new boolean flag on disk or in the API;
  the UI toggle is a convenience that remembers the last-typed TTL so turning caching back on doesn't lose
  it. Saving reloads `filesCache`, so the existing "Cached · Ns" header chip (#48) and the Caching tab's own
  table both stay in sync immediately, with no page reload.

## 50. A tables/joins flow diagram on the SQL tab

**Status: shipped.** The Access tab's "Query flow" diagram (tables with join edges, flowing into the query,
flowing out to the keys/roles that reach it) is now also shown on the SQL tab - scoped down to just the
query's own structure, since that tab isn't about reach. `renderFlowDiagram()` gained a nullable `reach`
parameter: `null` renders only the tables/query columns and skips the reach column and its edges, sharing
every other line of the existing diagram/edge-drawing code (`anchor()`/`curve()`/`edge()`/`label()`, the
table↔query edges) rather than duplicating it into a second function. A new `renderSqlQueryFlowPanel()`
mirrors `renderQueryFlowPanel()`'s exact fetch-then-paint shape and shares the same `queryFlowCache` (keyed
by filename:version) every other flow consumer already uses - free if the Access tab already loaded this
query's flow, one `/query_flow` call otherwise.

## 51. A per-query "Metrics" tab, after Cache

**Status: shipped.** History already lists every raw run of the selected version, but there was no
at-a-glance summary. A new "Metrics" tab, right after Cache, aggregates the exact same
`v.execution_history` array History renders as a raw table - no new fetch, same per-version scope as
History/Curl/SQL/Cache. Tiles: total runs, success rate (red below 95%, same `statTile(..., warn)` pattern
the top-level Metrics tab's own error-rate tile uses), avg/slowest duration, avg rows returned (success
runs only), last run. A "Runs by caller" `barCard` appears only when more than one distinct `key_name` shows
up - a one-bar chart says nothing a stat tile hasn't already. Reuses `renderMetrics()`'s own
`statTile()`/`barCard()` verbatim rather than inventing a second visual language; a cache hit already never
appears in `execution_history` in the first place (see `ExecutionHistoryInteractionTests`), so every number
here is already, correctly, about real runs only.

## 52. A "requests per day" chart below the meta row

**Status: shipped.** The saved-query detail header's meta row (connection/collection/author/modified/tags)
is always visible regardless of which subtab is open - the user wanted a daily request-count chart right
below it, in a box roughly 3x that row's height. New `dailyRequestCounts(history)` buckets the selected
version's `execution_history` by calendar day (`executed_at`'s first 10 characters, already
`YYYY-MM-DD...`), zero-filled from the first to the last day recorded so a quiet day is a real zero bar
rather than a gap - a column chart, per the dataviz skill's own "trend over time, discrete counts" guidance,
not a line (which would wrongly imply interpolation between days). Single hue (`var(--accent)`, already used
everywhere else in the app - no new palette to validate), plain `div` bars the same technique `barCard()`'s
horizontal bars already use, just vertical. Only the tallest bar is direct-labelled (the extreme, per
"label selectively"); every bar carries an exact date/count native `title` tooltip instead of a custom hover
layer, matching how `reachDot()` and others already do this. Omitted entirely (returns `null`) when there's
no history, same as every other conditional chart in this file.

## 53. Move connections, saved queries, API keys, roles and the audit log off JSON files onto SQLite

**Status: shipped, both phases.** Every store used to be a plain JSON file, serialized
through one process-local `threading.RLock` - exactly why the Dockerfile and docs pin `--workers 1`, since
that lock protects concurrent *threads* in one process, not concurrent *processes* (surfaced while scoping
#47's Redis cache). New `queryapigate/db.py` owns a `queryapigate.db` SQLite file (one connection per thread,
WAL mode, `foreign_keys=ON`, a `busy_timeout` so lock contention waits instead of raising) with a hybrid
relational/JSON schema - structured columns only for what's filtered or joined on (name, collection, active,
version, status), everything else (host/port/user/password, per-dialect fields, tags, query_parameters) kept
flexible as a JSON blob column, same as it always was inside a `.json` file. Every `store.py` public function
(`read_connections`, `update_connections`, `resolve_saved_file`, `load_versions`, `save_version`,
`set_cache_ttl`, `record_execution`, `move_collection`, etc.) kept its exact name, signature and return
shape, so the ~10 other modules that call into it - `app.py`, `engine.py`, `runners.py`, `examples.py`,
`collection_admin.py`, `cli.py` and the rest - needed zero changes. Compound "read, mutate, write" operations
(saving a new version, moving a query between collections, recording an execution and trimming its history
to the last 50) became real, all-or-nothing SQL transactions - a genuine correctness upgrade over the old
single coarse lock, not just a port; `move_collection()` in particular is now atomic across every query it
touches, where before each file was only atomic on its own. Rather than a separate, explicit
`migrate-to-sqlite` command (the original plan), a real Docker Compose CI regression - the app no longer
reading `db_connections.json` at all, on a fresh read-only-mounted `/data` - led to a simpler design:
`store.import_legacy_data_if_empty()` runs once on startup (`create_app()` and the CLI's `main()`), only when
the relevant table is completely empty, reading whatever legacy JSON is there and importing it without ever
touching or deleting the original files. `GET /view_file_content` (the admin UI's "Show raw file" toggle) and
the `filepath` field on saved-query responses - the two places file-based storage reached the public API -
keep working unchanged: `resolve_saved_file()` now resolves down to a canonical name rather than a real path,
and `/view_file_content` reconstructs the identical JSON text a saved-query file always looked like from
`load_versions()`'s output. Verified against the full test suite, a real `docker compose up -d --build --wait`
run, the CLI's `init`/`examples load`/`export`/`collection export|import` commands, and a real browser check
that History, the per-query Metrics tab and the requests-per-day chart all still read correctly from
`execution_history` now living in SQLite. (Phase 1.)

**Phase 2** moved the three stores Phase 1 deliberately deferred - `api_keys.json`, `roles.json` and
`audit_log.json` (all owned by `apikeys.py`, roles included - not a separate module) - onto the same
`queryapigate.db`, following the identical contract-preservation and hybrid-schema design Phase 1
established: `api_keys`/`roles` get real columns for what `authenticate()`'s per-row scan and `is_expired()`
need (`hash`, `active`, `expires_at`, `created_at`) plus a `details_json` blob for everything else;
`audit_log` mirrors `execution_history`'s own shape (`entry_json` verbatim, `timestamp` as a real column),
and its "keep the newest N" cap became the identical per-insert indexed `DELETE ... LIMIT -1 OFFSET ?` trim
`execution_history` already used, replacing a whole-file read/append/rewrite. Every `apikeys.py` function
(`create_key`, `update_key`, `delete_key`, `create_role`, `update_role`, `delete_role`, `authenticate`,
`collection_grants`, `rewrite_collection_grants`, ...) and `store.record_audit()`/`read_audit_log()` kept
their exact name, signature and return shape - `authenticate()` in particular still fetches every key row and
compares each one in Python with `hmac.compare_digest()`, deliberately not "optimized" into an indexed
lookup, since that per-row scan is what makes a wrong-key guess and a right-key-for-someone-else guess take
the same time. One deliberate design choice, decided explicitly rather than defaulted into: `rename_collection()`
and `examples.load()`/`unload()` were **not** wrapped in one bigger atomic transaction even though every store
they touch is now SQLite - both are explicitly documented and tested as resumable/idempotent-by-design, not
atomic (`rename_collection()`'s own docstring: "an interruption at any point leaves grants that are a
superset of the intended result... running the same rename again completes it" - a real, load-bearing
mid-flight state a wrapping transaction would silently roll back and break); `examples.load()` also writes a
real on-disk SQLite file (the bundled example database) mid-sequence, a side effect no `queryapigate.db`
transaction could ever undo anyway. So `store.lock` still wraps the same call sequences it always did - only
what happens *inside* each call changed, from a JSON rewrite to a SQL transaction. The same
`import_legacy_*_if_empty()` first-boot bootstrap pattern Phase 1 proved out covers all three new stores too.
Verified against the full test suite (802 tests), a real dev server (create a key and role, generate audit
entries, restart, confirm everything persisted and the key still authenticates), a hand-built legacy home
with all five old JSON files and no `queryapigate.db` (every entry round-tripped, originals left untouched),
a real `examples load`/`unload` CLI cycle, direct `sqlite3` inspection of every new table, and a real browser
check of the API Keys/Roles/Audit log admin UI tabs. Every persistent store this app owns is now SQLite-backed
with real cross-process transactions - `--workers 1` is still the recommendation, but the reason changed: not
because any file needs an in-process lock (that's gone), but because the built-in rate limiter and in-memory
`/metrics` are still per-process state with no cross-worker aggregation, a distinct, separately-scoped gap
this migration doesn't touch.

## 54. Surface the MCP server's status in the admin UI

**Status: shipped**, as a Settings row only - the two sub-decisions below resolved in favor of the smaller
option, confirmed explicitly rather than assumed. `config.describe_settings()` gained an `mcp` section (same
file/function the `cache` section from #43/#47 already lives in) with a `QUERYAPIGATE_MCP_PORT`/
`QUERYAPIGATE_MCP_MAX_ROWS` row each, rendered by the Settings screen's existing generic section/row
machinery - no `ui.py` changes needed, since every section already renders the same read-only "effective
value and whether it's the default" shape. **No reachability check**: the section's own description says
plainly that `queryapigate mcp` is a separate process this one doesn't start or probe, rather than showing a
green/red dot that would just be guessing (the REST process serving `/ui` has no way to know whether a
*different* process is actually up without adding a probe request, its latency, and its own failure mode to
every `/settings` load - deliberately not done). **No tool-listing panel** either: the richer treatment
(listing what `tools/list` would currently return for the admin key, via `mcp_server.list_tools_for()`,
closer to how #48's Caching screen became its own panel) was scoped out in favor of the minimal row-only
version. Both remain open, smaller follow-ups if wanted later, not started.

**Both follow-ups shipped later.** A "Reachability" panel with a "Check now" button (`GET
/settings/mcp_status`, a plain TCP connect attempt against `QUERYAPIGATE_MCP_PORT` - not a full MCP
handshake) - on demand only, exactly preserving the original reasoning against an automatic check: it never
runs as part of loading `/settings`, only on an explicit click. A "Tools" panel (`GET /settings/mcp_tools`)
listing every tool `list_tools_for(apikeys.OPEN)` currently returns - name, kind (saved query vs. ad-hoc),
declared params, description - computed in-process from the same function the real MCP server calls, so it
works whether or not `queryapigate mcp` actually happens to be running. Both admin-only, both verified live
against a real running `queryapigate mcp` process (reachable/unreachable states) and a real saved-query
catalog.

**Impact:** `queryapigate mcp` (#42) is a separate process with no presence in `/ui` at all today - unlike
the Redis-backed response cache (#47) and the SSE broadcaster (#43), both of which got a Settings-panel row
showing their backend/status. An admin currently has no way to tell, from the UI, whether the MCP server is
even configured to run, what port it's on, or that it's reachable - they have to know to run `queryapigate
mcp` and check its own stdout/logs, or use an MCP client to probe it directly.

---

**Status:** #1-#11, #12, #13, #14, #15-#18, #19, #20, #21, #22, #23, #24, #25, #26, #27, #28, #29, #30, #31,
#32, #33 and #34 are shipped; #21 is shipped in full (three of three gaps), with `allowed_tables` covering
`mysql`/`postgres`/`clickhouse`/`sqlite`/`duckdb` and `h2`/`jdbc`/`mongo` deliberately deferred (fail-closed,
not silently unsupported - see its own entry); #25 is shipped as three new benchmark scripts with real MySQL
and ClickHouse runs committed, with Postgres/SQLite/H2/DuckDB runs left as a follow-up (the harness already
handles them, see its own entry); #36 is shipped as its
MongoDB find-only slice only, with MSSQL/Oracle/Redis/Snowflake/BigQuery and Mongo's own aggregation/write/
schema-sampling/caching/streaming/query-builder-UI remainder - each its own separately-scoped unit of work -
still open. #27 is shipped,
in a different shape than originally sketched - see the correction in its own entry; #38 is shipped for
mysql/sqlite/clickhouse, with Postgres/H2/DuckDB the documented remainder; #37 is shipped for
mysql/postgres/sqlite/duckdb, with H2 and ClickHouse's differing constraint model left as explicit gaps;
#39 is shipped as its table-scoped `alias.` autocomplete slice only, with the flat
"every column, suggested anywhere" version still open; #40 is shipped, including table/join extraction, the
Access map's table filter, real pretty-printing for `formatSql()`'s call sites, and the node-link diagram
on the Access tab, with only column lineage and write-target detection deferred, plus H2/JDBC permanently
out of scope for real parsing; #41, #42, #43, #44, #45, #46, #47, #48, #49, #50, #51, #52 and #53 are shipped
(#45's "empty collections" item excepted - it doesn't apply to this app's data model, see its own entry; #53
shipped in full, both phases - every persistent store this app owns now lives in `queryapigate.db`, see its
own entry; #43 shipped for its one scoped consumer, Home's recent-requests panel, with the example
dashboard's KPI polling and Metrics' manual refresh left as explicit, not-yet-converted remainders, see its
own entry; #42 shipped for read-only saved queries only plus two ad-hoc tools (`list_tables`/`execute_sql`)
and a generic `outputSchema`, with write-capable MCP tools - needing a `destructiveHint` classification and
a write-confirmation decision - left explicitly open, see its own entry); #54 shipped in full, including
its own two originally-deferred follow-ups (an on-demand reachability check and a tool-listing panel), see
its own entry. The "still
open" note under #9 (confirming its CI changes
on a real run) is a smaller follow-up on finished work, not an open capability gap.

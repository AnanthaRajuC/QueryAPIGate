# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- **Table access restrictions for API keys/roles, `allowed_tables` (BACKLOG #21's remaining gap).** A key or
  role can now be narrowed to a specific set of tables it may query - `mysql`, `postgres`, `clickhouse`,
  `sqlite` and `duckdb` connections only, the dialects `sqlglot` (already an optional dependency, the `flow`
  extra) can parse for this. Unlike `allowed_write_ops`, this restricts *every* statement, read or write.
  Checked against every table a query actually touches - joins, subqueries, CTEs (a CTE's own name is never
  mistaken for a real table) and the target table of a bare `DELETE`/`UPDATE`/`INSERT` are all resolved
  correctly. A table-restricted key used against an `h2`/`jdbc`/`mongo` connection is refused on every query
  with a clear `403`, never silently left unrestricted, since those dialects can't be verified. New
  "Allowed tables" fields on the API Keys/Roles forms, and a table-count badge on the Access column.
- **An MCP server exposing read-only saved queries as tools (BACKLOG #42).** `queryapigate mcp` (needs
  `pip install "queryapigate[mcp]"`, Python >= 3.10) starts an MCP server on its own port
  (`QUERYAPIGATE_MCP_PORT`, default 5001), separate from `queryapigate serve` - MCP's Streamable HTTP
  transport is ASGI, this app is WSGI, so the two run as separate processes against the same
  `QUERYAPIGATE_HOME` rather than being bridged into one. `tools/list` reuses the exact same
  `apikeys.can_run_saved()` scoping `GET /catalog` already enforces, computed fresh per request (never
  cached - two different API keys reach different queries); `tools/call` runs a query through the exact same
  `run_saved()` the REST API uses via a synthetic Flask request context, so caching, `execution_history`,
  audit logging and the Home tab's live feed (BACKLOG #43) all fire exactly as they do over REST - no
  parallel execution path. Only read-only saved queries (`SELECT`/`WITH`/`SHOW`/`DESCRIBE`/`EXPLAIN`, and any
  Mongo `find()`) become tools in this version; a write-capable query stays reachable over REST as before,
  since exposing it over MCP needs a `destructiveHint` classification and a confirmation-flow decision this
  codebase doesn't make yet. A tool call's result is capped at `QUERYAPIGATE_MCP_MAX_ROWS` (default 200),
  independent of the REST API's own page-size default, since an LLM's context can't hold a large result.
- **Live updates for the admin UI's Home tab via Server-Sent Events (BACKLOG #43).** The "Recent API
  requests"/"Slowest queries" panels used to poll `GET /list_files` (the whole saved-queries catalog) every
  5 seconds while Home was visible. A new `GET /events` (admin only) now pushes one `data: {...}` line per
  saved-query execution as it's recorded, and the panels patch themselves in place instead of refetching -
  updates now arrive in well under a second instead of up to 5. Backed by a new in-process `Broadcaster`
  (`queryapigate/broadcast.py`), one instance per Flask app, with the same swappable-backend shape
  `cache.py`/`rediscache.py` already established for the response cache, so a future Redis-pub/sub variant
  (for a multi-*instance* deployment) is a clean addition later, not a rewrite - not built yet, since nothing
  needs it under the documented single-process deployment. The client reads the stream with `fetch()`'s
  streamed response body rather than a plain `new EventSource(...)`, since `EventSource` cannot set the
  `X-API-Key` header every other admin request already uses, and putting the key in the URL instead would
  leak it into logs and browser history. Falls back to the old 5s poll if the stream can't be reached or
  drops, so a live-feed failure never means the panel just stops updating.
- **`--worker-class gthread` added to the Dockerfile's gunicorn command** (and the example command in
  `documentation/INSTALLATION_AND_SETUP.md`), found necessary while building the above: gunicorn's default
  `sync` worker class ignores `--threads` entirely, so the image's existing `--threads 8` was silently inert -
  under `sync`, the single worker handles one connection at a time, and a long-lived `/events` connection
  would have blocked every other request to the server for as long as that one client stayed connected.
  Verified against a real container: a held-open `/events` connection no longer delays a concurrent `/health`
  request.

### Changed
- **API keys, roles and the audit log now live in `queryapigate.db` (SQLite), not `api_keys.json`/
  `roles.json`/`audit_log.json`** (BACKLOG #53, Phase 2 - completes the JSON-to-SQLite migration Phase 1
  started for connections and saved queries). Every public `apikeys.py`/`store.record_audit()`/
  `read_audit_log()` function kept its exact name, signature and return shape, so no other module needed to
  change. `create_key`/`update_key`/`delete_key`/`create_role`/`update_role`/`delete_role` now read-check-
  write against real SQL rows inside a transaction instead of rewriting a whole JSON file; the audit log's
  "keep the newest N entries" cap is now a per-insert indexed trim (the same technique Phase 1 already gave
  `execution_history`) instead of a whole-file read/append/rewrite. `rename_collection()` and
  `examples load`/`unload` keep their existing resumable-not-atomic design exactly as before (both are
  explicitly documented and tested that way, and `examples load` also writes a real on-disk SQLite file
  mid-sequence that no transaction could roll back anyway) - what changed is only that each step inside them
  now writes to SQLite instead of a JSON file. Pre-existing `api_keys.json`/`roles.json`/`audit_log.json` are
  imported automatically, once, the first time their table is empty - nothing to run by hand, and the JSON
  files are never consulted again afterward. Every persistent store this app owns is now SQLite-backed with
  real cross-process transactions; `--workers 1` is still the recommendation, now because the built-in rate
  limiter and in-memory `/metrics` are per-process state, not because any file needs an in-process lock.

## [0.10.0] - 2026-09-29

### Added
- **A "requests per day" chart below the meta row (BACKLOG #52).** The saved-query detail view now shows a
  daily request-count column chart right under the connection/collection/author/modified row, visible on
  every subtab. Zero-filled from the first to the last day in the selected version's `execution_history` -
  a quiet day is a real zero bar, not a gap - with the tallest day direct-labelled and every bar carrying
  an exact date/count tooltip. No new fetch.
- **A per-query Metrics tab (BACKLOG #51).** Right after Cache, showing that version's own run stats -
  total runs, success rate, avg/slowest duration, avg rows, last run, and a "Runs by caller" chart when
  more than one key has called it - aggregated entirely from `execution_history` already on the client, no
  new fetch. Reuses `renderMetrics()`'s own `statTile()`/`barCard()` building blocks.
- **A tables/joins flow diagram on the SQL tab (BACKLOG #50).** The saved-query SQL tab now shows the same
  "Query flow" diagram the Access tab does - tables and their join edges, flowing into the query - minus
  the keys/roles column, since this tab is about the query's own structure, not who can reach it. Shares
  the same cached `/query_flow` fetch, so it's instant if the Access tab already loaded it.
- **A per-query Cache tab (BACKLOG #49).** The saved-query detail view gets a new "Cache" tab, right after
  Access, showing and editing that version's `cache_ttl` - an on/off toggle plus a TTL field, saved via a
  new `PUT /saved_sql/<name>/cache_ttl` (admin only). Editing it is not a new version, the same way moving
  a query's collection already isn't - no version bump, no `execution_history` entry, takes effect on the
  very next request.
- **A Caching screen (BACKLOG #48).** A new "Caching" tab (next to Metrics) shows the response cache's
  backend (in-process or Redis, from `/settings`), live entry/hit/miss counts and hit rate (two new
  `/metrics` counters, `queryapigate_cache_hits_total`/`queryapigate_cache_misses_total`, plus a
  `queryapigate_cache_entries` gauge - counted from the `X-Cache` header responses already carry, no
  changes to the cache lookup/store paths themselves), and a table of every saved query that declares a
  `cache_ttl`. The Saved queries detail view also gets a "Cached · Ns" chip next to the existing
  version/latest/status ones, on any query with a `cache_ttl` set.
- **A shared, Redis-backed response cache (BACKLOG #47).** Setting `QUERYAPIGATE_REDIS_URL` swaps the
  in-process `cache_ttl` cache for a Redis-backed one (`pip install "queryapigate[redis]"`) that survives a
  restart and is shared across horizontally-scaled instances - useful when a hot OLTP query is hit hundreds
  of times a second. Unset means today's behavior, byte-for-byte unchanged. A cache failure is always
  treated as a miss/no-op, never a request failure. Does not change the documented `--workers 1`
  recommendation for the file-based store - see the correction added to BACKLOG #43.
- **A per-table reach indicator in Run SQL's Schema browser (BACKLOG #46).** Each table now shows the same
  Q/C/W badge the Access map uses - green means at least one API key or role can reach a query touching
  this table, a muted dot means only the admin key can, and no badge at all means no saved query touches it.
  Clicking it opens a panel listing which queries touch the table and who can reach them, with a "View in
  Access map" link straight into the fuller screen. Built entirely by reusing the Access map's own
  machinery (`getQueryFlow()`, `queryReach()`) - one pass over the connection's queries per Schema-tab
  paint, not one lookup per table, so browsing a connection's schema now costs about what clicking one
  table in the Access map already cost today, computed once and shared across every table shown. Only
  wired into Run SQL's own schema browser, same reason as the DDL icon (#38) - the saved-query form's
  embedded schema browser already lives inside the shared drawer this needs. The panel now also shows each
  touching query's actual, formatted SQL (not just its name), in a wider drawer to fit it comfortably. Also
  fixes a race where the Schema browser's very first paint could run before the saved-queries list finished
  loading, permanently caching an empty usage index for that connection until a manual refresh.
- **A "show CREATE TABLE" icon in the schema browser (BACKLOG #38).** A new ⌸ icon next to the existing
  ⧉/👁 ones shows a table's real DDL (`GET /connections/{name}/table_ddl`) for `mysql`, `sqlite` and
  `clickhouse` connections - SQLite's is free (`sqlite_master.sql` already *is* the CREATE TABLE text),
  MySQL/ClickHouse each have a single `SHOW CREATE TABLE` statement. Postgres has no single-statement DDL
  dump (real reconstruction from `pg_catalog` would be its own follow-up); H2/DuckDB are left out the same
  way they were for the PK/FK work (#37) - unverified completeness in this environment; Mongo has no DDL at
  all. Safe against a malicious table name by construction: the requested name must already be a table the
  connection's own schema listing reported before any DDL query is ever built, since no driver supports
  parameter-binding for an identifier the way it does for a value. Only wired into Run SQL's schema
  browser, not the saved-query form's embedded one, since that one already lives inside the shared drawer
  this feature also needs.
- **BACKLOG #45 finished: three more zero-cost reuses of already-loaded data, and one corrected note.**
  A new "Slowest queries" panel on Home, ranking the same `execution_history` data Recent API requests
  already shows by `duration_ms` instead of time. A role with zero keys created from it now renders dimmed
  in both the Roles table and a saved query's Roles tab, instead of a plain "0" easy to miss. The schema
  browser's "copy starter query" icon now generates a real `JOIN` (with real table aliases, collision-safe
  even for a self-referencing foreign key) when a table has foreign keys, instead of always a bare
  `SELECT * FROM table` - a table with no foreign keys is completely unchanged. "Empty collections," the
  fourth item #45 originally listed, turned out not to apply to this app's data model - a collection is a
  derived property of the queries filed under it, not a standalone entity, so there's no such state to flag.
- **Two zero-cost reuses of already-loaded data (BACKLOG #45).** Table-scoped autocomplete now shows a
  `→ table` hint (full detail on hover) next to a foreign-key column, using the same schema data the
  schema browser's PK/FK badges (#37) already carry. The API Keys table now highlights a key expiring
  within 7 days (not just an already-expired one), reusing the `isKeyExpiringSoon()` check added for
  Home's health panel (#44).
- **A "System health" panel and three more stat tiles on the Home tab (BACKLOG #44).** Active queries,
  pool idle connections and rate-limit rejections join the existing stat tiles - the same numbers the
  Metrics tab already computes, just not previously shown on Home. The new health panel flags expired and
  soon-to-expire (within 7 days) API keys and any connection with recorded errors, each clickable straight
  to the relevant tab; shows "No issues detected" when clean, since that's a real health signal too. No new
  fetch, no new metric - every figure was already loaded somewhere on the page.
- **A "Recent API requests" panel on the Home tab, auto-refreshing every 5 seconds.** A live-ish tail of
  saved-query runs (newest first: time, query, connection, caller, rows, duration), built from the same
  `execution_history` the per-query History tab already shows, just aggregated across every saved query.
  Auto-refreshes only while Home is the visible tab, via a lightweight `GET /list_files` poll - not the
  full Saved Queries screen's own load path. Click a query name to jump straight to its History tab. Only
  saved-query runs through `/q/<name>` are recorded this way - ad-hoc Run SQL calls have no saved query to
  attach a history entry to, so they don't appear here, and the panel says so plainly when empty.
- **The example database grows from 3 tables to 8 (BACKLOG #27).** New `category`, `store`, `staff`,
  `address` and `payment` tables, each with a real foreign key (except `category`, a deliberate plain
  lookup table joined into nothing), generated purely additively - `film`/`customer`/`rental`'s own rows
  are untouched, and their generation order is unchanged, so this doesn't affect anything already built on
  them. `example_all_rentals` (the export scenario) now joins six tables - `rental`, `film`, `customer`,
  `payment`, `staff`, `store` - `payment` is exactly 1:1 with `rental` by construction, so the row count
  stays exactly `RENTAL_COUNT`, same as before. Gives the schema browser's PK/FK badges and the Access
  tab's Query flow diagram real, richer data to show.
- **A fifth example role, `example-executive` (BACKLOG #27), spanning two collections at once.** Read-only,
  `300/hour`, granted both `examples-reporting` and `examples-dashboard` - the one thing none of the four
  existing example scenarios showed on its own: a role (and so a key created from it) reaching more than
  one collection. `documentation/EXAMPLES.md` also now points at the newer schema/parsing features using
  the existing example data - PK/FK badges on `rental`'s real foreign keys, the Access tab's Query flow
  diagram on a real joined query, table-scoped autocomplete, and real SQL pretty-printing. Also fixes the
  admin UI's "Example APIs are loaded" strip, which hardcoded "four roles" and was about to be wrong the
  moment a fifth role existed.
- **A Home tab for the admin UI.** A new first tab (ahead of Connections) - an at-a-glance overview:
  connection/saved-query/API-key/role counts, requests and error rate, the 5 most recent audit log entries
  (click one to jump to its likely tab), and quick actions (Run SQL, New saved query, New connection, Help).
  Built entirely from caches the other tabs already load - no new endpoint - and re-rendered from the tail
  of each of those loaders so it's never stale regardless of load order. Becomes the default landing tab for
  a fresh session; a returning user's last-used tab (remembered per browser tab) still takes over as before.
- **Real SQL parsing for the Access tab's "Query flow" panel (BACKLOG #40).** A new `queryapigate/sqlflow.py`
  parses a saved query's SQL with `sqlglot` (new `flow` extra) and extracts the tables and joins it touches
  (`GET /query_flow`). Supports `mysql`, `postgres`, `clickhouse`, `sqlite` and `duckdb` connections;
  `h2`/`jdbc`/`mongo` (and any query sqlglot can't parse) get a plain explanatory message instead - this
  never touches the execution or write-guard path, and a parse failure never blocks or raises. The Query
  flow panel shows this as a real node-link diagram - tables (with join edges) flowing into the query,
  flowing out to the keys/roles that can call it - built from plain HTML nodes plus a thin SVG line layer,
  no new charting dependency.
- The Access map's own "which queries reference this table" filter now uses that same real parsing where
  the connection's dialect supports it, falling back to the original text-search heuristic per-query when
  it doesn't (h2/jdbc/mongo, or an unparseable query) - fixes false positives like a table name only
  appearing in a SQL comment.
- The SQL tab and the Access map's query info popup now show a real `sqlglot` pretty-print of a one-line
  saved query when the dialect supports it, in place of `formatSql()`'s naive token reflow (still the
  fallback for unsupported dialects, unparseable queries, and any query using the legacy `{name}` brace
  placeholder, which `sqlglot` doesn't understand). Note this can cosmetically normalize the SQL text
  itself, not just its layout - e.g. an implicit `orders o` alias becomes explicit `orders AS o`, and a
  `--` line comment becomes `/* */` - both still exactly equivalent, valid SQL.
- **Table-scoped column autocomplete (BACKLOG #39, table-scoped slice).** Typing `alias.` or `table.` in
  either SQL editor (Run SQL, or the saved-query form) now pops up that table's columns, filtered as you
  keep typing, accepted with Enter/Tab/click, dismissed with Escape/click-away. Uses a lightweight
  `FROM`/`JOIN` regex to resolve the alias (not `sqlflow.py`'s real parser, which needs finished SQL, not
  text that's routinely mid-keystroke) and the existing schema cache for columns - no new backend work.
  No suggestion for an alias that doesn't resolve, and none at all for a Mongo connection's JSON editor.
- **Primary/foreign key markers in the schema browser's Columns tab (BACKLOG #37).** A column now shows a
  "PK" badge and a "FK → table.column" badge when it's part of one, for `mysql`, `postgres`, `sqlite` and
  `duckdb` connections. A new, additive `_KEY_QUERIES` per dialect (`schema.py`) merges onto the existing
  schema fetch and degrades gracefully - a permissions error or unexpected catalogue shape just means no
  badges, never a broken schema fetch. `h2` (unverifiable in this environment) and `clickhouse` (no real
  foreign-key concept) are explicit gaps, not bugs; a composite key reports only its first column.

### Changed
- **Connections and saved queries now live in `queryapigate.db` (SQLite), not `db_connections.json`/
  `saved_sql/*.json`** (BACKLOG #53) - Phase 1 of moving off hand-rolled JSON file storage (API keys, roles
  and the audit log are unaffected for now, and stay JSON-backed for a later phase). Every public
  `store.py` function kept its exact name, signature and return shape, so no other module needed to change;
  compound read-modify-write operations (saving a new version, moving a query between collections, trimming
  `execution_history` to its last 50 runs) are now real, all-or-nothing SQL transactions instead of a
  whole-file rewrite under one coarse lock. Pre-existing `db_connections.json`/`saved_sql/*.json` are
  imported automatically, once, the first time the relevant table is empty - nothing to run by hand, and the
  JSON files are never consulted again afterward. `GET /view_file_content` (the admin UI's "Show raw file"
  toggle) still returns the identical JSON text a saved-query file always looked like, reconstructed from
  SQLite. `QUERYAPIGATE_HOME` now needs to be writable (previously only needed to be readable) - the bundled
  `docker-compose.yml` demo, whose data folder was mounted read-only, now seeds a proper writable volume from
  it via a small one-shot init container instead.

## [0.9.0] - 2026-09-28

### Added
- **MongoDB support (find-only).** A new `mongo` connection type; ad-hoc `find()` queries from Run SQL and
  `POST /execute_mongo`; saved Mongo queries (`query_type: "mongo"`) exposed through `GET/POST /q/{name}`
  exactly like a saved SQL query, including OpenAPI/`/catalog` listing, bundle export/import and `:name`
  bound parameters (substituted as real JSON values via `mongotools.fill_placeholders()`, not string
  splicing); collection listing in the Schema tab; native database listing/switching. Read-only, full stop -
  there is no write path at all yet (see BACKLOG #36 for the deliberately-deferred remainder: aggregation
  pipelines, writes, per-field schema sampling, caching, streaming and a dedicated query-builder UI - v1
  uses one JSON textarea, `{"collection", "filter", "projection", "sort"}`, in both Run SQL and the
  saved-query form). A `$where`/`$function`/`$accumulator` filter is rejected outright (arbitrary
  server-side JavaScript execution), the Mongo equivalent of the SQL guard's read-only/single-statement
  check.
- **Run SQL's sidebar redesign**: Recent/Settings/Schema tabs instead of one long stacked column, a
  draggable splitter between the editor and the sidebar (width remembered across sessions), the redundant
  Table dropdown removed now that the Schema tab already inserts a table on click, and the schema browser
  capped with its own scrollbar instead of growing unbounded for a connection with a lot of tables. Editing
  an existing mysql/postgres/clickhouse(/mongo) connection now loads its database list automatically instead
  of waiting for a manual click, and the tab drops its usual width cap so the editor and results fill the
  whole window on wide screens.
- **A "Test connection" button** on the New/Edit connection form, and `POST /connections/test` (admin only):
  tries to actually connect with the fields on screen - not yet saved - so a typo'd host or a firewalled port
  is found out before saving, not on the query that comes after. Never touches the connection pool's saved
  state or writes anything; a driver's own error text is redacted before being shown, so a real password can
  never leak back through a connection error.
- **Connections now record when they were created and last edited** (`created_at`/`updated_at`, shown as new
  columns in the Connections table and in the Edit form). Server-controlled - a client cannot fake either one
  by echoing them back. A connection saved before this existed shows `—` until its next edit.
- **The Access map's UI improvements from this cycle**, listed together here: quick-glance Database/Created/
  Last modified/Last used/Version columns, an info popup per query (with its pretty-printed SQL), sortable
  columns, Database/Reach filters, a combined API-keys-and-roles matrix, and a Connection-to-table drill-down
  that shows which tables a connection's saved queries actually expose (with a shortcut to draft a new saved
  query on one that isn't exposed yet).
- **Run SQL's connection picker is now Type → Host → Connection** instead of one flat name list - at more
  than a handful of connections, "which of these is the one I want" is a type-and-host question first.
- **The schema browser is now Tables and Columns tabs** instead of an expand/collapse tree: clicking a
  table jumps straight to its Columns (name and type, with its own scrollbar for a table with a lot of
  them), and a copy icon per table builds a starter query into the editor without running it - a full
  `SELECT` (every column by name, one per line, schema-qualified `FROM`, `ORDER BY` the first column,
  `LIMIT 100`) for SQL, or the equivalent `find()` document for mongo. The editor grows to fit whatever it
  pastes in instead of clipping it behind a scrollbar. The preview icon is now an eye rather than a play
  triangle, next to the copy icon at the end of the row.
- **A "Save as New API" button in Run SQL**: opens the same "New saved query" form the Saved Queries screen
  has, pre-filled with the query you just tried there, its connection, and an empty entry per detected bound
  parameter - test it first, then save it, rather than writing a saved query blind.
- **Double-click a column (or its comparison value) in Run SQL to turn it into a bound parameter**: a
  popover previews the exact edit (`customer_id = 5` → `customer_id = :customer_id`) before anything
  changes, and confirming fills Bound Parameters with the real, correctly-typed value automatically.
- **Saved Queries is now Collections and Queries tabs**, mirroring the schema browser, instead of one long
  expand/collapse list per collection; the search box still spans every collection at once regardless of
  which tab is open.
- **Run SQL's Recent Queries/Settings/Schema tabs are a consistent size** regardless of which is open -
  previously the whole row resized depending on which tab's own content happened to be tallest - and each
  now scrolls internally for its own overflow, the way Schema already did. "Recent" is renamed "Recent
  Queries", and Schema is now the tab shown by default.
- **The Access map's Reach column moved next to Connection** instead of last; clicking a query's name there
  now opens it directly in Saved Queries. A saved query's own "who can reach this" panel shows the same
  Q/C/W reach badges inline per key and role now, instead of a plain comma-separated name list, with a
  "View in Access map" link for the full cross-query matrix.
- **A Help screen** (sidebar, below Settings): getting-started steps, keyboard shortcuts, a short concepts
  glossary (saved query, collection, key vs. role, Q/C/W reach, bound parameters) and links to the docs
  site, GitHub repo, issues and changelog.

## [0.8.0] - 2026-09-26

### Added
- **A redesigned admin UI.** The tab bar is now a collapsible left sidebar grouped into Data, Access and Observability
  (`Ctrl`/`Cmd`+`B` toggles it, and the choice is remembered), with a breadcrumb in the top bar. API docs, OpenAPI and
  the API key panel live at the foot of the sidebar; every screen has a title with a one-line description; Connections
  has All / Active / Inactive tabs; and a global search (`Ctrl`/`Cmd`+`K`) jumps to any connection, saved query, API key
  or role. Metrics shows bar rows instead of charts, the audit log can be exported as JSON, and the first saved query
  opens by default with only its collection expanded.
- **A read-only Settings screen** and `GET /settings` (admin only): every environment variable the server reads, its
  effective value and whether it was set or is the built-in default, with a "Copy as .env" button. A secret (the admin
  key, the encryption key) is only ever reported as configured or not - never returned, and never copied.
- **Interface preferences** in Settings: theme (system, light, dark), table density and the default result format. They
  live in the browser only and are never sent to the server.

## [0.7.1] - 2026-09-26

### Fixed
- **The container crashed at startup with `QUERYAPIGATE_LOAD_EXAMPLES=yes` when its data folder was not writable** (for example
  a `-v "$PWD/data:/data"` folder that Docker had created, which is owned by root while the container runs as a non-root
  user). SQLite's own error escaped the examples loader, the worker died and gunicorn kept restarting it. Loading the
  examples is a convenience, so a failure now only logs a warning saying the folder must be writable, and the server
  starts; `queryapigate examples load` prints the same message instead of a traceback. The previous test for this mocked
  the wrong error type; the new ones use a genuinely read-only folder.

### Changed
- README: the Docker examples now use a named volume (`-v queryapigate-data:/data`), which works without any host
  permission setup, and explain what a host folder needs (writable by uid 1000).

## [0.7.0] - 2026-09-26

### Changed
- **License: from MIT to the Functional Source License, Version 1.1, MIT Future License (`FSL-1.1-MIT`).** QueryAPIGate
  is now *source-available* rather than open source. You can still use, modify and redistribute it for any purpose
  except a *Competing Use* - offering it to others in a commercial product or service that substitutes for it or
  offers substantially similar functionality (so: not selling it, or hosting it as a paid service). Running it inside
  your company, non-commercial education and research, and professional services for a licensee are all permitted.
  Each version becomes MIT on the second anniversary of its release. **Versions 0.4.0 through 0.6.1 were released under
  MIT; they are no longer distributed, and a copy already obtained under MIT stays under those terms.** The code is unchanged in this release; only the license and its metadata are
  (`LICENSE`, the package's `License-Expression`, the container image label, the README). Outside code contributions
  are not being accepted for now (CONTRIBUTING.md).

### Removed
- The Sakila and Chinook sample SQLite databases and the `examples/` folder that held them (with their two saved queries and
  the licence notices those datasets need). They were never part of the wheel; the repository now has no third-party
  data. Use `queryapigate examples load` for a working sample instead - its data is generated locally. The Docker demo's
  film titles (which came from Sakila) are replaced with generated ones, and the documentation's examples now use the
  generated `examples` connection (`film` table) instead of the `actor` table. The repository history was also reset to a single commit for this release, so earlier commits and releases are no longer published.

## [0.6.1] - 2026-09-25

A small fix; no behaviour change beyond it. Nothing to do when upgrading from 0.6.0.

### Fixed
- A key's `last_used_at` was not recorded for its first use when the host had booted less than 60 seconds earlier: the
  "never recorded" state was `0.0`, compared against `time.monotonic()` (which counts from boot on Linux), so the first
  use fell inside the throttle window and was skipped. Only a machine that had just booted was affected, which in
  practice meant freshly created CI runners - it made the CI test job fail on a random subset of Python versions on
  every run. Fixed by treating "never recorded" as absent, with a regression test on a simulated freshly-booted clock.

## [0.6.0] - 2026-09-25

**Upgrade recommended: this release fixes a security issue in scoped API keys** (see *Fixed*). Adds collections,
example APIs, a Postman export and caller-supplied request IDs. Stored keys, roles and saved queries from 0.5.0 work
unchanged. One behaviour change comes from the fix: a key granted a saved query (or collection) can no longer point it
at a different connection with `?connection_name=` unless it also holds a grant on that connection - a client that
relied on doing so needs that connection added to its key.

### Added
- **Collections** ([#35](BACKLOG.md)): a saved query can belong to one named collection, stored on the query
  file itself. A key or role can be granted a `collections` list - read-only, additive, live (it reaches whatever
  is in the collection now and later), with no `"*"` wildcard and no write access through it. Newly granted
  names must exist. New: `PUT /saved_sql/<name>/collection` (move; audited with the keys and roles that gain or
  lose access), `GET /collections` (queries, keys and roles per collection, including a grant whose collection
  has emptied), `PATCH /collections/<name>` (rename - resumable, never narrows access part-way; `merge` for an
  existing target), and `queryapigate collection export|import` (portable bundle of definitions only; validated
  like saving a query, all-or-nothing before writing, `--on-conflict fail|skip|new-version`, `--dry-run`).
  `collection` is also accepted by `PATCH /save_sql_to_file` and reported by `GET /list_files` and `GET /catalog`.
  The admin UI groups the Saved Queries list by collection, adds **Move…** with a who-gains/loses-access preview,
  rename, and a Collections field on the key and role forms.
- **Example APIs** ([#27](BACKLOG.md)): `queryapigate examples load | unload | status` installs four worked scenarios -
  reporting, dashboard (cached 30 s), streaming export and partner integration - as 11 saved queries in four
  collections, four matching roles, and an `examples` connection to a ~2 MB SQLite database generated locally (no
  third-party data shipped). Everything is marked `example` and removal deletes exactly that; loading refuses, changing
  nothing, if something of yours holds an example's name. Idempotent. `QUERYAPIGATE_LOAD_EXAMPLES=yes` loads them at
  startup (for containers; a problem is a warning, a malformed value is rejected). `GET/POST/DELETE /examples`, a Load
  / Remove control in the admin UI, and a walkthrough in `documentation/EXAMPLES.md`. No API key is created.
- **Postman export** of a collection: `GET /collections/<name>/postman`, `queryapigate collection export --format
  postman`, and a Postman button in the admin UI. One `GET /q/<name>` request per query with its parameters filled
  in from their own rules (a test runs every generated example back through the server's parameter validation);
  authentication is an empty `{{apiKey}}` variable, so the file never holds a key. Export only, and a snapshot.
- **New collection** in the admin UI: pick a name and the queries to file under it, with the who-gains/loses-access
  preview.
- **Caller-supplied `X-Request-Id`**: send your own (1-64 characters of letters, digits, `.`, `_`, `:`, `-` - a UUID
  works) and it is used in the response header, the log lines and a saved query's run history, so a run can be tied
  to a trace in your own system. Anything else is ignored and the server generates its own, as before; it is a
  correlation aid only and nothing authorises by it. Matched with `fullmatch`, so a trailing newline cannot smuggle
  a forged log line in.
- A test that every route appears in the OpenAPI document.

### Fixed
- Admin UI: at widths between about 1240 and 1320 px the last tab (Run SQL) was clipped now that tabs carry counts - the
  compact two-row header now starts below 1360 px. Collection group headers in the Saved Queries list no longer truncate
  a collection's name behind their own controls.
- `X-Request-Id` was not in the CORS exposed headers, so a browser page on another origin could not read the ID
  the server returned; it is now exposed (and allowed on requests).
- **A key granted a saved query (`queries`) could run that query's SQL against any connection** by adding
  `?connection_name=<other>` (or `connection_name` in a POST body), even with `connections: []`. A `queries` or
  `collections` grant now authorises the query only on its own connection; another one needs a real connection
  grant. Anyone relying on scoped external keys should upgrade.

### Changed
- One function (`apikeys.can_run_saved()`) now decides whether a caller can reach a saved query for `/openapi.json`
  and `/catalog` (previously the same rule was written out separately in each), saved-query validation is
  shared by saving and importing, and a query's effective parameters are derived in one place for OpenAPI, the
  catalogue and the Postman export.

## [0.5.0] - 2026-09-25

### Changed
- **Breaking: renamed from SQL2API to QueryAPIGate.** A clean break with no compatibility aliases, so an
  existing deployment must change these before upgrading (full table and rationale in
  [Upgrading from SQL2API](documentation/INSTALLATION_AND_SETUP.md#upgrading-from-sql2api)):
  the PyPI package is `queryapigate` (was `sql2api`), the command is `queryapigate` (was `sql2api`), the Python
  package is `queryapigate`, environment variables are `QUERYAPIGATE_*` (were `SQL2API_*`), Prometheus metrics
  are `queryapigate_*` (were `sql2api_*`), the Docker image is `ghcr.io/anantharajuc/queryapigate`, and the
  JSON log `logger` field and the admin UI's browser-storage keys use the new name. Data files
  (`db_connections.json`, `saved_sql/`, `api_keys.json`, ...) are unchanged. Because an unset API key means an
  open server, the old `SQL2API_*` variables are not merely ignored: a server that still finds any of them in
  its environment refuses to start and names each one to rename, rather than silently running unprotected.
  To migrate environment files in place: `sed -i 's/SQL2API_/QUERYAPIGATE_/g' <file>`; the same substitution
  with `sql2api_` -> `queryapigate_` fixes Prometheus queries and alert rules. Releases 0.1.0 through 0.4.0
  below shipped as `sql2api` and are described under that name.
  This is the first release published under the new name.

### Added
- `queryapigate export <query> --out <path>` (closing #31): runs a saved query and writes its full result to a
  file, entirely in-process against `QUERYAPIGATE_HOME` - no server needs to be running, no HTTP round trip, no
  API key. Built for cron/systemd/Kubernetes CronJob to call, deliberately not a scheduler itself.
  `{name}`/`{date}` placeholders in `--out`; `--format csv|tsv|ndjson` (the same formats `?stream=true`
  supports, via the same underlying code path - `formats.iter_stream_chunks()`, newly factored out of
  `stream_response()` so both share it); `--connection` overrides the saved query's own default;
  `--param name=value` (repeatable) supplies a required parameter. Writes to a temp file and renames into
  place only on success, so a failed run never leaves a partial file at the final path; exits non-zero on
  any failure so cron's own failure handling works unmodified. See [Scheduled exports to a
  file](documentation/INSTALLATION_AND_SETUP.md#scheduled-exports-to-a-file).
- Configurable, exportable audit log retention (closing #30): `QUERYAPIGATE_AUDIT_LOG_LIMIT` replaces the
  hardcoded 500-entry cap on `audit_log.json` (validated at startup, following the same pattern
  `QUERYAPIGATE_STREAM_MAX_ROWS` already uses - always a positive count, never "unbounded", since the file is
  read and rewritten in full on every audit event). `QUERYAPIGATE_AUDIT_LOG_EXPORT_FILE`, when set, also appends
  every entry to a separate, never-capped, never-rewritten file (one JSON object per line) - for retention a
  rolling cap can never satisfy, independent of the primary write so a problem with one never blocks the
  other. See [Audit log](documentation/API.md#audit-log).
- Package metadata for PyPI discoverability (closing #28): `Documentation` and `Repository` links in
  `[project.urls]` (PyPI's sidebar renders both as clickable links; only `Homepage`/`Issues`/`Changelog`
  were set before), and `Environment :: Web Environment`, `Operating System :: OS Independent` and
  per-version `Programming Language :: Python :: 3.9` .. `3.14` classifiers matching the CI test matrix
  exactly (PyPI's "Programming Language" filter facet uses these). Deliberately does *not* add a
  `Typing :: Typed` classifier or a `py.typed` marker - the codebase is gradually typed for `mypy`'s own
  benefit (see `[tool.mypy]`'s comment), not annotated throughout, and that classifier is a PEP 561 promise
  to consumers this project doesn't actually keep yet. Verified with `python -m build` and
  `twine check dist/*`, and by inspecting the built wheel's own `METADATA` file.
- Modernized `license` in `pyproject.toml` to a PEP 639 SPDX expression (`license = "MIT"`, replacing
  `license = { text = "MIT" }`) and dropped the now-redundant `License :: OSI Approved :: MIT License`
  classifier - `python -m build` was warning that the classifier form is deprecated. The build now emits
  `License-Expression: MIT` and bundles `LICENSE` under the wheel's standard `licenses/` directory
  automatically, with no change to what's actually licensed.
- Live usage on the Connections and API Keys admin-UI screens (closing #32): `GET /connections` and
  `GET /api_keys` now carry a `usage` object per entry (queries run, failures, rows returned - plus average
  latency for a connection) aggregated from the same in-process counters `/metrics` already renders, via two
  new `metrics.summary_for_key()`/`metrics.summary_for_connection()` functions. Previously that data existed
  only in Prometheus text form; a key or connection's row showed grants and, at best, a single
  `last_used_at` timestamp, with no way to see how much it had actually been used or how often it had
  failed without a separate `/metrics` scrape. See [Connections](documentation/API.md#connections) and
  [Authentication and permissions](documentation/API.md#authentication-and-permissions).
- A saved query's History tab (closing #33) now shows `request_id` and `key_name` columns (`execution_history`
  already recorded both, added for the log-correlation work closing #19, but the admin UI never rendered
  either), plus a client-side status filter (all/success/failed) and a search box over connection, caller,
  request ID and error text. See [Admin UI](documentation/API.md#admin-ui).
- The Audit Log tab (closing #34) now has an action-type filter and a search box (actor, target, timestamp),
  filtering client-side over what it already fetched, and a create/delete snapshot's `changes` omits unset
  fields (`null`/`""`/`[]`) instead of always listing every field on the entity - a diff (an update) is
  unaffected, since it never had unset fields to begin with. See [Audit log](documentation/API.md#audit-log).
- `GET /catalog` (closing #26): every saved query a caller can reach, and the terms it's offered under -
  `cache_ttl`, whether that specific caller can write through it (per-query write curation may differ query
  to query), and the caller's own rate limit alongside the server-wide one. Assembled from data that already
  existed but was scattered across admin-only screens a scoped key can never reach; scoped by the same
  reachability rule `/openapi.json` already uses, so a query a caller can't use is never listed. New
  `config.format_rate_limit()` (the inverse of `parse_rate_limit()`) renders a resolved `(count, seconds)`
  limit back into its human form, e.g. `"100/minute"`. See [The API catalogue](documentation/API.md#the-api-catalogue).
- A **Metrics** tab in the admin UI (closing #29) and a pre-built Grafana dashboard
  (`documentation/grafana-dashboard.json`). The tab parses `/metrics` client-side into stat tiles (requests,
  error rate, active queries, pool idle connections, rate-limit rejections, rows returned), two bar charts
  (requests by status, queries by connection) and a per-connection latency/error table - a live snapshot,
  deliberately with no history or trends, so a deployment with no Prometheus/Grafana stack still gets some
  visibility; needs no API key, since `/metrics` is already public. The dashboard is for deployments that do
  have that stack: request/query rate and latency (p50/p95/p99), error rate, rows returned, active queries,
  pool occupancy and rate-limit rejections, ready to import against a Prometheus scrape of `/metrics`. See
  [Seeing it: a built-in view, or a real dashboard](documentation/API.md#seeing-it-a-built-in-view-or-a-real-dashboard).
- Refreshed admin UI screenshots in the README (Connections, Saved Queries, API Keys, Roles, Run SQL), plus new
  ones for a saved query's History tab, the Audit Log and the Metrics tab.

### Fixed
- The admin UI header broke at common laptop widths once it reached eight tabs: tab labels wrapped onto two
  lines at 1280px, the API key bar's Apply button was clipped at the right edge, and between roughly 980px and
  1200px the header overflowed and scrolled the whole page sideways. Labels no longer wrap, the key bar never
  shrinks, the tab row scrolls horizontally if it ever runs out of room, and below 1240px the tabs move onto
  their own row. Checked with no horizontal page scroll at every width from 390px to 1440px.
- The action and status dropdowns on the Audit Log and saved-query History filters stretched to the full row
  width, pushing the search box onto its own line; they now size to their content.
- The Connections, API Keys, Roles and Audit Log tables now scroll horizontally inside their panel on narrow
  screens instead of widening the whole page.

## [0.4.0] - 2026-09-25

### Added
- Named permission roles (closing #22): a role (`/roles`, admin only) is a reusable *template* for a key's
  `connections`, `allow_writes`, `queries`, `rate_limit`, `allowed_ips` and `allowed_write_ops` -
  `POST /api_keys` with `"role": "<name>"` copies those fields onto the new key once, at creation. It's a
  template, not a live link: editing or deleting a role afterward never touches a key already created from
  it, since `authenticate()` only ever reads the key's own stored entry. A key records which role it came
  from in `created_from_role`, purely informational. `role` can't be combined with an explicit grant field in
  the same request (`400`) - `expires_at` is the one exception, since it's inherently per-key rather than
  part of a shared template. Admin UI gained a Roles tab and a "Create from" picker in the New API key form.
  See [Permission roles](documentation/API.md#permission-roles-templates).
- Encryption at rest for connection passwords (`SQL2API_SECRET_KEY`, closing out #24): a literal password is
  now encrypted with Fernet before it touches disk - existing connections immediately at startup, new ones
  the moment they're saved - and decrypted only in memory at the instant a connection is actually opened.
  Needs the new optional `sql2api[encryption]` extra (bundled in `[all]`); a clear startup error names the
  missing package or a malformed key rather than a raw `ImportError` or silent failure. A `${VAR}` reference
  is untouched either way - never a secret stored in the file to begin with. An encrypted password masks the
  same as a literal one in `GET /connections` and the audit log; a missing or rotated
  `SQL2API_SECRET_KEY` fails a request clearly (`500`, naming the problem) rather than passing ciphertext to
  the driver, and a startup warning fires if encrypted passwords exist on disk with no key configured to
  read them. See [Encryption at rest](documentation/API.md#encryption-at-rest-for-connection-passwords).
- Per-query write curation for API keys: a `queries` entry can now be `{"name": ..., "allow_writes": true}`
  instead of a plain name, granting write access to that one saved query specifically - on top of, never
  instead of, the key's blanket `allow_writes`. Lets a key have `connections: []` and `allow_writes: false`
  yet still write through one curated endpoint (e.g. `submit_order`), the shape the external-client
  `queries` grant was originally built for but couldn't quite express. The `"*"` wildcard can never carry a
  write grant - wanting write access to a specific query means enumerating the whole `queries` list
  explicitly, the same explicit opt-in shape `allow_writes` already has everywhere else. Admin UI's
  saved-query checkbox grid gained a "write" checkbox per query. See
  [Per-query write curation](documentation/API.md#per-query-write-curation).
- Finer-grained query governance, two of three gaps: an API key with write access can now be narrowed to
  specific `allowed_write_ops` (e.g. `["insert", "update"]`) rather than every write keyword being equally
  permitted once `allow_writes` is on - checked in `sqltools.validate_sql()`, never restricts a read-only
  statement. A new `SQL2API_STREAM_MAX_ROWS` caps a `?stream=true` export, which was previously unbounded
  regardless of `SQL2API_MAX_PAGE_SIZE`; past the cap the export ends early, a `WARNING` is logged, and
  `sql2api_stream_exports_total` counts it under a new `status="truncated"`, distinct from `"success"`.
  Admin UI's API Keys tab gained an "Allowed write operations" form field and shows a write-op count on the
  Access column. Table-level allow-listing (the third, hardest gap) remains open - it needs real SQL
  parsing, not the lightweight guard this project deliberately uses. See
  [Write operation granularity](documentation/API.md#write-operation-granularity) and
  [Streaming exports](documentation/API.md#streaming-exports).
- Structured per-query observability fields, second slice (SQL hash, serialization time): the per-query and
  streaming-start log lines now carry a full SHA-256 `sql_hash` alongside the existing full SQL text, so a
  log aggregator can spot "did this same query run elsewhere/before" without storing or searching the SQL
  itself. Response body serialization (JSON/CSV/TSV/XML/YAML/XLSX encoding) is now timed separately from
  query execution - a new `sql2api_serialization_duration_seconds` metric by output format, a
  `serialization_ms` field on the per-request access log line, and a `serialization_ms` field on a saved
  query's `execution_history` entries alongside the existing `duration_ms` (query time) - so encoding cost
  (which can rival query time for XLSX or other large-page exports) is no longer invisible, folded into
  "whatever's left over" between total and query latency. Closes out #19 entirely. See
  [Observability](documentation/API.md#observability).
- Structured per-query observability fields, first slice (query correlation): a saved query's
  `execution_history` entries now carry `request_id` and `key_name`, so a slow or failed run visible in the
  admin UI's History tab can be traced back to the exact log line and caller that produced it. In
  `SQL2API_JSON_LOGS` mode, the per-query, streaming-start, slow-query and per-request access log lines now
  also carry their key fields (`connection`, `dialect`, `limit`/`offset`/`timeout`, `duration_ms`,
  `method`/`path`/`status`) as real top-level JSON keys via `extra={...}`, not just folded into `message` -
  a log aggregator can filter or aggregate on them directly. See
  [Observability](documentation/API.md#observability).
- Basic data visualization in the admin UI: a "Chart" toggle on any tabular result (Run SQL and saved-query
  Run tabs) draws a quick bar chart of the current page, off by default. Deliberately scoped to the page on
  screen, not the full result - a visible note says so, since `page_size` is capped and a chart of one page
  of a much larger result could otherwise look complete without being one. Label/value columns are
  pickable; no charting library, inline SVG matching the editor's own no-dependency approach.
- Audit logging for administrative actions (`GET /audit_log`, admin only): a durable, capped record of
  every API key, connection and saved query created, changed or removed, distinct from live request/query
  observability. An update records a diff of only the fields that actually changed; a create or delete
  records a full snapshot instead. A connection's password is never a value in either form - masked as
  `********` in a snapshot, reported only as the literal string `"changed"` in a diff - and an API key's
  entry never includes its secret or hash. Admin UI gained an "Audit Log" tab. See
  [Audit log](documentation/API.md#audit-log).
- A startup warning for connections storing a literal, non-empty password directly in
  `db_connections.json` instead of a `${VAR}` reference to an environment variable - names every affected
  connection in one line. Doesn't block startup or change stored data; a nudge toward the existing `${VAR}`
  convention, not new enforcement. See [Connections](documentation/API.md#connections).
- Two new `/metrics` series: `sql2api_rows_returned_total` (rows actually returned, by connection, dialect
  and calling key - the trimmed page for a paged query, or however many rows made it out of a streaming
  export before it finished or failed partway through) and `sql2api_active_queries` (a gauge of queries
  currently executing, paged or mid-stream - for a streaming export this stays incremented for as long as
  the client keeps reading, since the connection stays checked out the whole time, not just for the initial
  dispatch). See [Observability](documentation/API.md#observability).
- IP allowlisting per API key (`allowed_ips`, a list of IP addresses or CIDR ranges - IPv4 or IPv6, mixed
  freely): real defense in depth for a key handed to an external party with known, stable infrastructure,
  since even a leaked key then only authenticates from an expected address. Checked in
  `apikeys.authenticate()` against the same client address `SQL2API_TRUST_PROXY`/`ProxyFix` already
  establish as trustworthy for rate limiting, not re-derived. Uses the stdlib `ipaddress` module for parsing
  and containment - no new dependency. A request from outside the list fails exactly like a wrong key
  (`401`), not a distinct error. Restricts *who* may use a key at all, independent of per-key rate limiting
  (how much a caller who is already allowed may do); the admin key is never restricted by it. `PATCH
  /api_keys/<name>` with an explicit `{"allowed_ips": null}` clears an existing restriction, the same
  pattern `expires_at`/`rate_limit` use. Admin UI gained an "Allowed IPs" form field and an "IPs" table
  column. See [IP allowlisting](documentation/API.md#ip-allowlisting).
- Per-key rate limiting (`rate_limit`, e.g. `"100/minute"`): an API key can now carry its own quota,
  checked *in addition to* `SQL2API_RATE_LIMIT`, never instead of it - so handing scoped keys to several
  external clients no longer means they all draw from one shared server-wide budget where a single noisy
  integration can exhaust it for everyone else. Applies even when the server-wide limit is unset entirely.
  Rejections from a key's own limit read `{"error": "Rate limit exceeded for this API key", ...}`,
  distinguishable from the server-wide rejection's plain `"Rate limit exceeded"`, and surface their own
  `X-RateLimit-Key-Limit`/`X-RateLimit-Key-Remaining` headers alongside the existing server-wide pair.
  `PATCH /api_keys/<name>` with an explicit `{"rate_limit": null}` clears an existing per-key limit.
  Required a small architectural addition, not just a new field: `RateLimiter` only ever enforces one
  `(count, period)` spec per instance (by design - it wipes every bucket when a *different* spec arrives, so
  an admin changing `SQL2API_RATE_LIMIT` doesn't mix old and new rules), so two keys with different limits
  need genuinely separate limiter instances - see the new `ratelimit.KeyRateLimiters`, one instance per
  distinct spec actually in use, with keys sharing a spec correctly sharing an instance too. Admin UI gained
  a "Rate limit" field and table column. See
  [Per-key rate limiting](documentation/API.md#per-key-rate-limiting).
- Per-key usage visibility (`last_used_at`): `GET /api_keys` now reports when a key last authenticated a
  request, so a stale key nobody has called in months is easy to spot, or a newly-issued external key's
  wiring can be confirmed. Updated at most once a minute per key (not on every single request, which for a
  busy key would turn every call into a disk write for no real benefit) - read it as "roughly how
  recently," not an exact timestamp. A never-used key simply has no `last_used_at` field. The admin UI's
  API Keys table gained a "Last used" column alongside "Created". See
  [Last used](documentation/API.md#last-used).
- API key expiry (`expires_at`, `YYYY-MM-DD`): a key stops authenticating on its own once the date passes
  (valid through the end of that date), checked live on every request the same way `active` already is - no
  background sweep, nothing to schedule or fail silently. For time-boxed access (a trial integration, a
  partner engagement with a known end date) without anyone having to remember to come back and revoke it.
  `PATCH /api_keys/<name>` with an explicit `{"expires_at": null}` clears an existing expiry without
  rotating the secret; omitting the field from a `PATCH` body leaves whatever expiry a key already had
  untouched. The admin UI's API Keys form gained an "Expires" date field, and the table shows each key's
  expiry (or "never"), visually distinguishing an already-expired key from an active one. See
  [Key expiry](documentation/API.md#key-expiry).
- A "Curl" tab on each saved query in the admin UI, after Run/SQL/History: a ready-to-copy `curl` command
  for that query's `GET /q/<name>` endpoint, with each parameter that has no declared default shown as a
  readable `<name>` placeholder to fill in (not percent-encoded - built as a plain string rather than through
  `URL.href`, which would otherwise turn `<id>` into `%3Cid%3E`) and the API key, if any, redacted to a
  placeholder rather than the session's real value, same policy as the Run SQL tab's own "Copy as curl".
- Per-saved-query API key access (`queries`): a key can now be scoped to a specific list of saved-query
  names, independent of and additive with `connections` - so an external-client key can reach exactly
  `monthly_revenue` and a handful of other approved queries, with no connection access of its own and no
  ad-hoc SQL access, while internal keys keep the existing coarser connection-wide grant unchanged.
  `queries` may also be `"*"` for every saved query by name without ad-hoc access, a middle tier between a
  single connection and full admin. `/openapi.json`/`/docs` now reflect a key's actual reach - a
  `queries`-scoped key sees only its own approved catalogue, not the full internal list of saved queries,
  closing a pre-existing gap where any authenticated key could see every saved query's name, description
  and parameters regardless of its own connection scope. Backward compatible: a key created before this
  field existed keeps behaving exactly as it did. See
  [Per-saved-query access](documentation/API.md#per-saved-query-access-external-clients).
- Streaming exports: `?stream=true` on `POST /execute_sql` and `GET`/`POST /q/<name>` (csv/tsv/ndjson only)
  streams the whole result straight from the database cursor instead of capping it at `page_size` - always
  read-only regardless of `SQL2API_ALLOW_WRITES`, since a large export has no business mutating data (this
  also sidesteps a lot of incidental complexity around commit timing on a connection held open for a long
  download). MySQL (an unbuffered cursor), PostgreSQL (a named, server-side cursor) and ClickHouse
  (`execute_iter`) stream without the driver buffering the whole result client-side first - verified
  end-to-end against real servers: 1 million rows streamed over real HTTP with the server process's own
  memory sampled throughout stayed flat (~49-55MB for MySQL/PostgreSQL), against several hundred MB fetching
  the same result the ordinary way. SQLite, H2, the generic `jdbc` type and DuckDB still bound this
  project's own memory to one batch at a time regardless of result size, even where the underlying engine or
  driver holds more than that internally (documented per-dialect, not assumed - see the `_DuckDB`/`_H2`/
  `_Postgres` driver docstrings in `runners.py`). A PostgreSQL-specific quirk only a real server surfaced:
  a named cursor's `execute()` is really a `DECLARE CURSOR` under the hood and does not run the query at all
  - column info and `statement_timeout` cancellation are only available after the *first fetch*, the reverse
  of every other driver here. See [Streaming exports](documentation/API.md#streaming-exports).
- Generic DuckDB connections (`db: "duckdb"`) - opt-in via `sql2api[duckdb]`, needing no external runtime
  (a native Python extension, like SQLite). One connection type covers two uses: a genuinely capable
  embedded database (`database: <path>`, same shape as SQLite) and querying CSV/JSON/Parquet files directly
  from a saved query's own SQL (`SELECT * FROM read_csv(:path)`), no import step or new connection fields.
  Schema introspection, pooling and the SQL guard's ANSI (quote-doubling) literal rules all apply unchanged.
  Two things a real DuckDB database surfaced that aren't in its docs: opening a second connection to a file
  with a different read-only setting than one already open on it in-process fails outright, so - like
  `h2`/`jdbc` - every pooled connection here is opened read-write and the read-only guarantee rests on the
  SQL guard alone; and unlike `h2`/`jdbc`, the query time limit *is* enforced, via `Connection.interrupt()`
  on a background timer, since DuckDB's Python client (unlike JDBC through jaydebeapi) exposes a real
  cancellation hook. See [DuckDB connections](documentation/DATABASE_CONNECTION_CONFIGURATION.md#duckdb-connections).
- A show/hide toggle on the connection form's password field, in both create and edit mode - verified with
  a real headless-Chrome test that it doesn't disturb the existing password-mask round-trip.
- The Run SQL tab's stat bar now leads with the response's HTTP status code and status text
  (Postman-style "200 OK · 1 row · json · 8 ms"), accent-colored to read as success at a glance; the error
  path is unchanged. Verified with a real headless-Chrome test.
- The admin UI's Run SQL tab and New saved query drawer have a Schema panel next to the SQL editor: lists
  the selected connection's tables, expands to show columns (type/nullability as a tooltip), and clicking a
  table or column inserts its name at the cursor. Updates automatically when the connection changes; a
  connection whose schema isn't available shows that message inline rather than through the page's error
  banner. A thin client of the existing `GET /connections/<name>/schema` endpoint - no new backend logic.
  Verified end-to-end with a real headless Chrome (Playwright) test covering both mount points, expand/
  collapse, click-to-insert for both tables and columns, connection-switch reactivity, the unsupported-
  dialect message, and the refresh button - zero uncaught JS errors.
- `mypy` runs in CI (gradual typing - `[tool.mypy]` in `pyproject.toml`; the codebase has no type hints yet,
  so it catches genuine static errors rather than demanding annotations everywhere). It found two real, if
  low-impact, issues fixed here: `runners._Driver.DIALECT` was untyped, so mypy inferred `None` as its exact
  type and flagged every dialect subclass for assigning a string to it; and `metrics.py`'s module-level
  counters had no annotation for their (tuple key -> value) shape. mypy 2.x dropped support for running on
  Python 3.9 (`Requires-Python >=3.10`), so CI's 3.9 job resolves the last compatible 1.x release instead -
  which, it turned out, disagrees with 2.x about whether `ignore_missing_imports` alone covers a module
  that's installed but ships no type stubs (PyYAML's `import-untyped` error, as opposed to one mypy can't
  find at all). `disable_error_code = ["import-untyped"]` covers it on both mypy generations.
- Test coverage is measured in CI and uploaded to [Codecov](https://codecov.io/gh/AnanthaRajuC/SQL2API) (a
  badge is in the README) - currently 93% across `sql2api/` from the unit test suite alone (not counting the
  real-database integration tests).
- Python 3.14 added to the CI test matrix.
- Generic JDBC connections (`db: "jdbc"`) reach any database not covered by a dedicated driver - Oracle,
  SQL Server, DB2, Snowflake and others - by generalising the embedded-JVM approach `h2` already used.
  A connection needs `jar` (the vendor's driver jar), `driver_class` and `jdbc_url` instead of
  `host`/`port`/`database`; everything else (the SQL guard, parameter binding, pooling, output formats)
  is unchanged. Two limits are inherent to sharing one JVM per process, not specific to this connection
  type, and are documented in [DATABASE_CONNECTION_CONFIGURATION.md](documentation/DATABASE_CONNECTION_CONFIGURATION.md#generic-jdbc-connections):
  `SQL2API_QUERY_TIMEOUT` is not enforced (no portable way to cancel a statement across arbitrary JDBC
  drivers), and a *new* `jdbc` connection whose jar was not already on the classpath when the JVM first
  started needs the server restarted before it can be used. Schema introspection
  (`GET /connections/<name>/schema`) answers 400 for this connection type rather than guessing at a
  vendor's system catalogue. Verified end-to-end against a real H2 server reached through the *generic*
  driver (not the dedicated `h2` one) - connection pooling, bound parameters, client-side pagination
  (LIMIT/OFFSET is not portable SQL either, so it is no longer appended server-side for this type), the
  read-only guard, and the schema-browser boundary - plus the JVM classpath-union logic that lets an H2
  and a jdbc connection share the one JVM regardless of which one is used first.
- The admin UI (`/ui`) has an API Keys tab: create, edit and revoke scoped API keys, matching the
  connections/saved-query tabs' style. A freshly created key's secret is shown once, with a copy button,
  the same one-time reveal the API itself enforces. A scoped (non-admin) key sees the same "only the admin
  key" message here as on the Connections and Saved Queries tabs. Verified end-to-end with a real headless
  Chrome browser: create, the secret-reveal, edit (including switching between the `"*"` wildcard and
  specific connections), revoke, and the admin-only empty state for a scoped key - zero uncaught JS errors
  across the whole flow.
- Audit logging: log lines and `/metrics` now carry the name of the API key that made the request - `admin`
  for `SQL2API_API_KEY`, a scoped key's own name, or `-` when no key is configured at all. Distinguishing
  `admin` from `-` needed splitting what was one "no key configured" state into two in `apikeys.Permission`.
  Kept off the latency histograms so the number of keys never multiplies their bucketed output; requests and
  query counts still carry it.
- Opt-in response caching for saved queries: set `cache_ttl` (seconds) when saving one. A cache hit answers
  with the identical body and `X-Cache: HIT` (`X-Cache: MISS` on a fresh response), carries `ETag` and
  `Cache-Control: max-age=<cache_ttl>`, and honours `If-None-Match` with a bodyless `304`. Never used for a
  saved query whose SQL is a write, regardless of `cache_ttl` - serving a cached response in its place
  would silently skip that write - and a cache hit is not recorded in `execution_history`, since nothing
  ran against the database. Verified against a real MySQL server, including that a cached write still runs
  on every call.
- A documentation site (MkDocs, Material theme), built from this README and `documentation/` - there is
  still exactly one place to edit each document; `docs/` only mirrors their paths so cross-links keep
  working. Deployed to GitHub Pages by `.github/workflows/docs.yml` on every push to `main` that touches a
  doc file (needs a one-time `Settings -> Pages -> Source: GitHub Actions` from a repository admin).
- Per-key API permissions: `SQL2API_API_KEY` stays a full-access admin key, unchanged. New scoped keys
  (`POST /api_keys`, admin only) are each limited to a list of connection names (or every connection) and
  can be denied write access even when the server otherwise allows it - a key's `allow_writes` can only
  narrow `SQL2API_ALLOW_WRITES`, never widen it. Only the admin key can manage connections, saved queries
  or other API keys. A key's secret is never stored, only its SHA-256 hash in `api_keys.json`; the server
  generates it and shows it exactly once, when the key is created. Creating the first scoped key turns on
  authentication for the whole server immediately, even without `SQL2API_API_KEY` set (the server warns at
  startup if that would lock configuration changes out, since only the admin key can manage the server).
- Observability: every response carries `X-Request-Id`, and log lines written while handling that request
  carry the same ID (plain text by default; `SQL2API_JSON_LOGS=1` for one JSON object per line). A query
  taking at least `SQL2API_SLOW_QUERY_THRESHOLD` seconds (default 1) is logged as a warning. `GET /metrics`
  (always public, like `/health`) serves Prometheus text-format metrics: request and query counts/latency
  histograms (by endpoint/status and by connection/dialect), idle pool occupancy, and rate-limit rejections.
  Logging is now configured once inside `create_app()`, so it applies under gunicorn/WSGI too, not just
  `sql2api serve` - previously `INFO`-level application logs were silently dropped in that path.
- `GET /connections/<name>/schema` lists a connection's tables and views with their columns (name, type,
  nullability, position) - self-service query writing without leaving the API. One catalogue query per
  database (`information_schema` for MySQL/PostgreSQL/H2, `system.tables`/`system.columns` for ClickHouse,
  `sqlite_master`/`pragma_table_info` for SQLite), run through the normal read-only execution pipeline, so
  it needs no new driver logic. Capped at 5000 columns per connection (`truncated: true` if a schema is
  larger than that).
- A small admin UI at `/ui`: manage connections (including proper password-mask round-tripping) and saved
  queries (create, run, per-version delete, an execution-history view per version), and run ad-hoc SQL with
  a syntax-highlighted editor, page-size presets and Next/Previous paging - without leaving the browser.
  Self-contained (no build step, no external script or stylesheet) and a pure client of the existing JSON
  API - no new server-side logic. Linked from `/docs`, and shares its `X-API-Key` storage with the docs
  page. Query results are always rendered through DOM APIs, never `innerHTML`, so a value coming back from
  a database can never execute as markup - verified with a 39-assertion real-browser (Playwright) test,
  including an XSS-payload check, run both unauthenticated and with an API key set.
- A documentation callout pointing out that `/openapi.json` can be imported directly by URL into Postman or
  Insomnia to get a ready-made request collection - no separate export step.
- An "Explain" button next to Run on the Run SQL tab: runs `EXPLAIN <current query>` through the existing
  execute path and shows the plan, a UI shortcut for a statement the SQL guard already allows.
- A collapsible "Headers" panel under query results, listing every header the response actually carries
  (`X-Page`, `X-Request-Id`, etc.), built from data `renderResponse()` already has.
- "Copy as curl" and "Copy as TSV" buttons on the Run SQL tab's results: the former builds the exact `curl`
  command for the request just made (with the API key, if any, redacted to a `YOUR_KEY_HERE` placeholder so
  copying the command doesn't leak the key), the latter copies the current result rows as a paste-ready TSV
  table for Excel/Sheets - both pure client-side transforms of data already on hand.
- A `sessionStorage`-backed ad-hoc query history on the Run SQL tab: the last 20 distinct queries run,
  clickable to restore into the editor, surviving a page reload within the same tab.
- A "preview" affordance next to each table in the schema browser (both the Run SQL tab's panel and the New
  saved query drawer's) that fills in and runs `SELECT * FROM <table>` through the existing execute path, so
  a table's data can be seen without hand-writing SQL.
- A collapsible JSON tree for non-tabular `/execute_sql` responses (anything that isn't a JSON array),
  replacing the previous flat `<pre>` dump - each object/array level can be expanded or collapsed.

  All eight of the above were verified end-to-end with real headless-Chrome (Playwright) tests against a
  live server, including a dedicated clipboard-reading test for "Copy as curl"/"Copy as TSV" and coverage of
  both schema-browser preview entry points - zero uncaught JS errors across the runs.

### Fixed
- `GET /list_files` returned 404 ("Folder not found") on a brand-new install before anything had ever been
  saved, instead of an empty list - inconsistent with the very similar `latest_versions()` used for the
  OpenAPI catalogue, which already handled this correctly. A list endpoint with nothing to list now
  answers `{"files": []}` with 200, as it always should have.
- **Security hardening (MySQL/ClickHouse):** the single-statement/read-only SQL guard now reads string
  literals with the quoting rules the *target database* actually uses. MySQL and ClickHouse honour a
  backslash escape inside `'...'`/`"..."` string literals by default; PostgreSQL, SQLite and H2 do not.
  The guard previously used one, doubling-only rule for every database. For MySQL/ClickHouse connections,
  a crafted value (ending in an escaped quote, more text, then a closing quote) could make the guard
  think a `;` was safely inside a string literal when the database would treat it as a live, second
  statement - confirmed against real MySQL and ClickHouse servers. No path to unauthorized data access or
  modification was found on the current codebase (this project's runners never call `cursor.nextset()`,
  so on MySQL the smuggled statement was queued but never pulled, and MySQL's own read-only-transaction
  mode - already set on every read-only connection - independently rejects a smuggled write; ClickHouse's
  server independently refuses multi-statement queries outright) - but it was a real gap in an explicitly
  documented guarantee and is now fixed with a dialect-aware guard, covered by a fuzz/property test suite
  (`tests/test_sql_guard_fuzz.py`, using [Hypothesis](https://hypothesis.readthedocs.io/)) that pins the
  exact confirmed payload and its outcome on each database. `--` comments now also require a following
  whitespace character or end of input, matching real SQL comment syntax, and backtick-identifier doubling
  (`` `` ``) is now recognised - both changes only make the guard *more* likely to reject ambiguous input,
  never less.

## [0.3.0] - 2026-09-21

### Added
- Docker images are published to GitHub Container Registry on every release (`ghcr.io/anantharajuc/sql2api`, tags
  `X.Y.Z` and `latest`, plus `-h2` variants with Java and the H2 driver), for `linux/amd64` and `linux/arm64`. The
  workflow tests each image before publishing and can be run by hand as a dry run.
- `docker compose up --build` starts a self-contained demo: SQL2API in front of a seeded PostgreSQL database, with
  example saved queries, an API key and a rate limit. CI runs it on every change.
- The image has a `HEALTHCHECK` on `/health`, OCI labels, and access logging.
- Parameter rules for saved queries: besides a type, `query_parameters` can declare `default`, `required`, `enum`,
  `min`/`max`, `min_length`/`max_length`, `pattern` and `description`. Violations are rejected with a 400 that lists
  every problem in an `errors` map; optional parameters without a value are bound as NULL.
- Every saved query is documented as its own endpoint in `/openapi.json` and `/docs`, with its parameters, rules and
  default connection (never its SQL). With an API key set, this section is only shown to authenticated readers, and
  the docs page has a box for the key.
- The Release workflow now refuses to publish when the tag does not match `sql2api.__version__`, is not on `main`,
  or has no dated changelog section.
- CORS support for browser clients (`SQL2API_CORS_ORIGINS`, off by default): allowed origins are echoed back,
  preflight requests are answered without an API key, and the pagination and rate-limit headers are exposed to the
  page. Starting with `*` and no API key logs a warning.
- Rate limiting (`SQL2API_RATE_LIMIT`, e.g. `60/minute`, off by default): a per-client token bucket answering `429` with
  `Retry-After`, plus `X-RateLimit-Limit`/`X-RateLimit-Remaining` headers. It runs before the API key check so key
  guessing is throttled; `/health` and preflights are exempt. A malformed value stops startup.
- `SQL2API_TRUST_PROXY` (number of reverse proxies) makes the app use the client address and scheme from
  `X-Forwarded-*` headers; without it those headers are ignored so they cannot be forged.

### Changed
- Saving a query validates its `query_parameters` and rejects declarations that the SQL does not use.
- Requests rejected by parameter validation are not recorded in `execution_history`.

### Fixed
- The Docker image ran gunicorn with its default 30 second worker timeout, the same as the default query time limit,
  so a query hitting its limit raced gunicorn killing the worker. The timeout is now 120 seconds.
- The OpenAPI document was not valid OpenAPI 3.0 (`exclusiveMinimum: 0` and empty `required` lists), which strict
  tools and client generators reject. It is now validated in the test suite.

## [0.2.0] - 2026-09-21

### Added
- Connection pooling for MySQL, PostgreSQL, ClickHouse and H2: connections are reused between requests instead of
  opened per request (`SQL2API_POOL_SIZE`, default 5 idle connections per distinct setting, `0` disables;
  `SQL2API_POOL_IDLE_TIMEOUT`, default 300 s). Against a local server, per-request time for a trivial query dropped
  from about 14 ms to 0.5 ms on MySQL and H2; ClickHouse barely changed (about 1.2 ms to 1.0 ms).
  Connections are reset between users, health-checked after idling, discarded after errors, and closed at once when
  a connection is changed or deleted through the API.
- Query time limit: statements are cancelled on the database after `SQL2API_QUERY_TIMEOUT` seconds (default 30,
  `0` disables) and the request fails with HTTP 504. A request can lower the limit with `?timeout=` (or a `timeout`
  field in the body) but never raise it. Enforced natively on MySQL/MariaDB, PostgreSQL, ClickHouse, SQLite and H2.

### Changed
- Queries that run longer than 30 seconds are now cancelled by default. Set `SQL2API_QUERY_TIMEOUT=0` to restore the
  previous unlimited behaviour.

### Fixed
- The process could hang on exit after H2 had served concurrent requests: JPype waited forever for worker threads
  that jaydebeapi had attached to the JVM as non-daemon threads. Threads that use H2 are now attached as daemons.

## [0.1.0] - 2026-09-21

First public release, restructured from the original single-file application.

### Added
- Installable `sql2api` package with a `sql2api serve` / `sql2api init` command line and `python -m sql2api`.
- Bound query parameters (`:name`) for every database, so values never become part of the SQL text.
- Saved queries served as endpoints: `GET|POST /q/<name>` with typed parameters and an optional default connection.
- `DELETE /saved_sql/<name>` (whole query or one `?version=`) and `DELETE /connections/<name>`.
- Execution history recorded per saved-query version (last 50 runs, with status, row count and duration).
- `X-Page`, `X-Page-Size` and `X-Has-More` response headers; `ndjson` output format.
- `${ENV_VAR}` references in connection settings so secrets can stay out of `db_connections.json`.
- OpenAPI description at `/openapi.json`, Swagger UI at `/docs` (`/` redirects there), and a `/health` endpoint.
- Read-only-by-default execution (`SQL2API_ALLOW_WRITES`), optional API key (`SQL2API_API_KEY`),
  page size limit (`SQL2API_MAX_PAGE_SIZE`) and configurable data folder (`SQL2API_HOME`).
- Dockerfile, GitHub Actions CI (unit tests plus integration tests against PostgreSQL, MySQL, ClickHouse and H2),
  Dependabot, and runnable examples.

### Changed
- Saved-query and connection files are read from `SQL2API_HOME` (default: the current directory) instead of the
  folder next to the source file; file access is confined to `saved_sql/`.
- Passwords are masked by `GET /connections`.
- Unknown `format` values now return 400; `page_size` is capped; errors use proper HTTP status codes.
- The development server no longer runs in debug mode by default.

### Fixed
- Arbitrary file read through `/view_file_content` and path traversal through saved-query filenames.
- Pagination now works on every database; trailing `LIMIT`/`OFFSET` handling is case-insensitive.
- Database connections are always closed; ClickHouse queries no longer run twice.
- JSON column order is preserved; Decimal, date and driver-specific number types serialise correctly.
- Concurrent saves can no longer lose a version.

[Unreleased]: https://github.com/AnanthaRajuC/QueryAPIGate/compare/v0.8.0...HEAD
[0.8.0]: https://github.com/AnanthaRajuC/QueryAPIGate/releases/tag/v0.8.0
[0.7.1]: https://github.com/AnanthaRajuC/QueryAPIGate/releases/tag/v0.7.1
[0.7.0]: https://github.com/AnanthaRajuC/QueryAPIGate/releases/tag/v0.7.0

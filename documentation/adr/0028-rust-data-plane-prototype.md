# ADR 0028: A Rust data plane for `/q/`, as a prototype with a go/no-go decision

- **Status:** Proposed (2026-10-27)
- **Backlog:** 0.44. Motivated by the CPU profiles in [PERFORMANCE_MYSQL_V2.md](../PERFORMANCE_MYSQL_V2.md#where-the-time-goes-now)
  and [PERFORMANCE_V4.md](../PERFORMANCE_V4.md). This is a prototype: it ends in a measured decision, not a commitment
  to ship a second runtime.

## Context

After 0.43, the gateway, not the database, limits throughput. Serving `order_status` at 32 callers, one worker used
1.2-1.3 cores while MySQL used 0.07-0.52 ([MySQL v2](../PERFORMANCE_MYSQL_V2.md)). That is about 1.9 ms of gateway CPU
per call against 0.1-0.8 ms in the database, so the same MySQL could take several times the calls the gateway sends
it. Per gateway core, one worker serves about 520-620 calls a second, and that halves by eight workers as they share
hyperthreads.

The cost is spread across the whole request path, so there is no single hotspot to port:

| Share of one worker's CPU (0.43, `order_status`) | |
|---|---|
| Store layer: the key, the query and the connection | 23% (4% with the metadata cache) |
| Data query: mysql-connector and the pool | 20% |
| Logging | 15% |
| gunicorn, sockets, HTTP | 14% |
| Flask and Werkzeug | 7% |
| SQL checks, parameters | 5% |
| JSON | 5% |
| History writer thread | 5% |

0.43 already took out what it could within Python: round trips, the GIL-holding ping, repeated store reads and
filesystem checks. What remains is the cost of running a request through Python at all. Only moving the whole hot path
changes that. The planning estimate is about 10x less CPU per call. It hasn't been measured, and network round trips
and database time won't improve whatever the language.

Security parity matters more than speed. A faster path that answers differently from the Python one, including
refusing differently, is a regression.

## Decision

Build a prototype **data plane in Rust that serves `GET`/`POST /q/<name>`** for the calls it fully supports, and
**forwards everything else, unchanged, to the existing Python server**. Measure it against 0.43, gate it on a
differential test, then decide go or no-go.

### 1. Shape: a front that forwards whatever it doesn't fully support

The data plane listens on the public port. gunicorn runs behind it, on a loopback port or a Unix socket. For each
request:

- **`/q/<name>`, and every feature that call uses is supported** (section 4): served in Rust.
- **Anything else**: forwarded to Python as it came, and its response passed back. That covers the Console, the
  Management API, `/execute_sql`, MCP, `/events`, `/metrics`, docs, health, and any `/q/` call using an unsupported
  feature.

The rule is **when in doubt, forward**. Every check that decides whether Rust serves a call fails towards Python, so a
gap in the prototype costs speed, never correctness. The Console, Management API, CLI and the store schema stay in
Python. Both processes read the same store, and Python stays the only writer of definitions: keys, queries,
connections and roles.

### 2. Governance: Python decides, Rust applies

The prototype **doesn't reimplement the SQL checks in Rust.** A saved query's SQL is fixed by its publisher, so for
the calls the data plane serves, the table check and the masking trace depend only on things Python can work out once:
the query version's SQL, the connection's dialect, and the caller's grants. Masking also depends on the connection's
schema.

So when Python runs a `/q/` call that the data plane could serve, it also writes a **plan** to the store. A plan holds:

- the verdict: allowed, or refused with its status and error code;
- the statement to send, already converted from `:name` to the driver's positional parameters, the parameters'
  order, and the paged form Python's runner builds;
- whether the call is read-only;
- for each result column, the mask action and its argument, or none;
- the expected column count, so a result of a different shape is refused (`mask_unresolvable`) exactly as Python
  refuses it;
- what it was computed from, and when it expires.

A plan is keyed by workspace, query, version, connection, a fingerprint of the caller's grants (`allowed_tables`,
`allow_writes`, `allowed_write_ops`), and the fingerprint of the effective mask that `masking.fingerprint` already
computes. A masked plan expires with the schema it was traced against: at most `masking.SCHEMA_TTL`, 60 s, the limit
Python holds itself to. An unmasked plan depends on no schema, and changes only when its key does.

**On a plan miss, Rust forwards the call.** Python serves it normally and writes the plan, and the next such call is
served in Rust. The data plane never computes a verdict of its own. It applies one Python computed, or it forwards.

This replaces the gate first planned for 0.44: fuzzing the same queries through sqlglot in Python and sqlparser-rs in
Rust, and treating any disagreement as a bug. With plans, there is one implementation of the SQL governance, not two
to keep in agreement, and the differential test (section 5) checks the whole response instead.

**Saved queries with legacy `{name}` text placeholders are always forwarded.** Their values become part of the SQL
text (`sqltools.fill_placeholders`), so the statement, and with it the verdict, can change from one call to the next.

### 3. What the data plane does for a call it serves

Each of these matches what Python does today, and the differential test checks each one:

1. **The request ID** (`X-Request-Id`, accepted or generated by the same rules), and the client address: the real
   socket peer, or `X-Forwarded-For` only under `QUERYAPIGATE_TRUST_PROXY`, as `_CapturePeer` and `ProxyFix` do.
2. **Authentication, `X-API-Key` only.** The SHA-256 lookup on `api_keys.hash` or `previous_hash` (within its grace
   period, ADR 0009), then `active`, expiry and `allowed_ips`, with the same constant-time comparisons. It records
   `last_used_at`, throttled the same way. A wrong, inactive, expired or out-of-range key gets the same 401 as Python
   gives, with nothing to say which. Any other credential is forwarded: the break-glass admin key, administrator
   tokens, JWTs and SSO sessions.
3. **The saved query:** the published version only, with drafts and `?version=` forwarded. Then the `queries`,
   `collections` and `connections` grants, including the rule that a by-name grant reaches only the query's own
   connection.
4. **Parameters:** the same type rules, defaults, required checks and error codes as `params.py`, with the values
   bound, never put into the SQL text.
5. **Running it on PostgreSQL or MySQL** with ADR 0027's prefix and read-only mode, the effective timeout (`?timeout`
   may lower the server's, never raise it), and paging: `page`, `page_size`, `QUERYAPIGATE_MAX_PAGE_SIZE` and the
   caller's `max_rows`.
6. **Masking**, applied by column position from the plan: `hash` with the same key and output, plus `mask`, `null`
   and `drop`.
7. **The response:** byte-identical JSON for every column type both drivers return (numbers and decimals, dates and
   times, time zones, bytes, nulls). Also `X-Row-Count`, the security headers (`secheaders.py`) and CORS.
8. **The run's record:** the same `record_execution` rows in the same tables, batched like Python's history writer;
   the request log line in the same JSON format, under the same `QUERYAPIGATE_LOG_REQUESTS` and
   `QUERYAPIGATE_ACCESS_LOG`.

### 4. What the prototype forwards

| Forwarded | Why |
|---|---|
| Other connection types: SQLite, DuckDB, ClickHouse, Trino, MongoDB, H2, JDBC | Prove the idea on the two benchmarked databases first |
| `?stream=true`, and formats other than JSON | JSON is what the benchmarks serve; the others are renderers to port later |
| Queries with `cache_ttl` | The response cache, local or Redis, keeps its own state |
| Keys with a rate limit or usage quota; any server with `QUERYAPIGATE_RATE_LIMIT` set | Limits must hold across both processes. Without Redis they are per process, so they need shared counters first |
| MCP, agent writes, `embed` parameters, JWT claim binding | Not on the hot path being measured |
| Writes (a key or query that may write) | Reads first; write limits and `allowed_write_ops` come after |
| Workspace paths; tracing (`OTEL_EXPORTER_OTLP_ENDPOINT` set) | Default workspace and no tracing in the prototype |

Each row is a reason for Rust to forward, checked before it serves anything. Moving a row out of this table is a
separate change, with its own differential tests.

### 5. The gate: a differential test of whole responses

A harness sends the same calls to plain Python 0.43 and to the data plane in front of the same code, store and
databases, and compares:

- the status, the error `code` and message, the response body byte for byte, and the headers that carry meaning:
  `X-Row-Count`, the security headers and CORS;
- the history rows each run leaves.

The inputs are generated:

- keys with and without `allowed_tables` and mask rules;
- the saved queries of the TPC-C and OLTP benchmarks, plus the masking fuzz's statement shapes saved as queries;
- parameters that are valid, missing, extra, of the wrong type, out of range, or hostile text;
- revoked, expired and rotated keys; changed grants and changed schemas while calls are running.

**Any difference is a bug, a refusal included.** It must also be true that every call the data plane serves, it
serves with Python's answer: a call that can't be shown to match is forwarded. The harness runs on every change to
the data plane, and before any benchmark is reported.

### 6. Measurement, and the decision

Measured with `benchmarks/oltp.py` (PostgreSQL) and `benchmarks/tpcc_mysql.py` (MySQL), with the same pinned cores,
interleaved rounds and medians with ranges as v4 and MySQL v2:

- calls a second per gateway core, counting the data plane's cores and Python's together, for plain, masked and
  table-restricted lookups;
- p50 and p99 at a fixed load, with the database on the same host and 0.5 ms away;
- the forwarded path's added latency (the extra hop), for the calls that still go to Python;
- memory per process.

The decision:

- **Go:** at least 3x the calls a second per gateway core on the plain and governed lookups, p99 no worse, and the
  differential test clean. Then a follow-up ADR covers shipping it: packaging, the Docker image, and which rows of
  section 4 to move.
- **Between 1.5x and 3x:** report it, and weigh it against the alternatives below before deciding.
- **Under 1.5x, or any differential failure that can't be fixed within the design:** stop. The prototype and its
  results stay as a documented experiment.

### 7. Security notes

- **Open-access mode** answers local clients only, judged by the real socket peer. Behind the data plane, every
  forwarded request reaches Python from loopback. So the data plane applies the rule itself, on its own socket peer,
  and refuses to start in front of a server with no credential configured unless it listens on loopback only.
- **The trusted-proxy hop count** gains one when the data plane fronts Python. Python must trust `X-Forwarded-For`
  only from it: over a Unix socket, or from loopback with a hop count of exactly one.
- **Plans are security decisions held in the store.** Only Python writes them, they are keyed by everything they
  depend on, and the data plane refuses a plan whose fields don't match the call. A plan doesn't widen anything: the
  key, its grants and the query are still read and checked on every call.
- The data plane gets an entry in THREAT_MODEL.md before any benchmark is published.

### 8. Where it lives

A Cargo crate in `dataplane/`: Axum and Tokio, sqlx for PostgreSQL, MySQL and the SQLite store. It is built by the
benchmark harness and the differential test only: it isn't in the Docker image, the PyPI package or the Helm chart
while it's a prototype. The CI job that builds it and runs the differential test is added with the pull request that
reports the result.

## Consequences

- **A second language in the repository,** with its own toolchain, dependencies and Dependabot ecosystem. That's the
  main cost of a go, and the main reason this starts as a prototype.
- **The SQL governance stays in one implementation.** The data plane can only apply verdicts Python computed. A plan
  miss costs one forwarded call, not a second verdict that might disagree.
- **The first call to each query, key and schema goes through Python,** and so does every call using a feature in
  section 4. A deployment's speed-up therefore depends on its traffic. The benchmarks report it by call type, not as
  one number.
- **Live events and `/metrics`:** runs served in Rust are in the history, but the prototype doesn't publish them to
  Python's in-process broadcaster or counters. Without Redis, the Console's live Home feed and `/metrics` miss them. A
  go needs both, through Redis or the store.
- Python keeps every feature. Turning the data plane off means pointing traffic at gunicorn again.

## Alternatives considered

- **Port only the hot functions to Rust (PyO3) inside Python.** The SQL checks and JSON are 10% of the CPU between
  them. Logging, gunicorn, Flask and the store layer would still run in Python, so this caps the gain well below the
  target.
- **Port the SQL governance to Rust too (sqlparser-rs), fuzzed against sqlglot.** The plan first written for 0.44. It
  is two implementations of the most security-sensitive code, kept in agreement by a fuzz that can only sample the
  space. Plans (section 2) keep one.
- **Async Python: Starlette or another ASGI framework on Granian or uvicorn.** Removes gunicorn's and Werkzeug's
  overhead, and part of Flask's. Estimated 1.3-2x, keeping one language. It is the comparison point if the prototype
  lands between 1.5x and 3x.
- **Free-threaded Python (3.13, 3.14).** More cores per worker, but the same CPU per call, and it waits on
  free-threaded builds of psycopg2 and mysql-connector.
- **Rewrite the whole server in Rust.** The Console, the Management API and the configuration code aren't on the hot
  path, and rewriting them risks every guarantee they carry for no measured gain.

## Open questions

- Should the data plane read the store through the metadata cache's rules (`QUERYAPIGATE_METADATA_CACHE_TTL`), or
  always read key and query rows fresh? Matching Python's setting is the default.
- Is the plan written on Python's normal path enough, or should Python precompute plans when a query is published or
  a key is changed, so the first call is fast too?
- On a PostgreSQL store, should plans live in their own table, or in Redis when it is configured?

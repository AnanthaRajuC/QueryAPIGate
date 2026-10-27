# ADR 0028: A Rust data plane for `/q/`, as a prototype with a go/no-go decision

- **Status:** Accepted (2026-10-27). Amended 2026-10-27, before any Rust code: plans travel back on the forwarded
  response and are kept in the data plane's memory, not in the store; they name the rows they were computed from by
  digest; and the data plane sends SQL as Python's drivers do (sections 2 and 3). Amended again while building it: served
  runs are reported to Python, which records them, and the cases the implementation forwards are listed (sections 3, 4
  and Consequences). Amended a third time after the first benchmark: Python's work per reported run couldn't keep up
  with what the data plane serves, so runs arrive already serialized and are queued a batch at a time (section 3, item
  8, and Consequences).
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

So when Python serves a `/q/` call that the data plane forwarded and could serve, its successful response also
carries a **plan** (`queryapigate/dataplane.py`), in an `X-QAG-Plan` header the data plane removes before the
response leaves. Python sends one only to a request carrying `QUERYAPIGATE_DATAPLANE_SECRET`, a header the data plane
removes from whatever a client sends. A plan holds:

- the statement to send, in segments around its bound `:name` parameters, with its own trailing row window apart,
  each operand a number or a parameter, resolved per call as `sqltools.paginate` does;
- on MySQL, where the time-limit hint goes;
- the `search_path` a table-restricted PostgreSQL call pins;
- for each result column, the mask action or none, and the column count the trace expects, so a result of a
  different shape is refused (`mask_unresolvable`) exactly as Python refuses it;
- the digests of the rows it was computed from, and when it expires.

A refusal gets no plan: the data plane forwards that call again, and Python refuses it again.

**A plan names its rows by digest.** While serving a data plane's request, Python reads the key, the saved query and
the connection fresh, bypassing the metadata cache, and records a SHA-256 digest of each row set exactly as it was
read (`metacache.digest_rows`). The data plane runs the same three SELECTs for every call it serves, and uses a plan
only when its own rows digest the same. So any change to a key's grants, a query's version, its mask rules or the
connection, made anywhere, retires every plan built from the old rows. A row that changes while the call is in
flight can't pair a plan with rows it wasn't computed from.

A masked plan also expires with the schema it was traced against: at most `masking.SCHEMA_TTL`, 60 s, the limit
Python holds itself to. An unmasked plan depends on no schema. Plans live in the data plane's memory, per process,
so a restart starts empty and the store gains no table.

**On a plan miss, Rust forwards the call.** Python serves it normally and returns the plan, and the next such call is
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
5. **Running it on PostgreSQL or MySQL** as Python's drivers do: one text-protocol message, each parameter's value
   filled in as a quoted literal client-side, as psycopg2 and mysql-connector do for the runner's `%s` markers. Also
   ADR 0027's prefix and read-only mode, the effective timeout (`?timeout`
   may lower the server's, never raise it), and paging: `page`, `page_size`, `QUERYAPIGATE_MAX_PAGE_SIZE` and the
   caller's `max_rows`.
6. **Masking**, applied by column position from the plan: `hash` with the same key and output, plus `mask`, `null`
   and `drop`.
7. **The response:** byte-identical JSON for every column type both drivers return (numbers and decimals, dates and
   times, time zones, bytes, nulls). Also `X-Row-Count`, the security headers (`secheaders.py`) and CORS.
8. **The run's record, reported to Python.** The data plane writes nothing to the store. It sends each run to
   `POST /internal/dataplane/runs` in batches (every `QUERYAPIGATE_HISTORY_FLUSH_INTERVAL`, up to four at once): its
   history entry already serialized as `json.dumps` writes it, and the labels and timings `/metrics` counts it by.
   Python queues a batch's history rows and events at once (`history.record_reported`), sampled as its own runs are,
   and counts each set of labels once. Recording a run this way costs Python about 7 µs, against 27 µs through
   `_record_and_broadcast`: at the data plane's rates that difference decided whether runs were kept. Sampling,
   retention, the event log, NOTIFY on a PostgreSQL store, the Console's live feed and the metrics stay in one place.
   Runs the data plane can't queue are counted with the next batch, as `dropped` in
   `queryapigate_history_runs_total`. The endpoint answers 404 to anything without the data plane's secret, is outside
   rate limiting, and isn't counted as a request itself. A batch lost in a crash is lost as Python's own queue is.
9. **The log lines**, in Python's plain or JSON format, under the same `QUERYAPIGATE_LOG_REQUESTS`,
   `QUERYAPIGATE_JSON_LOGS`, `QUERYAPIGATE_SLOW_QUERY_THRESHOLD` and `QUERYAPIGATE_ACCESS_LOG`.

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
| Any server with `QUERYAPIGATE_TRUST_PROXY` above 0 | Reading `X-Forwarded-For` as ProxyFix does comes later |
| Requests with `Origin` or `Authorization`, several `X-API-Key`s, `?version=`, `?connection_name=`, `?stream=` | Python's rules for each |
| Connections with a `${...}` reference, a credential source or TLS settings; a PostgreSQL server that requires TLS | Encrypted passwords (Fernet) are served; the rest come later |
| Types and values the renderer doesn't reproduce: PostgreSQL arrays, an interval with years or months, MySQL `BIT`, `hash` on `bytea` | Forwarded once the query has run, and that plan left to Python for five minutes |
| A key's first call each minute | So Python records its `last_used_at` |
| Any call whose query fails in the database | Python runs it again and answers as it does; a timeout is the exception, answered here with Python's 504 |

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
- **Plans are security decisions.** Only Python makes them, only for a request with the shared secret, and the data
  plane uses one only for a call whose rows digest as the plan's did. A plan doesn't widen anything: the
  key, its grants and the query are still read and checked on every call.
- **Filling in literals client-side is the injection boundary.** Python's drivers do it, so the data plane must too:
  quoting for PostgreSQL that holds whatever `standard_conforming_strings` is, and for MySQL whether or not the
  session's `sql_mode` has `NO_BACKSLASH_ESCAPES`, on a `utf8mb4` connection. The differential test includes hostile
  strings for exactly this.
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
- **Live events and `/metrics` include served runs.** They are reported to Python, which records and counts them
  (section 3, item 8). That closes the gap this ADR first listed here. The access-log and request counts in `/metrics`
  lag by up to one flush interval.
- **The data plane needs at least two Python workers behind it.** One process's history writer keeps up with about
  17,000 runs a second; the data plane serves more than that on the benchmark machine, and with one worker about half
  of its runs went unrecorded (counted as dropped). With two, every run was kept at 28,800 calls a second.
- **The run history and event log grow with the data plane's rate.** The event log keeps every run for at least an
  hour, whatever its count. At tens of thousands of runs a second that is tens of millions of rows an hour, more than
  SQLite or a PostgreSQL store should hold. A store built for that volume is ADR 0029's question.
- **A plan is learnt from a call that succeeds.** Python only sends one with a success, so a query that has only ever
  failed is always forwarded. A call the data plane serves and then can't render runs twice once: here, then in Python.
  After that its plan is left to Python for five minutes.
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
- Is a plan returned on Python's normal path enough, or should Python precompute plans when a query is published or
  a key is changed, so the first call is fast too?

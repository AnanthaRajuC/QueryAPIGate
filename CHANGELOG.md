# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## Versioning and compatibility

**Covered by the version number** - won't change without it being called out here as a breaking change:
REST API endpoints and response shapes, CLI commands and flags, environment variables, the on-disk storage
format (a newer version can always read a database an older version wrote), the API-key/role grant fields,
the saved-query definition format, the MCP tool contract, and error `code`s (an existing one is never renamed or
repurposed; new ones may be added). **Not covered** - can change in any release,
including a patch: the admin UI's internal markup/JS structure, exact error-message or log-line wording,
and anything in `queryapigate/` not re-exported from `queryapigate/__init__.py` (only `create_app` and
`__version__` are public Python API).

**Python versions** are supported until their upstream end of life (see python.org's release status). Dropping one
is a minor release, called out under **Breaking** - never a patch.

**Experimental** features are outside that promise: they may change or be removed in any minor release (a patch
release still never breaks them), and every such change is noted here. Each is marked where you meet it - a
callout in its documentation, `x-experimental: true` on its operations in `/openapi.json`, an "experimental" tag on
its rows in the Console's Settings, and a warning in the log at startup while one is in use. Today: H2, JDBC and Trino
connections, workspaces, tracing with OpenTelemetry (`OTEL_EXPORTER_OTLP_ENDPOINT`), secrets and credentials from AWS
(`${aws-sm:...}`, `aws-rds-iam`), semantic search (embed parameters), the Rust data plane in front of /q/
(`QUERYAPIGATE_DATAPLANE_SECRET`), and a ClickHouse run store for history, the event log and usage
(`QUERYAPIGATE_RUN_STORE_URL`). The list is `queryapigate/experimental.py`.

**Graduating.** A feature leaves that list - and comes under the promise - when it's tested for real in CI against
what it works with; has had a security review, if it guards access or data; hasn't changed shape for a release, with
no change planned; has its known gaps closed or written down as limits; is tested on the oldest dependency versions
it allows; and its documentation says what may still be added (fields, kinds, options - never removals, which go
through deprecation). The terms are `experimental.GRADUATION`; connection types have their own
(`databases.GRADUATION`).

**Still pre-1.0.** Strict SemVer allows any `0.y.z` release to break compatibility; this project doesn't
take that license casually. A patch release (`0.10.0` -> `0.10.1`) never breaks a covered surface. A minor
release (`0.10.x` -> `0.11.0`) is this project's pre-1.0 equivalent of a major bump and may - rarely, and
always called out under its own **Breaking** note in that release's entry below, never left to be
discovered. Once a 1.0 ships, that same rule simply moves to major versions, as SemVer intends.

**Deprecations.** Something covered is deprecated only once its successor has shipped, and is announced everywhere
you could meet it: a **Deprecated** note in that release's entry below, a callout where its documentation describes
it, a `Deprecation` header ([RFC 9745](https://www.rfc-editor.org/rfc/rfc9745)) and a `Link: <successor>;
rel="successor-version"` header on every response from a deprecated route, `deprecated: true` on its operation in
`/openapi.json`, and a warning in the log the first time it is used in a process. It keeps working, unchanged,
until it is removed: before 1.0, no sooner than the next minor release after the one that deprecated it; from 1.0,
not before the next major release. Removing it is always listed under **Breaking**. Two exceptions: experimental
features (above) need no deprecation period, and a security fix that can't be made compatibly may remove something
sooner, saying why. The list of what is deprecated now is `queryapigate/deprecations.py`.

## [Unreleased]

### Changed

- **The Docker image runs Python 3.13 from python-build-standalone, not the official `python:3.12-slim` image's 3.12**
  (ADR 0031). The official image compiles Python in a way that runs it 27-30% slower per CPU than other builds of it.
  At the gateway that's 6-9% more calls a second when Python serves them. Behind the data plane, one Python worker
  past its ceiling now records 60% more runs.
  - **Python is upstream's 3.13 release, now 3.13.18,** installed by `uv` in a build stage. `uv` is pinned by
    digest and checks the download's hash, and each `uv` release resolves 3.13 to one exact patch release. Its
    updates come from Dependabot, as the base images' do.
  - **The image is `debian:trixie-slim`, pinned by digest,** with the packages in a virtualenv, `/opt/venv`, first
    on `PATH`.
  - **Python's own `ssl` now uses the OpenSSL compiled into it** (3.5.9), as psycopg2 and cryptography already use
    their own.
  - **The image carries no `pip`.** It never ran it, and the old copies of urllib3, msgpack and setuptools that
    `pip` vendors were the image's only HIGH findings among its Python packages: Trivy now finds none there. **If you
    build an image `FROM` this one and `pip install` into it,** add `USER root` and
    `RUN python -m ensurepip --default-pip` first (and `USER app` after). Running the image, with Docker, Compose or
    the Helm chart, needs no change.
  - **The image is 10.8 MB larger.** The PyPI package is unchanged, and still supports Python 3.11-3.14.
- **CI:** the upgrade fixtures from 0.47.0 are added, and the rolling-upgrade and Helm checks now start from 0.47.0.
- **CI:** signing the image and the chart retries up to three times. Rekor once answered cosign's own retried upload
  with `409 createLogEntryConflict`, which failed 0.47.0's Docker publish until the job was rerun.
- **CI:** a release, or a `docker.yml` rehearsal, checks that the arm64 image's Python runs under QEMU. A test checks
  that every `COPY --from=` in the Dockerfile names one of its stages, so Dependabot sees each image the build uses.
- **CI:** the Docker image job checks that the image has no `pip`, and that `python -m ensurepip --default-pip`
  brings one back. A test checks the Dockerfile installs with `pip` and uninstalls it in one step.

### Fixed

- **A NUL character in a string parameter was a 500 on PostgreSQL.** PostgreSQL's text can't hold one and psycopg2
  refuses to send it, so the call failed as `query_failed`. It is now the caller's `400 param_invalid`, naming the
  parameter, on saved queries, ad-hoc SQL and MCP tools alike; the other databases take the value as before. Found by
  the data plane's differential test, which forwards such calls to Python.
- **An MCP tool that found no rows answered with REST's message, not an empty list.** A saved query's tool returned
  `{"message": "No results returned"}` as `structuredContent.rows`, against its own `outputSchema` (an array): the
  official MCP SDK's client raised an error for it, so an agent looking for something that isn't there got a failure
  instead of `[]`. A key with a row quota was also charged one row for each such call. Now `rows` is `[]`. REST's
  answer is unchanged.
- **`queryapigate init` failed when `QUERYAPIGATE_HOME` didn't exist yet** (`unable to open database file`): the
  store was opened before `init` made the folder. It makes it first now, as it always meant to.
- **`queryapigate --help` called `config` experimental,** and so did the Helm chart's row in DEPLOYMENT.md:
  configuration as code has been stable, under the compatibility promise, since 0.30.

## [0.47.0] - 2026-10-30

The Rust data plane ships in the image and the Helm chart - experimental, and off unless started - and two security
fixes. Upgrade now if you run `queryapigate mcp` on an address other than loopback without an API key: any client that
reached it was an unrestricted admin. The image's listening address now comes from `QUERYAPIGATE_BIND`
(`0.0.0.0:5000`, as before), not a `--bind` in its command.

### Added

- **The Rust data plane ships in the image and the Helm chart** (experimental -
  [ADR 0030](documentation/adr/0030-shipping-the-data-plane.md),
  [guide 64](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/how-to/64-try-the-rust-data-plane.md)). The image
  has `queryapigate-dataplane` beside Python, for amd64 and arm64, and runs it only when started. The chart's
  `dataplane.enabled` adds it to each API server pod as a second container from the same image, takes the pod's port
  with it, moves Python to the pod's loopback, and makes the secret the two share, kept across upgrades. With Docker,
  it's a second container sharing the first one's network; `deploy/scale-out/docker-compose.dataplane.yml` puts one in
  front of each instance. Nothing changes unless it's turned on. Measured: the image's data plane serves as fast as the
  from-source one, and a pod with it held to 2 CPUs and no run store recorded every one of 9.8 million runs at 16,400
  calls a second ([results](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/benchmarks/results/dataplane-image-2026-10-30/README.md)).
- **`QUERYAPIGATE_BIND`:** where the image's gunicorn listens - `0.0.0.0:5000`, as before, or `127.0.0.1:5001` behind
  the data plane. The image's command no longer has `--bind`; unset outside the image, gunicorn's own default applies.

### Changed

- **The data plane serves behind trusted proxies.** With `QUERYAPIGATE_TRUST_PROXY` set it used to forward every call,
  so it served nothing behind the chart's ingress or the scale-out example's Caddy. It now finds the client's address
  and scheme as Python's `ProxyFix` does, for the plainly written `X-Forwarded-For` and `X-Forwarded-Proto` a proxy
  sends, and forwards any other shape for Python to read. It sends HSTS (`QUERYAPIGATE_HSTS_MAX_AGE`) on a call that
  arrived over HTTPS, as Python does. A key's `allowed_ips` is judged by that address, so the differential test runs
  again behind two proxies with every shape of those headers, `X-Forwarded-Host` included: no difference in 9,000
  calls on PostgreSQL and MySQL, over three seeds.
  Behind Caddy, three data planes served 26 of 30 calls.
- **The data plane and Python pair only within a release.** The data plane sends its version with its secret, and
  Python gives no plan and no run store lease to a different one, logging why once: a mismatched pair forwards every
  call. The crate's version is now the release's (`dataplane/Cargo.toml`, checked by a test).
- **CI:** the upgrade fixtures from 0.46.0 are added, and the rolling-upgrade and Helm checks now start from 0.46.0.
  The image's smoke test, on every pull request and release, also runs it as two containers and checks that
  the data plane serves a call; the Helm chart's kind test turns the data plane on behind the ingress's trusted hop and
  checks an IP-restricted key is judged by the client the hop names; the data plane's crates are audited against
  RustSec (`cargo audit`), and its binary is built with `cargo auditable`, so the image's SBOM lists them.

### Fixed

- **The data plane served calls Python refuses for their host.** werkzeug answers 400 to a `Host` it can't encode as
  IDNA - an empty label, as in `a..b`, or one over 63 characters - and the data plane served them. It now forwards
  them, and behind trusted proxies checks the `X-Forwarded-Host` Python would take instead. Found by the differential
  test, which now sends such hosts.

### Security

- **The MCP server answered any client in open-access mode, as an unrestricted admin.** With no API key,
  administrator, JWT or MCP OAuth configured, REST answers local (loopback) clients only and refuses others with
  `403 open_access_remote`; `queryapigate mcp` never applied that rule. On any address but loopback - and the Docker
  image needs `--host 0.0.0.0` - any client that reached its port could list and run every saved query, run read-only
  SQL on every connection and use an agent's write tools, with no credential. It now refuses them as REST does, from
  the connection's peer, before a session starts. A server with a key, administrators, JWT or OAuth configured wasn't
  affected, nor one on its default loopback address. If you ran `queryapigate mcp` on another address without a key,
  set `QUERYAPIGATE_API_KEY`.
- **Behind the data plane, gunicorn took a client's forwarding headers as a trusted proxy's.** Every request reaches
  gunicorn over loopback then, which gunicorn trusts by default to set the scheme (`X-Forwarded-Proto`, `-Ssl`,
  `-Protocol`) and `SCRIPT_NAME` from headers - and the data plane passed a client's own on. A client could move part
  of a path out of what Flask routes, past a proxy's rules for that path (the route's own authorization still applied),
  turn a call into a 400 with contradictory scheme headers, or make Python send HSTS over plain HTTP. With
  `QUERYAPIGATE_DATAPLANE_SECRET` set, `gunicorn_conf.py` now trusts no peer's (unless `FORWARDED_ALLOW_IPS` says
  otherwise), and Python reads forwarding headers only through `ProxyFix`, as it does without the data plane. Only a
  deployment running the experimental data plane, built from the repository, was affected.

## [0.46.0] - 2026-10-29

The experimental ClickHouse run store's security review, and a replay twice as fast at 250 million runs. Only a
deployment with `QUERYAPIGATE_RUN_STORE_URL` set is affected by the review's fixes; its tables move to version 2 at
the first start, under a lock, with the grants guide 65 gives. Upgrade if you run the run store from 0.45.0. The
Console's DOMPurify is updated too.

### Security

The experimental ClickHouse run store's security review (ADR 0029, one of its conditions to graduate). Nothing here
affects a deployment without `QUERYAPIGATE_RUN_STORE_URL`. Its tables move to version 2 at the first start, with the
grants guide 65 gave.

- **A run store row could split an event stream.** `queryapigate events` wrote an event's stored text into its frame
  as it was, so a row with a line break - written by anyone with an insert credential for the run store, such as the
  data plane's - became frames of the writer's choosing: other events, in any workspace, and an `id:` that moved
  clients' resume points past every event to come. Frames are one line now whatever a row holds, and `events`
  refuses ids above 2^53.
- **A data plane given another run store wrote its runs there**, out of history and out of an erasure's reach: unlike
  Python's instances, it never checked where the metadata store keeps runs. Python's lease now names it, and a data
  plane configured with another writes none of its runs, and logs why, until they agree.
- **Ad-hoc statements were kept past the retention settings:** 400 days in `run_hours` and 7 in `run_minutes`,
  whatever `QUERYAPIGATE_HISTORY_RETENTION_DAYS` said, though a statement may hold values. `run_hours` keeps their
  SHA-256 now, the hash `QUERYAPIGATE_HISTORY_ADHOC_SQL=hash` records - existing rows are rewritten, their counts
  kept - and `run_minutes` keeps rows only as long as the event log (`QUERYAPIGATE_EVENTS_RETENTION_HOURS`), all a
  usage report reads.
- **`forget` could miss runs still on their way**, in a data plane's batch or another worker's queue: it deletes
  again once they'd have landed, a flush interval and the settle window later.
- `queryapigate runs leave-clickhouse` continues the metadata store's event ids from the newest one up to the clock,
  not from any id a writer put in the run store; a history cursor that this server didn't return is a 400
  `invalid_filter`, not a 500; the data plane gives its id lease back as it stops; guide 65 no longer grants
  `TRUNCATE`, which nothing uses.

Beside the review, dependencies with published advisories, and one code-scanning alert:

- **The Console's DOMPurify is 3.4.17**, for GHSA-cg2g-xh64-v4q7 (critical), GHSA-mrv2-3pqr-j5jr and
  GHSA-c6gr-qvv4-5v3f. The Console sanitises the Help page's Markdown with DOMPurify's defaults, and those three need
  options it doesn't set (`SAFE_FOR_TEMPLATES`, `IN_PLACE` with hooks, `PARSER_MEDIA_TYPE`), so it wasn't open to them;
  it ships, so it's updated all the same. undici 7.30.1 and zod 4.6.7, for nine more advisories, are only in the
  Console's tests and lint. The data plane's `block-buffer` 0.10.4 (GHSA-qwgh-2vcv-g2f7, a panic that can corrupt a
  hash's buffer) waits on `fernet` and `sqlx-core`, which pin it.
- The run store lease's 503 says why in fixed words, not an exception's text.

### Changed

- **A run store replay is about twice as fast at 250 million runs:** a Last-Event-ID resume finds up to 100,000 of
  its event ids in one read, not 1,000 a read, and a key's creation date bounds the ids it reads. One key's replay of
  the whole log went from 12.0 s to 6.2 s, inside ADR 0029's 10 s, and a busy replay is no slower. The run store's
  storage and reads were measured at 250 million runs: 13.8 bytes a run, every read within its target
  ([results](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/benchmarks/results/runstore-250m-2026-10-29/README.md)).
- **CI:** the upgrade fixtures from 0.45.0 are added, and the rolling-upgrade and Helm checks now start from 0.45.0.

## [0.45.0] - 2026-10-28

An optional ClickHouse run store, as a prototype that ended in a measured decision: go
([ADR 0029](documentation/adr/0029-clickhouse-run-store.md)), the second condition of ADR 0028's. With it, the Rust
data plane writes every run it serves to ClickHouse itself, and run history, the event log and usage rollups are
kept there instead of the metadata store. At 20,000 calls a second for 30 minutes, and at the 64-caller peak where
0.44's store kept about half, every run was recorded, in about 13 bytes each and with 11% fewer gateway cores. It is
experimental and off unless `QUERYAPIGATE_RUN_STORE_URL` is set; nothing changes without it.

### Added

- **An optional ClickHouse run store** (experimental, a prototype, measured and decided go -
  [ADR 0029](documentation/adr/0029-clickhouse-run-store.md),
  [guide 65](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/how-to/65-keep-runs-in-clickhouse.md)). With
  `QUERYAPIGATE_RUN_STORE_URL` set, run history, the event log and per-minute and per-hour usage rollups are kept in
  ClickHouse 24.8 or later instead of the metadata store, for a data plane's rates. Each batch of runs is one insert
  into `events`; materialized views fill `history` and the rollups, and each table's TTL keeps its retention.
  - **The Rust data plane writes the runs it serves itself**, with an insert-only user if given one
    (`QUERYAPIGATE_DATAPLANE_RUN_STORE_URL`); Python only counts them for `/metrics`.
  - **Event ids stay increasing and below 2^53**, now minted from the time and a block each writer leases in the
    metadata store (schema 18, additive). `queryapigate events` delivers an event once it's a second old, so a resume
    misses nothing; `queryapigate_events_late_total` counts any acknowledged later.
  - **Usage reports are exact** for windows within the event log's retention, whatever the sample rate.
  - **History is kept by time** there (`QUERYAPIGATE_HISTORY_RETENTION_DAYS`, 30 days unless set).
  - `queryapigate runs copy-to-clickhouse` brings existing history across; `queryapigate runs leave-clickhouse` goes
    back, with event ids still increasing. Every instance must name the same run store, or it refuses to start.
  - CI runs the whole suite against ClickHouse 24.8 and 26.8, and the data plane's tests write to it.
  - [Measured](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/benchmarks/results/runstore-2026-10-28/README.md) against 0.44's store, the data plane in front of
    both: every run recorded at 20,000 a second for 30 minutes and at the 64-caller peak, where 0.44's kept about
    half; 11% fewer gateway cores; about 13 bytes a run; history and usage reads in 6-17 ms at 107.6 million runs.

### Changed

- **CI tests MongoDB 7.0 for now, not 8.0.** MongoDB 8.0 and later refuse to start on Linux kernels 6.19 to 7.0.13
  ([SERVER-121912](https://jira.mongodb.org/browse/SERVER-121912)), which GitHub's ubuntu-24.04 runners have run since
  image 20261025. The integration and oldest-dependencies jobs go back to 8.0 once the runners are on 7.0.14 or later.
  The upgrade fixtures from 0.44.0 are added, and the rolling-upgrade and Helm checks now start from 0.44.0.

## [0.44.0] - 2026-10-28

A Rust data plane for `/q/`, as a prototype that ended in a measured decision: go
([ADR 0028](documentation/adr/0028-rust-data-plane-prototype.md)). In front of the Python server, it serves
saved-query reads itself from the plans Python computes, and forwards everything else. Against the Python build
behind it, it serves 3.2-3.5x the calls a second per gateway core without the metadata cache and 4.0-9.4x with it,
about 8x with each side at its best on the same 8 cores, with p99 2-20x lower and every run recorded
([Performance, v5](documentation/PERFORMANCE_V5.md)). It is built from the repository, not shipped in the image or
the chart.

### Added
- **The Rust data plane** (experimental), in `dataplane/`: the plans it applies (`queryapigate/dataplane.py`), the
  `QUERYAPIGATE_DATAPLANE_SECRET` setting both processes share (at least 16 characters, checked at startup), and the
  internal endpoint it reports served runs to, which Python records in run history, the event log and `/metrics`.
  [Try the Rust data plane](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/how-to/64-try-the-rust-data-plane.md) builds and runs it; the threat model has
  [its entry](documentation/THREAT_MODEL.md#the-rust-data-plane-adr-0028-a-prototype).
- **The differential test** (`dataplane/tests/test_differential.py`): random calls, including hostile parameters and
  keys revoked and re-granted mid-run, sent to Python directly and through the data plane, compared response for
  response and history row for history row. CI runs it against real PostgreSQL and MySQL, with the data plane's own
  checks, in a new `dataplane` job; Dependabot updates its Rust dependencies.
- **Benchmarks of the data plane:** blocks d1-d4 in `benchmarks/oltp.py` and `benchmarks/tpcc_mysql.py`, each cell
  recording the share of its runs kept, with results in `benchmarks/results/dataplane-*-2026-10-27/`.

### Changed
- **Run history writes only the runs a version keeps.** A batch with more of a version's runs than
  `QUERYAPIGATE_HISTORY_LIMIT` inserts only the newest, instead of inserting all and deleting most. The rows kept are
  the same. On a PostgreSQL store, a batch's events go as one multi-row INSERT. Together, a batch of 500 runs of one
  query took 10 ms to write instead of 33 in a profile.

## [0.43.0] - 2026-10-27

Security and performance. Six fixes to the governance - column masking, table allow-lists and MySQL's read-only
connections - and fewer round trips per query: PostgreSQL 4 to 1, MySQL 3 to 2. With the database 0.5 ms away, a
PostgreSQL call takes 4.39 ms instead of 6.58 ms, and a MySQL worker with 32 callers serves 52% more calls
([Performance, v4](documentation/PERFORMANCE_V4.md), [on MySQL, v2](documentation/PERFORMANCE_MYSQL_V2.md)). Two
breaking changes, both for keys with `allowed_tables`: grants are schema-qualified, and statements whose tables
can't be checked are refused. Read **Breaking** before upgrading a deployment that uses table allow-lists.

### Breaking
- **`allowed_tables` grants are schema-qualified on every SQL connection** (they already were on Trino). A bare grant
  such as `customer` now means the connection's own `customer` only: on MySQL and ClickHouse the connection's
  database, on PostgreSQL the `public` schema (or the connection's new `options.schema`), on SQLite and DuckDB `main`.
  A key that reads a table in another schema or database needs that grant written out, `billing.customer`. A key that
  queries a table through a three-part name (`shop.public.orders`) needs the grant written the same way. See the
  Security entry below.
- **A table-restricted key can no longer run statements whose tables can't be checked:** `SHOW`, `SET`, `USE`,
  `ATTACH`, `PRAGMA`, `CALL`, `PREPARE`/`EXECUTE`, `DECLARE`/`FETCH`, `LOAD`/`INSTALL`, `VACUUM`, and anything else
  the SQL parser keeps only as text. They are refused with `403 table_check_failed`. `EXPLAIN` still works, checked
  against the tables of the statement it explains.

### Security
- **Column masking could put a mask on the wrong column, and return the masked one in clear.** Masks are applied by
  position. Every sqlglot that 0.42 and earlier allowed (and sqlglot 30 until late in that series) expands `SELECT *`
  with the tables first, and a subquery, `VALUES` or `LATERAL` after them, whatever order the query names them in.
  So in `SELECT * FROM (SELECT ...) s JOIN customers c ON ...`, the database returned `s`'s columns first while the
  trace had put `customers`' there, and a rule on `customers.email` masked another column. Only the number of
  columns was checked, and that matched. Now a `SELECT *` over several sources, one of which isn't a table, is
  refused (`mask_unresolvable`) on every sqlglot version; `s.*, c.*` or named columns trace in the order written. A
  new test runs generated queries against a real engine (DuckDB) and checks that no masked value comes back in
  clear. Found by a differential fuzz of sqlglot 25 against 30. Affects every release with masking (0.17 to 0.42).
- **A CTE could hide a real table from `allowed_tables` by differing from it only in quoting.** CTE names were
  matched case-insensitively, but in PostgreSQL `"Customer"` (an ORM's quoted table name) is a different name from
  the CTE `customer`. So a key allowed only `orders` could read that table with `WITH customer AS (SELECT 1) SELECT *
  FROM "Customer"`, confirmed against PostgreSQL 16. Now a reference matches a CTE only when named the same way:
  unquoted and equal ignoring case, or quoted and exactly equal. Found by the same fuzz. Affects every release with
  `allowed_tables`.
- **Workspaces (experimental): masking could trace one workspace's query against another's schema.** Connection
  names are unique per workspace, but masking's cached copy of a connection's schema was keyed by name alone. So
  within a minute, a query on workspace B's `crm` could be traced against workspace A's `crm` tables. A column only
  reached through a view in B, which masking refuses to follow, could then be traced as a plain table column in A's
  schema. Masking rules written for B's base table wouldn't apply, and the column came back unmasked. The cache is
  now keyed by workspace and store as well. Affects 0.38 to 0.42, with workspaces in use.
- **Table allow-lists (`allowed_tables`) matched table names, not schemas or databases.** A key allowed `customer`
  could read every table called `customer` that the connection's database user could see: `billing.customer` and
  `hr.customer` as well as its own. Affected MySQL, MariaDB, PostgreSQL, ClickHouse, DuckDB and SQLite connections
  whose user can see more than one schema or database; Trino matched qualified names already (ADR 0026). Now names
  are matched qualified everywhere, a grant's and a query's missing parts filled from the connection's defaults. The
  schema browser and MCP `list_tables` apply the same grants. On PostgreSQL, a table-restricted call also pins
  `search_path` to that schema, so that neither the role's own `search_path` nor a `"$user"` schema can send an
  unqualified name somewhere other than the table the grant was checked against. Affects every release to 0.42.
- **A table-restricted key could reach other tables through statements that name no table.** A read-only key could
  read a forbidden table's columns and DDL, though not its rows: `SHOW CREATE TABLE` and `SHOW COLUMNS` on MySQL
  and ClickHouse, `EXPLAIN` on PostgreSQL and SQLite, DuckDB's `SHOW`. A key that may also write could read its rows
  through `PREPARE ... AS SELECT` then `EXECUTE`, a cursor (`DECLARE`, then `FETCH`), or `CALL`. It could also
  redirect a pooled connection's later unqualified names with `SET search_path` or `ATTACH` on DuckDB. These are now
  refused for a table-restricted key (see Breaking). Affects every release to 0.42.
- **MySQL: a query could make a pooled read-only connection writable, or lift its time limit, for the calls after it.**
  A read-only connection was made read-only once per session, and its time limit was set on the session only when
  it changed. A stored function that sets the session's transaction mode (`SET SESSION TRANSACTION READ WRITE`) or
  its `max_execution_time`, called as `SELECT f()` - which the SQL guard passes as a read - changed them for every
  later call on that pooled connection: a later write through it was no longer refused by MySQL, and a query given
  0.5 s ran for over ten minutes. It needs such a function in the database and `EXECUTE` on it for the connection's
  user; the SQL guard still refused writes from read-only keys, so it disabled MySQL's own backstop rather than
  allowing writes outright. Now a read-only connection is read-only per transaction (`START TRANSACTION READ ONLY`,
  ended on release with `ROLLBACK AND CHAIN`, which starts the next one read-only), and a read's time limit travels
  with the statement (a `MAX_EXECUTION_TIME` hint; `SET STATEMENT ... FOR` on MariaDB), so nothing a query sets
  outlives it. [ADR 0027](documentation/adr/0027-fewer-round-trips-per-query.md). Affects every release to 0.42.

### Added
- **Benchmarks of 0.43 against 0.42:** [Performance, v4](documentation/PERFORMANCE_V4.md) on PostgreSQL and
  [Performance on MySQL, v2](documentation/PERFORMANCE_MYSQL_V2.md), with the results in `benchmarks/results/`. v2
  also answers v1's open questions: threads across a network, scale-out on a PostgreSQL store, what a hyperthread is
  worth, what logging costs (both request logs off: +21%), the store's round trips, and the laptop's ceiling.
  `benchmarks/tpcc_mysql.py` gains session `C` for them.
- **`QUERYAPIGATE_THREADS` and `QUERYAPIGATE_ACCESS_LOG` tune the Docker image without replacing its command.** The
  image's gunicorn took its threads (`--threads 8`) and its access log (`--access-logfile -`) from flags in its
  command, which beat any other setting, so tuning either meant overriding the whole command. Both now come from
  `queryapigate/gunicorn_conf.py`. `QUERYAPIGATE_THREADS` (default 8, 1 to 256) sets the threads, and
  `QUERYAPIGATE_ACCESS_LOG=off` drops the access log - 7% of throughput in
  [Performance, v3](documentation/PERFORMANCE_V3.md#the-access-log), for a deployment whose proxy logs requests
  already. Unset, the image behaves exactly as before. A `--threads` flag or `GUNICORN_CMD_ARGS` still overrides
  them. Both show on the Console's Settings screen.
- **A how-to, [Tune a deployment for throughput](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/how-to/63-tune-a-deployment-for-throughput.md):** threads by query
  time and database distance (the default eight were up to 28% slower than the best setting on fast lookups), CPU
  per container (a worker uses 1.1-1.3 cores), the metadata cache, logging, the event log's disk, and how many
  containers to start from, each from a measurement. The Helm chart and the Kubernetes manifests now say what CPU to
  request for throughput (1 CPU, limit 1.5); their defaults are unchanged.
- **`QUERYAPIGATE_LOG_REQUESTS=off` leaves out the per-request log lines** - the `METHOD /path -> status` line and the
  `Executing on ...` / `Finding on ...` / `Streaming from ...` line for each query. Errors, warnings, slow queries and
  agent writes are still logged, and the run history keeps each run's `request_id`. On by default, as before. For a
  server whose proxy already logs requests: logging was a fifth of a worker's CPU in
  [Performance, v3](documentation/PERFORMANCE_V3.md#the-cpu), much of it threads queueing on the one handler.

### Changed
- **The connection pool and the `/events` stream cap follow the thread count.** `QUERYAPIGATE_POOL_SIZE` defaults to
  `QUERYAPIGATE_THREADS` (8, as before), so a server given more threads doesn't open a fresh connection per call for
  the ones beyond the pool. `QUERYAPIGATE_EVENTS_MAX_STREAMS` defaults to 4 as before, but never to more than half the
  threads, so a few long-lived streams can't hold every thread of a server tuned down. Either set explicitly is used
  as given.
- **sqlglot 26 to 30 are supported** (`queryapigate[flow]` now allows `sqlglot>=25.2,<31`; Dependabot #77). sqlglot
  30 renamed the parsed `with` and `from` arguments to `with_` and `from_`. Read under the old names, a statement's
  CTEs went missing from the table check (it refused CTE queries), and an `UPDATE ... FROM` went unrecognised by
  masking's check for a change that joins other tables in (it traced what should have been refused). Both names are
  read now. sqlglot 30 parses `RANK()` as its own type, which masking now accepts as a constant ranking, as before.
  It is also faster: a statement's first table check 0.39 to 0.27 ms, its first masking trace 3.66 to 2.86 ms.
  Tested on 25.2, 26, 27, 28, 29, 30.0 and 30.24.
- **No more masked call that waits once a minute.** Masking traces a statement against the connection's schema, which
  it keeps for 60 seconds. When that copy expired, the next call read the schema again and traced its statement
  again: about 75 ms, once a minute, on every governed key
  ([Performance, v3](documentation/PERFORMANCE_V3.md#the-cold-path-every-60-seconds)). Now the first call after 45
  seconds has the schema read again in the background, and is served from the cache meanwhile. When the schema hasn't
  changed, its traces are kept. A schema is still never used for longer than 60 seconds, so a connection with no
  masked call for a minute is read in its next call, as before.
- **The one-command Compose demo turns on the metadata cache** (`QUERYAPIGATE_METADATA_CACHE_TTL: "5"` in
  `docker-compose.yml`): one instance, so only a change made with the CLI inside the container waits, up to 5 s. The
  server's own default stays off, as do the scale-out Compose setup and the Helm chart, whose `values.yaml` now shows
  how to set it. See [Performance](documentation/PERFORMANCE.md#the-metadata-cache).
- **Fewer round trips per query** ([ADR 0027](documentation/adr/0027-fewer-round-trips-per-query.md)). Across a
  network every round trip costs a call in full, so:
  - **PostgreSQL: 4 round trips down to 1.** Connections run in autocommit, and each query goes as one message
    (`RESET ALL; ...; SET TRANSACTION READ ONLY; SET LOCAL statement_timeout = ...; <query>`) that PostgreSQL runs
    as one transaction, ending with the message - no `BEGIN`, and no `ROLLBACK` on release. A write commits with its
    message. Streaming exports and agent writes still use an explicit transaction, which they need.
  - **On PostgreSQL, each message starts with `RESET ALL`,** which puts back any session setting a query left behind.
    In autocommit, `SELECT set_config('TimeZone', 'Asia/Tokyo', false)` (or `search_path`, or `DateStyle`) is
    committed with its query, and would apply to the next call on that pooled connection, from any key. Rolling back
    each call's transaction undid it before. The reset added no measurable time.
  - **MySQL: 3 round trips down to 2, and no more stalls.** A pooled connection keeps one cursor instead of making one
    per query: mysql-connector pings the server on every `cursor()`, and its C extension holds the GIL during the
    ping, so across a network every call froze the whole worker for a round trip - the 22% drop at 32 callers in
    [Performance on MySQL](documentation/PERFORMANCE_MYSQL_V1.md#a-database-across-a-network).
  - PostgreSQL's statement log now shows one combined message per call.
  - Measured through whole requests, with the database 0.5 ms away
    ([Performance, v4](documentation/PERFORMANCE_V4.md), [on MySQL, v2](documentation/PERFORMANCE_MYSQL_V2.md)): a
    PostgreSQL call 6.58 ms to 4.39 ms, and 1.60 ms with the metadata cache; a MySQL call 3.71 ms to 2.89 ms, and one
    worker with 32 callers 446 to 680 calls a second (+52%). With the database on the same host, 0.43 is 6-10% faster
    on PostgreSQL and 15-19% on MySQL.
- **Reading the store's location no longer touches the filesystem on every call.** With an absolute
  `QUERYAPIGATE_HOME` (the Docker image's `/data`), where the store lives is worked out once - 3.6-3.8% of a worker's
  CPU in the MySQL benchmark.
- **The check that a connection can't open QueryAPIGate's own store now skips server databases.** PostgreSQL, MySQL,
  ClickHouse, MongoDB and Trino open no file, so resolving their `database` as a path on disk (several system calls)
  on every call protected nothing, and cost 7-10% of a worker's CPU. SQLite, DuckDB, H2, JDBC - and any type it
  doesn't know - are checked as before.
- **A masked call no longer reads the masking hash key from the store every time.** The key a store makes for
  `hash` never changes, so each process reads it once and keeps it until it next changes the store. With
  `QUERYAPIGATE_MASK_KEY` set, nothing is read, as before.

### Fixed
- **Formatting a query no longer rewrites its bound parameters.** The Console's Format turned `:status` into
  `$status` on DuckDB and `{status: }` on ClickHouse (and, with sqlglot 30, `%(status)s` on PostgreSQL), none of
  which QueryAPIGate binds, so formatted SQL saved back broke its parameters. A bound parameter is now printed as
  written.

## [0.42.0] - 2026-10-24

Performance. A key with masking and a table allow-list is now about as fast as one without, and every call reads
QueryAPIGate's own store less: 3 statements instead of 7. The numbers come from a pinned, interleaved benchmark on
PostgreSQL ([Performance, v3](documentation/PERFORMANCE_V3.md)), whose harness ships in `benchmarks/oltp.py`. One
behaviour change: the connection pool now keeps up to 8 idle connections, not 5.

### Added
- **`QUERYAPIGATE_METADATA_CACHE_TTL`: reuse the key, saved query and connection a call reads, for that many
  seconds.** Off by default. Every call reads three rows from the store before it runs; with this set, a server keeps
  them in memory instead. It caches the rows, never a decision: whether a key is active, expired, used from an
  allowed address or inside a rotation's grace period is still checked on every call. A change made through the same
  server clears its cache at once; one made through another instance, another worker or the CLI is seen up to one
  TTL later - so a key revoked there keeps working here for that long. Measured on PostgreSQL with
  `QUERYAPIGATE_METADATA_CACHE_TTL=5` (one worker, 32 callers): 16-19% more requests a second (743 to 875 lookups),
  a single call 1.38 ms to 1.05 ms, 41% off a single call across a 0.5 ms network round trip, and 23% more with
  eight workers (2,215 to 2,727 lookups a second). See [Performance](documentation/PERFORMANCE.md#the-metadata-cache).
- **An OLTP benchmark on PostgreSQL, and its harness.** `documentation/PERFORMANCE_V3.md` measures v0.41.0 against
  this release on pgbench's 10-million-row schema: throughput, latency against offered load, scale-out, threads, a
  simulated network, the round trips per call, and a profile of where the time goes - and turns that into a sizing
  rule. `benchmarks/oltp.py` reruns it on your own machine, with each part pinned to its own cores and every
  comparison interleaved; `benchmarks/oltp_report.py` makes the tables.
- **How-to guides for Trino (61) and for using the Console (62).**

### Changed
- **`QUERYAPIGATE_POOL_SIZE` defaults to 8, up from 5** - one idle connection per thread of the Docker image. With 5,
  a server busy on all eight threads closed three connections as each batch of calls finished, then opened new ones
  for the next. A database now sees up to 8 idle connections per connection and process, not 5; set
  `QUERYAPIGATE_POOL_SIZE=5` to keep the old number.
- **A saved query's run reads the store less.** It read the query three times - a check that it exists, its header,
  its versions - and now reads it once. History and event rows are written in one statement per batch, not one per
  row. On PostgreSQL, a call now sends 3 statements to the store instead of 7 (counted with `log_statement`), and one
  worker serves 7-9% more (682 to 743 lookups a second, 32 callers).
- **Masked and table-restricted calls are several times faster.** Column masking traced each statement's result
  columns to their sources (sqlglot lineage) on every call, and `allowed_tables` parsed the statement on every call -
  milliseconds of work for SQL that, for a saved query, never changes. Both are now worked out once and reused: a
  statement's trace is kept with the connection's schema it was traced against (and dropped with it, when the schema
  is read again or the connection changes), and the tables a statement reads are kept per statement, dialect and
  connection defaults. Measured on PostgreSQL: a key with masking and `allowed_tables` went from 171 to 693 lookups a
  second (32 callers), and a single call from 6.0 ms to 1.5 ms - within 7% of a key without them. What is masked,
  and what is refused, is unchanged; a statement that can't be traced or checked is refused every time, as before.
  Both caches are bounded, so varied ad-hoc SQL can't grow them.
- **The README, the landing page and the feature list present the MCP server as a pillar** beside the REST API -
  one governed core, two surfaces - and the Console screenshots are retaken, with a new one of the MCP screen.

## [0.41.0] - 2026-10-20

Trino joins the native connection types (experimental): one connection federates every catalog behind a Trino
endpoint, fully governed through sqlglot's `trino` dialect. The Console's access screens gain a per-key details
view and GitHub-style 30-day activity sparklines, so you can see at a glance what each key, connection, cached
query and MCP tool is actually used for.

### Added
- **Trino as a native connection type (experimental).** A `trino` connection turns the Trino distributed SQL engine
  - which federates many catalogs (Hive, Iceberg, Delta, Kafka, object stores, other databases) behind one endpoint
  - into a governed source, instead of the minimal generic-JDBC path. The connection's `database` field is a Trino
  catalog; the schema browser, column masking, the tables-and-joins diagram, `allowed_tables`, and a real query time
  limit (cancellation on the cluster) all work, through sqlglot's `trino` dialect. `allowed_tables` is matched
  **fully qualified** (`catalog.schema.table`), so a grant never reaches the same table name in another catalog or
  schema. Install with `pip install "queryapigate[trino]"`. **Experimental** (ADR 0026): a read-only, scoped Trino
  user is still recommended as the outer boundary; the federation boundary is security-reviewed
  (`documentation/THREAT_MODEL.md`), and a release of stable shape and real-deployment use remains before it
  graduates. Read-only rests on the SQL guard; agent writes are not supported on Trino yet. See
  `documentation/DATABASE_CONNECTION_CONFIGURATION.md` → Trino connections.
- **Console: an API key details screen, and 30-day activity sparklines across the access screens.** Each API key
  now has a **Details** view (from the API keys table) showing the saved queries (APIs) it can reach, the
  connections and tables, its limits and masking, and its full run history - clicking a query opens its SQL. A
  30-day run-activity sparkline is shown per key (API keys), per reachable API scoped to that key (a key's
  details), per connection (Connections), per cached query (Caching), per tool scoped to its MCP calls (MCP
  server), and per role as its keys' combined activity (Roles). Console only - it reads existing run history,
  no API change.

## [0.40.0] - 2026-10-18

MCP for AI agents: a dedicated MCP screen in the Console, per-query control over what's exposed, and tools agents
can actually use - self-describing parameters, table inspection, capped-result guidance, and semantic search
(experimental). Plus a workspace-boundary security fix for signed-in users.

### Added
- **Stronger MCP tools for agents (four changes).**
  - **Self-describing parameters.** A query parameter can now carry an `example`, and both `description` and
    `example` now flow into the MCP tool's input schema (and `/openapi.json`) - so an agent sees what each argument
    means and a sample value, not just its type.
  - **A `describe_table` MCP tool.** One table's columns, types and primary/foreign keys on a connection the key
    can use - masking-aware, so a masked or dropped column is flagged, never shown as readable. A table the key
    can't reach answers as a missing one.
  - **Clearer capped results.** When a tools/call result hits `QUERYAPIGATE_MCP_MAX_ROWS`, it now carries an
    agent-facing `note` ("…more rows match; add a filter or ask for a summary") as a field and a second text
    block, so a model narrows or aggregates instead of assuming it saw everything. The first content block stays
    pure JSON.
  - **Semantic search (experimental).** A text parameter marked `"embed": true` lets a caller (or an agent) search
    by meaning: the server turns the text into a vector via a configured OpenAI-compatible embeddings endpoint
    (`QUERYAPIGATE_EMBEDDING_URL`/`_MODEL`/`_API_KEY`, Ollama-compatible) and binds it for a `pgvector` search -
    still masked, row-filtered and audited. Postgres/`pgvector` only, off unless an endpoint is set, and refused
    (`embedding_unavailable`) rather than run unfiltered when it isn't. See ADR 0025 and API.md "Semantic search".
- **Hide a saved query from MCP, while it keeps serving over REST.** A query version now carries an `mcp` flag
  (default `true`). When `false`, the query is not listed as an MCP tool and a call to it over MCP is refused with
  the new `mcp_disabled` code (`404`); it still answers over REST as before. Toggle it in place from the API
  Repository's new **MCP** tab (no new version needed), set it when saving a version (`"mcp": false` in the query
  definition), or change it with `PATCH /api/v1/queries/{name}/versions/{version}` alongside `cache_ttl`. The MCP
  tab also summarises how the query appears as a tool (read-only / write-with-approval / insert, and its row ceiling).
- **Query-level column masking.** A saved query can now carry its own `mask` rules (same shape as a key's grant
  mask, ADR 0005), set in the API Designer's Settings pane for a preview and on the save form for the query itself.
  The query's mask applies to *every* caller, admins included, and is unioned with any mask on the caller's key -
  per column, the stricter action wins (`drop` > `null` > `mask` > `hash`). Masking traces SQL columns, so it is
  not available on Mongo queries. A query whose mask can't be traced is refused, never served unmasked.
- **Console: the API Repository's History tab can filter to MCP-only runs**, and the left sidebar's section
  headings (Data, API, MCP, …) are now collapsible, remembering what you collapsed.
- **A dedicated MCP screen in the Console.** The MCP server's tools, a reachability check and the connect snippet
  (the URL and `X-API-Key` header, as Claude Desktop takes them) now have their own top-level **MCP** item in the
  sidebar, instead of living under Settings. The tools shown are the current workspace's. The MCP *settings*
  (`QUERYAPIGATE_MCP_PORT`, `QUERYAPIGATE_MCP_MAX_ROWS`) stay read-only under Settings → MCP server. No API change -
  the page uses the existing `GET /api/v1/mcp/tools` and `/api/v1/mcp/status` (both need `observe`).

### Fixed
- **Console: the API Designer shows a text cursor in the SQL editor**, and after a run the status line
  (`200 OK · 13 ms · …`) is shown once, not twice.

### Changed
- **Workspaces (experimental): a signed-in user's workspace now comes from their token, not the request path.** A
  security review of the workspace boundary found that a federated (JWT) user's role was resolved in whatever
  workspace the `/w/<workspace>/` prefix named, so a user signed in for one team could reach another team's data by
  changing the prefix where both workspaces had a role of the same name. A signed-in user is now confined to one
  workspace: the new `QUERYAPIGATE_JWT_WORKSPACE_CLAIM` claim names it, or the default workspace when that is unset,
  and a path naming a different workspace is a `404`, exactly as for an API key. If you relied on signed-in users
  addressing several workspaces by path (experimental, 0.38 only), set `QUERYAPIGATE_JWT_WORKSPACE_CLAIM` to a claim
  your tokens carry. The threat model's *Workspaces* section now also records the shared-JVM risk a workspace owner's
  H2/JDBC connection carries, and that there are no per-workspace resource limits yet.

## [0.39.0] - 2026-10-17

QueryAPIGate is now open source, under Apache-2.0. The code is unchanged.

### Changed
- **License: from the Functional Source License, Version 1.1, MIT Future License (`FSL-1.1-MIT`) to the Apache License,
  Version 2.0 (`Apache-2.0`).** QueryAPIGate is now open source: use, modify and redistribute it for any purpose,
  commercial or not, including offering it as a service. A `NOTICE` file carries the copyright notice. Versions 0.7.0
  through 0.38.0 were released under FSL-1.1-MIT and versions up to 0.6.1 under MIT; a copy already obtained keeps the
  license it came with. The code is unchanged; only the license and its metadata are (`LICENSE`, `NOTICE`, the
  package's `License-Expression`, the container image label, the README).

## [0.38.0] - 2026-10-16

Workspaces (experimental): several teams on one server, each running its own connections, saved queries, keys,
roles and exports - administering nothing of another's - while the platform team sees all of it. And a sign-in page
for the Console.

### Breaking
- **Not a rolling upgrade.** The store's primary keys change (schema 17, every name unique per workspace), so a 0.37
  instance can't write to a store 0.38 has upgraded. Stop every instance before the first 0.38 one starts - the one
  exception before 1.x's rule that store changes are additive ([Rolling
  upgrades](documentation/DEPLOYMENT.md#rolling-upgrades-several-instances-on-one-store)). A single instance upgrades
  as always.

### Upgrading from 0.37
- **Stop every instance before starting 0.38** - this upgrade isn't a rolling one. The store moves to schema 17
  (workspaces, [ADR 0023](documentation/adr/0023-workspaces.md)): everything it holds goes into the workspace
  `default`, and each name becomes unique per workspace, which changes the tables' primary keys. A 0.37 instance can't
  write to a store that has been upgraded, so don't run the two side by side. Back up the store, stop every instance
  (in Kubernetes, scale the Deployment to 0), and start one 0.38 instance - it upgrades the store - then the rest.
  Nothing changes for a server with one workspace, which is every server until a second is created.

### Added
- **Workspaces** (experimental - [ADR 0023](documentation/adr/0023-workspaces.md), [guide 60](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/how-to/60-give-each-team-its-own-workspace.md)):
  delegated administration for teams on one server. A workspace owns connections, saved queries, API keys, roles,
  destinations and exports - each name unique within it - and its own run history, audit entries, alerts, events,
  usage quotas and cached responses. Everything a server had is in `default`, and a server with only `default`
  behaves exactly as before.
  - **Reaching one**: every route also answers at `/w/<workspace>/...`. An API key works in the workspace it was
    created in, with or without the prefix; naming another - or one that doesn't exist - is `404
    workspace_not_found`, so a caller can't learn which there are. A signed-in user's role is found in the workspace
    the path names.
  - **Who administers what**: an administrator's role is server-wide, as before, or `null` - with roles only in
    workspaces (`/api/v1/workspaces/{name}/members`). A role from a membership is that role in one workspace, without
    what only a server-wide one gives (administrators, settings, re-keying, erasing a person, creating workspaces);
    a workspace's owner manages its members and applies its configuration file. With roles only in other
    workspaces, an unprefixed call is `403 workspace_required`.
  - **`/api/v1/workspaces`**: create, describe, remove (owners; only an empty workspace, never `default`) - every
    change audited.
  - **Seeing what happened**: audit entries say their `workspace` (in the hash chain; `null` for the server's own),
    and a member reads their workspace's; alerts and events are the workspace's own, the server's alerts for
    server-wide roles; in `/metrics` a key or connection outside `default` is named `<workspace>/<name>`.
  - **Configuration files** take an optional `workspace:` and are applied there only; the CLI's `export`, `exports`,
    `collection`, `config export` and `examples` take `--workspace`.
  - **The Console**: a workspace switcher once there is a second workspace, every screen in the tab's workspace, and
    a Workspaces screen.
  - **Limits, for now**: a local-folder destination only in `default`; no per-workspace limits; identity-provider
    groups aren't mapped to memberships.
- **A sign-in page for the Console.** On a server that needs credentials, a tab without them sees only the sign-in
  page - **Sign in with SSO** when it's set up, and a field for an admin token or API key - instead of the whole
  Console with every screen failing. A refused token, or an SSO sign-in the server refused, is said there. **Sign out**
  in the sidebar ends a token's sign-in and comes back to it. A server open to all (no `QUERYAPIGATE_API_KEY`), and a
  key that isn't an administrator's, see the Console as before. The server decides as it always did; this changes only
  what a signed-out tab is shown.

## [0.37.0] - 2026-10-16

MongoDB graduates to tier 1, under the compatibility promise: a find() now streams its whole result - over HTTP, from
`queryapigate export` and in saved exports - in NDJSON, CSV, TSV and Parquet, the one essential feature it lacked.

### Upgrading from 0.36
- **Nothing to change.** No schema change, no setting. A server with a MongoDB connection no longer logs that it's
  experimental; H2 and JDBC connections still do.

### Added
- **MongoDB streams** ([ADR 0022](documentation/adr/0022-mongodb-streaming-and-graduation.md)): `?stream=true` on a
  saved `mongo` query and on `POST /execute_mongo`, `queryapigate export`, and saved exports to a bucket or a folder -
  in NDJSON, CSV, TSV and Parquet, with the same time limit, row limits, concurrency slot, history and grants as a SQL
  stream. Documents don't share a schema, so each format decides what that means: NDJSON writes each document whole;
  Parquet, and anything written to a destination, makes every field of every document a column; a streamed CSV or
  TSV takes its columns from the projection, in its order, or else from the first 1,000 documents - a field first
  seen later is left out, and the run's history counts them (`fields_dropped`). Exporting a `mongo` query
  incrementally is refused, for now; full exports work.

### Changed
- **MongoDB graduates to tier 1** - under the compatibility promise, its entry gone from `experimental.py`
  ([MongoDB connections](documentation/DATABASE_CONNECTION_CONFIGURATION.md#mongodb-connections)). It meets
  `databases.GRADUATION`: tested against MongoDB 8.0 in CI and at `pymongo` 4.0, its 0.34 security review's findings
  fixed and held by fuzzing, the time limit and `allowed_tables` working, and now streaming exports. The support
  matrix says *always (find() only)* for its read-only default. Its documentation says what may still be added
  (aggregation pipelines, writes, response caching, masking, incremental exports) and its limits. H2 and JDBC stay
  experimental.

### Fixed
- **A client that leaves a stream closes the database cursor at once**, not when the response is garbage-collected:
  the rows' close() closed the wrapper reading them but not the database's own generator - so a MongoDB cursor stayed
  open on the server, and a pooled connection checked out, a little longer than the request.

### Removed
- **The `stream_unsupported` error code**, which only a streamed MongoDB query answered with.

## [0.36.0] - 2026-10-16

A role for customer-facing APIs: its signed-in users run only saved queries that bind their own token, so a query
granted to it without a `from_claim` parameter can't show every customer every row. And image builds no longer depend
on one CDN for the H2 driver.

### Upgrading from 0.35
- **Nothing to change.** No schema change, no setting. Roles you have work as they did: `require_claim_binding` is
  off unless you turn it on - through the API, a configuration file, or the switch in the Console's role form.
- **MongoDB stays experimental** a release longer than 0.35 said: it graduates in 0.37, with streaming exports.

### Added
- **`require_claim_binding` on a role, for customer-facing APIs** ([Signed-in users](documentation/API.md#signed-in-users-jwt)).
  A `from_claim` parameter limits a query to the signed-in user's own rows - but only that query: another granted to
  the same role without one showed every user every row it returned, and nothing said so. With `"require_claim_binding":
  true`, a role's users run only saved queries that take a parameter from their token (anything else is `403
  claim_binding_required`, and left out of their `/catalog`, `/openapi.json` and MCP tools), reach queries by name or
  collection only - no connection, so no ad-hoc SQL or schema browsing - and the role refuses a connection grant. A
  role's alone: a key created from it doesn't take it. Off by default; through the API and configuration files.

### Fixed
- **Building the image could fail on Maven Central.** The H2 driver was fetched from repo1.maven.org alone, which twice
  answered GitHub's runners with something other than the jar - the pinned hash refused it, and the build stopped.
  The image now fetches it with the same script the release uses, trying Maven Central, Apache's mirror and Google's,
  a few times each; the hash still decides, so a mirror can't change what's installed. A wrong answer is reported -
  its status, type, size and first bytes - rather than only its hash.

## [0.35.1] - 2026-10-15

Two security fixes anyone running `allowed_tables` or ad-hoc SQL should take: a CTE named after a table read past a
key's table allow-list, and one statement could hold a worker for over a minute. Found by a review, CodeQL and the new
fuzzing - which arrive in this release too, with build provenance for the Python packages and a GitHub Release for
every version.

### Upgrading from 0.35.0
- **Nothing to change.** No schema change, no setting. Two things look different: the plain-text log writes a line
  break inside a message as `\n` (one event, one line), and a key whose stored expiry can't be read is refused - only
  a store edited by hand could hold one.
- **From a source checkout, fetch the H2 driver** before using an `h2` connection:
  `python .github/scripts/fetch_h2_jar.py`. The wheel and the images bundle it as before.

### Added
- **Build provenance for the sdist and wheel**: the release workflow attests what it uploads to PyPI with GitHub's
  build provenance, signed through Sigstore - `gh attestation verify <file> --owner AnanthaRajuC`. With the images'
  and the chart's, every release artifact meets **SLSA Build Level 2**; SECURITY.md says so, and why not Level 3.
- **OpenSSF Scorecard**, run on every push to `main` and weekly, its results published (the README's badge) and in
  the repository's code scanning alerts.
- **A GitHub Release for every version**, its notes the changelog's section, carrying the sdist, the wheel and their
  build provenance (`queryapigate-<version>.intoto.jsonl`) - made once PyPI has accepted them.
- **CodeQL** on the server's Python, the Console's TypeScript and the workflows, on every push and pull request.
- **Fuzzing with ClusterFuzzLite**: Atheris targets for the SQL guard, table checks, MongoDB documents and parameter
  rules (`fuzz/`), each checking a property - no server-side JavaScript survives into what MongoDB is sent, a
  declared parameter is never bound to a document - not only that nothing crashes. Five minutes on every pull
  request, longer on `main` and weekly.

### Changed
- **Every workflow gives its jobs a read-only token** unless a job asks for more, as `docs.yml` already did.
- **The H2 driver isn't committed any more.** The wheel and the image still bundle it: each build fetches
  `h2-2.2.224.jar` from Maven Central and refuses it unless its SHA-256 is the pinned one. In a source checkout, run
  `python .github/scripts/fetch_h2_jar.py` before using an `h2` connection - without it, one says so
  (`driver_missing`) rather than failing inside Java.

### Security
- **One request could hold a worker for over a minute** (found by CodeQL). Before checking a statement, the SQL guard
  stripped trailing whitespace and semicolons with a regular expression that took quadratic time on a long run of
  whitespace anywhere in it: 200,000 spaces in ad-hoc SQL - any key allowed to send some - took 81 seconds of CPU.
  It's a linear scan now: 9 ms for the same statement.
- **A line break in a caller's SQL, or `%0A` in a request path, could forge a line of the plain-text log** (CodeQL).
  A message's line breaks are written as `\n` now, and its backslashes as `\\`, so one event is one line; JSON logs
  (`QUERYAPIGATE_JSON_LOGS`) were never affected. Multi-line SQL reads as one line in the plain-text log.
- **`/console/` told anyone whether a file existed on the server** (CodeQL). Its check before serving a file looked
  outside the build: `/console/%2e%2e/...` naming a file that existed answered 404, one that didn't the Console's page.
  Nothing outside was ever served. Only paths inside the build are looked at now.
- **`allowed_tables` could be read past with a CTE named after the table** (found by review). Every table sharing a
  name with a CTE was dropped from the check, the one the CTE itself reads included: `WITH secret AS (SELECT * FROM
  secret) SELECT * FROM secret` named no table, so a key allowed only `orders` read `secret` - and so did a CTE read
  before it was defined, a schema-qualified `public.secret`, and the same inside a subquery. A name is a CTE's only
  where SQL makes it one now: after its `WITH`, in the CTEs after it, and in its own body under `WITH RECURSIVE`.
  Affects every release with `allowed_tables` (0.11.0 on). To check whether it happened: ad-hoc runs by keys with
  `allowed_tables`, in run history, whose SQL has a `WITH`.
- **An API key or admin token whose stored expiry couldn't be read never expired.** Every way in validates it, so
  only a store edited by hand or damaged had one; such an expiry now counts as passed.
- **The SIEM stream's TLS says its minimum, 1.2**, rather than leaving it to the default.
- **An audit entry that couldn't be written is logged as an error.** The action it recorded still stands, as before -
  but with the reason, rather than nothing.

### Fixed
- **`EXPLAIN EXPLAIN ...` was looked into once per `EXPLAIN`** - found by the new fuzzing. Each level re-scanned the
  whole statement, so a few thousand of them in one request cost quadratic time and ended in a `RecursionError` (a
  500). No database runs a nested `EXPLAIN`: it's refused as a write now (`read_only`), and nothing deeper is read.

## [0.35.0] - 2026-10-14

MongoDB is tested on every push against a real server, as every other connection type already was - and every
database's tests now fail CI if they skip. A person forgotten while usage counts were being written no longer keeps
a period of them.

### Upgrading from 0.34
- **Nothing to change.** No schema change, no setting. MongoDB stays experimental: it graduates in 0.36, with
  streaming exports.

### Added
- **CI tests MongoDB against a real server** - MongoDB 8.0 in the integration job: `find()` with filter, projection,
  sort and pages; a saved query's typed parameter; the time limit cancelling a find; `allowed_tables` as collections;
  and 0.34.2's refusals - server-side JavaScript and injected operators - sent to a real server. And at `pymongo`'s
  floor, 4.0, in the oldest-dependencies job. The support matrix now says MongoDB is tested in CI.

### Changed
- **The integration job fails if a database's tests skip** for want of their server, as the services step already
  did: a missing `QUERYAPIGATE_IT_*` would otherwise have passed it quietly.

### Fixed
- **Forgetting a person could leave a usage period behind** (0.29.0 to 0.34.2). If `queryapigate history forget` or
  `DELETE /api/v1/history?caller=` ran while the background writer was storing usage counts, the counts it was
  writing were in neither place `forget` looked: it missed them, and the writer stored them after the person was
  forgotten - a period of their usage counts kept, and left out of the count `forget` reported. Forgetting now waits
  for a write in flight, then deletes what it wrote. Runs and events were never affected. Found by CI, as a test
  that failed one run in many.

## [0.34.2] - 2026-10-13

### Security
- **A MongoDB parameter's value could be query operators** (0.9.0 to 0.34.1). A `:name` placeholder took whatever
  JSON was sent: for a saved `mongo` query whose placeholder wasn't declared with a type, `{"params": {"u": {"$ne":
  null}}}` turned `{"name": ":u"}` into a filter that matched every document - any caller of that query read the
  whole collection. Through `/execute_mongo`'s `params`, an object could carry `$function`, past the check that keeps
  server-side JavaScript out of the filter. A value is now a string, number, boolean, null or a list of those; a
  document is refused with `param_invalid`. Placeholders declared with a type were never affected. To check whether
  it happened: history keeps parameter names, never values, so look at the `rows` of each run of a saved `mongo`
  query with an undeclared placeholder - a run that returned far more than that query usually does.
- **Server-side JavaScript in a MongoDB projection or sort** (0.9.0 to 0.34.1). `$where`, `$function` and
  `$accumulator` were refused in a filter, but not in `projection` or `sort`, and MongoDB 4.4+ evaluates
  `{"x": {"$function": ...}}` in a projection: any key with a MongoDB connection could run JavaScript on the database
  server through `/execute_mongo`. They are refused there too, and when a saved `mongo` query is saved.

## [0.34.1] - 2026-10-13

### Security
- **`allowed_tables` wasn't checked on MongoDB connections** (0.11.0 to 0.34.0). A key narrowed to some tables read
  every collection of a `mongo` connection it was granted - through `/execute_mongo`, and through any saved `mongo`
  query it could run - though the documentation said such a key was refused there. On MongoDB the list is now the
  collections a key may `find()` in: one outside it is refused with `403 table_not_allowed` before the server is asked
  anything. Keys without `allowed_tables`, and every SQL connection, were never affected. To check whether it
  happened: `GET /api/v1/history?key=<name>&kind=adhoc` for each key with `allowed_tables` and a `mongo` connection -
  each ad-hoc run names its `mongo_collection` - and the saved `mongo` queries that key could run.

## [0.34.0] - 2026-10-13

Live events, and secrets from files and Vault, are stable: under the compatibility promise, each after a security
review ([ADR 0021](documentation/adr/0021-live-events-and-secrets-graduate.md)). The reviews found three things, fixed
here. AWS's secrets and credentials stay experimental until they're tested against AWS.

### Upgrading from 0.33
- **`psycopg2-binary` 2.9.5 or later** (was 2.9): 2.9.0 has no wheel for Python 3.11, the oldest supported, so the
  old floor was never one anybody could install from a wheel. CI now tests 2.9.5, on a PostgreSQL store.
- **If a script reads why a secret was unavailable from `error`, read `detail`** (below). The `code` and status are
  unchanged.
- **A `${file:...}` naming one of the server's own secret files is refused** (below). Nothing documented ever
  pointed one there; Settings lists which of the server's settings came from files.

### Changed
- **Live events are stable** ([ADR 0021](documentation/adr/0021-live-events-and-secrets-graduate.md)), with the 0.33
  payload unchanged. Event types, payload fields, filters and settings may still be added; ids, the fields documented
  and what each filter means stay as they are. Their limits are in the documentation. They're no longer marked
  experimental in Settings, `/openapi.json` (`GET /events`) or the startup log, of either server.
- **Secrets from files and Vault are stable** - `${VAR}`, `${file:...}`, `${vault:...}` and `vault-database`
  credentials, tested on every push against Vault 1.21. Providers, credential sources, sign-in methods and settings may
  still be added. **AWS stays experimental:** `${aws-sm:...}` and `aws-rds-iam`, documented under their own heading,
  [Secrets and credentials from AWS](documentation/API.md#secrets-and-credentials-from-aws), and the startup warning
  now names only the `QUERYAPIGATE_AWS_*` settings.
- **`secret_unavailable` (503) says only that a secret is unavailable.** Why - the reference and the provider's
  answer, a Vault address or path among them - is in the log and in the error's `detail`, which a scoped key calling a
  saved query doesn't get, as with a database's own error text.
- **The oldest-dependencies CI job runs the suite on a PostgreSQL store too**, with `psycopg2-binary` 2.9.5.

### Security
- **A new key with a deleted key's name saw the old key's runs.** The event log kept them under the name, so the new
  key could resume from `Last-Event-ID: 0` and read its predecessor's runs of the last
  `QUERYAPIGATE_EVENTS_RETENTION_HOURS`. A key now sees no event from before it was created.
- **A `${file:...}` could read the server's own secrets** where they were files inside an allowed folder -
  `QUERYAPIGATE_SECRET_KEY_FILE=/run/secrets/fernet` beside `QUERYAPIGATE_SECRET_FILE_DIRS=/run/secrets` - and so an
  administrator could have the server's key sent to a host of theirs as a password. A file named by any of the
  server's own settings or the credentials it runs with, the Kubernetes and EKS service account tokens, and `/proc`
  are never readable now, whatever the allow-list says.
- **`secret_unavailable` told any caller where the secrets live** - the file path, Vault's address, its path and its
  answer - a partner's scoped key and MCP clients included. Now only `detail` says, and only to those who'd get a
  database's own error text.

## [0.33.0] - 2026-10-13

Live events get a log of their own ([ADR 0020](documentation/adr/0020-the-event-log.md)). Until now an event was a row
of run history, so history's settings decided which runs became events and how far back a client could resume. Now
every run is written to the event log in the same batch as its history row, and kept for a set time. The payload says
what happened and where it came from, ready for event types other than a run.

### Upgrading from 0.32
- **Schema 16, applied on first start: a new `events` table.** The upgrade copies the runs still in history from the
  last `QUERYAPIGATE_EVENTS_RETENTION_HOURS` (24 by default) into it, each under its history row id, which was its
  event id. New event ids continue above every one the old server could have sent. A client resuming with an id from
  before the upgrade gets what it missed, and nothing twice.
- **If you read live events, update your client for the new payload** (below). Live events are experimental, so this
  needs no deprecation period.
- **Rolling upgrades:** replace `queryapigate events` with the instances. An instance still on 0.32 writes its runs to
  history only, so they aren't events until it's upgraded.

### Added
- **The event log** ([ADR 0020](documentation/adr/0020-the-event-log.md)): `queryapigate events` streams the new
  `events` table, not run history. Every run is an event, including one `QUERYAPIGATE_HISTORY_SAMPLE_RATE` samples
  out of history, and one whose saved query was deleted before its batch was written. Ids are never handed out twice:
  on SQLite, an id could be reused when the newest run was deleted before the next was recorded.
- **`QUERYAPIGATE_EVENTS_RETENTION_HOURS`** (default 24): how long the event log keeps an event, and so how far back a
  client can resume. History's limit, retention and sample rate no longer decide it.
- **Subscription filters** on both `/events` endpoints: `?type=`, `?kind=` (`saved` or `adhoc`), `?query=`,
  `?connection=` and `?status=` (`success` or `error`), combined with AND. They narrow what a key may see, never
  widen it, and a resume applies them too. A `kind` or `status` that can never match is refused with
  `400 invalid_filter`.
- **`GET /metrics` on `queryapigate events`**, behind `QUERYAPIGATE_METRICS_TOKEN` like the main server's:
  `queryapigate_events_streams`, `_delivered_total`, `_replayed_total`, `_slow_disconnects_total` and
  `queryapigate_events_lag_seconds`, from when an event happened to its delivery.

### Changed
- **The live event payload (experimental)** ([ADR 0020 §5](documentation/adr/0020-the-event-log.md)): every run is
  `"type": "query.run"`, with `source` (the front door: `rest`, `mcp`, `export`), `occurred_at`, and `kind`, `query`
  and `version` as `GET /api/v1/history` names them. `"type": "execution"`, `"type": "adhoc_execution"` and
  `filename` are gone: check `kind` instead, and read `query` instead of `filename`. `connection_name` and `entry` are
  unchanged. The Console's Home is updated to match.
- **`queryapigate history forget` and `DELETE /api/v1/history?caller=` erase the caller's events too**, and report
  how many as `events`.
- **Live events stay experimental for this release**, as ADR 0017 asks of a payload that has just changed. They
  graduate in the next release if the payload stays unchanged.

### Fixed
- **A resume more than 1,000 events back lost the rest.** `queryapigate events` replayed the first 1,000 events after
  `Last-Event-ID`, then skipped to live without a word. It now replays every page up to the live stream. A client too
  slow to take it is disconnected as too slow, and resumes from where it got to.

## [0.32.0] - 2026-10-13

The audit log's stream to a SIEM, tested on every push against the software that receives it - rsyslog, syslog-ng and
an OpenTelemetry Collector - and under the compatibility promise. And a SIEM's own copy of the log is something you can
check on its own: the same command that checks the store.

### Upgrading from 0.31
- **Nothing to change.** No schema change, no setting; the stream's messages are exactly as before.

### Added
- **CI tests the SIEM stream against real receivers** ([ADR 0019](documentation/adr/0019-the-siem-stream-against-real-receivers.md)):
  rsyslog 8.2604 and syslog-ng 4.12.0 taking RFC 5425 syslog over mutual TLS, and an OpenTelemetry Collector 0.160.0
  taking OTLP/HTTP logs, each pinned by digest - `tests/test_audit_siem.py` reads what each one parsed and wrote:
  every entry, in order, as meant; a missing client certificate, and one from another CA, refused and noticed with
  nothing lost; an outage - the receiver stopped with a connection still open - survived; and the receiver's copy
  verified by `audit verify --file`, an edit in it found. `.github/scripts/siem_receivers.sh` starts the three, for
  CI or for you.
- **Guide 53 re-run** against rsyslog and the OpenTelemetry Collector:
  [Send the audit log to your SIEM, and prove nobody edited it](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/how-to/53-send-the-audit-log-to-your-siem.md) -
  a refused client certificate, an outage with OTLP keeping up, and the SIEM's copy verified.

### Changed
- **The audit log streamed to a SIEM is stable** ([ADR 0019](documentation/adr/0019-the-siem-stream-against-real-receivers.md)),
  under the compatibility promise: it met the last of `experimental.GRADUATION`, a real receiver in CI. Its
  documentation says what may still be added and its limits; it's no longer marked experimental in Settings,
  `/openapi.json` (`GET /api/v1/audit/delivery`) or the startup log. The OTLP records' attributes are documented in
  full - `queryapigate.audit.prev_hash` was sent and not listed.
- **`queryapigate audit verify --file` checks what your SIEM received** ([ADR 0019](documentation/adr/0019-the-siem-stream-against-real-receivers.md)):
  syslog lines as a receiver writes them, as well as JSON lines; an entry repeated - delivery to a SIEM is at least
  once - is dropped when it's an exact repeat, and counted; entries are checked in `seq` order, however they arrived.
  A repeat that differs from the first copy is reported, `conflicting_copies`, with the two lines. A SIEM's copy of
  an untouched log used to fail with `out_of_order` as soon as it held one repeat.

## [0.31.0] - 2026-10-13

The first signed release: the image and the Helm chart, signed keyless by this repository's release workflow, with
provenance naming the commit and an SBOM of what's inside - and checked before they're published, in the two forms
cosign and today's admission controllers read. What a release is built from is pinned: base images by digest, every
GitHub Action by commit.

### Upgrading from 0.30
- **Nothing to change.** No schema change, no setting. To run only signed releases from here on, see
  [guide 59](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/how-to/59-verify-a-release-before-you-run-it.md);
  0.30 and earlier aren't signed, so a policy that requires a signature refuses them.

### Added
- **Signed releases** ([ADR 0018](documentation/adr/0018-signed-releases.md)): each image variant and the Helm chart
  are signed keyless with cosign, by digest - the certificate names this repository's `docker.yml` at the release's
  tag - in both forms verifiers read: the Sigstore bundle (cosign v3, `gh attestation`) and the classic `.sig` tag
  that admission controllers such as Sigstore's policy-controller still need. With SLSA provenance and an SPDX SBOM on each image, and a GitHub attestation for each; the workflow verifies
  them before it ends, so a release that doesn't verify fails. `Publish Docker image` run by hand with `rehearse`
  does all of it under `-rehearsal` names, never the real ones.
- **`image.digest` in the Helm chart**: the digest you verified, so the image is pulled by it.
- **Guide 59,** [Verify a release before you run it](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/how-to/59-verify-a-release-before-you-run-it.md):
  `cosign verify`, the GitHub attestation, the provenance and the SBOM - and a kind cluster with Sigstore's
  policy-controller 0.13.1, then Kyverno 1.19.1, admitting the signed image and refusing an unsigned one, all run
  against the release workflow's rehearsal. SECURITY.md has the two commands, for a release.

### Changed
- **What a release is built from is pinned** ([ADR 0018](documentation/adr/0018-signed-releases.md)): every GitHub
  Action by commit SHA - the PyPI publishing one followed a branch, `release/v1` - and the image's base images,
  `python:3.12-slim` and `node:22-slim`, by digest. Dependabot proposes each update as a commit; a test fails on an
  action or a base image that isn't pinned.

## [0.30.0] - 2026-10-13

Eight features come under the compatibility promise - descriptions, agent usage, usage quotas, configuration as code,
OAuth for the MCP server, Console sign-in through SSO, write tools over MCP and the Helm chart - each after a
security review, tested on the oldest dependency versions it allows, its limits written down
([ADR 0017](documentation/adr/0017-what-graduates-for-1-0-0-rc1.md)). The reviews closed three ways a server let out
what it shouldn't: its own cloud credentials through a `${VAR}` reference, `/metrics` through the chart's ingress, and
words of anyone's choosing in the Console's sign-in banner.

### Security
- **A `${VAR}` reference could name the server's own cloud credentials.** Only its `QUERYAPIGATE_*` and `OTEL_*`
  settings were refused, so an administrator who may change connections - or the author of a configuration file -
  could save one whose password was `${AWS_SECRET_ACCESS_KEY}` and whose host was theirs, and receive the server's AWS
  identity: more than every allow-list of 0.28 opens. Now `AWS_*`, `AZURE_*`, `ARM_*`, `GOOGLE_*`, `GCP_*`, `VAULT_*`
  and `KUBERNETES_*` are refused too, when saved and when used; and **`QUERYAPIGATE_SECRET_ENV_VARS`** (names, or
  prefixes ending in `*`) limits `${VAR}` to what it lists, like the other references' allow-lists. Unset, any other
  variable may still be referenced - the server warns at startup while references are in use - so nothing breaks on
  upgrade unless a connection used one of those names. Found in the security review for 0.30
  ([ADR 0017](documentation/adr/0017-what-graduates-for-1-0-0-rc1.md)).
- **The Helm chart published `/metrics` through its ingress.** The ingress routes every path to the API server, and
  `/metrics` was open, with API keys', queries' and connections' names in its labels - and each administrator's own
  name, often their username. Now **`QUERYAPIGATE_METRICS_TOKEN`** (at least 16 characters) makes `/metrics` - the API
  server's and the MCP server's - answer only `Authorization: Bearer <the token>`, and an administrator who may
  observe (the Console's Metrics screen); unset, it stays open, for setups no one outside reaches. The chart sets one
  (`metricsAuth`, on by default): made in a Secret of its own, kept across upgrades, and sent by its ServiceMonitor. And
  administrators are counted under one `admin` label, as signed-in users are under `jwt`, so no person's name is a
  label anywhere. Found in the security review for 0.30.
- **The Console showed what a sign-in link said.** A refused sign-in through SSO came back to the Console as
  `?sso_error=...&sso_message=...`, and the Console showed `sso_message` as written - so anyone could send an
  administrator a Console link whose error banner said what they liked. Now only the code travels, and the Console
  says what each one means in its own words; the reason stays in the audit log. Found in the security review for 0.30.

### Upgrading from 0.29
- **On the Helm chart, `/metrics` needs a token** (`metricsAuth`, on by default). The ServiceMonitor sends it; any
  other scraper reads it from the `<release>-metrics` Secret. Rendered without the cluster (`helm template`, Argo CD),
  set `metricsAuth.existingSecret` to a Secret of yours - the chart can't see the one it made, and would make another.
  `metricsAuth.enabled: false` keeps `/metrics` open, as before.
- **In `/metrics`, administrators are `key="admin"`** - each one used to be under their own name. A dashboard that
  split by administrator: the audit log says which one did what.
- **No schema change.**

### Added
- **Guide 58,** [Keep the server's own secrets, and its metrics, in](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/how-to/58-keep-the-servers-own-secrets-and-metrics-in.md):
  `QUERYAPIGATE_SECRET_ENV_VARS` and `QUERYAPIGATE_METRICS_TOKEN` on a running server, with Prometheus 3.15 scraping
  it - and refused without the token.
- **Higher minimum versions:** Flask 3.0 (was 2.2), Werkzeug 3.0 (now listed: QueryAPIGate imports it), PyYAML 6.0.1
  (was 6.0), and for the `mcp` extra, mcp 1.21.1 (was 1.10). None of the old minimums could actually be used: Flask
  2.x can't be imported beside the Werkzeug 3 that pip installs with it, PyYAML 6.0 can't be installed on Python 3.12
  or later, and mcp before 1.21.1 can't be imported beside the pydantic pip installs with it.

### Changed
- **Stable now, under the compatibility promise:** descriptions, agent usage, usage quotas, configuration as code,
  OAuth for the MCP server, Console sign-in through SSO, write tools over MCP, and the Helm chart's values
  ([ADR 0017](documentation/adr/0017-what-graduates-for-1-0-0-rc1.md)). Each met every one of `experimental.GRADUATION`, and each one's documentation
  now says what may still be added and its limits. No longer marked experimental in `/openapi.json`, Settings, the
  Console's Agents and Approvals screens and quota fields, the chart's `Chart.yaml` and notes, or the startup log.
  Usage quotas' documentation now says what doesn't count - exports, live events, MCP resources, the Management API -
  and that an agent's write request does.
- **`config diff` and `config apply --server`** are tested against a real `queryapigate serve`, over HTTP.
- **OpenAPI no longer calls column masking experimental** - two field descriptions left over from before 0.20.
- **A test holds the MCP server's key fetching to no redirects** - PyJWT's own behaviour, from 2.14, the floor.
- **CI tests the lowest version allowed of every core dependency, and of the `mcp`, `jwt` and `redis` extras**
  ([ADR 0017](documentation/adr/0017-what-graduates-for-1-0-0-rc1.md)) - the whole suite on Python 3.11, with Redis.
  It tested only DuckDB's and sqlglot's before. A test fails if a minimum in pyproject.toml and the version CI pins
  for it differ, or if a core dependency has no pin.

## [0.29.0] - 2026-10-12

The answers a security review asks for, written down and kept true: where every kind of data goes, for how long and
who reads it; values kept out of the logs; a person's data erased on request; and cryptography that is FIPS-approved
throughout, tested under OpenSSL's FIPS provider.

### Upgrading from 0.28
- **The image's access log no longer has query strings or referrers.** A log pipeline that parsed them:
  `QUERYAPIGATE_ACCESS_LOG_QUERY=1` brings the query string back. A `command:` override of the image needs
  `-c python:queryapigate.gunicorn_conf` to keep the new format.
- **No schema change.**

### Added
- **Where data goes** (BACKLOG #101, [ADR 0016](documentation/adr/0016-data-flows-and-fips.md)):
  [documentation/DATA_PROTECTION.md](documentation/DATA_PROTECTION.md) - every place QueryAPIGate keeps or sends data:
  what, for how long and by which setting, who reads it, what is never kept, and the categories of personal data - for
  a record of processing or a security questionnaire. A test fails if a table of the store, a setting that bounds
  what's kept, or a way data leaves isn't on it.
- **FIPS 140:** every algorithm QueryAPIGate uses is FIPS-approved - a test fails if the code gains one that isn't -
  and [Where data goes](documentation/DATA_PROTECTION.md#cryptography) lists each use and says how to run on a
  validated module: Red Hat Enterprise Linux 9 or Ubuntu Pro FIPS, with Python and `cryptography` from the
  distribution (PyPI's wheel carries an unvalidated OpenSSL), and the database drivers built against it. A new CI job
  runs the whole suite in Red Hat UBI 9 with OpenSSL's FIPS provider forced, and first checks that it is.
- **Forgetting a person:** `queryapigate history forget CALLER` (`--dry-run`) and `DELETE /api/v1/history?caller=`
  delete every run by a caller - `jwt:alice@example.com` - and their usage counts, in the store and in Redis.
  Audited as `forget_caller` by the caller's SHA-256, not their name. Owners only (`history.forget`). Guide 57,
  [Answer a security questionnaire about data and cryptography](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/how-to/57-answer-a-security-questionnaire-about-data-and-cryptography.md),
  every answer checked on a running server.

### Changed
- **The access log leaves query strings out.** The image's gunicorn (`-c python:queryapigate.gunicorn_conf`) and
  `queryapigate serve` log the request's path without its query string - parameter values sent in the URL - and
  never the referrer. `QUERYAPIGATE_ACCESS_LOG_QUERY=1` keeps the query string. A `command:` override of the image
  needs `-c python:queryapigate.gunicorn_conf` to keep this.
- **With `QUERYAPIGATE_HISTORY_ADHOC_SQL` `hash` or `none`, the log shows statements by their SHA-256** too - an
  ad-hoc statement, or a legacy `{name}` placeholder, can carry a value in its text.

## [0.28.0] - 2026-10-12

No database password anywhere: a connection's secrets come from where they live - a file the CSI driver or Vault
Agent keeps current, Vault, AWS Secrets Manager - and its credentials can be short-lived, made by Vault per lease or
as RDS IAM tokens, rotated with no failed call. Nothing is reachable until the deployer lists it, and a provider
that fails is forgiven only within what it gave before. Experimental in 0.28.

### Upgrading from 0.27
- **No schema change.** Nothing changes for a deployment that sets none of the new settings - except the TLS fix
  below: a PostgreSQL or MySQL connection whose `sslmode` or `ssl_*` asked for more than its server gives now fails
  instead of connecting without it.

### Security
- **A PostgreSQL or MySQL connection's TLS settings are now given to its driver.** `sslmode` (with `sslrootcert`,
  `sslcert`, `sslkey`, `sslcrl`) and MySQL's `ssl_ca`, `ssl_cert`, `ssl_key`, `ssl_verify_cert`,
  `ssl_verify_identity` and `ssl_disabled` were documented as passed through, and weren't: a connection set to
  `verify-full` connected without checking the server's certificate - with libpq's default, `prefer`, even without
  TLS when the server didn't offer it. They are passed now, and only these: a connection that asks for TLS the
  server can't give, or a certificate that doesn't verify, now fails instead.

### Fixed
- **`queryapigate serve` now exits cleanly on SIGTERM**, so what runs at exit does - the audit stream's lease given
  back (0.25), Vault leases revoked. It used to be ended without it. (The image's gunicorn already exited cleanly.)

### Added
- **Secrets from files** (BACKLOG #98,
  [ADR 0015](documentation/adr/0015-secrets-managers-and-short-lived-credentials.md), experimental), first part: a
  connection's or destination's field can be `${file:/run/secrets/name}` - read when the connection is opened, again
  as soon as the file changes, so a password the CSI driver or Vault Agent rotates in place is used without a restart.
  Only inside `QUERYAPIGATE_SECRET_FILE_DIRS`, never the server's home, checked when saved and when used. Resolved
  values are kept in memory at most `QUERYAPIGATE_SECRET_CACHE_SECONDS`; past that a failing provider fails the
  connection with `503 secret_unavailable`, never a stale secret. Every secret setting can be read from a file too:
  `QUERYAPIGATE_SECRET_KEY_FILE` and the like. **Vault:** `${vault:secret/data/warehouse#password}` from the KV
  engine, and a connection's `credentials: {source: vault-database, role: ...}` - a username and password from the
  database secrets engine for each lease, shared by the connections opened during it, every pooled connection retired
  before its lease ends, and the leases revoked when the process exits. The server signs in with a token (or Vault
  Agent's token file), Kubernetes auth or AppRole; `QUERYAPIGATE_VAULT_PATHS` and `QUERYAPIGATE_VAULT_DATABASE_ROLES`
  say what it may ask for. Tested in CI against Vault 1.21. **AWS** (the `aws` extra, in the image):
  `${aws-sm:prod/warehouse#password}` from Secrets Manager, by name or ARN, and `credentials: {source: aws-rds-iam}`
  - an IAM token as the password for RDS and Aurora, PostgreSQL or MySQL, only over TLS - with IRSA through the
  chart's `serviceAccount.annotations`; `QUERYAPIGATE_AWS_SECRET_PREFIXES` and `QUERYAPIGATE_AWS_RDS_HOSTS`. The
  chart's `vaultToken` mounts a projected token for Vault's Kubernetes auth. The `secret_provider_failing` alert, and
  each provider's last answer in Settings. A Vault database role can be bound to its database's host
  (`reporting-ro@warehouse.internal`), so its credentials go to no other. Guide 56,
  [Use short-lived database credentials from Vault](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/how-to/56-use-short-lived-database-credentials-from-vault.md),
  run against Vault 1.21: 861 calls across four leases, none failed. See
  [Secrets from files and secrets managers](documentation/API.md#secrets-from-files-and-secrets-managers).

## [0.27.0] - 2026-10-12

A server's setup as a file in git: connections, saved queries, roles, keys' grants and the rest, with no secret in it,
reviewed as a pull request against what the server holds now, and applied all or nothing - by CI, or by the server
itself as it starts, which with the Helm chart makes a merge the deploy. Experimental in 0.27.

### Upgrading from 0.26
- **Schema 15** adds two tables, `config_managed` (what a configuration file manages) and `config_applied` (every
  file applied, by its hash). Additive - 0.26 instances keep working on the upgraded store.

### Added
- **Configuration as code** (BACKLOG #97, [ADR 0014](documentation/adr/0014-configuration-as-code.md), experimental),
  first part: `queryapigate config export` writes a server's connections, destinations, roles, saved queries, API
  keys' grants, descriptions and exports as one YAML file, with no secret in it - passwords as `${VAR}` references,
  keys as their hashes. `queryapigate config diff FILE` says, field by field, what applying a file would change, and
  reports every problem in it at once (exit 0, 2 or 1, as `terraform plan -detailed-exitcode`). `config new-key`
  makes a key's secret and its hash. `queryapigate config apply FILE` (and `POST /api/v1/config/apply`) makes the
  server match it, all or nothing: one transaction, every change through the API's own rules and audited
  `via: config`, under one `config_applied` entry with the file's hash. `--prune` deletes only what a file manages
  (applied or named before), never what was made in the Console. `POST /api/v1/config/diff` too; both owners only
  (`config.apply`). See [Configuration as code](documentation/API.md#configuration-as-code).
  `QUERYAPIGATE_CONFIG_FILE` applies a file at startup, once per file - with several instances, the first to start
  applies it - and one that can't be applied stops startup, changing nothing; `QUERYAPIGATE_CONFIG_PRUNE` prunes
  then too. `QUERYAPIGATE_CONFIG_LOCKED` keeps what a file manages to the file: the API and the Console answer
  `409 config_managed`. `GET /api/v1/config/managed`, and the Console tags what's managed. The Helm chart's
  `configAsCode` mounts the file and rolls the pods when it changes - checked on kind, a file that can't be applied
  stopping the rollout at its first pod. `config diff|apply --server URL` runs against a running server, for CI.
  Guide 55,
  [Promote a setup from dev to prod through a pull request](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/how-to/55-promote-a-setup-from-dev-to-prod-through-a-pull-request.md),
  run against two real servers.

### Security
- **A connection or destination can't reference the server's own `OTEL_*` settings** (`${OTEL_EXPORTER_OTLP_HEADERS}`
  often holds a collector token), as it already couldn't `QUERYAPIGATE_*` ones: an administrator could otherwise
  have one sent to a host of theirs as a password. One saved with such a reference stops working; give it a
  variable of its own.
- **An audit entry's copy in the export file is written once its transaction commits**, so an entry rolled back -
  a configuration file that didn't apply - never appears there.

## [0.26.0] - 2026-10-12

QueryAPIGate on Kubernetes the way most teams install things there: `helm install` from ghcr, one pod on SQLite or
several on PostgreSQL and Redis, locked-down pods, and a chart that refuses setups that can't work. CI installs it on a
real cluster and upgrades it under load on every change. The chart is experimental in 0.26.

### Upgrading from 0.25
- **No schema change**, and rolling upgrades from 0.25 work as usual.
- **The image needs gunicorn 26 or later and passes `--no-control-socket`.** If you override the image's `command:`,
  add the flag (DEPLOYMENT.md's example has it). Without it gunicorn writes a control socket under the home folder,
  which a read-only root filesystem doesn't allow.

### Added
- **A Helm chart** (BACKLOG #100, [ADR 0013](documentation/adr/0013-helm-chart.md), experimental): `deploy/helm/queryapigate`,
  for Helm 3 and 4, released with the server (chart X.Y.Z runs X.Y.Z). One pod on SQLite with its store on a volume, or
  several on PostgreSQL and Redis rolled one at a time behind a disruption budget; the secrets from a Secret you manage
  (or one the chart makes), every other setting as `config`; ingress with `/events` routed to the events server, the
  MCP server on its own host; locked-down pods (non-root, read-only root filesystem, no capabilities); a
  ServiceMonitor, NetworkPolicy and autoscaler if you want them. `helm install` refuses what can't work - more than
  one pod on SQLite or without Redis, a secret setting in `config` (plain text in the Deployment) - and
  `values.schema.json` a misspelt value. `extraVolumes` for files settings name by path, such as the SIEM's
  certificates.
  Published with each release: `helm install queryapigate oci://ghcr.io/anantharajuc/charts/queryapigate --version
  0.26.0`. CI lints it with Helm 3 and 4, renders every option, and installs it on kind, upgrading from the previous
  release with requests running and failing on a single failed request. Guide 54,
  [Install on Kubernetes with Helm](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/how-to/54-install-on-kubernetes-with-helm.md),
  run on kind; [Kubernetes (Helm)](documentation/DEPLOYMENT.md#kubernetes-helm) in the deployment guide.

### Changed
- **The image starts gunicorn with `--no-control-socket`** (and gunicorn 26 or later): its control interface, unused,
  wrote under the home folder - the one write outside `/data` and `/tmp`. The image now runs on a read-only root
  filesystem.
- **`deploy/kubernetes/queryapigate.yaml`** runs its pods the same way - non-root, read-only root filesystem, no
  capabilities, no service account token - and gives the events server a liveness probe. A test keeps it in step
  with what the chart renders.

### Fixed
- **On several instances, the example queries answered 404 from every instance but the one that loaded them**: the
  store said they were there, but their SQLite database was a file only that instance had. Each instance now makes
  its own copy when it's missing. Found by the new Kubernetes test in CI.
- **The threat model still said the audit log wasn't tamper-evident**, in two places 0.25 missed. It now says what
  the chain shows and what only a copy outside the store does. It also gains a section on running on Kubernetes.

## [0.25.0] - 2026-10-12

An audit trail an auditor can rely on: every administrative change chained to the one before and signed, checked in
one command, and streamed as it happens - over mutual TLS - to the SIEM where security teams already look, without
losing an entry while it's down. The chain is stable; the stream is experimental.

### Upgrading from 0.24
- **Schema 14** adds three columns to `audit_log` (`seq`, `prev_hash`, `hash`) and two tables (`audit_chain`,
  `audit_delivery`). The entries already stored are chained at the upgrade, as they are, and an `audit_chain_started`
  entry records when. A 0.24 instance still running keeps writing entries without a link; the next upgraded write
  links them in - but upgrade every instance.

### Added
- **A tamper-evident audit log** (BACKLOG #96, [ADR 0012](documentation/adr/0012-tamper-evident-audit-log-and-siem.md)):
  each entry chained to the one before (`seq`, `prev_hash`, `hash`), HMAC-signed with `QUERYAPIGATE_AUDIT_KEY` when set
  (rotated as `new,old`), plain SHA-256 otherwise; the chain kept through the rolling cap by an anchor.
  `queryapigate audit verify [--file]` and `GET /api/v1/audit/verify` name the first entry that doesn't verify -
  changed, missing, out of order, or re-written without the key - and the `audit_chain_broken` alert says so. The
  export file's lines carry the chain too. See
  [A tamper-evident chain](documentation/API.md#a-tamper-evident-chain).
- **The audit log streamed to a SIEM** (ADR 0012, experimental): syslog (RFC 5424) over TLS - mutual TLS too - or
  TCP, with `QUERYAPIGATE_AUDIT_SYSLOG_URL`; OTLP logs with `QUERYAPIGATE_AUDIT_OTLP=1`. A cursor per output in the
  store and a lease, so one instance delivers and another takes over; at least once; entries not yet delivered kept
  past the rolling cap, up to ten times it. `GET /api/v1/audit/delivery` and the `audit_delivery_behind` alert. See
  [Streaming the audit log to a SIEM](documentation/API.md#streaming-the-audit-log-to-a-siem). A process that exits
  cleanly gives its lease back, so another takes over at once. Guide 53,
  [Send the audit log to your SIEM](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/how-to/53-send-the-audit-log-to-your-siem.md),
  run against syslog-ng over mutual TLS.

## [0.24.0] - 2026-10-11

Limits a contract can be written in: a key or role can cap the queries it runs at once, how long each may run and
how many rows a call returns - so one partner can't starve the rest - and carry a quota of calls and rows per day or
month, enforced across every instance and reported on every response. The limits are stable; quotas are
experimental.

### Added
- **Per-key limits on concurrency, time and rows** (BACKLOG #94,
  [ADR 0011](documentation/adr/0011-per-key-limits-and-quotas.md)): `max_concurrent`, `query_timeout` and `max_rows`
  on keys and roles, each narrowing the server's own limit, never widening it, on every front door - REST, ad-hoc
  SQL, MCP tools. `max_concurrent` counts queries running at once, a stream until its last row is read, across
  instances through Redis, with leases so a dead process's slots free themselves; over it, `429
  too_many_concurrent`. A key shows its `effective_limits`. See
  [Per-key limits](documentation/API.md#per-key-limits-concurrency-time-and-rows).

- **Usage quotas** (BACKLOG #95, ADR 0011, experimental): `"quota": {"calls": 100000, "rows": 10000000, "per":
  "month"}` on keys and roles - calendar days or months, per caller (per user for signed-in users). Successful data
  calls and their rows count, cached responses included; used up, `429 quota_exceeded` with `resets_at`.
  `X-Quota-Remaining-Calls`, `X-Quota-Remaining-Rows` and `X-Quota-Reset` on every response to a caller with one; a
  key's `usage_this_period`, `GET /api/v1/usage/quotas` and the `quota_nearly_used` alert. Counted in the store
  (schema 13, `usage_counters`) through the run-history batch, not a write per call, and with several instances in
  Redis, one counter for all of them: measured, 1,016 calls answered for a quota of 1,000 on two instances under
  load (1,615 without Redis). See
  [Usage quotas](documentation/API.md#usage-quotas).
- **`X-Row-Count`** on every paged data response: the rows in it.
- **The Console:** the key and role forms take *Queries at once*, *Time limit*, *Rows per call* and a *Quota*; the API
  keys list shows how much of its quota each key has used, amber from 80%, red when used up. Guide 52,
  [Give a partner a quota and a concurrency cap](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/how-to/52-give-a-partner-a-quota-and-a-concurrency-cap.md),
  with the precision measured.

### Upgrading from 0.23
- **Schema 13** adds one table (`usage_counters`); nothing existing changes. A 0.23 instance still running keeps
  working against the upgraded store, but doesn't count or enforce quotas - upgrade every instance before giving a
  key one.

### Fixed
- **A streamed response closed before its first chunk** - the client gone at once - left its query counted as
  active and its database connection checked out until garbage collection: closing a generator that never started
  runs none of its code. The response now closes the rows itself.

## [0.23.0] - 2026-10-11

Seeing inside, and hardened at the edge: QueryAPIGate's spans in your traces - from the app's request through the
saved query to the database - without a parameter value, row or secret ever leaving in one; and security headers on
every response, so none depends on a proxy to add them. Tracing is experimental; the headers are stable.

### Added
- **Tracing with OpenTelemetry** (BACKLOG #92, [ADR 0010](documentation/adr/0010-tracing-and-security-headers.md),
  experimental): `pip install "queryapigate[otel]"` and set `OTEL_EXPORTER_OTLP_ENDPOINT` - every other `OTEL_*`
  variable works as usual. Spans for each request (by route rule), saved query, database call, serialisation, MCP tool
  call, live stream opened and export, continuing a caller's `traceparent` (and `TRACEPARENT` for
  `queryapigate export`). `trace_id` in run history, error bodies and JSON log lines. Spans never hold parameter
  values, rows, statements, headers or secrets - a test fails if one does; an MCP client's tool name only when it is
  well formed. Measured: about 1 ms a request with every request traced, nothing measurable at 10% sampling. Guide 51,
  [See QueryAPIGate in your traces](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/how-to/51-see-queryapigate-in-your-traces.md).
  See [Tracing](documentation/API.md#tracing-opentelemetry). The Docker image includes the extra: set the endpoint to
  turn it on.
- **Security headers on every response** (BACKLOG #93, [ADR 0010](documentation/adr/0010-tracing-and-security-headers.md)):
  `X-Content-Type-Options: nosniff`; on API responses `Content-Security-Policy: default-src 'none'; frame-ancestors
  'none'` and `Referrer-Policy: no-referrer`; `Cache-Control: no-store` unless the route says otherwise - on the main
  server, the MCP server and `queryapigate events`, so none relies on a proxy to add them. HSTS when
  `QUERYAPIGATE_HSTS_MAX_AGE` is set, only on requests that arrived over HTTPS. See
  [Security headers](documentation/API.md#security-headers).

### Changed
- **`/docs` loads one exact Swagger UI release (5.33.0), with Subresource Integrity hashes** and a CSP that allows
  only those files and the page's own script - it loaded the newest 5.x from the CDN, unchecked, on the page where
  people type their API key.
- **Responses are `Cache-Control: no-store`** unless they say otherwise (a cached saved query's
  `private, max-age=N`, the Console's assets). A client that kept API responses in its HTTP cache no longer may.
- **No version banner:** `queryapigate serve` answers `Server: QueryAPIGate` (was Werkzeug's and Python's versions);
  the MCP server sends no `Server` header.

## [0.22.1] - 2026-10-11

### Security
- **A cached saved query's response could be kept by a shared cache in front of the server** (0.4.0 to 0.22.0). A
  saved query with `cache_ttl` answered with `Cache-Control: max-age=<cache_ttl>`. Callers authenticate with
  `X-API-Key`, not `Authorization`, so HTTP's rule that keeps authenticated responses out of shared caches doesn't
  apply: a CDN or caching proxy between callers and QueryAPIGate could store one caller's rows - masked or filtered
  for that caller's key - and serve them to the next request for the same URL, with any key or none. The server's own
  response cache was never affected: it checks the caller's grant before answering, and keys each response by its
  parameter values and the caller's masking rules. Now it is `Cache-Control: private,
  max-age=<cache_ttl>`, on hits and `304`s too. To check whether it could have happened: whether anything in front of
  the server caches responses, and whether it honoured `max-age` on `/q/` paths.

## [0.22.0] - 2026-10-11

Rotating secrets without downtime: the key stored passwords are encrypted with, and an API key's secret, can each be
replaced while everything keeps working - several encryption keys at once and `queryapigate rekey`, and a reissued
API key whose old secret works for a grace period you choose. Both are stable from the start, so 1.0 freezes them.

### Upgrading from 0.21
- **Schema 12** adds two nullable columns to `api_keys` (`previous_hash`, `previous_expires_at`) and an index; nothing
  existing changes. A 0.21 instance still running keeps working against the upgraded store, but doesn't know a
  rotated key's old secret - so upgrade every instance before rotating a key.

### Added
- **Reissuing an API key's secret** (BACKLOG #91, [ADR 0009](documentation/adr/0009-key-rotation.md)):
  `POST /api/v1/api-keys/{name}/rotate` gives a key a new secret, shown once, while the old one keeps working for
  `grace_minutes` (default 60, at most a week; 0 for a leaked secret) - same name, same grants, the same history, so
  whoever uses the key switches without an outage. `previous_expires_at` and `previous_last_used_at` on the key show
  whether they have; `DELETE /api/v1/api-keys/{name}/previous` ends the grace early. A second rotation while an old
  secret still works is refused (`key_rotation_pending`). Audited as `rotate_api_key` and `end_previous_secret`.
- **Rotating the encryption key** (BACKLOG #90, [ADR 0009](documentation/adr/0009-key-rotation.md)):
  `QUERYAPIGATE_SECRET_KEY` takes several keys, comma-separated - the first encrypts, every one decrypts - and
  `queryapigate rekey` (or `POST /api/v1/settings/rekey`, owners only: the new `secrets.rotate` capability)
  re-encrypts every stored connection and destination password under the first, in one transaction: all of them, or
  none when one can't be decrypted by a listed key. `--check` (`{"check": true}`) only reports how many are under
  each key. Until the old keys are removed, startup logs it, Settings shows it, and the
  `encryption_key_rotation_incomplete` alert says what's left. Audited as `rekey`, with counts only. One key behaves
  exactly as before. Guide 22's [Changing the key](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/how-to/22-encrypt-passwords-at-rest.md#changing-the-key).

- **The Console:** API keys → Edit → **Secret** rotates a key (keeping the old secret working for an hour, a day, a
  week, or not at all), shows the new secret once, and shows an old secret's expiry and last use, with **End now**;
  a key being rotated is tagged *rotating*. Guide 50,
  [Rotate an API key without downtime](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/how-to/50-rotate-an-api-key-without-downtime.md).

### Fixed
- **A destination password saved before `QUERYAPIGATE_SECRET_KEY` was set stayed in plaintext.** Startup encrypts
  literal passwords already in the store, but only connections' were; destinations' are now too.

### Changed
- `password_undecryptable` now says that no listed key decrypts the password, and how to fix it. Each key in
  `QUERYAPIGATE_SECRET_KEY` is validated at startup by its place in the list; the same key twice is refused.

## [0.21.0] - 2026-10-11

Agents that act, governed: an AI agent may write through a saved query its author marked for agents - opening a
ticket by itself, or closing one only once a named person has approved that exact change. Every agent write runs in a
transaction, rolled back past the rows it may change, runs once, and is on record with who asked and who approved.
Experimental.

### Upgrading from 0.20
- **Schema 11** adds one table (`write_requests`); nothing existing changes. A 0.20 instance still running keeps
  working against the upgraded store, but can't be restarted on it - upgrade every instance.

### Added
- **Write tools over MCP, each write approved by a person** (BACKLOG #88,
  [ADR 0008](documentation/adr/0008-mcp-write-tools-with-approval.md), experimental): a saved query its author marks
  `agent_writes` - one INSERT, UPDATE or DELETE, never DDL - is a write tool for callers whose grant allows the write.
  With `"approval"` the first call asks, an administrator with `writes.approve` approves it
  (`/api/v1/write-requests`), and the second call runs it once, with exactly the approved arguments; `"insert"` runs
  an INSERT at once, with an idempotency key. Every agent write runs in a transaction, rolled back past its
  `max_affected_rows`. On PostgreSQL, MySQL, SQLite and DuckDB. The Console's **Approvals** screen (and the link each
  agent is given) shows the SQL and the exact arguments to approve or reject; a `writes_awaiting_approval` alert and
  the Agents screen's write counts follow them. The credential that asked can never approve (`own_write`), an agent
  can't pile up more than 100 waiting writes, and the audit log records the arguments' names and hash - never their
  values. What a write returns (`RETURNING`) is masked like any read. Guide 49. See
  [Write tools](documentation/MCP.md#write-tools-agents-that-write-approved-by-a-person).

## [0.20.1] - 2026-10-11

### Security
- **A saved query that writes could be run over MCP by calling it by name** (0.11.0 to 0.20.0). The MCP server lists
  only read-only saved queries as tools, but a call naming one that writes wasn't refused - so an agent holding a key
  whose grant allowed that write (`{"name": "...", "allow_writes": true}`, with `QUERYAPIGATE_ALLOW_WRITES` on) could
  run it unsupervised, including an older version that writes through `version`. It reached nothing the same key
  couldn't do over REST, but it broke the promise that agents only read. Now a saved query whose SQL writes is refused
  over MCP before it runs (`read_only`), whatever the version and the grant. To check whether it happened: run history
  entries with `"transport": "mcp"` for a saved query that writes.

## [0.20.0] - 2026-10-11

A consolidation release: four features leave the experimental list and come under the compatibility promise -
alerts, remote files through DuckDB, exports to object storage and column masking - on terms now written into the
versioning policy. Getting them there found two dependency minimums that weren't true (sqlglot for masking; DuckDB,
fixed in 0.19.1), and CI now tests against real S3, a real identity provider and the oldest dependencies allowed.

### Upgrading from 0.19
- **`queryapigate[flow]` needs sqlglot 25.2 or later** (was 23). pip upgrades it with QueryAPIGate; an install that
  pins an older one keeps everything but column masking working, and masked callers get `masking_unavailable` saying
  so.
- Nothing else changes: no schema change, and the graduated features work exactly as in 0.19.

### Changed
- **Alerts, remote files through DuckDB, exports to object storage and column masking are stable** - under the
  compatibility promise, no longer experimental: each met the graduation terms now written into the versioning policy
  above (tested for real in CI, security-reviewed, unchanged for a release, gaps closed or stated, tested on the oldest
  dependencies allowed). Each one's documentation says what may still be added: new alert kinds and fields (a client
  should ignore a `kind` it doesn't know); storage types and connection fields; export formats, path placeholders
  and fields; masking actions, and query shapes masking can follow. Their startup warnings, `x-experimental` marks
  and Console tags are gone.
- **Column masking needs sqlglot 25.2 or later**, the first its tracing works on - `queryapigate[flow]` asked for 23,
  and on 23.0 to 25.1 a masked caller's query failed inside the parser (refused, never unmasked). An older sqlglot
  pinned by something else now gets `masking_unavailable`, naming the version and the upgrade.

### Fixed
- **A table function run by a table-restricted key** answered `500` instead of `table_not_allowed` on older sqlglot
  releases (25.2 among them): the check looked for an expression class they don't have. Refused either way.

### Added
- **CI tests against real services and the oldest dependencies:** the integration job runs exports, files on S3, the
  MCP server's OAuth and Console sign-in against SeaweedFS and Keycloak, none allowed to skip; a new job runs the
  DuckDB, masking and table-check tests on Python 3.11 with DuckDB 1.2.0 and sqlglot 25.2.0, the lowest versions
  allowed. Column masking is tested against real PostgreSQL, MySQL and ClickHouse - in their own SQL - on every push.

## [0.19.1] - 2026-10-11

### Fixed
- **DuckDB connections need DuckDB 1.2 or later - and now say so.** `queryapigate[duckdb]` asked for DuckDB 1.0, but
  since 0.15 every DuckDB connection is locked to its `allowed_paths` with settings that arrived in DuckDB 1.2: on 1.0
  and 1.1 every DuckDB connection - files, exports and plain databases alike - failed with *unrecognized
  configuration parameter "allowed_directories"*. It never opened one unlocked. `queryapigate[duckdb]` now requires
  1.2, and an older DuckDB pinned by something else gets `driver_missing`, saying which version is installed and how to
  upgrade. Verified on DuckDB 1.2.0 and 1.5.5, files and exports on S3 included. The Docker image ships a current
  DuckDB and was never affected.

## [0.19.0] - 2026-10-11

The people who govern QueryAPIGate sign in the way agents already do: administrators sign in to the Console through
the organisation's identity provider, with its MFA and policies, and their role comes from the groups they're in
there - someone leaving or moving team changes their access at their next sign-in. Administrators who had tokens keep
their names and history. Experimental.

### Upgrading from 0.18
- **Schema 10** adds two tables (`admin_sessions`, `sso_logins`); nothing existing changes. A 0.18 instance still
  running keeps working against the upgraded store, but can't be restarted on it - upgrade every instance.

### Added
- **Console sign-in through SSO** (BACKLOG #84 Phase 2, [ADR 0007](documentation/adr/0007-console-sign-in-through-sso.md),
  experimental): administrators sign in through the organisation's identity provider (OpenID Connect, the
  authorization code flow with PKCE, run by the server), with their role from their groups at every sign-in
  (`QUERYAPIGATE_SSO_ROLES`) and an administrator created on first sign-in. The session is a `SameSite=Strict`,
  `HttpOnly` cookie that authenticates only together with its CSRF token. Administrators who had tokens are linked at
  their next SSO sign-in (`{"sso": "pending"}`), their role then their groups'; `QUERYAPIGATE_SSO_ONLY` takes personal
  tokens from people who sign in through SSO. The Console offers **Sign in with SSO**, and a new alert,
  `sso_unreachable`, says when sign-in is down. See [Console sign-in through SSO](documentation/API.md#console-sign-in-through-sso)
  and [guide 48](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/how-to/48-sign-in-to-the-console-through-your-identity-provider.md).

### Changed
- **An identity provider's metadata is checked more strictly**, for the MCP server's OAuth too: a document listing
  any plain-HTTP endpoint (keys, authorization, token, sign-out) is refused whole, not only one whose keys are. It is
  fetched again every hour, and the last good one kept while the provider can't be reached.

## [0.18.0] - 2026-10-11

Letting AI agents near your data, governed: hosted AI clients sign in through your identity provider, with tokens
bound to the MCP server; agents are told what tables and columns mean - imported from the database's own comments or
written in the Console - so they write the right SQL; and you can see what each agent did, with an alert when one
suddenly reads far more than usual. All three are experimental.

### Upgrading from 0.17
- **Schema 9** adds one table (`descriptions`); nothing existing changes. A 0.17 instance still running keeps working
  against the upgraded store, but can't be restarted on it - upgrade every instance.
- **The MCP server answers a missing or wrong credential with HTTP `401`**, not a tool error inside a `200` - and
  a rate-limited client with `429`. A client with a valid key or token sees no change.

### Added
- **OAuth for the MCP server** (BACKLOG #85, [ADR 0006](documentation/adr/0006-ai-agents.md),
  [guide 46](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/how-to/46-let-ai-clients-sign-in-through-your-identity-provider.md),
  experimental): hosted
  AI clients sign in through your identity provider. `/mcp` becomes an OAuth 2.1 resource server - Protected Resource
  Metadata at `/.well-known/oauth-protected-resource`, `401`/`403` with `WWW-Authenticate` - accepting tokens that
  name its own URL as their audience (`QUERYAPIGATE_MCP_RESOURCE_URL`) from the provider in
  `QUERYAPIGATE_MCP_AUTH_SERVER`, with a scope and a role (`QUERYAPIGATE_MCP_SCOPE`, `QUERYAPIGATE_MCP_ROLE`,
  `QUERYAPIGATE_MCP_ROLE_CLAIM`). The role's grants and masking rules apply to the agent, whose runs are under
  `jwt:<sub>` - or `mcp:<sub>` when REST trusts another provider, so two people never share a name. REST and MCP
  tokens are kept apart: neither is accepted by the other. See [Sign-in for AI clients](documentation/MCP.md#sign-in-for-ai-clients-oauth).
- **Descriptions** (BACKLOG #86, [ADR 0006](documentation/adr/0006-ai-agents.md), experimental): what each table
  and column means, and a connection's notes (the joins to use, how a metric is worked out) -
  `/api/v1/connections/{name}/descriptions`, written in the Console's schema browser or imported from the database's
  own comments (PostgreSQL, MySQL, ClickHouse, DuckDB). Served with the schema, in MCP's `list_tables` and as a data
  dictionary MCP resource per connection - only for the tables a caller can reach, marking the columns it gets
  masked. A saved query describes its result columns (`columns`), in its MCP tool's output schema.
  See [Descriptions](documentation/API.md#descriptions) and
  [guide 47](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/how-to/47-tell-agents-what-your-data-means-and-see-what-they-did.md).
- **Agent usage** (BACKLOG #87, experimental): `GET /api/v1/usage/agents` and the Console's **Agents** screen - per AI
  agent on the MCP server, its calls, rows read, refusals by code, how often masking applied, the saved queries it ran
  and its most frequent ad-hoc statements, from run history. A new alert, `agent_unusual_volume`, warns when an agent
  reads ten times its usual hourly rows, past `QUERYAPIGATE_ALERT_AGENT_ROWS` (default 10,000). See
  [Agent usage](documentation/API.md#agent-usage).

### Changed
- **The startup warning for an experimental feature** reads `Experimental: <feature> (<what shows it>) - ...`, so it
  reads right for every feature's name ("OAuth for the MCP server are experimental" didn't). Log searches for
  `are experimental` need the new wording.

## [0.17.0] - 2026-10-11

Delivering data to partners and pipelines: a saved query can be exported to a bucket or folder as Parquet, CSV or
NDJSON - incrementally, a file of only what changed each run - and column masking hides personal data from the
callers you hand it to, traced through the SQL so a renamed column doesn't slip past. Both are experimental. A
security review before release keeps exports, connections and the store out of an administrator's reach, and three
fixes reach back into 0.16: Parquet output in steady memory, a stream closed before its first row, and switching keys
in the Console.

### Upgrading from 0.16
Nothing has to change for what you run today; check two things if they apply to you.
- **Schema 8** adds three tables (`destinations`, `exports`, `server_secrets`); nothing existing changes. A 0.16
  instance still running keeps working against the upgraded store, but can't be restarted on it - upgrade every
  instance.
- **A `${VAR}` reference to a `QUERYAPIGATE_*` setting** in a connection is now refused - the connection fails with
  `connection_misconfigured` until it refers to a variable of its own (one that isn't a server setting).
- **A connection that opens the server's own `queryapigate.db`** - by `database`, or a DuckDB `allowed_paths` folder
  containing it - is now refused (`store_not_allowed`).
- **Local-folder export destinations** need `QUERYAPIGATE_EXPORT_DIRS`; object-storage destinations need nothing.

### Added
- **Destinations** (BACKLOG #89, [ADR 0004](documentation/adr/0004-exports-to-object-storage.md),
  [guide 44](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/how-to/44-deliver-a-daily-file-to-a-partners-bucket.md),
  experimental - with saved exports below, exports to object storage): `/api/v1/destinations` - an `s3://`, `gs://` or `r2://` prefix, or a
  local folder, and the credentials to write there, stored like a connection password (masked, `${VAR}`, or encrypted
  with `QUERYAPIGATE_SECRET_KEY`). Every write goes through a DuckDB connection locked to that prefix, so nothing can
  be written elsewhere; `POST /api/v1/destinations/{name}/test` writes a probe object to prove the url, credentials and
  permission together. Owners and admins define destinations (`destinations.write`); every administrator can read them.
  Exports need DuckDB (`queryapigate[duckdb]`, in the Docker image); without it they say so (`exports_unavailable`).
- **`queryapigate export QUERY --to DESTINATION --out PATH`** writes a saved query's full result under a destination
  as Parquet, CSV or NDJSON, through DuckDB: `--out 'orders/{date}/orders_{run}.parquet'` - with `{name}`, `{date}`,
  `{time}`, `{run}` and the query's own parameters (`{region}`), whose values may only be plain names, so none can add
  a folder or climb out. Each run is in history (`transport: "export"`, the destination and the object written).
- **Saved exports** (experimental): `/api/v1/exports` - a saved query, its parameters, a format, a destination and an
  object path, run with `POST /api/v1/exports/{name}/runs` or `queryapigate exports run NAME` (scheduling stays with
  cron, a CronJob or Airflow). **Incremental:** each run binds the largest value of a column it last delivered to a
  parameter (`WHERE updated_at > :since`), so a file holds only what changed; the watermark moves only after the file
  is written, so a failed run is sent again, never lost - and a value the parameter wouldn't accept back is refused
  before anything is written. One run at a time per export (`409 export_running`, a lease that a crashed run can't
  hold for ever). Runs are in history, and the `export_failing` alert fires while an export's newest run failed.
  Owners and admins define exports (`exports.write`); developers may run them (`exports.run`).
- **Column masking** (BACKLOG #63, [ADR 0005](documentation/adr/0005-column-masking.md),
  [guide 45](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/how-to/45-hide-personal-data-from-a-partner-or-an-ai-agent.md),
  experimental): a
  `mask` grant on API keys and roles, and on exports - rules on source columns (`customers.email`, `*.phone`) that
  `hash`, `mask`, `null` or `drop` their values in everything that caller receives: `/q/<name>`, `/execute_sql`, every
  format, streamed exports, MCP tools and exports. Each result column is traced through the SQL to the table columns it
  comes from - aliases, expressions, CTEs, unions and joins don't get around it - and a query that can't be traced is
  refused (`403 mask_unresolvable`) before it runs, never served unmasked. Cached responses are kept per set of rules;
  run history records what was masked; a caller with rules never sees the database's own error text. `GET /catalog`
  tells such a caller which of each query's columns it gets masked, and how. In the Console: rules in the API key, role
  and export forms, and a "masked" tag on keys and roles - and on the Access map - that carry them.
- **Exports and Destinations in the Console** (a new Delivery group): define a destination and test it before saving;
  define an export - query, destination, format, file path, parameters, incremental column - run it with **Run now**,
  and see its runs. Each role sees what it may do: owners and admins everything, developers the Run button, auditors
  the lists.

### Fixed
- **Parquet output now runs in steady memory.** DuckDB wrote Parquet with a thread per core, each buffering its share,
  so memory grew with the result: about 600 MB for 2 million rows and 1.4 GB for 6 million, for `?format=parquet`
  (paged or streamed) and `queryapigate export --format parquet` alike. It now writes on one thread - about 150 MB
  at either size, at the same speed, and rows always keep the query's order.
- **A stream closed before its first row ends its query.** `?stream=true` and `queryapigate export` hand back rows as
  they come; a caller that stopped before reading one - a client gone before the first chunk - left the query counted
  in `queryapigate_active_queries` and its pooled connection checked out until garbage collection.
- **Switching keys in the Console's key box no longer briefly keeps the previous key's role** - the sidebar could
  show screens the new key can't use until something else redrew it.
- **The OpenAPI alert `kind` enum lists every alert** - `break_glass_used` (0.16) and the instance and Redis alerts
  were missing from it. `alerts.KINDS` is now the one list, checked against the docs.

### Security
- **Exports, connections and the store are kept apart from an administrator's reach** (the 0.17 pre-release review,
  now that `admin` is a role below `owner`): a local-folder export destination is off unless the operator sets
  `QUERYAPIGATE_EXPORT_DIRS`, must sit inside a folder listed there, and is never the server's home - so no export can
  write over the store or elsewhere on the host; a connection can't be pointed at the server's own `queryapigate.db`
  (`store_not_allowed`), by `database` or by a DuckDB `allowed_paths` folder containing it; and a `${VAR}` reference
  in a connection or destination can't name one of the server's own `QUERYAPIGATE_*` settings. Object-storage
  destinations are unaffected - their credentials bound them.

## [0.16.0] - 2026-10-06

Named administrators replace the one shared admin key - each person or pipeline with a role, their own tokens, and an
audit log that says who changed what - which completes the last change to the management API before the 1.0 freeze.
Any query can come back as Parquet, a folder or bucket of files becomes a set of tables by itself, and the read-only
guard closes a gap that let DuckDB, H2 and JDBC connections run writes hidden behind `EXPLAIN ANALYZE` or a CTE.

### Upgrading from 0.15
Nothing has to change: no route, setting or stored field is removed, and the shared key keeps working.
- **Schema 7** adds two tables (`administrators`, `admin_tokens`); nothing existing changes. A 0.15 instance still
  running keeps working against the upgraded store, but can't be restarted on it - upgrade every instance.
- **`QUERYAPIGATE_API_KEY` keeps working, as an owner.** To give people their own access and retire the shared key,
  follow [guide 43](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/how-to/43-give-your-team-their-own-admin-access.md).
  Once a named owner exists, each use of the shared key is logged and raises the `break_glass_used` alert. The startup
  warning about scoped keys without `QUERYAPIGATE_API_KEY` is no longer logged once an active owner exists.
- **Read-only is stricter** (see Security): with writes off, a statement that hides a write behind `WITH`, `EXPLAIN
  ANALYZE` or `SELECT ... INTO` is now refused with `read_only` on every database type - on PostgreSQL, MySQL,
  ClickHouse and SQLite those already failed, in the database. A key's `allowed_write_ops` counts those writes too.
- **The Management API checks roles**: a caller that isn't an administrator gets `403 admin_only`, as before; an
  administrator whose role lacks an operation's capability gets `403 role_forbidden`. The shared key is an owner, so
  existing scripts are unaffected.

### Added
- **Parquet output** (BACKLOG #81): `?format=parquet` on any query and any database type - paged or streamed - and
  `queryapigate export --format parquet`. Written by DuckDB (no new dependency; in the Docker image), with column
  types taken from the values - decimals stay exact, dates and timestamps stay typed. The Console's format pickers
  offer it and download the file. On the example APIs' 20,000-row export: 513 KB of Parquet against 2.2 MB of CSV.
  Arrow IPC output is still open: it would need `pyarrow`, a large new dependency.
- **Automatic views over files** (DuckDB connections, experimental with remote files): `"auto_views": true` makes a
  view of each file and each subfolder under the connection's allowed folders and bucket prefixes - `orders.parquet`
  becomes `orders`; a folder of Parquet files, with hive-style `key=value/` partitions, becomes one view over all of
  them. Views in `views` take precedence; a file that can't be read is skipped with a warning.
- **A "Files (Parquet, CSV, JSON)" connection type in the Console**: a DuckDB connection with no database file of its
  own and automatic views on - choose the folders or buckets it may read, and each file is a table to query.
- **Named administrators with roles** (BACKLOG #84 Phase 1, [ADR 0003](documentation/adr/0003-named-administrators.md),
  [guide 43](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/how-to/43-give-your-team-their-own-admin-access.md)): administrators - people or pipelines - each with a role (`owner`, `admin`, `developer`, `auditor`)
  and their own admin tokens (`qagadm_...`, sent as `X-API-Key`, stored only as hashes, with an expiry). Every
  `/api/v1` operation needs a capability its caller's role holds (`403 role_forbidden` otherwise); `GET /api/v1/me`
  says who the caller is. `queryapigate admins create|token|list` creates the first owner without a server. Audit
  entries gain `via` (`token`, `break-glass`, `open`, `cli`, `startup`). `QUERYAPIGATE_API_KEY` keeps working as an
  owner - the break-glass key: once a named owner exists, each use is logged and raises a `break_glass_used` alert.
  Authentication stays required while any administrator exists, even with `QUERYAPIGATE_API_KEY` unset. Moving a
  query or merging collections in a way that gives an API key or role new reach takes an owner or admin
  (`access.write`) - a developer organises queries but doesn't decide who can call them.
  `/api/v1/administrators` manages administrators and tokens (owners; anyone manages their own tokens). In the
  Console: an **Administrators** screen (owners see everyone; others, their own account and tokens), "Signed in as
  alice · Developer" in the sidebar, and screens and actions a role can't use are hidden.

### Security
- **Writes hidden behind a read-only statement are refused.** The read-only guard classified a statement by its first
  keyword, so with writes switched off a DuckDB connection - opened read-write, with the guard as its only lock - ran
  `EXPLAIN ANALYZE DELETE FROM t` (emptying the table) and `WITH x AS (...) INSERT ...`; H2 and JDBC connections are
  in the same position. The guard now also finds writable CTEs, the statement `EXPLAIN ANALYZE` runs, and
  `SELECT ... INTO`, and refuses them (`read_only`), ignoring literals, quoted names and comments. A key's
  `allowed_write_ops` is checked against those too: a key limited to `insert` could delete through a writable CTE on
  PostgreSQL. Response caching and the MCP tool list use the same rule. PostgreSQL, MySQL, ClickHouse and SQLite
  refused these writes already, in their read-only sessions.

## [0.15.0] - 2026-10-04

Several instances become a supported deployment shape - shared rate limits, an instance list, a rolling-upgrade rule
and test, and reference deployments for Compose and Kubernetes - files in buckets and on the web can be published
through DuckDB, and what 0.14 deprecated is removed before the 1.0 freeze.

### Upgrading from 0.14
Read these first.
- **Breaking: `POST /execute_sql_from_file` and `POST /execute_sql_with_parameters_from_file` are removed** - call
  `/q/{name}` (GET or POST) instead. 0.14 marked them deprecated, with `Deprecation` headers and a log line naming each
  caller's key.
- **Breaking: the pre-SQLite JSON files are no longer imported.** A home last run by **0.10 or older** - 0.14's notice
  said 0.9, but 0.10 still kept saved queries, keys, roles and the audit log in those files - must be started once by
  0.14 first. 0.15 refuses to start on one, saying so, rather than start without its data.
- **Breaking: a DuckDB connection reads only the files its new `allowed_paths` lists** - none by default. List the
  folders and files your DuckDB queries read, as absolute paths.
- **The store is upgraded to schema 6** on first start (a new `instances` table) - additive, so a 0.14 instance still
  running against it keeps working during a rolling upgrade. Back up first anyway.
- **Running several instances?** Give them all the same `QUERYAPIGATE_REDIS_URL`: rate limits are now shared through
  it, and an alert says when some instances run without it. See DEPLOYMENT.md's *Scaling out*.
- **The Docker image is about 90 MB larger** - it now includes every optional feature the docs describe.

### Breaking
- **`POST /execute_sql_from_file` and `POST /execute_sql_with_parameters_from_file` are removed** (deprecated in
  0.14, BACKLOG #67) - `/q/{name}` runs the same saved query by name.
- **The pre-SQLite JSON files are no longer imported** (deprecated in 0.14). A home with data only in them (0.10 or
  older) is refused at startup with what to do: start 0.14 on it once.
- **A DuckDB connection reads only the files its `allowed_paths` lists** (BACKLOG #75). Before, any key that could
  run SQL on a DuckDB connection could read any file the server's user could - `read_csv('/etc/passwd')` included.
  Now a connection with no `allowed_paths` reads no files at all; list the folders and files a connection's queries
  use (absolute paths), e.g. `"allowed_paths": ["/data/sales/"]`. DuckDB itself enforces it: each connection's
  configuration is locked as it opens. A refused path answers `403 path_not_allowed`.

### Added
- **Several instances, supported** (BACKLOG #56, #70): one instance, or several on a PostgreSQL store with Redis,
  are both supported deployment shapes. `deploy/scale-out/` is a reference Compose deployment (three instances,
  PostgreSQL, Redis, the events server, Caddy) and `deploy/kubernetes/` the same on Kubernetes (Deployment, probes,
  zero-downtime rolling updates, a PodDisruptionBudget, Ingress) - both run end to end, including failover and a
  rolling update under traffic. DEPLOYMENT.md's "Why one worker" became *Scaling out*; how-to guide 42 walks
  through both.
- **Which instances are running** (BACKLOG #58): `GET /api/v1/instances` lists every process using the store - role,
  host, version, whether it shares limits through Redis - and two new alerts, also logged at startup, flag several
  instances without Redis (`instances_not_shared`) and instances on different versions (`instances_versions_differ`).
  The store moves to schema 6 for it (a new `instances` table), upgraded automatically on first start.
- **Rolling upgrades, as a rule and a test** (BACKLOG #58): within 1.x a release changes the store only additively,
  so the previous release keeps serving on a store the new one has upgraded - DEPLOYMENT.md's *Rolling upgrades*.
  `tests/test_rolling_upgrade.py` runs the previous release from PyPI beside the current code on one store in CI.
- **One instance runs the history retention sweep**: on a shared PostgreSQL store the instances take turns through an
  advisory lock rather than all deleting the same rows. The Grafana dashboard's stat panels sum across instances;
  DEPLOYMENT.md says how many PostgreSQL connections several instances need.
- **Rate limits shared across instances** (BACKLOG #55). With `QUERYAPIGATE_REDIS_URL` set, the server-wide limit
  per client address and every key's (and signed-in user's) `rate_limit` are counted in Redis - an atomic token
  bucket on Redis's own clock - so every instance, and `queryapigate mcp` and `events`, enforce one budget instead of
  one each. If Redis fails, each process falls back to counting on its own (limits stay enforced, never lifted),
  tries Redis again after a few seconds rather than on every request, logs it at most once a minute, counts it in
  `queryapigate_rate_limit_fallbacks_total`, and raises the new `rate_limits_not_shared` alert. Settings shows where
  limits are counted.
- **A multi-server test** (BACKLOG #57): CI starts three servers and an events server on one PostgreSQL store and
  one Redis, and checks that a change made through one - keys, query versions, connections, rate limits, the cache,
  history, the audit log, live events - takes effect on the others.
- **Files on S3, GCS, R2 and the web as an API** (BACKLOG #75, experimental): a DuckDB connection's `allowed_paths`
  takes `s3://`, `gs://` and `r2://` prefixes and `http(s)://` files; `user`/`password` carry the access key (masked,
  encrypted and `${VAR}`-capable like any password), with `region`, `endpoint`, `url_style`, `use_ssl` and `storage`
  beside them. `database: ":memory:"` makes a connection of nothing but files, and `views` names them - for the schema
  browser, saved queries and `allowed_tables`. The Console's connection form has a section for all of it, the Docker
  image includes DuckDB's `httpfs` extension, and how-to guide 41 walks through publishing a bucket.

### Fixed
- **A table-restricted key can't read data through a table function.** `allowed_tables` checked the tables a statement
  names, but `read_parquet(...)`, `read_csv(...)`, `glob(...)` or ClickHouse's `url(...)` name none, so they passed.
  They're now refused for such a key (`table_not_allowed`); pure generators (`range`, `numbers`, `generate_series`,
  `unnest`) still work.

## [0.14.0] - 2026-10-04

The groundwork for 1.0's promise: a deprecation policy and an "experimental" label for what's outside it, a database
support matrix with tiers, backups you can restore, Python 3.11 or newer - and all 40 how-to guides, whose writing
found and fixed the access-control and packaging gaps listed under *Fixed*.

### Upgrading from 0.13
Read these first.
- **Breaking: Python 3.11 or newer.** On 3.9 or 3.10, pip keeps installing 0.13.0. The Docker image is unaffected.
- **No store migration**: the store stays at schema 5. Back up first anyway ([how](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/how-to/33-back-up-and-restore.md)).
- **Coming from 0.9 or older?** Start 0.14 once before going further: 0.15.0 may stop importing the pre-SQLite JSON
  files (*Deprecated*, below).
- **Callers of `/execute_sql_from_file` or `/execute_sql_with_parameters_from_file`** should move to `/q/{name}`
  before 0.15.0. Each process logs the first such call with the key that made it.
- **A key with `"allowed_ips": []` now works from no address** (it used to work from any). Find any with
  `GET /api/v1/api-keys` and set them to `null`, or to real addresses.
- **A table-restricted key's schema shows only its tables** - a client that listed the rest no longer sees them.

### Breaking
- **Python 3.11 or newer is required** (was 3.9). Python 3.9 reached its end of life in October 2025 and 3.10
  reaches its own this month, so neither is carried into 1.0. pip on 3.9 or 3.10 keeps installing 0.13.0, the last
  release for them. The Docker image already runs Python 3.12 and is unaffected.

### Deprecated
- **`POST /execute_sql_from_file` and `POST /execute_sql_with_parameters_from_file`** (BACKLOG #67), in favour of
  `GET` or `POST /q/{name}`, which runs the same saved query by name. They work as before, now with `Deprecation`
  and `Link` headers, `deprecated: true` in `/openapi.json` and a log warning on first use; 0.15.0 may remove them.
- **Importing the pre-SQLite JSON files** (`db_connections.json`, `saved_sql/`, `api_keys.json`, `roles.json`,
  `audit_log.json`) on first start. A home last run by 0.9 or older should be started once by 0.14 before 0.15.0,
  which may stop reading them; stores from 0.10 on never wrote them. An import now logs a warning.

### Added
- **All 40 how-to guides are written** - the 26 that were planned topics, from connecting via generic JDBC to
  deciding how to expose QueryAPIGate: partner keys, roles, table and IP restrictions, rate limits, expiry, the audit
  log, encryption, reverse proxies, MCP's ad-hoc tools and structured results, private live feeds, scheduled
  exports, Docker, backups, upgrades, Prometheus and Grafana, logs, the connection pool, CORS and the threat model.
  Every command and response in them was run against a real server, and the Console's Help lists them all.
  `tests/test_howto.py` checks every guide is listed and every link in them resolves.
- **A deprecation policy** (BACKLOG #67), under *Versioning and compatibility* above: what is deprecated, how
  you'll find out, and how long it keeps working before it may go.
- **A database support matrix** (BACKLOG #66) in DATABASE_CONNECTION_CONFIGURATION.md: what each of the eight types
  supports - read-only enforcement, time limits, `allowed_tables`, schema browsing, streaming, MCP and more - and
  which are **tier 1** (PostgreSQL, MySQL, SQLite, DuckDB, ClickHouse: every feature, tested against a real server
  in CI, covered by the compatibility promise) or experimental (H2, JDBC, MongoDB), with what an experimental type
  needs to become tier 1. The table is generated from the code that implements each feature
  (`queryapigate/databases.py`) and checked by a test, so it can't claim more than the code does.
- **Backups you can restore, documented and tested** (BACKLOG #71). `queryapigate backup FILE` copies the SQLite
  store consistently while the server runs (SQLite's backup API, so nothing still in the write-ahead log is
  missed); on a PostgreSQL store it prints the `pg_dump` command for its schema. DEPLOYMENT.md's *Backups and
  restores* covers both stores - backing up while serving, restoring, and what a backup doesn't contain:
  `QUERYAPIGATE_SECRET_KEY` (without it, encrypted connection passwords can't be read), the admin key, and
  variables connections refer to. `tests/test_backup_restore.py` fills a store, backs it up, loses it, restores
  it and checks the admin API sees exactly what it saw, on SQLite and PostgreSQL alike.
- **PostgreSQL upgrade fixtures** (the rest of BACKLOG #65): stores built by 0.12.0 and 0.13.0 on PostgreSQL, as
  `pg_dump` output, are restored and started under the current code in the PostgreSQL CI job.
- **An "experimental" label** (BACKLOG #64) for features outside the compatibility promise - they may change in
  any minor release, always noted here (see *Versioning and compatibility* above). Today: live events (`GET
  /events`, `queryapigate events`), H2, JDBC and MongoDB connections, and alerts (`GET /api/v1/alerts`). Each is
  marked in its docs, with `x-experimental: true` in `/openapi.json`, with a tag in the Console (its Settings rows,
  the database-type picker, the Alerts screen), and with a warning in the log at startup while one is in use.

### Changed
- **The one-command demo (`docker compose up`) seeds itself through the Management API** (`demo/seed.sh`) instead of
  copying the pre-SQLite JSON files that 0.14 deprecates.

### Fixed
- **Kubernetes Services named like QueryAPIGate's settings no longer break it.** Kubernetes injects
  `<SERVICE>_PORT=tcp://...` into every pod for each Service, so a Service named `queryapigate-events` set
  `QUERYAPIGATE_EVENTS_PORT` and the server refused to start (and a Service named `queryapigate` broke every CLI
  command, which read `QUERYAPIGATE_PORT` while parsing arguments). Such a value is now ignored with a warning
  naming the cause.
- **A table-restricted key sees only its tables in the schema.** `allowed_tables` refused statements on other
  tables, but the schema browser and MCP's `list_tables` still listed every table and column on the connection, and
  `table_ddl` showed any table's `CREATE TABLE`. Now the others are left out (and a foreign key pointing at one shows
  as none), and `table_ddl` answers a forbidden table with the same `404` as a missing one.
- **An empty `allowed_ips` list allows no address.** `"allowed_ips": []` on a key or role used to mean "no
  restriction" - failing open, unlike an empty `allowed_tables` or `allowed_write_ops`, which allow nothing. `null`
  is still how to say "any address"; the Console sends `null` for an empty field, as before.
- **`queryapigate events` streams can be read by every HTTP client.** Its responses were delimited only by the
  connection closing, so some clients - Python's `requests`, reading with `iter_content(chunk_size=None)` - received
  nothing until the stream ended. They now use HTTP/1.1 chunked encoding, as `GET /events` on the main server
  already did.
- **The Docker image includes every optional feature the documentation describes**: DuckDB (a tier 1 database),
  `allowed_tables` (sqlglot - a table-restricted key's queries failed with `500` in the image), the Redis response
  cache, `queryapigate mcp` and MongoDB. Only H2 and generic JDBC still need the `-h2` variant, for its Java runtime.
  The image is about 90 MB larger.
- **No "experimental" warning for an inactive connection.** `queryapigate init` seeds an inactive H2 template, so
  every fresh server warned about H2 connections nobody used.
- **Browser pages can read every header the API sends them.** CORS responses now also expose
  `X-RateLimit-Key-Limit`/`-Remaining` (a key's own rate limit), `X-Cache`, `ETag`, `Deprecation` and `Link` - before,
  a page's JavaScript couldn't see them.
- **A server with no API key says so in its log at startup**, wherever it runs - before, only `queryapigate serve`
  on a non-local address warned, so a Docker container started without `QUERYAPIGATE_API_KEY` was open with nothing
  in its log to say so (the Console's `open_server` alert did).
- **SQL sent to a MongoDB connection is refused clearly** - `400 wrong_connection_type`, pointing to
  `/execute_mongo` - on `/execute_sql`, streamed exports and MCP's `execute_sql`. It used to fail inside the driver
  lookup, as a `500` whose detail was just `'mongo'`.

## [0.13.0] - 2026-10-04

The Console replaces `/ui`, the Management API (`/api/v1`) replaces the unversioned management routes, and every
front door - REST, MCP, live events - now shares one set of rules, one error format and one run history.

### Upgrading from 0.12
Read these first; everything else in this release is additive.
- **Breaking: the unversioned management routes are removed - use `/api/v1`.** The table under *Removed* maps each
  one to its successor. Runtime routes (`/q/<name>`, `/execute_sql`, `/catalog`, `/events`, ...) are unchanged.
- **`/ui` redirects to `/console`**, the new admin UI. It needs a browser from 2024 or later (Chrome and Edge 128,
  Firefox 126, Safari 17.5).
- **New versions of a saved query are drafts** until published; only the published version is served. Existing
  queries are published at their newest version on first start, so nothing callers see changes.
- **MCP calls count against rate limits** (`QUERYAPIGATE_RATE_LIMIT` and the key's own `rate_limit`) like REST
  requests: an agent calling faster than its key allows now gets a rate-limit error.
- **A saved query's own `LIMIT` is respected**: paging no longer overrides it.
- **An unreachable database answers 502 `connection_failed`**, not 500 `query_failed`.
- **Error bodies gain `code` and `request_id`** beside `error` - additive; branch on `code` from now on.
- **The store is upgraded to schema 5 on first start** - back up `QUERYAPIGATE_HOME` (or the PostgreSQL store) first.
  Startup now refuses a store with a missing column, or one a newer release already upgraded, with a message saying
  what to do.

### Added
- **QueryAPIGate Console, at `/console` - the admin UI.** It replaces `/ui`
  ([ADR 0001](documentation/adr/0001-console-and-management-api.md)). Built with React + TypeScript in `frontend/`,
  it is a client of the public JSON API only, and looks exactly like `/ui`: the same stylesheet, sidebar, header,
  search and API key panel (the key is shared with `/docs` in the same browser tab). It has every screen `/ui` had
  (below). The wheel and the Docker image
  include the built Console, so running it needs no Node.js; a source checkout without a build shows how to build it.
  The Console is served with a strict Content-Security-Policy, and loading its static files doesn't count toward
  `QUERYAPIGATE_RATE_LIMIT`.
- `GET /health`'s response is now described in the OpenAPI document (`status`, `version`).
- **Drafts and publishing for saved queries.** Each query now has at most one published version, which is the only
  one served (`/q/<name>`, MCP, catalog, `/openapi.json`, Postman, bundle export, `queryapigate export`). Newer
  versions are drafts: only the admin key can run them, by number, to test before publishing; other keys get the
  same 404 as for a version that doesn't exist. Rolling back means publishing an older version. `GET /list_files`
  shows `published_version`. See [Drafts and publishing](documentation/API.md#drafts-and-publishing).

- **Management API v1: `/api/v1/queries`** ([ADR 0001](documentation/adr/0001-console-and-management-api.md),
  BACKLOG #72). A versioned, resource-oriented interface for saved queries:
  - list, filter, create, add a version (a draft unless published), publish, roll back, unpublish and delete;
  - change a query's collection or a version's cache TTL;
  - page through a query's run history;
  - validate a definition without saving it.

  Also `GET /api/v1/connections` and `GET /api/v1/connections/{name}/schema`. Every error carries a stable `code`
  and the `request_id`; edits honour `If-Match` against the query's `ETag` (412 when it changed meanwhile); every
  request and response is described in full in `/openapi.json`, and the tests validate real responses against it.
  See [Management API (v1)](documentation/API.md#management-api-v1).
- **The Console's API Repository** (`/console/queries`), rebuilt on the Management API and **identical in look and
  feel to the classic one**. The Console now uses the classic UI's own stylesheet, sidebar, header, Ctrl K search,
  API key panel, drawers and toasts (ADR 0001, "Visual parity"). What it has:
  - the list (filter, Type/Host/Database filters, Collections/Queries, Postman, Rename);
  - the detail panel: versions, meta, stat tiles, the requests-per-day chart, and the Run, SQL, History, Curl,
    API Keys, Roles, Access, Cache, Metrics and CLI tabs;
  - the New API, New version, Move, New collection and Rename collection drawers.

  Drafts and publishing appear in the classic style: a Published / Draft / Previous pill; Publish, Roll back to and
  Unpublish buttons; and Save as draft next to Save in the drawer (Save still publishes at once, as before). The SQL
  editor is CodeMirror with completion that knows the connection's tables and columns, styled as the classic editor.
  The API Repository loads on first visit, so the shell stays light.

- **The Console's API Designer** (`/console/designer`), rebuilt at visual parity with the classic one. It has:
  - the Type → Host → Connection → Database picker, with the connection's live usage;
  - the SQL editor (CodeMirror, with completion that knows the dialect, tables and columns), run with Ctrl+Enter;
  - Explain, and double-click a `column = literal` to Parameterize it;
  - the sidebar's Recent Queries (shared with `/ui` in the same tab), Settings (bound parameters, page, page size,
    timeout) and Schema, behind the draggable splitter;
  - the schema browser's starter query, preview, `CREATE TABLE` and "used by" drawers;
  - the quick-stats strip, the results panel (with Copy as curl and Chart), and Save as New API (with Save as draft).

  The API Repository drawer's schema browser regains its 👁 "preview in API Designer". The Designer keeps its query
  while you visit other screens, as the classic tab always did. `GET /api/v1/connections/{name}/schema` gains
  `?database=` for the Database picker.

- **Management API: `/api/v1/connections`**, complete:
  - list (now with user, timestamps and live usage), create, get, change and delete (a reason is required, and
    kept in the audit log);
  - the deleted list, test a connection, and list a server's databases.

  `PATCH` merges: fields it doesn't mention are kept, driver options included. (The legacy `PATCH /connections`
  replaces the whole record, so a form save could drop options and the `example` flag.) Passwords are masked in
  every response and audited only as "changed"; edits honour `If-Match`. A connection that can't be reached is a 502
  `connection_failed` with the driver's (redacted) message in `detail`.
- **The Console's Connections screen** (`/console/connections`), at visual parity with the classic one. It has the
  All / Active / Inactive / Deleted segments, the filter, the table with live usage, and the New / Edit drawer
  (password reveal, Load databases…, Test connection). Delete asks for a reason and the name typed back. Query opens
  the API Designer on that connection.
- **Management API: `/api/v1/api-keys` and `/api/v1/roles`**: list, create, get, change and delete. A new key's
  secret is in the create response only (sent with `Cache-Control: no-store`), never in a read or the audit log. A
  key can be created from a role, and a role reports how many keys were created from it. Edits honour `If-Match`;
  `null` clears an optional field, and `active: false` revokes a key.
- **The Console's API keys and Roles screens** (`/console/api-keys`, `/console/roles`), at visual parity with the
  classic ones: both tables, the key drawer (Create from a role, the grant fields, Expires, Active), the one-time
  secret reveal, the role drawer and "New key from this". The API Repository's API Keys and Roles tabs now edit in
  the Console.
- **Management API: `/api/v1/history` and `/api/v1/audit`.** The first searches every saved query's runs, with
  the same filters and cursor paging as `GET /history`. The second lists administrative changes newest first, with
  filters, and says how many entries are stored and how many the log keeps.
- **The Console's Metrics and Audit log screens** (`/console/metrics`, `/console/audit-log`), at visual parity with
  the classic ones. Metrics has the stat tiles (the two pool tiles live every 2 s), requests by status, queries by
  connection and the per-connection table. Audit log has the action filter, the text filter, Refresh and Export.
- **Management API: `/api/v1/settings`, `/api/v1/mcp/status` and `/api/v1/mcp/tools`**: the server's configuration
  by section (secrets never exported), the MCP port check, and the tools an MCP client sees.
- **The Console's Settings screen** (`/console/settings`), at visual parity with the classic one: every section,
  Copy as .env, the MCP section's Check now and Tools, and the Interface preferences (theme, table density, default
  result format). A preference changed in either UI applies to both, and now takes effect in the Console at once.

- **The Console now has every screen of the classic UI.** Home (now the screen it opens on), Caching, the Access
  map and Help join the others, at visual parity:
  - Home: the stat tiles, System health, Recent activity, Quick actions, and Recent API requests / Slowest queries,
    kept live from `GET /events`;
  - Caching: the cache's numbers, the queries with a `cache_ttl`, and the live cache entries, each viewable as a caller
    receives it;
  - Access map: every query against every key and role, with the connection → table drill-down, filters, search
    and sortable columns, and each query's details;
  - Help: the quick reference and the docs browser (this project's markdown, read from GitHub at the running version).

  The Console no longer sends you to `/ui` for anything.
- **Management API:** `/api/v1/cache/entries` (list, read, evict, clear); `GET
  /api/v1/queries/{name}/versions/{version}/flow` (the tables and joins a version touches - the successor of
  `GET /query_flow`); and on saved queries, `created_at`, `last_used_at` and `cache_ttl` in summaries and
  `run_count` per version.

- **Management API:** `/api/v1/collections` (list, rename, Postman export) and `/api/v1/examples` (status, install,
  remove). The Console now calls only the Management API and the runtime routes.
- **End-to-end tests of the Console** (`frontend/e2e/`, Playwright): every screen and the main flows, in Chromium
  against a real server, in CI on every push.
- **Ad-hoc SQL runs are recorded in run history and live events** (BACKLOG #62): `POST /execute_sql`,
  `POST /execute_mongo` and MCP's `execute_sql`, with the caller, `transport` (`rest`/`mcp`), connection, SQL,
  parameter names (never values), rows, duration and status. `GET /api/v1/history?kind=saved|adhoc` separates them;
  live events carry them as `adhoc_execution`; the Console's Home shows them. New settings:
  `QUERYAPIGATE_HISTORY_ADHOC_LIMIT` (default 1000) and `QUERYAPIGATE_HISTORY_ADHOC_SQL` (`text`, `hash` or `none`).
  Saved-query runs now record `transport` too. See [Ad-hoc runs](documentation/API.md#ad-hoc-runs).
- **`queryapigate export` runs are recorded in run history**, by `key_name` `cli`. The CLI stays outside grants
  and rate limits: it is a local operator with the store's files already in reach.
- **A light/dark switch in the Console's top right corner.** It sets the same Theme preference as Settings ›
  Appearance (which is where to go back to following the operating system), and is remembered in this browser.
- **A font size preference in the Console: Small, Medium (the default) or Large**, in the top right corner beside the
  light/dark switch and under Settings › Appearance (the section was called Interface). It scales the whole page, text and spacing together, and is remembered in this
  browser.
- **Times in the Console follow the viewer.** Settings › Appearance › Time zone shows every timestamp in this
  computer's time zone (the default), UTC, or the server's; Time format shows them as a date and time or as "5 min
  ago". Hovering any time shows it exactly, with its zone. Before, timestamps were the server's local time with no
  zone, wrong by the difference for anyone elsewhere. `GET /health` now reports the server's zone (`time_zone`,
  `utc_offset`) so clients can convert too.
- **Settings › Editor & results** (with Default result format, moved from Appearance): rows per page to start with,
  NULL shown as `NULL` or blank, thousands separators in result grids, and line wrapping and line numbers in the SQL
  editor. **Settings › Appearance › Reduce motion** turns off sliding drawers and pulsing indicators, and is on
  whenever the operating system asks for less motion.
- **Help in the Console:** How-to guides have a tab of their own, next to Docs; both show an "On this page" list of the
  open page's sections, marking the one being read, and a doc's own links to its sections now work. Opening Help
  collapses the sidebar for room (your saved sidebar choice is kept for every other screen).
- **Alerts: what needs attention now.** `GET /api/v1/alerts` lists live conditions, most severe first: a server with
  no API key, failing connections, keys expired, expiring within 7 days or long unused, saved queries failing often,
  timing out or typically slow, keys and clients that keep hitting their rate limit, and run history that can't be
  written. Each says what to do and where, and clears by itself once its cause does. The Console has an Alerts
  screen (Observability) with All and a tab per check (`/alerts/slow-queries`, ...), each saying what it watches, a bell with a count in its header, and Home's System health now shows the same list.
  Dismissing an alert hides it in that browser until it clears. New settings `QUERYAPIGATE_ALERT_ERROR_RATE` and
  `QUERYAPIGATE_ALERT_KEY_UNUSED_DAYS`; a query is slow by `QUERYAPIGATE_SLOW_QUERY_THRESHOLD`. See
  [Alerts](documentation/API.md#alerts).
- **New screenshots** of every Console screen in the README, including Alerts, Access map, Settings, Help and the dark
  theme - taken by `frontend/scripts/screenshots.mjs` (`npm run screenshots`) from a seeded throwaway server.
- **`queryapigate mcp` serves its own `GET /metrics` and `GET /health`** beside `/mcp`.
- **One error format everywhere** (BACKLOG #69). Every error response - the runtime routes (`/q/<name>`,
  `/execute_sql`, `/catalog`, ...) as well as `/api/v1` and `queryapigate events` - now carries a stable `code` and
  the `request_id`, beside the existing `error` and extras. MCP tool errors carry `error` and `code` in
  `structuredContent`. Failed runs keep their `code` in run history. Every code is listed in
  [Errors](documentation/API.md#errors) and covered by the compatibility promise; a test fails if a code is raised
  but undocumented, or documented but never raised. Additive for the runtime routes: `error`, `detail`, `errors`
  and the HTTP statuses are unchanged.
- **The upgrade guarantee is tested** (BACKLOG #65): stores built by released 0.7.1, 0.8.0, 0.9.0, 0.10.0, 0.11.0
  and 0.12.0, and by this release, through their own APIs (`tests/fixtures/stores/`) are started under the current code on every CI run,
  which must read back their connections, queries and versions, run history, keys and roles, and run the query.
  Each release adds its fixture.
- **[ADR 0002](documentation/adr/0002-event-ids.md): event ids** (BACKLOG #59, the storage decision). Event ids
  stay history row ids for now and are documented as opaque, increasing integers; a future event log continues
  their sequence, so a client's `Last-Event-ID` survives that upgrade.

### Changed
- **MCP calls are governed exactly like REST requests** (BACKLOG #61). They now count against
  `QUERYAPIGATE_RATE_LIMIT` (checked before authentication) and the caller's own `rate_limit` grant, appear in
  `/metrics` (method `MCP`, endpoint `mcp.saved_query`, `mcp.execute_sql` or `mcp.list_tables`), and accept a
  signed-in user's `Authorization: Bearer` token, so `from_claim` queries work for an agent acting for a user.
  REST, MCP and `queryapigate events` share one implementation (`queryapigate/governance.py`). **Behaviour change:**
  an agent that called faster than its key's `rate_limit` now gets a rate-limit error. See
  [Governance](documentation/MCP.md#governance-the-same-rules-as-rest).
- **More specific `/api/v1` error codes.** Some errors that had only their status's code now have their own:
  `admin_only` (was `forbidden`), `invalid_body` for an invalid query definition and `invalid_filter` for a bad
  history/audit filter (both were `invalid_request`), `connection_not_found`, `version_not_found` and others.
  Creating an API key, role or collection whose name is taken is always `409`.
- **The metadata store moves to schema 5**, on first start, from a store any earlier release created:
  - schema 4 adds `saved_queries.published_version`: every existing query is published at its newest version, so
    nothing a caller sees changes. Deleting a query's published version publishes the newest *older* one, never a
    draft;
  - schema 5 lets `execution_history` hold ad-hoc runs (`query_name` and `version` may be NULL). On SQLite the table
    is rebuilt, keeping every run and its id (live events' resume ids).

  Back up first: going back to 0.12 on an upgraded store is not supported. `migrate-to-postgres` needs a store at
  schema 5 - start this version on it once.
- **Startup refuses a store it can't safely run on**, with a message naming the problem and what to do, instead of
  starting and failing on first use: a table missing a column this version needs, or a store a newer release has
  already upgraded (which used to have its schema version silently written back down).
- **The Console's Connections table shows Edit and Delete as icons** (a pencil, and a red trash can), each with a
  tooltip naming the connection, leaving room for more columns.
- **`/ui` redirects to `/console`.** The hand-written admin page it served (`queryapigate/ui.py`) is removed; the
  Console has every screen it had, and looks the same.

### Removed
- **Breaking: the unversioned management routes are gone; use `/api/v1`.** Every one had a Management API successor
  first, and nothing in QueryAPIGate calls them any more; removing them before 1.0 keeps them out of the 1.x
  contract. The runtime routes (`/q/<name>`, `/execute_sql`, `/execute_mongo`, `/execute_sql_from_file`, `/catalog`,
  `/events`, `/metrics`, `/health`, `/openapi.json`) and schema browsing (`/connections/<name>/schema`, `/table_ddl`)
  are unchanged.

  | Removed | Use instead |
  |---|---|
  | `GET /list_files`, `GET /view_file_content`, `PATCH /save_sql_to_file`, `DELETE /saved_sql/{name}`, `PUT /saved_sql/{name}/collection`, `PUT /saved_sql/{name}/cache_ttl`, `GET /query_flow` | `/api/v1/queries` and its versions (a new version is a draft unless saved with `"publish": true`) |
  | `GET`/`PATCH /connections`, `DELETE /connections/{name}`, `POST /connections/test`, `POST /connections/databases` | `/api/v1/connections` |
  | `/api_keys`, `/roles` | `/api/v1/api-keys`, `/api/v1/roles` (a new key's secret is `secret`, not `key`) |
  | `GET /history`, `GET /audit_log` | `/api/v1/history`, `/api/v1/audit` (`items`, not `entries`) |
  | `GET /settings`, `/settings/mcp_status`, `/settings/mcp_tools` | `/api/v1/settings`, `/api/v1/mcp/status`, `/api/v1/mcp/tools` |
  | `/cache/entries`, `/collections`, `/examples` | `/api/v1/cache/entries`, `/api/v1/collections`, `/api/v1/examples` |

  See [Removed routes](documentation/API.md#removed-routes). The Postman collection in `documentation/`, the
  how-to guides and the benchmarks use the new routes.

### Fixed
- **Creating a saved query in a collection through `/api/v1` now records the collection in the audit log**, as the
  old save route did - filing a query there grants it to every key on that collection.
- **Creating a connection through `/api/v1` now closes idle pooled connections**, as changing or deleting one does,
  so a name used before (deleted, then re-created) never reuses the old connection's sockets.
- **The audit log no longer shows `[object Object]`** for a grant like `{"name": "films", "allow_writes": true}`
  (a key or role with write access through a named query). It now shows the object as JSON.
- **A query's own `LIMIT` is respected** (BACKLOG #74). Paging used to replace a trailing `LIMIT`/`OFFSET` with the
  page window, so a saved "top 3" query (`... LIMIT 3`) returned a whole page: 10 rows by default, 50 with
  `?page_size=50`. Paging now happens within the statement's own window: `LIMIT 3` returns 3 rows, and `LIMIT 25`
  pages 10, 10 and 5, then `X-Has-More: false`. This applies to `/q/<name>`, `/execute_sql`, MCP tools and the Console's Run tab. Also fixed in the same place:
  - `LIMIT :n` (a bound parameter), a bare `OFFSET m` and `FETCH FIRST n ROWS ONLY` used to produce invalid SQL;
  - a comment after the `LIMIT` (`... LIMIT 3 -- top three`) used to break the query.

  **Behaviour change:** a client that relied on the page size overriding a saved query's `LIMIT` now gets the
  query's own limit. Raise or remove the `LIMIT` in the query to get the old result.
- **The Console's Theme preference took no effect:** the built stylesheet followed the operating system's light or
  dark setting whatever was chosen. The build now keeps the stylesheet's `light-dark()` colours as written, for
  browsers from 2024 on (Chrome and Edge 123, Firefox 120, Safari 17.5).
- **A database that can't be reached is reported as such:** a run whose driver couldn't connect (refused,
  unreachable, unknown host, timed out connecting) now fails with `connection_failed` and HTTP 502, not
  `query_failed` and 500 - so callers, run history and the "connection failing" alert can tell it from SQL the
  database rejected. The same for schema browsing.
- **The Console's light/dark switch no longer shows a focus ring all the time.**
- **A store first created by 0.10 no longer breaks run history after upgrading.** 0.10 created an
  `execution_history` table in an early shape it never wrote to (its runs still lived in `saved_sql/*.json`), and
  no later release replaced it, so recording or reading runs failed with `no such column: entry_json`. Startup now
  replaces that empty table; one that somehow has rows stops startup with instructions rather than being dropped.

## [0.12.0] - 2026-10-03

### Added
- **Signed-in users (JWT).** `Authorization: Bearer <token>` is accepted as an alternative to an API key, verified
  against your identity provider's signing keys (`QUERYAPIGATE_JWT_JWKS_URL`, RS256, with mandatory
  `QUERYAPIGATE_JWT_ISSUER`/`QUERYAPIGATE_JWT_AUDIENCE`) or a shared secret (`QUERYAPIGATE_JWT_SECRET`, HS256).
  `exp` is required; `none` and HMAC/public-key algorithm mixing are refused; JWKS keys are cached and refetched
  on rotation. What users may do comes from a role (`QUERYAPIGATE_JWT_ROLE`, or per user via
  `QUERYAPIGATE_JWT_ROLE_CLAIM`), read live, its rate limit counted per user. Callers are named `jwt:<sub>`
  (`QUERYAPIGATE_JWT_USER_CLAIM`) in logs, run history and live events - so each user's event stream, on both the
  main server and `queryapigate events`, carries only their own runs; `queryapigate events` closes a stream when
  its token expires. `/metrics` counts all signed-in users under one `jwt` label. `/openapi.json` gains a
  `BearerAuth` scheme. Unsafe settings stop startup. Needs `queryapigate[jwt]` (in the Docker image).
- **`from_claim` parameter rule.** A saved-query parameter can take its value from the caller's verified token -
  `{"customer_id": {"from_claim": "sub"}}` - so the query can only ever return that user's rows. Sending it is a
  400, a token without the claim a 403, an API key other than admin a 403; it is left out of every caller-facing
  description (OpenAPI, catalog, Postman, MCP).
- **`queryapigate events`: a live-events server for many clients.** A separate asyncio process (no new
  dependency) serving `GET /events` on its own port (`QUERYAPIGATE_EVENTS_PORT`, default 5002) to thousands of
  open streams - measured at 9,000 in one process, about 170 MB, every event reaching all of them within 0.75 s.
  Events come from run history in the store, so it sees every worker's and instance's runs: woken by a PostgreSQL
  `NOTIFY` (sent with each history batch) or checking every `QUERYAPIGATE_EVENTS_POLL_INTERVAL` seconds on
  SQLite. Each event carries its history id, and a client reconnecting with `Last-Event-ID` (or
  `?last_event_id=`) gets what it missed - filtered to its key - then the live stream, without gaps or repeats.
  Same key rules as the main server's `/events` (admin sees everything, other keys their own runs); a revoked or
  expired key's stream is closed within about a minute; a client that stops reading is disconnected so it can
  resume; a client that hangs up frees its slot at once. `QUERYAPIGATE_EVENTS_MAX_CONNECTIONS` (default 10,000)
  caps open streams; `QUERYAPIGATE_RATE_LIMIT`, `QUERYAPIGATE_CORS_ORIGINS` and `QUERYAPIGATE_TRUST_PROXY` apply.
  Deployment: a second service from the same image, with `/events` routed to it (DEPLOYMENT.md section 9).
- **Batched run history.** A saved-query run's history entry is queued in memory and written by a background
  thread in one transaction per batch (`QUERYAPIGATE_HISTORY_FLUSH_INTERVAL`, default 1 s; `0` restores writing
  inside the request), so a request never waits on it. A process always reads its own queued runs (they are written
  before any history read); other workers/instances see them within one interval. At most 10,000 runs are queued
  per process - beyond that new ones are dropped and counted rather than slowing requests. New `/metrics`:
  `queryapigate_history_runs_total{outcome=recorded|sampled_out|dropped|failed}` and
  `queryapigate_history_pending`.
- **History retention and sampling.** `QUERYAPIGATE_HISTORY_RETENTION_DAYS` keeps every run for that many days
  instead of each version's newest runs (a sweep every 10 minutes removes older ones) - meant for a PostgreSQL
  store. `QUERYAPIGATE_HISTORY_SAMPLE_RATE` records only that fraction of successful runs; failed runs are always
  recorded. `QUERYAPIGATE_HISTORY_LIMIT` makes the per-version count (50) configurable.
- **`GET /history`** (admin): pages through every stored run, newest first, filtered by query, version, status,
  key and time range - for looking past the newest runs a list shows.
- Settings screen: a **Run history** section for the four new variables.
- **PostgreSQL as an optional metadata store** (`QUERYAPIGATE_DATABASE_URL=postgresql://...`). Connections, saved
  queries and their run history, API keys, roles and the audit log can live in a PostgreSQL database instead of
  `queryapigate.db`, so several instances can share one store - a key created or revoked through one is
  live on all of them at once. Unset (the default) keeps SQLite exactly as before. Needs
  `queryapigate[postgres]` (already in the Docker image). Tables are created on first start; writes that read
  before they modify are serialised with an advisory lock, giving the same guarantees SQLite's single write
  lock did. Legacy pre-SQLite JSON files are never imported into PostgreSQL.
- **`queryapigate migrate-to-postgres`** copies an existing `queryapigate.db` into the (empty) PostgreSQL
  database in one transaction, keeping history and audit-log order; it refuses, changing nothing, if the target
  already has data. `--from PATH` names another file. The server logs a hint at startup when PostgreSQL is
  empty but the home folder still holds a `queryapigate.db` with data.
- The admin UI's **Settings** screen shows which metadata store is in use (password masked).
- CI runs the whole test suite a second time against PostgreSQL (`QUERYAPIGATE_TEST_DATABASE_URL`, see
  `tests/__init__.py`).

### Changed
- Lists (`/list_files`, the admin UI) return a version's newest `QUERYAPIGATE_HISTORY_LIMIT` runs however many are
  stored, so a long retention period never makes them larger.
- **Schema version 3**: `execution_history` gains `status` and `key_name` columns (copies of each run's own entry,
  so `GET /history` can filter on them on both backends) and an index on `executed_at`. Added and backfilled
  automatically on first start; `migrate-to-postgres` needs its source at version 3, i.e. started once with this
  release.
- **Saved-query calls by a scoped key no longer receive the database's own error text.** A failed
  `GET`/`POST /q/<name>` (or `/execute_sql_from_file`) made with any key other than the admin key now returns
  only `{"error": "An error occurred while executing the SQL query"}` - the `detail` field is dropped, since a
  driver message can name tables, columns, constraints or row values to a caller who didn't write the SQL
  and can't fix it. The full message is still in the server log under the same request ID. The admin key
  keeps `detail`, and so does `/execute_sql` for every key (its caller wrote the SQL). **Breaking** for a
  client that read `detail` from a saved-query error with a scoped key.

### Fixed
- **Read-your-writes for run history could miss a batch still being written** (from the batched history in this
  release): a read checked only whether the queue was empty, and the writer empties it before writing - so a read
  in that window went ahead before the batch committed. It now waits while anything queued is unwritten. Seen as
  intermittent failures of history-reading tests on PostgreSQL.
- **A connection's audit entry no longer reports the store's own `updated_at`** as a change. It appeared whenever
  two saves fell in different seconds, and an unchanged re-save a second later produced an empty update entry.
- **The main server's `GET /events` can no longer starve it of request threads.** Each open stream holds a thread,
  so a handful of clients could leave none for anything else (8 froze the Docker image's single worker entirely).
  At most `QUERYAPIGATE_EVENTS_MAX_STREAMS` (default 4) are now held at once; beyond that it answers `503` with
  `Retry-After` and points to `queryapigate events`. `0` turns it off.
- **A malformed JSON body on an endpoint whose body is optional (`POST /q/<name>`) is now a 400** (`Request
  body is not valid JSON`). It used to be silently treated as no body at all, so a typo turned into a
  misleading `<param> is required` - or, for a query whose parameters all have defaults, a successful run
  with none of the values the caller sent.
- **An `int` parameter outside the signed 64-bit range is now a 400** (`must be a 64-bit integer`) naming the
  parameter, instead of a 500 raised from inside the database driver.

### Performance
- **Recording a run's history no longer holds the store's write lock while it trims older entries** - the
  insert and the trim are separate transactions. On PostgreSQL, history writes also skip the store-wide lock
  and the WAL flush wait (the durability SQLite's `synchronous=NORMAL` already gives), so concurrent runs
  across workers and instances don't queue behind one another.
- **API-key authentication is one indexed lookup**, no longer a scan of every stored key: per-request cost
  stayed flat as keys were added (measured: 64 ms per request with 10,000 keys before, unaffected after).
- **Running a saved query no longer loads its execution history** (up to 50 rows per version, each
  JSON-decoded) only to ignore it.
- **The home directory path is resolved once**, not on every metadata-store access (several per request).
- **The metadata store uses `PRAGMA synchronous=NORMAL`**, SQLite's recommended setting under WAL: the file
  still can't be corrupted, but the history row every saved-query run commits no longer waits on an
  fsync. A power loss (not a process crash) can now lose the last few commits.

## [0.11.0] - 2026-10-01

### Added
- **The admin UI's Help > Docs browser now lists the `how-to/` guides too**, under a new "How-to guides"
  section beneath the existing "Reference" docs - the same fetch-from-GitHub-at-the-running-version's-tag
  mechanism already used for README/API/MCP/etc., no new plumbing. `DOCS` entries gained a `group` field;
  the nav now renders a small section label whenever the group changes. Verified rendering for a real
  how-to guide (headings, code blocks, links) with the fetch stubbed locally, since `how-to/` isn't pushed
  to GitHub yet.
- **`queryapigate examples load` now prints a ready-to-run SSE example too**, alongside the existing curl
  example - `curl -N http://127.0.0.1:5000/events -H 'X-API-Key: ...'` - so the live-events feature (see
  below) is discoverable the same way the REST endpoint already was, not just something you'd find by
  reading the docs. Verified: the printed command really receives a live event when a query runs in another
  terminal.
- **`GET /events` (live SSE feed) opened to any authenticated key, each getting its own personal activity
  feed** - previously admin-only, broadcasting every execution to that one connection. A scoped key can now
  connect and receives only executions it triggered itself (`broadcast.Broadcaster` gained a per-subscriber
  `key_name` filter); the admin key's subscription is unchanged, still seeing everything. Documented for the
  first time - see [API.md](documentation/API.md#live-events-server-sent-events). Verified: two scoped keys
  running the same saved query each see only their own event; the admin key still sees both.
- **Two ad-hoc MCP tools, `list_tables` and `execute_sql`**, alongside the existing per-saved-query tools -
  schema discovery and read-only ad-hoc SQL for an agent, gated by the same `connections` grant REST's own
  `/connections/<name>/schema` and `/execute_sql` already check, reusing their exact permission checks and
  execution code. `execute_sql` is always forced read-only over MCP regardless of the calling key's own
  `allow_writes` grant, and still honors a key's `allowed_tables` restriction if it has one. Verified
  end-to-end against a real running MCP server with a real client: tool listing, schema discovery, a real
  query, a refused write, and a refused unauthorized connection. See
  [documentation/MCP.md](documentation/MCP.md#ad-hoc-tools-list_tables-and-execute_sql).
- **`outputSchema`/`structuredContent` for every MCP tool** - a generic result-envelope schema
  (`{"rows": [...], "truncated": bool}` for a saved query or `execute_sql`; `{"tables": [...], "truncated":
  bool}` for `list_tables`), not per-column, since a saved query's actual columns are only known once it
  runs. Verified round-tripping through the real `mcp` SDK's own client, not just this project's own dict
  shape. **Breaking, per this release's own new versioning policy above, if you already call an MCP tool
  and parse its result text**: a row-returning tool's text content used to be a bare JSON array with an
  ad-hoc `"(truncated to N rows)"` suffix string when capped; it is now always the wrapped
  `{"rows": [...], "truncated": bool}` object (or read `structuredContent` directly instead of parsing
  text at all). The `mcp` extra's floor moves to `mcp>=1.10` (from `>=1.9`) - that's where the SDK's own
  `outputSchema`/`structuredContent` support landed.
- **Two on-demand MCP panels in the admin UI's Settings > MCP server section**: a "Reachability" check
  (`GET /settings/mcp_status`, a plain TCP connect attempt against `QUERYAPIGATE_MCP_PORT`, only ever run on
  an explicit "Check now" click - never automatically, preserving this project's original reasoning against
  probing a separate process on every settings load) and a "Tools" panel (`GET /settings/mcp_tools`) listing
  every tool `tools/list` currently returns for an unrestricted caller, computed in-process so it works
  whether or not `queryapigate mcp` is actually running. Verified live against a real running MCP server in
  both the reachable and unreachable states.
- **A real versioning and compatibility policy** (see the "Versioning and compatibility" section above),
  replacing a bare "we use SemVer" line: what's covered by the version number, what isn't, and this
  project's pre-1.0 rule that only a minor release - never a patch - may carry a breaking change, always
  flagged as one. CONTRIBUTING.md and DEPLOYMENT.md's upgrade guidance now point at it instead of restating
  it.
- **`documentation/THREAT_MODEL.md`** - a threat model and security architecture reference, distinct from
  SECURITY.md's operational checklist: what's actually at risk, who might attack it, and how each existing
  control (auth, the scoped-key grant model, the SQL guard's real limitations including a documented past
  bypass, secrets at rest, rate limiting, audit logging) works and where it stops. Linked from SECURITY.md,
  README's security section, the mkdocs nav, and the admin UI's new Docs browser (see below).
- **A "Docs" tab inside Help**, open by default and sized to fill the window (the same full-width treatment
  the API Designer's Run tab already gets), a real in-app browser over this project's markdown docs (README,
  Installation, API Reference, Production Deployment, MCP Server, Examples, Security) - a sidebar list on the
  left, rendered markdown (headings, tables, code blocks, links) on the right. Since `documentation/*.md` isn't shipped in the
  pip package or the Docker image, each doc is fetched client-side straight from GitHub at the tag matching
  the server's own running version, falling back to `main` if that tag isn't published (e.g. a dev build) -
  so what's shown always matches, or is newer than, what's actually running, never older and never wrong for
  the installed version. Markdown is rendered with marked.js, loaded lazily from a CDN on first open, the same
  CDN-script pattern the existing `/docs` (Swagger UI) page already uses.
- **A fuller Help screen.** New "Where to find things" card explains the sidebar's Data/API/Access/Observability
  groups in one place; "Concepts" now covers the response cache and CLI export; "Resources" links the
  production Docker deployment guide and the MCP server docs, neither of which was linked from the admin UI
  anywhere before.
- **A CLI tab on the API Repository screen**, next to Metrics. Shows the `queryapigate export` command for
  that specific saved query - the one CLI command that's actually about a specific query (the
  cron/systemd/Kubernetes CronJob path, running in-process against `QUERYAPIGATE_HOME` with no server or API
  key needed) - with a Copy button. A declared parameter with a default becomes its real value; one without
  becomes a `<placeholder>` to fill in, exactly the same convention the existing Curl tab already uses, off
  the same parameter source, so the two never disagree about what a query's parameters are.

### Fixed
- **`pip install -e ".[dev]"` was broken on Python 3.9**, failing CI on every matrix job at that version.
  The `dev` extra unconditionally pulled in `queryapigate[mcp]`, but the `mcp` package itself requires
  Python ≥3.10 - pip's resolver had no compatible version to install and failed before anything else ran.
  `queryapigate[mcp]` in `dev` now carries a `python_version>='3.10'` marker, so it's simply skipped on 3.9
  (matching how `mcp_server.py`'s own imports are already deferred and `test_mcp.py`'s SDK-dependent tests
  already skip without the package) rather than breaking the install for every other dependency too.
  Verified against a real Python 3.9 container: `pip install -e ".[dev]"`, `ruff`, `mypy` and the full test
  suite (906 tests) all pass.

### Added
- **`documentation/DEPLOYMENT.md`**: a production Docker deployment guide, distinct from the repo-root
  `docker-compose.yml` (a one-command, localhost-only demo). Covers pinning a real image tag, a
  production compose file (reverse proxy/TLS via Caddy, secrets in `.env`, resource limits), backups
  (`queryapigate.db` is SQLite in WAL mode - the published image has no `sqlite3` CLI, so the guide uses
  Python's own backup API instead), Prometheus/Grafana wiring, the optional Redis-backed cache, and why the
  image runs one gunicorn worker rather than a replica count. Every command in it was run against the real
  published image while writing it - including a real ordering bug this caught: running `queryapigate init`
  via `docker compose exec` *after* `up` writes the template connections file, but they don't actually
  appear until the container restarts, since `serve` already initialized an empty `queryapigate.db` first;
  the guide has readers run `init` before the first `up` instead, which sidesteps it entirely.
- **Three API Designer improvements**, addressing the screen feeling sparse compared to API Repository:
  - A connection context strip under the Type/Host/Connection bar - active/inactive, host/database, and the
    same live usage summary (queries/failed/avg latency) the Connections tab's own table shows per row.
  - A per-run quick-stats strip pinned right after the editor (status, rows, duration, format, and the
    widest column on the page) - stays visible without scrolling past a long results table.
  - The Recent Queries side tab now shows each past run's outcome (rows, duration, or "failed (4xx/5xx)")
    under its SQL, not just the query text.

### Changed
- **"Save as New API" no longer leaves API Designer.** It used to navigate to API Repository and open the
  side drawer; now a compact form opens in a box right below the SQL editor, on the same screen, with the
  connection and query text read live from the editor above at save time (not frozen into the form) so
  editing the query with the form open just works.
- **Merged Home's "Recent API requests" and "Slowest queries" into one box with Recent/Slowest tabs**,
  instead of two separate panels - same underlying data (execution_history), shown two ways.

### Added
- **Type/Host/Database filters on the API Repository screen.** Three cascading dropdowns above the
  Collections/Queries list (same cascading idea as API Designer's own Type → Host → Connection picker) narrow
  the list to queries saved against a matching connection - a collection with nothing left after filtering
  just disappears rather than showing up empty. Combines with the existing name/description/tag text search.
- **A cache-entries browser on the Caching screen.** Lists every live response-cache entry (query, version,
  connection, format, content type, size, TTL remaining) for whichever backend is configured - in-process or
  Redis, both now implementing the same `list_entries()`/`get_body()`/`delete()`/`clear()` shape. A "Preview"
  action opens the exact cached response body, served with its real content type, through the **same result
  renderer API Designer's own Run tab already uses** - not a bespoke viewer. Per-entry delete and a "Clear
  cache" action, both admin-only and unaudited (cache housekeeping, not a configuration change). New routes:
  `GET`/`DELETE /cache/entries`, `GET`/`DELETE /cache/entries/{key}`. Scoped deliberately to what
  QueryAPIGate itself put in the cache (`qag:cache:*`), not a general Redis key browser - Redis here is an
  internal cache implementation detail, not a modeled connection.
- **Real-time connection-pool numbers on Home and Metrics.** `pool.py` gained an `active_count()` alongside
  the existing `idle_count()` (a new `queryapigate_pool_active_connections` gauge, next to
  `queryapigate_pool_idle_connections`) - the pool tracked idle connections only before, with no visibility
  into how many are checked out right now. Both tiles carry a small pulsing "live" indicator and refresh
  every 2 seconds via a light, targeted poll (just these two numbers, not a full metrics reload) whenever
  the Home or Metrics tab is visible.
- **Three new benchmark scripts (BACKLOG #25): `benchmarks/latency.py`, `pooling.py`, `caching.py`**,
  alongside the existing buffered-vs-streamed `run.py`, sharing new common plumbing in `benchmarks/common.py`.
  `latency.py` measures p50/p95/p99 for a small `GET /q/<name>`/`POST /execute_sql` call against a fixed
  100-row table, isolating queryapigate's own overhead from query execution time. `pooling.py` measures
  throughput/latency and the idle-pool hit rate as concurrent callers increase relative to
  `QUERYAPIGATE_POOL_SIZE` - and documents a real finding along the way: `pool.py` only bounds *idle*
  connections, not concurrent ones, so there is no bounded-pool "wait time" to measure the way a
  traditional pool would have. `caching.py` measures the real hit-vs-miss latency delta for a
  `cache_ttl`-carrying saved query, classified by the response's own `X-Cache` header. Real runs committed
  for MySQL and ClickHouse (see `benchmarks/README.md`); the other four dialects are a follow-up.

### Changed
- **Renamed the "Saved queries" and "Run SQL" admin UI sections to "API Repository" and "API Designer"**
  - the sidebar nav, breadcrumb, page heading, Home stat tile, quick actions, Ctrl-K search group, and every
  button/hint/tooltip that names the section (e.g. "New saved query" → "New API", "Open in Saved queries" →
  "Open in API Repository"). Scoped to UI chrome only: internal ids (`data-tab="queries"`/`"run"`), routes
  (`/q/<name>`), JSON field names, CLI output, and the generic noun "a saved query" used elsewhere in hints
  and docs are all unchanged.
- **Regrouped the admin UI sidebar**: API Repository and API Designer moved out of "Data" into their own new
  "API" section; Caching moved from "Observability" into "Data", alongside Connections.
- **Collapsed-sidebar nav items now show an inline SVG icon instead of a two-letter abbreviation** (e.g.
  "Sq"/"Rn" → a house for Home, a key for API keys, a shield for Roles, ...). Self-contained, no icon font or
  external library - matches the page's existing "no build step, no external script" design.

### Added
- **The admin UI's Settings screen now has an "MCP server" section (BACKLOG #54)**, showing
  `QUERYAPIGATE_MCP_PORT`/`QUERYAPIGATE_MCP_MAX_ROWS` in the same read-only "effective value and whether it's
  the default" shape as every other setting. `queryapigate mcp` (BACKLOG #42) is a separate process, so this
  is configuration only, not a live health check - the section says so plainly rather than showing a status
  dot that would just be guessing whether that other process is actually up.
- **Example API keys and seeded request history for `queryapigate examples load` (BACKLOG #27).** Loading
  the example APIs now also creates one real API key per role (reporting/dashboard/export/partner/executive)
  and seeds each freshly-loaded query with realistic-looking `execution_history`, so the admin UI's History
  tab, Home tab and requests-per-day chart show real data immediately instead of staying empty until someone
  actually calls a query. **This is a deliberate reversal of examples.py's previous "no key is created"
  design** - the server now requires authentication for every request the moment these keys exist, not just
  the example endpoints. Each key's secret is shown exactly once (printed by the CLI, returned by
  `POST /examples`, or logged once at startup for `QUERYAPIGATE_LOAD_EXAMPLES`) and is never recoverable
  after that. The seeded history is real, persisted `execution_history` via the same function a real request
  already uses; the in-memory `/metrics` counters and the API Keys/Connections "Usage" columns are
  deliberately not seeded, since those are documented as live-traffic-only and reset on every restart.
  README's Quick Start now sets `QUERYAPIGATE_API_KEY` from the start, since none of the example keys can run
  ad-hoc SQL and the server no longer stays open by default.
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
  existing deployment must change these before upgrading (full table and rationale in that release's
  documentation/INSTALLATION_AND_SETUP.md):
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

[Unreleased]: https://github.com/AnanthaRajuC/QueryAPIGate/compare/v0.47.0...HEAD
[0.47.0]: https://github.com/AnanthaRajuC/QueryAPIGate/compare/v0.46.0...v0.47.0
[0.46.0]: https://github.com/AnanthaRajuC/QueryAPIGate/compare/v0.45.0...v0.46.0
[0.45.0]: https://github.com/AnanthaRajuC/QueryAPIGate/compare/v0.44.0...v0.45.0
[0.44.0]: https://github.com/AnanthaRajuC/QueryAPIGate/compare/v0.43.0...v0.44.0
[0.43.0]: https://github.com/AnanthaRajuC/QueryAPIGate/compare/v0.42.0...v0.43.0
[0.42.0]: https://github.com/AnanthaRajuC/QueryAPIGate/compare/v0.41.0...v0.42.0
[0.41.0]: https://github.com/AnanthaRajuC/QueryAPIGate/compare/v0.40.0...v0.41.0
[0.40.0]: https://github.com/AnanthaRajuC/QueryAPIGate/compare/v0.39.0...v0.40.0
[0.39.0]: https://github.com/AnanthaRajuC/QueryAPIGate/compare/v0.38.0...v0.39.0
[0.38.0]: https://github.com/AnanthaRajuC/QueryAPIGate/compare/v0.37.0...v0.38.0
[0.37.0]: https://github.com/AnanthaRajuC/QueryAPIGate/compare/v0.36.0...v0.37.0
[0.36.0]: https://github.com/AnanthaRajuC/QueryAPIGate/compare/v0.35.1...v0.36.0
[0.35.1]: https://github.com/AnanthaRajuC/QueryAPIGate/compare/v0.35.0...v0.35.1
[0.35.0]: https://github.com/AnanthaRajuC/QueryAPIGate/compare/v0.34.2...v0.35.0
[0.34.2]: https://github.com/AnanthaRajuC/QueryAPIGate/compare/v0.34.1...v0.34.2
[0.34.1]: https://github.com/AnanthaRajuC/QueryAPIGate/compare/v0.34.0...v0.34.1
[0.34.0]: https://github.com/AnanthaRajuC/QueryAPIGate/compare/v0.33.0...v0.34.0
[0.33.0]: https://github.com/AnanthaRajuC/QueryAPIGate/compare/v0.32.0...v0.33.0
[0.32.0]: https://github.com/AnanthaRajuC/QueryAPIGate/compare/v0.31.0...v0.32.0
[0.31.0]: https://github.com/AnanthaRajuC/QueryAPIGate/compare/v0.30.0...v0.31.0
[0.30.0]: https://github.com/AnanthaRajuC/QueryAPIGate/compare/v0.29.0...v0.30.0
[0.29.0]: https://github.com/AnanthaRajuC/QueryAPIGate/compare/v0.28.0...v0.29.0
[0.28.0]: https://github.com/AnanthaRajuC/QueryAPIGate/compare/v0.27.0...v0.28.0
[0.27.0]: https://github.com/AnanthaRajuC/QueryAPIGate/compare/v0.26.0...v0.27.0
[0.26.0]: https://github.com/AnanthaRajuC/QueryAPIGate/compare/v0.25.0...v0.26.0
[0.25.0]: https://github.com/AnanthaRajuC/QueryAPIGate/compare/v0.24.0...v0.25.0
[0.24.0]: https://github.com/AnanthaRajuC/QueryAPIGate/compare/v0.23.0...v0.24.0
[0.23.0]: https://github.com/AnanthaRajuC/QueryAPIGate/compare/v0.22.1...v0.23.0
[0.22.1]: https://github.com/AnanthaRajuC/QueryAPIGate/compare/v0.22.0...v0.22.1
[0.22.0]: https://github.com/AnanthaRajuC/QueryAPIGate/compare/v0.21.0...v0.22.0
[0.21.0]: https://github.com/AnanthaRajuC/QueryAPIGate/compare/v0.20.1...v0.21.0
[0.20.1]: https://github.com/AnanthaRajuC/QueryAPIGate/compare/v0.20.0...v0.20.1
[0.20.0]: https://github.com/AnanthaRajuC/QueryAPIGate/compare/v0.19.1...v0.20.0
[0.19.1]: https://github.com/AnanthaRajuC/QueryAPIGate/compare/v0.19.0...v0.19.1
[0.19.0]: https://github.com/AnanthaRajuC/QueryAPIGate/compare/v0.18.0...v0.19.0
[0.18.0]: https://github.com/AnanthaRajuC/QueryAPIGate/compare/v0.17.0...v0.18.0
[0.17.0]: https://github.com/AnanthaRajuC/QueryAPIGate/releases/tag/v0.17.0
[0.16.0]: https://github.com/AnanthaRajuC/QueryAPIGate/releases/tag/v0.16.0
[0.15.0]: https://github.com/AnanthaRajuC/QueryAPIGate/releases/tag/v0.15.0
[0.14.0]: https://github.com/AnanthaRajuC/QueryAPIGate/releases/tag/v0.14.0
[0.13.0]: https://github.com/AnanthaRajuC/QueryAPIGate/releases/tag/v0.13.0
[0.12.0]: https://github.com/AnanthaRajuC/QueryAPIGate/releases/tag/v0.12.0
[0.11.0]: https://github.com/AnanthaRajuC/QueryAPIGate/releases/tag/v0.11.0
[0.10.0]: https://github.com/AnanthaRajuC/QueryAPIGate/releases/tag/v0.10.0
[0.9.0]: https://github.com/AnanthaRajuC/QueryAPIGate/releases/tag/v0.9.0
[0.8.0]: https://github.com/AnanthaRajuC/QueryAPIGate/releases/tag/v0.8.0
[0.7.1]: https://github.com/AnanthaRajuC/QueryAPIGate/releases/tag/v0.7.1
[0.7.0]: https://github.com/AnanthaRajuC/QueryAPIGate/releases/tag/v0.7.0

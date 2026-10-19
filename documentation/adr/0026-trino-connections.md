# ADR 0026: Trino as a native connection type

- **Status:** Accepted (2026-10-19)
- **Backlog:** roadmap item #76 (Trino / Presto as a native connection type). Launches `trino` experimental (#64),
  to graduate against the connection-type criteria (`databases.GRADUATION`, #66). Implemented, with fully-qualified
  `allowed_tables` (§2), tested against a real Trino in CI, and reviewed for security (THREAT_MODEL.md, "Trino"); a
  release of stable shape and real-deployment use remain before it graduates.

## Context

Trino (and its ancestor Presto) is a distributed SQL engine that federates many catalogs behind one endpoint -
Hive, Iceberg, Delta, Kafka, object stores, other databases. One Trino connection could turn every one of those
into a source QueryAPIGate publishes as a governed REST and MCP tool, through a single well-understood engine.

Today Trino is reachable only through the **generic JDBC** connection type, which is deliberately minimal: no query
time limit, no schema browser, and `allowed_tables` and masking off (JDBC can't be traced by dialect). That is the
worst of both worlds for a federation engine - broad reach, little governance.

A native type can do better, and the pieces exist:

- The codebase has a clean "add a dialect" shape: a `runners._Driver` subclass plus entries in a handful of
  per-dialect registries (`RUNNERS`, `schema._QUERIES`, `tableguard._DIALECT_MAP`, `masking._DIALECT_MAP`, …).
- **sqlglot ships a `trino` dialect**, so the table guard, column masking and the flow diagram work by mapping
  `trino → 'trino'`, no new parser.
- The `trino` Python client (DB-API) supports parameter binding, `information_schema` per catalog, and
  cancellation of a running query.

Three things about Trino make it different from the single-database types already supported, and each needs a
decision: its names are **three-part** (`catalog.schema.table`); it has **no read-only session mode**; and it has
**no server-enforced timeout** unless we ask for one.

## Decision

Add `trino` as a **native, experimental** connection type, mirroring how H2/JDBC and (before it graduated) MongoDB
were introduced.

### 1. The connection is a Trino endpoint; `database` means *catalog*

A `trino` connection carries host, port, user, auth and `http_scheme`, plus a **catalog** (mapped to the generic
`database` field) and an optional default **schema**. "Switch database" in the Console switches catalog
(`LIST_DATABASES_QUERIES['trino'] = 'SHOW CATALOGS'`). The schema browser reads the catalog's own
`information_schema`.

The connection reaches whatever that Trino **user** is allowed to reach. We treat Trino's own access control as the
outer boundary and recommend a **read-only, catalog/schema-scoped Trino user** per connection - QueryAPIGate's
grants then narrow *within* that.

### 2. `allowed_tables` is matched fully qualified

For every other dialect a table matcher extracts a **bare, lower-cased** table name (sqlglot's `.name`), dropping
catalog and schema. For a **federation** engine that would be dangerous: a grant of `orders` would match `orders`
in *every* catalog the connection can see.

So for `trino`, extraction and matching are **fully qualified**. sqlglot with `read='trino'` parses
`exp.Table.catalog` / `.db`; `tableguard` emits each real table as `catalog.schema.table`, filling the parts a
reference omits from the connection's defaults (the connection's catalog = its `database` field, and schema). A
grant is qualified the same way (`tableguard.qualify_grant`): `orders` → `<catalog>.<schema>.orders`,
`sales.orders` → `<catalog>.sales.orders`, `hive.sales.orders` as written. So a grant of `orders` reaches only the
default catalog+schema's `orders`, never `orders` in another catalog or schema. The same qualified matching is used
by the schema browser (`schema._only_allowed`). A scoped, read-only Trino user is still recommended as an outer
boundary, but `allowed_tables` is now catalog/schema-accurate - not the schema-blind limitation other dialects carry.

### 3. Read-only rests on the SQL guard, not the driver

Trino has no read-only transaction or session mode, so - like DuckDB, H2 and JDBC - the read-only guarantee is the
server's SQL check (`sqltools.validate_sql` / `write_operations`): a non-read-only statement is refused with
`read_only` before it runs. `trino` is **not** a member of `databases.DRIVER_READ_ONLY`. We verify the write-keyword
set covers Trino's write surface (INSERT/UPDATE/DELETE/MERGE, `CREATE TABLE AS`, `CALL`, and `ANALYZE`, which
collects statistics and is treated as a write). Trino uses ANSI string quoting, so it correctly stays out of
`BACKSLASH_ESCAPE_DIALECTS`. A read-only Trino user is recommended as defense in depth. **Agent writes (ADR 0008)
are out of scope** for the first cut - `trino` is not in `WRITE_RUNNERS`.

### 4. The query time limit is enforced by cancellation

There is no engine-level timeout; each driver enforces its own. For `trino` we start a daemon timer that cancels
the running query on the client (`cursor.cancel()`), the same shape DuckDB uses, and also `SET SESSION
query_max_run_time` as a server-side backstop. With this, `trino` is in `databases.TIMEOUT_ENFORCED` and passes the
timeout integration test (unlike JDBC, which self-skips it).

### 5. Not in the first cut

- Qualification-aware `allowed_tables` (see §2) - the graduation blocker.
- Primary/foreign keys in the schema browser (Trino has no enforced constraints) and `SHOW CREATE TABLE` DDL
  (three-part identifier handling) - both left off, as for ClickHouse.
- Agent writes.

### 6. Experimental, with a graduation checklist

`trino` joins `experimental.FEATURES['connection_types'].db_types`. Already met: a real Trino runs in the
integration CI job, with the read-only check, the time limit and (fully-qualified) `allowed_tables` each tested
against it; and the federation boundary has a security review (THREAT_MODEL.md, "Trino"). Remaining to graduate (per
`databases.GRADUATION`): the shape unchanged for a release and real-deployment use; the docs record what may still
be added (writes, more metadata).

## Consequences

- Trino's catalogs become first-class, governed sources - with masking, the schema browser, the flow diagram, a
  real time limit, and run/audit history - instead of the ungoverned generic-JDBC path.
- The federation reach that makes Trino valuable is also what makes bare-name `allowed_tables` risky; we are explicit
  that, until qualification-aware matching lands, the scoped Trino user is the boundary, and the type stays
  experimental to say so.
- No new parser or masking engine: the sqlglot `trino` dialect carries the table guard, masking and the diagram.
- `trino` is an optional extra (`queryapigate[trino]`); a server without it is unaffected.

## Alternatives considered

- **Leave it to generic JDBC.** Works today, but with no timeout, no schema browser, and no `allowed_tables`/masking
  - unacceptable governance for an engine that reaches everything.
- **Build our own federation layer** (query several connections at once). Far larger, and it reinvents exactly what
  Trino is for; the backlog already parks that idea behind this one.
- **Pin each connection to a single catalog** so bare names are scoped. Safer, but it throws away Trino's reason to
  exist (one connection, many catalogs); rejected in favour of qualification-aware matching as the graduation path.

## Delivery

Roughly: a `_Trino` driver + `RUNNERS`/`STREAM_RUNNERS` entries and the `trino` extra; the membership trio
(`config.SUPPORTED_DB_TYPES`, `databases.TYPES`, `experimental` `db_types`); the sqlglot-dialect maps
(`tableguard`, `masking`, `sqlflow`, `tracing`) and the frontend lists; `schema._QUERIES['trino']` and
`SHOW CATALOGS`; a `TrinoTests` integration class with a `trinodb/trino` CI service; the regenerated support matrix
and a `## Trino connections` doc section. Launched experimental; qualification-aware `allowed_tables` and the CI
security review are the work that graduates it.

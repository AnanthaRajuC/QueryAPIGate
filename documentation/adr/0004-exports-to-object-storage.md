# ADR 0004: Exports to object storage - destinations, saved exports, incremental runs

- **Status:** Accepted (2026-10-06)
- **Date:** 2026-10-06
- **Backlog:** #89 (exports to object storage, incremental). Pairs with #63 / [ADR 0005](0005-column-masking.md).

## Context

A saved query's result can leave QueryAPIGate today in two ways: over HTTP (`?stream=true`, any format), or as a
local file (`queryapigate export --out /exports/{name}_{date}.parquet`, run by cron or a Kubernetes CronJob). Neither
reaches where partners and pipelines actually pick data up - a bucket - so every deployment that delivers data writes
its own script around them: fetch, upload, remember what was sent last time.

Two costs follow. Credentials for the destination live in that script, outside QueryAPIGate's governance (no audit,
no masking, no history). And every run exports everything: a daily export of a growing table reads it all, daily.

What exists to build on: DuckDB already reads `s3://`, `gs://` and `r2://` through `httpfs`, with credentials from a
connection's `user`/`password` (encrypted at rest like any connection password) and each connection locked to its
`allowed_paths` (`duckfiles.py`). Parquet output is written by DuckDB (`parquet.py`). History, alerts and the audit
log are shared by every instance.

Scheduling stays out: *when* to run something, retrying it and paging someone are what cron, systemd timers,
Kubernetes CronJobs and Airflow already do (BACKLOG, "A general job scheduler").

## Decision

### 1. Destinations: where exports may write, with whose credentials

A **destination** is a bucket prefix and the credentials to write under it - nothing else:

```json
{"name": "partner-acme", "url": "s3://acme-exchange/from-us/", "storage": "s3", "region": "eu-west-1",
 "user": "AKIA...", "password": "${ACME_SECRET_KEY}"}
```

- `url` is an `s3://`, `gs://` or `r2://` prefix ending in `/`; `storage`, `region`, `endpoint`, `url_style`,
  `use_ssl` and `account_id` mean what they mean on a DuckDB files connection. A local folder (`file:///exports/`)
  is accepted too, so one mechanism covers both.
- The secret is stored like a connection password: masked in every response, `${VAR}` or encrypted with
  `QUERYAPIGATE_SECRET_KEY`.
- **Writes can't leave the prefix.** Every export run writes through a fresh in-memory DuckDB connection locked to
  exactly that prefix (`duckfiles.lock_down` with `allowed_paths = [url]`), so no path template, parameter or file
  name can reach another key in the bucket - DuckDB refuses it. Give the credentials write access to that prefix
  only, as with reading (`duckfiles.py`'s second lock).
- `/api/v1/destinations` (create, read, update, delete, and `POST .../test`, which writes and deletes a probe
  object). Kept apart from connections: a connection is something to *read*, a destination somewhere to *write*,
  and a credential that can do both is the thing to avoid.

### 2. Saved exports: what to deliver, where, and in what shape

An **export** names a saved query, its parameters, a format, a destination and a path:

```json
{"name": "acme-daily-orders", "query": "orders_since", "params": {"region": "EU"},
 "format": "parquet", "destination": "partner-acme", "path": "orders/{date}/orders_{run}.parquet",
 "incremental": {"column": "updated_at", "parameter": "since", "start": "2026-01-01"},
 "mask": [{"column": "customers.email", "action": "hash"}]}
```

- It runs the query's **published** version, as `/q/<name>` does.
- `format`: `parquet`, `csv` or `ndjson` - all written by DuckDB (`COPY ... TO`), from the same staging `parquet.py`
  uses, so a run streams through the server in constant memory and never holds the result.
- `path` is relative to the destination; placeholders `{name}`, `{date}` (`YYYY-MM-DD`), `{time}` (`HHMMSS`),
  `{run}` (the run's id) and `{param}` for a declared parameter's value (restricted to `[A-Za-z0-9._-]`). No `..`.
  A path that would write over an existing object is the caller's choice - `{run}` makes every file unique.
- `mask`: column rules applied to every row before it is written (ADR 0005) - a partner export's masking is part of
  the export, not of whoever triggers it.
- `skip_empty` (default true): a run with no rows writes no file, and says so.
- `/api/v1/exports` (create, read, update, delete) and `GET /api/v1/exports/{name}/runs` (its history).

### 3. Incremental runs: a watermark per export

With `incremental`, each export remembers a **watermark**: the largest value of `column` it has delivered.

- A run binds the watermark to the query's `parameter` (`:since`), or `start` on the first run. The query says how to
  use it - `WHERE updated_at > :since`.
- While the rows stream to the destination, the run tracks the largest `column` value; **only after the file is
  written** is it stored as the new watermark. A failed run leaves the watermark where it was, so the next run sends
  those rows again: at-least-once, never lost.
- Stored in the store (an `exports` table, schema 8), so it survives restarts and every instance sees it.
  `PATCH /api/v1/exports/{name}` with `{"watermark": ...}` resets or rewinds it, audited.
- **Ties at the boundary are the query's choice**, documented: `>` can miss rows written later with the same
  timestamp, `>=` re-sends the boundary rows; a monotonic id column avoids both.

### 4. Running an export

- `POST /api/v1/exports/{name}/runs` runs it and answers when the file is written: rows, bytes, the object written,
  the new watermark. For a scheduler that speaks HTTP (Airflow, a Kubernetes CronJob with `curl`).
- `queryapigate exports run NAME` does the same from the store directly - no server, for cron on the host.
- **One run at a time per export**: a run takes a lease in the store; a second run while it holds answers
  `409 export_running`. A lease older than the query timeout plus a margin is treated as abandoned, so a crashed
  run doesn't block the next one forever.
- Each run is recorded in run history (`transport: "export"`, with the export, destination object, rows, bytes and
  watermark) and the `export_failing` alert fires when an export's newest run failed.

### 5. Who may do what

New capabilities, additive to ADR 0003's table:

| Capability | Owner | Admin | Developer | Auditor |
|---|:-:|:-:|:-:|:-:|
| `destinations.write` - create or change where data may be written, and its credentials | ✓ | ✓ | | |
| `exports.write` - create or change exports (what data goes where) | ✓ | ✓ | | |
| `exports.run` - run an export someone else defined | ✓ | ✓ | ✓ | |
| `exports.read` - destinations (secrets masked), exports, their runs | ✓ | ✓ | ✓ | ✓ |

Defining an export decides who receives data, so it is an owner's or admin's decision - the same line ADR 0003's
amendment drew for collections. Running one changes nothing about who receives what, so a CI pipeline on a developer
token may trigger it. Scoped API keys can't touch exports.

## Consequences

- **New:** two resources and their routes, `queryapigate exports run|list`, schema 8 (`destinations` and `exports` tables,
  the latter holding each export's watermark and lease - additive), the `export_running` and `export_failing` codes, four
  capabilities. Nothing existing changes; `queryapigate export --out` keeps writing local files.
- **Experimental in its first release** (BACKLOG #64), so the shapes can still change once people use it.
- **Formats:** CSV and NDJSON written by DuckDB, not by `formats.py` - dates and decimals are rendered DuckDB's way,
  which is documented; the HTTP formats are unchanged.
- **Not in scope:** scheduling, SFTP/FTP/WebDAV, exports of ad-hoc SQL (save it as a query first), and pushing to
  HTTP endpoints. Later, if asked: Iceberg/Delta tables as a destination format (#83).

## Delivery

About a week, in slices:

1. Destinations: store, validation, `/api/v1/destinations` and its test write, capabilities.
2. The writer: DuckDB `COPY` to a locked destination for parquet/csv/ndjson, path templates; `queryapigate exports
   run` against a SeaweedFS bucket in the tests (as `test_duckdb_files.py` does) and a local folder.
3. Saved exports, runs, the lease, incremental watermarks, history and the alert.
4. Console: Destinations and Exports screens, a run button and run history.
5. A how-to guide ("Deliver a daily file to a partner's bucket"), docs, changelog.

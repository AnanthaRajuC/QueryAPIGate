# ADR 0022: MongoDB streaming exports, and MongoDB graduates

- **Status:** Accepted
- **Date:** 2026-10-16
- **Backlog:** the roadmap's item 25 (0.37: MongoDB graduates, with streaming exports). Graduates `mongo` out of the
  `connection_types` entry in `experimental.py`, on `databases.GRADUATION`'s terms.

## Context

MongoDB has been an experimental connection type since 0.9. 0.34 and 0.35 closed most of what kept it there:

- **Real CI.** Since 0.35 the integration job runs it against MongoDB 8.0, and the oldest-dependencies job against
  `pymongo` 4.0. The tests cover find() with filter, projection, sort and pages; typed parameters; the time limit;
  `allowed_tables` as collections; and the refusals that keep a find() a find().
- **A security review.** 0.34.1 and 0.34.2 fixed what a review of MongoDB found. `allowed_tables` wasn't checked.
  Server-side JavaScript got through in a projection or sort. A parameter could be operators. The fuzzing added in
  0.35.1 (`fuzz/fuzz_mongo.py`) holds those refusals as properties.
- **Time limit, read-only, `allowed_tables`.** `maxTimeMS` cancels a find. MongoDB support is find() only, so it is
  read-only by construction. `allowed_tables` names the collections a key may read.

One essential tier-1 feature is missing: **streaming exports**. `tests/test_databases.py` requires it of every tier-1
type. Today MongoDB refuses it everywhere it's asked for:

| Where | Today |
|---|---|
| `GET /q/<name>?stream=true` (a saved `mongo` query) | `400 stream_unsupported` |
| `queryapigate export <name>` and scheduled exports | `invalid_body`: "A MongoDB query can't be exported yet" (`exports.prepare()`) |
| `/execute_mongo` | paged only |

Streaming a SQL result is simple because the database says what its columns are before the first row. MongoDB
documents don't share a schema. The fields of the thousandth document may not be those of the first, a field may
hold a number in one document and a string in the next, and values nest. Each streamed format has to decide what
that means for it.

The four streamed formats need different things:

- **NDJSON** is one JSON object per line. It needs no columns at all.
- **Parquet** is already written whole before any of it is sent (a Parquet file's footer comes last), and
  `parquet.py` works out each column's type from all of its values: a mix becomes text. Nothing has to be known up
  front.
- **CSV and TSV** send their header line first. The columns have to be decided before the first document is
  written. They are the only formats with the schemaless problem.

## Decision

### 1. Every streamed format, every place a SQL query streams

A saved `mongo` query streams through `?stream=true`, `queryapigate export` and scheduled exports, in CSV, TSV,
NDJSON and Parquet. `/execute_mongo` takes `?stream=true` too, as `/execute_sql` does.

### 2. What each format writes

- **NDJSON: each document whole**, as MongoDB returned it after the projection: nested documents and arrays stay
  nested, and no field is ever dropped. `ObjectId`, dates and decimals are written as the paged JSON writes them.
- **Parquet: every field of every document.** Documents are staged as they arrive. The columns are the union of their
  fields, in first-seen order, and the types are worked out from all the values, as for SQL. A field that is a
  number in one document and a string in another becomes a text column. Nested values are JSON text.
- **CSV and TSV: the columns are decided before the first row.**
  - With an inclusion projection (`{"name": 1, "total": 1}`), the columns are its fields in the projection's order,
    with `_id` first unless the projection sets `"_id": 0`. That is exact and the same on every run, and it is what
    the documentation recommends for a CSV export.
  - Otherwise (no projection, or an exclusion-only one), the first 1,000 documents are read before anything is
    sent. The columns are the union of their fields in first-seen order, the same rule the paged result already
    uses.
  - A field that first appears after those documents has no column. It is left out of the row, never squeezed into
    another column. The run says how many fields were left out (`fields_dropped` in its history entry, and a
    warning in the log). The response can't say so, because its headers went out with the first row.
  - Nested documents and arrays are JSON text in their cell, as in the paged CSV.
- **Written to a destination** (saved exports, `queryapigate export --to`): every format is staged before DuckDB
  writes it, so every field of every document is a column, as for Parquet - CSV included, with no field left out.
  NDJSON there is written from those columns too, so nested values are JSON text, not nested: a document stays whole
  only in NDJSON sent over HTTP or written to a local file by `queryapigate export`.

### 3. The same governance as a SQL stream

`allowed_tables` (as collections) is checked before the find runs. The query time limit is `maxTimeMS` on the cursor,
covering the whole stream. A caller's `max_rows`, `QUERYAPIGATE_STREAM_MAX_ROWS` and the concurrency slot all apply,
and the slot is held until the last document is read or the client leaves. Runs, events, metrics, quotas and the
audit trail are recorded as for SQL. Column masking stays refused on MongoDB, as today: that is a separate gap,
written down as a limit.

### 4. Not in this release

- **Incremental exports** (`incremental`, which keeps a watermark between runs) need the saved filter combined with
  "after the last value", which is its own design. A `mongo` export with `incremental` is refused with a clear error,
  and full exports work.
- **Aggregation pipelines** (#78) stay out. Streaming covers find() as it is today.

### 5. Then MongoDB graduates

With streaming, MongoDB meets every essential tier-1 feature. In the support matrix, *Read-only by default* reads
"always (find() only)" for MongoDB rather than *n/a*. In the same release:

- `mongo` leaves `experimental.py`'s `connection_types` entry, which keeps H2 and JDBC. The Console's
  `EXPERIMENTAL_DB_TYPES`, the startup warning and Settings follow the registry.
- The documentation's experimental section becomes "Experimental: h2 and jdbc", and MongoDB gets a stable callout.
  It says what may still be added (aggregation pipelines, writes, response caching, masking, incremental exports)
  and its limits: find() only, no writes, no response caching, no MCP `execute_sql`, no column masking, CSV columns
  from the projection or the first 1,000 documents, and collection names matched case-insensitively for
  `allowed_tables`.
- The threat model records the review: the 0.34.1 and 0.34.2 findings and the fuzzing that now holds them.

## Consequences

- **MongoDB comes under the compatibility promise.** The `mongo` saved-query shape (`mongo_collection`,
  `mongo_filter`, `mongo_projection`, `mongo_sort`), `/execute_mongo` and the streamed formats above can then change
  only by addition.
- **Closing a stream closes the database's own generator.** Building this found that a client leaving a stream
  closed the wrapper reading the rows but not the generator under it, so a MongoDB cursor stayed open on the server
  until garbage collection. That applied to SQL streams too, which hold a connection the same way; both now close at
  once.
- **The CSV rule becomes a contract.** Projection order or the first 1,000 documents decides the columns. Changing
  that later would change exports people already parse. That's why it's decided here.
- **A CSV export without a projection can lose fields** that first appear late. It's a documented limit, with a count
  in the run's history. NDJSON and Parquet never lose a field.
- **No new dependency.** pymongo's cursor and the existing writers are enough.

## Alternatives considered

- **CSV columns from the first document only.** That's simpler, but a single sparse first document drops most of an
  export. The first 1,000 match the paged result's rule and cost little memory.
- **Two passes for CSV: read everything to learn the columns, then send.** It's exact, but it either buffers the
  whole result or runs the find twice. That's wrong for an export of millions of documents, and a second run may
  not see the same data.
- **Write the late fields into an extra JSON column.** It's lossless, but every row then carries an "everything else"
  cell that no spreadsheet or loader expects. NDJSON is the format for a lossless export, and the docs say so.
- **Parquet with the column types of the first documents.** A later value of another type would then fail the
  export half-way. The existing writer already reads every value before choosing a type, and MongoDB needs that.
- **Refuse Parquet for MongoDB.** That would have been simpler, but the writer already handles mixed types and
  nested values. Refusing would leave a gap with no reason behind it.

## Delivery

1. **Streaming** (§1-§3): the cursor as a stream runner, the CSV column rule, the NDJSON and Parquet writers for
   documents, `?stream=true` on both MongoDB routes, `exports.prepare()` and the CLI. Tests run against the fake
   client for the column rules and a time limit mid-stream (a real server can't be made to time out half-way on
   cue), and against MongoDB 8.0 in the integration job for real cursors across batches, the time limit on a stream,
   a client that leaves early (its cursor closed on the server), and `fields_dropped`.
2. **Graduation** (§5): the registry split, the support matrix, the documentation, the threat-model entry and the
   changelog.
3. **0.37.0**, once both are green.

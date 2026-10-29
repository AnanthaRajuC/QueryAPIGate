# ADR 0029: An optional ClickHouse run store for history, the event log and usage

- **Status:** Accepted (2026-10-28). **Decided: go (2026-10-28),** on the measurements in section 9's
  [Result](#result-every-criterion-met-at-the-sizes-measured): every criterion met, at the sizes this laptop's disk
  allowed, with what differs from plan. Amended while building it: three columns in `events`, a `history_counts`
  table, and the rollups' code and statement dimensions, each found by measuring a read (sections 3 to 5, *as
  built*). It ships experimental (section 1), and graduates after a release unchanged, a security review and the
  250-million-run measurement. **Security review: done (2026-10-28),** its findings fixed in section 7's
  *Reviewed*; schema version 2 of the run store's tables. **250 million runs: measured (2026-10-29),** every
  criterion met with the replay reading its ids in batches (section 9, *At 250 million runs*). Both shipped in 0.46.0.
  What's left to graduate: a release unchanged after 0.46.0.
- **Date:** 2026-10-28
- **Backlog:** #103. The second condition of [ADR 0028](0028-rust-data-plane-prototype.md#result-go)'s go decision:
  a store built for runs at the data plane's rate. Keeps [ADR 0002](0002-event-ids.md)'s event-id rule and
  [ADR 0020](0020-the-event-log.md)'s every-run-is-an-event rule. Like 0028, this is built as a prototype and ends
  in a measured decision (section 9).

## Context

The data plane serves 20,000 to 24,000 calls a second per host (Performance v5). Every run is written twice: a row
in `execution_history` and an event in `events`, in the SQLite or PostgreSQL store that also holds keys, queries and
the audit log. At 20,000 a second that is 1.7 billion events a day, and the event log keeps each one for 24 hours
by default. The benchmark that decided 0028 filled this laptop's disk before recording was fixed. Neither store
was built for this:

- **Volume.** A row in either table costs a few hundred bytes with its indexes. A day of events alone would be
  hundreds of gigabytes on PostgreSQL.
- **The write path.** In 0.44 the data plane sends each batch of runs to Python, which queues and inserts them.
  That costs Python about 17,000 runs a second per process at most (ADR 0028, Consequences), so two workers
  behind every data plane are a condition of the go.
- **Reads that scan.** A usage report reads at most 100,000 runs and then says `truncated`. The alerts read the newest
  5,000. Both are summarised in Python, and both read history, which `QUERYAPIGATE_HISTORY_SAMPLE_RATE` may sample.
- **The ordering guard.** On PostgreSQL, transactions commit out of id order, so the events server looks back 1,000
  ids behind the newest it delivered (events.py). At 20,000 events a second, 1,000 ids is 50 ms.

ClickHouse was built for append-only streams like this. It compresses columns of repeated values well, takes
large batched inserts cheaply, and expires rows by TTL instead of by a sweep. Its table engines can also maintain
rollups as rows arrive. The project already supports ClickHouse as a connection type, tested against 24.8 in CI.

### Measured before writing this

On ClickHouse 24.8.14, one container on this laptop, with the schema in section 3: 5,000,000 runs shaped like the
data plane's (200 keys, 50 queries with 3 versions each, 2 connections, 1% errors; each entry as Python's `json.dumps`
writes it). They were loaded in batches of 100,000, 250 seconds of traffic at 20,000 a second.

| | Measured |
|---|---|
| Load, through two materialized views | 16 s for 5,000,000 runs (about 310,000 a second, one client) |
| `events`, compressed | 24.3 bytes a run (1.21 GiB raw to 116 MiB). 16 of the 24 bytes are the entry, mostly its random `request_id` |
| `history`, compressed | 24.1 bytes a run |
| Per-minute rollups | 48 bytes a row. Rows grow with the distinct key, query, version, connection and status combinations each minute, not with calls: this data was close to the worst case, about 69,000 a minute |
| Tail: the next 1,000 events after an id | 26,000 rows read, 2.4 ms |
| Replay for one key, from 4 minutes back | 4.2 million rows read for 1,000 events, 11 ms |
| History, newest 100 in a workspace | 118,000 rows read, 5 ms |
| History, newest 100 for one key | 724,000 rows read, 7 ms |
| The Console's newest 50 runs of 10 versions, last minute | 1.2 million rows read, 27 ms |
| Usage per key over 24 hours, from the rollups | 10 ms, exact (no scan limit, no sampling) |
| Erasing a key from all three tables (lightweight `DELETE`) | about 0.1 s a table; purged from disk with `APPLY DELETED MASK`, 1.4 s for the 5 million |
| The same insert twice with one `insert_deduplication_token` | one copy kept |

Two findings shaped the schema:
- **Projections and erasure don't mix on 24.8.** A by-version projection on `history` made the Console's read
  cheap, at +23% storage for a narrow one and +157% for a full copy. But 24.8 refuses a lightweight `DELETE` on a
  table with a projection, and `forget` needs one. An `ALTER TABLE ... DELETE` mutation works with projections, but
  it rewrote the 5 million rows in 3.4 s and would rewrite every part at scale. So history has no projections, and its
  reads are windowed by time instead (section 5).
- **The planner ignores a projection for a tuple `IN (...)` list**, even when it uses one for a single version.
  This is one more reason not to depend on projections.

These are 5 million rows on one laptop, not a day's 1.7 billion. Section 9 sets the measurements that decide.

## Decision

### 1. An optional run store

When `QUERYAPIGATE_RUN_STORE_URL` is set, runs, the event log and usage rollups go to ClickHouse. Unset, nothing
changes: SQLite or PostgreSQL holds them as now. Everything else stays in the store it's in today: keys, queries,
connections, roles, workspaces, instances, settings and the audit log. The audit log's hash chain (ADR 0012) and its
low volume belong with the definitions it records.

```
QUERYAPIGATE_RUN_STORE_URL=https://queryapigate:password@clickhouse.internal:8443/queryapigate
```

- **ClickHouse's HTTP interface, from both languages.** Python uses the standard library's `http.client`, with
  keep-alive. It inserts `JSONEachRow`, and every value in a read is a typed query parameter
  (`{key:String}`), never SQL text. That adds no Python dependency. Rust uses the official `clickhouse` crate, which
  sends RowBinary with LZ4 over HTTP. One URL and one port serve both.
- **TLS:** `https://` is required except to a loopback host. The URL is a secret setting, like
  `QUERYAPIGATE_DATABASE_URL`: it can be read from a file named by `QUERYAPIGATE_RUN_STORE_URL_FILE` (ADR 0015).
- **Versions:** ClickHouse 24.8 LTS or later, tested in CI against 24.8 and the newest LTS. One server, or
  ClickHouse Cloud. Self-managed replicated clusters are an open question.
- **Experimental** (`queryapigate/experimental.py`) until section 9's measurements and a release unchanged.

### 2. Event ids: time-ordered, below 2^53, continuing the sequence

ADR 0002 freezes an event id as an opaque integer that increases per store, and promises that a resume after one
misses nothing and repeats nothing. ClickHouse has no sequence. Writers in several processes on several hosts
therefore mint the ids themselves:

```
id = milliseconds since 2026-01-01 UTC x 4096 + a number within that millisecond
```

- **Below 2^53 until 2095,** so a JavaScript client that compares ids as numbers, as the docs allow, compares
  them exactly. Today's ids are around 10^14, far above any id the SQL stores have handed out, so a client holding an
  old id resumes after it (ADR 0002 §3).
- **Writers lease blocks of the 4,096 numbers in each millisecond.** The block is 16 for a Python process
  (16,000 runs a second) and 256 for a data plane (256,000 a second). That gives 4.1 million ids a second per store,
  for example 8 data planes and 128 Python processes. The lease is a row in the SQL store (schema 18), renewed every
  10 s and freed 60 s after its last renewal. The data plane gets its lease from Python, at
  `POST /internal/dataplane/lease` behind its secret, so its store access stays read-only (ADR 0028 §3).
- **One clock: ClickHouse's.** Each process estimates the offset between its own clock and the server's
  (`SELECT now64(3)`, corrected by half the round trip) when it connects and every minute after. It stamps ids on the
  corrected clock. A writer's clock never goes backwards. If a burst fills its block for a millisecond, it moves on to
  the next one. It never runs more than 1 s ahead: past that, runs are dropped and counted, as history's
  back-pressure does, and serving is never slowed.
- **Stamped just before the insert, not when the run happened.** A run's `occurred_at` is its own time, as now. Its
  id is the moment its batch is sent. A retry is stamped again, because rows that were never inserted were never
  visible. If an insert's outcome was ambiguous (a timeout), the writer checks whether the batch's first id landed
  before re-stamping. Every batch also carries an `insert_deduplication_token`.

**Delivery waits a settle window.** The events server delivers events up to *now − 1 s*, on the ClickHouse clock,
in id order. An insert sent with a fresh id and acknowledged within the window is never behind a delivered id,
so resuming after any id a client was given misses nothing. That is the property ADR 0002 promises. On
PostgreSQL today, it rests on the 1,000-id look-back.

**A late event** is one whose insert was acknowledged more than 1 s after its id. Writers count it
(`queryapigate_events_late_total`). The events server keeps the ids it delivered over the last 30 s. Each check
compares that window's per-second counts with what it delivered, and sends any event it missed, out of order, as
the PostgreSQL look-back does now. So a live stream still gets every event. A client resuming from an id above a
late event can miss it. Section 9 requires the late count to be zero at full rate.

### 3. Three tables and two views, from one insert

A writer inserts one batch into `events`, and ClickHouse fans it out:

```sql
CREATE TABLE events (                        -- every event, as ADR 0020's table, plus what rollups count
  id UInt64 CODEC(Delta, ZSTD(1)),
  type LowCardinality(String), source LowCardinality(String),
  occurred_at DateTime, workspace LowCardinality(String),
  kind Enum8('saved' = 1, 'adhoc' = 2), query_name LowCardinality(String), version UInt32,
  key_name LowCardinality(String), connection_name LowCardinality(String), status LowCardinality(String),
  in_history Bool,                           -- false when QUERYAPIGATE_HISTORY_SAMPLE_RATE sampled it out
  rows UInt64, seconds Float32,
  entry String CODEC(ZSTD(3)),               -- a run's history entry, exactly as json.dumps writes it
  payload_json String CODEC(ZSTD(3)),        -- other event types' payload; empty for a run
  stamped_at DateTime MATERIALIZED toDateTime(1767225600 + intDiv(id, 4096000))
) ENGINE = MergeTree PARTITION BY toStartOfHour(stamped_at) ORDER BY id
  TTL stamped_at + INTERVAL 24 HOUR          -- QUERYAPIGATE_EVENTS_RETENTION_HOURS
  SETTINGS ttl_only_drop_parts = 1, non_replicated_deduplication_window = 1000;

CREATE TABLE history (...)                   -- the runs history keeps
  ENGINE = MergeTree PARTITION BY toYYYYMMDD(executed_at) ORDER BY (workspace, executed_at, id)
  TTL executed_at + INTERVAL 30 DAY;         -- QUERYAPIGATE_HISTORY_RETENTION_DAYS
CREATE MATERIALIZED VIEW history_mv TO history AS SELECT ... FROM events WHERE type = 'query.run' AND in_history;

CREATE TABLE run_minutes (...)               -- every run, counted: runs, rows, quantilesTDigest of seconds
  ENGINE = AggregatingMergeTree ORDER BY (workspace, minute, key_name, query_name, version, connection_name, ...)
  TTL minute + INTERVAL 7 DAY;
CREATE TABLE run_hours (...) ... TTL hour + INTERVAL 400 DAY;
CREATE MATERIALIZED VIEW run_minutes_mv TO run_minutes AS SELECT ... FROM events WHERE type = 'query.run' GROUP BY ...;
CREATE MATERIALIZED VIEW run_hours_mv TO run_hours AS SELECT ... FROM events WHERE type = 'query.run' GROUP BY ...;
```

- **A run's event payload is rebuilt when it's read,** by `events.run_payload_json()` from the columns and the
  entry, so it's byte for byte what the SQL stores send. The entry is stored once in `events` rather than twice. A
  run sampled out of history is still an event, as ADR 0020 requires, and is still counted in the rollups.
- **Retention is TTL.** Whole hourly or daily partitions are dropped. Python's history and event sweeps don't run
  against this store. `QUERYAPIGATE_HISTORY_RETENTION_DAYS` defaults to 30 here. Trimming to a count per version
  (`QUERYAPIGATE_HISTORY_LIMIT`) doesn't map to ClickHouse, so here that setting limits how many runs a read returns
  per version, as the Console shows them.
- **The tables are created and migrated by Python at startup.** A changed retention setting is applied with
  `ALTER TABLE ... MODIFY TTL`. The views use `SQL SECURITY DEFINER`, so a writer needs `INSERT` on `events` and nothing
  else.

*As built:* `events` also has `code`, `masked` and `statement` - what usage reports count by, written with the run so
no report parses an entry - and `written_at`, ClickHouse's own clock at the insert, which shows how late any writer's
insert was. `history_counts` (an `AggregatingMergeTree` filled by a view on `history`, with history's TTL) keeps each
day's runs and last run per query, version and key: the Console's counts. The rollups gained `code` and `statement`
dimensions and `masked`, `first` and `last`. No projections anywhere: on 24.8 they refuse the lightweight `DELETE`
`forget` needs.

### 4. Who writes

- **The data plane writes its runs directly.** It batches up to 100,000 runs or 250 ms, with one insert in flight
  and a bounded queue (200,000 runs, about 10 s at full rate). When the queue is full, runs are dropped and counted,
  never slowing a call. Its ClickHouse user can only insert into `events`. Its report to Python shrinks to what
  /metrics counts: per-label totals and histogram counts in /metrics' own buckets. Entries are sent only while the
  main server has live subscribers. Python stops parsing and queueing those runs, which removes the
  two-workers condition's cause.
- **Python writes the runs it serves** (forwarded calls, MCP, exports, the CLI) through history's existing queue and
  writer thread. Its batches are small and many processes send them, so it inserts with
  `async_insert=1, wait_for_async_insert=1`. ClickHouse then merges small inserts from many writers into fewer parts.
  A flush still means a caller's own runs are readable once it returns.
- **The audit log, definitions and quotas** are untouched.

*As built:* the data plane sends a batch per flush interval, up to 100,000 runs - at 500 a batch ClickHouse spent 1.6
cores merging parts, at this size 0.2. Python inserts synchronously, one insert per flush. The data plane's report
still carries each run: Python counts it for /metrics and the Console's live feed, but no longer queues it.

### 5. Who reads, and how

| Reader | Today | With the run store |
|---|---|---|
| `queryapigate events`, replay and tail | `events` by id, NOTIFY or polling | `events` by id, polled every `QUERYAPIGATE_EVENTS_POLL_INTERVAL`, up to the settle window (section 2) |
| `GET /api/v1/history` and `/queries/<name>/history` | SQL with filters, cursor `(executed_at, rowid)` | `history`, newest first, in widening windows (a minute, an hour, a day, then all). The cursor is `(executed_at, id)`, still opaque |
| The Console's runs per version | the newest `HISTORY_LIMIT` per version | the same, one windowed read a version |
| Usage reports | up to 100,000 history rows, summarised in Python | `run_minutes` or `run_hours`: exact, whatever the sample rate. Ad-hoc statements come from `history` |
| Alerts (error rate, speed, failing exports) | the newest 5,000 runs | rates and quantiles from the rollups; failures from `history` |
| `forget` (ADR 0016) | `DELETE` from both tables | lightweight `DELETE` from `events`, `history` and both rollups, counted first for `dry_run`, then purged from disk by `APPLY DELETED MASK`, one partition at a time |

- **Deleting a query or a workspace** deletes its rows from `history`. The delete is repeated after one flush
  interval plus the settle window, so runs still in a batch when it was deleted go too. The SQL stores do this with
  `ON DELETE CASCADE`.
- **Every read is scoped by `workspace`,** as in the SQL stores, and the workspace boundary tests run against this
  store too.
- **/metrics stays in memory.** Prometheus scrapes counters every 15 seconds, and serving them from queries would
  put a database on every scrape. The rollups give the history /metrics never kept.

*As built:* a usage report takes the window's whole minutes from `run_minutes` and only its partial minutes at either
end from `events`, found by id range, so it costs the same whatever the traffic and stays exact. Alerts still read
history's newest runs, as with the metadata store. A replay finds its ids first, up to 100,000 in one read and from
the key's creation on, then reads their rows a page at a time (measured at 250 million runs, section 9).
`queryapigate events` doesn't format an event no stream is listening for.

### 6. Switching it on and off

- **The SQL store records which run store is in use,** with the first id it handed out. A process whose setting
  disagrees refuses to start and says why, so two instances can't record runs in different places. Switching is a
  stop-and-restart of every instance, like `migrate-to-postgres`, not a rolling change.
- **On:** the first start copies the event log's retention window into ClickHouse with its ids, so a client that
  disconnected just before the switch resumes. History is copied by a command,
  `queryapigate runs copy-to-clickhouse`, because it can be months of rows.
- **Off:** the SQL store's event sequence is set above the newest ClickHouse id, so ids keep increasing. ClickHouse's
  history isn't copied back. The documentation says to export it first if it's needed.

### 7. Security

- A new outbound connection, with its own credentials. The data plane's credentials are insert-only, and Python's
  are limited to this database.
- **Erasure (ADR 0016)** reaches all four tables. The rollups hold key names, so they're included. Erased rows are
  hidden at once and gone from disk when the purge finishes. The data-flow inventory lists the store.
- The threat model gains an entry: what a stolen run-store credential reads (entries, not results) or forges
  (runs and events, not definitions).

#### Reviewed (2026-10-28), and what changed

A review of 0.45.0's run store: Python's and the data plane's code, the views and grants, against the threat model.
Parameterisation, transport and credential handling, the insert-only user, the lease's gate, the marker on every
Python entry point and the scoping of replay and delivery held. What it found, each fixed with a test that fails
without the fix:

1. **A row could split an event stream** (medium). The events server wrote a stored payload into its frame as it
   was, so a line break in a row - from anyone with an insert credential, the data plane's included - made frames of
   the writer's own, with an `id:` that moved every client's resume point. A frame's data is one line now (a break
   becomes a space, which means the same between JSON tokens), and `events` has a check constraint: ids at most
   2^53 - 1.
2. **The data plane's run store wasn't checked** (medium). Python's instances refuse to start against another run
   store than the metadata store records (section 6); the data plane read only its own URL. The lease now carries
   the location, and a data plane whose own differs writes none of its runs - counted as dropped, and logged - until
   they agree.
3. **Ad-hoc statements outlived the retention settings** (medium, data protection). `run_hours` kept their text for
   400 days and `run_minutes` for 7, though only `run_minutes` is read, and only within the event log's retention.
   `run_hours`' view hashes them now (SHA-256, the `sha:` form of `QUERYAPIGATE_HISTORY_ADHOC_SQL=hash`), and
   `run_minutes` expires with the event log.
4. **`forget` could miss runs in flight** (low): it deletes a second time once a flush interval and the settle
   window have passed, as deleting a query's runs already did.
5. **`leave` trusted the newest id** (low): it now continues from the newest id up to the clock and a writer's lead.
6. **A forged history cursor was a 500** (low): it's a 400.

Schema version 2 (`run_store_schema`) is reached at the first start, under a lock in the metadata store, with the
grants guide 65 gives (no `DROP`): the constraint added, `run_hours_mv` changed by `MODIFY QUERY`, and each of
`run_hours`' rows that holds a statement's text inserted again hashed, then deleted. That runs at any start that
finds such a row, for an insert that was under way as the view changed.

### 8. What the SQL stores keep

Nothing changes for them. The bound the event log needs at the data plane's rate (BACKLOG #102, a row cap or
sampling) is a separate, smaller change, needed whatever this ADR decides. If time-ordered ids and the settle
window work here, they could replace the 1,000-id look-back on PostgreSQL too. That's an open question, not part of
this decision.

### 9. Prototype, then a measured decision

Built on one branch, as 0028 was. It is measured at the data plane's top rates on Performance v5's laptop, with
ClickHouse on the same host:

1. **Every run recorded:** 0 dropped and 0 late events at 20,000 runs a second (PostgreSQL connection, fixed rate)
   and at the MySQL 64-caller peak, for 30 minutes.
2. **Cheaper gateway:** the data plane's CPU per call with direct writes no more than 10% above 0.44's, and the
   Python workers' CPU for those runs down to the totals report. With one Python worker, 100% of runs recorded.
3. **Storage:** at most 40 bytes a run in `events` and in `history` each, over at least 250 million runs.
4. **Delivery:** live events at p99 within flush + settle + 0.5 s. Under full load, clients that disconnect and
   resume at random each see every id exactly once.
5. **Reads at 250 million runs:** first history page, one key's page and the Console's version list at p95 ≤
   100 ms; a 24-hour usage report at p95 ≤ 500 ms; a one-key replay of 24 hours ≤ 10 s in all.
6. **Same answers:** the differential test runs the same calls against a PostgreSQL store and a ClickHouse store.
   Event payloads and history entries match byte for byte, and `/api/v1/history` pages match.

250 million runs is about 12 GB across the tables, sized to this laptop's free disk with room to spare. Each run
checks the space first, as the 0.44 benchmarks do.

- **Go:** all six. The run store ships as experimental, and graduates after a release unchanged. *Decided: go,
  2026-10-28.*
- **Not met:** the prototype and its results stay as a documented experiment. The SQL stores' bound (section 8)
  ships regardless.

#### Result: every criterion met, at the sizes measured

Measured on 2026-10-28 ([results](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/benchmarks/results/runstore-2026-10-28/README.md)),
the data plane in front in both builds: 0.44's, recording through Python on the PostgreSQL store with two workers,
against the run store with one.

| Criterion | Measured |
|---|---|
| 1. Every run recorded at 20,000/s and at the peak | 35,999,897 of 35,999,897 calls in 30 minutes, 0 late (insert lag p99 13 ms, max 37 ms). At the 64-caller peak: 100%, where 0.44's store kept 52-54% |
| 2. Cheaper gateway | -11% gateway cores at 20,000/s; Python 0.49 to 0.13 cores; the data plane's own CPU per call unchanged (2.91 and 2.89 cores). One worker records everything |
| 3. Storage ≤ 40 B a run | 13.0 B in `events`, 13.4 B in `history`, at 72.3 million runs |
| 4. Delivery and resume | 35 million events to clients resuming at random over 7,515 reconnects: none missing, repeated or out of order. At 2,000/s, p99 1.35 s from id to arrival |
| 5. Reads, p95 | At 107.6 million runs: history pages 8-13 ms, the Console's versions 6.7 ms, a 24-hour usage report 16.9 ms, a filtered replay of the whole log 8.8 s |
| 6. Same answers | The run store differential, three seeds: the same history, Console lists and event payloads byte for byte |

**What differs from the plan:**
- **Size:** 72.3 million runs for storage and 107.6 million for reads - the latter real runs doubled through the same
  views - not 250 million: the laptop had 13 GB free.
- **The peak** is PostgreSQL at 64 callers, not MySQL's.
- **Delivery speed** was measured at 2,000/s. At 20,000/s one `queryapigate events` process delivers about 10,000
  events a second per stream, whichever store it reads: the soak's clients fell behind and resumed, which tested
  resume, not speed. That ceiling is the events server's, and an open question below.
- **Three reads missed at first,** each fixed from ClickHouse's query log, and are what *as built* in sections 3 and
  5 describe: usage 4,991 ms (JSON parsed per run), the Console's version list 723 ms (the whole history scanned for
  counts), the replay 41 s (`entry` read for nearly every granule).

#### At 250 million runs (2026-10-29)

Measured on `main` with the review's fixes ([results](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/benchmarks/results/runstore-250m-2026-10-29/README.md)),
the laptop and set-up as before, at 250,029,885 runs (and again at 250,028,467): a 30-minute soak's real runs,
doubled twice and copied once in part through the same views.

| Criterion | Measured |
|---|---|
| 3. Storage ≤ 40 B a run | 13.8 B in `events`, 13.7 B in `history`: 6.9 GB for 134 GB of entries |
| 5. History pages, the Console's versions, p95 ≤ 100 ms | 12.5 ms (first page), 73.6 ms (one key's), 37.3 ms (versions) |
| 5. Usage report, 24 hours, p95 ≤ 500 ms | 66.7 ms |
| 5. One key's replay ≤ 10 s | **11.8 s: missed;** 6.2 s once a replay finds its ids in batches |

The replay missed by 18%: finding each page's 1,000 ids was a read of its own, 0.1-0.2 s of planning and index work
at this size whatever it found - 7.0 s of 11.3 - where all of them came back in one read in 0.5 s. A run store
replay now finds up to 100,000 ids in one read, bounded below by the key's creation, and fetches their rows a page at
a time (section 5): on the same runs, 12.0 s to 6.2 s for a key's, 9.1 to 5.7 s for a query's, and no slower when
most events match. The soak again recorded every run with none late, and its resuming clients missed and repeated
nothing; the id check schema 2 adds to each insert cost nothing measurable.

## Alternatives considered

- **Partitioned PostgreSQL** (hourly partitions dropped for retention, `COPY` ingestion, BRIN indexes). No new
  system, but no compression: at a few hundred bytes a row, a day of events is hundreds of gigabytes against about
  41 GB here (24 bytes × 1.7 billion). A day's usage report would still scan the day.
- **TimescaleDB:** compression and continuous aggregates, but as an extension most managed PostgreSQL services
  don't offer, under a source-available licence for those features.
- **The data plane reports to Python, and Python writes ClickHouse.** One writer and one schema owner, with no
  ClickHouse client in Rust. But Python stays in every run's path, at about 17,000 runs a second per process.
- **Kafka or Redpanda for the event log,** ClickHouse for history. Offsets would make resume exact, but it's a second
  system to run, for every deployment that wants this. It could be a sink later.
- **Parquet on object storage.** Cheap for long history, but there's no live tail, and erasure means rewriting files.
- **Ids from ClickHouse** (a `DEFAULT now64()` column). Every row in a block gets the same time, and concurrent inserts
  still commit in any order. Nothing is gained over writer-stamped ids.
- **Ids from the SQL store's sequence, handed out in blocks.** Unique, and increasing within a block. But blocks from
  different writers interleave in time, so an id's order is no longer the order rows become visible, which resume
  depends on.
- **64-bit ids,** with more bits for time and writers. Ids above 2^53 lose precision in JavaScript, where the docs
  let clients store and compare them.
- **/metrics served from ClickHouse:** rejected in section 5.
- **clickhouse-driver** (the connection extra's native protocol) or clickhouse-connect for Python. Rust's official
  crate speaks HTTP only, so the native protocol would mean two protocols and two ports. The standard library over
  HTTP keeps one URL and no new dependency.
- **One table for events and history,** with `TTL ... DELETE WHERE NOT in_history`. Each run would be stored once,
  but expiring the non-history rows rewrites parts in merges instead of dropping whole partitions, and history's
  reads want their own sort. The view stores a kept run twice, for the event log's 24 hours only.
- **Projections on `history`:** measured, and rejected for erasure's sake (Context).

## Consequences

- **A third store, optional,** for deployments at the data plane's rates. The default deployment is unchanged:
  SQLite, or one PostgreSQL.
- **Live events arrive about a second later:** the flush interval plus the 1 s settle window, against the flush
  interval alone today. That's the cost of making ADR 0002's promise hold at 20,000 events a second.
- **Usage reports become exact** with the run store, whatever history's sample rate (alerts, as built, still read
  history's newest runs). At these rates
  history's sample rate is then only about keeping example runs, and can be set low.
- The run store is two writers sharing one schema. The data plane's row format joins the differential test, as its
  history entries already have.
- Schema 18 in the SQL stores: the writer leases and the run-store marker. Additive, as schema 16 was.
- Docker and Helm: ClickHouse is the operator's to run. The shipping ADR that follows 0028 decides how the chart
  points at one.

## Open questions

- **Self-managed replicated clusters:** a read from a replica behind the one that took the insert could miss the
  settle window. `select_sequential_consistency`, or reading from the inserting replica, needs measuring.
- **Should the main server's `GET /events` tail the store** too, instead of its in-process broadcaster?
- **Time-ordered ids for the SQL stores**, replacing the 1,000-id look-back (section 8).
- **The rollups' grains and defaults** (by minute for the event log's retention, by hour for 400 days), once real
  traffic's combinations per minute are known.
- **The events server's ceiling:** about 10,000 events a second per stream from one process, below what a data plane
  serves. Several processes behind a load balancer each tail the store already; whether one process should serve
  more is a separate measurement.
- **The events server under a soak:** each stream delivered about 6,500 events a second in the 250-million-run
  soaks, against about 9,700 the day before, on a busier or hotter laptop; its clients fell behind and resumed
  through the replay, faster now. A soak with the batched replay is the next measurement of that ceiling.

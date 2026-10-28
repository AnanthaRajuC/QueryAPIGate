# The ClickHouse run store, measured (ADR 0029 §9, 2026-10-28)

`benchmarks/oltp.py` blocks r1-r7, on Performance v5's laptop: AC power, the `performance` profile, the gateway on
CPUs 0-7, PostgreSQL 16 and ClickHouse 24.8 sharing 8-11, the load generator (oha) on 12-19. Both builds have the
Rust data plane in front, with the metadata cache. **SQL store** is 0.44's: runs reported to Python and written to
the PostgreSQL store, two gunicorn workers behind it. **Run store** is this branch: the data plane writes its runs to
ClickHouse itself, one gunicorn worker behind it. Rows are in `results.jsonl`; a cell is the median of 3 rounds of
30 s unless it says otherwise.

**Not as section 9 planned:**
- **Size.** The laptop had 13 GB free, not the ~12 GB plus headroom 250 million runs need. Storage was measured at
  72 million runs (a 30-minute soak and one copy of it), the reads at 107.6 million (r6's real runs doubled until
  then, through the same views - synthetic copies of real runs).
- **The peak.** PostgreSQL at 64 callers, not the MySQL 64-caller peak.
- **The soak's latency** has the events server and its two clients on the load generator's cores.
- `results-invalid-401.jsonl` is a run whose calls all got 401 (keys from an earlier store): oha counts any answer
  as a success, so it looked fine. Kept for the record; nothing here is read from it, and the harness now fails a
  cell with more than 1% of answers that aren't 200.

## 1. Every run recorded (r1, r3)

| 20,000 calls a second | SQL store | Run store |
|---|---|---|
| Runs recorded, 30 s cells | 100% | 100% |
| Runs recorded, 30-minute soak | - | 35,999,897 of 35,999,897 calls |
| Late events (inserted over 1 s after their id's time) | - | 0, soak included |
| Insert lag (id to ClickHouse's own `written_at`) | - | p99 6 ms (r1), 13 ms (soak), max 37 ms |
| Latency p50 / p99 | 0.62 / 2.40 ms | 0.55 / 2.80 ms |

## 2. The gateway (r1, r2)

| | SQL store | Run store |
|---|---|---|
| Gateway cores at 20,000/s | 3.40 | **3.02** (-11%) |
| of which Python | 0.49 | **0.13** (-73%) |
| of which the data plane | 2.91 | 2.89 |
| PostgreSQL cores / ClickHouse cores | 2.11 / - | 1.54 / 0.20 |
| Capacity, plain lookups, 64 callers | 34,240/s, **52% of runs recorded** | 35,501/s, **100% recorded** |
| Capacity, governed lookups, 64 callers | 29,543/s, **54% of runs recorded** | 33,530/s, **100% recorded** |

The data plane's CPU per call is unchanged (2.91 and 2.89 cores at the same rate): criterion 2 asks for at most
+10%. Before the batch fix (inserts of 500 runs, 40 a second), ClickHouse spent 1.6 cores merging the parts; with
inserts of up to 100,000 runs, 0.20.

## 3. Storage (r4, at 72.3 million runs)

| Table | Rows | Compressed | Bytes a run |
|---|---|---|---|
| `events` | 72,340,716 | 943 MB | **13.0** |
| `history` (sample rate 1) | 72,340,716 | 968 MB | **13.4** |
| `history_counts` | 3 | 670 B | - |
| `run_minutes` / `run_hours` | 94 / 6 | 344 KB / 29 KB | - |

20.3 GB of entries raw, 21x smaller. One day at 20,000 runs a second is about 22 GB in `events` (24 hours) and the
same again per day of history kept at sample rate 1.

## 4. Delivery and resume (r3, r6)

- **Resume:** during the soak, two clients hanging up every 3 to 10 seconds and resuming with Last-Event-ID received
  35,084,849 events over 7,515 reconnects, every one checked against ClickHouse: **0 missing, 0 repeated, 0 out of
  order.** At 2,000/s: 524,162 events over 46 reconnects, the same.
- **Speed, at 2,000/s (r6):** from an event's id to its arrival, **p50 1.20 s, p99 1.35 s, max 1.38 s** once the
  events server had caught up with the warm-up's events (1.6 s; 3.2 s before it stopped formatting events nobody
  listens to). The 1 s settle window is most of it, as designed.
- **The events server's own ceiling:** at 20,000/s each stream got about 10,000 events a second and fell behind - so
  in the soak the clients resumed from further and further back, which tested resume well and delivery speed not at
  all. One `queryapigate events` process delivers about 10,000 events a second per stream, whichever store it reads.

## 5. Reads (r5, at 107.6 million runs)

Twenty sequential GETs each, through Python, after two to warm:

| Read | p50 | p95 | First measured |
|---|---|---|---|
| `GET /api/v1/history`, newest 100 | 10.8 ms | 11.8 ms | 12.8 ms |
| ... one key's newest 100 | 12.7 ms | 13.3 ms | 63.8 ms |
| ... a page from the oldest hour | 7.3 ms | 7.9 ms | 7.0 ms |
| `GET /api/v1/queries/account` (the Console's versions and counts) | 6.0 ms | 6.7 ms | **723 ms** |
| `GET /api/v1/usage/agents?hours=24` (about 27 million MCP runs in it: a quarter) | 16.1 ms | 16.9 ms | **4,991 ms** |
| Replay of the whole log filtered to one query (44,491 events) | 8.8 s | - | **41.5 s** |

*First measured* is the first r5, at 72.3 million runs - the soak's and one copy, all of the copy MCP traffic: a
different set, so read it as the order of magnitude a change took, not a like-for-like pair. What changed between
the two, each from ClickHouse's query log:
- **Usage** parsed every run's JSON entry: the code, masking and statement became columns written with the run
  (2,125 ms), then whole minutes came from `run_minutes` and only the window's partial minutes from `events`, found
  by id range (16 ms) - exact, as a test checks against counting every run.
- **The Console** scanned the query's whole history for its run counts and last run: `history_counts`, filled by a
  view on `history` with history's own TTL, now holds both.
- **The replay** read `entry` for nearly every granule to find 1 match in 2,400: it now finds a page's ids first,
  then reads those rows.

## 6. The same answers

`tests/test_runstore_differential.py`, seeds 29, 7 and 2026: 120 random calls each - saved queries over two
versions, failures, ad-hoc SQL, three callers - against a server on the metadata store and one on the run store. The
same history page for page under six filters, the same Console lists and query details, and every event payload
byte for byte, leaving out only times and request ids.

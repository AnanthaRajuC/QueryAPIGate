# Live queries under load (ADR 0033 §7, item 2, 2026-11-16)

`benchmarks/live.py` blocks m1-m5, on `main` at `00c34c7` (after 0.50.1), on the benchmark laptop: AC power,
the `performance` profile, each part on its own cores - gunicorn (2 workers x 8 threads, as the image runs it) on CPUs
0-3, `queryapigate events` on 4-7, PostgreSQL 16 on 8-11 (the data and the metadata store), the load on the E-cores
12-19 (oha, the subscribers, the writer). Live queries on, `QUERYAPIGATE_LIVE_MIN_INTERVAL` at its default, 1 second.
Rows are in `results.jsonl`.

**Debezium isn't in these numbers.** The harness plays it: right after each write commits it posts the change to
`POST /ingest/cdc/shop`, signed as Debezium Server signs. So a lag here is QueryAPIGate's share - signal to stream -
plus the write. Debezium's own, polling PostgreSQL's log, is 0.5 to 1.7 s (ADR 0033 §9).

The saved query is `SELECT id, name, price, stock FROM item WHERE id = :id`, run by a key that may run only it. A
*group* is one caller's subscription to one item: streams on the same item share it, and one run.

## Summary

| Question (ADR §7.2) | Answer |
|---|---|
| Re-run cost per group under a write storm (m1) | **0.75 ms** of the events server's CPU and **0.12 ms** of PostgreSQL's a re-run; none of the gateway's |
| How many groups one events server keeps fresh (m5) | **1,000 groups**, each re-run 0.96 times a second (as often as the 1 s interval lets), at 0.75 of a core - about 1,300 re-runs a second before its one Python core runs out |
| Delivery lag, 1,000 subscriptions across 100 groups (m2) | Commit to arrival **p50 51 ms, p99 86 ms** (median of 20 rounds); every stream, every round |
| Does `/q/` latency move while re-runs happen? (m3) | **No.** p50 8.19 vs 8.24 ms, p99 13.24 vs 13.24 ms, throughput +15 req/s (±62), re-running vs not |
| Ingest throughput, batches of 200 (m4) | **1,200-1,470 posts a second** (240,000-290,000 changes a second) on two gunicorn workers, CPU-bound: 1.55-1.9 ms of gateway CPU a post |

## m1: what a re-run costs

100 groups (one stream each, items 1-100). A storm for 30 s: 200 single-row `UPDATE`s a second to those items, and
20 posts a second of 20 changes each - Debezium batches, so a storm is a few posts a second naming many rows. The same
storm twice, interleaved over 5 rounds: once naming `public.item` (every group re-runs), once naming `public.other`
(no stream reads it: no re-run). Everything else is equal, so the difference is what the re-runs cost.

| Per 30 s cell (mean of 5) | Signals name `item` | Signals name `other` | Per re-run |
|---|---|---|---|
| Re-runs | 2,900 (97 a second) | 0 | - |
| Results pushed (changed) | 2,496 (86%) | 0 | - |
| Events server CPU | 3.15 s | 0.96 s | **0.75 ms** |
| PostgreSQL CPU | 2.43 s | 2.09 s | **0.12 ms** |
| Gateway CPU | 0.82 s | 0.82 s | 0 |

- **The interval bounds it.** 6,000 writes and 600 posts in 30 s became 2,900 re-runs: each group at most once a
  second, however many changes arrived. Nothing was postponed (no rate limit on the key).
- **A re-run's own query is cheap; the run around it is the cost** - running the saved query as the subscriber
  through `run_saved()` (grants, masking, the run's history entry), hashing the result, and sending it. Most of the
  0.75 ms is Python in the events server.
- A signal that concerns no stream still costs the events server something: 0.96 s for 600 signals and 100 idle
  streams. In m4, with no stream at all, 1,450 signals a second kept it at 0.72 of a core - about 0.5 ms a signal,
  hearing and reading it. Debezium's batching keeps real rates to a few a second.

## m5: how many groups one events server keeps fresh

m1's storm on 1,000 groups, 3 rounds of 30 s:

| | Round 0 | Round 1 | Round 2 |
|---|---|---|---|
| Re-runs a second (of 1,000 possible) | 959 | 958 | 961 |
| Events server cores | 0.74 | 0.76 | 0.75 |
| PostgreSQL cores | 0.15 | 0.17 | 0.15 |

Every group re-ran as often as the interval allows - the same 96% of it as m1's 100 groups (a run's own time delays
the next interval's start). The events server is one Python process, so one core is its ceiling: at 0.75 ms a
re-run, about **1,300 re-runs a second**, or 1,300 groups changing every second. Past that, run more events servers
(they share the store, and each hears every signal) and spread the subscribers across them.

## m2: delivery lag

1,000 streams: 10 on each of items 1-100, so 100 groups. Each round, one `UPDATE` changes all 100 rows, then the
post says `public.item` changed. Measured from the commit to each stream's new result, 20 rounds, 1.5 s apart (past
the interval):

| Commit to arrival | Median of 20 rounds | Best round | Worst round |
|---|---|---|---|
| p50 | **51 ms** | 49 ms | 72 ms |
| p90 | 81 ms | 79 ms | 102 ms |
| p99 | **86 ms** | 83 ms | 106 ms |
| Slowest stream | 86 ms | 83 ms | 106 ms |
| The post itself (signed, stored, answered) | 3.2 ms | 2.9 ms | 9.1 ms |

All 1,000 streams got the new result in every round. The spread is the 100 re-runs queueing for the events server's
8 re-run workers: a group that runs last waits for the others. The events server's CPU for a round was 0.09 s.

With Debezium in front, a change made in PostgreSQL reaches a stream in Debezium's 0.5-1.7 s plus these 50-90 ms.

## m3: GET /q/ while re-runs happen

oha on `GET /q/item?id=<1-9999>`, 16 callers, 20 s, during m1's storm, with 100 groups subscribed. Interleaved over
6 rounds, the storm's posts name `item` (100 re-runs a second) or `other` (none):

| | Re-running | Not | Paired difference (mean ± sd) |
|---|---|---|---|
| Requests a second | 1,944 | 1,929 | +15 ± 62 |
| p50 | 8.19 ms | 8.24 ms | -0.05 ± 0.26 ms |
| p90 | 10.48 ms | 10.48 ms | 0.00 ± 0.84 ms |
| p99 | 13.24 ms | 13.24 ms | 0.00 ± 0.84 ms |
| p99.9 | 16.89 ms | 17.02 ms | -0.12 ± 1.41 ms |

No difference beyond the noise. Re-runs happen in the events server, on its own cores, and add 0.12 ms of database
CPU each - 12 ms a second at 100 re-runs a second. (The ~1,900 requests a second is two gunicorn workers on four
hyperthreads, with a storm running; it isn't this laptop's ceiling for `/q/`.)

## m4: ingest throughput

oha posting one batch of 200 change events (Debezium's JSON, with schemas) to `/ingest/cdc/shop`, 20 s per cell, no
stream open, 3 rounds, interleaved:

| | 8 at once | 32 at once |
|---|---|---|
| Posts a second | 1,471 / 1,338 / 1,215 | 1,451 / 1,428 / 1,215 |
| Changes a second | 294,000 / 268,000 / 243,000 | 290,000 / 286,000 / 243,000 |
| p50 / p99 (round 0) | 5.2 / 15.3 ms | 21.3 / 35.6 ms |
| Gateway CPU a post | 1.55-1.87 ms | 1.57-1.88 ms |
| PostgreSQL CPU a post | 0.40-0.45 ms | 0.39-0.46 ms |

- **CPU-bound on the gateway:** 8 and 32 callers post at the same rate, two workers' worth (about 2.3 cores across
  them). Each post is a signature over the body, parsing 200 events into table names, and one signal row - its
  answer waits until that row is stored.
- **It fell across the rounds** (1,471 to 1,215) as the package reached 100 C in round 1: the gateway's CPU a post
  rose with it. Thermal, not the server: the database's CPU a post stayed flat.
- Debezium Server posts one batch at a time per source, a few a second under a heavy write load. This is two orders
  of magnitude above that.

## Not measured

- Debezium itself (ADR 0033 §9: 0.5-1.7 s from the `UPDATE`, mostly its polling).
- A Redis-shared rate limiter, signed-in users, masking, other databases - each re-run is an ordinary run, measured
  for `/q/` elsewhere ([PERFORMANCE_V2.md](../../../documentation/PERFORMANCE_V2.md)).
- More than one events server.

## Re-running it

```bash
python benchmarks/live.py --out /tmp/qag-live setup all teardown   # about 15 minutes; --smoke checks the mechanics
```

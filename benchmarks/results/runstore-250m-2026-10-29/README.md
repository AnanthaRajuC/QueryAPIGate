# The ClickHouse run store at 250 million runs (ADR 0029 §9, 2026-10-29)

The measurement [the first one](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/benchmarks/results/runstore-2026-10-28/README.md)
couldn't make for want of disk: section 9's storage and reads at 250 million runs. `benchmarks/oltp.py` blocks
r1, r3, r7 and r5 on the same laptop, set up the same way: AC power, the `performance` profile, the gateway on CPUs
0-7, PostgreSQL 16 and ClickHouse 24.8 sharing 8-11, the load generator on 12-19. The build is `main` at b90bc1d,
which has the security review's fixes (#121, the run store's schema 2), with the Rust data plane in front.

The store was grown as before: a 30-minute soak at 20,000 calls a second (36 million real runs), then doubled twice
and copied once in part (r7 `--runs 250e6`) - synthetic copies of real runs, each shifted back in ids and time,
through the same views. About one run in 10,000 belongs to a rare query and one to a rare key, for the replays. The
store was merged (`OPTIMIZE FINAL`) before its bytes were measured.

- `results.jsonl`: the run, start to end, to 250,029,885 runs.
- `results-rebuild.jsonl`: the same steps again, to 250,028,467 runs (the first run's teardown deleted its store
  before the replay could be worked on), then r5 twice on it: `main`, and `main` with the replay change below. The
  `build` field on its r5 rows was added afterwards, from their times; r5 doesn't record it.

## Section 9, at 250 million runs

| Criterion | Target | Result | |
|---|---|---|---|
| 3. Storage, `events` | ≤ 40 B/run | **13.8 B/run** (3.44 GB) | ✓ |
| 3. Storage, `history` (sample rate 1) | ≤ 40 B/run | **13.7 B/run** (3.43 GB) | ✓ |
| 5. First history page, p95 | ≤ 100 ms | 12.5 ms | ✓ |
| 5. One key's history page, p95 | ≤ 100 ms | 73.6 ms | ✓ |
| 5. The Console's versions, p95 | ≤ 100 ms | 37.3 ms (5.9 in the rebuild) | ✓ |
| 5. Usage report, 24 hours, p95 | ≤ 500 ms | 66.7 ms (16.7 in the rebuild) | ✓ |
| 5. One key's replay of the log | ≤ 10 s | 11.8 s; **6.2 s** with the replay change | ✓ with it |

134 GB of entries raw in 6.9 GB: bytes a run didn't grow from 72 million (13.0 and 13.4). One key's page is 5x
what it was at 107.6 million because that key's runs are only in the copies, older than the soak: the read finds
none in its newest minute and hour and reads the day. The two p95s that differ between the runs are one slow GET of
twenty.

## The replay

A replay found each page's 1,000 ids in a read of its own, then fetched their rows. At 250 million runs a read
costs 0.1-0.2 s of planning and index work whatever it finds; all 34,893 of the rare key's ids came back in one read
in 0.5 s (1.0 s with the key's creation date as a filter), against 7.0 s over 35 pages. Its rows, a page at a time,
were 4.4 s. Now a replay finds up to 100,000 ids in one read and fetches their rows a page at a time, and a key's
creation bounds the ids it reads, as it already bounded the times (`runstore.replay_pages()`).

On the same 250 million runs, r5 on `main` and then with the change:

| Replay (from before the oldest event) | `main` | With the change |
|---|---|---|
| One key's, 34,893 events | 12.0 s | **6.2 s** |
| One query's, 35,997 events | 9.1 s | **5.7 s** |

A busy replay is no slower: with every event matching, a read of 100,000 ids took 7 ms against 5 ms for 1,000; with
half matching, 186 ms against 149 ms. What's left of the replay is mostly fetching the rows, each in a different
granule.

## Writing, again (r1, r3)

| | 0.45.0 (2026-10-28) | `main` |
|---|---|---|
| ClickHouse cores at 20,000/s, r1 median | 0.20 | **0.18** |
| Gateway cores, run store, r1 median | 3.02 | 2.68 |
| Runs recorded, 30-minute soak | 35,999,897 of 35,999,897 | 36,151,000 (and 36.2 million in the rebuild): every call |
| Late events | 0 | 0, in both soaks |
| Insert lag p99, soak | 13 ms | 27 ms (26 in the rebuild) |
| Resume, soak | 0 missing, 0 repeated | 0 missing, 0 repeated, 0 out of order: 47.8 million events over 22,088 reconnects, both soaks |

The id check schema 2 adds to every insert costs nothing measurable (a comparison across days: the laptop's
variance is larger than the difference). The SQL store's r1 median for comparison: 2.96 gateway cores.

**Not settled here:** in both soaks each event stream delivered about 6,500 events a second, against about 9,700 on
0.45.0, and its latency was higher (p50 1.1-1.3 ms against 0.55 ms) - the same laptop, busier or hotter. It isn't the
review's line-break handling: that costs 42 ns an event (`_format()`, 291 against 333 ns; building the payload is
3.9 µs). The soak's clients fell behind and resumed through the replay, which this change makes faster; a soak with
it is the next measurement of the events server's ceiling (ADR 0029, open questions).

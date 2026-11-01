# Rate-limited keys through the data plane (ADR 0032 §8.3, 2026-11-01)

[ADR 0032](../../../documentation/adr/0032-dataplane-rate-limits.md)'s measurement: a key with a rate limit, its
lookups served by the data plane counting in Redis, against the same key on Python alone and an unlimited key through
the data plane. `benchmarks/oltp.py` blocks `l1` (32 callers) and `l2` (one caller), on the same laptop as every
result here (i7-12700H, 32 GB), set up as for [PERFORMANCE_V2.md](../../../documentation/PERFORMANCE_V2.md): AC power,
the `performance` profile, the gateway on CPUs 0-7, PostgreSQL 16 (pgbench scale 100, and the store) on 8-11, oha on
12-19. Redis 7.4.11 runs on the database's CPUs, on loopback. The commit is `feat/dataplane-rate-limits` at `797b2a8`;
the data plane is its `cargo build --release`.

Every build has the metadata cache on and Redis set (`QUERYAPIGATE_REDIS_URL`). The limited key's limit is
`1000000000/second`: every call is counted and none refused, so this measures what counting costs. `+limit` adds
`QUERYAPIGATE_RATE_LIMIT` with the same limit, so each call counts two buckets: the client's, then the key's. Python
alone is one worker; behind the data plane, two. Cells are 30 s (`l1`) or 20 s (`l2`), 5 rounds, rotated. Every call
answered 200, and Redis never failed.

- `results.jsonl`: every cell, and the `/health` floors.
- `results.md`: `benchmarks/oltp_report.py`'s tables from it.
- `run.txt`: the run's log.

## Per gateway core, 32 callers (`l1`)

The criterion: the limited key through the data plane serves at least 3x Python's calls a second per gateway core
(CPUs 0-7, Python's and the data plane's CPU together). The median of 5 rounds, with the range:

| Limits counted | Python alone | Through the data plane | Ratio |
|---|---|---|---|
| The key's | 809 (806-811) | 3,560 (3,527-5,189) | **4.4x** |
| The key's and the server-wide | 728 (721-731) | 3,616 (3,610-3,660) | **5.0x** |
| None (the unlimited key) | - | 3,820 (3,792-3,968) | - |

**It passes.** The ratio is conservative: Python alone has the gateway's CPUs mostly to itself and ran at about
4,100 MHz; the data plane cells use six of them and ran at 2,340-2,490 MHz. With Redis's CPU added to the gateway's,
the key's limit alone is still 4.2x: 3,262 against 775.

Round 0's limited cell is the outlier at the top of its range, 31,020 calls a second: the gateway ran at 3,460 MHz in
that cell, against 2,343-2,391 MHz in the other four. The median is unaffected.

## What counting costs

| 32 callers | Calls a second | p50 ms | p99 ms | Gateway cores | Redis cores | Redis CPU per call |
|---|---|---|---|---|---|---|
| Unlimited key | 25,406 (25,133-28,121) | 1.203 | 2.56 | 6.65 | 0.003 | - |
| The key's limit | 21,678 (21,245-31,020) | 1.390 | 3.56 | 6.07 | 0.541 | 25.0 µs |
| The key's and the server-wide | 18,122 (17,913-18,257) | 1.712 | 3.32 | 5.01 | 0.685 | 37.7 µs |

| One caller (`l2`) | Calls a second | p50 ms | p99 ms | Redis CPU per call |
|---|---|---|---|---|
| Unlimited key | 8,132 (7,943-8,490) | 0.110 | 0.27 | - |
| The key's limit | 5,720 (5,693-5,820) | 0.161 | 0.34 | 16.7 µs |
| The key's and the server-wide | 4,836 (4,753-4,856) | 0.193 | 0.38 | 29.0 µs |

- **One Redis round trip adds about 51 µs to a call** at one caller (p50 0.110 to 0.161 ms), and the second about 32 µs
  more. That's on loopback; a network hop to Redis adds its own round trip to each.
- **Per gateway core, counting costs about 7%** (3,820 to 3,560). That's an upper bound: in the unlimited key's cells
  Python fell behind recording the runs (55-100% recorded, against 93-100% for the limited key), which flatters the
  unlimited key's CPU.
- **At 32 callers, calls a second drop more than CPU does:** 15% with one bucket, 29% with two, while the gateway's
  CPU falls too (6.65 to 5.01 cores). The calls wait on Redis, not on the gateway: one Redis thread, busy 54-69% of the
  time, serves every call's script in turn. A pod that needs those calls back has CPU to spare for more callers.
- **Redis's own CPU is 17-25 µs a call for one bucket**, the cost of running the Lua script. At 21,678 calls a second
  that's about half a core of Redis.

## For ADR 0032's open question

*Both buckets in one round trip:* with the server-wide limit, the second round trip costs 32 µs at one caller, and at
32 callers 16% of the calls a second (21,678 to 18,122) with the same calls a second per gateway core. It's a latency
and throughput cost, not a CPU one. Measured here; whether that's worth a second script is left open.

# Benchmarks

Four scripts, each answering one specific question against real databases, one dialect at a time. None of
this is wired into CI - it is run manually, on demand, before a release or when a driver changes, never on
every push/PR (a memory/timing measurement on a shared CI runner is exactly the kind of thing that's flaky,
and pulling database images on every push would slow down every PR for something that only needs checking
occasionally).

| Script | Question | Needs a concurrent client? |
|---|---|---|
| `run.py` | Buffered vs. streamed: memory and latency for one large flat `SELECT` | No |
| `latency.py` | General API overhead: p50/p95/p99 for a small `GET /q/<name>`/`POST /execute_sql` call | No |
| `pooling.py` | How throughput and latency change as concurrent callers increase relative to the pool | Yes |
| `caching.py` | Cache hit vs. miss latency delta for a `cache_ttl`-carrying saved query | No |

Shared plumbing (`common.py`): a real queryapigate subprocess hit over real HTTP - not the Flask test
client, so there's an actual separate OS process whose RSS (`/proc/<pid>/status`) can be sampled, the same
way a user would actually experience it - and a native driver connection for fast seeding (seeding a million
rows through `/execute_sql` would itself dominate the clock, so setup uses each driver directly; see
`dataset.py`'s own docstring).

## Why not a standard benchmark (TPC-H, sysbench, ...)?

TPC-H/TPC-DS benchmark a query engine's join and aggregation performance - not what any of these scripts
measure. `dataset.py` builds one small, purpose-built table (`bench_data`: `id INT`, `payload` a string) in
the spirit of sysbench's simple synthetic tables, rather than adopting a schema designed to stress a query
planner none of this touches. Content is deterministic (not random), so seeding is fast and a run is
reproducible.

## Common methodology

- **One dialect at a time, sequentially, in its own process.** Noisy neighbors (another DB container
  competing for the host's CPU/disk/page cache) and a dirty baseline (a previous run's memory still resident
  in the same process) both showed up as real distortion during manual testing before `run.py` existed - see
  the git history of `runners.py`'s driver docstrings for what that looked like.
- **Absolute numbers are machine-specific; the shape is the portable claim.** "Peak RSS delta stays flat as
  rows grow" survives a different machine - a specific megabyte figure doesn't. Read the numbers here as
  illustrative of the shape, not as a promise about any particular deployment's performance.
- Needs a reachable database. sqlite and duckdb need nothing extra (a scratch file is created automatically);
  the others read the same `QUERYAPIGATE_IT_*` JSON connection env vars `tests/test_integration.py` uses:

  ~~~bash
  export QUERYAPIGATE_IT_MYSQL='{"host": "127.0.0.1", "port": 3306, "user": "root", "password": "pw", "database": "it"}'
  ~~~

Each script writes its own `benchmarks/results/<prefix>-<dialect>-<date>.md` and prints the same report to
the terminal. Commit the result files that matter so the numbers stay checked in and dated - a benchmark
nobody has rerun in six months is worse for credibility than no benchmark at all if it's read as current.

---

## `run.py` - buffered vs. streamed

Two independently-controlled knobs: **row count** (`--rows`, default 1,000,000) and **row width** (`--widths`,
`narrow` ~40 bytes or `wide` ~480 bytes). The "buffered" comparison needs `QUERYAPIGATE_MAX_PAGE_SIZE` raised
for the run - by design, you cannot normally request 1,000,000 rows in one page; the server is started with
the cap raised specifically so the comparison point exists at all. That is itself part of the finding:
without streaming, matching what streaming gives you for free means disabling the safety limit that protects
you from doing this by accident. N=5 repeats per scenario (`--repeats`); the report shows median, min and max.

~~~bash
python benchmarks/run.py mysql
python benchmarks/run.py mysql --rows 100000 --widths narrow --repeats 3   # a smaller, faster run
~~~

### Results

| Dialect | Last run | Notes |
|---|---|---|
| MySQL | 2026-09-24 | Memory flat regardless of row width (~0MB streamed vs. 337-988MB buffered); streaming is 2-4x slower in latency than buffered for the same data - see [results/mysql-2026-09-24.md](results/mysql-2026-09-24.md) |
| PostgreSQL | 2026-09-24 | Memory flat regardless of row width (~0MB streamed vs. 352-928MB buffered); streaming is 2-5x slower in latency than buffered for the same data - see [results/postgres-2026-09-24.md](results/postgres-2026-09-24.md) |
| ClickHouse | 2026-09-24 | Memory flat regardless of row width (~0MB streamed vs. 348-1233MB buffered); streaming is 2-5x slower in latency than buffered for the same data - see [results/clickhouse-2026-09-24.md](results/clickhouse-2026-09-24.md) |
| SQLite | 2026-09-24 | Memory flat regardless of row width (~0MB streamed vs. 333-1034MB buffered), matching the network dialects despite no special unbuffered-cursor code - just the same fetchmany() loop, relying on SQLite's naturally incremental cursor; streaming is 2-4x slower in latency than buffered - see [results/sqlite-2026-09-24.md](results/sqlite-2026-09-24.md) |
| H2 | 2026-09-24 | Memory flat regardless of row width (~0MB streamed vs. 206-1062MB buffered); latency pattern *reverses* by width - streaming is slower for narrow rows (24.7s vs 19.9s) but faster for wide rows (54.0s vs 65.1s), unlike every other dialect - see [results/h2-2026-09-24.md](results/h2-2026-09-24.md) |
| DuckDB | 2026-09-24 | Streamed memory reduced (~7-81MB vs. 344-1076MB buffered) but not flat like the others - matches the documented finding that DuckDB materialises the whole result during execute() before the first fetch; fastest dialect overall (no network) - see [results/duckdb-2026-09-24.md](results/duckdb-2026-09-24.md) |

---

## `latency.py` - general API latency (BACKLOG #25)

`run.py` only measures latency at the 1M-row large-export scale. This answers the common-case question
instead: for a small `GET /q/<name>` or `POST /execute_sql` call against a **fixed, tiny** table (100 rows,
narrow width), how much overhead does queryapigate itself add on top of the database's own query time -
parameter binding, the SQL guard, rate-limit/permission checks, response serialization? `health` never
touches a database at all, as a floor for pure HTTP/WSGI overhead to compare the other two scenarios against.
200 sequential requests per scenario by default (`--requests`), after a 10-request warmup; p50/p95/p99 are
computed from the full sample.

~~~bash
python benchmarks/latency.py mysql
python benchmarks/latency.py mysql --requests 500
~~~

### Results

| Dialect | Last run | Notes |
|---|---|---|
| MySQL | 2026-09-30 | `health` p50 1.4ms; `GET /q/<name>` p50 6.4ms; `POST /execute_sql` p50 4.1ms - the saved-query path costs ~2ms more than ad-hoc SQL at this scale (lookup + versioning + parameter resolution) - see [results/latency-mysql-2026-09-30.md](results/latency-mysql-2026-09-30.md) |
| ClickHouse | 2026-09-30 | `health` p50 2.3ms; `GET /q/<name>` p50 8.7ms; `POST /execute_sql` p50 6.3ms - same ~2ms saved-query overhead pattern as MySQL, on top of ClickHouse's own higher per-call latency - see [results/latency-clickhouse-2026-09-30.md](results/latency-clickhouse-2026-09-30.md) |

---

## `pooling.py` - connection pooling under concurrency (BACKLOG #25)

**Read `pool.py`'s own module docstring first**: this pool only bounds *idle* connections kept around - it
does not cap concurrent connections, so there is no "wait time when the pool is exhausted" to measure the
way a traditional bounded pool would have. What this script measures instead: a thread-per-caller client
(real threads, not asyncio, so the client side genuinely overlaps requests) fires `GET /q/<name>` in a tight
loop for a fixed window (`--window`, default 3s) at each concurrency level (`--levels`, default
`1,5,10,20,50`), against a server started with a fixed `QUERYAPIGATE_POOL_SIZE` (`--pool-size`, default 5 -
matching `config.DEFAULT_POOL_SIZE`). `queryapigate_pool_idle_connections` (`GET /metrics`) is sampled every
200ms during each window to show the idle-pool hit rate alongside throughput and p50/p95 latency.

~~~bash
python benchmarks/pooling.py mysql
python benchmarks/pooling.py mysql --levels 1,10,50,100 --window 5
~~~

### Results

| Dialect | Last run | Notes |
|---|---|---|
| MySQL | 2026-09-30 | Throughput peaks at concurrency=5 (309 req/s) then plateaus/declines slightly through concurrency=50 (276 req/s) while p95 latency degrades sharply (44ms → 906ms); idle connections never exceed the configured pool_size=5, confirming the idle cap holds under load - see [results/pooling-mysql-2026-09-30.md](results/pooling-mysql-2026-09-30.md) |
| ClickHouse | 2026-09-30 | Same shape as MySQL: throughput roughly flat ~270-305 req/s from concurrency=5 onward, p95 latency climbs from 42ms to 854ms; idle connections again capped at pool_size=5 - see [results/pooling-clickhouse-2026-09-30.md](results/pooling-clickhouse-2026-09-30.md) |

At concurrency=1, idle connections spend almost no time actually idle (0-1) despite a pool_size of 5 - a
single caller with no think-time between requests keeps its one connection perpetually checked out rather
than returning it to the idle pool, which is a real, expected shape given `pool.py`'s design, not a bug in
this benchmark.

---

## `caching.py` - cache hit/miss performance (BACKLOG #25)

Opt-in saved-query response caching (`cache_ttl`, see [Response
caching](../documentation/API.md#response-caching)) has only ever been tested for correctness
(`tests/test_queryapigate.py`), never benchmarked for the performance claim it exists to deliver. A saved
query with `cache_ttl=2s` is called every 200ms for 4 TTL windows (40 calls); each response's real `X-Cache`
header classifies it as a hit or miss, rather than assuming from timing. A same-shape query with no
`cache_ttl` set at all gives a baseline query cost, isolating "a miss still runs a real query" from "a miss
also has to write the cache."

~~~bash
python benchmarks/caching.py mysql
~~~

### Results

| Dialect | Last run | Notes |
|---|---|---|
| MySQL | 2026-09-30 | Hit 5.8ms vs. miss 14.2ms median (2.5x); baseline query cost with no caching at all: 6.8ms - a miss costs *more* than the uncached baseline (query + cache write), which is the expected shape - see [results/caching-mysql-2026-09-30.md](results/caching-mysql-2026-09-30.md) |
| ClickHouse | 2026-09-30 | Hit 6.9ms vs. miss 12.5ms median (1.8x); baseline query cost with no caching at all: 5.2ms - same shape as MySQL - see [results/caching-clickhouse-2026-09-30.md](results/caching-clickhouse-2026-09-30.md) |

See `results/` for the full reports.

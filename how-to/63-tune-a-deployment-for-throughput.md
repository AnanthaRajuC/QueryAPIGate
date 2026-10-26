# How to tune a deployment for throughput

**Time:** 20 minutes to read, plus a load test of your own. **You'll end up with:** threads, CPU, logging, cache and
disk settings chosen for your queries and your database's distance, a number of containers to start from, and the
figures each one rests on.

Every figure here comes from the benchmarks in [Performance, v3](../documentation/PERFORMANCE_V3.md) (PostgreSQL) and
[Performance on MySQL](../documentation/PERFORMANCE_MYSQL_V1.md), run on one laptop with the gateway, the database and
the load generator each on their own cores. They are measured on 0.42 and `main` before 0.43's round-trip changes
([ADR 0027](../documentation/adr/0027-fewer-round-trips-per-query.md)), which make calls across a network cheaper still,
so treat them as conservative. Measure your own queries before you commit to a size.

## 1. One worker per container, more containers for more

The image runs gunicorn with **one worker** and keeps it that way: each process has its own `/metrics`, its own
response cache without Redis, and its own `/events` streams. Python runs one thread at a time per process, so a worker
saturates about one core. More capacity means more containers, behind a load balancer, on a shared PostgreSQL store and
Redis ([Run several instances](42-run-several-instances.md)).

## 2. Threads: by how long your calls wait

A worker's threads let it run Python for one call while others wait on the database. They help only while calls spend
real time waiting. Measured, one worker, 32 callers:

| Threads | PostgreSQL lookup, 0.03 ms | MySQL lookup, 0.03 ms | MySQL `stock_level`, 0.42 ms |
|---|---|---|---|
| 1 | 788 req/s | **865** | 630 |
| 2 | **843** | 823 | **772** |
| 4 | 777 | 722 | 683 |
| 8 (the default) | 735 | 676 | 642 |

Past what the waiting needs, threads cost: they compete for Python's interpreter lock, and on these fast queries the
best setting was 15-28% faster than the default eight. So:

| Your calls | `QUERYAPIGATE_THREADS` |
|---|---|
| Fast lookups (well under 1 ms) on a database on the same host or rack | `2` |
| Queries of a few ms, or a database a network hop away | `4`-`8` |
| Slow reports (tens of ms and up), or a warehouse or Trino across a network | `8`-`16`, while CPU allows |

The waiting limit of one worker is **threads ÷ (query time + round trips × network round-trip time)** calls a second.
From 0.43 a PostgreSQL call makes 1 round trip to its database and a MySQL call 2, plus 3 to a PostgreSQL store, none
with the metadata cache (section 4). A 20 ms report query at 0.5 ms away, with 8 threads: 8 ÷ (20 + 4 × 0.5 ms) = about
360 a second - more threads would raise it, as long as the CPU figure in section 7 isn't lower.

**MySQL across a network before 0.43:** extra threads gained nothing - mysql-connector held Python's interpreter lock
while it pinged the server, once per call (ADR 0027). 0.43 removes the ping; threads overlap MySQL waits again.

The pool of idle database connections follows the threads (`QUERYAPIGATE_POOL_SIZE` defaults to them), and the main
server's `/events` streams never take more than half of them.

```yaml
# Docker Compose
    environment:
      QUERYAPIGATE_THREADS: "2"
```

```yaml
# Helm
config:
  QUERYAPIGATE_THREADS: "2"
```

## 3. CPU: about 1.25-1.5 cores per container

At full load one worker used **1.1-1.3 cores**: one for Python, the rest for its threads' I/O (PostgreSQL 1.2, MySQL
1.1-1.3). A CPU limit below that throttles it, and the throttling shows up as latency, not as an error.

| | Request | Limit |
|---|---|---|
| A small or shared cluster (the Helm chart's default) | `250m` | none |
| Serving at capacity | `1` | `1.5`, or none |

```yaml
# Helm
resources:
  requests: {cpu: "1", memory: 256Mi}
  limits: {cpu: 1500m, memory: 1Gi}
```

On one host, give each container its own physical core. In the benchmarks, eight workers on four cores' hyperthreads
did 307 lookups a second per core, against 612 for one worker on its own: a hyperthread is worth far less than a core.

## 4. The metadata cache: +12-23%, at the cost of a delay

`QUERYAPIGATE_METADATA_CACHE_TTL=5` keeps the key, saved query and connection a call reads from the store for 5
seconds instead of reading them on every call. Measured: +18-23% on PostgreSQL, +12-15% on MySQL, and half of
PostgreSQL's load, which was mostly the store's.

The cost: a change made on **another** instance (or with the CLI) takes up to the TTL to arrive there - a revoked key
keeps working on the other instances for that long. A change made through the same instance is seen at once. With
one instance, turn it on; with several, decide whether a 5-second revocation delay is acceptable
([the metadata cache](../documentation/PERFORMANCE.md#the-metadata-cache)).

## 5. Logs: off where something else already logs

| Setting | What it writes | Cost measured | Turn off when |
|---|---|---|---|
| `QUERYAPIGATE_ACCESS_LOG=off` | gunicorn's access line per request, on stdout | 7% of throughput | the proxy or load balancer logs requests |
| `QUERYAPIGATE_LOG_REQUESTS=off` | QueryAPIGate's own request and query lines (INFO) | part of the fifth of CPU logging took | the same - errors, warnings, slow queries and agent writes are still logged |

Turn the access log off with the setting, not by sending it to `/dev/null`: that still formats and writes every line.
At these rates the logs grow by gigabytes an hour - 6.6 GB in a five-hour MySQL run.

## 6. Disk: the event log

Every call is recorded in the store's event log (ADR 0020), kept for 24 hours by default: **about 570 bytes a call**
on a SQLite store, more on PostgreSQL.

| Sustained calls a second | Event log after 24 hours |
|---|---|
| 50 | about 2.5 GB |
| 500 | about 25 GB |
| 2,000 | about 100 GB |

`QUERYAPIGATE_EVENTS_RETENTION_HOURS` shortens it, and the disk with it. It is also how far back a `queryapigate
events` client can resume after a disconnect, so keep it above the longest outage a client must ride out.

## 7. How many containers

Per worker, at 75% of measured capacity, where p99 stays at about 4-5 ms:

| | Plain | With the metadata cache |
|---|---|---|
| PostgreSQL, fast OLTP calls | 550 calls a second | 650 |
| MySQL, `order_status`-class calls | 440 | 490 |

**Containers** = the larger of (your calls a second ÷ that figure) and (your calls a second ÷ the waiting limit from
section 2), **plus one** to lose without falling over. Every millisecond of Python a call costs beyond these simple
lookups lowers the CPU figure; masked keys and large pages cost more.

Then load-test your own queries at 75% of what you plan to serve, and watch p99, not the average: it holds flat to
about 75% and triples by 90%.

## What hasn't been measured yet

- **Containers on separate hosts.** The scale-out figures are workers sharing one laptop's cores; separate hosts
  should scale better, by an amount not yet measured.
- **0.43's figures.** The round-trip changes, the thread setting and sqlglot 30 are measured at the driver, not yet
  through whole requests. The benchmarks are rerun for the 0.43 release, and this guide will be updated with them.

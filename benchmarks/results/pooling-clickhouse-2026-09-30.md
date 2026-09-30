# Pooling benchmark: clickhouse

- queryapigate 0.10.0, 2026-09-30T05:05:52.947469+00:00
- Linux-6.8.0-139-generic-x86_64-with-glibc2.39, 20 CPUs
- QUERYAPIGATE_POOL_SIZE=5 (fixed for this run), 100 rows (fixed, narrow width)

queryapigate's pool only bounds *idle* connections kept around, not concurrent ones (see pool.py) - there is no bounded-pool wait time to show. `Idle range` is the min-max of `queryapigate_pool_idle_connections` sampled every 200ms during the window.

| Concurrency | Throughput (req/s) | p50 (ms) | p95 (ms) | Idle range | Errors |
|---|---|---|---|---|---|
| 1 | 121 | 8.93 | 10.78 | 0-1 | 0 |
| 5 | 300 | 10.83 | 41.57 | 1-4 | 0 |
| 10 | 267 | 19.48 | 114.78 | 4-5 | 0 |
| 20 | 305 | 18.68 | 242.52 | 2-5 | 0 |
| 50 | 278 | 34.69 | 854.11 | 2-5 | 0 |

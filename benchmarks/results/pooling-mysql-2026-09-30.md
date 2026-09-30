# Pooling benchmark: mysql

- queryapigate 0.10.0, 2026-09-30T05:04:59.562944+00:00
- Linux-6.8.0-139-generic-x86_64-with-glibc2.39, 20 CPUs
- QUERYAPIGATE_POOL_SIZE=5 (fixed for this run), 100 rows (fixed, narrow width)

queryapigate's pool only bounds *idle* connections kept around, not concurrent ones (see pool.py) - there is no bounded-pool wait time to show. `Idle range` is the min-max of `queryapigate_pool_idle_connections` sampled every 200ms during the window.

| Concurrency | Throughput (req/s) | p50 (ms) | p95 (ms) | Idle range | Errors |
|---|---|---|---|---|---|
| 1 | 165 | 6.28 | 8.91 | 0-1 | 0 |
| 5 | 309 | 9.36 | 44.12 | 1-4 | 0 |
| 10 | 294 | 12.84 | 112.04 | 3-4 | 0 |
| 20 | 287 | 17.06 | 242.67 | 4-5 | 0 |
| 50 | 276 | 27.42 | 905.93 | 4-5 | 0 |

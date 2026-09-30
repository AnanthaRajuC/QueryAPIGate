# Latency benchmark: clickhouse

- queryapigate 0.10.0, 2026-09-30T05:05:45.364930+00:00
- Linux-6.8.0-139-generic-x86_64-with-glibc2.39, 20 CPUs
- 100 rows (fixed, narrow width), 200 requests per scenario, sequential (not concurrent - see pooling.py for that dimension)

| Scenario | p50 (ms) | p95 (ms) | p99 (ms) | Min (ms) | Max (ms) |
|---|---|---|---|---|---|
| health (no database) | 2.29 | 2.75 | 4.20 | 1.16 | 11.42 |
| GET /q/<name> | 8.72 | 10.90 | 12.63 | 3.45 | 13.34 |
| POST /execute_sql | 6.28 | 7.71 | 8.33 | 2.63 | 10.30 |

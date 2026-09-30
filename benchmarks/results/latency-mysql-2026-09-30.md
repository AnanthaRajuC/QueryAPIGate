# Latency benchmark: mysql

- queryapigate 0.10.0, 2026-09-30T05:04:51.705929+00:00
- Linux-6.8.0-139-generic-x86_64-with-glibc2.39, 20 CPUs
- 100 rows (fixed, narrow width), 200 requests per scenario, sequential (not concurrent - see pooling.py for that dimension)

| Scenario | p50 (ms) | p95 (ms) | p99 (ms) | Min (ms) | Max (ms) |
|---|---|---|---|---|---|
| health (no database) | 1.35 | 2.64 | 4.69 | 0.80 | 20.06 |
| GET /q/<name> | 6.39 | 8.23 | 9.08 | 2.48 | 9.79 |
| POST /execute_sql | 4.05 | 5.33 | 5.87 | 1.63 | 6.25 |

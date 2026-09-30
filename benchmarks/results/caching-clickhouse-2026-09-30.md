# Caching benchmark: clickhouse

- queryapigate 0.10.0, 2026-09-30T05:06:21.101315+00:00
- Linux-6.8.0-139-generic-x86_64-with-glibc2.39, 20 CPUs
- cache_ttl=2s, called every 0.2s, 40 calls total (36 hits, 4 misses)

| | Median latency (ms) |
|---|---|
| Cache hit | 6.85 |
| Cache miss (query + cache write) | 12.47 |
| No cache_ttl at all (baseline query cost) | 5.24 |

Hit vs. miss speedup: **1.8x**.

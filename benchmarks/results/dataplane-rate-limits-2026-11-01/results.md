## Environment


## /health floors

| block | when | req/s | p50 ms | power profile | AC | EPP | package C | MHz |
|---|---|---|---|---|---|---|---|---|
| l1 | start | 2,137 | 0.46 | performance | 1 | performance | 70.0 | 3613 |
| l1 | end | 2,107 | 0.47 | performance | 1 | performance | 77.0 | 2436 |
| l2 | start | 2,123 | 0.47 | performance | 1 | performance | 85.0 | 4520 |
| l2 | end | 2,321 | 0.43 | performance | 1 | performance | 83.0 | 3770 |

## pgbench

| workload | tps | latency ms | n |
|---|---|---|---|
| pk_c1 | - | - | 0 |
| pk_c32 | - | - | 0 |
| tpcb_c32 | - | - | 0 |

## b1: c=32

| scenario | build | n | req/s | p50 ms | p99 ms | p99.9 ms | gateway cores | db cores | load cores | store tx/req | MHz | vs v0.41.0 | vs main |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|

## b2: c=1

| scenario | build | n | req/s | p50 ms | p99 ms | p99.9 ms | gateway cores | db cores | load cores | store tx/req | MHz | vs v0.41.0 | vs main |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|

## b3: capacity (closed loop, c=32), then open loop, latency corrected

| build | offered (x capacity) | offered req/s | achieved req/s | p50 ms | p99 ms | p99.9 ms | ok | n |
|---|---|---|---|---|---|---|---|---|

## b4: scale-out, c=64

| scenario | workers | build | n | req/s | p50 ms | p99 ms | gateway cores | req/s per gateway core | db cores | load cores | vs main |
|---|---|---|---|---|---|---|---|---|---|---|---|

## b5: cold path over 150 s (per-request durations from the access log)


## b6: access log

| access log | n | req/s | p50 ms | p99 ms | gateway cores |
|---|---|---|---|---|---|

## b7: c=1

| scenario | build | n | req/s | p50 ms | p99 ms | p99.9 ms | gateway cores | db cores | load cores | store tx/req | MHz | vs v0.41.0 | vs main |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|

## b7: c=32

| scenario | build | n | req/s | p50 ms | p99 ms | p99.9 ms | gateway cores | db cores | load cores | store tx/req | MHz | vs v0.41.0 | vs main |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|

## b8: mixed and hot

| scenario | n | req/s | p50 ms | p99 ms |
|---|---|---|---|---|

## b10: threads per worker, c=32

| threads | n | req/s | p50 ms | p99 ms | gateway cores |
|---|---|---|---|---|---|

## l1: rate limits counted in Redis

| scenario | c | build | n | req/s | p50 ms | p99 ms | gateway cores | Python cores | data plane cores | Redis cores | Redis CPU µs per call | req/s per gateway core | MHz | runs kept, lowest | per core vs Python, same limits |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| lookup_limited | 32 | dataplane-rate-limits+redis+cache | 5 | 923 (918-924) | 34.465 (34.371-34.616) | 40.53 (39.95-40.83) | 1.14 | 1.14 | 0.00 | 0.050 | 54.1 (52.4-55.1) | 809 (806-811) | 4135 | 100% |  |
| lookup_limited | 32 | dataplane-rate-limits+redis+cache+limit | 5 | 839 (831-843) | 37.908 (37.754-38.231) | 44.62 (44.22-45.71) | 1.15 | 1.15 | 0.00 | 0.051 | 60.7 (59.6-69.8) | 728 (721-731) | 4062 | 100% |  |
| lookup_limited | 32 | dataplane-rate-limits+dp+redis+cache | 5 | 21,678 (21,245-31,020) | 1.390 (0.944-1.415) | 3.56 (2.87-3.64) | 6.07 | 0.88 | 5.19 | 0.541 | 25.0 (17.4-25.4) | 3,560 (3,527-5,189) | 2369 | 93% | 4.4x |
| lookup_plain | 32 | dataplane-rate-limits+dp+redis+cache | 5 | 25,406 (25,133-28,121) | 1.203 (1.103-1.215) | 2.56 (2.00-2.61) | 6.65 | 1.08 | 5.57 | 0.003 | 0.1 (0.1-0.1) | 3,820 (3,792-3,968) | 2337 | 55% |  |
| lookup_limited | 32 | dataplane-rate-limits+dp+redis+cache+limit | 5 | 18,122 (17,913-18,257) | 1.712 (1.704-1.720) | 3.32 (3.20-3.38) | 5.01 | 0.66 | 4.36 | 0.685 | 37.7 (37.7-37.9) | 3,616 (3,610-3,660) | 2487 | 100% | 5.0x |

## l2: rate limits counted in Redis

| scenario | c | build | n | req/s | p50 ms | p99 ms | gateway cores | Python cores | data plane cores | Redis cores | Redis CPU µs per call | req/s per gateway core | MHz | runs kept, lowest | per core vs Python, same limits |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| lookup_limited | 1 | dataplane-rate-limits+dp+redis+cache | 5 | 5,720 (5,693-5,820) | 0.161 (0.159-0.162) | 0.34 (0.31-0.37) | 0.66 | 0.09 | 0.56 | 0.095 | 16.7 (16.0-17.3) | 8,736 (8,711-8,793) | 3962 | 100% |  |
| lookup_plain | 1 | dataplane-rate-limits+dp+redis+cache | 5 | 8,132 (7,943-8,490) | 0.110 (0.106-0.111) | 0.27 (0.23-0.28) | 0.77 | 0.13 | 0.64 | 0.002 | 0.2 (0.2-0.2) | 10,505 (10,437-10,591) | 4030 | 100% |  |
| lookup_limited | 1 | dataplane-rate-limits+dp+redis+cache+limit | 5 | 4,836 (4,753-4,856) | 0.193 (0.192-0.195) | 0.38 (0.37-0.42) | 0.60 | 0.08 | 0.52 | 0.140 | 29.0 (28.5-29.7) | 8,008 (7,948-8,023) | 4002 | 100% |  |

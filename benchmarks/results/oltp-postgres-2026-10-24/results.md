## Environment

```
{
 "block": "env",
 "scenario": "env",
 "profile": "performance",
 "ac": "1",
 "epp": "performance",
 "pkg_temp_c": 83.0,
 "load1": 1.474609375,
 "builds": [
  "v0.41.0",
  "main",
  "main+cache"
 ],
 "refs": {
  "v0.41.0": "f61d4ed3e7170a990435c40a2b3b75722cd43d15",
  "main": "99db30b424e3f4d75e17a1a57526db73df421b9e",
  "main+cache": "99db30b424e3f4d75e17a1a57526db73df421b9e"
 },
 "python": "Python 3.12.3",
 "gunicorn": "26.2.0",
 "psycopg2": "2.9.10",
 "flask": "3.1.1",
 "sqlglot": "25.34.1",
 "postgres": "16.15 (Debian 16.15-1.pgdg13+2)",
 "oha": "ghcr.io/hatoo/oha@sha256:3ec3dbf549ea197793482d47a6324797411406bbf438c2fe8b91f244ec641a2f",
 "kernel": "6.8.0-147-generic",
 "cpu": "12th Gen Intel(R) Core(TM) i7-12700H",
 "cpus": {
  "gateway": "0-7",
  "database": "8-11",
  "load": "12-19"
 },
 "ts": "2026-10-24T08:45:21"
}
```
```
{
 "block": "env",
 "scenario": "env",
 "profile": "performance",
 "ac": "1",
 "epp": "performance",
 "pkg_temp_c": 96.0,
 "load1": 1.16162109375,
 "builds": [
  "v0.41.0",
  "main",
  "main+cache"
 ],
 "refs": {
  "v0.41.0": "f61d4ed3e7170a990435c40a2b3b75722cd43d15",
  "main": "99db30b424e3f4d75e17a1a57526db73df421b9e",
  "main+cache": "99db30b424e3f4d75e17a1a57526db73df421b9e"
 },
 "python": "Python 3.12.3",
 "gunicorn": "26.2.0",
 "psycopg2": "2.9.10",
 "flask": "3.1.1",
 "sqlglot": "25.34.1",
 "postgres": "16.15 (Debian 16.15-1.pgdg13+2)",
 "oha": "ghcr.io/hatoo/oha@sha256:3ec3dbf549ea197793482d47a6324797411406bbf438c2fe8b91f244ec641a2f",
 "kernel": "6.8.0-147-generic",
 "cpu": "12th Gen Intel(R) Core(TM) i7-12700H",
 "cpus": {
  "gateway": "0-7",
  "database": "8-11",
  "load": "12-19"
 },
 "ts": "2026-10-24T11:08:17"
}
```

## /health floors

| block | when | req/s | p50 ms | power profile | AC | EPP | package C | MHz |
|---|---|---|---|---|---|---|---|---|
| b3 | start | 2,233 | 0.44 | performance | 1 | performance | 83.0 | 4389 |
| b3 | end | 2,245 | 0.44 | performance | 1 | performance | 83.0 | 3566 |
| b4 | start | 2,197 | 0.45 | performance | 1 | performance | 80.0 | 2046 |
| b4 | end | 2,258 | 0.44 | performance | 1 | performance | 81.0 | 3511 |
| b7 | start | 2,174 | 0.45 | performance | 1 | performance | 84.0 | 2323 |
| b7 | end | 2,228 | 0.44 | performance | 1 | performance | 73.0 | 944 |
| b8 | start | 2,187 | 0.45 | performance | 1 | performance | 77.0 | 581 |
| b8 | end | 2,015 | 0.49 | performance | 1 | performance | 81.0 | 3021 |
| b10 | start | 1,959 | 0.50 | performance | 1 | performance | 81.0 | 4300 |
| b10 | end | 1,988 | 0.49 | performance | 1 | performance | 81.0 | 2693 |
| b11 | start | 1,984 | 0.50 | performance | 1 | performance | 96.0 | 4298 |
| b11 | end | 1,993 | 0.49 | performance | 1 | performance | 82.0 | 4472 |

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

- capacity, main: 730 (720-797) req/s over 5 rounds

- capacity, main+cache: 862 (840-872) req/s over 5 rounds

| build | offered (x capacity) | offered req/s | achieved req/s | p50 ms | p99 ms | p99.9 ms | ok | n |
|---|---|---|---|---|---|---|---|---|
| main | 0.25 | 182 | 182 (182-183) | 1.98 (1.78-1.99) | 3.0 (3.0-3.0) | 11.0 (8.9-17.0) | 1.0000 | 5 |
| main | 0.50 | 364 | 364 (364-364) | 1.63 (1.56-1.64) | 2.8 (2.7-2.9) | 8.4 (6.3-10.6) | 1.0000 | 5 |
| main | 0.75 | 547 | 547 (547-547) | 1.54 (1.52-1.56) | 3.8 (3.2-4.1) | 13.5 (11.6-16.1) | 1.0000 | 5 |
| main | 0.90 | 656 | 656 (656-656) | 1.65 (1.63-1.65) | 11.1 (7.6-12.9) | 21.7 (14.5-26.2) | 1.0000 | 5 |
| main | 1.00 | 729 | 729 (729-729) | 1.87 (1.76-2.04) | 22.7 (18.3-67.2) | 27.8 (24.3-70.5) | 1.0000 | 5 |
| main | 1.10 | 802 | 744 (738-748) | 663.92 (608.78-777.79) | 1,584.3 (1,473.9-1,751.7) | 1,606.6 (1,499.0-1,759.0) | 1.0000 | 5 |
| main+cache | 0.25 | 215 | 215 (215-215) | 1.51 (1.44-1.59) | 2.3 (2.3-2.4) | 10.5 (8.0-13.9) | 1.0000 | 5 |
| main+cache | 0.50 | 431 | 431 (431-431) | 1.14 (1.13-1.15) | 2.1 (2.0-2.2) | 9.8 (6.9-19.0) | 1.0000 | 5 |
| main+cache | 0.75 | 646 | 646 (646-646) | 1.16 (1.15-1.18) | 3.4 (3.3-4.6) | 15.0 (14.4-15.8) | 1.0000 | 5 |
| main+cache | 0.90 | 776 | 776 (776-776) | 1.18 (1.16-1.19) | 8.7 (6.3-11.6) | 19.0 (15.7-23.6) | 1.0000 | 5 |
| main+cache | 1.00 | 862 | 862 (862-862) | 1.60 (1.55-1.76) | 17.2 (15.2-23.6) | 21.8 (19.9-26.4) | 1.0000 | 5 |
| main+cache | 1.10 | 948 | 893 (888-899) | 304.53 (229.22-386.87) | 1,276.3 (1,152.5-1,340.0) | 1,295.5 (1,161.4-1,389.5) | 1.0000 | 5 |

## b4: scale-out, c=64

| scenario | workers | build | n | req/s | p50 ms | p99 ms | gateway cores | req/s per gateway core | db cores | load cores | vs main |
|---|---|---|---|---|---|---|---|---|---|---|---|
| lookup_plain | 1 | main | 5 | 734 (733-739) | 86.3 (85.9-86.5) | 103.0 (102.3-109.2) | 1.20 | 612 | 0.30 | 0.27 |  |
| lookup_plain | 1 | main+cache | 5 | 869 (866-871) | 72.9 (72.4-73.1) | 87.9 (86.6-90.0) | 1.18 | 736 | 0.17 | 0.26 | +18%, ranges clear |
| lookup_plain | 2 | main | 5 | 1,196 (1,193-1,202) | 54.5 (52.7-55.0) | 73.9 (68.4-77.7) | 2.39 | 501 | 0.56 | 0.35 |  |
| lookup_plain | 2 | main+cache | 5 | 1,441 (1,431-1,445) | 44.8 (44.3-46.6) | 58.9 (56.7-67.4) | 2.32 | 622 | 0.31 | 0.37 | +20%, ranges clear |
| lookup_plain | 4 | main | 5 | 1,716 (1,709-1,722) | 33.9 (26.4-36.7) | 66.2 (53.4-89.6) | 4.67 | 367 | 1.04 | 0.40 |  |
| lookup_plain | 4 | main+cache | 5 | 2,112 (2,104-2,124) | 23.5 (17.4-29.5) | 68.7 (46.1-87.9) | 4.56 | 463 | 0.56 | 0.42 | +23%, ranges clear |
| lookup_plain | 8 | main | 5 | 2,215 (2,127-2,264) | 14.2 (10.8-23.9) | 139.0 (83.8-183.9) | 7.22 | 307 | 1.65 | 0.73 |  |
| lookup_plain | 8 | main+cache | 5 | 2,727 (2,638-2,801) | 10.7 (9.1-18.1) | 91.9 (71.5-149.9) | 7.23 | 377 | 0.92 | 0.72 | +23%, ranges clear |
| write_writer | 1 | main | 5 | 732 (729-733) | 86.7 (86.5-87.0) | 104.6 (102.4-105.8) | 1.20 | 610 | 0.34 | 0.28 |  |
| write_writer | 1 | main+cache | 5 | 863 (844-866) | 73.2 (73.1-74.8) | 89.3 (87.9-91.6) | 1.18 | 733 | 0.19 | 0.26 | +18%, ranges clear |
| write_writer | 2 | main | 5 | 1,188 (1,185-1,190) | 53.5 (53.2-55.5) | 68.5 (67.5-77.0) | 2.38 | 499 | 0.66 | 0.36 |  |
| write_writer | 2 | main+cache | 5 | 1,430 (1,428-1,434) | 45.4 (44.5-46.5) | 60.5 (58.1-65.0) | 2.31 | 618 | 0.38 | 0.37 | +20%, ranges clear |
| write_writer | 4 | main | 5 | 1,706 (1,701-1,710) | 35.4 (28.9-38.6) | 68.6 (58.3-82.5) | 4.66 | 366 | 1.19 | 0.42 |  |
| write_writer | 4 | main+cache | 5 | 2,087 (2,078-2,094) | 25.3 (12.9-34.2) | 69.3 (59.2-102.0) | 4.54 | 459 | 0.69 | 0.42 | +22%, ranges clear |
| write_writer | 8 | main | 5 | 2,165 (2,096-2,229) | 13.5 (10.5-24.2) | 109.9 (93.8-128.4) | 7.10 | 305 | 1.89 | 0.70 |  |
| write_writer | 8 | main+cache | 5 | 2,651 (2,552-2,683) | 11.9 (9.8-22.5) | 111.8 (54.3-133.0) | 7.13 | 372 | 1.04 | 0.73 | +22%, ranges clear |

## b5: cold path over 150 s (per-request durations from the access log)


## b6: access log

| access log | n | req/s | p50 ms | p99 ms | gateway cores |
|---|---|---|---|---|---|

## b7: c=1

| scenario | build | n | req/s | p50 ms | p99 ms | p99.9 ms | gateway cores | db cores | load cores | store tx/req | MHz | vs v0.41.0 | vs main |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| lookup_plain | v0.41.0 | 5 | 133 (132-134) | 7.57 (7.54-7.60) | 8.9 (8.8-9.0) | 10.2 (9.4-13.1) | 0.25 | 0.07 | 0.17 | 5.90 | 3653 |  |  |
| lookup_plain | main | 5 | 162 (161-168) | 6.24 (5.85-6.25) | 7.2 (7.1-7.3) | 10.8 (7.7-11.6) | 0.26 | 0.07 | 0.18 | 3.48 | 3925 | +21%, ranges clear |  |
| lookup_plain | main+cache | 5 | 270 (267-273) | 3.65 (3.58-3.74) | 4.4 (4.4-4.5) | 6.3 (5.5-8.8) | 0.34 | 0.04 | 0.19 | 0.01 | 3910 | +103%, ranges clear | +67%, ranges clear |

## b7: c=32

| scenario | build | n | req/s | p50 ms | p99 ms | p99.9 ms | gateway cores | db cores | load cores | store tx/req | MHz | vs v0.41.0 | vs main |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| lookup_plain | v0.41.0 | 5 | 674 (670-753) | 47.08 (42.06-47.35) | 57.2 (50.6-57.7) | 62.0 (56.1-63.2) | 1.25 | 0.37 | 0.24 | 5.02 | 3627 |  |  |
| lookup_plain | main | 5 | 735 (734-741) | 43.02 (42.89-43.13) | 54.2 (52.9-55.0) | 59.6 (58.1-62.4) | 1.22 | 0.28 | 0.25 | 3.03 | 3701 | +9%, ranges overlap |  |
| lookup_plain | main+cache | 5 | 855 (852-874) | 36.93 (36.25-37.07) | 46.7 (45.5-47.0) | 51.0 (50.6-52.6) | 1.19 | 0.16 | 0.26 | 0.01 | 3773 | +27%, ranges clear | +16%, ranges clear |

## b8: mixed and hot

| scenario | n | req/s | p50 ms | p99 ms |
|---|---|---|---|---|
| mixed_reads | 5 | 548 (546-549) | 43.29 (43.21-43.45) | 54.4 (54.2-55.4) |
| mixed_writes | 5 | 181 (181-182) | 43.60 (43.49-43.75) | 54.9 (54.6-55.4) |
| write_writer | 5 | 734 (733-736) | 43.14 (42.99-43.19) | 54.4 (53.9-54.9) |
| hotwrite_writer | 5 | 738 (735-740) | 42.93 (42.79-42.98) | 53.9 (53.4-55.3) |

## b10: threads per worker, c=32

| threads | n | req/s | p50 ms | p99 ms | gateway cores |
|---|---|---|---|---|---|
| 1 | 5 | 788 (782-792) | 39.86 (39.68-40.36) | 51.7 (50.0-51.9) | 0.87 |
| 2 | 5 | 843 (842-851) | 37.27 (36.94-37.47) | 47.5 (47.1-48.3) | 1.12 |
| 4 | 5 | 777 (772-778) | 40.63 (40.54-40.82) | 51.0 (49.8-51.7) | 1.19 |
| 8 | 5 | 735 (734-737) | 42.96 (42.91-43.09) | 54.5 (54.0-56.2) | 1.20 |

## b11: single-call latency against network delay (p50 ms; the slope is round trips per call)

| | +0ms | +0.5ms | +1ms | +2ms | round trips (slope) | at 0 delay (intercept) |
|---|---|---|---|---|---|---|
| psycopg2 alone, one | 0.03 (0.03-0.03) | 0.63 (0.62-0.63) | 1.19 (1.18-1.20) | 2.33 (2.28-2.40) | 1.15 | 0.04 ms |
| psycopg2 alone, four | 0.06 (0.06-0.06) | 2.44 (2.40-2.44) | 4.68 (4.59-4.70) | 9.36 (9.00-9.47) | 4.60 | 0.08 ms |
| v0.41.0 | 1.42 (1.38-1.45) | 7.60 (7.51-7.61) | 12.47 (12.44-12.47) | 22.66 (22.39-22.68) | 10.46 | 1.86 ms |
| main | 1.43 (1.35-1.44) | 6.24 (6.23-6.25) | 10.01 (9.86-10.07) | 18.75 (18.69-18.82) | 8.58 | 1.59 ms |
| main+cache | 1.05 (1.03-1.06) | 3.70 (3.64-3.76) | 5.84 (5.83-5.88) | 10.59 (10.21-10.90) | 4.72 | 1.17 ms |

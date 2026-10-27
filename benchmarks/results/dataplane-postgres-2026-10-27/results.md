## Environment

```
{
 "block": "env",
 "scenario": "env",
 "profile": "performance",
 "ac": "1",
 "epp": "performance",
 "pkg_temp_c": 62.0,
 "load1": 0.87255859375,
 "builds": [
  "v0.43.0",
  "rust-data-plane",
  "rust-data-plane+cache"
 ],
 "refs": {
  "v0.43.0": "c57a546ea4f3bf5e466b01f76a396bce1609a486",
  "rust-data-plane": "5844c0f2f531abe6369348f70c3790d3540cfadb",
  "rust-data-plane+cache": "5844c0f2f531abe6369348f70c3790d3540cfadb"
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
 "dataplane": {
  "sha256": "2102b2a697b0d163aae50e571ec666ff4d9af136a3c9855675ee791b66db3819",
  "commit": "5844c0f2f531abe6369348f70c3790d3540cfadb",
  "dirty": false,
  "rustc": "rustc 1.98.1 (48a229cea 2026-09-01)"
 },
 "ts": "2026-10-27T20:24:26"
}
```
```
{
 "block": "env",
 "scenario": "env",
 "profile": "performance",
 "ac": "1",
 "epp": "performance",
 "pkg_temp_c": 80.0,
 "load1": 12.6083984375,
 "builds": [
  "v0.43.0",
  "rust-data-plane",
  "rust-data-plane+cache"
 ],
 "refs": {
  "v0.43.0": "c57a546ea4f3bf5e466b01f76a396bce1609a486",
  "rust-data-plane": "5844c0f2f531abe6369348f70c3790d3540cfadb",
  "rust-data-plane+cache": "5844c0f2f531abe6369348f70c3790d3540cfadb"
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
 "dataplane": {
  "sha256": "2102b2a697b0d163aae50e571ec666ff4d9af136a3c9855675ee791b66db3819",
  "commit": "5844c0f2f531abe6369348f70c3790d3540cfadb",
  "dirty": false,
  "rustc": "rustc 1.98.1 (48a229cea 2026-09-01)"
 },
 "ts": "2026-10-27T22:12:12"
}
```

## /health floors

| block | when | req/s | p50 ms | power profile | AC | EPP | package C | MHz |
|---|---|---|---|---|---|---|---|---|
| d1 | start | 2,208 | 0.45 | performance | 1 | performance | 64.0 | 4377 |
| d1 | end | 2,261 | 0.44 | performance | 1 | performance | 68.0 | 2305 |
| d2 | start | 2,243 | 0.44 | performance | 1 | performance | 76.0 | 1763 |
| d2 | end | 2,198 | 0.45 | performance | 1 | performance | 100.0 | 4134 |
| d3 | start | 2,190 | 0.45 | performance | 1 | performance | 73.0 | 3535 |
| d3 | end | 2,239 | 0.44 | performance | 1 | performance | 74.0 | 3830 |
| d4 | start | 2,224 | 0.45 | performance | 1 | performance | 69.0 | 945 |
| d4 | end | 2,212 | 0.45 | performance | 1 | performance | 67.0 | 1578 |

## pgbench

| workload | tps | latency ms | n |
|---|---|---|---|
| pk_c1 | - | - | 0 |
| pk_c32 | - | - | 0 |
| tpcb_c32 | - | - | 0 |

## b1: c=32

| scenario | build | n | req/s | p50 ms | p99 ms | p99.9 ms | gateway cores | db cores | load cores | store tx/req | MHz | vs v0.43.0 | vs rust-data-plane |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|

## b2: c=1

| scenario | build | n | req/s | p50 ms | p99 ms | p99.9 ms | gateway cores | db cores | load cores | store tx/req | MHz | vs v0.43.0 | vs rust-data-plane |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|

## b3: capacity (closed loop, c=32), then open loop, latency corrected

| build | offered (x capacity) | offered req/s | achieved req/s | p50 ms | p99 ms | p99.9 ms | ok | n |
|---|---|---|---|---|---|---|---|---|

## b4: scale-out, c=64

| scenario | workers | build | n | req/s | p50 ms | p99 ms | gateway cores | req/s per gateway core | db cores | load cores | vs rust-data-plane |
|---|---|---|---|---|---|---|---|---|---|---|---|

## b5: cold path over 150 s (per-request durations from the access log)


## b6: access log

| access log | n | req/s | p50 ms | p99 ms | gateway cores |
|---|---|---|---|---|---|

## b7: c=1

| scenario | build | n | req/s | p50 ms | p99 ms | p99.9 ms | gateway cores | db cores | load cores | store tx/req | MHz | vs v0.43.0 | vs rust-data-plane |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|

## b7: c=32

| scenario | build | n | req/s | p50 ms | p99 ms | p99.9 ms | gateway cores | db cores | load cores | store tx/req | MHz | vs v0.43.0 | vs rust-data-plane |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|

## b8: mixed and hot

| scenario | n | req/s | p50 ms | p99 ms |
|---|---|---|---|---|

## b10: threads per worker, c=32

| threads | n | req/s | p50 ms | p99 ms | gateway cores |
|---|---|---|---|---|---|

## d1: the data plane

| scenario | c | build | Python workers | n | req/s | p50 ms | p99 ms | gateway cores | db cores | req/s per gateway core | runs kept, lowest | per core vs Python, same cache setting |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| lookup_gov | 32 | rust-data-plane | 1 | 5 | 832 (817-833) | 38.29 (38.15-38.96) | 44.7 (44.5-46.0) | 1.17 | 0.25 | 714 (701-715) | 100% |  |
| lookup_gov | 32 | rust-data-plane+cache | 1 | 5 | 991 (988-996) | 32.14 (31.94-32.22) | 37.4 (37.2-37.5) | 1.12 | 0.11 | 883 (880-886) | 100% |  |
| lookup_gov | 32 | v0.43.0 | 1 | 5 | 829 (820-832) | 38.23 (38.09-38.75) | 47.4 (47.2-47.4) | 1.17 | 0.28 | 711 (703-714) | 100% |  |
| lookup_gov | 32 | rust-data-plane+dp | 2 | 5 | 12,358 (12,198-12,533) | 2.51 (2.48-2.54) | 5.3 (5.2-5.4) | 5.42 | 3.76 | 2,288 (2,263-2,304) | 100% | 3.2x |
| lookup_gov | 32 | rust-data-plane+dp+cache | 2 | 5 | 24,288 (24,020-27,254) | 1.27 (1.15-1.27) | 2.7 (2.0-2.8) | 6.64 | 3.55 | 3,685 (3,659-3,818) | 50% | 4.2x |
| lookup_plain | 32 | rust-data-plane | 1 | 5 | 873 (870-899) | 36.50 (35.43-36.57) | 42.8 (40.9-43.2) | 1.17 | 0.25 | 747 (744-769) | 100% |  |
| lookup_plain | 32 | rust-data-plane+cache | 1 | 5 | 1,043 (1,034-1,048) | 30.51 (30.33-30.79) | 35.9 (35.6-36.1) | 1.13 | 0.12 | 925 (917-931) | 100% |  |
| lookup_plain | 32 | v0.43.0 | 1 | 5 | 869 (864-895) | 36.52 (35.43-36.69) | 44.8 (43.2-45.7) | 1.17 | 0.29 | 743 (738-766) | 100% |  |
| lookup_plain | 32 | rust-data-plane+dp | 2 | 5 | 12,689 (12,622-17,597) | 2.44 (1.70-2.46) | 5.1 (4.2-5.2) | 5.36 | 3.75 | 2,361 (2,352-3,449) | 100% | 3.2x |
| lookup_plain | 32 | rust-data-plane+dp+cache | 2 | 5 | 27,519 (24,999-40,938) | 1.14 (0.75-1.23) | 2.1 (1.3-2.7) | 7.05 | 3.36 | 3,904 (3,792-5,722) | 51% | 4.2x |
| page_plain | 32 | rust-data-plane | 1 | 5 | 739 (738-745) | 43.10 (42.79-43.14) | 50.4 (49.9-50.8) | 1.17 | 0.35 | 634 (633-638) | 100% |  |
| page_plain | 32 | rust-data-plane+cache | 1 | 5 | 868 (865-874) | 36.58 (36.44-36.73) | 43.1 (42.3-43.8) | 1.13 | 0.23 | 767 (766-772) | 100% |  |
| page_plain | 32 | v0.43.0 | 1 | 5 | 737 (736-741) | 43.08 (42.86-43.20) | 52.5 (52.4-52.9) | 1.17 | 0.37 | 631 (631-635) | 100% |  |
| page_plain | 32 | rust-data-plane+dp | 2 | 5 | 6,776 (6,745-6,872) | 4.60 (4.55-4.61) | 8.0 (7.9-8.2) | 3.77 | 3.94 | 1,800 (1,793-1,807) | 100% | 2.8x |
| page_plain | 32 | rust-data-plane+dp+cache | 2 | 5 | 9,614 (9,507-9,678) | 3.27 (3.24-3.30) | 5.6 (5.5-5.8) | 3.57 | 3.90 | 2,700 (2,682-2,715) | 100% | 3.5x |
| write_writer | 32 | rust-data-plane+cache | 1 | 3 | 1,030 (1,025-1,031) | 30.84 (30.71-30.96) | 36.9 (36.4-37.2) | 1.13 | 0.14 | 912 (909-914) | 100% |  |
| write_writer | 32 | rust-data-plane+dp+cache | 2 | 3 | 1,653 (1,648-1,653) | 19.48 (18.65-20.98) | 28.9 (25.2-33.5) | 2.40 | 0.21 | 687 (685-687) | 100% | 0.8x |

## d2: the data plane

| scenario | c | build | Python workers | n | req/s | p50 ms | p99 ms | gateway cores | db cores | req/s per gateway core | runs kept, lowest | per core vs Python, same cache setting |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| lookup_gov | 1 | rust-data-plane | 1 | 5 | 717 (690-732) | 1.33 (1.29-1.38) | 2.1 (2.0-2.2) | 0.80 | 0.18 | 904 (895-910) | 100% |  |
| lookup_gov | 1 | rust-data-plane+cache | 1 | 5 | 911 (873-942) | 1.08 (1.04-1.12) | 1.4 (1.4-1.5) | 0.86 | 0.10 | 1,062 (1,040-1,062) | 100% |  |
| lookup_gov | 1 | rust-data-plane+dp | 2 | 5 | 4,876 (4,528-4,980) | 0.20 (0.19-0.21) | 0.3 (0.3-0.3) | 0.83 | 0.70 | 5,865 (5,466-6,135) | 100% | 6.5x |
| lookup_gov | 1 | rust-data-plane+dp+cache | 2 | 5 | 7,852 (7,658-8,627) | 0.12 (0.11-0.12) | 0.2 (0.2-0.3) | 0.80 | 0.52 | 9,812 (9,653-10,485) | 100% | 9.2x |
| lookup_plain | 1 | rust-data-plane | 1 | 5 | 745 (704-787) | 1.27 (1.21-1.32) | 2.1 (1.9-2.2) | 0.80 | 0.18 | 930 (917-950) | 100% |  |
| lookup_plain | 1 | rust-data-plane+cache | 1 | 5 | 970 (953-978) | 1.01 (1.00-1.03) | 1.4 (1.4-1.4) | 0.87 | 0.09 | 1,111 (1,094-1,122) | 100% |  |
| lookup_plain | 1 | rust-data-plane+dp | 2 | 5 | 4,785 (4,680-5,083) | 0.20 (0.19-0.20) | 0.3 (0.3-0.3) | 0.83 | 0.70 | 5,748 (5,633-6,299) | 100% | 6.2x |
| lookup_plain | 1 | rust-data-plane+dp+cache | 2 | 5 | 8,347 (8,167-8,870) | 0.11 (0.10-0.11) | 0.2 (0.2-0.2) | 0.80 | 0.52 | 10,467 (10,233-10,942) | 100% | 9.4x |

## d3: the data plane

| scenario | c | build | Python workers | n | req/s | p50 ms | p99 ms | gateway cores | db cores | req/s per gateway core | runs kept, lowest | per core vs Python, same cache setting |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| lookup_plain | 1 | rust-data-plane | 1 | 5 | 217 (212-227) | 4.60 (4.40-4.76) | 5.3 (5.2-5.3) | 0.30 | 0.09 | 720 (711-744) | 100% |  |
| lookup_plain | 1 | rust-data-plane+cache | 1 | 5 | 602 (582-615) | 1.62 (1.57-1.69) | 2.1 (2.1-2.1) | 0.58 | 0.04 | 1,024 (1,015-1,033) | 100% |  |
| lookup_plain | 1 | rust-data-plane+dp | 2 | 5 | 475 (471-484) | 2.07 (2.02-2.08) | 2.6 (2.6-2.7) | 0.17 | 0.11 | 2,726 (2,665-2,804) | 100% | 3.8x |
| lookup_plain | 1 | rust-data-plane+dp+cache | 2 | 5 | 1,317 (1,305-1,341) | 0.74 (0.73-0.75) | 1.1 (1.0-1.1) | 0.21 | 0.10 | 6,116 (6,075-6,231) | 100% | 6.0x |
| lookup_plain | 32 | rust-data-plane | 1 | 5 | 885 (865-898) | 35.93 (35.41-36.67) | 41.7 (41.0-43.7) | 1.19 | 0.21 | 746 (728-756) | 100% |  |
| lookup_plain | 32 | rust-data-plane+cache | 1 | 5 | 1,031 (1,028-1,036) | 30.87 (30.56-30.90) | 36.0 (35.8-38.4) | 1.14 | 0.09 | 907 (906-914) | 100% |  |
| lookup_plain | 32 | rust-data-plane+dp | 2 | 5 | 4,956 (4,917-4,960) | 6.43 (6.43-6.48) | 6.9 (6.9-7.0) | 1.07 | 0.72 | 4,612 (4,194-4,635) | 100% | 6.2x |
| lookup_plain | 32 | rust-data-plane+dp+cache | 2 | 5 | 14,234 (13,662-14,283) | 2.22 (2.22-2.32) | 2.6 (2.5-2.7) | 1.78 | 1.05 | 8,029 (5,926-8,144) | 100% | 8.9x |

## d4: the data plane

| scenario | c | build | Python workers | n | req/s | p50 ms | p99 ms | gateway cores | db cores | req/s per gateway core | runs kept, lowest | per core vs Python, same cache setting |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| lookup_gov | 64 | rust-data-plane+cache | 8 | 3 | 3,353 (3,353-3,405) | 9.58 (7.91-11.96) | 81.1 (63.9-100.4) | 7.37 | 0.52 | 459 (455-461) | 100% |  |
| lookup_gov | 64 | rust-data-plane+dp+cache | 2 | 3 | 24,271 (24,188-27,264) | 2.59 (2.37-2.60) | 4.7 (3.5-4.7) | 6.63 | 3.53 | 3,696 (3,663-3,787) | 49% | 8.0x |
| lookup_plain | 64 | rust-data-plane+cache | 8 | 3 | 3,537 (3,495-3,640) | 9.19 (8.38-12.99) | 89.8 (73.4-93.3) | 7.38 | 0.53 | 481 (472-493) | 100% |  |
| lookup_plain | 64 | rust-data-plane+dp+cache | 2 | 3 | 28,020 (27,991-28,059) | 2.31 (2.31-2.31) | 3.4 (3.4-3.5) | 7.19 | 3.26 | 3,895 (3,892-3,896) | 54% | 8.1x |

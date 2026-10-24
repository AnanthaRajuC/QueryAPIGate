## Environment

```
{
 "block": "env",
 "scenario": "env",
 "profile": "performance",
 "ac": "1",
 "epp": "performance",
 "pkg_temp_c": 65.0,
 "load1": 0.67529296875,
 "pg_ip": "172.17.0.2",
 "python": "Python 3.12.3",
 "gunicorn": "26.2.0",
 "psycopg2": "2.9.10 (dt dec pq3 ext lo64)",
 "flask": "3.1.1",
 "sqlglot": "25.34.1",
 "postgres": "16.15 (Debian 16.15-1.pgdg13+2)",
 "oha": "ghcr.io/hatoo/oha@sha256:3ec3dbf549ea197793482d47a6324797411406bbf438c2fe8b91f244ec641a2f",
 "kernel": "6.8.0-147-generic",
 "main_sha": "c9b15c2e9520cfd494c39e4e39c15e71fcca87b0",
 "v041_sha": "f61d4ed3e7170a990435c40a2b3b75722cd43d15",
 "cpu": "12th Gen Intel(R) Core(TM) i7-12700H",
 "ts": "2026-10-23T20:59:57"
}
```
```
{
 "block": "env",
 "scenario": "env",
 "profile": "performance",
 "ac": "1",
 "epp": "performance",
 "pkg_temp_c": 83.0,
 "load1": 2.5869140625,
 "pg_ip": "172.17.0.2",
 "python": "Python 3.12.3",
 "gunicorn": "26.2.0",
 "psycopg2": "2.9.10 (dt dec pq3 ext lo64)",
 "flask": "3.1.1",
 "sqlglot": "25.34.1",
 "postgres": "16.15 (Debian 16.15-1.pgdg13+2)",
 "oha": "ghcr.io/hatoo/oha@sha256:3ec3dbf549ea197793482d47a6324797411406bbf438c2fe8b91f244ec641a2f",
 "kernel": "6.8.0-147-generic",
 "main_sha": "c9b15c2e9520cfd494c39e4e39c15e71fcca87b0",
 "v041_sha": "f61d4ed3e7170a990435c40a2b3b75722cd43d15",
 "cpu": "12th Gen Intel(R) Core(TM) i7-12700H",
 "ts": "2026-10-23T23:15:08"
}
```

## /health floors

| block | when | req/s | p50 ms | power profile | AC | EPP | package C | MHz |
|---|---|---|---|---|---|---|---|---|
| b1 | start | 2,202 | 0.45 | performance | 1 | performance | 94.0 | 4300 |
| b1 | end | 2,295 | 0.43 | performance | 1 | performance | 84.0 | 2362 |
| b2 | start | 2,193 | 0.45 | performance | 1 | performance | 90.0 | 4048 |
| b2 | end | 2,296 | 0.43 | performance | 1 | performance | 82.0 | 4488 |
| b3 | start | 2,239 | 0.44 | performance | 1 | performance | 74.0 | 2797 |
| b3 | end | 2,282 | 0.43 | performance | 1 | performance | 77.0 | 4337 |
| b4 | start | 2,148 | 0.46 | performance | 1 | performance | 78.0 | 4400 |
| b4 | end | 2,267 | 0.44 | performance | 1 | performance | 84.0 | 2665 |
| b5 | start | 2,190 | 0.45 | performance | 1 | performance | 81.0 | 3592 |
| b5 | end | 2,234 | 0.44 | performance | 1 | performance | 81.0 | 3570 |
| b6 | start | 2,226 | 0.44 | performance | 1 | performance | 76.0 | 3659 |
| b6 | end | 2,278 | 0.43 | performance | 1 | performance | 81.0 | 3992 |
| b7 | start | 2,246 | 0.43 | performance | 1 | performance | 78.0 | 3545 |
| b7 | end | 2,026 | 0.48 | performance | 1 | performance | 77.0 | 4209 |
| b8 | start | 1,934 | 0.51 | performance | 1 | performance | 73.0 | 1643 |
| b8 | end | 2,003 | 0.49 | performance | 1 | performance | 76.0 | 3973 |
| b10 | start | 2,281 | 0.43 | performance | 1 | performance | 69.0 | 2280 |
| b10 | end | 2,003 | 0.49 | performance | 1 | performance | 77.0 | 2166 |

## pgbench

| workload | tps | latency ms | n |
|---|---|---|---|
| pk_c1 | 38,289 (37,465-39,586) | 0.026 (0.025-0.027) | 5 |
| pk_c32 | 112,321 (96,140-123,670) | 0.285 (0.259-0.333) | 5 |
| tpcb_c32 | 12,970 (12,617-15,293) | 2.467 (2.092-2.536) | 5 |

## b1: c=32

| scenario | build | n | req/s | p50 ms | p99 ms | p99.9 ms | gateway cores | db cores | store tx/req | MHz | vs v0.41.0 | vs main |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| lookup_plain | v0.41.0 | 5 | 682 (679-686) | 46.73 (46.43-46.93) | 55.8 (55.6-56.4) | 60.9 (60.0-64.7) | 1.24 | 0.37 | 5.02 | 3667 |  |  |
| lookup_plain | main | 5 | 743 (736-746) | 42.55 (42.44-42.96) | 54.1 (52.9-54.3) | 61.0 (59.2-65.4) | 1.20 | 0.29 | 3.02 | 3721 | +9%, ranges clear |  |
| lookup_plain | main+cache | 5 | 875 (869-876) | 36.13 (36.10-36.33) | 45.1 (44.6-45.9) | 50.2 (48.2-53.6) | 1.18 | 0.17 | 0.01 | 3796 | +28%, ranges clear | +18%, ranges clear |
| lookup_gov | v0.41.0 | 5 | 171 (171-172) | 186.79 (185.85-187.06) | 223.4 (220.3-229.6) | 247.9 (232.2-257.1) | 1.06 | 0.15 | 6.07 | 3995 |  |  |
| lookup_gov | main | 5 | 693 (691-694) | 45.71 (45.61-45.74) | 57.1 (56.8-58.1) | 63.9 (62.4-64.8) | 1.21 | 0.30 | 4.02 | 3741 | +304%, ranges clear |  |
| lookup_gov | main+cache | 5 | 802 (800-806) | 39.34 (39.24-39.49) | 49.9 (48.9-50.5) | 57.8 (54.9-59.1) | 1.19 | 0.19 | 1.01 | 3804 | +368%, ranges clear | +16%, ranges clear |
| page_plain | v0.41.0 | 5 | 596 (595-598) | 53.35 (53.24-53.48) | 64.8 (63.3-65.9) | 71.3 (68.3-75.5) | 1.22 | 0.44 | 5.03 | 3681 |  |  |
| page_plain | main | 5 | 639 (638-642) | 49.58 (49.37-49.62) | 61.4 (61.2-62.2) | 67.4 (66.6-71.0) | 1.19 | 0.38 | 3.02 | 3741 | +7%, ranges clear |  |
| page_plain | main+cache | 5 | 738 (737-740) | 42.85 (42.77-42.98) | 52.7 (52.5-53.6) | 56.8 (56.3-62.5) | 1.17 | 0.29 | 0.01 | 3809 | +24%, ranges clear | +16%, ranges clear |
| write_writer | v0.41.0 | 5 | 682 (679-683) | 46.69 (46.60-46.78) | 56.9 (55.5-57.7) | 61.9 (60.3-63.7) | 1.24 | 0.41 | 5.02 | 3645 |  |  |
| write_writer | main | 5 | 738 (733-743) | 42.78 (42.54-43.10) | 53.9 (53.6-55.3) | 60.6 (57.2-63.5) | 1.20 | 0.33 | 3.02 | 3694 | +8%, ranges clear |  |
| write_writer | main+cache | 5 | 875 (872-881) | 36.04 (35.86-36.19) | 45.9 (45.1-46.2) | 50.4 (49.1-53.1) | 1.18 | 0.19 | 0.01 | 3744 | +28%, ranges clear | +19%, ranges clear |

## b2: c=1

| scenario | build | n | req/s | p50 ms | p99 ms | p99.9 ms | gateway cores | db cores | store tx/req | MHz | vs v0.41.0 | vs main |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| lookup_plain | v0.41.0 | 5 | 671 (661-671) | 1.43 (1.43-1.44) | 2.1 (2.1-2.2) | 3.0 (2.9-3.1) | 0.84 | 0.23 | 5.05 | 4055 |  |  |
| lookup_plain | main | 5 | 685 (664-710) | 1.38 (1.35-1.42) | 2.2 (2.2-2.3) | 3.1 (2.8-3.2) | 0.81 | 0.21 | 3.03 | 4082 | +2%, ranges overlap |  |
| lookup_plain | main+cache | 5 | 912 (908-930) | 1.05 (1.04-1.06) | 1.7 (1.7-1.8) | 2.4 (2.3-2.5) | 0.94 | 0.12 | 0.01 | 4121 | +36%, ranges clear | +33%, ranges clear |
| lookup_gov | v0.41.0 | 5 | 164 (161-168) | 6.00 (5.79-6.06) | 7.6 (7.5-9.0) | 13.6 (10.9-16.3) | 0.89 | 0.09 | 6.15 | 4198 |  |  |
| lookup_gov | main | 5 | 638 (621-655) | 1.48 (1.46-1.51) | 2.4 (2.3-2.5) | 3.3 (3.3-3.5) | 0.80 | 0.22 | 4.04 | 4125 | +289%, ranges clear |  |
| lookup_gov | main+cache | 5 | 804 (802-828) | 1.20 (1.16-1.20) | 1.9 (1.9-1.9) | 2.5 (2.5-2.7) | 0.89 | 0.15 | 1.01 | 4201 | +391%, ranges clear | +26%, ranges clear |

## b3: open loop, latency corrected

| build | offered (x capacity) | offered req/s | achieved req/s | p50 ms | p99 ms | p99.9 ms | ok | n |
|---|---|---|---|---|---|---|---|---|
| main | 0.25 | 185 | 185 (185-185) | 2.00 (1.99-2.01) | 2.9 (2.9-3.2) | 12.0 (9.0-12.9) | 1.0000 | 3 |
| main | 0.50 | 371 | 371 (371-371) | 1.62 (1.59-1.63) | 2.7 (2.6-2.8) | 6.1 (4.6-17.8) | 1.0000 | 3 |
| main | 0.75 | 556 | 556 (556-556) | 1.53 (1.51-1.56) | 3.5 (3.4-3.8) | 13.6 (13.5-17.1) | 1.0000 | 3 |
| main | 0.90 | 668 | 668 (668-668) | 1.60 (1.56-1.62) | 7.9 (6.0-8.3) | 15.6 (15.1-18.7) | 1.0000 | 3 |
| main | 1.00 | 742 | 742 (742-742) | 1.70 (1.67-1.71) | 18.4 (13.5-19.2) | 25.3 (19.4-27.3) | 1.0000 | 3 |
| main | 1.10 | 816 | 765 (758-769) | 410.96 (358.52-447.54) | 1,378.8 (1,292.5-1,535.3) | 1,396.2 (1,307.8-1,561.8) | 1.0000 | 3 |
| main+cache | 0.25 | 218 | 218 (218-218) | 1.53 (1.51-1.61) | 2.3 (2.3-2.4) | 7.8 (7.7-11.6) | 1.0000 | 3 |
| main+cache | 0.50 | 437 | 437 (437-437) | 1.15 (1.14-1.16) | 2.0 (2.0-2.2) | 8.7 (4.5-9.8) | 1.0000 | 3 |
| main+cache | 0.75 | 655 | 655 (655-655) | 1.16 (1.15-1.18) | 3.4 (3.3-4.1) | 14.0 (13.1-15.4) | 1.0000 | 3 |
| main+cache | 0.90 | 787 | 787 (787-787) | 1.19 (1.17-1.19) | 12.2 (10.7-12.6) | 19.4 (19.3-20.5) | 1.0000 | 3 |
| main+cache | 1.00 | 874 | 874 (874-874) | 1.70 (1.50-1.80) | 15.0 (13.6-19.8) | 23.3 (18.5-27.0) | 1.0000 | 3 |
| main+cache | 1.10 | 962 | 917 (915-921) | 347.16 (265.82-359.47) | 1,047.0 (952.2-1,080.3) | 1,062.4 (975.3-1,097.6) | 1.0000 | 3 |

## b4: scale-out, c=64

| scenario | workers | build | n | req/s | p50 ms | p99 ms | gateway cores | req/s per gateway core | db cores | vs main |
|---|---|---|---|---|---|---|---|---|---|---|
| lookup_plain | 1 | main | 3 | 747 (741-749) | 85.0 (84.7-85.7) | 99.4 (98.7-101.3) | 1.20 | 623 | 0.29 |  |
| lookup_plain | 1 | main+cache | 3 | 878 (874-879) | 72.1 (72.1-72.5) | 86.2 (85.5-86.4) | 1.18 | 744 | 0.17 | +17%, ranges clear |
| lookup_plain | 2 | main | 3 | 1,214 (1,212-1,217) | 52.8 (52.3-52.9) | 69.0 (66.5-69.4) | 2.39 | 509 | 0.56 |  |
| lookup_plain | 2 | main+cache | 3 | 1,472 (1,466-1,472) | 45.4 (43.0-46.0) | 61.5 (55.0-63.8) | 2.31 | 636 | 0.31 | +21%, ranges clear |
| lookup_plain | 4 | main | 3 | 1,742 (1,737-1,747) | 38.1 (36.0-38.8) | 67.2 (61.3-72.0) | 4.68 | 372 | 1.04 |  |
| lookup_plain | 4 | main+cache | 3 | 2,156 (2,155-2,160) | 25.7 (24.0-27.4) | 60.8 (60.3-63.6) | 4.57 | 472 | 0.59 | +24%, ranges clear |
| lookup_plain | 8 | main | 3 | 2,241 (2,211-2,241) | 15.0 (14.9-18.4) | 148.3 (121.0-149.1) | 7.23 | 310 | 1.64 |  |
| lookup_plain | 8 | main+cache | 3 | 2,741 (2,721-2,820) | 12.5 (9.3-15.3) | 105.0 (76.0-121.1) | 7.20 | 381 | 0.88 | +22%, ranges clear |
| write_writer | 1 | main | 3 | 742 (742-744) | 85.5 (85.2-85.5) | 101.2 (100.0-102.0) | 1.20 | 619 | 0.33 |  |
| write_writer | 1 | main+cache | 3 | 873 (873-878) | 72.5 (72.1-72.5) | 86.1 (85.6-87.0) | 1.18 | 742 | 0.19 | +18%, ranges clear |
| write_writer | 2 | main | 3 | 1,202 (1,201-1,203) | 52.9 (52.7-54.5) | 66.6 (66.3-72.8) | 2.39 | 504 | 0.64 |  |
| write_writer | 2 | main+cache | 3 | 1,448 (1,444-1,452) | 43.9 (43.6-46.3) | 56.2 (54.5-65.1) | 2.32 | 625 | 0.36 | +20%, ranges clear |
| write_writer | 4 | main | 3 | 1,734 (1,728-1,738) | 34.5 (32.8-37.3) | 66.9 (56.5-67.1) | 4.68 | 371 | 1.22 |  |
| write_writer | 4 | main+cache | 3 | 2,114 (2,111-2,130) | 25.5 (23.0-30.4) | 64.3 (45.7-68.2) | 4.55 | 464 | 0.65 | +22%, ranges clear |
| write_writer | 8 | main | 3 | 2,200 (2,140-2,201) | 15.3 (14.1-16.6) | 144.4 (131.1-155.7) | 7.21 | 305 | 1.86 |  |
| write_writer | 8 | main+cache | 3 | 2,617 (2,587-2,729) | 12.4 (9.2-21.9) | 102.1 (57.2-117.9) | 7.02 | 373 | 1.02 | +19%, ranges clear |

## b5: cold path over 150 s (per-request durations from the access log)

### lookup_gov: 106,741 requests - p50 9.25 ms, p99 14.66, p99.9 19.20, p99.99 73.72, max 146.9

Median of each second's slowest request: 17.0 ms. Seconds whose slowest is over 4x that: t=1 s (147 ms), t=61 s (76 ms), t=121 s (74 ms)

### lookup_plain: 115,314 requests - p50 8.58 ms, p99 13.57, p99.9 16.95, p99.99 20.89, max 26.0

Median of each second's slowest request: 15.9 ms. Seconds whose slowest is over 4x that: none


## b6: access log

| access log | n | req/s | p50 ms | p99 ms | gateway cores |
|---|---|---|---|---|---|
| on | 5 | 736 (735-739) | 43.00 (42.78-43.00) | 54.4 (53.7-54.5) | 1.20 |
| off | 5 | 785 (782-792) | 40.17 (39.88-40.36) | 51.7 (50.2-52.0) | 1.21 |

off against on: +7%, ranges clear

## b7: c=1

| scenario | build | n | req/s | p50 ms | p99 ms | p99.9 ms | gateway cores | db cores | store tx/req | MHz | vs v0.41.0 | vs main |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| lookup_plain | v0.41.0 | 3 | 132 (132-134) | 7.63 (7.54-7.64) | 8.9 (8.9-9.0) | 9.5 (9.3-9.5) | 0.25 | 0.07 | 5.91 | 3645 |  |  |
| lookup_plain | main | 3 | 161 (159-162) | 6.24 (6.22-6.32) | 7.2 (7.2-7.3) | 7.7 (7.6-8.2) | 0.26 | 0.07 | 3.48 | 3817 | +22%, ranges clear |  |
| lookup_plain | main+cache | 3 | 270 (268-270) | 3.69 (3.66-3.72) | 4.3 (4.3-4.3) | 5.6 (5.5-5.8) | 0.34 | 0.04 | 0.01 | 3940 | +104%, ranges clear | +67%, ranges clear |

## b7: c=32

| scenario | build | n | req/s | p50 ms | p99 ms | p99.9 ms | gateway cores | db cores | store tx/req | MHz | vs v0.41.0 | vs main |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| lookup_plain | v0.41.0 | 3 | 677 (672-752) | 46.78 (42.18-47.20) | 57.4 (50.1-58.2) | 61.2 (54.3-67.5) | 1.25 | 0.37 | 5.02 | 3654 |  |  |
| lookup_plain | main | 3 | 741 (735-750) | 42.71 (42.45-42.88) | 54.2 (52.0-54.9) | 59.6 (56.0-62.7) | 1.22 | 0.27 | 3.02 | 3734 | +9%, ranges overlap |  |
| lookup_plain | main+cache | 3 | 860 (857-870) | 36.71 (36.37-36.88) | 46.5 (46.1-46.6) | 50.2 (49.9-51.5) | 1.19 | 0.15 | 0.01 | 3776 | +27%, ranges clear | +16%, ranges clear |

## b8: mixed and hot

| scenario | n | req/s | p50 ms | p99 ms |
|---|---|---|---|---|
| mixed_reads | 3 | 547 (544-549) | 43.34 (43.16-43.62) | 54.8 (54.3-55.1) |
| mixed_writes | 3 | 181 (180-182) | 43.60 (43.38-43.91) | 54.9 (54.3-55.6) |
| write_writer | 3 | 734 (729-734) | 43.08 (42.97-43.36) | 55.0 (54.4-55.1) |
| hotwrite_writer | 3 | 735 (731-740) | 42.98 (42.71-43.21) | 54.7 (53.9-55.2) |

## b10: threads per worker, c=32

| threads | n | req/s | p50 ms | p99 ms | gateway cores |
|---|---|---|---|---|---|
| 1 | 3 | 778 (777-828) | 40.31 (37.82-40.37) | 52.5 (49.3-52.8) | 0.88 |
| 2 | 3 | 832 (831-902) | 37.70 (34.83-37.78) | 49.1 (44.5-49.2) | 1.12 |
| 4 | 3 | 763 (760-763) | 41.21 (41.19-41.30) | 53.3 (52.6-53.4) | 1.18 |
| 8 | 3 | 723 (722-725) | 43.60 (43.49-43.64) | 56.4 (55.7-57.4) | 1.20 |

## b9: profile, main (8,550 samples, threads on CPU only)

| category | share |
|---|---|
| store layer: queries, decoding, checks | 28.2% |
| logging | 18.4% |
| gunicorn, sockets, HTTP | 16.8% |
| data query: driver and pool | 12.3% |
| Flask / Werkzeug | 9.9% |
| app request handling | 4.6% |
| history writer thread | 3.0% |
| auth, limits, quotas, metrics | 2.5% |
| SQL checks, parameters, masking | 1.7% |
| JSON | 1.3% |
| other | 1.3% |

| busiest attributed frames | share |
|---|---|
| `execute (queryapigate/db.py:649)` | 17.1% |
| `acquire (logging/__init__.py:973)` | 8.4% |
| `refuse_store_access (queryapigate/store.py:229)` | 7.0% |
| `write (gunicorn/util.py:348)` | 5.9% |
| `query (queryapigate/runners.py:281)` | 5.7% |
| `_fetch_page (queryapigate/runners.py:82)` | 3.6% |
| `emit (logging/__init__.py:1163)` | 3.2% |
| `notify (gunicorn/workers/workertmp.py:44)` | 3.0% |
| `flush (logging/__init__.py:1144)` | 3.0% |
| `reset (queryapigate/runners.py:273)` | 2.3% |
| `chunk (gunicorn/http/unreader.py:77)` | 2.1% |
| `execute (queryapigate/db.py:648)` | 1.0% |

## b9: profile, main+cache (8,338 samples, threads on CPU only)

| category | share |
|---|---|
| gunicorn, sockets, HTTP | 21.0% |
| logging | 20.4% |
| data query: driver and pool | 15.4% |
| store layer: queries, decoding, checks | 13.4% |
| Flask / Werkzeug | 11.9% |
| app request handling | 5.6% |
| history writer thread | 3.7% |
| auth, limits, quotas, metrics | 2.8% |
| JSON | 2.6% |
| SQL checks, parameters, masking | 1.9% |
| other | 1.3% |

| busiest attributed frames | share |
|---|---|
| `acquire (logging/__init__.py:973)` | 10.2% |
| `refuse_store_access (queryapigate/store.py:229)` | 9.6% |
| `write (gunicorn/util.py:348)` | 7.1% |
| `query (queryapigate/runners.py:281)` | 7.0% |
| `_fetch_page (queryapigate/runners.py:82)` | 4.5% |
| `notify (gunicorn/workers/workertmp.py:44)` | 3.6% |
| `emit (logging/__init__.py:1163)` | 3.2% |
| `flush (logging/__init__.py:1144)` | 3.0% |
| `reset (queryapigate/runners.py:273)` | 2.9% |
| `chunk (gunicorn/http/unreader.py:77)` | 2.6% |
| `iterencode (json/encoder.py:258)` | 1.2% |
| `executemany (queryapigate/db.py:657)` | 1.0% |

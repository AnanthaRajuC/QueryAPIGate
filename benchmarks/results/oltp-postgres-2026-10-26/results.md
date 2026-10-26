## Environment

```
{
 "block": "env",
 "scenario": "env",
 "profile": "performance",
 "ac": "1",
 "epp": "performance",
 "pkg_temp_c": 64.0,
 "load1": 8.34130859375,
 "builds": [
  "v0.42.0",
  "main",
  "main+cache"
 ],
 "refs": {
  "v0.42.0": "647ab6f739adc930d11ab5aa1db259dfa27daf1a",
  "main": "fe603c067fc27724a6361e94ff5b2272caf697a4",
  "main+cache": "fe603c067fc27724a6361e94ff5b2272caf697a4"
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
 "ts": "2026-10-26T22:00:13"
}
```
```
{
 "block": "env",
 "scenario": "env",
 "profile": "performance",
 "ac": "1",
 "epp": "performance",
 "pkg_temp_c": 81.0,
 "load1": 0.49560546875,
 "builds": [
  "v0.42.0",
  "main",
  "main+cache"
 ],
 "refs": {
  "v0.42.0": "647ab6f739adc930d11ab5aa1db259dfa27daf1a",
  "main": "fe603c067fc27724a6361e94ff5b2272caf697a4",
  "main+cache": "fe603c067fc27724a6361e94ff5b2272caf697a4"
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
 "ts": "2026-10-27T00:28:58"
}
```

## /health floors

| block | when | req/s | p50 ms | power profile | AC | EPP | package C | MHz |
|---|---|---|---|---|---|---|---|---|
| b1 | start | 2,229 | 0.44 | performance | 1 | performance | 77.0 | 4027 |
| b1 | end | 2,246 | 0.44 | performance | 1 | performance | 81.0 | 3905 |
| b2 | start | 2,253 | 0.44 | performance | 1 | performance | 75.0 | 3821 |
| b2 | end | 2,265 | 0.44 | performance | 1 | performance | 81.0 | 3747 |
| b3 | start | 2,185 | 0.45 | performance | 1 | performance | 75.0 | 4448 |
| b3 | end | 2,306 | 0.43 | performance | 1 | performance | 72.0 | 1942 |
| b5 | start | 2,273 | 0.44 | performance | 1 | performance | 79.0 | 4381 |
| b5 | end | 2,317 | 0.43 | performance | 1 | performance | 71.0 | 4572 |
| b7 | start | 2,257 | 0.44 | performance | 1 | performance | 73.0 | 3514 |
| b7 | end | 2,323 | 0.43 | performance | 1 | performance | 68.0 | 3611 |
| b10 | start | 2,289 | 0.43 | performance | 1 | performance | 79.0 | 4599 |
| b10 | end | 1,978 | 0.50 | performance | 1 | performance | 80.0 | 431 |
| b11 | start | 2,019 | 0.49 | performance | 1 | performance | 84.0 | 3707 |
| b11 | end | 1,978 | 0.50 | performance | 1 | performance | 73.0 | 940 |

## pgbench

| workload | tps | latency ms | n |
|---|---|---|---|
| pk_c1 | 39,579 (39,100-40,702) | 0.025 (0.025-0.026) | 5 |
| pk_c32 | 130,738 (127,754-133,558) | 0.245 (0.240-0.250) | 5 |
| tpcb_c32 | 16,696 (16,482-16,902) | 1.917 (1.893-1.942) | 5 |

## b1: c=32

| scenario | build | n | req/s | p50 ms | p99 ms | p99.9 ms | gateway cores | db cores | load cores | store tx/req | MHz | vs v0.42.0 | vs main |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| lookup_plain | v0.42.0 | 5 | 786 (751-829) | 40.27 (38.26-42.16) | 51.9 (46.8-52.5) | 59.0 (51.5-63.1) | 1.21 | 0.29 | 0.21 | 3.02 | 3908 |  |  |
| lookup_plain | main | 5 | 852 (827-892) | 37.30 (35.51-38.26) | 46.6 (43.5-47.7) | 52.5 (48.2-53.2) | 1.17 | 0.29 | 0.20 | 3.02 | 3941 | +8%, ranges overlap |  |
| lookup_plain | main+cache | 5 | 1,012 (991-1,043) | 31.26 (30.32-31.87) | 38.7 (37.8-40.1) | 44.1 (40.8-45.2) | 1.13 | 0.17 | 0.20 | 0.01 | 4024 | +29%, ranges clear | +19%, ranges clear |
| lookup_gov | v0.42.0 | 5 | 738 (713-771) | 43.19 (41.18-44.54) | 53.6 (50.2-54.4) | 59.6 (55.0-64.5) | 1.21 | 0.30 | 0.16 | 4.02 | 3921 |  |  |
| lookup_gov | main | 5 | 809 (803-856) | 39.26 (36.94-39.53) | 48.0 (46.0-48.4) | 54.1 (52.1-57.4) | 1.17 | 0.28 | 0.16 | 3.02 | 3879 | +10%, ranges clear |  |
| lookup_gov | main+cache | 5 | 964 (956-992) | 32.89 (31.94-33.13) | 40.5 (39.5-41.1) | 45.7 (45.0-47.2) | 1.12 | 0.16 | 0.16 | 0.01 | 4030 | +31%, ranges clear | +19%, ranges clear |
| page_plain | v0.42.0 | 5 | 682 (661-694) | 46.61 (45.72-48.10) | 57.5 (56.2-57.8) | 63.4 (60.1-63.8) | 1.20 | 0.37 | 0.16 | 3.02 | 3907 |  |  |
| page_plain | main | 5 | 725 (710-758) | 43.81 (41.82-44.73) | 53.7 (51.3-54.5) | 58.4 (56.1-62.2) | 1.17 | 0.39 | 0.16 | 3.02 | 3913 | +6%, ranges clear |  |
| page_plain | main+cache | 5 | 835 (833-873) | 38.02 (36.29-38.10) | 46.0 (44.7-46.3) | 49.3 (48.0-52.8) | 1.13 | 0.27 | 0.16 | 0.01 | 3992 | +22%, ranges clear | +15%, ranges clear |
| write_writer | v0.42.0 | 5 | 793 (764-836) | 40.11 (37.92-41.51) | 50.3 (46.7-51.0) | 56.7 (54.5-58.7) | 1.21 | 0.33 | 0.16 | 3.02 | 3861 |  |  |
| write_writer | main | 5 | 850 (832-887) | 37.50 (35.52-38.03) | 46.4 (44.5-46.7) | 50.8 (49.6-53.9) | 1.17 | 0.33 | 0.16 | 3.02 | 3832 | +7%, ranges overlap |  |
| write_writer | main+cache | 5 | 1,002 (999-1,046) | 31.57 (30.19-31.64) | 38.8 (37.6-39.3) | 42.4 (40.9-43.7) | 1.13 | 0.17 | 0.16 | 0.01 | 3913 | +26%, ranges clear | +18%, ranges clear |

## b2: c=1

| scenario | build | n | req/s | p50 ms | p99 ms | p99.9 ms | gateway cores | db cores | load cores | store tx/req | MHz | vs v0.42.0 | vs main |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| lookup_plain | v0.42.0 | 5 | 702 (680-709) | 1.36 (1.34-1.41) | 2.2 (2.2-2.2) | 4.3 (4.0-4.3) | 0.81 | 0.21 | 0.14 | 3.03 | 4166 |  |  |
| lookup_plain | main | 5 | 743 (722-769) | 1.27 (1.23-1.30) | 2.1 (2.0-2.2) | 4.2 (4.1-4.3) | 0.79 | 0.22 | 0.14 | 3.04 | 4154 | +6%, ranges clear |  |
| lookup_plain | main+cache | 5 | 966 (965-993) | 1.00 (0.99-1.01) | 1.6 (1.6-1.7) | 3.7 (3.7-3.8) | 0.88 | 0.14 | 0.16 | 0.01 | 4354 | +38%, ranges clear | +30%, ranges clear |
| lookup_gov | v0.42.0 | 5 | 638 (628-659) | 1.49 (1.44-1.51) | 2.4 (2.3-2.4) | 4.3 (4.2-4.4) | 0.79 | 0.21 | 0.14 | 4.05 | 4170 |  |  |
| lookup_gov | main | 5 | 719 (696-735) | 1.31 (1.27-1.35) | 2.2 (2.2-2.3) | 4.2 (4.2-4.3) | 0.79 | 0.21 | 0.14 | 3.03 | 4243 | +13%, ranges clear |  |
| lookup_gov | main+cache | 5 | 932 (912-944) | 1.05 (1.03-1.06) | 1.7 (1.7-1.7) | 3.8 (3.8-3.9) | 0.87 | 0.14 | 0.15 | 0.01 | 4356 | +46%, ranges clear | +30%, ranges clear |

## b3: capacity (closed loop, c=32), then open loop, latency corrected

- capacity, main: 892 (834-896) req/s over 5 rounds

- capacity, main+cache: 1,042 (1,001-1,053) req/s over 5 rounds

| build | offered (x capacity) | offered req/s | achieved req/s | p50 ms | p99 ms | p99.9 ms | ok | n |
|---|---|---|---|---|---|---|---|---|
| main | 0.25 | 222 | 222 (222-222) | 2.05 (1.83-2.06) | 3.0 (2.7-3.1) | 11.0 (9.8-13.0) | 1.0000 | 5 |
| main | 0.50 | 445 | 445 (445-445) | 1.50 (1.41-1.60) | 2.8 (2.6-2.9) | 7.5 (6.1-13.2) | 1.0000 | 5 |
| main | 0.75 | 668 | 668 (668-668) | 1.44 (1.40-1.51) | 4.6 (3.0-6.2) | 14.2 (12.7-17.2) | 1.0000 | 5 |
| main | 0.90 | 802 | 802 (802-802) | 1.58 (1.52-1.67) | 15.9 (9.6-17.9) | 20.5 (14.5-28.9) | 1.0000 | 5 |
| main | 1.00 | 891 | 891 (888-891) | 64.42 (11.65-115.15) | 107.9 (25.6-221.2) | 110.3 (27.9-223.1) | 1.0000 | 5 |
| main | 1.10 | 980 | 894 (892-897) | 972.48 (920.08-980.75) | 1,859.6 (1,807.1-1,907.4) | 1,881.9 (1,819.1-1,922.6) | 1.0000 | 5 |
| main+cache | 0.25 | 260 | 260 (260-260) | 1.35 (1.32-1.39) | 2.1 (2.0-2.2) | 10.4 (8.4-14.2) | 1.0000 | 5 |
| main+cache | 0.50 | 521 | 521 (521-521) | 1.10 (1.09-1.13) | 2.1 (2.0-2.4) | 9.3 (6.1-11.1) | 1.0000 | 5 |
| main+cache | 0.75 | 781 | 781 (781-781) | 1.10 (1.06-1.11) | 4.3 (3.1-8.9) | 13.0 (11.6-21.4) | 1.0000 | 5 |
| main+cache | 0.90 | 937 | 937 (937-937) | 1.13 (1.12-1.15) | 13.3 (10.8-14.3) | 19.0 (17.5-20.7) | 1.0000 | 5 |
| main+cache | 1.00 | 1,042 | 1,041 (1,039-1,042) | 88.22 (27.96-105.11) | 141.0 (62.0-173.5) | 150.0 (63.5-177.4) | 1.0000 | 5 |
| main+cache | 1.10 | 1,146 | 1,045 (1,044-1,045) | 939.13 (912.99-992.30) | 1,861.1 (1,845.6-1,865.6) | 1,875.0 (1,862.3-1,888.2) | 1.0000 | 5 |

## b4: scale-out, c=64

| scenario | workers | build | n | req/s | p50 ms | p99 ms | gateway cores | req/s per gateway core | db cores | load cores | vs main |
|---|---|---|---|---|---|---|---|---|---|---|---|

## b5: cold path over 150 s (per-request durations from the access log)

### lookup_gov: 131,380 requests - p50 7.59 ms, p99 10.82, p99.9 12.96, p99.99 22.33, max 138.8

Median of each second's slowest request: 12.6 ms. Seconds whose slowest is over 4x that: t=0 s (139 ms)

### lookup_plain: 131,380 requests - p50 7.62 ms, p99 10.92, p99.9 13.60, p99.99 18.89, max 22.0

Median of each second's slowest request: 12.8 ms. Seconds whose slowest is over 4x that: none


## b6: access log

| access log | n | req/s | p50 ms | p99 ms | gateway cores |
|---|---|---|---|---|---|

## b7: c=1

| scenario | build | n | req/s | p50 ms | p99 ms | p99.9 ms | gateway cores | db cores | load cores | store tx/req | MHz | vs v0.42.0 | vs main |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| lookup_plain | v0.42.0 | 5 | 153 (150-156) | 6.58 (6.47-6.64) | 7.4 (7.3-7.7) | 7.9 (7.8-9.1) | 0.25 | 0.06 | 0.06 | 3.51 | 3634 |  |  |
| lookup_plain | main | 5 | 226 (220-233) | 4.39 (4.32-4.51) | 5.2 (5.1-5.6) | 5.5 (5.3-7.3) | 0.31 | 0.09 | 0.07 | 3.35 | 3855 | +48%, ranges clear |  |
| lookup_plain | main+cache | 5 | 604 (558-615) | 1.60 (1.57-1.78) | 2.2 (2.1-2.2) | 2.8 (2.7-2.9) | 0.59 | 0.07 | 0.11 | 0.01 | 4145 | +296%, ranges clear | +167%, ranges clear |

## b7: c=32

| scenario | build | n | req/s | p50 ms | p99 ms | p99.9 ms | gateway cores | db cores | load cores | store tx/req | MHz | vs v0.42.0 | vs main |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| lookup_plain | v0.42.0 | 5 | 826 (770-832) | 38.39 (38.13-41.19) | 46.1 (45.8-50.0) | 52.3 (48.8-57.8) | 1.22 | 0.26 | 0.15 | 3.01 | 4100 |  |  |
| lookup_plain | main | 5 | 888 (829-892) | 35.60 (35.52-38.30) | 44.9 (42.8-46.0) | 50.0 (49.6-51.2) | 1.19 | 0.25 | 0.17 | 3.01 | 4100 | +7%, ranges overlap |  |
| lookup_plain | main+cache | 5 | 1,029 (999-1,033) | 30.73 (30.65-31.68) | 38.5 (37.2-38.8) | 44.0 (42.7-44.7) | 1.14 | 0.13 | 0.16 | 0.01 | 4100 | +25%, ranges clear | +16%, ranges clear |

## b8: mixed and hot

| scenario | n | req/s | p50 ms | p99 ms |
|---|---|---|---|---|

## b10: threads per worker, c=32

| threads | n | req/s | p50 ms | p99 ms | gateway cores |
|---|---|---|---|---|---|
| 1 | 5 | 904 (888-922) | 34.16 (33.75-35.32) | 44.8 (42.6-46.6) | 0.87 |
| 2 | 5 | 1,012 (960-1,016) | 31.18 (30.73-32.91) | 39.8 (38.6-40.8) | 1.11 |
| 4 | 5 | 928 (880-939) | 34.07 (33.73-35.96) | 42.1 (41.3-44.8) | 1.16 |
| 8 | 5 | 896 (839-903) | 35.30 (35.07-37.76) | 43.8 (42.9-46.1) | 1.17 |

## b11: single-call latency against network delay (p50 ms; the slope is round trips per call)

| | +0ms | +0.5ms | +1ms | +2ms | round trips (slope) | at 0 delay (intercept) |
|---|---|---|---|---|---|---|
| psycopg2 alone, one | 0.02 (0.02-0.02) | 0.67 (0.67-0.68) | 1.23 (1.23-1.23) | 2.53 (2.53-2.53) | 1.25 | 0.02 ms |
| psycopg2 alone, four | 0.05 (0.05-0.06) | 2.51 (2.46-2.56) | 4.80 (4.80-4.81) | 9.90 (9.63-9.91) | 4.87 | 0.03 ms |
| v0.42.0 | 1.38 (1.38-1.40) | 6.62 (6.62-6.64) | 10.22 (10.16-10.35) | 19.44 (19.38-19.53) | 8.90 | 1.64 ms |
| main | 1.26 (1.26-1.26) | 4.54 (4.43-4.58) | 6.86 (6.63-6.90) | 11.92 (11.90-11.97) | 5.25 | 1.54 ms |
| main+cache | 0.98 (0.97-1.02) | 1.67 (1.61-1.69) | 2.36 (2.34-2.37) | 3.46 (3.37-3.56) | 1.24 | 1.03 ms |

## b9: profile, main (8,378 samples, threads on CPU only)

| category | share |
|---|---|
| store layer: queries, decoding, checks | 22.9% |
| logging | 21.2% |
| gunicorn, sockets, HTTP | 18.7% |
| Flask / Werkzeug | 10.8% |
| data query: driver and pool | 7.6% |
| app request handling | 5.6% |
| SQL checks, parameters, masking | 3.2% |
| auth, limits, quotas, metrics | 3.1% |
| history writer thread | 2.9% |
| other | 2.3% |
| JSON | 1.6% |

| busiest attributed frames | share |
|---|---|
| `execute (queryapigate/db.py:649)` | 18.7% |
| `acquire (logging/__init__.py:973)` | 10.3% |
| `_fetch_page (queryapigate/runners.py:86)` | 6.3% |
| `write (gunicorn/util.py:348)` | 6.2% |
| `emit (logging/__init__.py:1163)` | 3.5% |
| `flush (logging/__init__.py:1144)` | 3.2% |
| `notify (gunicorn/workers/workertmp.py:44)` | 3.0% |
| `chunk (gunicorn/http/unreader.py:77)` | 2.7% |
| `execute (queryapigate/db.py:648)` | 1.2% |
| `iterencode (json/encoder.py:258)` | 0.9% |
| `executemany (queryapigate/db.py:657)` | 0.8% |
| `defer (gunicorn/workers/gthread.py:216)` | 0.8% |

## b9: profile, main+cache (8,058 samples, threads on CPU only)

| category | share |
|---|---|
| logging | 25.2% |
| gunicorn, sockets, HTTP | 22.3% |
| Flask / Werkzeug | 14.1% |
| data query: driver and pool | 10.5% |
| app request handling | 6.9% |
| store layer: queries, decoding, checks | 4.5% |
| SQL checks, parameters, masking | 4.3% |
| history writer thread | 3.9% |
| auth, limits, quotas, metrics | 3.6% |
| JSON | 3.1% |
| other | 1.5% |

| busiest attributed frames | share |
|---|---|
| `acquire (logging/__init__.py:973)` | 12.8% |
| `_fetch_page (queryapigate/runners.py:86)` | 8.4% |
| `write (gunicorn/util.py:348)` | 7.5% |
| `emit (logging/__init__.py:1163)` | 3.6% |
| `flush (logging/__init__.py:1144)` | 3.4% |
| `chunk (gunicorn/http/unreader.py:77)` | 3.2% |
| `notify (gunicorn/workers/workertmp.py:44)` | 3.0% |
| `iterencode (json/encoder.py:258)` | 1.3% |
| `defer (gunicorn/workers/gthread.py:216)` | 1.0% |
| `executemany (queryapigate/db.py:657)` | 0.9% |
| `<genexpr> (queryapigate/tracing.py:40)` | 0.8% |
| `register (selectors.py:359)` | 0.8% |

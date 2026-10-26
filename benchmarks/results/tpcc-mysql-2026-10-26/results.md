## Environment

```
{
 "block": "env",
 "scenario": "env",
 "profile": "performance",
 "ac": "1",
 "epp": "performance",
 "pkg_temp_c": 61.0,
 "load1": 1.0048828125,
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
 "mysql_connector": "9.3.0",
 "mysql": "8.4.13",
 "warehouses": 20,
 "customers": 600000,
 "zipf_s": 1.0,
 "oha": "ghcr.io/hatoo/oha@sha256:3ec3dbf549ea197793482d47a6324797411406bbf438c2fe8b91f244ec641a2f",
 "kernel": "6.8.0-147-generic",
 "cpu": "12th Gen Intel(R) Core(TM) i7-12700H",
 "cpus": {
  "gateway": "0-7",
  "database": "8-11",
  "load": "12-19"
 },
 "tables": [
  "billing.history=600271",
  "catalog.item=99297",
  "crm.customer=583929",
  "inventory.stock=1961966",
  "orders.new_order=197118",
  "orders.order_line=6078981",
  "orders.orders=625911",
  "org.district=200",
  "org.warehouse=20"
 ],
 "ts": "2026-10-26T18:32:43"
}
```
```
{
 "block": "env",
 "scenario": "env",
 "profile": "performance",
 "ac": "1",
 "epp": "performance",
 "pkg_temp_c": 72.0,
 "load1": 16.3212890625,
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
 "mysql_connector": "9.3.0",
 "mysql": "8.4.13",
 "warehouses": 20,
 "customers": 600000,
 "zipf_s": 1.0,
 "oha": "ghcr.io/hatoo/oha@sha256:3ec3dbf549ea197793482d47a6324797411406bbf438c2fe8b91f244ec641a2f",
 "kernel": "6.8.0-147-generic",
 "cpu": "12th Gen Intel(R) Core(TM) i7-12700H",
 "cpus": {
  "gateway": "0-7",
  "database": "8-11",
  "load": "12-19"
 },
 "tables": [
  "billing.history=600271",
  "catalog.item=99297",
  "crm.customer=583929",
  "inventory.stock=1961966",
  "orders.new_order=197118",
  "orders.order_line=6078981",
  "orders.orders=625911",
  "org.district=200",
  "org.warehouse=20"
 ],
 "ts": "2026-10-26T21:59:27"
}
```

## /health floors

| block | when | req/s | p50 ms | power profile | AC | EPP | package C | MHz |
|---|---|---|---|---|---|---|---|---|
| m2 | start | 2,228 | 0.44 | performance | 1 | performance | 70.0 | 4068 |
| m2 | end | 2,235 | 0.44 | performance | 1 | performance | 73.0 | 669 |
| m3 | start | 2,258 | 0.44 | performance | 1 | performance | 72.0 | 1257 |
| m3 | end | 2,311 | 0.43 | performance | 1 | performance | 75.0 | 2802 |
| m8 | start | 2,274 | 0.43 | performance | 1 | performance | 77.0 | 3718 |
| m8 | end | 2,252 | 0.44 | performance | 1 | performance | 61.0 | 402 |
| m9 | start | 2,270 | 0.44 | performance | 1 | performance | 69.0 | 2926 |
| m9 | end | 1,983 | 0.50 | performance | 1 | performance | 73.0 | 4127 |
| n1 | start | 2,186 | 0.45 | performance | 1 | performance | 72.0 | 4700 |
| n1 | end | 2,256 | 0.44 | performance | 1 | performance | 69.0 | 1480 |
| n2 | start | 2,250 | 0.44 | performance | 1 | performance | 71.0 | 4634 |
| n2 | end | 2,192 | 0.45 | performance | 1 | performance | 69.0 | 3596 |
| n3 | start | 2,213 | 0.45 | performance | 1 | performance | 72.0 | 4013 |
| n3 | end | 2,218 | 0.45 | performance | 1 | performance | 80.0 | 4163 |
| n4 | start | 2,104 | 0.47 | performance | 1 | performance | 74.0 | 3112 |
| n4 | end | 2,230 | 0.45 | performance | 1 | performance | 69.0 | 3266 |
| n5 | start | 2,110 | 0.47 | performance | 1 | performance | 68.0 | 1497 |
| n5 | end | 1,990 | 0.50 | performance | 1 | performance | 64.0 | 3166 |
| n6 | start | 1,991 | 0.50 | performance | 1 | performance | 73.0 | 4338 |
| n6 | end | 2,295 | 0.43 | performance | 1 | performance | 83.0 | 4600 |
| n9 | start | 2,295 | 0.43 | performance | 1 | performance | 79.0 | 3913 |
| n9 | end | 2,216 | 0.45 | performance | 1 | performance | 72.0 | 3406 |

## m1: MySQL on its own (sysbench)

| test | threads | tps | p50 ms | p99 ms | n |
|---|---|---|---|---|---|

## m2: c=32

| query | build | n | req/s | p50 ms | p99 ms | p99.9 ms | gateway cores | db cores | load cores | MySQL stmts/req | vs v0.42.0 | vs main | direct tps | gateway, % of direct |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| customer | v0.42.0 | 5 | 647 (636-693) | 48.97 (46.00-49.98) | 60.6 (54.5-62.3) | 66.3 (59.6-75.8) | 1.27 | 0.07 | 0.24 | 2.00 |  |  | - | - |
| customer | main | 5 | 743 (726-795) | 42.84 (40.00-43.77) | 52.7 (48.6-53.4) | 58.5 (57.6-60.4) | 1.31 | 0.08 | 0.23 | 2.00 | +15%, ranges clear |  | - | - |
| customer | main+cache | 5 | 875 (831-883) | 36.42 (36.06-38.30) | 42.4 (41.9-45.5) | 51.5 (48.6-53.0) | 1.18 | 0.10 | 0.26 | 2.00 | +35%, ranges clear | +18%, ranges clear | - | - |
| order_status | v0.42.0 | 5 | 573 (567-604) | 55.69 (52.79-56.17) | 64.9 (62.1-68.9) | 70.8 (69.0-81.4) | 1.26 | 0.12 | 0.12 | 2.00 |  |  | - | - |
| order_status | main | 5 | 662 (644-670) | 48.18 (47.56-49.44) | 57.6 (56.7-59.4) | 65.2 (62.9-65.8) | 1.31 | 0.14 | 0.14 | 2.00 | +16%, ranges clear |  | - | - |
| order_status | main+cache | 5 | 727 (721-753) | 43.94 (42.45-44.36) | 49.5 (47.3-49.9) | 56.1 (54.3-57.6) | 1.19 | 0.17 | 0.15 | 2.00 | +27%, ranges clear | +10%, ranges clear | - | - |
| stock_level | v0.42.0 | 5 | 630 (628-677) | 50.68 (47.01-50.87) | 58.8 (56.5-59.3) | 64.9 (63.1-65.5) | 1.24 | 0.39 | 0.14 | 2.00 |  |  | - | - |
| stock_level | main | 5 | 751 (733-795) | 42.73 (39.88-43.55) | 50.2 (48.6-50.8) | 57.1 (55.7-57.5) | 1.31 | 0.46 | 0.16 | 2.00 | +19%, ranges clear |  | - | - |
| stock_level | main+cache | 5 | 833 (829-902) | 38.34 (35.38-38.61) | 42.8 (41.3-42.9) | 50.4 (47.4-51.0) | 1.19 | 0.52 | 0.19 | 2.00 | +32%, ranges clear | +11%, ranges clear | - | - |

## m3: c=1

| query | build | n | req/s | p50 ms | p99 ms | p99.9 ms | gateway cores | db cores | load cores | MySQL stmts/req | vs v0.42.0 | vs main | direct p50 ms | gateway adds (p50) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| customer | v0.42.0 | 5 | 773 (768-775) | 1.26 (1.26-1.28) | 1.7 (1.7-1.7) | 4.0 (1.9-4.1) | 0.94 | 0.05 | 0.13 | 2.00 |  |  | - | - |
| customer | main | 5 | 857 (843-869) | 1.13 (1.12-1.15) | 1.5 (1.5-1.6) | 3.9 (3.8-4.0) | 0.94 | 0.05 | 0.14 | 2.00 | +11%, ranges clear |  | - | - |
| customer | main+cache | 5 | 910 (903-915) | 1.07 (1.07-1.08) | 1.4 (1.4-1.4) | 3.8 (2.4-3.8) | 0.95 | 0.05 | 0.15 | 2.00 | +18%, ranges clear | +6%, ranges clear | - | - |
| order_status | v0.42.0 | 5 | 665 (653-668) | 1.47 (1.46-1.50) | 1.9 (1.9-2.0) | 2.3 (2.2-4.3) | 0.91 | 0.08 | 0.12 | 2.00 |  |  | - | - |
| order_status | main | 5 | 686 (680-693) | 1.41 (1.40-1.43) | 1.9 (1.9-2.0) | 2.4 (2.3-4.3) | 0.88 | 0.10 | 0.12 | 2.00 | +3%, ranges clear |  | - | - |
| order_status | main+cache | 5 | 719 (712-733) | 1.35 (1.31-1.36) | 1.9 (1.9-1.9) | 2.2 (2.1-4.3) | 0.88 | 0.10 | 0.13 | 2.00 | +8%, ranges clear | +5%, ranges clear | - | - |
| stock_level | v0.42.0 | 5 | 566 (560-572) | 1.71 (1.70-1.73) | 2.3 (2.3-2.3) | 4.6 (2.5-4.8) | 0.71 | 0.26 | 0.12 | 2.00 |  |  | - | - |
| stock_level | main | 5 | 562 (554-595) | 1.76 (1.63-1.77) | 2.3 (2.2-2.3) | 4.6 (2.5-4.8) | 0.65 | 0.28 | 0.11 | 2.00 | -1%, ranges overlap |  | - | - |
| stock_level | main+cache | 5 | 588 (573-606) | 1.65 (1.61-1.72) | 2.2 (2.2-2.2) | 4.5 (2.3-4.6) | 0.65 | 0.29 | 0.12 | 2.00 | +4%, ranges clear | +5%, ranges overlap | - | - |

## m4: capacity (closed loop, c=32), then open loop on order_status, latency corrected

| build | offered (x capacity) | offered req/s | achieved req/s | p50 ms | p99 ms | p99.9 ms | ok | n |
|---|---|---|---|---|---|---|---|---|

## m5: scale-out, c=64

| query | workers | build | n | req/s | p50 ms | p99 ms | gateway cores | req/s per gateway core | db cores | load cores | vs main |
|---|---|---|---|---|---|---|---|---|---|---|---|

## m6: threads per worker, c=32

| query | threads | n | req/s | p50 ms | p99 ms | gateway cores |
|---|---|---|---|---|---|---|

## m7: uniform against Zipf-distributed customers, with and without a 30 s response cache

| customers | query | n | req/s | p50 ms | p99 ms | cache hit rate | MySQL stmts/req |
|---|---|---|---|---|---|---|---|

## m8: c=1

| query | build | n | req/s | p50 ms | p99 ms | p99.9 ms | gateway cores | db cores | load cores | MySQL stmts/req | vs v0.42.0 | vs main |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| order_status | v0.42.0 | 5 | 271 (267-278) | 3.71 (3.63-3.74) | 4.1 (4.1-4.2) | 4.5 (4.3-6.4) | 0.42 | 0.04 | 0.08 | 2.00 |  |  |
| order_status | main | 5 | 349 (347-362) | 2.89 (2.76-2.90) | 3.2 (3.2-3.3) | 3.6 (3.4-5.7) | 0.49 | 0.05 | 0.09 | 2.00 | +29%, ranges clear |  |
| order_status | main+cache | 5 | 349 (347-355) | 2.88 (2.84-2.89) | 3.2 (3.2-3.2) | 3.5 (3.4-5.6) | 0.48 | 0.05 | 0.09 | 2.00 | +29%, ranges clear | +0%, ranges overlap |

## m8: c=32

| query | build | n | req/s | p50 ms | p99 ms | p99.9 ms | gateway cores | db cores | load cores | MySQL stmts/req | vs v0.42.0 | vs main |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| order_status | v0.42.0 | 5 | 446 (445-449) | 71.41 (71.07-71.55) | 83.4 (83.1-85.9) | 102.5 (99.8-105.3) | 0.98 | 0.08 | 0.10 | 2.01 |  |  |
| order_status | main | 5 | 680 (643-684) | 46.80 (46.67-49.65) | 56.1 (54.0-58.0) | 61.4 (59.5-64.2) | 1.31 | 0.11 | 0.13 | 2.00 | +52%, ranges clear |  |
| order_status | main+cache | 5 | 737 (713-745) | 43.41 (42.90-44.80) | 48.5 (48.2-52.6) | 58.2 (55.5-60.9) | 1.21 | 0.12 | 0.14 | 2.00 | +65%, ranges clear | +8%, ranges clear |

## m8: the store, SQLite (local) against PostgreSQL (0.5 ms away), one caller, order_status

| store | build | n | req/s | p50 ms | p99 ms |
|---|---|---|---|---|---|
| sqlite | main | 5 | 351 (341-354) | 2.86 (2.82-2.94) | 3.2 (3.2-3.3) |
| sqlite | main+cache | 5 | 359 (350-366) | 2.82 (2.74-2.86) | 3.2 (3.1-3.2) |
| postgres | main | 5 | 176 (174-180) | 5.70 (5.55-5.79) | 6.4 (6.3-6.5) |
| postgres | main+cache | 5 | 349 (348-359) | 2.87 (2.79-2.88) | 3.3 (3.3-3.3) |

## m9: round trips per call

| build | statements per call (general log, 100 calls) | of which |
|---|---|---|
| v0.42.0 | 2.00 | 1.00 × `SELECT c_id, c_first, c_middle, c_last, c_balance, c_credit, c_since F`; 1.00 × `rollback` |
| main | 2.00 | 1.00 × `SELECT /*+ MAX_EXECUTION_TIME(N) */ c_id, c_first, c_middle, c_last, c`; 1.00 × `ROLLBACK AND CHAIN` |
| main+cache | 2.00 | 1.00 × `SELECT /*+ MAX_EXECUTION_TIME(N) */ c_id, c_first, c_middle, c_last, c`; 1.00 × `ROLLBACK AND CHAIN` |

Single-call p50 (ms) against netem delay; the slope is round trips per call:

| | +0ms | +0.5ms | +1ms | +2ms | slope | at 0 delay |
|---|---|---|---|---|---|---|
| mysql-connector alone, one | 0.04 (0.04-0.04) | 0.75 (0.73-0.75) | 1.27 (1.27-1.27) | 2.61 (2.60-2.62) | 1.27 | 0.05 ms |
| mysql-connector alone, two | 0.05 (0.05-0.05) | 1.41 (1.37-1.43) | 2.48 (2.47-2.48) | 5.09 (5.09-5.12) | 2.50 | 0.07 ms |
| v0.42.0 | 1.27 (1.27-1.28) | 3.39 (3.32-3.46) | 4.98 (4.96-5.02) | 9.19 (9.18-9.31) | 3.94 | 1.27 ms |
| main | 1.12 (1.11-1.13) | 2.60 (2.50-2.60) | 3.65 (3.65-3.68) | 6.47 (6.43-6.52) | 2.65 | 1.13 ms |
| main+cache | 1.05 (1.05-1.07) | 2.53 (2.49-2.57) | 3.62 (3.60-3.64) | 6.39 (6.38-6.39) | 2.63 | 1.09 ms |

## m10: profile, main (9,799 samples, threads on CPU only)

| category | share |
|---|---|
| store layer: queries, decoding, checks | 23.1% |
| data query: driver and pool | 20.0% |
| logging | 14.9% |
| gunicorn, sockets, HTTP | 13.8% |
| Flask / Werkzeug | 7.0% |
| SQL checks, parameters | 5.2% |
| JSON | 5.1% |
| history writer thread | 5.0% |
| app request handling | 3.2% |
| auth, limits, quotas, metrics | 2.1% |
| other | 0.7% |

| busiest attributed frames | share |
|---|---|
| `cmd_query (mysql/connector/connection_cext.py:772)` | 8.8% |
| `load (queryapigate/store.py:538)` | 7.6% |
| `<lambda> (queryapigate/apikeys.py:937)` | 7.4% |
| `<lambda> (queryapigate/store.py:336)` | 6.2% |
| `acquire (logging/__init__.py:973)` | 5.7% |
| `write (gunicorn/util.py:348)` | 4.4% |
| `emit (logging/__init__.py:1163)` | 3.4% |
| `_open_connection (mysql/connector/connection_cext.py:354)` | 3.0% |
| `flush (logging/__init__.py:1144)` | 2.4% |
| `notify (gunicorn/workers/workertmp.py:44)` | 2.3% |
| `get_rows (mysql/connector/connection_cext.py:540)` | 1.9% |
| `chunk (gunicorn/http/unreader.py:77)` | 1.9% |

## m10: profile, main+cache (8,707 samples, threads on CPU only)

| category | share |
|---|---|
| data query: driver and pool | 22.7% |
| logging | 19.2% |
| gunicorn, sockets, HTTP | 16.3% |
| Flask / Werkzeug | 9.9% |
| SQL checks, parameters | 7.9% |
| JSON | 7.3% |
| history writer thread | 5.1% |
| app request handling | 4.7% |
| store layer: queries, decoding, checks | 3.6% |
| auth, limits, quotas, metrics | 2.7% |
| other | 0.5% |

| busiest attributed frames | share |
|---|---|
| `cmd_query (mysql/connector/connection_cext.py:772)` | 10.6% |
| `acquire (logging/__init__.py:973)` | 9.4% |
| `write (gunicorn/util.py:348)` | 5.4% |
| `emit (logging/__init__.py:1163)` | 3.1% |
| `flush (logging/__init__.py:1144)` | 3.0% |
| `get_rows (mysql/connector/connection_cext.py:540)` | 3.0% |
| `notify (gunicorn/workers/workertmp.py:44)` | 2.4% |
| `_open_connection (mysql/connector/connection_cext.py:354)` | 2.2% |
| `chunk (gunicorn/http/unreader.py:77)` | 2.1% |
| `iterencode (json/encoder.py:258)` | 2.0% |
| `json_default (queryapigate/formats.py:21)` | 1.9% |
| `paginate (queryapigate/sqltools.py:234)` | 1.6% |

## m11: the table allow-list across databases (a key allowed `customer`)

| SQL | status | code |
|---|---|---|
| `SELECT COUNT(*) AS n FROM crm.customer` | 200 |  |
| `SELECT COUNT(*) AS n FROM billing.customer` | 403 | table_not_allowed |
| `SELECT COUNT(*) AS n FROM billing.history` | 403 | table_not_allowed |

## n1: threads per worker, MySQL 0.5 ms away, order_status, c=32

| build | threads | n | req/s | p50 ms | p99 ms | gateway cores | db cores | vs the base, same threads |
|---|---|---|---|---|---|---|---|---|
| v0.42.0 | 1 | 5 | 316 (311-317) | 100.76 (100.63-101.11) | 110.8 (108.8-116.6) | 0.47 | 0.04 |  |
| v0.42.0 | 2 | 5 | 501 (499-507) | 63.43 (62.97-63.93) | 72.0 (70.8-76.2) | 0.80 | 0.08 |  |
| v0.42.0 | 4 | 5 | 469 (468-473) | 68.06 (67.75-68.27) | 76.1 (74.1-81.7) | 0.91 | 0.08 |  |
| v0.42.0 | 8 | 5 | 447 (445-449) | 71.35 (70.97-71.46) | 82.7 (82.0-88.0) | 0.98 | 0.08 |  |
| main | 1 | 5 | 405 (403-408) | 78.62 (78.03-79.07) | 89.0 (84.5-90.2) | 0.53 | 0.05 | +28%, ranges clear |
| main | 2 | 5 | 648 (640-665) | 47.77 (45.60-52.05) | 62.4 (61.4-63.7) | 0.91 | 0.09 | +29%, ranges clear |
| main | 4 | 5 | 730 (729-730) | 43.73 (43.65-43.77) | 53.8 (52.8-54.6) | 1.18 | 0.11 | +55%, ranges clear |
| main | 8 | 5 | 685 (680-686) | 46.56 (46.49-46.90) | 54.0 (53.5-57.0) | 1.31 | 0.11 | +53%, ranges clear |

## n2: scale-out on a PostgreSQL store (local), order_status, c=64

| workers | build | n | req/s | p50 ms | p99 ms | gateway cores | req/s per gateway core | MySQL cores | store cores | vs main |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | main | 5 | 625 (620-667) | 102.16 (95.47-102.70) | 115.9 (108.3-117.8) | 1.20 | 522 | 0.14 | 0.24 |  |
| 1 | main+cache | 5 | 720 (716-760) | 88.23 (83.73-88.56) | 100.7 (95.6-103.0) | 1.16 | 619 | 0.17 | 0.07 | +15%, ranges clear |
| 2 | main | 5 | 1,008 (1,001-1,008) | 63.55 (63.46-64.03) | 78.9 (77.0-86.8) | 2.37 | 425 | 0.25 | 0.41 |  |
| 2 | main+cache | 5 | 1,198 (1,195-1,201) | 53.10 (53.01-54.33) | 66.1 (64.5-70.3) | 2.30 | 520 | 0.26 | 0.13 | +19%, ranges clear |
| 4 | main | 5 | 1,426 (1,420-1,431) | 43.78 (36.02-49.27) | 81.7 (62.0-93.5) | 4.67 | 305 | 0.44 | 0.74 |  |
| 4 | main+cache | 5 | 1,745 (1,735-1,753) | 27.71 (18.64-42.36) | 83.1 (65.6-111.2) | 4.55 | 383 | 0.41 | 0.24 | +22%, ranges clear |
| 8 | main | 5 | 1,862 (1,831-1,865) | 17.41 (9.22-20.09) | 173.3 (149.3-193.2) | 7.32 | 253 | 0.66 | 1.15 |  |
| 8 | main+cache | 5 | 2,270 (2,233-2,321) | 15.07 (10.24-20.18) | 100.0 (87.6-162.1) | 7.37 | 310 | 0.63 | 0.37 | +22%, ranges clear |

## n3: 4 workers on four cores, on two (both hyperthreads), and on four with both, order_status, c=64

| CPUs | n | req/s | p50 ms | p99 ms | gateway cores |
|---|---|---|---|---|---|
| 4 cores, 1 thread each (`0,2,4,6`) | 5 | 2,250 (2,243-2,256) | 25.92 (17.30-29.29) | 59.0 (49.3-75.7) | 3.94 |
| 2 cores, 2 threads each (`0-3`) | 5 | 1,807 (1,777-1,828) | 26.75 (19.29-41.71) | 72.3 (57.9-111.1) | 3.93 |
| 4 cores, 2 threads each (`0-7`) | 5 | 1,756 (1,749-1,767) | 34.94 (29.98-36.88) | 55.2 (48.6-76.0) | 4.57 |

## n4: what logging costs, main + cache, order_status, c=32

| logging | n | req/s | p50 ms | p99 ms | gateway cores | vs the image |
|---|---|---|---|---|---|---|
| access log + request log (the image) | 5 | 722 (717-754) | 44.22 (42.38-44.57) | 50.0 (47.5-50.3) | 1.19 | +0%, ranges overlap |
| request log only | 5 | 765 (763-766) | 41.71 (41.66-41.81) | 47.6 (47.4-48.0) | 1.19 | +6%, ranges clear |
| neither | 5 | 874 (873-903) | 35.97 (34.80-36.10) | 48.0 (47.2-48.7) | 1.22 | +21%, ranges clear |

## n5: every call at 150 a second for 60 s, latency from when each was due

| history flush | round | p50 ms | p99 ms | p99.9 ms | max ms | calls over 10 ms | in seconds | first slow calls (s into the run) |
|---|---|---|---|---|---|---|---|---|
| 1 s (default) | 0 | 2.58 | 3.65 | 26.6 | 32.7 | 29 | 9 | 0.2, 18.8, 23.4, 23.4, 23.5, 23.5, 28.4, 28.4, 28.5, 28.5, 35.5, 35.5 |
| 5 s | 0 | 2.65 | 3.36 | 26.5 | 34.6 | 25 | 7 | 4.4, 4.4, 4.4, 4.4, 9.4, 9.4, 9.4, 9.5, 17.7, 36.2, 36.2, 36.2 |
| 5 s | 1 | 2.59 | 3.36 | 22.0 | 31.8 | 16 | 5 | 18.3, 40.9, 40.9, 40.9, 41.0, 49.3, 49.3, 49.3, 49.3, 54.3, 54.3, 54.3 |
| 1 s (default) | 1 | 2.61 | 3.37 | 26.6 | 35.3 | 27 | 8 | 0.2, 10.4, 10.4, 10.4, 10.4, 16.4, 21.4, 21.4, 21.4, 21.4, 30.9, 30.9 |
| 1 s (default) | 2 | 2.65 | 3.36 | 15.9 | 36.4 | 13 | 4 | 0.2, 0.2, 16.4, 16.4, 16.4, 16.4, 16.4, 17.5, 31.0, 31.0, 31.0, 31.0 |
| 5 s | 2 | 2.62 | 3.37 | 21.3 | 33.2 | 16 | 5 | 17.6, 22.7, 22.7, 22.7, 22.7, 27.7, 27.7, 27.7, 27.7, 30.7, 30.7, 30.7 |

## n6: one caller, order_status, against delay on the PostgreSQL store only (main, no cache)

| store delay | n | req/s | p50 ms | p99 ms |
|---|---|---|---|---|
| 0ms | 3 | 539 (531-568) | 1.76 (1.70-1.79) | 2.7 (2.5-2.7) |
| 0.5ms | 3 | 237 (234-241) | 4.21 (4.16-4.31) | 5.0 (5.0-5.2) |
| 1ms | 3 | 166 (164-170) | 6.00 (5.81-6.08) | 7.0 (6.8-7.0) |
| 2ms | 3 | 100 (99-100) | 10.08 (10.06-10.18) | 11.1 (11.1-11.1) |

Slope: 4.10 ms of p50 per ms of delay.

## n9: the laptop's ceiling against v1's setting, main + cache, c=64

| setup | query | n | req/s | p50 ms | p99 ms | gateway cores | MySQL cores | load cores | gateway MHz | vs v1 setting |
|---|---|---|---|---|---|---|---|---|---|---|
| v1 setting | customer | 5 | 2,927 (2,876-4,029) | 9.97 (5.49-15.00) | 84.9 (63.0-123.5) | 7.37 | 0.34 | 0.48 | 2645 |  |
| v1 setting | order_status | 5 | 2,376 (2,318-3,060) | 14.05 (9.81-19.62) | 118.8 (54.5-152.2) | 7.22 | 0.55 | 0.44 | 2629 |  |
| v1 setting | stock_level | 5 | 2,532 (2,507-3,521) | 12.85 (8.89-16.87) | 111.3 (95.2-127.6) | 7.23 | 2.76 | 0.52 | 2407 |  |
| ceiling | customer | 5 | 4,382 (4,372-6,565) | 8.66 (4.76-11.17) | 47.8 (28.9-72.6) | 11.06 | 1.06 | 0.30 | 2266 | +50%, ranges clear |
| ceiling | order_status | 5 | 3,507 (3,490-3,935) | 12.35 (8.27-16.69) | 70.1 (61.1-80.5) | 11.04 | 1.61 | 0.30 | 2200 | +48%, ranges clear |
| ceiling | stock_level | 5 | 3,514 (3,382-3,522) | 14.14 (7.28-17.35) | 61.6 (42.8-129.1) | 9.57 | 5.37 | 0.36 | 2126 | +39%, ranges overlap |

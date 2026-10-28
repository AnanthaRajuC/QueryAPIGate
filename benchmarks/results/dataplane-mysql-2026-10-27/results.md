## Environment

```
{
 "block": "env",
 "scenario": "env",
 "profile": "performance",
 "ac": "1",
 "epp": "performance",
 "pkg_temp_c": 73.0,
 "load1": 9.21923828125,
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
  "billing.history=601473",
  "catalog.item=99731",
  "crm.customer=578811",
  "inventory.stock=1957651",
  "orders.new_order=190163",
  "orders.order_line=5946507",
  "orders.orders=595787",
  "org.district=200",
  "org.warehouse=20"
 ],
 "ts": "2026-10-27T22:12:57"
}
```
```
{
 "block": "env",
 "scenario": "env",
 "profile": "performance",
 "ac": "1",
 "epp": "performance",
 "pkg_temp_c": 68.0,
 "load1": 12.6806640625,
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
  "billing.history=601473",
  "catalog.item=99731",
  "crm.customer=578811",
  "inventory.stock=1957651",
  "orders.new_order=190163",
  "orders.order_line=5946507",
  "orders.orders=595787",
  "org.district=200",
  "org.warehouse=20"
 ],
 "ts": "2026-10-28T00:25:10"
}
```

## /health floors

| block | when | req/s | p50 ms | power profile | AC | EPP | package C | MHz |
|---|---|---|---|---|---|---|---|---|
| d1 | start | 2,185 | 0.45 | performance | 1 | performance | 69.0 | 2075 |
| d1 | end | 2,253 | 0.44 | performance | 1 | performance | 80.0 | 3324 |
| d2 | start | 2,237 | 0.44 | performance | 1 | performance | 77.0 | 2557 |
| d2 | end | 2,177 | 0.45 | performance | 1 | performance | 81.0 | 4689 |
| d3 | start | 2,011 | 0.49 | performance | 1 | performance | 74.0 | 3573 |
| d3 | end | 2,066 | 0.48 | performance | 1 | performance | 72.0 | 3226 |
| d4 | start | 2,000 | 0.49 | performance | 1 | performance | 71.0 | 4078 |
| d4 | end | 2,197 | 0.45 | performance | 1 | performance | 77.0 | 4126 |

## m1: MySQL on its own (sysbench)

| test | threads | tps | p50 ms | p99 ms | n |
|---|---|---|---|---|---|

## m2: c=32

| query | build | n | req/s | p50 ms | p99 ms | p99.9 ms | gateway cores | db cores | load cores | MySQL stmts/req | vs v0.43.0 | vs rust-data-plane | direct tps | gateway, % of direct |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|

## m3: c=1

| query | build | n | req/s | p50 ms | p99 ms | p99.9 ms | gateway cores | db cores | load cores | MySQL stmts/req | vs v0.43.0 | vs rust-data-plane | direct p50 ms | gateway adds (p50) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|

## m4: capacity (closed loop, c=32), then open loop on order_status, latency corrected

| build | offered (x capacity) | offered req/s | achieved req/s | p50 ms | p99 ms | p99.9 ms | ok | n |
|---|---|---|---|---|---|---|---|---|

## m5: scale-out, c=64

| query | workers | build | n | req/s | p50 ms | p99 ms | gateway cores | req/s per gateway core | db cores | load cores | vs rust-data-plane |
|---|---|---|---|---|---|---|---|---|---|---|---|

## m6: threads per worker, c=32

| query | threads | n | req/s | p50 ms | p99 ms | gateway cores |
|---|---|---|---|---|---|---|

## m7: uniform against Zipf-distributed customers, with and without a 30 s response cache

| customers | query | n | req/s | p50 ms | p99 ms | cache hit rate | MySQL stmts/req |
|---|---|---|---|---|---|---|---|

## m8: c=1

| query | build | n | req/s | p50 ms | p99 ms | p99.9 ms | gateway cores | db cores | load cores | MySQL stmts/req | vs v0.43.0 | vs rust-data-plane |
|---|---|---|---|---|---|---|---|---|---|---|---|---|

## m8: c=32

| query | build | n | req/s | p50 ms | p99 ms | p99.9 ms | gateway cores | db cores | load cores | MySQL stmts/req | vs v0.43.0 | vs rust-data-plane |
|---|---|---|---|---|---|---|---|---|---|---|---|---|

## m8: the store, SQLite (local) against PostgreSQL (0.5 ms away), one caller, order_status

| store | build | n | req/s | p50 ms | p99 ms |
|---|---|---|---|---|---|

## m9: round trips per call

| build | statements per call (general log, 100 calls) | of which |
|---|---|---|

## m11: the table allow-list across databases (a key allowed `customer`)

| SQL | status | code |
|---|---|---|

## d1: the data plane

| scenario | c | build | Python workers | n | req/s | p50 ms | p99 ms | gateway cores | db cores | req/s per gateway core | runs kept, lowest | per core vs Python, same cache setting |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| customer reader | 32 | rust-data-plane | 1 | 5 | 794 (791-812) | 40.16 (39.29-40.33) | 47.0 (46.1-47.5) | 1.29 | 0.09 | 616 (611-630) | 100% |  |
| customer reader | 32 | rust-data-plane+cache | 1 | 5 | 898 (892-902) | 35.55 (35.44-35.80) | 39.7 (39.3-41.1) | 1.17 | 0.12 | 769 (764-771) | 100% |  |
| customer reader | 32 | v0.43.0 | 1 | 5 | 786 (772-807) | 40.54 (39.38-41.29) | 48.4 (47.4-50.2) | 1.31 | 0.09 | 599 (589-616) | 100% |  |
| customer reader | 32 | rust-data-plane+dp | 2 | 5 | 15,871 (15,856-18,803) | 2.01 (1.58-2.01) | 3.2 (3.1-3.2) | 7.40 | 1.66 | 2,144 (2,142-2,549) | 100% | 3.5x |
| customer reader | 32 | rust-data-plane+dp+cache | 2 | 5 | 23,961 (23,868-28,568) | 1.33 (1.02-1.34) | 2.1 (2.0-2.1) | 7.35 | 2.76 | 3,253 (3,245-3,888) | 100% | 4.2x |
| order_status governed | 32 | rust-data-plane | 1 | 5 | 636 (632-647) | 50.11 (49.29-50.46) | 58.9 (58.2-60.1) | 1.28 | 0.14 | 498 (496-507) | 100% |  |
| order_status governed | 32 | rust-data-plane+cache | 1 | 5 | 700 (690-703) | 45.67 (45.43-46.15) | 51.2 (50.6-54.8) | 1.17 | 0.16 | 597 (589-600) | 100% |  |
| order_status governed | 32 | v0.43.0 | 1 | 5 | 632 (629-632) | 50.56 (50.51-50.72) | 59.2 (59.1-59.8) | 1.29 | 0.14 | 488 (486-489) | 100% |  |
| order_status governed | 32 | rust-data-plane+dp | 2 | 5 | 11,888 (11,842-14,602) | 2.70 (1.99-2.71) | 4.2 (3.9-4.2) | 7.38 | 3.16 | 1,612 (1,603-1,985) | 100% | 3.2x |
| order_status governed | 32 | rust-data-plane+dp+cache | 2 | 5 | 14,027 (13,998-14,068) | 2.29 (2.29-2.29) | 3.5 (3.4-3.5) | 5.86 | 3.73 | 2,395 (2,369-2,402) | 100% | 4.0x |
| order_status reader | 32 | rust-data-plane | 1 | 5 | 674 (670-695) | 47.36 (45.87-47.53) | 55.7 (54.4-56.5) | 1.29 | 0.14 | 522 (520-540) | 100% |  |
| order_status reader | 32 | rust-data-plane+cache | 1 | 5 | 746 (744-760) | 42.76 (42.03-42.94) | 48.2 (48.1-48.5) | 1.18 | 0.17 | 633 (632-646) | 100% |  |
| order_status reader | 32 | v0.43.0 | 1 | 5 | 671 (669-672) | 47.57 (47.49-47.69) | 55.9 (55.5-56.0) | 1.31 | 0.14 | 513 (511-514) | 100% |  |
| order_status reader | 32 | rust-data-plane+dp | 2 | 5 | 12,212 (12,071-15,127) | 2.64 (1.90-2.66) | 4.1 (3.8-4.1) | 7.35 | 3.23 | 1,661 (1,643-2,062) | 100% | 3.2x |
| order_status reader | 32 | rust-data-plane+dp+cache | 2 | 5 | 14,251 (14,211-14,273) | 2.26 (2.26-2.27) | 3.4 (3.3-3.4) | 5.66 | 3.76 | 2,515 (2,511-2,538) | 100% | 4.0x |
| stock_level reader | 32 | rust-data-plane | 1 | 5 | 770 (765-814) | 41.58 (39.10-41.87) | 49.2 (48.5-49.9) | 1.29 | 0.47 | 597 (592-634) | 100% |  |
| stock_level reader | 32 | rust-data-plane+cache | 1 | 5 | 862 (857-898) | 37.07 (35.43-37.33) | 42.4 (40.1-44.7) | 1.17 | 0.54 | 736 (731-768) | 100% |  |
| stock_level reader | 32 | v0.43.0 | 1 | 5 | 760 (758-813) | 42.12 (39.22-42.21) | 49.8 (46.3-49.9) | 1.31 | 0.47 | 581 (579-623) | 100% |  |
| stock_level reader | 32 | rust-data-plane+dp | 2 | 5 | 4,340 (4,321-5,059) | 7.44 (6.07-7.47) | 10.0 (9.1-10.0) | 1.65 | 3.93 | 2,637 (2,616-3,140) | 100% | 4.4x |
| stock_level reader | 32 | rust-data-plane+dp+cache | 2 | 5 | 4,588 (4,580-4,595) | 7.01 (7.00-7.02) | 9.4 (9.4-9.5) | 0.93 | 3.93 | 4,913 (4,896-4,926) | 100% | 6.7x |

## d2: the data plane

| scenario | c | build | Python workers | n | req/s | p50 ms | p99 ms | gateway cores | db cores | req/s per gateway core | runs kept, lowest | per core vs Python, same cache setting |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| customer reader | 1 | rust-data-plane | 1 | 5 | 865 (848-879) | 1.13 (1.11-1.15) | 1.5 (1.5-1.5) | 0.94 | 0.05 | 920 (912-927) | 100% |  |
| customer reader | 1 | rust-data-plane+cache | 1 | 5 | 908 (885-937) | 1.08 (1.04-1.10) | 1.4 (1.4-1.5) | 0.93 | 0.06 | 973 (954-979) | 100% |  |
| customer reader | 1 | rust-data-plane+dp | 2 | 5 | 5,519 (5,066-5,647) | 0.17 (0.17-0.19) | 0.3 (0.3-0.3) | 1.23 | 0.29 | 4,395 (4,150-4,552) | 100% | 4.8x |
| customer reader | 1 | rust-data-plane+dp+cache | 2 | 5 | 7,722 (7,649-8,375) | 0.12 (0.11-0.12) | 0.2 (0.2-0.2) | 0.97 | 0.42 | 7,990 (7,870-8,566) | 100% | 8.2x |
| order_status governed | 1 | rust-data-plane | 1 | 5 | 640 (630-649) | 1.53 (1.49-1.55) | 2.1 (2.0-2.1) | 0.86 | 0.09 | 744 (732-750) | 100% |  |
| order_status governed | 1 | rust-data-plane+cache | 1 | 5 | 670 (659-689) | 1.44 (1.40-1.47) | 2.0 (2.0-2.0) | 0.87 | 0.10 | 770 (762-782) | 100% |  |
| order_status governed | 1 | rust-data-plane+dp | 2 | 5 | 3,301 (3,115-3,425) | 0.28 (0.27-0.30) | 0.5 (0.4-0.5) | 0.95 | 0.38 | 3,479 (3,291-3,571) | 100% | 4.7x |
| order_status governed | 1 | rust-data-plane+dp+cache | 2 | 5 | 4,486 (4,176-4,531) | 0.21 (0.21-0.23) | 0.4 (0.4-0.4) | 0.73 | 0.51 | 6,164 (5,773-6,305) | 100% | 8.0x |
| order_status reader | 1 | rust-data-plane | 1 | 5 | 690 (663-713) | 1.39 (1.36-1.47) | 1.9 (1.9-2.0) | 0.88 | 0.09 | 790 (785-807) | 100% |  |
| order_status reader | 1 | rust-data-plane+cache | 1 | 5 | 704 (694-721) | 1.37 (1.35-1.39) | 1.9 (1.9-2.0) | 0.87 | 0.09 | 810 (804-821) | 100% |  |
| order_status reader | 1 | rust-data-plane+dp | 2 | 5 | 3,442 (3,201-3,517) | 0.28 (0.27-0.29) | 0.5 (0.4-0.5) | 0.95 | 0.38 | 3,603 (3,419-3,717) | 100% | 4.6x |
| order_status reader | 1 | rust-data-plane+dp+cache | 2 | 5 | 4,451 (4,388-4,674) | 0.22 (0.20-0.22) | 0.4 (0.4-0.4) | 0.71 | 0.52 | 6,231 (6,190-6,607) | 100% | 7.7x |

## d3: the data plane

| scenario | c | build | Python workers | n | req/s | p50 ms | p99 ms | gateway cores | db cores | req/s per gateway core | runs kept, lowest | per core vs Python, same cache setting |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| order_status reader | 1 | rust-data-plane | 1 | 5 | 342 (338-361) | 2.93 (2.76-2.95) | 3.2 (3.2-3.3) | 0.48 | 0.05 | 711 (706-726) | 100% |  |
| order_status reader | 1 | rust-data-plane+cache | 1 | 5 | 349 (347-366) | 2.87 (2.72-2.89) | 3.2 (3.2-3.2) | 0.48 | 0.05 | 731 (729-758) | 100% |  |
| order_status reader | 1 | rust-data-plane+dp | 2 | 5 | 1,004 (980-1,027) | 0.98 (0.96-1.00) | 1.2 (1.2-1.3) | 0.40 | 0.14 | 2,462 (2,454-2,518) | 100% | 3.5x |
| order_status reader | 1 | rust-data-plane+dp+cache | 2 | 5 | 1,158 (1,154-1,191) | 0.85 (0.83-0.85) | 1.1 (1.1-1.1) | 0.26 | 0.16 | 4,497 (4,440-4,607) | 100% | 6.1x |
| order_status reader | 32 | rust-data-plane | 1 | 5 | 678 (673-688) | 47.00 (46.43-47.39) | 55.1 (53.3-56.4) | 1.29 | 0.11 | 525 (523-533) | 100% |  |
| order_status reader | 32 | rust-data-plane+cache | 1 | 5 | 740 (738-742) | 43.20 (43.10-43.30) | 48.5 (48.2-48.7) | 1.19 | 0.12 | 621 (620-623) | 100% |  |
| order_status reader | 32 | rust-data-plane+dp | 2 | 5 | 8,900 (8,073-8,932) | 3.56 (3.54-3.95) | 4.1 (4.1-4.9) | 3.00 | 1.20 | 2,965 (1,967-3,042) | 100% | 5.6x |
| order_status reader | 32 | rust-data-plane+dp+cache | 2 | 5 | 8,353 (8,331-8,960) | 3.80 (3.54-3.81) | 4.5 (4.0-4.5) | 2.22 | 1.49 | 3,755 (3,732-5,263) | 100% | 6.0x |

## d4: the data plane

| scenario | c | build | Python workers | n | req/s | p50 ms | p99 ms | gateway cores | db cores | req/s per gateway core | runs kept, lowest | per core vs Python, same cache setting |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| customer reader | 64 | rust-data-plane+cache | 8 | 3 | 2,976 (2,950-2,987) | 12.32 (8.10-13.39) | 93.3 (90.8-128.2) | 7.34 | 0.34 | 405 (402-408) | 100% |  |
| customer reader | 64 | rust-data-plane+dp+cache | 2 | 3 | 23,619 (23,566-23,694) | 2.74 (2.74-2.75) | 3.8 (3.8-3.8) | 7.34 | 2.76 | 3,214 (3,213-3,230) | 100% | 7.9x |
| order_status governed | 64 | rust-data-plane+cache | 8 | 3 | 2,252 (2,222-2,266) | 15.23 (12.54-19.61) | 115.5 (97.2-167.5) | 7.35 | 0.53 | 306 (302-310) | 100% |  |
| order_status governed | 64 | rust-data-plane+dp+cache | 2 | 3 | 13,863 (13,842-13,873) | 4.67 (4.66-4.67) | 6.4 (6.4-6.4) | 5.82 | 3.70 | 2,384 (2,378-2,386) | 100% | 7.8x |

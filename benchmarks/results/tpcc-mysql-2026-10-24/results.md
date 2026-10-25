## Environment

```
{
 "block": "env",
 "scenario": "env",
 "profile": "performance",
 "ac": "1",
 "epp": "performance",
 "pkg_temp_c": 77.0,
 "load1": 1.1923828125,
 "builds": [
  "v0.41.0",
  "v0.42.0",
  "main",
  "main+cache"
 ],
 "refs": {
  "v0.41.0": "f61d4ed3e7170a990435c40a2b3b75722cd43d15",
  "v0.42.0": "647ab6f739adc930d11ab5aa1db259dfa27daf1a",
  "main": "3a92e5d200fb7941d4885a59d7a36c070b325014",
  "main+cache": "3a92e5d200fb7941d4885a59d7a36c070b325014"
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
  "billing.history=599530",
  "catalog.item=95062",
  "crm.customer=592343",
  "inventory.stock=1960046",
  "orders.new_order=181802",
  "orders.order_line=6013540",
  "orders.orders=617613",
  "org.district=200",
  "org.warehouse=20"
 ],
 "ts": "2026-10-24T19:33:19"
}
```
```
{
 "block": "env",
 "scenario": "env",
 "profile": "performance",
 "ac": "1",
 "epp": "performance",
 "pkg_temp_c": 85.0,
 "load1": 1.015625,
 "builds": [
  "v0.41.0",
  "v0.42.0",
  "main",
  "main+cache"
 ],
 "refs": {
  "v0.41.0": "f61d4ed3e7170a990435c40a2b3b75722cd43d15",
  "v0.42.0": "647ab6f739adc930d11ab5aa1db259dfa27daf1a",
  "main": "3a92e5d200fb7941d4885a59d7a36c070b325014",
  "main+cache": "3a92e5d200fb7941d4885a59d7a36c070b325014"
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
  "billing.history=599530",
  "catalog.item=95062",
  "crm.customer=592343",
  "inventory.stock=1960046",
  "orders.new_order=181802",
  "orders.order_line=6013540",
  "orders.orders=617613",
  "org.district=200",
  "org.warehouse=20"
 ],
 "ts": "2026-10-24T21:46:57"
}
```
```
{
 "block": "env",
 "scenario": "env",
 "profile": "performance",
 "ac": "1",
 "epp": "performance",
 "pkg_temp_c": 73.0,
 "load1": 1.8037109375,
 "builds": [
  "v0.41.0",
  "v0.42.0",
  "main",
  "main+cache"
 ],
 "refs": {
  "v0.41.0": "f61d4ed3e7170a990435c40a2b3b75722cd43d15",
  "v0.42.0": "647ab6f739adc930d11ab5aa1db259dfa27daf1a",
  "main": "3a92e5d200fb7941d4885a59d7a36c070b325014",
  "main+cache": "3a92e5d200fb7941d4885a59d7a36c070b325014"
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
  "billing.history=599530",
  "catalog.item=95062",
  "crm.customer=592343",
  "inventory.stock=1960046",
  "orders.new_order=181802",
  "orders.order_line=6013540",
  "orders.orders=617613",
  "org.district=200",
  "org.warehouse=20"
 ],
 "ts": "2026-10-25T00:44:12"
}
```

## /health floors

| block | when | req/s | p50 ms | power profile | AC | EPP | package C | MHz |
|---|---|---|---|---|---|---|---|---|
| m1 | start | 2,298 | 0.43 | performance | 1 | performance | 88.0 | 1236 |
| m1 | end | 2,074 | 0.48 | performance | 1 | performance | 77.0 | 4400 |
| m2 | start | 2,092 | 0.47 | performance | 1 | performance | 77.0 | 4285 |
| m2 | end | 2,219 | 0.45 | performance | 1 | performance | 77.0 | 2830 |
| m3 | start | 2,208 | 0.45 | performance | 1 | performance | 73.0 | 2462 |
| m3 | end | 2,258 | 0.44 | performance | 1 | performance | 84.0 | 4100 |
| m4 | start | 2,224 | 0.44 | performance | 1 | performance | 70.0 | 3632 |
| m4 | end | 2,234 | 0.44 | performance | 1 | performance | 89.0 | 4151 |
| m5 | start | 2,135 | 0.46 | performance | 1 | performance | 83.0 | 2930 |
| m5 | end | 2,011 | 0.49 | performance | 1 | performance | 81.0 | 3561 |
| m6 | start | 2,023 | 0.48 | performance | 1 | performance | 85.0 | 4185 |
| m6 | end | 1,952 | 0.50 | performance | 1 | performance | 72.0 | 3558 |
| m7 | start | 1,981 | 0.50 | performance | 1 | performance | 86.0 | 3858 |
| m7 | end | 1,919 | 0.51 | performance | 1 | performance | 77.0 | 1730 |
| m8 | start | 1,922 | 0.51 | performance | 1 | performance | 78.0 | 3710 |
| m8 | end | 2,269 | 0.43 | performance | 1 | performance | 89.0 | 4434 |
| m9 | start | 2,270 | 0.43 | performance | 1 | performance | 74.0 | 4136 |
| m9 | end | 2,279 | 0.43 | performance | 1 | performance | 80.0 | 3756 |

## m1: MySQL on its own (sysbench)

| test | threads | tps | p50 ms | p99 ms | n |
|---|---|---|---|---|---|
| sysbench_oltp_read_only | 1 | 1,695 (1,683-1,710) | 0.58 (0.58-0.59) | 0.83 (0.74-0.89) | 5 |
| sysbench_oltp_read_only | 32 | 4,425 (4,415-5,544) | 7.04 (5.77-7.30) | 8.74 (7.43-9.06) | 5 |
| direct_customer | 1 | 28,303 (28,197-28,677) | 0.03 (0.03-0.03) | 0.05 (0.04-0.05) | 5 |
| direct_customer | 32 | 75,420 (74,936-75,697) | 0.42 (0.42-0.43) | 0.54 (0.52-0.55) | 5 |
| direct_order_status | 1 | 12,053 (11,967-12,220) | 0.08 (0.08-0.08) | 0.13 (0.11-0.13) | 5 |
| direct_order_status | 32 | 25,063 (24,917-25,142) | 1.27 (1.25-1.27) | 1.64 (1.52-1.79) | 5 |
| direct_order_history | 1 | 9,618 (9,536-9,724) | 0.10 (0.10-0.10) | 0.14 (0.13-0.18) | 5 |
| direct_order_history | 32 | 19,116 (19,033-19,203) | 1.67 (1.64-1.67) | 2.26 (2.11-2.52) | 5 |
| direct_payments | 1 | 18,479 (18,429-18,610) | 0.05 (0.05-0.05) | 0.07 (0.07-0.07) | 5 |
| direct_payments | 32 | 38,136 (37,931-38,148) | 0.84 (0.83-0.84) | 1.06 (0.99-1.12) | 5 |
| direct_profile | 1 | 21,793 (21,576-21,888) | 0.04 (0.04-0.04) | 0.06 (0.06-0.07) | 5 |
| direct_profile | 32 | 50,026 (48,874-50,306) | 0.64 (0.63-0.65) | 0.81 (0.77-0.90) | 5 |
| direct_stock_level | 1 | 2,373 (2,341-2,395) | 0.42 (0.42-0.42) | 0.56 (0.55-0.64) | 5 |
| direct_stock_level | 32 | 5,343 (5,242-5,355) | 5.99 (5.77-6.09) | 8.74 (8.28-9.56) | 5 |

## m2: c=32

| query | build | n | req/s | p50 ms | p99 ms | p99.9 ms | gateway cores | db cores | load cores | MySQL stmts/req | vs v0.41.0 | vs main | direct tps | gateway, % of direct |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| customer | v0.41.0 | 5 | 609 (609-615) | 52.13 (51.64-52.15) | 63.6 (63.2-64.8) | 69.7 (68.6-70.5) | 1.30 | 0.08 | 0.18 | 2.00 |  |  | 75,420 | 1% |
| customer | v0.42.0 | 5 | 643 (641-649) | 49.44 (49.08-49.62) | 60.0 (58.8-61.3) | 67.3 (65.3-70.9) | 1.25 | 0.08 | 0.19 | 2.00 | +6%, ranges clear |  | 75,420 | 1% |
| customer | main | 5 | 676 (672-679) | 46.98 (46.84-47.23) | 57.9 (57.4-58.3) | 62.7 (62.2-70.0) | 1.25 | 0.08 | 0.20 | 2.00 | +11%, ranges clear |  | 75,420 | 1% |
| customer | main+cache | 5 | 777 (772-778) | 40.90 (40.87-41.16) | 49.3 (47.6-50.2) | 55.8 (53.5-58.4) | 1.12 | 0.09 | 0.21 | 2.00 | +27%, ranges clear | +15%, ranges clear | 75,420 | 1% |
| order_status | v0.41.0 | 5 | 532 (529-537) | 59.79 (59.29-60.07) | 73.1 (72.2-74.7) | 83.9 (78.2-86.0) | 1.29 | 0.12 | 0.18 | 2.00 |  |  | 25,063 | 2% |
| order_status | v0.42.0 | 5 | 558 (555-560) | 57.02 (56.90-57.32) | 68.9 (68.3-69.3) | 75.4 (73.2-76.5) | 1.25 | 0.12 | 0.18 | 2.00 | +5%, ranges clear |  | 25,063 | 2% |
| order_status | main | 5 | 579 (577-582) | 54.88 (54.71-55.09) | 67.3 (65.7-67.6) | 73.8 (72.1-76.4) | 1.24 | 0.12 | 0.19 | 2.00 | +9%, ranges clear |  | 25,063 | 2% |
| order_status | main+cache | 5 | 651 (642-652) | 49.06 (48.80-49.58) | 57.4 (56.4-58.6) | 64.2 (62.8-64.9) | 1.13 | 0.14 | 0.22 | 2.00 | +22%, ranges clear | +12%, ranges clear | 25,063 | 3% |
| order_history | v0.41.0 | 5 | 552 (550-553) | 57.54 (57.46-57.81) | 70.3 (69.7-71.1) | 81.5 (75.1-90.2) | 1.30 | 0.15 | 0.18 | 2.00 |  |  | 19,116 | 3% |
| order_history | v0.42.0 | 5 | 580 (579-583) | 54.86 (54.61-55.01) | 66.2 (65.5-66.9) | 72.3 (70.2-75.9) | 1.26 | 0.15 | 0.19 | 2.00 | +5%, ranges clear |  | 19,116 | 3% |
| order_history | main | 5 | 604 (601-607) | 52.64 (52.44-52.92) | 64.2 (63.2-64.8) | 70.1 (69.4-73.8) | 1.25 | 0.16 | 0.20 | 2.00 | +9%, ranges clear |  | 19,116 | 3% |
| order_history | main+cache | 5 | 677 (675-678) | 47.08 (47.05-47.19) | 55.3 (54.7-57.3) | 61.5 (60.4-62.4) | 1.13 | 0.18 | 0.21 | 2.00 | +22%, ranges clear | +12%, ranges clear | 19,116 | 4% |
| payments | v0.41.0 | 5 | 605 (601-606) | 52.57 (52.40-52.90) | 63.8 (63.1-64.4) | 70.3 (69.2-74.1) | 1.29 | 0.11 | 0.19 | 2.00 |  |  | 38,136 | 2% |
| payments | v0.42.0 | 5 | 638 (635-642) | 49.85 (49.48-50.10) | 60.9 (60.1-61.1) | 67.2 (65.6-70.3) | 1.25 | 0.11 | 0.19 | 2.00 | +6%, ranges clear |  | 38,136 | 2% |
| payments | main | 5 | 671 (666-672) | 47.33 (47.28-47.75) | 58.0 (57.7-58.9) | 64.1 (62.3-65.7) | 1.25 | 0.11 | 0.20 | 2.00 | +11%, ranges clear |  | 38,136 | 2% |
| payments | main+cache | 5 | 770 (761-771) | 41.38 (41.30-41.70) | 49.1 (47.9-50.1) | 55.1 (53.2-62.1) | 1.11 | 0.13 | 0.22 | 2.00 | +27%, ranges clear | +15%, ranges clear | 38,136 | 2% |
| profile | v0.41.0 | 5 | 609 (607-610) | 52.21 (52.12-52.33) | 63.3 (62.5-63.7) | 68.7 (68.0-72.6) | 1.30 | 0.09 | 0.18 | 2.00 |  |  | 50,026 | 1% |
| profile | v0.42.0 | 5 | 641 (639-642) | 49.63 (49.56-49.77) | 60.5 (60.2-61.0) | 69.0 (66.2-69.6) | 1.25 | 0.09 | 0.18 | 2.00 | +5%, ranges clear |  | 50,026 | 1% |
| profile | main | 5 | 677 (676-679) | 46.96 (46.92-47.00) | 56.8 (56.0-58.1) | 63.7 (62.5-65.6) | 1.25 | 0.10 | 0.19 | 2.00 | +11%, ranges clear |  | 50,026 | 1% |
| profile | main+cache | 5 | 772 (768-777) | 41.24 (40.99-41.50) | 48.0 (47.5-49.2) | 54.5 (54.0-55.8) | 1.12 | 0.11 | 0.22 | 2.00 | +27%, ranges clear | +14%, ranges clear | 50,026 | 2% |
| stock_level | v0.41.0 | 5 | 583 (581-586) | 54.67 (54.30-54.76) | 65.8 (64.8-67.7) | 74.8 (70.7-76.8) | 1.28 | 0.37 | 0.20 | 2.01 |  |  | 5,343 | 11% |
| stock_level | v0.42.0 | 5 | 613 (612-616) | 52.00 (51.74-52.01) | 62.7 (61.5-63.1) | 69.0 (67.7-71.5) | 1.23 | 0.39 | 0.20 | 2.00 | +5%, ranges clear |  | 5,343 | 11% |
| stock_level | main | 5 | 642 (640-644) | 49.61 (49.46-49.70) | 59.3 (59.1-60.3) | 66.4 (64.6-67.6) | 1.23 | 0.41 | 0.20 | 2.00 | +10%, ranges clear |  | 5,343 | 12% |
| stock_level | main+cache | 5 | 725 (719-728) | 44.02 (43.85-44.29) | 50.9 (50.3-51.9) | 59.2 (55.6-62.3) | 1.10 | 0.47 | 0.24 | 2.00 | +24%, ranges clear | +13%, ranges clear | 5,343 | 14% |

## m3: c=1

| query | build | n | req/s | p50 ms | p99 ms | p99.9 ms | gateway cores | db cores | load cores | MySQL stmts/req | vs v0.41.0 | vs main | direct p50 ms | gateway adds (p50) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| customer | v0.41.0 | 5 | 764 (755-765) | 1.28 (1.28-1.29) | 1.8 (1.8-1.8) | 2.6 (2.4-2.8) | 0.95 | 0.06 | 0.18 | 2.00 |  |  | 0.034 | +1.24 ms |
| customer | v0.42.0 | 5 | 767 (765-774) | 1.27 (1.27-1.27) | 1.8 (1.7-1.8) | 2.5 (2.4-2.7) | 0.94 | 0.06 | 0.19 | 2.00 | +0%, ranges clear |  | 0.034 | +1.24 ms |
| customer | main | 5 | 804 (796-819) | 1.22 (1.20-1.23) | 1.7 (1.6-1.7) | 2.6 (2.1-2.9) | 0.94 | 0.06 | 0.19 | 2.00 | +5%, ranges clear |  | 0.034 | +1.18 ms |
| customer | main+cache | 5 | 844 (837-848) | 1.16 (1.15-1.17) | 1.6 (1.6-1.6) | 2.3 (2.1-2.5) | 0.95 | 0.07 | 0.20 | 2.00 | +10%, ranges clear | +5%, ranges clear | 0.034 | +1.12 ms |
| order_status | v0.41.0 | 5 | 633 (628-641) | 1.54 (1.53-1.55) | 2.1 (2.0-2.1) | 3.2 (2.9-3.3) | 0.91 | 0.09 | 0.20 | 2.00 |  |  | 0.081 | +1.46 ms |
| order_status | v0.42.0 | 5 | 642 (637-646) | 1.52 (1.51-1.53) | 2.1 (2.0-2.1) | 3.2 (2.9-3.2) | 0.90 | 0.09 | 0.20 | 2.00 | +1%, ranges overlap |  | 0.081 | +1.44 ms |
| order_status | main | 5 | 665 (659-676) | 1.46 (1.45-1.48) | 2.0 (1.9-2.1) | 3.0 (2.5-3.1) | 0.90 | 0.09 | 0.20 | 2.00 | +5%, ranges clear |  | 0.081 | +1.38 ms |
| order_status | main+cache | 5 | 698 (684-700) | 1.40 (1.40-1.43) | 1.9 (1.9-2.0) | 2.9 (2.8-3.0) | 0.91 | 0.10 | 0.21 | 2.00 | +10%, ranges clear | +5%, ranges clear | 0.081 | +1.32 ms |
| stock_level | v0.41.0 | 5 | 551 (546-556) | 1.76 (1.75-1.78) | 2.4 (2.3-2.4) | 3.2 (3.0-3.3) | 0.71 | 0.27 | 0.20 | 2.00 |  |  | 0.417 | +1.35 ms |
| stock_level | v0.42.0 | 5 | 557 (548-558) | 1.75 (1.74-1.76) | 2.4 (2.3-2.4) | 3.1 (3.1-3.5) | 0.70 | 0.27 | 0.19 | 2.00 | +1%, ranges overlap |  | 0.417 | +1.33 ms |
| stock_level | main | 5 | 571 (562-573) | 1.70 (1.70-1.71) | 2.3 (2.3-2.5) | 3.2 (3.1-3.3) | 0.70 | 0.28 | 0.19 | 2.00 | +4%, ranges clear |  | 0.417 | +1.29 ms |
| stock_level | main+cache | 5 | 597 (591-605) | 1.64 (1.62-1.65) | 2.2 (2.1-2.2) | 2.8 (2.5-3.2) | 0.70 | 0.29 | 0.20 | 2.00 | +8%, ranges clear | +4%, ranges clear | 0.417 | +1.22 ms |

## m4: capacity (closed loop, c=32), then open loop on order_status, latency corrected

- capacity, main: 583 (581-584) req/s over 5 rounds

- capacity, main+cache: 652 (649-654) req/s over 5 rounds

| build | offered (x capacity) | offered req/s | achieved req/s | p50 ms | p99 ms | p99.9 ms | ok | n |
|---|---|---|---|---|---|---|---|---|
| main | 0.25 | 145 | 145 (145-145) | 2.11 (2.07-2.11) | 3.1 (3.0-3.1) | 20.2 (9.8-22.5) | 1.0000 | 5 |
| main | 0.50 | 291 | 291 (291-291) | 1.83 (1.80-1.85) | 2.5 (2.4-2.5) | 10.5 (8.1-26.3) | 1.0000 | 5 |
| main | 0.75 | 436 | 436 (436-436) | 1.62 (1.60-1.64) | 5.0 (3.0-8.8) | 29.8 (12.1-31.5) | 1.0000 | 5 |
| main | 0.90 | 524 | 524 (524-524) | 1.60 (1.58-1.65) | 5.5 (4.6-9.0) | 19.2 (14.2-23.6) | 1.0000 | 5 |
| main | 1.00 | 582 | 582 (582-582) | 1.86 (1.82-1.87) | 17.0 (13.4-25.3) | 27.1 (22.5-31.0) | 1.0000 | 5 |
| main | 1.10 | 640 | 619 (617-627) | 450.42 (373.86-468.65) | 857.0 (612.2-903.4) | 863.7 (616.7-913.1) | 1.0000 | 5 |
| main+cache | 0.25 | 162 | 162 (162-162) | 2.02 (2.00-2.02) | 2.9 (2.9-3.1) | 12.3 (8.5-26.6) | 1.0000 | 5 |
| main+cache | 0.50 | 325 | 325 (325-325) | 1.67 (1.60-1.69) | 2.3 (2.2-2.7) | 16.9 (7.0-30.6) | 1.0000 | 5 |
| main+cache | 0.75 | 488 | 488 (488-488) | 1.50 (1.49-1.52) | 4.6 (3.0-12.1) | 21.2 (16.9-31.4) | 1.0000 | 5 |
| main+cache | 0.90 | 586 | 586 (586-586) | 1.73 (1.65-1.75) | 9.0 (6.7-10.9) | 19.9 (18.4-23.6) | 1.0000 | 5 |
| main+cache | 1.00 | 651 | 651 (651-651) | 1.93 (1.85-2.00) | 17.8 (16.9-22.0) | 24.6 (21.5-30.0) | 1.0000 | 5 |
| main+cache | 1.10 | 716 | 673 (669-681) | 596.91 (491.56-664.21) | 1,366.0 (1,137.6-1,482.1) | 1,382.7 (1,163.6-1,496.9) | 1.0000 | 5 |

## m5: scale-out, c=64

| query | workers | build | n | req/s | p50 ms | p99 ms | gateway cores | req/s per gateway core | db cores | load cores | vs main |
|---|---|---|---|---|---|---|---|---|---|---|---|
| order_status | 1 | main | 5 | 583 (581-585) | 109.4 (109.0-109.7) | 128.5 (124.2-130.9) | 1.24 | 469 | 0.12 | 0.19 |  |
| order_status | 1 | main+cache | 5 | 650 (647-654) | 98.3 (97.6-98.5) | 111.1 (109.8-116.8) | 1.12 | 580 | 0.14 | 0.21 | +11%, ranges clear |
| order_status | 2 | main | 5 | 968 (963-972) | 65.9 (64.9-66.6) | 85.1 (82.8-88.7) | 2.47 | 392 | 0.20 | 0.23 |  |
| order_status | 2 | main+cache | 5 | 1,114 (1,111-1,116) | 57.0 (56.8-58.1) | 69.3 (67.2-77.4) | 2.26 | 494 | 0.23 | 0.29 | +15%, ranges clear |
| order_status | 4 | main | 5 | 1,363 (1,357-1,378) | 43.3 (42.1-46.6) | 83.1 (70.0-88.3) | 4.70 | 290 | 0.33 | 0.28 |  |
| order_status | 4 | main+cache | 5 | 1,639 (1,633-1,646) | 36.6 (35.6-38.5) | 64.8 (56.3-67.8) | 4.46 | 368 | 0.38 | 0.33 | +20%, ranges clear |
| order_status | 8 | main | 5 | 1,873 (1,805-1,936) | 24.3 (16.2-30.8) | 126.8 (92.7-147.2) | 7.16 | 262 | 0.52 | 0.56 |  |
| order_status | 8 | main+cache | 5 | 2,114 (2,092-2,202) | 25.6 (12.1-26.1) | 83.8 (74.4-133.5) | 7.20 | 294 | 0.58 | 0.56 | +13%, ranges clear |
| stock_level | 1 | main | 5 | 639 (636-642) | 99.9 (99.4-100.3) | 115.4 (112.8-118.0) | 1.22 | 522 | 0.40 | 0.21 |  |
| stock_level | 1 | main+cache | 5 | 724 (721-726) | 88.4 (88.1-88.6) | 99.4 (99.2-101.8) | 1.09 | 661 | 0.47 | 0.22 | +13%, ranges clear |
| stock_level | 2 | main | 5 | 1,044 (1,041-1,048) | 60.6 (60.5-60.7) | 77.6 (76.3-78.9) | 2.46 | 425 | 0.74 | 0.25 |  |
| stock_level | 2 | main+cache | 5 | 1,213 (1,207-1,219) | 52.5 (52.0-53.0) | 64.4 (61.7-67.3) | 2.21 | 549 | 0.90 | 0.32 | +16%, ranges clear |
| stock_level | 4 | main | 5 | 1,476 (1,471-1,485) | 38.7 (35.9-39.5) | 75.7 (74.9-95.7) | 4.65 | 317 | 1.30 | 0.34 |  |
| stock_level | 4 | main+cache | 5 | 1,763 (1,758-1,774) | 33.4 (24.6-37.6) | 71.7 (55.8-86.3) | 4.30 | 410 | 1.64 | 0.37 | +19%, ranges clear |
| stock_level | 8 | main | 5 | 1,989 (1,914-2,032) | 21.3 (15.6-31.6) | 136.1 (69.2-151.3) | 6.96 | 286 | 2.18 | 0.56 |  |
| stock_level | 8 | main+cache | 5 | 2,236 (2,213-2,254) | 18.0 (13.9-24.3) | 123.6 (78.1-155.3) | 6.85 | 327 | 2.47 | 0.58 | +12%, ranges clear |

## m6: threads per worker, c=32

| query | threads | n | req/s | p50 ms | p99 ms | gateway cores |
|---|---|---|---|---|---|---|
| customer | 1 | 5 | 865 (864-879) | 36.56 (35.92-36.65) | 45.0 (44.0-46.1) | 0.98 |
| customer | 2 | 5 | 823 (817-827) | 38.52 (38.34-38.91) | 46.2 (45.5-46.8) | 1.09 |
| customer | 4 | 5 | 722 (721-727) | 44.05 (43.73-44.10) | 52.2 (51.6-54.3) | 1.18 |
| customer | 8 | 5 | 676 (675-677) | 47.00 (46.92-47.07) | 57.4 (56.7-58.2) | 1.25 |
| stock_level | 1 | 5 | 630 (624-633) | 50.44 (49.87-50.79) | 60.6 (59.4-62.8) | 0.72 |
| stock_level | 2 | 5 | 772 (769-777) | 41.02 (40.70-41.19) | 48.8 (47.5-49.5) | 1.05 |
| stock_level | 4 | 5 | 683 (681-688) | 46.57 (46.32-46.73) | 55.9 (54.2-56.3) | 1.14 |
| stock_level | 8 | 5 | 642 (636-646) | 49.60 (49.32-49.94) | 59.2 (59.1-61.5) | 1.22 |

## m7: uniform against Zipf-distributed customers, with and without a 30 s response cache

| customers | query | n | req/s | p50 ms | p99 ms | cache hit rate | MySQL stmts/req |
|---|---|---|---|---|---|---|---|
| uniform | order_status | 5 | 582 (578-583) | 54.72 (54.59-55.01) | 66.7 (65.9-67.8) | - | 2.00 |
| uniform | order_status_cached | 5 | 537 (535-541) | 59.27 (58.94-59.45) | 72.9 (71.2-75.9) | 0.05 (0.05-0.06) | 1.90 |
| zipf | order_status | 5 | 581 (580-582) | 54.69 (54.54-54.88) | 66.8 (66.7-67.1) | - | 2.00 |
| zipf | order_status_cached | 5 | 717 (714-719) | 44.29 (44.10-44.54) | 57.8 (57.6-58.3) | 0.59 (0.58-0.59) | 0.83 |

## m8: c=1

| query | build | n | req/s | p50 ms | p99 ms | p99.9 ms | gateway cores | db cores | load cores | MySQL stmts/req | vs v0.41.0 | vs main |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| order_status | v0.41.0 | 5 | 280 (275-284) | 3.58 (3.48-3.64) | 4.1 (4.1-4.1) | 5.1 (4.8-5.2) | 0.44 | 0.05 | 0.16 | 2.00 |  |  |
| order_status | v0.42.0 | 5 | 279 (278-281) | 3.58 (3.57-3.61) | 4.1 (4.1-4.2) | 4.8 (4.5-5.1) | 0.44 | 0.05 | 0.16 | 2.00 | -0%, ranges overlap |  |
| order_status | main | 5 | 283 (282-287) | 3.55 (3.47-3.56) | 4.1 (4.0-4.1) | 4.8 (4.6-4.9) | 0.44 | 0.05 | 0.18 | 2.00 | +1%, ranges overlap |  |
| order_status | main+cache | 5 | 293 (287-295) | 3.39 (3.33-3.49) | 4.0 (4.0-4.0) | 4.7 (4.7-4.9) | 0.43 | 0.05 | 0.17 | 2.00 | +5%, ranges clear | +3%, ranges clear |

## m8: c=32

| query | build | n | req/s | p50 ms | p99 ms | p99.9 ms | gateway cores | db cores | load cores | MySQL stmts/req | vs v0.41.0 | vs main |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| order_status | v0.41.0 | 5 | 421 (420-427) | 75.72 (74.70-75.91) | 88.7 (88.3-91.8) | 101.9 (96.9-104.5) | 1.02 | 0.08 | 0.17 | 2.01 |  |  |
| order_status | v0.42.0 | 5 | 436 (434-444) | 73.08 (71.89-73.46) | 87.3 (83.2-87.7) | 93.9 (88.7-102.6) | 0.98 | 0.08 | 0.18 | 2.01 | +4%, ranges clear |  |
| order_status | main | 5 | 450 (450-451) | 70.79 (70.68-70.96) | 83.7 (83.6-85.8) | 92.6 (91.1-100.0) | 0.97 | 0.09 | 0.17 | 2.00 | +7%, ranges clear |  |
| order_status | main+cache | 5 | 489 (486-490) | 65.21 (65.04-65.70) | 75.1 (73.2-77.0) | 82.0 (80.4-94.3) | 0.87 | 0.09 | 0.19 | 2.00 | +16%, ranges clear | +9%, ranges clear |

## m8: the store, SQLite (local) against PostgreSQL (0.5 ms away), one caller, order_status

| store | build | n | req/s | p50 ms | p99 ms |
|---|---|---|---|---|---|
| sqlite | main | 5 | 284 (283-287) | 3.52 (3.48-3.54) | 4.1 (4.0-4.1) |
| sqlite | main+cache | 5 | 293 (289-293) | 3.39 (3.37-3.47) | 4.0 (4.0-4.0) |
| postgres | main | 5 | 166 (165-170) | 6.06 (5.91-6.09) | 7.0 (6.9-7.1) |
| postgres | main+cache | 5 | 292 (291-295) | 3.43 (3.39-3.44) | 4.1 (4.1-4.1) |

## m9: round trips per call

| build | statements per call (general log, 100 calls) | of which |
|---|---|---|
| v0.41.0 | 2.00 | 1.00 × `SELECT c_id, c_first, c_middle, c_last, c_balance, c_credit, c_since F`; 1.00 × `rollback` |
| v0.42.0 | 2.00 | 1.00 × `SELECT c_id, c_first, c_middle, c_last, c_balance, c_credit, c_since F`; 1.00 × `rollback` |
| main | 2.00 | 1.00 × `SELECT c_id, c_first, c_middle, c_last, c_balance, c_credit, c_since F`; 1.00 × `rollback` |
| main+cache | 2.00 | 1.00 × `SELECT c_id, c_first, c_middle, c_last, c_balance, c_credit, c_since F`; 1.00 × `rollback` |

Single-call p50 (ms) against netem delay; the slope is round trips per call:

| | +0ms | +0.5ms | +1ms | +2ms | slope | at 0 delay |
|---|---|---|---|---|---|---|
| mysql-connector alone, one | 0.04 (0.04-0.04) | 0.64 (0.63-0.64) | 1.23 (1.22-1.23) | 2.49 (2.44-2.51) | 1.22 | 0.03 ms |
| mysql-connector alone, two | 0.05 (0.05-0.05) | 1.26 (1.25-1.26) | 2.39 (2.38-2.41) | 4.75 (4.70-4.82) | 2.35 | 0.06 ms |
| v0.41.0 | 1.27 (1.27-1.28) | 3.30 (3.18-3.38) | 4.94 (4.92-4.95) | 8.57 (8.57-8.70) | 3.64 | 1.35 ms |
| v0.42.0 | 1.27 (1.26-1.28) | 3.28 (3.28-3.30) | 4.91 (4.88-4.94) | 8.68 (8.12-8.74) | 3.59 | 1.36 ms |
| main | 1.21 (1.20-1.23) | 3.21 (3.21-3.22) | 4.86 (4.84-4.89) | 8.25 (8.20-8.58) | 3.53 | 1.31 ms |
| main+cache | 1.16 (1.16-1.16) | 3.06 (3.05-3.07) | 4.78 (4.73-4.80) | 8.48 (8.47-8.57) | 3.66 | 1.17 ms |

## m10: profile, main (9,604 samples, threads on CPU only)

| category | share |
|---|---|
| store layer: queries, decoding, checks | 25.2% |
| data query: driver and pool | 20.7% |
| logging | 14.0% |
| gunicorn, sockets, HTTP | 13.9% |
| Flask / Werkzeug | 6.7% |
| history writer thread | 5.9% |
| JSON | 4.0% |
| SQL checks, parameters | 3.7% |
| app request handling | 3.2% |
| auth, limits, quotas, metrics | 2.2% |
| other | 0.6% |

| busiest attributed frames | share |
|---|---|
| `load (queryapigate/store.py:538)` | 7.3% |
| `<lambda> (queryapigate/apikeys.py:936)` | 7.3% |
| `<lambda> (queryapigate/store.py:336)` | 6.3% |
| `acquire (logging/__init__.py:973)` | 5.2% |
| `cmd_query (mysql/connector/connection_cext.py:772)` | 4.9% |
| `write (gunicorn/util.py:348)` | 4.4% |
| `_target (queryapigate/db.py:685)` | 3.6% |
| `_open_connection (mysql/connector/connection_cext.py:354)` | 3.2% |
| `rollback (mysql/connector/connection_cext.py:611)` | 3.1% |
| `emit (logging/__init__.py:1163)` | 3.0% |
| `is_connected (mysql/connector/connection_cext.py:414)` | 2.6% |
| `notify (gunicorn/workers/workertmp.py:44)` | 2.2% |

## m10: profile, main+cache (8,402 samples, threads on CPU only)

| category | share |
|---|---|
| data query: driver and pool | 24.9% |
| gunicorn, sockets, HTTP | 17.7% |
| logging | 17.3% |
| Flask / Werkzeug | 9.2% |
| JSON | 6.5% |
| history writer thread | 6.0% |
| SQL checks, parameters | 5.7% |
| store layer: queries, decoding, checks | 5.7% |
| app request handling | 4.2% |
| auth, limits, quotas, metrics | 2.3% |
| other | 0.5% |

| busiest attributed frames | share |
|---|---|
| `acquire (logging/__init__.py:973)` | 8.6% |
| `write (gunicorn/util.py:348)` | 6.3% |
| `cmd_query (mysql/connector/connection_cext.py:772)` | 6.2% |
| `_target (queryapigate/db.py:685)` | 3.8% |
| `is_connected (mysql/connector/connection_cext.py:414)` | 3.8% |
| `rollback (mysql/connector/connection_cext.py:611)` | 3.6% |
| `_open_connection (mysql/connector/connection_cext.py:354)` | 2.9% |
| `get_rows (mysql/connector/connection_cext.py:540)` | 2.8% |
| `emit (logging/__init__.py:1163)` | 2.7% |
| `notify (gunicorn/workers/workertmp.py:44)` | 2.5% |
| `chunk (gunicorn/http/unreader.py:77)` | 2.5% |
| `flush (logging/__init__.py:1144)` | 2.4% |

## m11: the table allow-list across databases (a key allowed `customer`)

| SQL | status | code |
|---|---|---|
| `SELECT COUNT(*) AS n FROM crm.customer` | 200 |  |
| `SELECT COUNT(*) AS n FROM billing.customer` | 200 |  |
| `SELECT COUNT(*) AS n FROM billing.history` | 403 | table_not_allowed |

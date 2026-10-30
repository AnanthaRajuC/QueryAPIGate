# The data plane as the image ships it (ADR 0030 §8, 2026-10-30)

ADR 0030's measurement: the Docker image against the from-source build of the same commit, and one pod's ceiling
without a run store. `benchmarks/oltp.py` blocks s1 and s2 on the same laptop as before, set up the same way: AC power,
the `performance` profile, the gateway on CPUs 0-7, PostgreSQL 16 on 8-11, the load generator on 12-19. The commit is
`feat/dataplane-shipping` at f206f49.

Both forms are built from one export of that commit: the image with its Dockerfile (`sha256:108513ba8465`, its data
plane 10.7 MB, cross-compiled and stripped), the from-source data plane with `cargo build --release` and the toolchain
the repository pins (1.98.1). Both run the data plane in front of one Python worker, the metadata cache on, on the
PostgreSQL store. The image runs as two containers on the host's network, so they meet on loopback as the processes
do, and their CPU is read from their cgroups; logs are written and dropped in both.

- `results.jsonl`: s1 and s2, the from-source side on the development virtualenv.
- `results-matched.jsonl`: s1 again, the from-source side on a virtualenv with the image's 69 packages at its exact
  versions (`matched-requirements.txt`).
- `interpreter.py`: the Python workload used to find where the difference is (below).

## The 2-CPU pod (s2): every run recorded

The image's data plane held to 2 CPUs (`--cpus 2`, as a pod's limit), one Python worker recording every run on the
PostgreSQL store, 64 callers, no rate limit.

| | Calls a second | Runs recorded | p99 | Data plane CPUs | Python CPUs |
|---|---|---|---|---|---|
| 5 rounds of 30 s | 16,788-17,018 | 100% each | 5.5-5.7 ms | 1.90 | 0.49 |
| 10 minutes | 16,403 | 9,842,067 of 9,842,054 calls: 100% | 5.5 ms | 1.92 | 0.50 |

About 8,900 calls a second per data plane CPU, more than the 7,000 ADR 0030 assumed from ADR 0029's measurement, and
Python recorded them with half a core. A pod sized as the docs say, about 2 CPUs for the data plane without a run
store, records everything, with room on Python's side.

## The image against the from-source build (s1)

Calls a second per gateway core (the data plane's and Python's CPU together), the median of 5 interleaved rounds:

| Per gateway core | From source | Image | Image ÷ source | Matched: from source | Image | Image ÷ source |
|---|---|---|---|---|---|---|
| Plain lookup | 4,095 | 4,058 | 0.99 | 4,092 | 4,013 | 0.98 |
| Masked, table-restricted lookup | 3,916 | 3,908 | 0.99 | 3,934 | 3,888 | 0.99 |
| 100-row page | 2,829 | 2,691 | 0.95 | 2,829 | 2,691 | 0.95 |

- **The data plane serves the same in the image.** It used the same 6.0-6.2 CPUs on the lookups, 3.2 on the page, with
  p99 the same (3.3-3.7 ms on lookups, 9.2-9.6 ms on the page).
- **What differs is Python.** On the page, Python used 0.37-0.38 CPUs in the image against 0.31-0.32. On the
  lookups, at 64 callers the data plane serves about 29,000 calls a second, above what one Python worker records (ADR
  0030 §3), and the image's recorded 11,700 a second against 14,500, on the same 1.0 CPU.
- **Not the packages:** the matched run, with the image's exact versions on the from-source side, gave the same
  numbers.
- **Not containers either.** `interpreter.py` - parsing batches of reported runs, as recording does - pinned to one
  core: 115-116 ms with the laptop's Python 3.12.3, 148-152 ms with the image's 3.12.15, the same with seccomp off. A
  loop with no JSON at all: 109 ms against 146 ms.
- **It's the interpreter's build.** The official `python:3.12-slim` image links Python against a shared
  `libpython3.12.so`; Ubuntu's `/usr/bin/python3.12` is linked statically. The image's interpreter is about 30%
  slower per CPU on this machine - for every call Python handles, with or without the data plane. It isn't new in
  this ADR: it's how the image has always run (BACKLOG #104).

The first two rounds of the plain lookup ran at 35,800-43,500 calls a second while the package was cool, and then
settled at 29,000; the comparison pairs each round's two builds, which ran back to back. The `/health` floors before
and after each block were 2,305-2,353 calls a second.

## For ADR 0030

Criterion 4 is met: the image shows no regression from running in containers - the data plane in it is the
from-source one's equal, and the difference in Python is the interpreter's build, not the container. One pod with the
data plane at 2 CPUs and no run store recorded every one of 9.8 million runs at 16,400 calls a second.

# The image's Python interpreter (BACKLOG #104, 2026-10-31)

[ADR 0030](../../../documentation/adr/0030-shipping-the-data-plane.md)'s measurement found the image's Python about
30% slower per CPU than the laptop's ([dataplane-image-2026-10-30](../dataplane-image-2026-10-30/README.md)), and
ruled out containers and packages. This screens the interpreters the image could use, and rebuilds the official one a
setting at a time to find the cause. It's the evidence for
[ADR 0031](../../../documentation/adr/0031-the-image-s-python.md).

**The interpreters alone, not the gateway:** no QueryAPIGate, no database, no network. Python's share of a call's CPU
depends on the call, so the gateway gains less than these ratios; ADR 0031's measurement is the gateway's.

## How

The same laptop as every result here (i7-12700H, 32 GB), set up as for
[PERFORMANCE_V2.md](../../../documentation/PERFORMANCE_V2.md): AC power, the `performance` profile.
Each run is one container, `--cpuset-cpus 2` (a P-core) and `--network none`, running one workload, which prints the
median of 9 passes after one warm-up:

- **`interpreter`**: ADR 0030's [`interpreter.py`](../dataplane-image-2026-10-30/interpreter.py), batches of reported
  runs parsed and re-encoded as Python's recording does (the `json` C extension, dicts, tuples).
- **`loop`**: [`loop.py`](loop.py), dicts, arithmetic and f-strings, no library at all.

[`screen.sh`](screen.sh) runs 5 rounds, rotating the candidates' order each round, so none always runs first or after
the hottest; each `*.jsonl` line is one run, with the hottest thermal zone after it. [`summarise.py`](summarise.py)
prints the tables below from them. Ratios are against the image's pinned base, `python:3.12-slim@sha256:a0f57064…`;
below 1 is faster.

## 1. The candidates ([`screen-candidates.jsonl`](screen-candidates.jsonl))

| Interpreter | Python | Linked | `interpreter` (range) | Ratio | `loop` (range) | Ratio |
|---|---|---|---|---|---|---|
| **The image's base**, `python:3.12-slim@sha256:a0f57064…` | 3.12.15 | shared `libpython` | 156.9 ms (153.8-158.1) | 1.00 | 57.2 ms (57.0-58.5) | 1.00 |
| `python:3.12-slim@sha256:aa943ce4…` (latest) | 3.12.16 | shared | 156.9 ms (155.6-158.9) | 1.00 | 57.1 ms (56.6-57.4) | 1.00 |
| `python:3.13-slim@sha256:2d44738e…` | 3.13.18 | shared | 157.9 ms (156.3-158.9) | 1.01 | 62.1 ms (61.6-62.4) | 1.09 |
| `python:3.14-slim@sha256:cd89c30e…` | 3.14.10 | shared | 176.1 ms (174.4-177.0) | **1.12** | 66.9 ms (63.1-67.4) | **1.17** |
| **Debian trixie's `python3`** ([`debian.Dockerfile`](debian.Dockerfile)) | 3.13.5-2+deb13u5 | static | 114.7 ms (114.3-115.2) | **0.73** | 40.0 ms (39.9-40.3) | **0.70** |
| python-build-standalone ([`standalone.Dockerfile`](standalone.Dockerfile)) | 3.12.16 | static | 115.1 ms (114.2-115.7) | 0.73 | 39.6 ms (39.1-40.3) | 0.69 |
| python-build-standalone | 3.13.18 | static | 117.6 ms (116.6-118.6) | 0.75 | 38.8 ms (38.6-39.8) | 0.68 |
| python-build-standalone | 3.14.10 | static | 116.7 ms (116.1-117.8) | 0.74 | 36.5 ms (36.4-36.8) | 0.64 |
| The laptop's Ubuntu `/usr/bin/python3.12` (no container) | 3.12.3 | static | 123.4 ms (123.2-124.5) | 0.79 | 44.6 ms (44.4-45.9) | 0.78 |

Thermal 84-98 C. Every official image is the slow one, and a newer Python doesn't help: 3.14-slim is slower again.
Every statically linked interpreter, from three different builders, is 21-36% faster.

## 2. The official recipe, rebuilt ([`screen-rebuilds.jsonl`](screen-rebuilds.jsonl))

[`cpython.Dockerfile`](cpython.Dockerfile) is the pinned image's own build of 3.12.15, read from its `docker history`
(the same source, `configure` flags, `--enable-optimizations --with-lto`, `dpkg-buildflags` and frame pointers, on
`debian:trixie-slim`), with switches to leave one thing out. Each takes 7.5-9 minutes to build here.

| 3.12.15, built | `interpreter` (range) | Ratio | `loop` (range) | Ratio |
|---|---|---|---|---|
| The pinned image | 161.8 ms (161.7-165.0) | 1.00 | 59.5 ms (58.8-59.9) | 1.00 |
| Its recipe, rebuilt as it is | 164.1 ms (161.0-165.6) | 1.01 | 59.4 ms (58.3-60.2) | 1.00 |
| Without frame pointers | 158.7 ms (156.4-159.3) | 0.98 | 57.2 ms (56.6-57.2) | 0.96 |
| Static (no `--enable-shared`) | 149.5 ms (147.9-151.6) | 0.92 | 49.7 ms (49.3-50.5) | 0.84 |
| Static, without frame pointers | 147.2 ms (146.0-147.8) | 0.91 | 47.6 ms (47.4-47.7) | 0.80 |
| python-build-standalone 3.12.16 | 119.4 ms (118.9-119.8) | 0.74 | 40.3 ms (39.8-57.7) | 0.68 |

Thermal 89-98 C. The rebuild reproduces the image (1.01, 1.00), so the recipe is the cause. Linking statically gains
8-16%, and frame pointers cost 2-4%.

## 3. Without Debian's build flags ([`screen-flags.jsonl`](screen-flags.jsonl))

The recipe appends `dpkg-buildflags`' `CFLAGS` and `LDFLAGS` (`-g -O2`, `-fstack-protector-strong`,
`-fstack-clash-protection`, `-fcf-protection`, `-Wl,-z,relro`) to CPython's own (`-O3`).

| 3.12.15, built | `interpreter` (range) | Ratio | `loop` (range) | Ratio |
|---|---|---|---|---|
| The pinned image | 163.1 ms (161.5-164.1) | 1.00 | 59.2 ms (58.8-59.6) | 1.00 |
| Static, without frame pointers | 146.7 ms (146.2-146.9) | 0.90 | 47.7 ms (47.3-48.3) | 0.81 |
| …and without `dpkg-buildflags` | 135.5 ms (134.5-135.7) | 0.83 | 42.0 ms (41.3-42.7) | 0.71 |
| python-build-standalone 3.12.16 | 118.8 ms (117.8-119.8) | 0.73 | 40.3 ms (39.8-41.3) | 0.68 |
| Debian trixie's `python3` 3.13.5 | 119.9 ms (119.3-120.3) | 0.74 | 41.3 ms (41.1-41.8) | 0.70 |

Thermal 69-101 C. Leaving the flags out gains another 7-10%. **Which flag isn't known:** Debian builds its own
`python3` with `-O2` and the same hardening flags (its `sysconfig`: `-fstack-protector -fstack-clash-protection
-fcf-protection -fwrapv -O2`, no frame pointers) and is as fast as python-build-standalone, so they don't by themselves
make an interpreter slow. Debian's packaging builds its static interpreter its own way, not with CPython's
`configure` alone. Neither that nor the rest of the official image's gap, after these three settings, was taken apart
further.

## What each build contains

| | The image's base | Debian trixie `python3` | python-build-standalone 3.12 |
|---|---|---|---|
| Compiler | GCC 14.2.0 | GCC 14.2.0 | Clang 23.1.3 |
| Optimization | PGO, LTO, `-O3`, then `-O2` from `dpkg-buildflags` | `-O2`; the rest is Debian's packaging, not checked | PGO, full LTO, BOLT, `-O3` |
| The interpreter | links `libpython3.12.so` | static | static, extension modules too |
| OpenSSL | Debian's `libssl3t64` 3.5.7 | Debian's `libssl3t64` 3.5.7-1~deb13u3 | its own, 3.5.9, compiled in |
| SQLite | Debian's | Debian's 3.46.1-7+deb13u2 | its own, 3.53.1, compiled in |
| Image, uncompressed | 119.4 MB | 114.1 MB (`debian:trixie-slim` is 78.9 MB) | 219.4 MB (uv and the whole install, unpruned) |

## Not measured

- **The gateway.** These are the interpreters alone; ADR 0031 measures the image itself.
- **arm64.** There's no arm64 hardware here, and timings under QEMU mean nothing. python-build-standalone's BOLT is
  x86-64 only; Debian builds both architectures the same way.
- **Which of Debian's build steps makes its interpreter fast** (above).

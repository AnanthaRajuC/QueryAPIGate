# The image's Python interpreter (BACKLOG #104, 2026-10-31)

[ADR 0030](../../../documentation/adr/0030-shipping-the-data-plane.md)'s measurement found the image's Python about
30% slower per CPU than the laptop's ([dataplane-image-2026-10-30](../dataplane-image-2026-10-30/README.md)), and
ruled out containers and packages. This screens the interpreters the image could use, and rebuilds the official one a
setting at a time to find the cause (sections 1-3). Then it measures the gateway, runs the suite inside the image,
and scans both images (sections 4-6). It's the evidence for
[ADR 0031](../../../documentation/adr/0031-the-image-s-python.md), which chose python-build-standalone's 3.13
(section 7) over Debian's `python3` (sections 4-6).

**Sections 1-3 are the interpreters alone, not the gateway:** no QueryAPIGate, no database, no network. Python's
share of a call's CPU depends on the call, so the gateway gains less than these ratios (section 4).

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

## 4. The gateway: the image against the image ([`p1-results.jsonl`](p1-results.jsonl))

ADR 0031, section 5, item 3: `benchmarks/oltp.py` block `p1`, with log [`p1-run.txt`](p1-run.txt) and summary
[`p1_summary.py`](p1_summary.py). The base is `main` at `a8f63da`, on the official `python:3.12-slim` (Python 3.12.15,
image `sha256:bd8c87ba…`, 443.9 MB). The head is the ADR's branch (stash `0ceb3df`), on Debian's Python 3.13.5 (image
`sha256:d6537f33…`, 446.9 MB). The application doesn't differ between them: `git diff` of `queryapigate/`,
`dataplane/` and `pyproject.toml` is empty.

Both images run as the chart runs them, on CPUs 0-7, with PostgreSQL 16 (pgbench scale 100, and the store) on 8-11
and oha on 12-19. The metadata cache is on, there are 64 callers, and each cell is 30 s. There are 5 rounds,
rotated. `/health` floors are 2,298 calls/s before and 2,367 after; the gateway's clock is 3,970-4,120 MHz with Python
alone and 2,200-2,570 MHz with the data plane.

**Python alone**, one worker, as the image runs by default:

| Scenario | Calls/s: official / Debian | Per gateway core: official / Debian | Ratio (range) | p99 ms: official / Debian |
|---|---|---|---|---|
| Plain lookup | 993 / 1,079 | 895 / 964 | **1.068** (1.059-1.083) | 72.2 / 67.2 |
| Masked, table-restricted lookup | 948 / 1,009 | 855 / 904 | **1.059** (1.055-1.060) | 76.7 / 70.9 |
| 100-row page | 814 / 890 | 733 / 794 | **1.079** (1.069-1.086) | 88.9 / 81.5 |

**The data plane in front**, Python behind it on 127.0.0.1, recording every run:

| Scenario | Calls/s: official / Debian | Per gateway core (range) | Python cores: official / Debian | Runs recorded: official / Debian |
|---|---|---|---|---|
| Plain lookup | 27,982 / 27,849 | 1.013 (0.992-1.023) | 1.00 / 0.95 | **42.0% / 67.7%** |
| Masked, table-restricted lookup | 27,130 / 27,031 | 1.008 (0.992-1.012) | 1.01 / 0.95 | **42.3% / 66.6%** |
| 100-row page | 9,420 / 9,596 | 1.029 (1.013-1.032) | 0.37 / **0.30** | 100% / 100% |

The interpreter's 27-30% shrinks to 6-9% on calls Python serves itself. Much of a call's CPU is outside the
interpreter: psycopg2, the C extensions and the kernel's networking are the same in both images. And Python 3.13
itself costs back some of the interpreter's gain: in section 1, the official 3.13 is 1-9% slower than its 3.12.
Recording is Python's own loop, and there the gain is large. Past its ceiling, one worker recorded 60% more of the
data plane's runs (about 18,900 a second against 11,750) with 5% less CPU. With every run recorded, it needed 20%
less CPU. No scenario is slower.

## 5. The suite inside the image

ADR 0031, section 5, item 2: the whole suite on the image's interpreter and installed packages
([`in-image-suite.sh`](in-image-suite.sh)), against a copy of the branch. Only what a development checkout has is
added: `git`, the test tools and `jaydebeapi`, with the image's own packages kept at their versions. The image's
`QUERYAPIGATE_BIND` is unset for the test process.

| Image | Store | Result |
|---|---|---|
| Debian, Python 3.13.5 | SQLite, Redis | 2,172 tests OK (skipped 118) |
| Debian, Python 3.13.5 | PostgreSQL 16, Redis | 2,172 tests OK (skipped 114) |
| The 0.47.0 release, Python 3.12.15 (the control) | SQLite, Redis | 2,172 tests OK (skipped 118) |

Without those additions, both images fail the same 15 tests (14 errors, 1 failure): no `git`, no H2 driver, and
`QUERYAPIGATE_BIND` set by the image.

## 6. What a scanner finds ([`trivy-*.json.gz`](.))

ADR 0031, section 5, item 4: Trivy 0.69.3, its database of 2026-10-31, `--scanners vuln`, on both images of
section 4.

| | Official Python 3.12.15 | Debian's Python 3.13.5 |
|---|---|---|
| Debian packages: findings (HIGH) | 166 (44) | 273 (70) |
| Python packages (pip) | 6 (none HIGH) | 6 (none HIGH) |
| The data plane's crates | 0 | 0 |

**The difference is Python itself.** It shows in eight packages, all of them Debian's Python or what it brings:
`python3.13`, `python3.13-minimal`, `python3.13-venv`, `libpython3.13-minimal`, `libpython3.13-stdlib`,
`python3-pip-whl`, `python3-setuptools-whl` and `libexpat1`. Debian's `python3.13` 3.13.5-2+deb13u5 has 19 CVEs that
trixie hasn't fixed (Trivy: `affected`, no fixed version), and five of them are HIGH:

- CVE-2026-15308: `html.parser` CPU denial of service;
- CVE-2026-19445: use-after-free in a server's SNI callback;
- CVE-2026-19553: `SSLContext.wrap_bio()` skipping hostname verification without `server_hostname`;
- CVE-2026-7210: Expat hash flooding;
- CVE-2026-82049: `tarfile` filters bypassed by hard links to symlinks.

The official image's Python is compiled into `/usr/local`, and **Trivy doesn't see it at all**, so its lower count is
not a clean bill. One fix can be checked by behaviour, CVE-2026-19553: a fixed `wrap_bio()` refuses a missing
`server_hostname`.

| Python | `wrap_bio()` without `server_hostname` |
|---|---|
| Official 3.12.15 (the image today) | accepted, with a `DeprecationWarning` |
| python-build-standalone 3.12.16 | accepted, with a `DeprecationWarning` |
| Debian's 3.13.5 | accepted, silently |
| Official 3.13.18, python-build-standalone 3.13.18 | refused: `check_hostname requires server_hostname` |

## 7. python-build-standalone 3.13.18, against Debian's

After section 6, the same three checks for the alternative: python-build-standalone's upstream 3.13.18, installed in a
build stage by `uv` 0.13.6 (pinned by digest; `uv` checks the download's hash) and copied into `debian:trixie-slim`
([`standalone-image.Dockerfile`](standalone-image.Dockerfile)). The pruned form leaves out the install's own `pip`,
Tk and Tcl with tkinter, IDLE and turtledemo, and `libpython3.13.so`, which the statically linked interpreter doesn't
load. That takes the image from 514.1 MB to 465.4 MB.

**At the gateway** ([`p1-standalone-results.jsonl`](p1-standalone-results.jsonl),
[`p1-standalone-run.txt`](p1-standalone-run.txt)), block `p1` with Debian's image (stash `0ceb3df`) as the base and
the unpruned standalone one (stash `1b2e6ff`) as the head. The application is the same. `/health` floors are 2,314
before and 2,359 after. Pruning only removes files the interpreter doesn't open, so it wasn't measured again.

| Scenario | Per gateway core: Debian / standalone | Ratio (range) | Runs recorded: Debian / standalone |
|---|---|---|---|
| Python alone, plain lookup | 964 / 979 | 1.014 (1.011-1.023) | 100% / 100% |
| Python alone, governed lookup | 914 / 927 | 1.009 (0.998-1.023) | 100% / 100% |
| Python alone, 100-row page | 807 / 810 | 1.014 (0.990-1.033) | 100% / 100% |
| Data plane in front, plain lookup | 4,054 / 4,046 | 0.999 (0.982-1.005) | 68.4% / 67.4% |
| Data plane in front, governed lookup | 3,927 / 3,930 | 0.999 (0.996-1.002) | 67.6% / 66.3% |
| Data plane in front, 100-row page | 2,767 / 2,772 | 1.006 (0.996-1.014) | 100% / 100% |

The two are the same speed, within 1-2% either way. Debian's image repeated its section 4 numbers: 964 calls/s per
core on the plain lookup, both times.

**The suite inside the image** ([`in-image-suite.sh`](in-image-suite.sh)), unpruned and pruned alike: 2,172 tests OK
on SQLite with Redis (skipped 118), and on the PostgreSQL store with Redis (skipped 114). The pruned image's own
health check reports `healthy`. HTTPS from its Python verifies against Debian's `ca-certificates` (OpenSSL's
`/etc/ssl/certs`).

**What a scanner finds**, Trivy 0.69.3 as in section 6:

| | Official 3.12.15 (today) | Debian's 3.13.5 | Standalone 3.13.18, pruned |
|---|---|---|---|
| Debian packages: findings (HIGH) | 166 (44) | 273 (70) | 164 (43) |
| Python packages: findings (HIGH) | 6 (0) | 6 (0) | 8 (5) |
| Python itself, as a scanner sees it | not at all | Debian's `python3.13`, 19 CVEs open | not at all |
| Python's own `ssl` | Debian's OpenSSL 3.5.7 | Debian's OpenSSL 3.5.7 | compiled in, 3.5.9 |

The standalone image's five HIGH Python findings are in the virtualenv's `pip` 26.2.2, read from its
`_vendor/vendor.txt` (msgpack 1.1.2, urllib3 2.7.0, setuptools 70.3.0). That `pip` installs the packages; nothing in
QueryAPIGate imports it. Debian's virtualenv gets Debian's older `pip` 25.1.1, which Trivy reports differently.

**OpenSSL is already compiled into the image today.** `psycopg2-binary` loads its own `libssl` and `libcrypto` from
`psycopg2_binary.libs`. `cryptography`'s wheel carries OpenSSL 4.0.3, in all three images. python-build-standalone
adds one more copy, Python's `ssl`: currently newer than Debian's, and fixed when python-build-standalone releases.

## 8. Without `pip` ([`trivy-qag-nopip.json.gz`](trivy-qag-nopip.json.gz), [`trivy-rehearsal-61cfb4f.json.gz`](trivy-rehearsal-61cfb4f.json.gz))

ADR 0031's amendment: the virtualenv is created without `pip`, and the step that installs the packages brings it in
with `ensurepip` and uninstalls it at its end. Compared with `main`'s image (61cfb4f, the `docker.yml` rehearsal's
amd64 image), with Trivy 0.69.3 and one database for both (2026-10-31 07:07 UTC), `--scanners vuln`:

| | `main` (with `pip`) | Without `pip` | Without `pip`, `-h2` |
|---|---|---|---|
| Debian packages: findings (HIGH) | 164 (43) | 164 (43) | 170 (43, and 1 CRITICAL), its JRE's |
| Python packages: findings (HIGH) | 8 (5) | 0 | 0 |
| Size | 465.4 MB | 454.7 MB | 667.9 MB |

All eight findings on `main` were `pip`'s vendored copies: msgpack 1.1.2 (two HIGH), urllib3 2.7.0 (two HIGH, one
MEDIUM) and setuptools 70.3.0 (one HIGH, one MEDIUM, one LOW). The image is smaller because no layer holds `pip`
any more. `ensurepip`'s wheel stays in `/opt/python`, and Trivy doesn't report it.

- **The gateway** starts and answers `/health`, and the `-h2` image imports `jaydebeapi` and the application.
- **The suite inside the image** ([`in-image-suite.sh`](in-image-suite.sh), which now brings in `pip` when the image
  has none): 2,175 tests OK on SQLite with Redis (skipped 118), and on the PostgreSQL store with Redis (skipped
  114).
- **Extending the image:** `FROM` it, `RUN pip install trino` fails (`pip: not found`, exit 127). With `USER root`
  and `RUN python -m ensurepip --default-pip && pip install trino`, it builds, and `import trino` works as the `app`
  user. On `main`'s image, with `pip`, a plain `RUN pip install trino` fails too: `Permission denied` on
  `/opt/venv`, which is root's. On 0.47.0's image it worked: `pip` installed into `/home/app/.local`.

## Not measured

- **arm64.** There's no arm64 hardware here, and timings under QEMU mean nothing. python-build-standalone's BOLT is
  x86-64 only; Debian builds both architectures the same way.
- **python-build-standalone 3.12** at the gateway, or **the pruned standalone image** (section 7).
- **Which of Debian's build steps makes its interpreter fast** (above).

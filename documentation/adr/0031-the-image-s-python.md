# ADR 0031: The image's Python: Debian's own, not the official image's

- **Status:** Proposed (2026-10-31).
- **Date:** 2026-10-31
- **Backlog:** #104, *A faster Python interpreter in the image*. [ADR 0030](0030-shipping-the-data-plane.md#result)'s
  measurement found it; this ADR chooses the interpreter, from the screening in
  [python-image-2026-10-31](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/benchmarks/results/python-image-2026-10-31/README.md).

## Context

The image is `FROM python:3.12-slim`, the official image, pinned by digest (ADR 0018). Its interpreter is about 30%
slower per CPU than other builds of the same Python. ADR 0030 ruled out containers, seccomp and the packages. The
gap affects every call Python handles in the image, with or without the data plane: Python serving alone, and
everything the data plane forwards and records. The PyPI package isn't affected: it runs on whatever Python its user
has.

On 2026-10-31, the interpreters alone, pinned to one core of the benchmark laptop. The times are relative to the
image's base; below 1 is faster:

| Interpreter | Python | JSON recording workload | Pure Python loop |
|---|---|---|---|
| The image's base, `python:3.12-slim` | 3.12.15 | 1.00 | 1.00 |
| `python:3.13-slim` | 3.13.18 | 1.01 | 1.09 |
| `python:3.14-slim` | 3.14.10 | 1.12 | 1.17 |
| **Debian trixie's `python3`** | 3.13.5 | **0.73** | **0.70** |
| python-build-standalone (as `uv` installs it) | 3.12.16 | 0.73 | 0.69 |
| python-build-standalone | 3.14.10 | 0.74 | 0.64 |
| Ubuntu 24.04's `python3.12` (the laptop's) | 3.12.3 | 0.79 | 0.78 |

Five things decide the design:

- **The cause is how the official image compiles Python.** The interpreters themselves aren't slow. Rebuilt from the
  image's own recipe, read from its `docker history`, it measures the same (1.01). Leaving settings out of that
  rebuild one at a time:
  - a statically linked interpreter instead of `libpython3.12.so` gains 8-16%;
  - removing the frame pointers gains 2-4%;
  - removing the Debian build flags the recipe appends to CPython's gains another 7-10%.

  The rest of the gap wasn't taken apart.
- **A newer official image doesn't help:** 3.13-slim measures the same, and 3.14-slim is 12-17% slower again.
- **Statically linked builds are all fast,** from three different builders. They are Debian's, python-build-standalone's
  and Ubuntu's. They differ in where their libraries come from. Debian's interpreter uses Debian's OpenSSL, SQLite,
  zlib and expat, which Debian patches and which scanners match to Debian's advisories, as they do the rest of the
  image. python-build-standalone compiles its own copies in: they're fixed only by a new release of it, and scanners
  can't see them.
- **The image is built for amd64 and arm64,** and the data plane stage is compiled on Debian trixie, so that it links
  against the same glibc as the image (ADR 0030, section 6). The official image is on trixie too. Building CPython
  in the Dockerfile would mean PGO under QEMU for arm64, which takes hours, on every release.
- **CI tests Python 3.11 to 3.14** with each one's latest release (`actions/setup-python`). The image's interpreter
  is a different build, which CI doesn't test.

## Decision

### 1. `debian:trixie-slim` and Debian's `python3`

The image's last stage is built `FROM debian:trixie-slim@sha256:...`, pinned by digest as the others are. Dependabot
proposes each new digest, as it does today. Its first step installs Debian's `python3` and `python3-venv`. It also
installs the three packages the official image adds to the same base: `ca-certificates` (TLS to databases and object
storage), `netbase` (`/etc/services`, `/etc/protocols`) and `tzdata`. Nothing is compiled, for either architecture.

### 2. The packages in a virtualenv, `/opt/venv`

Debian's Python is externally managed (PEP 668): `pip` refuses to install into the system's site-packages. The image
creates `/opt/venv` with Debian's `python3 -m venv` and puts its `bin` first on `PATH`. `pip install`, `python` (the
health check, the H2 fetch, DuckDB's `httpfs`) and `gunicorn` (the command) then work as they do now. The virtualenv
belongs to root and is only read at runtime, so the read-only root filesystem the chart sets (ADR 0013) still holds.
The `-h2` variant installs its JRE with `apt-get` as it does now.

### 3. Python 3.13, at Debian's version

The image moves from Python 3.12 to **3.13, the version Debian trixie ships**: 3.13.5 on 2026-10-31, with Debian's
security fixes backported (`3.13.5-2+deb13u5`). Upstream's later 3.13 bugfix releases don't reach it; Debian's
security updates do, with each new digest of the base. The image's Python then moves when the image moves to a new
Debian release. That release is a decision of its own, measured as this one was, because the official 3.14 build
shows a newer Python isn't faster by itself.

CI tests 3.13's latest release, not Debian's build, so the suite also runs **inside the built image** (section 5).

### 4. What a build installs

The base is pinned by digest, but `apt-get` installs the newest of each package in trixie and trixie-security on the
day the image is built. The `-h2` variant's JRE already works this way. Pinning every package's version would make
the build fail each time Debian publishes a security fix and drops the old version from its mirrors. The release's
SBOM and provenance record exactly what went in (ADR 0018), and that's where to read it.

### 5. Done when

It's Accepted when:

1. The image and its `-h2` variant are built on the new base for amd64 and arm64, and CI's image job and the release
   smoke tests pass: the data plane, the Helm chart on kind, and the rolling upgrade from 0.47.0.
2. **The suite passes inside the image**, on its interpreter and its installed packages, against SQLite and the
   PostgreSQL store, with Redis.
3. **Measured on the benchmark laptop,** `benchmarks/oltp.py`: the current image against the new one, both built
   from the same commit, interleaved on the same cores. The calls are the plain and governed lookups and a 100-row
   page, two ways:
   - Python alone, one worker, as the image runs by default;
   - the data plane in front, with Python recording.

   No scenario may be slower, and the gain is recorded. Arm64 is built and smoke-tested, not benchmarked: there's no
   arm64 hardware here.
4. The image's size and its scanners' findings are compared with the current image's. Both are recorded.
5. The changelog, `TECHNOLOGY_STACK.MD`, how-to 59's example SBOM, and `tests/test_dataplane.py`'s check of the base.

## Consequences

- **Every call Python handles in the image costs less CPU,** by up to the interpreter's 27-30% when Python is the
  whole cost. Section 5 measures how much reaches the gateway.
- **The image's Python is 3.13, Debian's build.** A deployment that imports its own code into the image, or reads
  `python --version`, sees the change. Python's bugfix releases arrive with Debian's, not upstream's.
- **Every library in the image is a Debian package,** patched by Debian and visible to scanners. Today
  that's true of everything except Python itself, which the official image compiles into `/usr/local`.
- **A build installs Python with `apt-get`,** so two builds of one commit on different days can differ in Debian's
  patch level. The SBOM records which they got.
- **Debian's Python has no frame pointers,** so `perf` sees less of it than of the official build. py-spy, which reads
  Python's own frames, isn't affected.
- **Going back is one stage:** `FROM python:3.12-slim@sha256:...` and no virtualenv, as now.

## Alternatives considered

- **Keeping the official image.** It changes nothing, and every call Python handles stays about 30% more expensive
  than it needs to be. A newer official image doesn't close the gap.
- **python-build-standalone,** installed by a pinned `uv`, which checks each download's hash. It's as fast as Debian's
  (0.73), and the image could choose its Python version, 3.12 included. But its OpenSSL, SQLite and zlib are compiled
  in. Fixing them waits for its next release, and the image's scanners and SBOM can't see them. It's the fallback if
  the image must choose its Python version.
- **Building CPython in the Dockerfile** without the official recipe's slow settings. We would choose the version and
  the flags, but each build compiles with PGO, which takes 7.5-9 minutes here on amd64 and hours under QEMU for arm64.
  Every security fix in CPython would also be ours to rebuild for.
- **Ubuntu 24.04's Python 3.12** (0.79). It's slower than Debian's, and Ubuntu 24.04's glibc is older than trixie's.
  The data plane would have to be built on Ubuntu to match.
- **Distroless or Wolfi images** (Chainguard's Python). They weren't measured. They would also change how the image
  is built and patched more than this ADR needs to.

## Open questions

- **arm64.** Debian builds both architectures the same way, so the gain should carry over. It isn't measured, for
  lack of hardware.
- **Why Debian's build is fast** isn't fully known. It uses the same `-O2` and hardening flags the official recipe
  appends. The ADR doesn't depend on it; a later Debian release is measured before the image moves to it (section 3).

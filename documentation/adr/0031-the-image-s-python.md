# ADR 0031: The image's Python: python-build-standalone, not the official image's

- **Status:** Accepted (2026-10-31): section 5's items are met ([*Result*](#result)). First proposed with Debian's
  own `python3`: measuring it found trixie's backlog of unfixed `python3.13` CVEs, so python-build-standalone was
  measured too, and chosen.
- **Date:** 2026-10-31
- **Backlog:** #104, *A faster Python interpreter in the image*. [ADR 0030](0030-shipping-the-data-plane.md#result)'s
  measurement found the problem, and the evidence is in
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
| Debian trixie's `python3` | 3.13.5 | 0.73 | 0.70 |
| python-build-standalone, as `uv` installs it | 3.12.16 | 0.73 | 0.69 |
| python-build-standalone | 3.13.18 | 0.75 | 0.68 |
| python-build-standalone | 3.14.10 | 0.74 | 0.64 |
| Ubuntu 24.04's `python3.12` (the laptop's) | 3.12.3 | 0.79 | 0.78 |

Six things decide the design:

- **The cause is how the official image compiles Python.** The interpreters themselves aren't slow. Rebuilt from the
  image's own recipe, read from its `docker history`, it measures the same (1.01). Leaving settings out of that
  rebuild one at a time:
  - a statically linked interpreter instead of `libpython3.12.so` gains 8-16%;
  - removing the frame pointers gains 2-4%;
  - removing the Debian build flags the recipe appends to CPython's gains another 7-10%.

  The rest of the gap wasn't taken apart.
- **A newer official image doesn't help:** 3.13-slim measures the same, and 3.14-slim is 12-17% slower again.
- **Every statically linked build is fast,** whoever built it. They differ in whose Python they are:
  - **Debian's** is 3.13.5 with the fixes Debian backports. On 2026-10-31, trixie had 19 CVEs open against it (5
    HIGH), which upstream's later 3.13 releases fix.
  - **python-build-standalone's** is upstream's latest release, built and published for each one.
- **OpenSSL is already compiled into the image.** `psycopg2-binary` loads its own `libssl`, and `cryptography`'s
  wheel carries its own OpenSSL, 4.0.3. Debian patches neither, and scanners that read Debian's packages see neither.
  Python's `ssl` is the one use of Debian's OpenSSL that a different Python would change.
- **The image is built for amd64 and arm64,** and the data plane stage is compiled on Debian trixie, so that it links
  against the same glibc as the image (ADR 0030, section 6). Building CPython in the Dockerfile would mean PGO under
  QEMU for arm64, which takes hours, on every release.
- **CI tests Python 3.11 to 3.14** with each one's latest release (`actions/setup-python`). The image's interpreter
  is a different build, which CI doesn't test.

## Decision

### 1. python-build-standalone, installed by a pinned `uv`

The Dockerfile gets a stage that installs Python with `uv python install 3.13`. `uv` comes in as its own stage,
`FROM ghcr.io/astral-sh/uv:<version>@sha256:...`, so Dependabot proposes its updates as it does the other base
images'. A `COPY --from=` an image directly is never updated, and a test now checks that every `COPY --from=` names a
stage.

Each `uv` release carries the list of Python releases it knows, with their hashes. It resolves `3.13` from that list,
without asking the network, to one exact release: `uv` 0.13.6 gives 3.13.18, 0.13.0 gives 3.13.16. So **the `uv`
pin is the Python pin.** Dependabot's bump of `uv` brings the next Python patch release, `uv` checks the download's
hash, and a test checks the shape. Python-build-standalone publishes both architectures, so nothing is compiled.

Only the installation is carried into the image. It leaves out what the image never runs:
- its own `pip` (the virtualenv gets one from `ensurepip`);
- Tk and Tcl with tkinter, IDLE and turtledemo;
- `libpython3.13.so`, which the statically linked interpreter doesn't load.

### 2. `debian:trixie-slim`, and a virtualenv

The image is built `FROM debian:trixie-slim@sha256:...`, the Debian release the data plane is compiled on. Its first
step installs the three packages the official image adds to the same base:
- `ca-certificates`, which Python's `ssl` reads from `/etc/ssl/certs`;
- `netbase` (`/etc/services`, `/etc/protocols`);
- `tzdata`.

The packages go in a virtualenv, `/opt/venv`, created by the installed Python and first on `PATH`. `pip install`,
`python` (the health check, the H2 fetch, DuckDB's `httpfs`) and `gunicorn` (the command) then work as they do now.
The virtualenv belongs to root and is only read at runtime, so the read-only root filesystem the chart sets
(ADR 0013) still holds. The `-h2` variant installs its JRE with `apt-get` as it does now.

### 3. Python 3.13

The image moves from Python 3.12 to 3.13: 3.13.18 with `uv` 0.13.6. Upstream supports 3.13 a year longer than 3.12
(to October 2029). In the screen, python-build-standalone's 3.13 is as fast as its 3.12, and CI already tests 3.13.
Moving to 3.14 is a change of its own, measured as this one was.

### 4. What a build installs

The Debian base and `uv` are pinned by digest, and `uv` pins Python with its hash. The three Debian packages come
from trixie and trixie-security at the patch level of the day the image is built, as the official image's did when it
was built. The `-h2` variant's JRE already works this way. The release's SBOM and provenance record what went in
(ADR 0018).

### 5. Done when

It's Accepted when:

1. The image and its `-h2` variant are built on the new base for amd64 and arm64, and CI's image job and the release
   smoke tests pass: the data plane, the Helm chart on kind, and the rolling upgrade from 0.47.0.
2. **The suite passes inside the image**, on its interpreter and its installed packages, against SQLite and the
   PostgreSQL store, with Redis.
3. **Measured on the benchmark laptop,** `benchmarks/oltp.py`: the current image against the new one, both built
   from the same application, interleaved on the same cores. The calls are the plain and governed lookups and a
   100-row page, two ways:
   - Python alone, one worker, as the image runs by default;
   - the data plane in front, with Python recording.

   No scenario may be slower, and the gain is recorded. Arm64 is built and smoke-tested, not benchmarked: there's no
   arm64 hardware here.
4. The image's size and its scanners' findings are compared with the current image's. Both are recorded.
5. The changelog, `TECHNOLOGY_STACK.MD`, how-to 59's example provenance (from a `docker.yml` rehearsal), and the tests
   of the Dockerfile.

#### Result

Measured on 2026-10-31 on the benchmark laptop: `benchmarks/oltp.py`'s new block `p1`, five rotated rounds of 30 s
per cell
([results](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/benchmarks/results/python-image-2026-10-31/README.md),
sections 4-7). The application is the same between the images: `git diff` of `queryapigate/`, `dataplane/` and
`pyproject.toml` is empty.

| Criterion | Measured |
|---|---|
| 1. CI and the release workflow | On `main` at 61cfb4f: CI's image job, with the data plane in front of Python, and the Helm chart on kind, upgraded under load from 0.47.0's. The `docker.yml` rehearsal (run 41208623399) built both images for amd64 and arm64, then signed, attested and verified them. The arm64 image's Python answered `aarch64 3.13.18 OpenSSL 3.5.9` |
| 3. Python alone, calls a second per gateway core | Debian's 3.13.5 against today's image: 1.068 on the plain lookup, 1.059 on the governed one, 1.079 on a 100-row page, p99 7% lower. python-build-standalone against Debian's: 1.009-1.014 |
| 3. The data plane in front | The same calls a second, 27,000-28,000. Past its ceiling, one Python worker recorded 42% of the runs on today's image and 66-68% on either new one, with 5% less CPU. Where every run was recorded, it needed 20% less |
| 2. The suite inside the image | 2,172 tests OK on SQLite with Redis and on the PostgreSQL store with Redis: Debian's image, the standalone one unpruned and pruned, and the 0.47.0 release as the control |
| 4. Size | 443.9 MB today; 446.9 MB with Debian's Python; 465.4 MB with python-build-standalone, pruned (514.1 MB before pruning) |
| 4. Trivy 0.69.3: Debian packages (HIGH) | 166 (44) today; 273 (70) with Debian's Python; 164 (43) with python-build-standalone |
| 4. Trivy: Python packages (HIGH) | 6 (0) today; 6 (0) with Debian's; 8 (5) with python-build-standalone, all five in the virtualenv's `pip` 26.2.2, read from the versions it vendors |
| 5. Docs and tests | The changelog, `TECHNOLOGY_STACK.MD` and the Dockerfile's tests with the change; how-to 59's commands and outputs from the rehearsal. Its SBOM, 383 packages for amd64, names `python 3.13.18` by CPE |

The interpreter's 27-30% shrinks to 6-9% at the gateway when Python serves a call itself. Much of a call's CPU isn't
the interpreter's: psycopg2, the C extensions and the kernel's networking are the same in every image. Recording
is Python's own loop, and there the difference is large.

## Consequences

- **Every call Python handles in the image costs less CPU:** 6-9% more calls a second when Python serves them, and
  60% more runs recorded by one worker past its ceiling.
- **The image's Python is 3.13, upstream's release.** A deployment that imports its own code into the image, or
  reads `python --version`, sees the change.
- **Python's security fixes arrive with `uv`'s releases,** which Dependabot proposes weekly. That's a
  python-build-standalone build and a `uv` release after CPython's own, not Debian's backports or the official image's
  rebuilds.
- **A scanner sees Python as it did before:** it isn't a Debian package in either image, so a scan of the image's
  Debian packages (Trivy's, by default) doesn't name it. The release's SBOM does, by version and CPE, in both: 3.12.15
  in 0.47.0's, 3.13.18 now. So a scanner reading the SBOM matches Python's own advisories to it. Its `ssl` now uses an
  OpenSSL compiled in, like psycopg2's and cryptography's; it's fixed by a new release of Python, not of Debian.
- **The virtualenv's `pip` shows five HIGH findings** for the versions it vendors (msgpack, urllib3, setuptools).
  QueryAPIGate doesn't import them. Removing `pip` after the install would clear them, at the cost of images that
  extend this one with `pip install`.
- **The image is 21.5 MB larger,** and its frame pointers stay: python-build-standalone keeps them, so `perf` sees
  Python as well as before.
- **Going back is two stages:** `FROM python:3.12-slim@sha256:...` and no virtualenv, as now.

## Alternatives considered

- **Debian's own `python3`** (the first proposal). It's as fast (section 5's result), and all of it is a Debian
  package: scanners see Python, and Debian patches its OpenSSL. But Debian's Python is 3.13.5 for trixie's life, with
  only the fixes Debian backports. On 2026-10-31 trixie had 19 CVEs open against `python3.13`, 5 HIGH, which
  upstream's 3.13 releases fix. One of them, `SSLContext.wrap_bio()` skipping hostname verification
  (CVE-2026-19553), was checked by behaviour:
  - Debian's 3.13.5 accepts the missing hostname silently;
  - the official 3.12.15 in today's image accepts it with only a deprecation warning;
  - upstream 3.13.18 refuses it.

  The image would scan with 26 more HIGH findings than today, every one of them real.
- **Keeping the official image.** It changes nothing, and every call Python handles stays more expensive than it
  needs to be. A newer official image doesn't close the gap.
- **Building CPython in the Dockerfile** without the official recipe's slow settings. We would choose the flags, but
  each build compiles with PGO: 7.5-9 minutes here on amd64, hours under QEMU for arm64. Every security fix in CPython
  would also be ours to rebuild for.
- **Ubuntu 24.04's Python 3.12** (0.79). It's slower than either, and Ubuntu 24.04's glibc is older than trixie's.
  The data plane would have to be built on Ubuntu to match.
- **Distroless or Wolfi images** (Chainguard's Python). They weren't measured. They would also change how the image
  is built and patched more than this ADR needs to.

## Open questions

- **arm64's speed.** `docker.yml` checks on every rehearsal and release, under QEMU, that the arm64 image's Python
  runs: the interpreter `uv` installed, its `ssl` and `sqlite3`, and the application importing. The first rehearsal
  did. Its speed isn't measured: there's no arm64 hardware here, and python-build-standalone's BOLT step is x86-64
  only.
- **Should the image keep `pip`?** It's there so an image built `FROM` this one can add a driver. Its vendored
  copies are what a scanner flags.
- **Python 3.14.** python-build-standalone's 3.14 was the fastest in the screen's loop (0.64), and isn't measured at
  the gateway.

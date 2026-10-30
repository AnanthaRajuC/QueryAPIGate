# ADR 0030: Shipping the Rust data plane in the image and the Helm chart

- **Status:** Accepted (2026-10-30), on section 8's measurement ([*Result*](#result)). Proposed and built the same
  day. Amended while building it: the crate's version is
  checked by a test, as the chart's is; Python moves with a new setting, `QUERYAPIGATE_BIND`; Python's check in the pod
  runs inside its container; the data plane serves only what its forwarding headers say plainly, and a scheme of `http`
  or `https`; and building it found gunicorn trusting a client's forwarding headers behind the data plane, fixed here
  (sections 1, 2, 4, 5 and 7, *as built*). Shipped in 0.47.0.
- **Date:** 2026-10-30
- **Backlog:** #102, *Next: shipping it*. The follow-up [ADR 0028](0028-rust-data-plane-prototype.md#result-go)
  promised: packaging, the Docker image, the Helm chart, and which of what it forwards to move. Since
  [ADR 0029](0029-clickhouse-run-store.md), shipping it also ships the run store's direct writes. It stays
  experimental: this ADR makes it deployable, not stable.

## Context

The data plane is built from the repository and started by hand (guide 64). It isn't in the Docker image, the PyPI
package or the Helm chart. So nobody deploying from the image or the chart can use what 0.44 and 0.45 measured:
3.2-9.4x the calls a second per gateway core, and every run recorded at 20,000 a second.

Five things in how it's deployed today decide the design:

- **It's two processes,** the data plane on the public port and gunicorn behind it on an inner one, sharing a secret.
  Both read the same store, and the protocol between them (plans, reported runs, the run store's lease) is internal:
  it isn't versioned, and nothing checks that the two come from one release.
- **The chart sets `QUERYAPIGATE_TRUST_PROXY=1`** whenever its ingress is on (`_helpers.tpl`). With a hop count set,
  the data plane forwards every call (ADR 0028, section 4). Shipped as it is, it would serve nothing behind the
  chart's own ingress, and the same goes for `deploy/scale-out`, behind Caddy.
- **ADR 0028's go needs two Python workers behind the data plane**, because one process records about 17,000 reported
  runs a second. The image runs one worker on purpose: `/metrics` is kept in each process's memory
  (`metrics.py`), so with two, a scrape sees one worker's numbers at random. ADR 0029 removed the cause when the run
  store is on: the data plane writes its own runs, and one worker kept up at 20,000 a second.
- **The image is built for amd64 and arm64,** with QEMU for the other architecture. That's fine for `pip install`,
  but compiling sqlx, ring and Tokio under emulation would take most of an hour.
- **The binary is small and plain:** 10.7 MB stripped, linked against glibc, libm and libgcc only (measured
  2026-10-30, the 0.46 build). Its cryptography is ring and RustCrypto, not a FIPS-validated module.

## Decision

### 1. One image, both binaries

The data plane goes into the image QueryAPIGate already publishes, and into its `-h2` variant, at
`/usr/local/bin/queryapigate-dataplane`. There's no second image. The default command doesn't change: a container
started as today runs gunicorn alone, and the binary is never run unless it's asked for.

One image means the two processes of a deployment always come from one release, and are signed, attested and
scanned together (ADR 0018). As a check for anyone who pairs them otherwise, the crate's version becomes the release's
(`dataplane/Cargo.toml`, checked by `check_release.py` with the other version strings). The data plane sends it with
the secret, and Python gives no plan and no lease to a different version, logging why once. A mismatched pair then
forwards every call, which is slower but answers correctly. *As built:* the version is checked by a test
(`tests/test_dataplane.py`, with `Cargo.lock`'s), as the chart's is by `tests/test_helm.py`; it goes in
`X-QAG-Dataplane-Version`, and the lease's refusal is a 409.

It isn't added to the PyPI package. A wheel per platform with a Rust binary in it is a different build, for a smaller
audience than the image's. Guide 64's build from the repository keeps working.

### 2. Two containers, no supervisor

The two processes run as two containers from that image, sharing one network namespace: the two containers of a
Kubernetes pod, or two Compose services with `network_mode: service:<name>`. Python binds `127.0.0.1:5001`, and the
data plane binds `0.0.0.0:5000`.

- **Each process is its own container:** its own restarts, logs, resource limits and probes. A crash in one is
  visible as that container's, and the orchestrator restarts it.
- **The image's health check keeps working unchanged.** In the shared namespace, `127.0.0.1:5000` is the data plane
  from either container, and it forwards `/health` to Python, so the check passes only when both are up.
- **Python's port is reachable from the pod only,** on loopback. Nothing outside it can skip the data plane.

*As built:* the image's command had `--bind 0.0.0.0:5000`, which would beat anything else, so the address became a
setting: `QUERYAPIGATE_BIND`, which the image sets to `0.0.0.0:5000` and `gunicorn_conf.py` reads only when it's set.
`QUERYAPIGATE_HOST` and `QUERYAPIGATE_PORT` were not reused: they're `queryapigate serve`'s, and its default host is
loopback, so an environment that already sets one could have moved a deployment's gunicorn without anyone asking.

There's no single-container mode. A supervisor in the image (tini, s6, or a Python one) would be new code in the
path of every call, would hide one process's crash behind the other's health, and would share one resource limit
between two processes that need sizing apart. If people running a single `docker run` ask for it, it's a small
addition later. Until then, the guide runs two `docker run`s, the second with `--network container:<first>`.

### 3. One Python worker per pod

The shipped form keeps the image's one worker, so `/metrics` stays correct. The chart already scales by adding pods,
not workers. For the shipped form, this replaces ADR 0028's two-worker condition with a limit on each pod:

- **With the run store** (`QUERYAPIGATE_RUN_STORE_URL`), there's no ceiling from recording: the data plane writes its
  runs, and one worker kept up at 20,000 a second (ADR 0029, criterion 2).
- **Without it,** a pod should stay below one worker's 17,000 recorded runs a second. At about 7,000 calls a second
  per data plane core (2.9 cores at 20,000 a second, ADR 0029), that's a data plane limited to about 2 CPUs per pod.
  The chart's docs say so. A pod that goes past it counts what it can't record in
  `queryapigate_history_runs_total{outcome="dropped"}`, as it does today. *Measured (section 8):* held to 2 CPUs, the
  image's data plane served about 8,900 calls a second per CPU, and one Python worker recorded every run, 9.8 million
  over 10 minutes, with half a core.

Shared metrics across workers (Prometheus' multiprocess mode, or the data plane aggregating) would lift the per-pod
limit. That's a separate change, only worth making if one-worker pods turn out too small.

### 4. The Helm chart

There's a new block of values, off by default:

```yaml
dataplane:
  enabled: false
  existingSecret: ""   # the shared secret, under `key`; otherwise the chart makes one, kept across upgrades
  key: secret
  poolSize: 16         # QUERYAPIGATE_DATAPLANE_POOL_SIZE: connections per database, per pod
  resources:
    requests: {cpu: 250m, memory: 64Mi}
    limits: {memory: 256Mi}
```

With `enabled: true`:

- **The API pods get a second container,** `dataplane`, from the same image and digest, running
  `queryapigate-dataplane`. The Service and the ingress point at its port. The Python container binds
  `127.0.0.1:5001`. The `events` and `mcp` deployments don't change.
- **The settings are the same for both containers**, from the same Secret and `config`, so both read the same store,
  keys and masking key. With the SQLite store, both mount the same volume. They're in one pod on one node, so
  SQLite's locking holds.
- **The secret** is made in a Secret of its own (`<release>-dataplane`) and kept across upgrades, as `metricsAuth`'s
  token is. `existingSecret` is for rendering without the cluster (`helm template`, Argo CD), which can't see the one
  already made.
- **Probes:** the data plane's readiness is `/health` through it, which proves both processes. Its liveness is a TCP
  check, so a slow Python doesn't restart it. Python's probes stay as they are, on its own port.
- **Sizing:** `QUERYAPIGATE_DATAPLANE_THREADS` defaults to the CPUs the container may use: Rust's
  `available_parallelism` reads the cgroup's quota. Each pod opens up to `poolSize` more connections to each database
  it serves from, on top of Python's pool. The chart's notes say how to count them.
- **A rolling upgrade replaces both containers together,** from one image. A new pod starts with no plans, so each
  query's and key's first call goes to Python, as on any restart.

*As built:* there's no `poolSize` value: `config` reaches both containers, so `QUERYAPIGATE_DATAPLANE_POOL_SIZE` and
`QUERYAPIGATE_DATAPLANE_THREADS` are set there. The kubelet probes the pod's address, not its loopback, so Python's
check is an exec probe inside its container (liveness, every 30 s), and its readiness is the data plane's, which goes
through to it. The chart refuses `QUERYAPIGATE_BIND` and the data plane's listen and upstream addresses in `config`,
and its secret in `config` or `secret.values`.

`deploy/scale-out` gets an opt-in override file, `docker-compose.dataplane.yml`, that adds a data plane service in
front of each instance. Guide 64 is rewritten around the image and the chart, with the build from the repository
kept as its last section.

### 5. What moves out of the forwarded list: the trusted-proxy hop count

Section 4 of ADR 0028 lists why the data plane forwards a call. This ADR moves one row:
**`QUERYAPIGATE_TRUST_PROXY` above 0.** It's the only one the chart and the scale-out example make unavoidable.

For a call it serves, the data plane takes the client's address and scheme as `ProxyFix(x_for=hops, x_proto=hops,
x_host=hops)` does. It uses the hops-th value from the right of `X-Forwarded-For`, and of `X-Forwarded-Proto`, and the
socket address when there are fewer values than hops. That address is what a key's `allowed_ips` is checked against,
what the log line and the run record name, and the scheme is what decides HSTS (`secheaders.py`). A forwarded call
doesn't change: the data plane still sends its socket peer in `X-QAG-Peer`, the forwarding headers go through
untouched, and Python applies `ProxyFix` itself.

It's an access decision now made in two places, so it gets its own differential cases against `ProxyFix`, which must
agree exactly:
- one hop and several;
- fewer values than hops;
- several `X-Forwarded-For` headers, and spaces around values;
- IPv6, with and without brackets;
- ports, garbage, and an empty value.

Everything else in ADR 0028's section 4 stays forwarded. Rate-limited keys with Redis come next, as their own change
with their own differential tests. Then other formats and workspaces.

*As built:* rather than reproduce every reading `parse_list_header` and werkzeug make, the data plane serves only
headers that read one way, and forwards the rest: one line of each, no quotes, no empty item; an address in its own
spelling (`2001:db8::5`, not `2001:DB8::5`), not an IPv4 address written as IPv6; and a scheme of `http` or `https`.
The proxied differential run found the reason for the last: werkzeug answers `wss` on a request that isn't a
WebSocket's with 400. The host is checked too, though the data plane uses none of it: werkzeug answers 400 to one it
can't encode as IDNA (`a..b`, a label over 63 characters), from `Host` - which was so before this ADR, and is fixed with
it - or behind proxies from `X-Forwarded-Host`. A hop count or HSTS value Python's `int()` would read but Rust wouldn't (`1_0`, another script's
digits) makes it forward every call. The differential test's proxied run - two hops, HSTS on, every shape of both
headers against IP-restricted keys, IPv6 ones included - found no difference in 9,000 calls, over three seeds.

### 6. The build

- **The Dockerfile gets a stage that compiles on the build machine's architecture and cross-compiles to the target's**
  (`FROM --platform=$BUILDPLATFORM`), with the C cross-compiler ring needs. It doesn't compile under QEMU. The Rust
  image is pinned by digest, as the others are (ADR 0018), on the same Debian release as the Python base. A test
  checks that its toolchain is the one `dataplane/rust-toolchain.toml` names.
- **The build uses `cargo auditable build --release --locked`,** then strips the binary. `cargo auditable` puts the
  crate list in the binary, so the image's SBOM and scanners such as Trivy and Grype see its dependencies, not an
  opaque file.
- **CI's `dataplane` job adds `cargo audit`** (RustSec), failing on an advisory with no recorded exception.
  `block-buffer` 0.10.4 (GHSA-qwgh-2vcv-g2f7) starts as one, with the reason it's waiting. *As built:* RustSec has no
  advisory for it (it's GitHub's alone), so no exception was needed: `cargo audit` 0.22.2 found none in 255 crates.
- **The image's smoke test** (`docker.yml`) also starts the image as two containers in one namespace, calls a saved
  query twice with a scoped key, and checks that the data plane served the second call (its log line). It runs for
  both architectures' builds of the binary.
- **The Helm job on kind** installs with `dataplane.enabled`, behind the ingress, and makes the same check. That's
  the case section 5 exists for.

*As built:* the cross-compiled build takes about 2 minutes for either architecture on the laptop. The smoke test is
`.github/scripts/smoke_dataplane.sh`, run by CI's image job on every pull request and by `docker.yml` on a release; it
calls until the data plane serves, since Python records a key's use within a history flush and forwards its calls
until then. The arm64 binary is started under QEMU from the pushed image, on a release: it can't serve a call there in
reasonable time. The kind job has no ingress controller, so it sets `ingress.enabled` (and with it the hop count) and
sends `X-Forwarded-For` as the controller would. The scale-out example was also run once on the laptop, behind Caddy:
three data planes served 26 of 30 calls, and a client's own `X-Forwarded-For` didn't move an IP-restricted key's
verdict.

### 7. Security

- **The image contains a binary that runs only when asked.** An image that's never started with it has the same
  attack surface as before. It's scanned and listed in the SBOM either way.
- **The shared secret** sits in a Secret, never in `config`, and the chart refuses it there as it refuses the other
  secrets. Python's port is on loopback in the pod (section 2).
- **The client address behind a proxy is an access decision** (a key's `allowed_ips`) made in Rust as well as
  Python, which is why section 5 has its own differential cases. THREAT_MODEL's data plane entry says so.
- **Version pairing** is checked, not assumed (section 1).
- *Found building it:* **behind the data plane, gunicorn trusted every client's forwarding headers.** Every request
  reaches gunicorn over loopback, which gunicorn trusts by default to set the scheme (`X-Forwarded-Proto`, `-Ssl`,
  `-Protocol`) and `SCRIPT_NAME` from headers, and the data plane passed a client's own on. A client could move part
  of a path out of what Flask routes, past a proxy's rules for that path, turn a call into a 400 with contradictory
  scheme headers, or have HSTS sent over plain HTTP. With the data plane's secret set, `gunicorn_conf.py` trusts no
  peer's (`forwarded_allow_ips` empty, unless `FORWARDED_ALLOW_IPS` says otherwise), and Python reads them through
  `ProxyFix` only, as it does without the data plane.
- **FIPS:** the data plane's cryptography isn't a validated module. A deployment that needs FIPS 140 leaves it off,
  and the FIPS documentation says so. A FIPS build (rustls on aws-lc-rs's FIPS module) is possible later, if asked.

### 8. Done when

It stays experimental (`queryapigate/experimental.py`). This ADR is done, and it's Accepted, when (*as of
2026-10-30:* all five - 1 and 2 by CI on #127, its image job and the chart on kind, and a `docker.yml` rehearsal):

1. The image and its `-h2` variant contain the binary, built for amd64 and arm64 by cross-compiling, with
   `cargo auditable`. The smoke test is green for both.
2. The chart's `dataplane` block works behind the ingress on kind, with the served call checked, and `helm template`
   works with `existingSecret`.
3. The trusted-proxy cases pass the differential test against `ProxyFix`, in CI against real PostgreSQL and MySQL.
4. **Measured, on Performance v5's laptop:** the image against the from-source build, both on the same 8 cores, shows
   no regression from running in containers. One pod with the data plane limited to 2 CPUs and no run store
   records every run at full load, which checks section 3's limit. Arm64 is built and smoke-tested, not
   benchmarked.
5. The version check, `cargo audit`, guide 64 rewritten, the chart's README and notes, THREAT_MODEL, and the
   changelog.

To graduate later, on the terms in `experimental.GRADUATION`, it needs a release unchanged and a security review of
the shipped form. Moving more of ADR 0028's forwarded rows isn't a condition.

#### Result

Measured on 2026-10-30, on the laptop and in the setup of the earlier data plane measurements, `benchmarks/oltp.py`
blocks s1 and s2 ([results](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/benchmarks/results/dataplane-image-2026-10-30/README.md)):

| Criterion 4 | Measured |
|---|---|
| The image against the from-source build, same commit, same 8 cores: calls a second per gateway core | 0.98-0.99x on the plain and governed lookups, 0.95x on a 100-row page; the data plane's own CPU and p99 the same |
| One pod, the data plane held to 2 CPUs, no run store, 64 callers | 16,400-17,000 calls a second, every run recorded: 9,842,067 over a 10-minute run |

The data plane runs as fast in the image as from source. What differs is the Python behind it, and the measurement
traced it past containers: with the image's exact packages on the from-source side the numbers were the same, and a
Python workload alone, pinned to one core, ran 29-34% slower with the image's interpreter, seccomp on or off. The
official `python:3.12-slim` image links Python against a shared `libpython`, where the laptop's is linked statically.
That's how the image has always run, with or without the data plane; a faster interpreter in the image is BACKLOG
#104, not a condition of this ADR.

## Consequences

- **The image grows by about 11 MB,** and its build by a Rust stage. A cached build compiles only when `dataplane/`
  changes.
- **Deploying it is one value in the chart,** and the speed-up measured in 0.44 is available to anyone on the image.
  What it serves still depends on traffic: the first call per query and key, writes, and every other forwarded case
  go through Python and pay one extra hop (+0.35 ms at p50).
- **Pods behind the chart's ingress are served,** because the trusted-proxy rule moves into the data plane.
- **A pod without the run store has a recording ceiling** of about 17,000 runs a second. The chart's docs size the
  data plane below it, and runs past it are counted as dropped, not lost silently.
- **More database connections per pod:** the data plane's pool comes on top of Python's.
- **FIPS deployments can't use it.**
- **Turning it off is one value:** the Service points at Python again, and nothing is stored on the data plane's
  side.

## Alternatives considered

- **A separate image for the data plane.** It would be smaller to pull for the data plane alone, but it's two
  versions to pair, sign and scan, for 11 MB saved. One image makes a mismatched pair the exception that section 1
  checks for, not the default.
- **A supervisor in one container** (tini, s6-overlay, or `queryapigate serve --with-dataplane`). It's the friendliest
  for a single `docker run`, at the costs in section 2. It can be added later without changing the two-container
  form.
- **Two workers per pod, with shared metrics.** It keeps ADR 0028's condition as it was, but needs `/metrics` across
  processes first, a change of its own. One worker with the run store records everything, and pods scale out.
- **Compiling arm64 under QEMU.** It's simpler, but slow enough to matter on every release. Prebuilt binaries
  copied in from a native arm64 runner would also work, but the image would then be built from two jobs' outputs
  instead of one Dockerfile.
- **A static musl build.** It wouldn't depend on the base image's glibc, but musl's allocator is markedly slower
  under Tokio's many threads, and this binary exists to be fast. Building on the same Debian release as the base
  avoids the glibc question.
- **Shipping without moving the trusted-proxy row.** It's less work, but the chart's own ingress would turn the data
  plane into a hop that serves nothing.
- **A wheel with the binary in it** (maturin). It reaches `pip install` users, at the cost of a wheel per platform.
  It can be reconsidered if they ask.

## Open questions

- **Plans before the first call** (ADR 0028's open question). Should Python compute plans when a query is published
  or a key changes? Still open. On a restart or a new pod, every query's and key's first call goes to Python.
- **Should the chart suggest the metadata cache when the data plane is on?** Much of the data plane's speed comes from
  it (3.2x without, 4.0-9.4x with). With several replicas, it delays revocation by its TTL. The proposal leaves it
  off, as now, and the docs show the trade.
- **Release binaries** (Linux, amd64 and arm64, with provenance) for people who don't use the image. Not proposed
  until someone asks.

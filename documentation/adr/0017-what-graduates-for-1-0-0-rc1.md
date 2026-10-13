# ADR 0017: What graduates for 1.0.0-rc1

- **Status:** Accepted
- **Date:** 2026-10-13
- **Amended 2026-10-13:** the graduations below ship in **0.30**, not 1.0.0-rc1. rc1 comes later, after more
  improvements; what this ADR decides is unchanged - only the release they land in.
- **Backlog:** #64 (what is experimental), and each feature's own entry below.

## Context

1.0.0-rc1 freezes the compatibility promise in CHANGELOG.md's "Versioning and compatibility": from then on, a
stable feature changes only by adding to it, and removes or renames only through the deprecation process. What is
still in `queryapigate/experimental.py` at rc1 stays outside that promise through 1.x until it graduates.

A feature graduates when it meets every one of `experimental.GRADUATION`:

1. **Real CI:** tested against the real database, store or provider it works with, not only in opt-in tests.
2. **Security review:** if it guards access or data, it has had one.
3. **Settled shape:** its shape hasn't changed for a release, and no change is planned.
4. **Gaps:** closed, or written down as limits where its documentation describes it.
5. **Floors:** the oldest dependency versions pyproject.toml allows are the ones CI tests.
6. **What may be added:** its documentation says what may still be added (fields, kinds, options).

The registry has twelve entries. A thirteenth feature, the Helm chart, is called experimental by the 0.26 changelog,
the chart's documentation and the threat model, but has no entry. That is because it has no setting or operation
for the registry to mark.

### What was checked

This is the evidence for each feature as of 0.29.0:

- **Real CI:** the `integration` job runs PostgreSQL 16, MySQL 8, ClickHouse 24.8 and H2. With none allowed to
  skip, it also runs SeaweedFS (S3), Keycloak 26, Vault 1.21 and moto, an AWS stand-in. `postgres-metadata` runs the
  whole suite on a PostgreSQL store with Redis 7. `helm-kind` installs the chart on kind and upgrades it under load.
- **Settled shape:** I read every release's changelog entry from 0.18 to 0.29 for each feature. Since the release
  that added it, every feature has changed only by addition. For example, 0.29 made `history forget` delete usage
  counts, and 0.27 and 0.28 added the chart's `configAsCode` and `vaultToken` values.
- **Floors:** the `oldest-dependencies` job tests DuckDB 1.2.0 and sqlglot 25.2.0, and nothing else. The
  `fips` job happens to run `cryptography` 41.0.7, which is the floor. Untested floors include:
  - the core dependencies: `Flask>=2.2`, `PyYAML>=6.0`
  - `mcp>=1.10`
  - `PyJWT[crypto]>=2.14`
  - `redis>=5`
  - `opentelemetry-sdk>=1.27`
  - `boto3>=1.28`
  - `pymongo>=4.0`
- **What may be added:** none of the twelve documentation sections says this today.
- **Security reviews:** each release from 0.16 had one before it shipped, and the threat model has a section for
  every feature that guards access. But no document records which review covered which feature.

## Decision

### Eight graduate in 1.0.0-rc1

Each graduates once the shared work in the next section and its own row's work are done.

| Feature | Since | Real CI | Shape | What's left |
|---|---|---|---|---|
| Descriptions | 0.18 | the whole suite, on SQLite and PostgreSQL stores; floors in `oldest-dependencies` | unchanged since 0.18 | docs: limits (imported only from PostgreSQL, MySQL and ClickHouse comments) and what may be added (fields per column) |
| Agent usage | 0.18 | the same | unchanged since 0.18 | docs: what is summarised, what may be added (fields per agent) |
| OAuth for the MCP server | 0.18 | Keycloak 26 | unchanged since 0.18 | floors: `mcp` 1.10, `PyJWT` 2.14; review; docs: what may be added (scopes, client registration as the MCP authorization spec grows) |
| Console sign-in through SSO | 0.19 | Keycloak 26 | unchanged since 0.19 | floor: `PyJWT` 2.14; review; docs: what may be added (claims, settings) |
| Write tools over MCP | 0.21 | the whole suite, through the MCP SDK | unchanged since 0.21 | floor: `mcp` 1.10; review; docs: what may be added (fields on a write request) |
| Usage quotas | 0.24 | Redis 7, in `postgres-metadata` | unchanged since 0.24 | floor: `redis` 5; review; docs: say exactly what counts as a call (a cached response, a stream, an export); give the measured overshoot across instances (1,016 answered for 1,000) as a limit; name `bytes` and other periods as possible additions |
| Configuration as code | 0.27 | the PostgreSQL store, through the API's own endpoints; the CLI's `--server` only with its HTTP mocked, so a test against a server started in CI is added | unchanged since 0.27; the file says `format_version: 1` | floor: `PyYAML` 6.0; review; docs: give the kinds it doesn't cover (administrators, admin roles, alerts) as limits, with new kinds as possible additions; a new `format_version` is how the format would ever change |
| The Helm chart | 0.26 | kind, Helm 3 and 4, upgraded under load | values only added since 0.26 | review; docs: possible additions are values; a value is renamed only through the deprecation process; remove "experimental" from the chart's README, `Chart.yaml` and the threat model |

### Five stay experimental through 1.0

| Feature | Criterion it fails | What would graduate it |
|---|---|---|
| Live events | 3: the dedicated event log (#59) is open, and its payload is meant to gain `type` and `source` when the log ships (ADR 0002 §5) | ship #59. The event-id rule is already outside the experiment (ADR 0002 §2) |
| H2, JDBC and MongoDB connections | `databases.GRADUATION`: `allowed_tables` doesn't work on any of the three; MongoDB has no CI test; generic JDBC has no time limit | per type: `allowed_tables`, plus a MongoDB integration test. Each can graduate separately |
| The audit log streamed to a SIEM | 1: CI tests the stream against a TLS receiver inside the test, not a real one | a real syslog receiver (rsyslog or Vector) and an OpenTelemetry Collector in the integration job, plus the `opentelemetry` floor |
| Tracing with OpenTelemetry | 3: OpenTelemetry's conventions for generative AI are still in development, and the agent spans follow them | the conventions settle. What never goes into a span is already a promise, and stays one |
| Secrets from files and secrets managers | 1: AWS is tested against moto, not AWS, and RDS IAM against neither. 3: it shipped in 0.28, one release ago | a test against real AWS (Secrets Manager and an RDS instance) that a maintainer can run, and the `boto3` floor. File and Vault references could graduate before AWS if needed |

### Work every graduate shares

1. **Floors in CI.** The `oldest-dependencies` job installs these pins and runs the graduates' tests:
   - `Flask==2.2.*`
   - `PyYAML==6.0`
   - `mcp==1.10.*`
   - `PyJWT[crypto]==2.14.*`
   - `redis==5.0.*`

   `tests/test_dependency_floors.py` checks each pin against pyproject.toml. A floor that doesn't pass is raised
   instead, in rc1's changelog under *Upgrading*. This is the last release before 1.0 where raising a floor costs
   nothing.
2. **One security review per graduate that guards access or data.** That means OAuth for MCP, SSO, write tools,
   quotas, configuration as code and the chart. Descriptions and agent usage don't guard access or data. Each
   review is recorded in the threat model's section for its feature: *Reviewed for 1.0.0-rc1*, with what was looked
   at and what was found.
3. **Documentation.** In each graduate's section, the experimental callout is replaced by two notes:
   - *Stable since 1.0.0-rc1*, and what may still be added
   - its limits

   The test that checks each experimental section says so is joined by one that checks each stable section has
   both notes.
4. **The registry.** The eight entries leave `experimental.py`. A 'helm_chart' entry isn't added, because it
   would graduate in the same release; instead the chart's three documents stop calling it experimental. The
   startup warning, the OpenAPI `x-experimental` flags and Settings' marks follow the registry on their own.

## Consequences

- **1.0.0-rc1's promise covers what people build on most:**
  - signing in, for people and for agents
  - agents' writes
  - quotas in contracts
  - configuration in Git
  - the chart
- **Five features stay outside it, each with a written reason and a way to graduate.** None blocks rc1, and each
  graduates in a 1.x minor release.
- **Raising a floor is a breaking change after 1.0,** so rc1 is the last chance to raise one for free.
- **Graduating quotas commits to a definition of a call.** A cached response, a stream and an export each count the
  way the documentation says, and only an addition can change that.

## Delivery

1. **Floors:** the CI pins and `test_dependency_floors.py`, with any floor raised that doesn't pass.
2. **Descriptions, agent usage, quotas and configuration as code:** their documentation and reviews, and their
   registry entries removed.
3. **OAuth for MCP, SSO, write tools and the chart:** the same.
4. **Release prep for 1.0.0-rc1:** the changelog's list of what is stable and what is still experimental, and the
   threat model's review notes.

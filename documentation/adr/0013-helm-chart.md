# ADR 0013: A Helm chart

- **Status:** Accepted
- **Date:** 2026-10-12
- **Backlog:** #100 (a Helm chart).

## Context

`deploy/kubernetes/queryapigate.yaml` runs several instances on one PostgreSQL store and Redis, plus the events
server, behind an ingress. It's correct - rolling updates replace one pod at a time, a `preStop` pause drains
requests, a disruption budget keeps two up - but it's plain YAML: a team changes the image tag, the host, the
resources and the secret's name by editing it, and keeps their edits through every upgrade by hand. Most teams on
Kubernetes install through Helm: `helm install`, values for what differs, `helm upgrade` for a new release.

The manifests also leave things out: the MCP server, a small single-instance install on SQLite, a security context,
metrics scraping. And nothing tests them in CI.

## Decision

### 1. One chart, in the repository, released with the server

- **Where:** `deploy/helm/queryapigate`, `apiVersion: v2`, working with Helm 3 and Helm 4.
- **Released in lockstep:** the chart's `version` and `appVersion` are the release's version. The release workflow
  pushes it to ghcr as an OCI artifact beside the image:

  ```bash
  helm install queryapigate oci://ghcr.io/anantharajuc/charts/queryapigate --version 0.26.0 -f values.yaml
  ```

  One version number means "chart 0.26.0 runs QueryAPIGate 0.26.0". Any chart-only fix waits for the next release,
  or makes a patch release.
- **The plain manifests stay,** for anyone not on Helm. A test keeps them in step with what the chart renders for the
  same setup.

### 2. Two shapes, chosen by `store`

- **`store.type: sqlite`**, the default: one replica, `queryapigate.db` on a PersistentVolumeClaim, and the
  `Recreate` strategy, since two pods can't share the file. It's for trying QueryAPIGate or a small internal
  deployment.
- **`store.type: postgres`**: `replicas` instances on the PostgreSQL store, with `QUERYAPIGATE_DATABASE_URL` taken
  from a Secret. Rolling updates replace one pod at a time, and a disruption budget keeps a quorum up.
- **What the chart refuses** (template errors, so a bad install fails at `helm install` rather than at runtime):
  - more than one replica on SQLite;
  - more than one replica without `redis.url`, since rate limits, `max_concurrent` and quotas would each be counted
    per pod;
  - Postgres without a database URL.

### 3. Secrets: yours, or the chart's

- **`existingSecret: queryapigate`** (recommended) names a Secret you manage - with sealed-secrets, External Secrets,
  Vault's injector, or by hand. The chart mounts it as environment variables and never reads it.
- **Or `secret.create: true`** with the values in `secret.values` (`QUERYAPIGATE_API_KEY`,
  `QUERYAPIGATE_SECRET_KEY`, ...), for trying it out. The values file then holds secrets, and the chart's notes say
  so.

### 4. What else the values cover

- **`config`:** every other `QUERYAPIGATE_*` (and `OTEL_*`) setting as a map, set as environment variables. The
  chart doesn't re-model the server's settings - Settings in the Console lists them. The settings that are secrets
  (the keys, the database and Redis URLs, the SSO client secret, OTLP headers) are refused there: `config` is plain
  text in the Deployment, where `get deployments` reads it.
- **`extraVolumes`, `extraVolumeMounts`:** files the settings name by path - the SIEM's CA and client certificate,
  a directory for exports - in every pod, since the root filesystem is read-only.
- **`ingress`:** class, host, TLS, annotations, with `/events` routed to the events server when it's on. It ships
  with ingress-nginx's no-buffering annotation, which other controllers can replace.
- **`events.enabled`, `mcp.enabled`:** each its own Deployment and Service, with its own replicas.
- **Production defaults that are on:**
  - a pod security context: non-root, `readOnlyRootFilesystem`, all capabilities dropped, `RuntimeDefault` seccomp,
    with an `emptyDir` for `/tmp`;
  - resources requests and limits;
  - the readiness, liveness and `preStop` behaviour the plain manifests already have;
  - `enableServiceLinks: false`;
  - a disruption budget when there's more than one replica.
- **Optional:** a `ServiceMonitor` for the Prometheus Operator (`/metrics`), a `NetworkPolicy`, a
  `HorizontalPodAutoscaler` (Postgres and Redis only), and the usual node selector, tolerations, affinity, topology
  spread and service account.
- **`values.schema.json`** checks the values, so a typo (`replica: 3`) fails `helm install` rather than being
  ignored.

### 5. Tested in CI, on a real cluster

- **On every change:**
  - `helm lint` with Helm 3 and 4;
  - the chart rendered with each shape and each optional part, and the output checked - every Deployment's security
    context, the routes, the refusals;
  - the plain manifests compared with the chart's rendering.
- **On a kind cluster:** install the Postgres shape (PostgreSQL and Redis from the existing dev-dependencies), wait
  for the pods, run the smoke test through the Service, then `helm upgrade` from the previous release's chart while
  requests keep running - none may fail. The SQLite shape is installed too, and data is checked to survive a pod
  restart.

### 6. Stability

The chart is **experimental in 0.26.** Value names may change while real installs try them. After that, its values
come under the same promise as environment variables: renamed only through deprecation.

## Consequences

- **New:** `deploy/helm/queryapigate`, published to `oci://ghcr.io/anantharajuc/charts/queryapigate` each release; CI's
  Helm job; a guide; DEPLOYMENT.md's Helm section.
- **The image changes in one place.** Run with a read-only root filesystem (checked with 0.25.0's image), it writes
  only to `/data` (the store) and `/tmp` - except gunicorn 26's control socket, under `/home/app/.gunicorn`, which
  QueryAPIGate doesn't use. The image starts gunicorn with `--no-control-socket`.
- **Not in scope:**
  - Bundling PostgreSQL or Redis as subcharts - production uses managed ones, and a dependency on another chart's
    values ties this one to its releases. The guide uses the throwaway ones in `dev-dependencies.yaml`.
  - An operator.
  - Multi-cluster setups.
- **Upgrading the store's schema** stays what it is: each instance upgrades on start under the store's lock, and a
  rolling upgrade works within the version promise (DEPLOYMENT.md). The chart adds no migration job.

## Delivery

About a week, in three slices:

1. The chart: both shapes, secrets, config, ingress, events, MCP, the security context and the optional parts, the
   values schema and the refusals. Tests that render it and check the output, and the plain manifests kept in step.
2. CI: lint with Helm 3 and 4, a kind cluster installing both shapes, and an upgrade with requests running. The
   release workflow pushes the chart to ghcr.
3. A guide that installs it on a real cluster (kind, here) from the OCI registry, upgrades it, and turns the optional
   parts on. DEPLOYMENT.md, the changelog and 0.26.0 prep.

# ADR 0014: Configuration as code

- **Status:** Accepted
- **Date:** 2026-10-12
- **Backlog:** #97 (configuration as code: export, diff and apply).

## Context

What a QueryAPIGate server does is decided by what's in its store: connections, saved queries and their
collections, roles, API keys' grants, masking rules, descriptions for agents, destinations and scheduled exports.
All of it is made through the Console or `/api/v1`, one change at a time.

That works for one server. It doesn't work for the way most teams run things:

- **Several environments.** Moving dev's setup to staging and then to prod is done by hand, or by scripts against
  `/api/v1`. Collection bundles (`queryapigate collection export|import`) move saved queries, but not the
  connections, roles, keys and exports around them.
- **Review before a change.** A new key's grants, or a widened role, should be a pull request someone approves, not a
  click nobody else saw. The audit log records the click afterwards; it doesn't stop it.
- **GitOps.** Since 0.26, teams install QueryAPIGate with Helm, often through Argo CD or Flux. Those tools keep the
  cluster matching git. Today they can do that for the deployment, not for what it serves.
- **Rebuilding a server.** Restoring the store from a backup brings it back. There is no description of what the
  server *should* hold to compare against.

What's already true, and shapes the design:

- **Connection values already take `${VAR}` references**, expanded from the server's environment each time the
  connection is used (`store._expand_env`). The server's own settings can't be referenced. So credentials can stay
  out of the store today, and out of a file.
- **An API key's secret is stored only as its SHA-256 hash** (`apikeys._hash`). A hash isn't a secret: from a
  generated key (`sk_` and 32 random bytes), it can't be reversed.
- **Every write goes through one validation path per kind** (`definitions.validate_definition()` for queries,
  `apikeys.create_key()` and `update_key()` for keys, and so on), and is audited.
- **`db.transaction()` holds the store's write lock** (`BEGIN IMMEDIATE` on SQLite, an advisory lock on PostgreSQL),
  but transactions don't nest: each kind's writes open their own.

## Decision

### 1. One file describes what the server holds

A YAML (or JSON) file, `format: queryapigate-config`, `format_version: 1`, with one list per kind. Every object is
identified by its name.

```yaml
format: queryapigate-config
format_version: 1
connections:
  - name: warehouse
    db: postgres
    host: ${WAREHOUSE_HOST}
    port: 5432
    user: reporting_ro
    password: ${WAREHOUSE_PASSWORD}
    database: analytics
queries:
  - name: monthly_revenue
    collection: finance
    connection_name: warehouse
    description: Revenue per month
    sql: SELECT month, revenue FROM revenue_by_month WHERE month >= :since
    parameters: {since: {type: string, pattern: "^[0-9]{4}-[0-9]{2}$"}}
    cache_ttl: 300
roles:
  - name: partner
    collections: [finance]
    rate_limit: 60/minute
    quota: {calls: 10000, per: month}
    mask: [{column: customers.email, action: hash}]
api_keys:
  - name: partner-a
    role: partner                     # the role's grants, kept in step with it
    secret_sha256: 50d858e0985ecc7f60418aaf0cc5ab587f42c2570a884095a9e8ccacd0f6545c   # the hash, never the secret
descriptions:
  warehouse:                          # by connection, as /api/v1/connections/<name>/descriptions takes them
    - {table: revenue_by_month, description: "Revenue per calendar month, net of refunds"}
destinations:
  - name: partner-bucket
    url: s3://partner-a-drop/daily/
    user: ${PARTNER_S3_KEY_ID}
    password: ${PARTNER_S3_SECRET}
exports:                              # run by your scheduler, as today: queryapigate exports run daily-revenue
  - {name: daily-revenue, query: monthly_revenue, destination: partner-bucket, format: parquet,
     path: "revenue/{date}.parquet"}
```

- **Each object's fields are exactly the API's.** A connection in the file is what `POST /api/v1/connections`
  takes. The same goes for a query, role, key, description, destination and export. No second schema to learn or
  keep in step; a field the API adds, the file has.
- **A kind left out of the file is left alone.** A file with only `queries` manages queries and nothing else. That
  lets a team start with one kind and add the rest later.
- **Not in the file:**
  - **The server's settings.** They're environment variables, set by Helm, Compose or systemd.
  - **Administrators and their tokens.** These are identities, managed by SSO (ADR 0007) or `queryapigate admins`.
  - **History, runs, usage counters and the audit log.** These are records, not configuration.
  - **Earlier versions of a query.** The file holds the definition that should be live. Its history stays in the
    store.

### 2. No secret is ever in the file

- **Credentials are `${VAR}` references, and nothing else is accepted.** A connection's `password`, a destination's
  keys, and any value the API masks are refused if they're a literal. The reference is stored as it is, and the
  *server* expands it when the connection is used, as today. So the variables must be set where the server runs (a
  Kubernetes Secret, through the chart's `existingSecret`), not where `apply` runs.
- **An API key carries its secret's hash** (`secret_sha256`), never the secret. The secret lives in the team's
  secrets manager, or with the partner. `queryapigate config new-key` prints a fresh secret and its hash, for the
  first time. A key in the file without `secret_sha256` is refused on creation; an existing key keeps its secret.
- **Rotating a key** is changing its `secret_sha256`. The old hash keeps working for `grace_minutes` (ADR 0009;
  60 by default, 0 ends it at once), as `rotate` does through the API.
- **`config export` never writes a secret.** A stored password, plaintext or encrypted, comes out as a reference with
  a generated name (`${QAG_WAREHOUSE_PASSWORD}`), and the export lists every variable it named. Encrypted values
  can't be moved to another server's key anyway.

### 3. `export`, `diff` and `apply`

| | |
|---|---|
| `queryapigate config export [--kinds ...] > queryapigate.yaml` | The store as a file: the starting point. |
| `queryapigate config diff queryapigate.yaml` | What `apply` would create, change and (with `--prune`) delete, field by field. Exit 0 if nothing, 2 if something, 1 if the file is invalid. |
| `queryapigate config apply queryapigate.yaml [--prune] [--dry-run]` | Make the store match the file. |
| `POST /api/v1/config/diff`, `POST /api/v1/config/apply` | The same, for CI against a running server. Owner only: a new capability, `config.apply`. |

- **Diffs are by field.** A diff is what a reviewer reads, so it names each field that changes, with the old and new
  values (secrets as `<changed>`).
- **A query whose definition changed gets a new version, published.** A query that didn't change gets nothing, so
  applying the same file twice changes nothing and records nothing.
- **Order is worked out, not written.** Connections come before the queries on them, roles before the keys using
  them, destinations before exports. A reference to something neither in the file nor in the store is an error.

### 4. All or nothing

- **Everything is validated before anything is written**, through the same functions the API uses, so a file can't
  do anything the API would refuse. A problem in the fortieth object stops the apply before the first is written,
  and the error names the object and the field.
- **The writes are one transaction.** `db.transaction()` learns to join a transaction already open, so each kind's
  existing write path runs inside the apply's. A failure or crash part-way through leaves the store as it was.
- **The audit log records each change** as the API would, with `via: config`, and one `config_applied` entry above
  them with the file's SHA-256 and the counts. "Who changed this key's grants?" is then answered by "the apply of
  file `<hash>`", and the commit that produced it.

### 5. What the file manages, and drift

Pruning is where configuration as code does damage. An object made in the Console that the file doesn't know about
must not be deleted by an apply that was never meant to own it.

- **Objects remember that the file manages them.** An object created or changed by `apply` is marked `managed` in the
  store. The mark is shown in the Console and the API.
- **`--prune` deletes only managed objects missing from the file.** It never deletes anything made in the Console or
  the API that the file has never named. A file naming an existing object adopts it.
- **Drift is reported, not hidden.** `config diff` shows managed objects changed outside the file. With
  `QUERYAPIGATE_CONFIG_LOCKED=1`, managed objects can't be changed in the Console or the API (`409
  config_managed`), only through the file. For teams whose rule is "all changes through review".

### 6. Applied at startup, for GitOps

- **`QUERYAPIGATE_CONFIG_FILE`** names a file the server applies when it starts, before it serves. With several
  instances, the first to start applies it under the store's write lock. The others see that the file's hash is
  already applied and skip it.
- **A file that doesn't apply stops startup** with the error, as an incompatible store does. The previous instances
  keep serving, so a rolling update with a bad file stops at its first pod.
- **The Helm chart** gets `configAsCode.file` (or `configAsCode.existingConfigMap`), mounted and set as
  `QUERYAPIGATE_CONFIG_FILE`. A checksum of it on the pods rolls them when it changes. So a commit to git, synced by
  Argo CD, becomes a rolling update that applies it.
- **Pruning at startup needs `QUERYAPIGATE_CONFIG_PRUNE=1`**. Without it, startup only creates and changes.

### 7. Stability

**Experimental in 0.27.** The file's shape follows the API's, which is stable, but `format_version: 1`, the
`managed` mark, pruning and drift may change as teams use them. Each change is noted in the changelog. A file is
always checked against its `format_version`, and a newer one is refused, not half-read.

## Consequences

- **New:**
  - `queryapigate config export|diff|apply|new-key`.
  - `POST /api/v1/config/diff` and `/apply`, and the owner-only capability `config.apply`.
  - `QUERYAPIGATE_CONFIG_FILE`, `QUERYAPIGATE_CONFIG_PRUNE` and `QUERYAPIGATE_CONFIG_LOCKED`.
  - A `managed` mark on each kind's objects (schema 15, additive), and the `config_applied` audit entry.
  - The chart's `configAsCode` values.
- **What it costs:** one more way to change the store, with its own tests that it can't do what the API refuses. The
  transaction change touches every kind's write path, so it lands first, with tests that each still behaves alone.
- **What it doesn't do:**
  - It doesn't fetch secrets. `${VAR}` comes from the server's environment. Reading them from Vault or a cloud
    secrets manager directly is #98, which can add a reference form (`${vault:...}`) the file then accepts.
  - There's no Terraform provider. One over `/api/v1/config/diff` and `/apply` comes later, if asked for.
  - It doesn't merge several files. One file per server; teams wanting more generate it (Helm, Kustomize, Jsonnet).
  - It doesn't hold earlier query versions, so applying a file can't restore one. Restoring a version stays in the
    Console and the API.
- **Collection bundles stay.** They move one collection's queries between servers that aren't managed by a file,
  and a bundle can be imported into a managed server like any other change.

## Delivery

About a week and a half, in four slices:

1. The model: the file's parser and validator for every kind (by the API's own validation), `export` with secrets
   turned into references, and `diff`. Nothing written yet.
2. `apply`: `db.transaction()` joining an open one, the ordered all-or-nothing apply, the `managed` mark, pruning,
   the audit entries, `config new-key`, and the API endpoints.
3. Startup: `QUERYAPIGATE_CONFIG_FILE` with several instances, `QUERYAPIGATE_CONFIG_LOCKED`, the Console showing what's
   managed, and the chart's `configAsCode`, checked on kind.
4. A guide promoting dev's setup to prod through a pull request, run for real. The security review, the threat
   model, and 0.27.0 prep.

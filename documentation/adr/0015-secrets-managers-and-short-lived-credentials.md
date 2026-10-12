# ADR 0015: Secrets from a secrets manager, and short-lived database credentials

- **Status:** Accepted
- **Date:** 2026-10-12
- **Backlog:** #98 (secrets from a secrets manager, and short-lived database credentials).

## Context

A connection's or destination's password is either stored in the store (encrypted with `QUERYAPIGATE_SECRET_KEY`) or
a `${VAR}` reference, expanded from the server's environment each time the connection is opened (`store._expand_env`).
Configuration files (ADR 0014) accept only references, so the environment is where every credential lives.

That leaves three gaps that larger organisations hit at once:

- **Rotation needs a restart.** A process's environment doesn't change. A password rotated in Vault or AWS Secrets
  Manager reaches QueryAPIGate only when its pods restart, and until then every new connection fails.
- **Short-lived credentials can't be used at all.** Vault's database secrets engine issues a username and password
  per lease, typically for an hour. RDS and Aurora IAM authentication take a token valid for 15 minutes in place of a
  password; Cloud SQL and Azure Database have the same. Security teams increasingly allow nothing else for production
  databases.
- **Secrets as files.** Kubernetes' Secrets Store CSI driver, Docker secrets and Vault Agent all deliver secrets as
  files, rotated in place. Today a file can only reach a connection through an entrypoint script that exports it.

What's already true, and shapes the design:

- **A reference is resolved when a connection is opened, never stored resolved** - the same place a new source of
  secrets has to plug in.
- **The pool keys connections by their resolved settings, password included** (`pool.fingerprint`), so a new
  credential already gets new connections. But a connection already open keeps its credential, and nothing retires
  it before that credential's lease ends - when Vault revokes it, typically ending the session.
- **A reference to the server's own settings is refused** (`QUERYAPIGATE_*`, `OTEL_*`): an administrator could
  otherwise point a connection at a host of theirs and have the server send them its own secrets as a password.
  Every new source of secrets reopens exactly that question.

## Decision

### 1. References name where a secret comes from

The `${...}` form gains a provider prefix. Any connection or destination field can use one, as `${VAR}` today; so can
a configuration file.

| Reference | From |
|---|---|
| `${VAR}` | The environment, as today. |
| `${file:/run/secrets/warehouse-password}` | A file, re-read when it changes - CSI, Docker secrets, Vault Agent. |
| `${vault:secret/data/warehouse#password}` | Vault's KV engine, version 1 or 2: the path, then the field. |
| `${aws-sm:prod/warehouse#password}` | AWS Secrets Manager: the secret's name or ARN, then a field of its JSON. |

Google Secret Manager and Azure Key Vault follow the same pattern, when someone asks; the provider interface is one
function: a reference in, a value and how long it may be kept out.

### 2. Short-lived database credentials

A connection can name where its credentials come from instead of carrying them:

```json
{"name": "warehouse", "db": "postgres", "host": "warehouse.internal", "database": "analytics",
 "credentials": {"source": "vault-database", "role": "reporting-ro"}}
```

| `credentials.source` | Gives | Lasts |
|---|---|---|
| `vault-database` | A username and password from Vault's database secrets engine, for `role` | The lease (Vault's TTL) |
| `aws-rds-iam` | An IAM authentication token as the password, for the connection's `user` | 15 minutes |

- **Fetched when a physical connection is opened**, and reused for the next ones until it nears expiry - not one
  Vault lease per connection.
- **The pool retires a connection before its credential expires.** Each pooled connection remembers when its
  credential ends; checkout discards one within a margin of that, and the idle sweep closes it. A query never runs on
  a connection whose credential is about to be revoked.
- **TLS is required** with `aws-rds-iam`, as AWS requires.
- **Leases are released, not left.** Vault leases this process took are revoked when it stops cleanly, so they don't
  pile up until their TTL.

### 3. The server's own secrets, from files

Every setting that is a secret - `QUERYAPIGATE_API_KEY`, `_SECRET_KEY`, `_DATABASE_URL`, `_REDIS_URL`,
`_AUDIT_KEY`, `_MASK_KEY`, `_JWT_SECRET`, `_SSO_CLIENT_SECRET` - also reads from a file with a `_FILE` suffix
(`QUERYAPIGATE_SECRET_KEY_FILE=/run/secrets/fernet`), the convention Docker and most images follow. Setting both is
refused. They are read at startup, as today: the server's own secrets aren't fetched from Vault by the server, because
the credential it would need to reach Vault is itself one of them. On Kubernetes, the External Secrets Operator or the
CSI driver puts them in the Secret or the file the chart already uses.

### 4. What a reference may reach: allow-lists, set by the deployer

A provider reads whatever the server's identity can - its files, its Vault token, its IAM role - which is more than
any one connection should. An administrator who can save a connection could point it at a host of theirs and have the
server send them a secret meant for something else. So **nothing is reachable until the deployer says so**:

- `QUERYAPIGATE_SECRET_FILE_DIRS`: the folders `file:` references may read. Unset, `file:` is refused. The store's own
  folder never is.
- `QUERYAPIGATE_VAULT_PATHS`: the path prefixes `vault:` may read, and `QUERYAPIGATE_VAULT_DATABASE_ROLES` the roles
  `vault-database` may request.
- `QUERYAPIGATE_AWS_SECRET_PREFIXES`: the secret names `aws-sm:` may read.

A reference outside them is refused when it's saved - through the API, the Console or a configuration file - and
again when it's resolved, in case the lists shrank. That's the same rule as for the server's own settings, applied to
everything the server can reach.

### 5. Caching, and failing closed

- A resolved value is kept in memory only, for the shorter of what the provider says (a lease, a token's life) and
  `QUERYAPIGATE_SECRET_CACHE_SECONDS` (default 300). It's never written to the store, a log, a response, a span or the
  audit log; responses and diffs show the reference as written.
- **When a provider fails, a value still within its time is used; one past it never is.** The connection then fails
  with `secret_unavailable` (503), naming the provider and its error, not the secret. The
  `secret_provider_failing` alert says when one has been failing for five minutes.
- Settings shows each provider: configured, how it authenticates, when it last answered.

### 6. How the server authenticates to each

- **Vault** (`QUERYAPIGATE_VAULT_ADDR`): Kubernetes auth (the pod's service account token, a role), AppRole, or a token
  from a file Vault Agent keeps current. TLS verified, with `QUERYAPIGATE_VAULT_CACERT` for a private CA. Over Vault's
  HTTP API, so no SDK is needed.
- **AWS**: the standard credential chain - IRSA on EKS, an instance role, the usual environment variables - through
  `boto3`, an optional extra (`queryapigate[aws]`).

The Helm chart gains what Kubernetes auth needs: a service account token mounted only when Vault is configured
(the chart turns it off by default today, and keeps it off otherwise), and the IRSA annotation.

### 7. Stability

**Experimental in 0.28.** The reference forms are additive - `${VAR}` and stored passwords work as they do - but the
providers' settings, and the `credentials` field, may change as they meet real Vault and AWS setups.

## Consequences

- **New:**
  - The reference forms `${file:...}`, `${vault:...}` and `${aws-sm:...}`, and a connection's `credentials`.
  - `QUERYAPIGATE_*_FILE` for every secret setting.
  - `QUERYAPIGATE_SECRET_FILE_DIRS`, `QUERYAPIGATE_VAULT_ADDR` (with its auth settings), `QUERYAPIGATE_VAULT_PATHS`,
    `QUERYAPIGATE_VAULT_DATABASE_ROLES`, `QUERYAPIGATE_AWS_SECRET_PREFIXES` and
    `QUERYAPIGATE_SECRET_CACHE_SECONDS`.
  - The `secret_unavailable` error and the `secret_provider_failing` alert.
  - The `aws` extra.
  - No schema change: a reference is a string in the fields that hold one today.
- **What it costs:** a provider call per resolution, made when a connection is opened and then cached; the pool's
  retirement check on each checkout. The provider code is the riskiest part, so each one is tested against the real
  thing: Vault in a container in CI, and a local AWS stand-in for Secrets Manager. RDS IAM tokens can only be tested
  against AWS, so `aws-rds-iam` is tested to the token's signing, and the guide says so.
- **What it doesn't do:**
  - The server's own secrets don't come from Vault directly (section 3).
  - Google Secret Manager, Azure Key Vault, Cloud SQL and Azure Database IAM: not in 0.28; the provider interface is
    there for them.
  - It doesn't rotate the secrets itself. The secrets manager does; QueryAPIGate picks up the new value.
- **Upgrading:** nothing changes for a deployment that sets none of these.

## Delivery

About two weeks, in four slices:

1. References with a provider, the allow-lists, the cache, failing closed (`secret_unavailable`), the `file:`
   provider, and `_FILE` for the server's secrets.
2. Vault: KV, the database secrets engine with the pool retiring connections before their lease ends, Kubernetes auth
   and AppRole, leases revoked at shutdown; tested against Vault and PostgreSQL in containers, in CI.
3. AWS: Secrets Manager and RDS IAM tokens (the `aws` extra); the Helm chart's service account and IRSA; Settings and
   the alert.
4. A guide rotating a database password in Vault while QueryAPIGate serves, run for real. The security review, the
   threat model, and 0.28.0 prep.

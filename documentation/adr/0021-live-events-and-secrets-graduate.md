# ADR 0021: Live events, and secrets from files and Vault, graduate

- **Status:** Accepted
- **Date:** 2026-10-13
- **Backlog:** the "Before 1.0.0-rc1" item in BACKLOG.md's roadmap: live events graduating once their 0.33 payload
  has gone a release unchanged, and AWS against AWS. Graduates `live_events` (ADR 0002, 0020) and most of
  `secret_providers` (ADR 0015), on ADR 0017's terms.

## Context

Two of the four features still in `experimental.py` were each one step from `experimental.GRADUATION`:

- **Live events** stayed experimental in 0.33 for criterion 3 alone. ADR 0020 changed the payload, adding `type` and
  `source` and replacing `filename` with `query`, and a shape has to go a release unchanged first. 0.34 is that
  release.
- **Secrets from files and secrets managers** stayed experimental in 0.30 (ADR 0017) on two counts. AWS is tested
  only against moto, and RDS IAM authentication against nothing. And it had shipped only one release before. ADR
  0017 allowed file and Vault references to graduate before AWS. They have been tested for real (the whole suite;
  Vault 1.21 in the integration job), and their shape has not changed since 0.28: 0.30 only refused more `${VAR}`
  names.

Testing AWS for real needs an AWS account that CI or a maintainer can use: a Secrets Manager secret and an RDS
instance with IAM authentication. There isn't one yet.

Three criteria still needed work for both: a security review (each one guards data), floors, and documentation
that says what may be added and what the limits are.

## Decision

### 1. Live events graduate

`live_events` leaves `experimental.py`. That removes the `x-experimental` flag on `GET /events`, the Settings tag,
and the startup warning on both servers. The payload stays exactly as 0.33 shipped it. What may still be added:
event types, payload fields, filters and settings. Clients already skip a type or field they don't know.

### 2. `${VAR}`, `${file:...}`, `${vault:...}` and `vault-database` graduate; AWS stays

`secret_providers` becomes `aws_secrets`: `${aws-sm:...}`, `aws-rds-iam` and their three `QUERYAPIGATE_AWS_*`
settings. Its documentation moves under its own heading, *Secrets and credentials from AWS*. AWS graduates when a
maintainer can run a test against real AWS, and CI pins boto3's floor (1.28). Neither is in this release.

### 3. The security reviews, and what they found

Each review is recorded in the threat model's section for its feature. Three findings, each fixed here, each with a
test that fails without the fix:

- **A deleted key's name brought its runs.** A key's name can be given to a new key once the old one is deleted. The
  event log kept the old key's runs under that name, so the new key could resume from `Last-Event-ID: 0` and read
  its predecessor's runs from the last day. Now a key sees no event from before its `created_at`, whether replayed
  or live. None of a key's own runs can predate it, so nothing it should see is lost.
- **A `${file:...}` could reach the server's own secrets.** The documented setup keeps the server's secrets as files
  beside the connections' secrets: `QUERYAPIGATE_SECRET_KEY_FILE=/run/secrets/fernet` alongside
  `QUERYAPIGATE_SECRET_FILE_DIRS=/run/secrets`. An administrator could then reference the Fernet key as a password,
  with a host of their own, and receive it. The rule that already kept `${VAR}` away from the server's own settings
  now covers files too. A file named by any `QUERYAPIGATE_*` or `OTEL_*` setting, or by a credential variable the
  server may run with (`AWS_*`, `VAULT_*`, `KUBERNETES_*`...), is refused. So are `/proc` and the Kubernetes and EKS
  service-account tokens, whatever the allow-list says.
- **`secret_unavailable` told any caller where the secrets live.** The message carried the reference and the
  provider's answer, such as a file path, a Vault address, a Vault path or a policy refusal. It reached a partner's
  scoped key calling a saved query, and every MCP client. Now the message says only that a secret is unavailable.
  The reason goes to the log and to `detail`, which follows the rule for a database's own error text: the admin key
  and administrators get it, and a scoped key calling a saved query doesn't.

### 4. Floors

Vault and file references use only the standard library. Live events use `psycopg2` on a PostgreSQL store, for
LISTEN/NOTIFY, and that floor was never tested. It couldn't have been, because `psycopg2-binary` 2.9.0 has no wheel
for Python 3.11, the oldest version supported. The floor is raised to **2.9.5**, the first release with 3.11
wheels. The `oldest-dependencies` job pins it, gets a PostgreSQL service, and runs the suite a second time on a
PostgreSQL store. Raising a floor before 1.0 costs nothing (ADR 0017), and it is called out under *Upgrading*.

### 5. Limits, written down

- **Live events:**
  - retention bounds a resume
  - a run dropped before it was written is no event
  - a revoked key keeps its stream for up to about 75 seconds
  - there's no per-key cap on streams below `QUERYAPIGATE_EVENTS_MAX_CONNECTIONS`
  - the main server's `/events` has no resume
- **Secrets:**
  - a rotated or revoked value is used for up to `QUERYAPIGATE_SECRET_CACHE_SECONDS`
  - an administrator can still send an allowed secret to a host of theirs, so bind Vault roles
  - whoever can write inside an allowed folder decides what a reference reads
  - an `http://` Vault address sends the token in the clear
  - leases are revoked when a process stops, not when it crashes

## Consequences

- **1.0.0-rc1 is one roadmap item closer.** What stood in front of it was "AWS against AWS, and live events
  graduating". Only AWS is left, and it doesn't block rc1: like the connection types and tracing, it can stay
  experimental through 1.x with a written reason and a way to graduate.
- **Breaking, for an experimental feature's error message:** `secret_unavailable`'s `error` text no longer says why.
  A script that read the reason from it reads `detail` instead. The `code` and status are unchanged.
- **A key no longer sees events from before it was created.** The only events that rule hides belonged to a deleted
  key of the same name.
- **A `${file:...}` that pointed at one of the server's own secret files** is refused when saved, and fails as
  `connection_misconfigured` when used. That was never a supported thing to reference.

## Alternatives considered

- **Delete a key's events when the key is deleted.** That closes the same hole, but an admin stream resuming after
  an outage would then miss the deleted key's runs. Runs written in the second after the deletion would also still
  land under the name.
- **Graduate AWS against moto.** moto is the stand-in ADR 0017 already judged not enough. Whether RDS accepts an IAM
  token can only be tested against RDS.
- **Keep the provider's reason in the message for administrators only.** The message is the same for everyone on
  every front door, and MCP clients show only the message. `detail` is where the codebase already puts what only
  some callers may see.

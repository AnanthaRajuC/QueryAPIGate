# ADR 0003: Named administrators with fixed roles, signing in with personal admin tokens

- **Status:** Accepted (2026-10-05)
- **Date:** 2026-10-05
- **Backlog:** #84 Phase 1 (admin identity). Phase 2, SSO for the Console, gets its own record.

## Context

Everything that changes QueryAPIGate's configuration goes through the Management API (`/api/v1`, ADR 0001), and
one check guards all of it: `v1.admin_only()` calls `app.require_admin()`, which passes for exactly one caller - the
holder of `QUERYAPIGATE_API_KEY` (or anyone, when nothing is configured). So:

- nobody can be given less than everything: a developer who should only edit saved queries can also read connection
  details, mint API keys and change settings;
- the audit log says `admin` for every change - it can't say who;
- removing one person's access means changing the key for everyone, and every script that holds it.

Data callers - scoped API keys and JWT-signed app users - are not part of this. They already have their own grants
(`apikeys.py`, `jwtauth.py`), and nothing here changes how they work.

1.0 freezes the Management API's permission model and the audit record's shape, so this has to land before it.

## Decision

### 1. Administrators, and their tokens

An **administrator** is a named account: `alice`, or `ci-deploy` for automation - a person and a pipeline are the
same thing here, each with a role. Stored in a new `administrators` table (schema 7, additive):

| Field | |
|---|---|
| `name` | unique; also unique against API key names, so a name in logs, `/metrics` and run history means one caller |
| `email` | optional; shown in the Console and the audit log |
| `role` | `owner`, `admin`, `developer` or `auditor` |
| `active` | an inactive administrator's tokens all stop working at once |
| `created_at`, `last_seen_at` | `last_seen_at` throttled like an API key's `last_used_at` |

An administrator signs in with a **personal admin token**, in an `admin_tokens` table: several per administrator
(a laptop, a CI job), each with a label, stored only as a SHA-256 hash, shown once when issued, with an
`expires_at` (default 90 days in the Console; optional through the API, so automation can choose) and a
`last_used_at`. Revoking one token leaves the others.

A token is sent **the same way as any key - `X-API-Key`** - and carries a recognisable prefix (`qagadm_...`), so:

- the Console's existing key box is the sign-in: paste your token instead of the shared key - no new sign-in flow
  in Phase 1;
- secret scanners (GitHub's among them) can be given the pattern;
- `authenticate()` looks it up in `admin_tokens`, not `api_keys`, by its prefix - one lookup, as today.

**Rejected:** local passwords (reset, email, MFA and lockout are what an identity provider does better; Phase 2 is
SSO, and the break-glass key covers teams without one); reusing the `api_keys` table with a "management" flag (a
data key and a person's admin access have different lifecycles and grants; one table would blur which a leaked
secret is).

### 2. Four fixed roles, and one table that says what each may do

Every `/api/v1` operation is mapped to a **capability**; every role is a fixed set of capabilities. One table in
code (`queryapigate/adminroles.py`), applied in `v1.admin_only()` before any route runs, and a test that fails if an
operation has no entry - as the OpenAPI staleness test does for the spec.

| Capability | Owner | Admin | Developer | Auditor |
|---|:-:|:-:|:-:|:-:|
| Read saved queries, collections, schema, catalog | ✓ | ✓ | ✓ | ✓ |
| Write saved queries and collections; validate, publish | ✓ | ✓ | ✓ | |
| Read connections (passwords are always masked) | ✓ | ✓ | ✓ | ✓ |
| Write connections; test and probe them | ✓ | ✓ | | |
| Read and write API keys and roles; the access map | ✓ | ✓ | | read |
| Cache, MCP tool settings | ✓ | ✓ | | |
| Audit log, run history, alerts, instances, metrics views | ✓ | ✓ | ✓ | ✓ |
| Read server settings (they are environment variables - read-only through the API) | ✓ | ✓ | | ✓ |
| Administrators and their tokens | ✓ | | | |
| Ad-hoc SQL and saved queries (the data plane) | all connections | all connections | all connections | none |

- Roles are fixed in Phase 1. Custom roles can come later without breaking anything: a role is a set of
  capabilities, and the capabilities are the stable names.
- A developer can run SQL on every connection, as the shared key can today; narrowing that is what scoped API keys
  are for.
- An auditor has no data-plane access at all: reading what happened needs no rows.
- An administrator always has their own tokens: anyone can list, issue and revoke **their own**, whatever the role.
- A refused operation answers `403` with the existing `admin_only` code when the caller isn't an administrator,
  and a new `role_forbidden` code when they are, with the capability it needed.

`GET /api/v1/me` answers who the caller is: name, role, capabilities, and how they authenticated. The Console uses
it for "Signed in as alice (Developer)" and to hide what that role can't do. (The server still enforces every rule;
hiding is only so nobody meets a button that always fails.)

### 3. The shared key becomes the break-glass key

`QUERYAPIGATE_API_KEY` keeps working, unchanged, with the **Owner** role - so no installation breaks on upgrade, and
a team that never creates an administrator sees no difference.

Once at least one active owner exists, every use of the shared key is logged as a warning and raises a
`break_glass_used` alert, naming the endpoint. Owners can then remove `QUERYAPIGATE_API_KEY` from the environment
entirely. **Authentication stays required while any administrator exists**, with or without the shared key -
otherwise unsetting it would silently reopen the server (today, no key and no scoped keys means open access).

The first owner can be created either way: in the Console with the shared key, or without a server -
`queryapigate admins create alice --role owner --email alice@corp.com`, which prints her first token once. The CLI
already writes to the store directly, as `queryapigate export` reads it.

### 4. The audit record says who, and how

Each audit entry gains `via`, alongside the existing `actor`:

```json
{"timestamp": "...", "actor": "alice", "via": "token", "action": "save_query", "target": "daily_revenue", ...}
```

`via` is `token` (a personal admin token), `break-glass` (the shared key), `cli` (a CLI command on the server) or
`open` (no authentication configured). Old entries have no `via`, and keep their `actor` as written. Run history
records an administrator's runs under their name, as it records a scoped key's under the key's name.

## Consequences

- **Breaking? No.** Every existing request, key, script and Console session works as before. New: two tables
  (schema 7, additive - a 0.15 instance refuses a schema-7 store, as with every schema bump), a `via` field in audit
  entries, a `role_forbidden` error code, the `/api/v1/administrators` and `/api/v1/me` routes.
- **The 1.0 contract** gains the capability names, the four roles and the audit `via` field. Freezing them is the
  point of doing this before 1.0.
- **Multi-instance:** administrators and tokens live in the store, so every instance sees a revocation on the next
  request, as with API keys today.
- **MCP and events:** an admin token works wherever the shared key does today, with its role's data-plane access.
- **Phase 2 (SSO) builds on this**: an SSO sign-in creates or finds an administrator and maps group claims to these
  same roles; the Console then gets a session cookie instead of a pasted token. Nothing here has to change for it.

## Delivery

About 1-1.5 weeks, in slices that each leave everything working:

1. Store (schema 7), `admins.py`, authentication of admin tokens, `queryapigate admins` CLI, the break-glass alert.
2. The capability table on every `/api/v1` operation, `GET /api/v1/me`, `role_forbidden`, the
   every-operation-is-mapped test, a test of each role against each capability.
3. `/api/v1/administrators` and tokens (OpenAPI, `API.md`), audit `via`.
4. Console: a Users screen (administrators, roles, tokens), "Signed in as ...", screens and actions by role.
5. A how-to guide ("Give your team their own admin access"), the threat model, `CHANGELOG.md`.

## Amendment (2026-10-05): who decides who can call a query

Found in the security review before 0.16.0. API keys and roles reach saved queries through collection membership,
read live; moving a query between collections, and renaming or merging collections, needed only `queries.write`. So a
developer - who by this record has "no keys" - could move a query into a collection a partner's key holds and give
that key the data, which the review showed end to end.

**Decision:** a move, rename or merge that gives any API key or role reach it doesn't have now also needs
`access.write` (owners and admins); otherwise `403 role_forbidden`, naming the keys and roles that would gain
(`gaining`). Changes that give no one anything new - filing into a collection nobody holds, taking a query out of
one, renaming a collection (its grants follow) - stay with developers.

**Not changed, and stated as the developer's power:** a developer decides what saved queries *return* - publishing a
new version of a query a partner already calls, or adding a query to a collection a partner holds. That is authoring;
drafts, the audit log and the run history make it reviewable. Owners and admins decide *who* can call what.


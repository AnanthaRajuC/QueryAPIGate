# ADR 0009: Rotating the encryption key, and reissuing an API key's secret

- **Status:** Accepted
- **Date:** 2026-10-11
- **Backlog:** #90 (rotating the key stored passwords are encrypted with) and #91 (reissuing an API key's secret, with
  the old one working for a while).

## Context

Two secrets can't be rotated today, and a security review asks about both.

**The encryption key.** With `QUERYAPIGATE_SECRET_KEY` set, a connection's or destination's literal password is stored
Fernet-encrypted (`enc:...`, `store._encrypt_password()`) and decrypted only when a connection is opened. It is one key.
Change it and every stored password fails with `password_undecryptable`; nothing re-encrypts them under the new key. So
rotating it means re-entering every password, and a leaked key can't be replaced quickly.

**API keys.** A scoped key's secret is generated once and stored as its SHA-256 hash (`api_keys.hash`, unique and
indexed, so authentication is one lookup). There's no way to give a key a new secret. Rotating one means creating
another key, copying the grants by hand and revoking the old one. The partner using it has an outage unless both sides
switch at the same moment. The copied grants can drift from the original, and run history and audit split across two
names.

What already rotates, and stays as it is:

- **Admin tokens:** an administrator can hold several tokens. Issue another, switch to it, then revoke the old one.
- **The break-glass `QUERYAPIGATE_API_KEY`:** one value from the environment, changed with a restart. Named
  administrators exist so that nobody needs it day to day.
- **The masking key** (`QUERYAPIGATE_MASK_KEY`): changing it changes every `hash`-masked value. That makes it a data
  decision, not a credential rotation, and it is out of scope here.

Both changes extend the store format and the API. Making them before the 1.0 freeze means they're designed together
with the contract rather than added around it.

## Decision

### 1. `QUERYAPIGATE_SECRET_KEY` takes several keys: the first encrypts, every one decrypts

`QUERYAPIGATE_SECRET_KEY=new-key,old-key`. Values are always encrypted with the first key. A stored value is decrypted
with whichever listed key works (`cryptography`'s `MultiFernet`). With one key, nothing changes: same format, same
errors. Each key is validated at startup as today, and the error names which entry in the list is invalid. The same key
listed twice is refused.

`password_undecryptable` now says that none of the listed keys decrypts the value.

### 2. `queryapigate rekey` moves every stored value to the first key

```
queryapigate rekey --check   # how many stored values are under each listed key, and any no listed key decrypts
queryapigate rekey           # re-encrypt them all under the first key
```

- **What it covers:** every value the store marks `enc:`. Today those are connection passwords, for active and deleted
  connections, and destination passwords. A future encrypted field joins by being registered in the same list.
- **All or nothing:** everything is re-encrypted in one store transaction. If any value can't be decrypted by any listed
  key, the command changes nothing and names the connections and destinations involved.
- **Safe to run again** and while servers are running. A value already under the first key is left alone. A connection
  being edited at the same moment waits for the transaction.
- **Recorded:** an audit entry `rekey`, with the counts and never the values. Without `QUERYAPIGATE_SECRET_KEY` set,
  the command refuses: there's nothing to encrypt with.
- **From the API too:** `POST /api/v1/settings/rekey` (owner only, a new capability `secrets.rotate`), for deployments
  where nobody has a shell. Same result, same audit entry.

**Rotating, with several instances:**

1. Set `new,old` on every instance and restart them one by one. During the restart, an instance still on `old` alone
   can't read a password just saved by one on `new,old`. That surfaces as a clear `password_undecryptable` on that
   connection until the instance restarts. To avoid even that, roll out `old,new` first, then `new,old`.
2. Run `queryapigate rekey`. `--check` should then report nothing left under `old`.
3. Remove `old` from every instance.

At startup, a server whose store still has values under a non-first key logs it, and the Settings screen shows the
count. An `encryption_key_rotation_incomplete` alert reminds whoever started a rotation that it isn't finished.

### 3. An API key's secret can be reissued, with the old one working for a grace period

```
POST /api/v1/api-keys/{name}/rotate   {"grace_minutes": 1440}
-> {"name": "partner-acme", "key": "sk_...new...", "previous_expires_at": "2026-10-12 14:00:00"}
```

- **The new secret** is generated and shown once, as when the key was created. Name, grants, expiry, rate limit and
  `active` don't change. Run history and audit stay under the one name.
- **The old secret** keeps working until `previous_expires_at`. `grace_minutes` is from 0 to 10080 (a week) and
  defaults to 60. Use 0 for a leaked secret, which then stops working at once.
- **Only one old secret at a time.** Rotating again while an old secret is still in its grace period is refused with 409
  `key_rotation_pending`, so a double click can't cut off the secret a partner is still using.
  `DELETE /api/v1/api-keys/{name}/previous` ends the grace period early.
- **Whether the partner has switched:** the key's entry gets `previous_last_used_at` next to `last_used_at`. The
  Console shows "The old secret was last used 3 minutes ago" and, once its grace period ends, "has expired".
- **How it's stored** (schema 12): `api_keys` gains `previous_hash` (indexed) and `previous_expires_at`. Authentication
  stays one indexed lookup: `WHERE hash = ? OR previous_hash = ?`. A match on `previous_hash` counts only before
  `previous_expires_at`. An expired old hash is cleared on the next rotation or the retention sweep. The secret itself
  is never stored, before or after, as today.
- **Every rule applies to both secrets the same way:** `allowed_ips`, expiry, `active`, the rate limit (one bucket per
  key, not per secret).
- **Capability:** `access.write`, as for creating a key. **Audit:** `rotate_api_key` (with `grace_minutes`) and
  `end_previous_secret`.
- **Console:** the key's drawer gets **Rotate secret**, with a choice of grace period (none, 1 hour, 1 day, 1 week). It
  shows the new secret once, as at creation, and the old secret's status, with **End now**.

### 4. Stable from the start

Neither part is experimental. Both extend surfaces that are already stable: a setting that accepts more than it did, and
a route and two response fields on API keys. They're added now precisely so that 1.0 freezes them.

## Consequences

- **New:** a list in `QUERYAPIGATE_SECRET_KEY`; `queryapigate rekey [--check]` and `POST /api/v1/settings/rekey`; the
  `secrets.rotate` capability (owners only); schema 12 (`api_keys.previous_hash`, `previous_expires_at`);
  `POST /api/v1/api-keys/{name}/rotate` and `DELETE /api/v1/api-keys/{name}/previous`; `previous_expires_at` and
  `previous_last_used_at` on a key; the `encryption_key_rotation_incomplete` alert; the Console's Rotate secret.
- **Nothing changes for what exists:** one key in `QUERYAPIGATE_SECRET_KEY` behaves as today. A key never rotated has no
  old secret, and the API answers as before, with the two new fields `null`.
- **Downgrade:** a 0.21 server can't read schema 12 and refuses with its usual message. A store rotated under several
  keys needs only its first key on an older server once `rekey` has run.
- **Not in scope:** rotating the break-glass key or the masking key (see Context); secrets managers and KMS-held keys
  (#98), where the key would live outside the environment; automatic scheduled rotation, which belongs to the
  deployer's own scheduler running `rekey` and `/rotate`.

## Delivery

About a week, in three slices:

1. Several encryption keys and `rekey`: the list, validation, `rekey --check` and `rekey`, the API route and
   `secrets.rotate`, the alert. Tests on both store backends, including a value no listed key decrypts (nothing
   changes), a concurrent edit, and a rolling restart from `old` to `new,old`.
2. Reissuing an API key: schema 12, `/rotate` and `/previous`, authentication by either secret within the grace period,
   `previous_last_used_at`, the audit entries. Tests: the old secret stops at the second its grace ends, a second
   rotation during the grace is refused, grace 0, `allowed_ips` and expiry applied to both secrets, the upgrade from
   0.21.
3. The Console (Rotate secret, the Settings count), a guide that rotates both against a real server, the threat model,
   the changelog and 0.22.0 prep.

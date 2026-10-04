# How to give a key an expiry date

**Time:** 3 minutes. **You'll end up with:** a key that stops working on its own after a set date - for a trial, a
contractor, a partner engagement with a known end - with nobody having to remember to revoke it.

## Set `expires_at`

A date, `YYYY-MM-DD`:

```bash
curl -X POST http://127.0.0.1:5000/api/v1/api-keys -H 'X-API-Key: demo-key' -H 'Content-Type: application/json' -d '{
  "name": "trial-partner", "connections": ["shop"], "expires_at": "2026-12-31"
}'
```

```json
{"name": "trial-partner", ..., "active": true, "expires_at": "2026-12-31", "expired": false, ..., "secret": "sk_..."}
```

The key works **through the end of that day** (23:59:59, server time) and is refused from the next. Only a date is
accepted - `"31/12/2026"` gets `400`: *expires_at must be a date in YYYY-MM-DD format, or null for no expiry*.

In the Console, the API key form has an **Expires** date field. A key created from a [role](15-create-a-role.md) can
have its own expiry too: `{"name": "acme", "role": "partner", "expires_at": "2026-12-31"}`.

## What an expired key gets

Verified with a key that expired yesterday:

```bash
curl 'http://127.0.0.1:5000/q/orders_by_status?status=paid' -H 'X-API-Key: <expired secret>'
# {"error": "Unauthorized", "code": "unauthorized", ...}
```

The same `401` as a wrong key. The expiry is checked on every request, so there's no background job to schedule or
to fail quietly. The key isn't deleted - `GET /api/v1/api-keys` still lists it, with `"expired": true`, and its run
history and audit trail stay.

## Seeing it coming

**Alerts** (Console, under **Observability**, or `GET /api/v1/alerts`) warns from seven days before:

```json
{"id": "key_expiring:today", "severity": "warning", "kind": "key_expiring",
 "title": "API key 'today' expires today",
 "detail": "On 2026-10-04. Calls with it will be refused from then on - extend it, or give its callers a new key first."}
```

Once it has passed, that becomes a `key_expired` alert: *every call with it is refused. Extend it if it is still in
use, or revoke it.*

## Extending, removing or ending it early

All without changing the secret, so the caller changes nothing:

```bash
# Extend it - an expired key works again from the next request
curl -X PATCH http://127.0.0.1:5000/api/v1/api-keys/trial-partner -H 'X-API-Key: demo-key' -H 'Content-Type: application/json' \
  -d '{"expires_at": "2027-03-31"}'

# Never expire
curl -X PATCH http://127.0.0.1:5000/api/v1/api-keys/trial-partner -H 'X-API-Key: demo-key' -H 'Content-Type: application/json' \
  -d '{"expires_at": null}'

# Stop it now, keeping the key to switch back on later
curl -X PATCH http://127.0.0.1:5000/api/v1/api-keys/trial-partner -H 'X-API-Key: demo-key' -H 'Content-Type: application/json' \
  -d '{"active": false}'
```

A `PATCH` that doesn't mention `expires_at` leaves it as it was. Every change is in the [audit log](20-read-the-audit-log.md).

`DELETE /api/v1/api-keys/trial-partner` removes the key for good, which is the right end for a key that won't be
needed again.

## Good to know

- **Server time, by date.** "End of the day" is the end of that date on the server's clock. If the server runs in
  UTC and your partner is in UTC-8, the key stops at 16:00 their time. Pick the day after if that matters.
- **Signed-in users (JWT)** have no stored key, so no `expires_at`; their token's own `exp` does that job.
- **Expired keys still count** toward the API keys list. Delete the ones nobody will extend, to keep the list
  honest.

## Next steps

- [Give an external partner access to exactly one query](14-give-a-partner-one-query-only.md).
- [Restrict a key to specific source IPs](18-restrict-a-key-to-ips.md).
- [Read the audit log](20-read-the-audit-log.md).

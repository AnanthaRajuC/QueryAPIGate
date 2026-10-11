# ADR 0011: Per-key limits on concurrency, time and rows, and usage quotas

- **Status:** Accepted
- **Date:** 2026-10-11
- **Backlog:** #94 (per-key limits on simultaneous queries, time and rows) and #95 (usage quotas: calls and rows per
  day or month).

## Context

A key's only limit of its own is `rate_limit`: a token bucket of calls per second, minute or hour, per caller. Every
other limit is the whole server's:

- **Time:** `QUERYAPIGATE_QUERY_TIMEOUT`, which a saved query's version can lower.
- **Rows:** `QUERYAPIGATE_MAX_PAGE_SIZE` for a page, `QUERYAPIGATE_STREAM_MAX_ROWS` for a stream,
  `QUERYAPIGATE_MCP_MAX_ROWS` for an agent.

That leaves two problems a deployment with partners hits:

- **One caller can starve the rest.** A rate limit counts calls, not their cost. Twenty slow queries a minute stay
  under `60/minute` and can still hold every connection in the pool, so everyone else sharing that connection waits.
  And a partner's key can't be given a shorter time limit or a smaller result than an internal service's.
- **Contracts are written in quotas.** "100,000 calls a month" or "10 million rows a month" is how data products and
  partner agreements are sold and metered. A per-minute rate approximates neither.

Like every grant field, these become part of the 1.0 contract, so they're decided before the freeze.

## Decision

### 1. Three limits a key or role can narrow, never widen

New grant fields on API keys and roles. For a signed-in user, the role their token maps to supplies them, as it does
`rate_limit`.

| Field | Meaning | Effective limit |
|---|---|---|
| `max_concurrent` | queries this caller may have running at once, 1-1000 | this, across every instance |
| `query_timeout` | seconds one query may run | the smallest of this, the saved query version's own and `QUERYAPIGATE_QUERY_TIMEOUT` |
| `max_rows` | rows one call may return: a page, a stream, an MCP result | the smallest of this and the server's limit for that kind of call |

- **Narrow only.** A value above the server's limit is accepted, because the server's setting can change later, but
  the server's limit applies. The key's detail shows the effective limit beside the one granted.
- **What counts as running** for `max_concurrent`:
  - A query from the moment it's sent until its last row is read. A stream or a large export holds its slot for as
    long as it takes.
  - Every front door counts the same way: REST, ad-hoc SQL, MCP tools and exports run with the key.
  - A cached response holds no slot, since no query runs. A live `/events` stream holds none either.
- **Over `max_concurrent`:** `429 too_many_concurrent`, with `Retry-After: 1`. The query isn't queued: waiting
  belongs to the client, which knows whether it can.
- **Across instances:** with `QUERYAPIGATE_REDIS_URL` set, the count is in Redis, as rate limits are. Each running
  query holds a slot with a lease (its time limit plus a margin), so a slot held by a process that crashed is freed
  when the lease runs out rather than never. Without Redis, each process counts its own, as rate limits do, and the
  existing `rate_limits_not_shared` alert covers it.
- **Over `max_rows`:** the same as the server's limit for that kind of call does today. A page is cut to
  `max_rows` with `X-Has-More`; a stream stops at `max_rows`; an MCP result is truncated with `truncated: true`. A
  key's limit never becomes an error the server's limit isn't.
- **Over `query_timeout`:** `query_timeout`, as the server's time limit already answers.
- **Administrators and the break-glass key** have none of these limits, as they have no `rate_limit`.

### 2. Quotas: calls and rows per day or per month

```json
"quota": {"calls": 100000, "rows": 10000000, "per": "month"}
```

- **One quota per key or role.** `per` is `day` or `month`, calendar periods in the server's time zone (the zone every
  timestamp is in). Either count may be left out, but not both.
- **What's counted:** successful calls that return data, and the rows they return. That covers saved queries,
  ad-hoc SQL, MCP tool calls and cached responses, which are served data even though nothing ran. Refused calls aren't
  counted: unauthorised, invalid, over a limit, or a database error. A quota measures what was delivered, which is
  what contracts measure.
- **Per caller:** a key's own counter. For signed-in users, one counter per user (`jwt:<subject>`), so a role's quota
  is each user's allowance, not one pool the first user can exhaust.
- **When used up:** `429 quota_exceeded`, naming what ran out and `resets_at`, the next period boundary.
- **Every response to a caller with a quota** carries `X-Quota-Remaining-Calls` and/or `X-Quota-Remaining-Rows`, and
  `X-Quota-Reset`, so a client can slow down before it's refused.
- **Checked before the call, counted after it.** A call is refused once the period's counts reach the quota. The one
  call that crosses the row quota still completes, because its rows aren't known until it has run, and `max_rows`
  bounds how far it can go over.
- **Where it's counted:** in the store, so it survives restarts (schema 13, `usage_counters`: caller, period, calls,
  rows). Counts are added in the batched write that run history already uses, not as a write per call. With several
  instances, the count a check compares against is kept in Redis (`QUERYAPIGATE_REDIS_URL`), added to atomically by
  every instance and seeded from the store, so a caller overshoots only by its calls in flight. *(Amended while
  building it: the store alone, re-read once a flush interval, let a short burst through each instance nearly a
  whole quota - 1,615 calls for 1,000 on two instances - not "one interval's calls" as first written. With Redis the
  same burst got 1,016 through.)* Without Redis, the store's view applies, and the docs say what that means.
- **Visible:** each key's `usage_this_period` (calls, rows, period start, `resets_at`) in `/api/v1/api-keys`, and a bar
  in the Console. A `quota_nearly_used` alert fires at 80% (warning) and again when the quota is used up (critical).
  Each change to a quota is audited, as every grant change is.
- **Not billing.** The counts are what a contract meters, but invoicing, overage pricing and per-customer reports are
  for the deployer's own systems. `usage_counters` is readable through the API for that.

### 3. Stability

- **The three limits are stable from 0.24.** They narrow existing server limits in the existing ways, and they're
  grant fields 1.0 will freeze.
- **Quotas are experimental in 0.24.** What counts as a call (cache hits, partial streams) and the counter's precision
  may change once quotas meet real contracts. The aim is to graduate them before 1.0.0-rc1.

## Consequences

- **New:** grant fields `max_concurrent`, `query_timeout`, `max_rows` and `quota` on keys and roles, and in the Console's
  grant form; schema 13 (`usage_counters`); `429 too_many_concurrent` and `quota_exceeded`; the `X-Quota-*` headers;
  `usage_this_period` on a key; the `quota_nearly_used` alert.
- **Nothing changes for an existing key:** none of the fields is set, so the server's limits apply as before.
- **Cost:** `max_concurrent` adds a Redis round trip at the start and end of each query for callers that have it.
  Quota checks read a per-process cached counter, refreshed with each history flush, and add no write per call.
- **Not in scope:** queueing calls over `max_concurrent`; quotas on bytes (rows are what callers and contracts
  count); per-connection pool reservations (`max_concurrent` per key is the lever); billing.

## Delivery

About a week and a half, in three slices:

1. The limits: the fields on keys and roles (validated, copied from a role as the others are), the effective limits
   on every front door, `max_concurrent` with leases in Redis and per process, `too_many_concurrent`. Tests: a stream
   holding its slot until its last row, a crashed holder's slot freed by its lease, the narrowest limit winning, admins
   unaffected.
2. Quotas: schema 13, counting in the history batch, the check and `quota_exceeded`, the headers, `usage_this_period`,
   the alert, MCP. Tests on both stores and with Redis, including period boundaries in a non-UTC zone, per-user
   counters for signed-in users, and the overshoot bound.
3. The Console (the new fields in the grant form, the usage bar), a guide that gives a partner key a monthly quota and
   a concurrency cap and hits both for real, with the overshoot measured; the threat model, the changelog and 0.24.0
   prep.

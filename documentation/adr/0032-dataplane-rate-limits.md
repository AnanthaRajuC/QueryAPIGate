# ADR 0032: The data plane serves rate-limited keys, counting in Redis with Python

- **Status:** Accepted (2026-11-01): section 8's items are met. Through the data plane, a limited key served 4.4x
  Python's calls a second per gateway core, and 5.0x with the server-wide limit as well. First proposed 2026-10-31.
  Shipped in 0.48.0.
- **Date:** 2026-10-31
- **Backlog:** #102, *Next*: "Rate-limited keys next, then other formats and workspaces"
  ([ADR 0030](0030-shipping-the-data-plane.md), section 5's follow-up). It moves two rows out of
  [ADR 0028](0028-rust-data-plane-prototype.md)'s forwarded table: keys with a rate limit, and any server with
  `QUERYAPIGATE_RATE_LIMIT` set.

## Context

The data plane serves a saved-query read only for a caller with no limit of its own
(`dataplane.eligible_caller()`), and it serves nothing at all on a server with a server-wide limit
(`QUERYAPIGATE_RATE_LIMIT`: `serve.rs` sets `forward_everything`). ADR 0028 gave the reason: "Limits must hold across
both processes. Without Redis they are per process, so they need shared counters first."

That leaves out the deployments the data plane is for. A gateway that serves partners and agents gives their keys a
rate limit (how-to 17), and a public gateway sets a server-wide limit so that guessing keys is throttled. Today every
call such a key makes costs Python's full per-call CPU, the cost the data plane measured at 3.2-9.4x
([Performance, v5](../PERFORMANCE_V5.md)).

What Python does now (ADR 0011; `ratelimit.py`, `governance.py`, `app.py`'s `gate()`):

- **Two token buckets.** One is per client IP, the server-wide `QUERYAPIGATE_RATE_LIMIT`, checked before
  authentication. The other is per key, the key's `rate_limit` grant, checked after it. Both use the same grammar,
  `N/second|minute|hour|day`. A bucket starts full, refills at `count / period` a second, and a call spends one token.
- **With `QUERYAPIGATE_REDIS_URL`,** each bucket is a Redis hash, `qag:ratelimit:<scope>:<count>/<period>:<client>`.
  A Lua script reads and writes it atomically, with Redis's own clock (`_TOKEN_BUCKET`). So every process and node
  counts against one bucket.
- **Without Redis,** each process counts on its own. The effective limit is multiplied by the number of processes,
  as `ratelimit.py` documents.
- **When Redis fails,** Python counts in its own memory and leaves Redis alone for 5 s. It shows the failure in
  `/metrics` and in the `rate_limits_not_shared` alert. A limit stays enforced, per process, rather than lifted.
- **A refusal is a 429** with code `rate_limited`, `Retry-After` and `retry_after`, and one of two messages. It isn't
  recorded as a run, but it counts in `queryapigate_rate_limit_rejections_total` and feeds the `client_rate_limited`
  and `key_rate_limited` alerts.
- **Every answer to a limited caller** carries `X-RateLimit-Limit`/`-Remaining` (the server's limit) and
  `X-RateLimit-Key-Limit`/`-Remaining` (the key's), 200s and 429s alike.

**Found while writing this ADR: a key's bucket is named by the key's name only, not its workspace.** Key names are
unique within a workspace (`api_keys`' primary key is `(workspace, name)`), but `check_key_limit()` counts against
`permission.name`. So a key named `partner` in workspace `team-b` spends the budget of a key named `partner` in
`team-a` with the same limit. Reproduced: after `team-b`'s key spent a 2/minute budget, `team-a`'s key was refused on
its first call. The same is true in Redis (`qag:ratelimit:key:2/60:partner`) and of `max_concurrent`'s slots
(`qag:running:<name>`, `limits.py`). Quotas aren't affected: they count under `<workspace>~<caller>`. The fix comes
first, as its own change (section 1), because the bucket's name is what Python and the data plane must agree on.

## Decision

### 1. First: a bucket is named by workspace and key

A separate fix, before anything here is built. The key bucket and the concurrency slots are named
`<workspace>~<name>`, as quotas already are:
- `qag:ratelimit:key:<count>/<period>:<workspace>~<name>`;
- `qag:running:<workspace>~<name>`;
- in process, the same pair as the bucket's key.

The client bucket is unchanged: an IP isn't in any workspace.

*Built (2026-11-01):* `ratelimit.budget_name(permission)` names both, and `governance.check_key_limit()` and
`limits.acquire()` count under it. Tests pin it: two workspaces' `partner` keys have separate rate-limit budgets and
separate slots (`test_workspace_isolation`), and the Redis bucket is `...:default~<name>`
(`test_shared_rate_limit`). Each fails without the fix. The changelog has it under Security, with no advisory.

On upgrade every key's bucket starts full once, as a changed limit's does today. While a rolling upgrade has old and
new nodes side by side, they count in different buckets, so a key can briefly get up to twice its limit. The
changelog says so.

### 2. Only with Redis

The data plane counts a limited call only when `QUERYAPIGATE_REDIS_URL` is set, and then only in Redis. Without
Redis, every limited call is still forwarded, as now. Buckets in the data plane's own memory would add a second budget
beside Python's in every pod, which is the problem ADR 0028 named. A deployment with several replicas needs Redis for
its limits to mean anything already.

Python stays the one that decides. With Redis configured, `eligible_caller()` no longer refuses a key for its
`rate_limit` alone. The plan then carries the key's limit as `(count, period)`, so the data plane never parses a
key's grants. The plan's identity already covers the key's row, so a changed limit is a new plan. A data plane
without a Redis connection forwards any call whose plan has a limit, and, as now, everything on a server with
`QUERYAPIGATE_RATE_LIMIT`.

The server-wide limit is read from the environment, as Python reads it: the same grammar, with `period` in seconds.
A value the data plane can't read forwards everything, as now.

*Built (2026-11-01):* `eligible_caller()` lets a rate limit through when `config.redis_url()` is set, and
`dataplane.build()` adds `rate_limit: [count, period]`. The data plane keeps a plan only for a limit Python could have
made: a count of at least 1, and a period of 1, 60, 3600 or 86400. `ratelimit::parse()` reads
`QUERYAPIGATE_RATE_LIMIT` only in ASCII, with spaces and tabs, and a count that fits in a u64. Anything else that
Python's regex reads, such as other scripts' digits or a newline between the parts, forwards everything. A Redis URL
counts only with `redis://`, `rediss://` or `unix://` and no query string or fragment: their options are each
client's own. With any other URL, limited calls are forwarded.

### 3. The same buckets, counted by the same script

The data plane runs Python's `_TOKEN_BUCKET` script, byte for byte, against the same Redis keys: `EVALSHA` with the
script's SHA-1, and `SCRIPT LOAD` on `NOSCRIPT`. A test checks that the Rust copy is Python's text. The key's
`<period>` is written as Python's `format(period, 'g')` writes it, with the data plane's `pyfmt` (`60`, not `60.0`).
So a key's budget is one budget, whichever process takes the call.

The client is the address Python would see: the peer, or the hop that `QUERYAPIGATE_TRUST_PROXY` picks from
`X-Forwarded-For`. `proxy::client` already reads it, for ADR 0030's section 5. It is spelled as Werkzeug spells
`remote_addr`. The differential tests include an IPv6 client.

The connection is the `redis` crate with Tokio, `rediss://` over rustls and `unix://`, multiplexed. It has Python's
timeouts: 0.5 s to connect and 0.5 s to answer.

*Built (2026-11-01):* `ratelimit.rs`. `redis` 1.7.1, with `tokio-rustls-comp`, `connection-manager` and `script`. Its
rustls has default features off, so ring stays the only crypto provider, and its roots are the system's, as Python's
`ssl` reads them. A lazy `ConnectionManager` means startup never waits on Redis. It makes no reconnect attempts
within a call, and each count has a 1 s bound. `redis::Script` sends `EVALSHA`, then `SCRIPT LOAD` on `NOSCRIPT`, as
redis-py's `register_script()` does, so neither client sends `EVAL`. A test checks that `TOKEN_BUCKET` is
`_TOKEN_BUCKET`'s text. The client is `forward::peer_text()` of the address `proxy::client` found, which is the text
`X-QAG-Peer` gives Python. The key's bucket is `default~<name>`.

### 4. Counted once, when the data plane is going to serve

Python counts the client bucket before authentication and the key bucket after. The data plane counts neither until
every check that can forward before the query has passed: authentication, the plan, the parameters, and the
connection's pool. Then, in Python's order:

1. the client bucket, when `QUERYAPIGATE_RATE_LIMIT` is set. If it refuses, the answer is Python's 429,
   `Rate limit exceeded`, and the key bucket isn't touched, as in Python;
2. the key bucket, when the plan has a limit. If it refuses: `Rate limit exceeded for this API key`;
3. the query.

A call forwarded before step 1 is counted by Python, as now, so a key guessed at the data plane is still throttled,
by Python. A call forwarded *after* counting, because the query failed in the database or the renderer declined its
rows, must not be counted twice. The forwarded request carries what was counted, as
`X-QAG-Rate-Counted: client=<limit>,<remaining>; key=<limit>,<remaining>`. Python honours it only beside the data
plane's secret (`X-QAG-Dataplane`, checked with `hmac.compare_digest`, as the plan request is). Python then skips
those buckets and sends those numbers in its headers. Without the secret, the header is ignored.

*Built (2026-11-01):* the data plane counts after the mask key, the time limit and the page's offset: the last checks
that forward. A Redis failure on the key's bucket, after the client's counted, forwards with `client=` alone, and
Python counts the key's. `X-QAG-Rate-Counted` is one of the data plane's own headers, so it is removed from whatever
a client sends. Python reads it in `_CapturePeer`, only beside the secret and from a data plane of its release
(`dataplane.counted()`: anything not in that shape counts as nothing counted). `check_rate_limit()` and
`check_key_rate_limit()` then use its numbers, for a limit that still applies.

### 5. What a limited caller gets, byte for byte

A 200 the data plane serves carries the four `X-RateLimit-*` headers Python would send, with the bucket's
`remaining`. A 429 is Python's:
- the status;
- `Retry-After`, with the script's `retry`;
- the limit headers of the buckets counted: both when the key's bucket refused, the server's two alone when the
  client's did (the key's isn't counted then), the refused bucket's `remaining` being 0;
- the body, in Python's key order: `{"error":"Rate limit exceeded for this API key","code":"rate_limited",
  "retry_after":60,"request_id":"..."}`, or `"Rate limit exceeded"` for the client's bucket;
- the security headers.

A 429 isn't a run, so it isn't recorded or reported as one. It is counted, though. The runs report
(`POST /internal/dataplane/runs`) gains `rate_limited`: per batch, how many 429s for each client or `(workspace,
key)`. From that, Python increments `queryapigate_rate_limit_rejections_total`, notes the alerts' rejections and adds
the 429s to its request counters, as if it had refused them itself. The alerts then see one gateway, whichever process
refused.

*Built (2026-11-01):* `respond::too_many()` and `respond::rate_limits()`, also on the 200 and the 504. A 429 is logged
as Python logs one: with key `-` for the client's bucket, which Python checks before the caller is known. A batch's
`rate_limited` entries are `{scope, client | workspace + key, method, seconds}`, one duration per 429.
`governance.note_refused()` counts them under endpoint `api.run_named_query`. The 429s since the last report are
gathered in memory, so a flood of them costs a report per flush interval, not a queue entry each.

### 6. When Redis fails

A Redis error or timeout during a call forwards it uncounted. Python then counts it, in Redis or in its own memory
with its alert, as it does now. After a failure the data plane leaves Redis alone for 5 s, Python's
`RETRY_AFTER_FAILURE`, and forwards every limited call meanwhile. So an outage costs at most one 0.5 s timeout every
5 s. A limit is never lifted, and the data plane never answers a 429 that Redis didn't give.

### 7. What stays forwarded

- **Quotas** (`quota`): counted after a 200, in Redis and the store, with headers of their own. That's a change of its
  own.
- **`max_concurrent`:** a slot leased for the call's length and renewed while it runs. That's its own change too.
- **`query_timeout` and `max_rows`:** these keep no state, and the data plane could narrow its own settings with them.
  They're the next change after this one, and smaller.
- **MCP and the events server.**

### 8. Done when

*Status (2026-11-01):* all five are done (below).

It's Accepted when:

1. **Section 1's fix has shipped**, with tests that two workspaces' same-named keys have separate budgets, in process
   and in Redis, for rate limits and concurrency slots.
2. **The differential gate covers limited keys, against real Redis.** It uses a key with a rate limit, and a server
   with `QUERYAPIGATE_RATE_LIMIT`. The two front doors draw from one gunicorn, so each path gets its own key with the
   same limit. Each runs the same sequence across the refill boundary, and they must agree call by call: status,
   headers, body. Plus:
   - one budget, spent half through the data plane and half through Python, refuses at the same call as Python alone;
   - a call forwarded after counting (a database error) is counted once;
   - Redis stopped mid-sequence: no call is refused that Redis didn't refuse, and none is lifted;
   - without `QUERYAPIGATE_REDIS_URL`, every limited call is forwarded.
3. **Measured with `benchmarks/oltp.py`** on the benchmark laptop, with AC power, the performance profile and pinned
   cores: a limited key's lookups through the data plane, against the same key on Python alone and against an
   unlimited key through the data plane. The Redis round trip's cost is recorded. The limited key must still serve
   at least 3x Python's calls a second per gateway core.
4. **Python's metrics and alerts count the data plane's 429s:** a test that a 429 the data plane gave shows in
   `queryapigate_rate_limit_rejections_total` and in the `key_rate_limited` alert.
5. **Docs:** ADR 0028's table, how-to 64 (what it serves), how-to 17 (one budget with the data plane, and Redis
   needed for that), the changelog, and `SECURITY.md` if the Redis ACL below is recommended.

*Built (2026-11-01):*
1. Section 1 shipped as #139.
2. `dataplane/tests/test_rate_limits.py`, in CI's data plane job with a Redis service. Both processes reach Redis
   through a TCP relay the test cuts. It covers a key at 2/second across the refill boundary and at 3/minute, the
   server-wide limit with a key's, IPv4 and IPv6 clients, an invalid parameter (forwarded before counting) and a
   query failing in the database. All agree call by call. Two findings:
   - A new key's first two calls go to Python. The first records the key's use, which changes the digest the
     second reads.
   - During a Redis cut, Python's in-memory buckets start full: a key gets a fresh budget for the outage, as it
     always has.
3. `benchmarks/oltp.py` blocks `l1` (32 callers) and `l2` (one caller), at `797b2a8`, with a limit that counts every
   call and refuses none
   ([results](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/benchmarks/results/dataplane-rate-limits-2026-11-01/README.md)).
   Per gateway core, the limited key made 3,560 calls a second through the data plane against 809 on Python alone
   (**4.4x**). With the server-wide limit too, it made 3,616 against 728 (**5.0x**). The unlimited key through the
   data plane made 3,820, so counting costs at most 7% per core. The Redis round trip:
   - at one caller, it adds 51 µs to a call's p50 (0.110 to 0.161 ms), and the second bucket 32 µs more;
   - at 32 callers, calls a second drop 15% with one bucket and 29% with two, while the gateway's CPU drops too. The
     calls wait on Redis's one thread, not on the gateway;
   - Redis spends 17-25 µs of CPU per call on the script.
4. `tests/test_dataplane.py`: a report's `rate_limited` moves the rejections, the request counters and the alert.
   The gate shows the data plane's 429s in Python's `/metrics` and in the `key_rate_limited` alert.
5. The docs listed, and in `SECURITY.md` the ACL's one line (Consequences: the script's own commands need
   granting too).

## Consequences

- **Rate-limited keys and servers with a server-wide limit get the data plane's speed,** with Redis. These are the
  deployments that face partners and agents.
- **The data plane gains a dependency, the `redis` crate,** and a connection to Redis per pod. Its Redis user needs
  only `EVALSHA` and `SCRIPT LOAD` on `qag:ratelimit:*`, plus the commands the script runs: Redis checks a script's
  calls against the user too. A Redis ACL can say exactly that, and the docs recommend it:
  `~qag:ratelimit:* +evalsha +script|load +time +hmget +hset +expire`, and `+select` for a database other than 0.
  Checked against Redis 7: the data plane served and refused on that user alone. `CLIENT SETINFO` on connecting is
  refused and ignored.
- **Each served limited call costs one or two Redis round trips:** measured on loopback at about 50 µs for the first
  and 32 µs for the second (section 8), plus the network's own round trip where Redis is on another host.
- **One more header crosses from the data plane to Python** (`X-QAG-Rate-Counted`). It's trusted only with the secret
  that already guards the plan and the runs endpoint.
- **Without Redis nothing changes:** limited calls are forwarded, as now.
- **The workspace fix is a behaviour change on its own.** Two workspaces' same-named keys stop sharing a budget, and
  every bucket starts full once on upgrade.

## Alternatives considered

- **Buckets in the data plane's memory, without Redis.** Each pod would have two budgets, Python's and the data
  plane's, and a key could make up to twice its limit per pod. That is the failure ADR 0028 forwarded these keys to
  avoid.
- **Asking Python for each token** (an internal endpoint). It's one budget without new Redis code, but each call
  would pay a round trip into the process the data plane exists to spare. Measurement would decide against it.
- **One script for both buckets in one round trip.** It's faster on a server with both limits, but it's a second
  script whose arithmetic must match Python's. That's left as an open question, decided by section 8's measurement.
- **Counting before authentication, as Python does,** then forwarding a refused key's call uncounted. Every forwarded
  call would then need the counted header. Counting only once the data plane will serve keeps that header to the rare
  call that fails after counting.
- **Accepting a double count** on a call forwarded after counting, without the header. It's simpler, but a key near
  its limit could be refused by Python for a call the data plane already paid for.

## Open questions

- **Both buckets in one round trip** (above). Measured (section 8): the second round trip costs 32 µs at one caller
  and 16% of the calls a second at 32 callers, with the same calls a second per gateway core. It's a latency and
  throughput cost, not a CPU one; whether that's worth a second script is still open.
- **Redis Cluster.** Python's script touches one key per call, so it works on a cluster as it is. A combined script
  would need both keys in one hash slot.
- **Should a failed Redis also stop Python's plans for limited keys?** That would spare the data plane its 5 s of
  forwarding, at the cost of one more piece of state.

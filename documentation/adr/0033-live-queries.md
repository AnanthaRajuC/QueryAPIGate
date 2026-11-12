# ADR 0033: Live queries - a saved query's result, pushed again when the data behind it changes

- **Status:** Accepted (2026-11-11). Ships experimental, behind `QUERYAPIGATE_LIVE_QUERIES` (off by default), tested
  against a real Debezium Server 3.7.1 and PostgreSQL 16 (section 9). Section 7 lists what remains before it graduates.
- **Date:** 2026-11-11
- **Backlog:** #60 (change data capture via Debezium). Builds on [ADR 0020](0020-the-event-log.md) (the event log).
  Additive after 1.0: a new event type, a new events-server route and a new ingest route, all experimental (#64).

## Context

An app calls a saved query, shows the result, and wants to know when that result changes. Examples: a job's status
becoming `done`, an order shipping, a film's price changing on a screen someone has open. Today QueryAPIGate can't
tell it. Live events (`GET /events`) report that *a query ran with your key*, not that *data changed*. The app has to
poll, or the backend has to run its own push system.

#60 proposed Debezium for this. Debezium reads the database's change log; QueryAPIGate decides who may see each
change. A first draft of this ADR (2026-11-10, never accepted) streamed the changes themselves: each row change
became an event with its `before` and `after` values, visible to keys that could query the table, masked per key at
delivery. Working through real use cases showed three problems:

- **It couldn't serve the people who asked for it.** App users reach data through named saved queries and through
  `from_claim` (their own rows). A table-level rule has to either refuse them, which the draft did, or show them
  everyone's rows.
- **It made the store hold row data.** Every change, unmasked, on disk and in backups for the retention window. That
  would have made the store a sensitive data store, with `forget` unable to find a person inside arbitrary rows.
- **It needed new access rules:** a `changes` grant, row predicates for JWT users, and masking at delivery. Each one
  was a new kind of rule to design, review and keep consistent with the existing ones.

A different question avoids all three. The app doesn't want *the change*; it wants *the new result of the query it
already runs*. QueryAPIGate can produce that by re-running the query, as the subscriber, whenever a table the query
reads changes. The only thing it needs from the outside is a signal: *table T on connection C changed*.

Facts that shape the design, checked against the code and against Debezium Server:

- **Debezium Server's HTTP sink can't send a static header.** It has no setting for `X-API-Key` or a fixed bearer
  token. It authenticates by `jwt` (a login endpoint we don't have), `oauth2` (an identity provider), or
  `standard-webhooks`: an HMAC-SHA256 signature of each request with a shared secret (DBZ-8063, since 3.0). That last
  one works with nothing else in place. It was verified against 3.7.1.Final (section 9).
- **`tableguard` already names the tables a statement touches**, qualified the same way for a saved query's `SELECT`
  and for a write's target (`schema.table`). A write and a subscription can be matched by plain string comparison.
- **MCP already runs saved queries outside an HTTP request** (`mcp_server.call_tool_for()`: `run_saved()` in a
  synthetic request context), with every rule REST applies.
- **The event log already carries events across instances**, with `NOTIFY` on PostgreSQL. A signal written there
  reaches every `queryapigate events` process sharing the store.

## Decision

### 1. A live query is a saved query, subscribed to over Server-Sent Events

`GET /live/<query>?<params>` on `queryapigate events` (and `/w/<workspace>/live/...`), with an `X-API-Key` or a
bearer token, like `/events`.

```text
event: result
data: {"query": "film_lookup", "params": {"film_id": "1"}, "rows": [{"id": 1, "title": "Academy Dinosaur",
       "rental_rate": 0.99}], "truncated": false, "reason": "subscribe", "refreshed_at": "2026-11-11 05:11:10"}

event: result
data: {..., "rows": [{"id": 1, "title": "Academy Dinosaur (Director's Cut)", "rental_rate": 1.49}],
       "reason": "change", "refreshed_at": "2026-11-11 05:11:12"}
```

- The first event is the current result, as `GET /q/<name>?format=json` would return it. Each later one is the new
  result after a change, sent only when it differs from the last one sent.
- **A refusal is the HTTP status `GET /q/<name>` would answer**, before any stream opens: 401, 403
  (`connection_forbidden`, `read_only`), 404, or 400 for a missing parameter.
- **Only queries that read.** A saved query that writes is refused with `403 read_only`. Re-running it would repeat
  the write with every change.
- **No `id:` and no `Last-Event-ID`.** A live query is a state, not a log. A reconnecting client gets the current
  result first, which is everything it missed. A client that falls behind gets the newest result, not every one in
  between (`_offer()`).
- If a re-run fails, the stream gets `event: error` with the house error body. A 401, 403 or 404 also ends the
  stream, since the caller is no longer allowed or the query is gone. A 5xx keeps the stream, and the next change
  tries again.

### 2. Access: nothing new

Every result is an ordinary run of the saved query, as the subscriber, through `run_saved()`. That covers grants
(`connections`, `queries`, `collections`), `allowed_tables`, `from_claim`, masking, the key's `max_rows` and
`query_timeout`, and usage quotas, which are checked and counted on every run. Each run is recorded in history with
transport `live`. A key granted only `film_lookup` can subscribe to `film_lookup`. A signed-in user whose query binds
`customer_id` from their token gets their own rows, and a change to someone else's row costs a re-run that returns
the same result, so nothing is sent.

A stream re-checks its credential every 60 seconds, as `/events` does. A revoked key ends the stream, and changed
grants apply from the next re-run.

### 3. Signals: `table.changed`, no row data

```json
{"type": "table.changed", "source": "cdc:shop", "occurred_at": "2026-11-11 05:11:12",
 "connection_name": "shop", "tables": ["public.film"]}
```

A signal is an event in the event log (ADR 0020), so every events server sharing the store hears it. It holds a
connection and table names, and nothing else. `tables` is `null` when they can't be told, meaning "anything on this
connection". It names no key, so only the admin's `/events` stream shows it. Retention is the event log's.

Signals come from:

1. **QueryAPIGate's own writes.** After a successful write through `engine.execute_sql()` (a saved write query,
   ad-hoc SQL, an agent's approved write), the tables the statement touches (`source: write`). Queued with the run's
   batch, never failing the write. This needs no infrastructure at all, and covers apps whose data changes only
   through QueryAPIGate.
2. **Debezium Server's HTTP sink** (section 4), for changes made anywhere else (`source: cdc:<source>`).

`pg_notify` triggers, Debezium's Redis Streams sink and a Kafka consumer can be added later as more signal sources.
Each only has to write the same event. None is needed to start.

### 4. Ingest: `POST /ingest/cdc/<source>`, signed with Standard Webhooks

On the main server, for `debezium.sink.http.url`:

- **The signature is the only credential.** `webhook-id`, `webhook-timestamp` and `webhook-signature` are checked
  against the source's secret (`v1,` + base64 HMAC-SHA256 of `id.timestamp.body`, key = the secret's base64 bytes
  after `whsec_`). A timestamp more than 5 minutes off is refused. API keys and tokens, the admin's included, aren't
  accepted here, and the secret is accepted nowhere else.
- **An unknown source and a bad signature both answer `404 cdc_source_not_found`**, so source names can't be probed.
  A body that isn't a change event, or comes from a server other than the source's `server_name`, gets
  `422 invalid_change`.
- **The body is reduced to table names**: one change event or a batch, with or without the `{schema, payload}`
  wrapper. Snapshot rows (`op: r`), tombstones and heartbeats name no table. The row values are read only to find
  the table and are never stored.
- **Durable before acknowledged.** The signal is committed in its own transaction before the `204`. On failure the
  route answers `503 store_unavailable` and Debezium retries. Debezium moves its offset only on success, so no change
  is lost. A resent change is a second signal, which costs at most one extra re-run whose result is unchanged.

In the prototype, sources come from `QUERYAPIGATE_CDC_SOURCES` (JSON: name → connection, secret, workspace,
`server_name`). Before it ships they become workspace objects with an API (`/api/v1/cdc-sources`): the secret
generated and shown once, stored encrypted, reissued with a grace period (ADR 0009), and every change audited.

### 5. Re-runs: grouped, throttled, compared

- **Matching.** A subscription listens on its query's connection and the tables its SQL reads
  (`tableguard.extract_tables()` on the published version). A signal marks every subscription whose tables it names.
  When either side's tables are unknown (MongoDB, a parse failure, a `{name}` text placeholder), any signal on that
  connection marks it.
- **Grouping.** Subscriptions by the same caller to the same query with the same parameters are one group, with one
  run for all of them (`subscribers alike share one run`, tested).
- **Throttling.** A group re-runs at most once per `QUERYAPIGATE_LIVE_MIN_INTERVAL` seconds (default 1), however many
  signals arrive. A bulk `UPDATE` of a million rows is one re-run per group. In the Debezium test, two changes 10 ms
  apart caused one re-run.
- **Comparing.** The result's SHA-256 is compared with the last one sent. An unchanged result is not sent.
- **Cache.** A live re-run skips the response cache's lookup (it runs *because* the data changed) and stores its
  result as usual.
- At most 8 re-runs at a time per events process, in worker threads.

### 6. Metrics

On the events server: `queryapigate_live_subscriptions`, `queryapigate_live_groups`, and the counters
`queryapigate_live_signals_total`, `_reruns_total`, `_pushed_total` and `_unchanged_total`. A high ratio of
unchanged re-runs to pushes means signals are broader than subscriptions: many writes to a table, few of them
affecting the rows being watched. That ratio is what section 8's key-level matching would reduce.

### 7. Before it graduates (ADR 0017's terms)

1. **Sources API**, as described in section 4.
2. **Measured**, on the benchmark laptop under the usual conditions:
   - re-run cost per group under a write storm;
   - delivery lag (commit to push) with 1,000 subscriptions across 100 groups;
   - that `/q/` latency doesn't move while re-runs happen;
   - ingest throughput with batches of 200.
3. **Counted against rate limits.** In the prototype, only quotas count re-runs. A re-run refused by the key's rate
   limit should be postponed, not dropped.
4. **The response cache invalidated by signals**, for `GET /q/` callers too. That is the performance win this
   opens up: long `cache_ttl`s without stale reads.
5. **A security review** (it guards access, and adds an unauthenticated-by-key route).
6. **Real Debezium in CI**, as section 9's test, run on every PR that touches it.
7. A how-to guide. (The `experimental.py` entry, the Settings rows, the API documentation's *Live queries* section
   and the changelog entry ship with the first version.)

### 8. Later, not now

- **Key-level matching.** Debezium's events carry the primary key. A subscription whose query filters on a column
  equal to a parameter (`WHERE id = :film_id`) could skip re-runs for other keys. That cuts the unchanged re-runs of
  section 6, at the cost of storing key values in signals.
- **More signal sources:** `pg_notify` triggers (no Debezium, best-effort), Debezium's Redis Streams sink, a Kafka
  consumer behind an optional extra.
- **MCP:** a resource subscription per live query, so agents can react instead of polling.
- **Webhooks:** a live query's result posted to a URL when it changes.

### 9. Tested for real (2026-11-11)

Prototype on `feat/live-queries`. PostgreSQL 16 (`wal_level=logical`) and Debezium Server 3.7.1.Final in Docker
(`pgoutput`, `snapshot.mode=no_data`, HTTP sink with `authentication.type=standard-webhooks`), QueryAPIGate from the
branch:

- A key granted only `film_lookup` subscribed to `/live/film_lookup?film_id=1` and got the current row.
- `UPDATE film ... WHERE id = 2` and `UPDATE film ... WHERE id = 1`, run directly in `psql`: Debezium posted twice,
  each `204` in 1-3 ms. One re-run followed (the two signals were throttled together), and the new row for film 1
  arrived on the stream about 2 seconds after the `UPDATE`, most of it Debezium's own polling. The change to film 2
  was not sent, because the stream's result hadn't changed.
- A forged signature, and the admin key with no signature, were both refused with `404`.
- When the client disconnected, its subscription and group were removed (`queryapigate_live_groups 0`).

Recording a demo video the same day (two phones on Rental Desk, through the same stack) found two things:

- **A signal reached the admin's streams twice.** It names no key, and the events server's fan-out read the streams
  that see everything once for "everything" and again for "this key", which for no key is the same set. Fixed, with
  a test. Runs always name a key (`-` at least), so nothing before this was affected.
- **Debezium's HTTP sink should batch.** Without `debezium.sink.http.batch.enabled=true`, a 1,000-row `UPDATE` was
  1,000 requests and 1,000 signals - each phone still got one new result, thanks to the throttle. With it, a handful.
  The documentation's example sets it. Recorded latencies through that stack: 0.5 to 0.7 s from the `UPDATE` to the
  new result on the phone.

The unit and integration tests (`tests/test_live.py`, 24 tests) cover the same paths with SQLite: access refusals,
write queries refused, writes through `/execute_sql` and saved write queries, unrelated writes, shared runs,
throttling, ingest signature and body checks, and the feature off by default. The full suite passes.

**More databases (2026-11-11 and -12), from `main`.** The same scenarios against each database, with Debezium Server
3.7.1.Final and changes made directly in the database: a subscription's row changed, another row changed (nothing
sent), a join's other table changed (only the join's stream), 2,000 rows changed at once (10 to 11 signals), a
`DELETE` (an empty result), and changes after an `ALTER TABLE`. Every ingest answered `204`.

| Database | Connector | From the change to the new result | Notes |
|---|---|---|---|
| MySQL 8.4 | MySQL (binary log) | 0.4 to 0.6 s | signals name `database.table` |
| MariaDB 11.4 | MariaDB (binary log) | 0.8 s | through a `mysql` connection |
| MongoDB 7.0 | MongoDB (change streams, a one-member replica set) | about 1 s | needed a fix, below |
| SQL Server 2022 | SQL Server (CDC tables, the Agent running) | 0.4 to 2.4 s | through a `jdbc` connection: any change on it re-runs the query; most of the delay is SQL Server's capture job |

What the testing found, each fixed with a test:

- **MongoDB changes signalled nothing.** Debezium names a MongoDB change by `collection`, not `table`, so ingest
  skipped every one, and a MongoDB query's subscription got its first result and nothing after it. Ingest now reads
  the collection, and a MongoDB query listens for `database.collection`.
- **One signed-in user's tokens with different claims shared a result.** Subscriptions were grouped by caller name,
  query and parameters; a JWT user's name is the same in every token, while a `from_claim` parameter (a tenant, say)
  may differ. The second token got the first one's rows. The group key now includes the token's claims, leaving out
  the ones that only say when and which token (`exp`, `iat`, `nbf`, `jti`, `auth_time`). Never in a release.
- **Two first subscriptions at once built the internal Flask app twice.** It is now built once, under a lock.
- **Two first JDBC queries at once failed with `JVM is already started`.** jaydebeapi checks and then starts the JVM;
  two threads could both start it. This one predates live queries: the main server had it for H2 and `jdbc`
  connections too. The JVM is now started under a lock.

Unexplained, and not reproduced: twice, a fresh `queryapigate events` process exited with no log line and no Python
fault (`PYTHONFAULTHANDLER` was on the second time), right after its first two concurrent MariaDB re-runs started.
68 fresh starts in the same way afterwards all survived. Worth watching in the CI job of section 7.

## Alternatives considered

- **Stream the changes themselves**, the first draft. See Context: it couldn't serve app users, stored row data, and
  needed three new kinds of access rule. A raw change feed for internal consumers can still be built later on the
  same ingest route. Nobody has asked for it yet.
- **Thin change events** (the table and the key, no values), with clients fetching on their own. This stores less
  than the first draft, but every client needs fetch logic, and access still needs a rule per event. Live queries
  do the fetch for the client, with the rules it already has.
- **`pg_notify` triggers instead of Debezium.** No extra process, but notifications are lost while nothing listens,
  PostgreSQL only, and someone has to add triggers to every table. Because a live query is a state, a lost signal
  only delays freshness, so this is worth adding later as a cheaper signal source.
- **Debezium Server's JDBC sink into PostgreSQL.** It mirrors tables rather than producing events, which moves the
  problem one hop.
- **Kafka first.** The heaviest dependency, for the deployments least in need of help.
- **Polling inside QueryAPIGate** (re-run every N seconds, as Hasura's live queries do). It needs no signals, but
  costs a run per group per interval whether or not anything changed. Signals make re-runs happen only after a
  change.
- **Authenticate ingest with an API key.** The HTTP sink can't send one, and a credential that can trigger re-runs
  for every subscriber shouldn't sit in the same table as credentials that read data.

## Consequences

- **The store holds no row data.** Signals are connection and table names, so the store's sensitivity, backups and
  `forget` are unchanged.
- **Re-runs cost database load:** at most one per group per interval, only after a change. Throttling, grouping and
  comparing results bound it. Section 7's measurements set the defaults, and a hot table with many distinct
  parameter values is the case to watch.
- **The events server now runs queries.** It builds the Flask app on the first subscription (as MCP does), and needs
  the same database drivers and network access as the main server. A deployment that splits them across hosts must
  allow it.
- **Signals on `/events`:** the admin's stream gains `table.changed` events. ADR 0020's contract tells clients to
  ignore types they don't know.
- **One write can signal twice:** a write through QueryAPIGate to a table Debezium also captures signals once from
  each. That costs at most one extra unchanged re-run.

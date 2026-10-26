# ADR 0027: Fewer round trips per query, and a read-only mode a query can't undo

- **Status:** Accepted (2026-10-26). Amended 2026-10-26, before release: each PostgreSQL message begins `RESET ALL`
  (see [1. PostgreSQL](#1-postgresql-one-message-per-query)).
- **Backlog:** 0.43, item 2. Found by the benchmarks in [PERFORMANCE_V3.md](../PERFORMANCE_V3.md#round-trips-per-call)
  and [PERFORMANCE_MYSQL_V1.md](../PERFORMANCE_MYSQL_V1.md#round-trips).

## Context

A saved query costs the database microseconds; the gateway's own work and its round trips to the database cost the
rest. The benchmarks counted the round trips exactly (with `log_statement` / the general log) and confirmed them by
timing calls at increasing network delay:

| Database | What a read sends today | Round trips |
|---|---|---|
| PostgreSQL | `BEGIN READ ONLY` · `SET LOCAL statement_timeout` · the query · `ROLLBACK` on release | 4 |
| MySQL | a ping (mysql-connector's `cursor()` checks the connection) · the query · `ROLLBACK` on release | 3 |

Across a network every round trip is paid in full: at 0.5 ms, PostgreSQL's 4 cost a call about 2.3 ms and MySQL's 3
about 1.8 ms, more than the gateway's own CPU. Trino and cloud warehouses, further away still, make this worse.

Two further findings:

1. **mysql-connector's C extension holds Python's GIL while it waits for a ping.** With 0.5 ms of delay, 8 threads
   ran `SELECT` 8.0× faster than 1 thread and `ROLLBACK` 7.8×, but `ping()` 0.9×: the whole process waits. Because
   every `cursor()` pings, a networked MySQL call freezes the worker for one round trip, which is why throughput at 32
   callers fell 22% with a 0.5 ms network and adding threads didn't help (PERFORMANCE_MYSQL_V1.md).

2. **On MySQL, a `SELECT` can make a pooled connection writable.** The driver makes a read-only connection read-only
   once, for the session (`SET SESSION TRANSACTION READ ONLY`), then rolls back after each call. A stored function
   that runs `SET SESSION TRANSACTION READ WRITE` (or sets `transaction_read_only`), called as `SELECT f()` - which
   the SQL guard passes as a read - flips the session, and every later call on that pooled connection runs
   read-write. Tested against MySQL 8.4 with a user holding `EXECUTE` on such a function: after `SELECT flip()`, the
   next `INSERT` on the same connection succeeded. The SQL guard still refuses writes from read-only keys, so this
   disables the database-level backstop rather than opening a write; it matters because that backstop is the
   point. The same function can also set `max_execution_time = 0`, which the driver only re-sets when the time
   limit changes: with the same limit on both calls, a query given 0.5 s ran for over ten minutes, until killed.

The obvious way to save PostgreSQL's round trips - make the connection read-only once, as a session default - has
the same flaw: `SELECT set_config('default_transaction_read_only', 'off', false)` passes the guard and changes it.

## Decision

Make every query **one or two round trips**, with read-only set **per transaction**, where a query running inside
that transaction can't change it.

### 1. PostgreSQL: one message per query

The connection runs in autocommit mode, and each query is sent as **one message**, which PostgreSQL runs as a single
implicit transaction that ends with the message:

- a read: `SET TRANSACTION READ ONLY; SET LOCAL statement_timeout = <ms>; <the query>`
- a write: `SET LOCAL statement_timeout = <ms>; <the statement>` (its `RETURNING` rows and row count are the last
  result, as today)

That is **1 round trip**, and no `ROLLBACK` on release: the connection is idle between calls.

Tested against PostgreSQL 16, with psycopg2 in autocommit:

| Check | Result |
|---|---|
| A write inside the message | refused: `cannot execute CREATE TABLE in a read-only transaction` |
| The time limit | enforced: `canceling statement due to statement timeout` |
| The query's rows | returned |
| `SELECT set_config('default_transaction_read_only', 'off', false)`, then a write in the next message | the next message is still read-only: refused |
| `statement_timeout` after the message | back to the session's (`0`): `SET LOCAL` didn't leak |
| Connection state between messages | idle: nothing to roll back |

**Amendment, before release: each message begins `RESET ALL`.** The attack above can't change the per-message
`SET TRANSACTION READ ONLY`, but it showed the wider problem with autocommit: a session setting a query changes is
committed with it, and applies to every later call on that pooled connection, from any key. Tested against
PostgreSQL 16: after `SELECT set_config('search_path', 'billing', false), set_config('TimeZone', 'Asia/Tokyo',
false), set_config('DateStyle', 'SQL, DMY', false)`, the next call on the connection ran with all three. A rerouted
`search_path` sends another key's unqualified table names to another schema; a changed `TimeZone` shifts the
timestamps it gets back. Rolling back each call's transaction had undone all of it.

So a message is `RESET ALL; SET LOCAL DateStyle = ISO; SET TRANSACTION READ ONLY; SET LOCAL statement_timeout =
<ms>; <the query>`. `RESET ALL` puts back the server's, database's and role's own defaults, and runs before the
query, so whatever a query sets lasts only until the next message on that connection starts. Measured on
loopback, 5,000 calls per variant, three interleaved rounds: no difference beyond noise (about 40 µs per call with
it or without). It does two other things:

- It clears psycopg2's `DateStyle`. psycopg2 needs ISO, and sets it at connect if the server's default differs, so
  the message sets it again, `LOCAL`.
- It clears a session-default read-only mode, so the connection no longer sets one. Read-only is the message's
  `SET TRANSACTION READ ONLY`, as above, and a stream's or agent write's own transaction.

A table-restricted PostgreSQL call also pins `SET LOCAL search_path` to the schema its `allowed_tables` were
checked against (0.43's schema-qualified grants).

**Streaming exports keep their explicit transaction** (`BEGIN` · `SET LOCAL` · a named cursor · `ROLLBACK`): a
server-side cursor needs one, and the round trips are paid once per export, not per row.

### 2. MySQL: one cursor per connection, and a chained read-only transaction

- **One cursor per pooled connection**, created when the connection is, reused for every query. No `cursor()` per
  call means no ping per call - and no GIL-holding wait. The pool's own liveness check, after 5 s idle, still pings;
  that is rare and stays.
- **Read-only per transaction, chained:** a read-only connection starts `START TRANSACTION READ ONLY` when it's
  opened, and on release sends `ROLLBACK AND CHAIN`, which ends the transaction and starts the next with the same
  access mode. Each call is then **2 round trips**: the query, and `ROLLBACK AND CHAIN`.

  Tested against MySQL 8.4: after `SELECT flip()` (which sets the session read-write), `ROLLBACK AND CHAIN` started
  the next transaction **read-only** - the next `INSERT` was refused - while the session default read `0`. Reads
  worked and saw fresh data. The idle chained transaction holds no metadata lock: an `ALTER TABLE` from another
  session completed in 0.01 s while the pooled connection idled in it. (InnoDB takes a read view at a transaction's
  first read, not at its start, so an idle chained transaction holds no snapshot either.)
- **Write connections** use the same pattern with `START TRANSACTION` and `COMMIT AND CHAIN`.
- **The time limit, set where a query can't change it:** the `MAX_EXECUTION_TIME(<ms>)` optimizer hint on the
  `SELECT` itself, placed with sqlglot's MySQL dialect. Where the hint can't be placed, fall back to setting
  `max_execution_time` on every call rather than only when it changes.
- **Multi-statement mode stays off.** It would allow one-message calls, as on PostgreSQL, but MySQL refuses a second
  statement in a message today, and that server-side refusal is worth more than the round trip.

### 3. Everything else is unchanged

ClickHouse and Trino speak HTTP and keep no session state between calls; DuckDB runs in process; H2 and JDBC are
outside the measured paths. Agent writes keep their own transaction handling (ADR 0008).

### 4. What the read-only boundary rests on

In both designs, as today, the user's SQL runs inside the read-only transaction, after QueryAPIGate's own
statements. On PostgreSQL, a second statement smuggled into the user's SQL - say `SET TRANSACTION READ WRITE` - would
run, so the SQL guard's one-statement check stays load-bearing, exactly as it is now (psycopg2 sends the user's SQL
as a simple-protocol string either way). On MySQL, the server refuses a second statement on its own. Moving
PostgreSQL to psycopg 3's extended protocol, which also refuses a second statement on the server, would add that
layer there too; it is a separate decision.

## Consequences

| | Round trips per read, today | After |
|---|---|---|
| PostgreSQL | 4 | **1** |
| MySQL | 3 (one of them holding the GIL) | **2** (none holding the GIL) |

- **Latency across a network:** at 0.5 ms, about 1.7 ms off every PostgreSQL call (6.2 → about 4.5 ms in v3's
  network test, and lower still with the metadata cache) and about 0.6 ms off every MySQL call, plus MySQL's threads
  overlapping their waits again.
- **CPU:** fewer driver calls per query - the round trips were part of the "data query" share of the profile.
- **Security:** MySQL's read-only and time limit can no longer be undone by a query, a weakness in every release up
  to 0.42. PostgreSQL's per-message read-only keeps today's guarantee.
- **Observability:** PostgreSQL's statement log shows one combined message per call instead of four statements.
- **Pooled MySQL connections sit inside an open transaction while idle.** Verified harmless above (no lock, no
  snapshot); `wait_timeout` still closes connections idle too long, and the pool discards dead ones.

### Tests

- Per database, against real servers in CI: a write in a read-only call is refused; the `set_config` and stored-function
  attacks don't carry into the next call; the time limit is enforced and doesn't leak; consecutive calls see each
  other's committed writes (fresh snapshots); an idle pooled MySQL connection doesn't block DDL.
- Round trips counted from the statement log: 1 per PostgreSQL call, 2 per MySQL call.
- The benchmarks rerun before release (`benchmarks/oltp.py`, `benchmarks/tpcc_mysql.py`), including the network
  blocks and MySQL threads at 0.5 ms.

## Alternatives considered

- **Session-level read-only on both** (1 round trip on MySQL with autocommit): a query can turn it off, as shown
  above. Rejected.
- **MySQL multi-statement messages** (1 round trip): gives up the server's one-statement refusal. Rejected.
- **Keep explicit `BEGIN` / `ROLLBACK`, only drop MySQL's ping:** safe, but leaves PostgreSQL at 4 round trips and
  MySQL's session-level read-only as it is. Rejected.
- **psycopg 3 with pipeline mode:** could also save round trips, and brings the extended protocol; a larger change of
  driver, left for its own decision.

## Open questions

- Whether the `MAX_EXECUTION_TIME` hint can be placed on every saved-query `SELECT` sqlglot parses, or how often the
  per-call `SET` fallback is needed.
- Whether MySQL's finding (2) warrants a security advisory for 0.42 and earlier, alongside the `allowed_tables` one.

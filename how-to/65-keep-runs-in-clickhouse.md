# How to keep run history and the event log in ClickHouse (a prototype)

**Time:** 20 minutes, with a ClickHouse server. **You'll end up with:** every run recorded in ClickHouse instead of the
metadata store - history, the event log and usage rollups - written in batches by the Python server and by the Rust
data plane, if you run one.

The metadata store (SQLite or PostgreSQL) records two rows per run: one in history and one in the event log. At a few
hundred calls a second that's fine. At the data plane's rates, 20,000 or more a second, it's 1.7 billion events a day.
A ClickHouse run store keeps them in about 13 bytes a run, expires them by partition, and counts usage from all of them.
It is a **prototype** ([ADR 0029](../documentation/adr/0029-clickhouse-run-store.md), measured and decided go): its
tables, its event ids and how it's switched on may change before it graduates. Try it on a test deployment first.

Keys, queries, connections and the audit log stay in the metadata store. `/metrics` doesn't change.

## 1. A database, and two users

ClickHouse 24.8 or later. On it, a database and a user for QueryAPIGate, which creates its tables at startup:

```sql
CREATE DATABASE queryapigate;
CREATE USER queryapigate IDENTIFIED BY '...';
GRANT SELECT, INSERT, CREATE TABLE, CREATE VIEW, ALTER TABLE, ALTER VIEW, ALTER DELETE
    ON queryapigate.* TO queryapigate;
```

With a data plane, give it a user of its own that can only insert runs. It can't read anything, and the views
QueryAPIGate made still fill history and the rollups from what it inserts:

```sql
CREATE USER queryapigate_dataplane IDENTIFIED BY '...';
GRANT INSERT ON queryapigate.events TO queryapigate_dataplane;
```

## 2. Point every instance at it

Set the same URL on **every** instance - the server, `queryapigate events`, MCP, exports and the CLI - and restart them
all together. Use `https://` (port 8443 on ClickHouse Cloud); plain `http://` is refused except to this machine.

```bash
export QUERYAPIGATE_RUN_STORE_URL=https://queryapigate:...@clickhouse.internal:8443/queryapigate
# or QUERYAPIGATE_RUN_STORE_URL_FILE=/run/secrets/run-store-url
```

The first instance to start logs:

```
Run store: runs, events and usage are kept in ClickHouse at clickhouse.internal:8443/queryapigate from now on (1234 events copied)
```

It copied the event log's last 24 hours with their ids, so a client that was connected to `/events` resumes as if
nothing had happened. The metadata store now records where runs are kept: an instance started without the URL, or with
a different one, refuses to start and says so, rather than recording runs somewhere else.

**History from before the switch** is still in the metadata store. To bring it across, once:

```bash
queryapigate runs copy-to-clickhouse
```

## 3. The data plane

Give it the same URL, or its own insert-only one - for the same server and database, named the same way:

```bash
QUERYAPIGATE_DATAPLANE_RUN_STORE_URL=https://queryapigate_dataplane:...@clickhouse.internal:8443/queryapigate \
QUERYAPIGATE_DATAPLANE_LISTEN=0.0.0.0:5000 QUERYAPIGATE_DATAPLANE_UPSTREAM=http://127.0.0.1:5001 \
    dataplane/target/release/queryapigate-dataplane
```

It logs `writing its runs to the run store (ADR 0029)`. It then writes the runs it serves straight to ClickHouse, with
event ids from a block Python leases it, and Python only counts them for `/metrics`. The lease names where Python
keeps runs: a data plane whose URL names another server or database writes none of them, and logs
`Python keeps runs in ClickHouse at ..., not ...` until it's corrected. One gunicorn worker behind it is
enough again: Python no longer queues every run.

## 4. See it working

Run a few queries, then ask ClickHouse:

```sql
SELECT count(), max(occurred_at) FROM queryapigate.events;
SELECT key_name, sum(runs) FROM queryapigate.run_hours GROUP BY key_name;
```

The Console's history, `GET /api/v1/history` and `/events` read from it as they read from the metadata store.

## What changes

- **History is kept by time,** not by a count per version: `QUERYAPIGATE_HISTORY_RETENTION_DAYS`, 30 days unless set.
  `QUERYAPIGATE_HISTORY_LIMIT` still sets how many runs per version the Console shows.
- **Live events arrive about a second later.** An event is delivered once it's a second old, so a client resuming from
  any id it was given misses nothing. `queryapigate_events_late_total` in `/metrics` counts the rare insert
  acknowledged later than that; a client resuming from a newer id could miss one. Above zero, ClickHouse is too slow
  for its inserts.
- **Usage reports are exact** for any window within the event log's retention, 24 hours by default: every run counts,
  whatever `QUERYAPIGATE_HISTORY_SAMPLE_RATE` keeps in history, and nothing is truncated. With these rates, the sample
  rate can be set low: history becomes examples to look at, not the record.
- **Backups:** a backup of the metadata store doesn't hold the runs any more. Back up ClickHouse with its own tools,
  if you need history kept.
- **`queryapigate history forget`** erases a person from all five tables in ClickHouse too.
- **Ad-hoc statements** are kept as text, by minute, only as long as the event log; the hourly rollup keeps a year
  of their SHA-256 hashes instead.

## To stop using it

```bash
queryapigate runs leave-clickhouse   # with QUERYAPIGATE_RUN_STORE_URL still set
```

Then unset `QUERYAPIGATE_RUN_STORE_URL` on every instance and restart them. The metadata store's event ids continue
above ClickHouse's, so clients still resume. The runs already in ClickHouse stay there; nothing is copied back.

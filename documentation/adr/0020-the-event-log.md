# ADR 0020: The event log - events stop being a view on run history

- **Status:** Accepted
- **Date:** 2026-10-13
- **Backlog:** #59 (a dedicated event log), the "Before 1.0.0-rc1" item in BACKLOG.md's roadmap. Carries out
  ADR 0002 §3-§5. Unblocks #60 (change data capture) and webhook delivery.

## Context

Live events are rows of `execution_history`. `queryapigate events` tails that table, and a run's history row id is its
event's SSE `id:`. ADR 0002 kept it that way in 0.13 and wrote down what that costs:

- **History policy decides what is an event.** Runs `QUERYAPIGATE_HISTORY_SAMPLE_RATE` samples out never become
  events. How far back a client can resume depends on `QUERYAPIGATE_HISTORY_LIMIT` (per version) and
  `QUERYAPIGATE_HISTORY_ADHOC_LIMIT`, not on time: a busy query trims its own replay window to minutes.
- **SQLite reuses an id** when the newest run is deleted before the next is recorded, and a client that saw the
  deleted run can miss the one that took its id.
- **There is nowhere to put an event that isn't a run** - a data change (#60), a key revoked, a query published.

Live events stayed experimental in 0.30 (ADR 0017) for one reason: the payload was meant to gain `type` and `source`
when this log shipped. Two more things turned up reading the code for this ADR:

- **A resume further back than 1,000 events loses the rest.** The replay sends the oldest 1,000 after
  `Last-Event-ID`, then switches to live, and everything between the two is skipped without a word.
- **Erasing a caller (ADR 0016) would miss the new table** unless it's told about it.

## Decision

### 1. An `events` table

| Column | |
|---|---|
| `id` | `INTEGER PRIMARY KEY AUTOINCREMENT` on SQLite, `BIGSERIAL` on PostgreSQL: never reused, on either |
| `type` | `query.run` - the only type this release writes |
| `source` | where it came from: for a run, the front door its history entry names (`rest`, `mcp`, `export`); `null` for a run that names none - a `queryapigate export` to a local file, or an older run, copied in at the upgrade, recorded before history kept it |
| `occurred_at` | `YYYY-MM-DD HH:MM:SS`, server time, like every other timestamp in the store |
| `key_name`, `connection_name`, `query_name`, `status` | what access checks and subscription filters read, without parsing the payload |
| `payload_json` | the event's `data:` exactly as sent |

Schema 16. Additive: a 0.32 process still running ignores the table.

### 2. Every run is an event, written with its history row

A run's event goes into the same batch transaction as its history row (history.py), and the `NOTIFY` that wakes the
events server is unchanged. Two things change from history's rules:

- **Sampling no longer applies to events.** A run sampled out of history is still queued, as an event only. The
  sample rate now reduces history, which is what it was for; the event log is bounded by its own retention instead.
- **A run whose saved query was deleted before its batch was written** loses its history row, as now, but keeps its
  event: the run happened, and a client may already be waiting for it.

Back-pressure is unchanged: when the queue is full, a run is dropped from both. Runs the examples seed into history
are not live traffic and are not events.

### 3. Retention by time

`QUERYAPIGATE_EVENTS_RETENTION_HOURS`, default 24, a whole number of hours. Each process's history writer sweeps
older events every ten minutes, in chunks; on PostgreSQL one instance at a time, under its own advisory lock, as the
history sweep does. A client can resume from anywhere in that window. Beyond it, it gets what a trimmed id always got:
the stream from the oldest event still kept.

24 hours covers a phone left overnight or a dashboard over a weekend's first day. One row per run is small, and
history at its default keeps far fewer runs than a busy day's events, so the default is a day and not a week. Lower
it on SQLite under heavy traffic; raise it on PostgreSQL if clients sleep for longer.

### 4. The upgrade continues the sequence (ADR 0002 §3)

The schema-16 step:

1. **Copies the runs still in history from the retention window** into `events`, keeping each one's id - its
   history row id, which is the event id a client already holds.
2. **Seeds the id sequence above `MAX(execution_history.rowid)`** (`sqlite_sequence` on SQLite, `setval` on
   PostgreSQL), so the first new event's id is above every id the old server could have sent.

A client holding an id from before the upgrade resumes after it: the runs it missed are replayed from the copy,
nothing twice. The step runs only while the sequence has never been used, so it's safe to re-run, and does nothing
on a new store.

`queryapigate migrate-to-postgres` copies `events` with its ids and carries the sequence across, from
`sqlite_sequence`, not from `MAX(id)`, so an id handed out and since swept away is never handed out again.

### 5. The payload, settled

```json
{"type": "query.run", "source": "rest", "occurred_at": "2026-10-13 12:00:00", "kind": "saved",
 "query": "monthly_revenue", "version": 3, "connection_name": "warehouse",
 "entry": {"executed_at": "2026-10-13 12:00:00", "status": "success", "rows": 42, "key_name": "mobile-alice", ...}}
```

- `type` and `source` say what happened and where, for every event type to come. `query.run` covers both a saved
  query's run and ad-hoc SQL: what differs is `kind`, `saved` or `adhoc`, and `query`/`version` are `null` for an
  ad-hoc run. `kind`, `query` and `version` are the names `GET /api/v1/history` already uses for the same things.
- `entry` is the run's history entry, as before.
- **Breaking, for an experimental feature:** `type: "execution"` and `"adhoc_execution"` and the `filename` field
  are gone. The Console's Home is updated in the same release. `GET /events` on the main server sends the same
  payload.

**Versioning.** No version field. Once live events graduate, the payload is under the compatibility promise like any
response: fields and event types may be added, none removed or renamed before 2.0. A client ignores a `type` it
doesn't know. A version field would be a promise to serve two shapes at once, which nothing here needs.

**CloudEvents** isn't adopted now. `id`, `type`, `source` and `occurred_at` map onto its required attributes one to
one, so a CloudEvents rendering can be added later (an `Accept` value or a parameter) without changing this one.

### 6. Subscription filters

`?type=`, `?kind=`, `?query=`, `?connection=`, `?status=` (`success` or `error`) on both `/events` endpoints. Each
takes one value, and the filters combine with AND. They narrow what the key may already see and never widen it: the
admin sees every event, any other key only its own, and then the filters apply. A replay applies them too. A
malformed `status` or `kind` is refused with `400 invalid_filter`, as `GET /api/v1/history` refuses one. A filter
naming something that doesn't exist matches nothing; it isn't an error, since a query may be created after the
stream opens.

### 7. A resume replays everything it may

The replay pages through every event after `Last-Event-ID` the key may see, 1,000 at a time, until it reaches the
live stream, instead of stopping at the first 1,000. Live events queue up meanwhile, as now; a client too slow to
take a long replay is disconnected as too slow and resumes from where it got to.

### 8. `/metrics` on the events server

The events server answers `GET /metrics`, behind `QUERYAPIGATE_METRICS_TOKEN` as the main server's does:

- `queryapigate_events_streams` - open streams
- `queryapigate_events_delivered_total` - events taken off the log and queued to clients
- `queryapigate_events_replayed_total` - events sent on a resume
- `queryapigate_events_slow_disconnects_total` - clients cut off for falling behind
- `queryapigate_events_lag_seconds` - a histogram of `occurred_at` to delivery. `occurred_at` has one-second
  precision, so read it in seconds, not milliseconds.

### 9. Still experimental for this release

The payload changed in this release, and ADR 0017's terms ask a graduate to stay unchanged for a release. Live
events graduate in the next release if nothing here has to change. The event-id rule (ADR 0002 §2) is already
outside the experiment.

## Alternatives considered

- **Keep `type: "execution"` and `filename`, adding `source` beside them.** Nothing breaks, but `type` would then
  name a run, not a kind of event. A data change (#60) would need a second field for that, and 1.0 would freeze the
  muddle. The payload is experimental so that this could be fixed once, before the freeze.
- **One type per kind of run (`query.run`, `sql.run`).** A subscriber that wants every run would need two filters,
  and the history API already calls this difference `kind`.
- **Don't copy recent history on upgrade; just seed the sequence.** ADR 0002 allowed it: an id from before the upgrade
  then resumes with nothing replayed. It's simpler, but a client that disconnected just before the upgrade would lose
  the runs in between. The copy is bounded by the retention window.
- **Respect the sample rate for events too.** It halves the writes a sampled server makes, but a client watching its
  own requests would see some of them arrive and others not, depending on chance. Sampling is for keeping history
  small; the event log has retention for that.
- **A setting to turn the event log off.** Writing an event costs one more insert in a batch that's already open. It
  doesn't seem worth a setting until someone measures it as a problem.
- **Retention by count, like history.** Count-based retention is what made the replay window depend on how busy a
  query is.

## Consequences

- One more row per run, in the same transaction as its history row. On SQLite at high traffic, the default 24 hours
  is the setting to look at.
- Live events no longer depend on history's sample rate, limit or retention. History settings stop appearing in the
  live events documentation.
- `forget` (ADR 0016) erases a caller's events with their runs and counts them in its report, and the data-flow
  inventory lists the table.
- **Rolling upgrades:** an instance still on 0.32 writes runs to history only, so its runs aren't events until it's
  upgraded. Upgrade `queryapigate events` with the instances: a 0.32 events server keeps tailing history, and its ids
  are no longer the store's event ids.
- #60 and webhook delivery have a table to write to and read from.

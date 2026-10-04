# ADR 0002: Event ids stay execution_history row ids in 0.13; a future events table continues them

- **Status:** Accepted (2026-10-04)
- **Date:** 2026-10-04
- **Backlog:** #59 (dedicated event log, *storage decision only*), #62 (ad-hoc runs in history and events),
  #64 (experimental label)

## Context

Live events (`GET /events` on the main server, and the standalone `queryapigate events` server) are rows of
`execution_history`. Each run's row id is its event's SSE `id:`. A client that reconnects with `Last-Event-ID`
is replayed every run after that id it may see. On PostgreSQL the events server is woken by a `NOTIFY` when a
history batch commits; on SQLite it polls.

That design made resume and cross-instance delivery cheap: there is one table, written in one batch, read by
every process sharing the store. It also ties events to history policy:
- runs that `QUERYAPIGATE_HISTORY_SAMPLE_RATE` samples out, or that the bounded queue drops, never become events;
- how far back a client can resume depends on `QUERYAPIGATE_HISTORY_LIMIT` (per query version) and
  `QUERYAPIGATE_HISTORY_ADHOC_LIMIT`, not on time;
- there's no place for an event that isn't a run: a data change (#60), a key revoked, a query published.

#62 had to decide in 0.13 where ad-hoc SQL runs are recorded, and the backlog asked for that choice to be made
together with #59's, so the store isn't migrated twice. 1.0 then freezes the event payload and the meaning of
an event id for anything not marked experimental (#64).

The question for 0.13 is only **where event ids come from**, and whether a later event log can change that
without breaking a client already holding an id.

## Decision

1. **In 0.13, event ids remain `execution_history` row ids.** Ad-hoc runs (#62) are rows in the same table, with
   `query_name` and `version` NULL (schema 5), and arrive as `type: adhoc_execution` events. They share the id
   sequence with saved-query runs, so one `Last-Event-ID` resumes both.
2. **An event id is an opaque, increasing integer per store.** Clients may compare and store it; they may not
   treat it as a history row id or look it up anywhere else. This is the contract the documentation states, and
   what 1.0 would freeze.
3. **When the dedicated `events` table (#59) ships, it continues the sequence instead of restarting it.** Its
   migration creates the table with its id sequence starting above `MAX(rowid)` of `execution_history`, and from
   then on the events server tails `events` only. A client holding an id from before the upgrade resumes after
   it, with nothing replayed twice, and an id from a run older than the table's first event gets what any trimmed
   id gets today: the stream from the oldest event still kept.
4. **The events table gets its own ids, not shared ones.** It uses `INTEGER PRIMARY KEY AUTOINCREMENT` on SQLite
   and an identity column on PostgreSQL, so ids are never reused after a delete. History keeps its row ids for
   pagination cursors, but they stop being event ids.
5. **Live events are marked experimental before 1.0 (#64)** until #59 ships, so the payload can still gain fields
   like `type`/`source` without a 2.0. The id rule above is the part that is not experimental.

## Alternatives considered

- **Build the `events` table in 0.13 and record ad-hoc runs only there.** That is the cleaner end state, but it
  doubles 0.13's storage work: a new table, its own retention, a dual write in the history batch, and moving the
  events server's tailing, ordering look-back and `NOTIFY` over. It also leaves ad-hoc runs out of
  `GET /api/v1/history`, which is where operators ask "what did the agents run last week". Ad-hoc runs belong in
  history either way, so recording them there now is not wasted when the events table arrives.
- **Restart ids at 1 in the new table and ask clients to reconnect without `Last-Event-ID`.** Simple, but every
  EventSource client sends its last id on reconnect by itself, so after an upgrade each would silently skip
  every event until the new ids passed its old one. Rejected.
- **Use UUIDs or `(source, id)` pairs as event ids.** They don't order, and resuming needs an order. Rejected.

## Consequences

- No second migration is needed for #62: schema 5 is the only store change in 0.13.
- Until #59 ships, everything listed under Context still holds. Sampling and trimming decide which runs become
  events and how far back a client can resume. The docs say so.
- **Known limitation until #59:** SQLite's implicit row id is `MAX(rowid) + 1`, so if the newest run is deleted
  (its query deleted, or trimmed by retention) before another is recorded, that id is handed out again. A client
  that saw the deleted run and then resumes may miss the run that reused its id. PostgreSQL's `BIGSERIAL` never
  reuses ids. The events table's `AUTOINCREMENT` removes this on both backends.
- The #59 migration has one hard requirement, recorded here so it isn't lost: **seed the events id sequence above
  `MAX(execution_history.rowid)`** (`sqlite_sequence` on SQLite, `setval` on PostgreSQL). Its upgrade test must
  resume from a pre-upgrade id across the migration.

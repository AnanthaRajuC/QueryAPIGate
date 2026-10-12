# ADR 0012: A tamper-evident audit log, streamed to a SIEM

- **Status:** Accepted
- **Date:** 2026-10-12
- **Backlog:** #96 (a tamper-evident audit log, streamed to a SIEM).

## Context

Every administrative change - a key created, a connection deleted, a write approved, a secret rotated - is an entry in
the store's `audit_log` table. The table keeps the newest `QUERYAPIGATE_AUDIT_LOG_LIMIT` entries (500 by default),
trimmed in the same transaction as each insert. `QUERYAPIGATE_AUDIT_LOG_EXPORT_FILE` appends every entry to a file as
well, so retention isn't limited by the cap.

Two things are missing, and SOC 2 and ISO 27001 auditors ask for both:

- **Evidence that nobody edited it.** Anyone who can write to the store - a database administrator, an attacker who
  got the store's credentials - can change or delete an entry, and nothing shows it happened.
- **A copy somewhere else.** The organisation's SIEM (Splunk, Microsoft Sentinel, Elastic, QRadar) is where security
  teams look and where retention is managed. Getting entries there today means tailing the export file.

What's already true, and shapes the design: every audit entry is written inside `db.transaction()`, which holds the
store's write lock - `BEGIN IMMEDIATE` on SQLite, an advisory lock on PostgreSQL. So even with several instances,
entries are written one at a time, in one order.

## Decision

### 1. Each entry is chained to the one before it

Every entry gets three more fields, computed inside its insert's transaction:

- `seq`: 1, 2, 3, ... with no gaps, across every instance.
- `prev_hash`: the previous entry's `hash`.
- `hash`: over `prev_hash` and the entry's canonical JSON (sorted keys, no whitespace).

Change, remove, insert or reorder any entry, and every `hash` from there on stops matching.

**A hash alone only proves consistency.** Someone with write access to the store can recompute every hash after
editing. So the chain is keyed when the deployer gives it a key:

- **With `QUERYAPIGATE_AUDIT_KEY`** (an HMAC-SHA256 key, kept like `QUERYAPIGATE_SECRET_KEY` - outside the store), an
  edit can't be re-signed without the key. Someone with the store but not the key can't hide a change.
- **Without it**, the chain is plain SHA-256. It catches accidental damage and edits by someone who doesn't know to
  recompute, and a SIEM copy (section 3) still contradicts any rewrite. The docs say plainly which one is in effect,
  and Settings shows it.

**The rolling cap still applies.** Trimming removes the oldest entries, so the chain's start moves. An anchor row
(`audit_chain`) records the `seq` and `hash` of the newest entry trimmed so far. Verification starts there: the
oldest kept entry must follow the anchor. A deletion from the head of the log then looks different from a trim.

**Upgrading:** existing entries are chained at the upgrade, in their current order. The chain proves nothing about
them before that, and an audit entry `audit_chain_started` records when it began.

### 2. Verifying it

- `queryapigate audit verify` walks the chain from the anchor and reports the first break: the `seq`, and whether an
  entry was changed, is missing or is out of order. Exit 0 if intact, 1 if not.
- `GET /api/v1/audit/verify` does the same, for the Console and monitoring.
- Each server checks the chain incrementally in the background, from the last entry it verified. The
  `audit_chain_broken` alert (critical) names the first bad `seq`.
- The export file's lines carry `seq`, `prev_hash` and `hash` too, so `queryapigate audit verify --file` checks a
  file on its own - including one kept long after the table trimmed those entries.

### 3. Streaming to a SIEM

**Two outputs**, each optional and each turned on by one setting:

- **Syslog** (RFC 5424, `QUERYAPIGATE_AUDIT_SYSLOG_URL`):
  - `tls://siem.example.com:6514`: TCP with TLS (RFC 5425), octet-counted framing.
  - `tcp://` is allowed. UDP isn't: it loses entries silently.
  - Each message is one entry as JSON, with `seq` and `hash` in its structured data.
  - This is what Splunk, Sentinel (through its agent), Elastic, QRadar and every log forwarder take.
- **OTLP logs** (`QUERYAPIGATE_AUDIT_OTLP=1`, with the `otel` extra): entries as OpenTelemetry log records, to the
  logs endpoint the standard `OTEL_EXPORTER_OTLP_*` variables name.

**Delivery survives the SIEM being down.**

- **A cursor per output** (`audit_delivery`: the last `seq` delivered) lives in the store, so it survives restarts.
  An in-memory queue would be lost.
- **One instance delivers at a time:** whichever holds the output's lease, renewed while it runs. If that instance
  dies, another takes over when the lease ends.
- **At least once:** an entry sent just before a crash is sent again after it. Each carries `seq` and `hash`, so the
  SIEM can drop the repeat.
- **Undelivered entries aren't trimmed**, even past the cap. That growth is bounded at ten times the cap, after which
  the oldest undelivered entries are trimmed anyway. The `audit_delivery_behind` alert says when an output is more
  than five minutes behind, or failing.
- **The TLS certificate is verified** against the system's trust store, or `QUERYAPIGATE_AUDIT_SYSLOG_CA`. A client
  certificate for mutual TLS can be given with `QUERYAPIGATE_AUDIT_SYSLOG_CERT` and `_KEY`.

### 4. Stability

- **The chain is stable from 0.25.** Its fields (`seq`, `prev_hash`, `hash`), the canonical form it hashes and
  `audit verify` are part of the audit log's contract. That's the reason to settle them before 1.0.
- **The SIEM outputs are experimental in 0.25.** Their settings and message format may change as they meet real
  SIEMs.

## Consequences

- **New:**
  - Schema 14: `audit_log.seq`, `prev_hash` and `hash`, plus the `audit_chain` and `audit_delivery` tables.
  - `QUERYAPIGATE_AUDIT_KEY`, `QUERYAPIGATE_AUDIT_SYSLOG_URL` (with `_CA`, `_CERT`, `_KEY`) and
    `QUERYAPIGATE_AUDIT_OTLP`.
  - `queryapigate audit verify [--file]` and `GET /api/v1/audit/verify`.
  - The `audit_chain_broken` and `audit_delivery_behind` alerts.
  - `seq` and `hash` on every audit entry in `/api/v1/audit` and the export file.
- **What it costs:** one HMAC per audit entry, and one read of the previous `hash`, inside a transaction that already
  holds the lock. Audit entries are administrative changes, a handful a minute, so this adds nothing measurable.
- **What it doesn't do:**
  - It doesn't stop someone with the store and the audit key from rewriting history. The SIEM copy is the defence
    against that, since it's outside both.
  - It doesn't cover run history. Runs are high-volume telemetry, not administrative evidence; they have their own
    retention.
  - It doesn't sign individual entries for third parties (a public-key signature per entry).
- **Rotating `QUERYAPIGATE_AUDIT_KEY`** is a new entry in the chain. `audit_key_rotated` is the last entry keyed with
  the old key, and the chain carries on from there with the new one. Verification takes both keys, as the encryption
  key does (ADR 0009): `QUERYAPIGATE_AUDIT_KEY=new,old`.

## Delivery

About a week, in three slices:

1. The chain: schema 14 (the upgrade chaining existing entries), `seq`, `prev_hash` and `hash` in every insert's
   transaction, the anchor kept through trims, `QUERYAPIGATE_AUDIT_KEY` and its rotation, `audit verify` (store and
   file) and the API, the background check and its alert. Tests:
   - each kind of tampering found and named: changed, deleted, inserted, reordered, head deleted;
   - many instances writing at once on PostgreSQL;
   - a trim, and the upgrade from 0.24.
2. Streaming: syslog over TLS (mutual TLS too) and OTLP logs, cursors and leases, undelivered entries kept, the
   delivery alert. Tests against a real TLS syslog receiver: the receiver down for a while, then back; the delivering
   instance killed mid-batch; a second instance taking over.
3. A guide that sends the audit log to a real syslog server over TLS, edits an entry in the store by hand to show
   `audit verify` finding it, and checks both against the SIEM copy. Then the threat model, the changelog and 0.25.0
   prep.

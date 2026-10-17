# ADR 0024: Fail-closed auditing - an action may require its audit entry

- **Status:** Proposed
- **Date:** 2026-10-17
- **Backlog:** from the 2026-10-17 security review ahead of 1.0.0-rc1. A stub: the decision is recorded, the work is
  deferred. Not a 1.0.0-rc1 blocker - today's behaviour is deliberate and documented.

## Context

Every administrative change - a key created, a query saved, a workspace member set - is recorded by
`store.record_audit()`, which appends one entry to a hash-chained log (ADR 0012) and, optionally, to an export file
(ADR 0016). Today it **never raises**: if the chain append fails it logs a loud error
(`<action> on <target> by <actor> happened, but its entry could not be written`) and the action stands. The export
copy is independently best-effort.

This is the same trade-off `record_execution()` makes for query-run history, and it is the right default for most
deployments: the audit log lives in the **same SQLite store** as the data it audits, so a failure that stops the
audit write is almost always one that would stop the change's own write too. The realistic independent failure is the
export *file* path (a missing directory, a full disk), which is already best-effort by design.

But some buyers - the regulated audience QueryAPIGate aims at (finance, health, insurance) - require the stronger
property: **no privileged change without a durable, tamper-evident record of it.** For them, "the action happened but
its audit entry didn't, and we only logged a warning" is not acceptable.

The review found that making this real is **not a flag flip.** `record_audit()` runs in its *own* transaction,
*after* the mutation has already committed (e.g. `services/workspaces.create()` commits the workspace, then calls
`record_audit()`). By the time the audit write runs, the change is already durable, so "fail the action when its
audit can't be written" is impossible at that point. True fail-closed auditing requires the audit entry to be written
**in the same transaction as the mutation**, so the two commit or roll back together - which touches every mutating
service.

## Decision

Keep today's best-effort behaviour as the default (it matches the store's own fate and avoids turning a transient
storage hiccup into a failed admin action). Add an **opt-in** strict mode, off by default:

- `QUERYAPIGATE_AUDIT_REQUIRED=1` makes a privileged change fail when its audit entry cannot be durably written, by
  writing the audit entry **in the same transaction as the mutation**. Both commit or neither does; the caller gets a
  `500` with a stable code (e.g. `audit_write_failed`) and the change did not happen.
- The export-file copy (ADR 0016) stays best-effort even under strict mode: it is a secondary sink, and the SIEM
  stream (ADR 0019) is the durable off-box path. Strict mode is about the primary chained log.
- Open question for the design: whether strict mode also covers run history (`record_execution()`), or only the
  administrative audit log. Leaning **audit log only** - run history is observability, not the tamper-evident record
  - but to be settled when this is built.

## Consequences

- A real feature with real scope: the audit insert moves into each mutation's transaction, or a shared helper every
  mutating service routes through. Needs a pass over `services/*` and the handful of direct `record_audit()` callers,
  plus tests that force an audit-write failure and assert the mutation rolled back.
- Until built, the threat model and `documentation/API.md` state plainly that auditing is best-effort-but-loud by
  default, so nobody assumes fail-closed semantics they don't have.
- Independent of the workspaces boundary review and the open-access-mode change from the same review; can land in any
  later release on its own.

## Status note

Proposed only. Build when a buyer or release actually needs it; it does not hold up 1.0.0-rc1.

# Architecture decision records

Significant architectural decisions, one file each, numbered in the order they were made. Each record states
the context at the time, the decision, the alternatives considered and the consequences, so later readers can
tell *why* the code looks the way it does, not just *what* it does.

A record is never rewritten once accepted. A later decision that changes it is a new record that says which
one it supersedes.

| ADR | Title | Status |
|---|---|---|
| [0001](0001-console-and-management-api.md) | QueryAPIGate Console: a React/TypeScript frontend over a versioned Management API | Accepted |
| [0002](0002-event-ids.md) | Event ids stay execution_history row ids in 0.13; a future events table continues them | Accepted |

Statuses: **Proposed** (under discussion) → **Accepted** (in effect) → optionally **Superseded by NNNN** or
**Deprecated**.

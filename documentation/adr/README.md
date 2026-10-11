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
| [0003](0003-named-administrators.md) | Named administrators with fixed roles, signing in with personal admin tokens | Accepted |
| [0004](0004-exports-to-object-storage.md) | Exports to object storage - destinations, saved exports, incremental runs | Accepted |
| [0005](0005-column-masking.md) | Column masking - rules on keys, roles and exports, traced to source columns, failing closed | Accepted |
| [0006](0006-ai-agents.md) | AI agents - the MCP server as an OAuth resource server, descriptions agents read, a per-agent usage view | Accepted |
| [0007](0007-console-sign-in-through-sso.md) | Console sign-in through the organisation's identity provider (OpenID Connect) | Accepted |
| [0008](0008-mcp-write-tools-with-approval.md) | Write tools over MCP, each write approved by a person before it runs | Accepted |
| [0009](0009-key-rotation.md) | Rotating the encryption key, and reissuing an API key's secret | Accepted |
| [0010](0010-tracing-and-security-headers.md) | Distributed tracing with OpenTelemetry, and security headers on every response | Accepted |
| [0011](0011-per-key-limits-and-quotas.md) | Per-key limits on concurrency, time and rows, and usage quotas | Proposed |

Statuses: **Proposed** (under discussion) → **Accepted** (in effect) → optionally **Superseded by NNNN** or
**Deprecated**.

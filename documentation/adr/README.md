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
| [0011](0011-per-key-limits-and-quotas.md) | Per-key limits on concurrency, time and rows, and usage quotas | Accepted |
| [0012](0012-tamper-evident-audit-log-and-siem.md) | A tamper-evident audit log, streamed to a SIEM | Accepted |
| [0013](0013-helm-chart.md) | A Helm chart | Accepted |
| [0014](0014-configuration-as-code.md) | Configuration as code | Accepted |
| [0015](0015-secrets-managers-and-short-lived-credentials.md) | Secrets managers and short-lived credentials | Accepted |
| [0016](0016-data-flows-and-fips.md) | Where data goes, and FIPS-validated cryptography | Accepted |
| [0017](0017-what-graduates-for-1-0-0-rc1.md) | What graduates for 1.0.0-rc1 | Accepted |
| [0018](0018-signed-releases.md) | Signed releases, and what they're built from | Accepted |
| [0019](0019-the-siem-stream-against-real-receivers.md) | The SIEM stream, against real receivers | Accepted |
| [0020](0020-the-event-log.md) | The event log: events stop being a view on run history | Accepted |
| [0021](0021-live-events-and-secrets-graduate.md) | Live events, and secrets from files and Vault, graduate | Accepted |
| [0022](0022-mongodb-streaming-and-graduation.md) | MongoDB streaming exports, and MongoDB graduates | Accepted |
| [0023](0023-workspaces.md) | Workspaces: delegated administration for teams | Accepted |
| [0024](0024-fail-closed-auditing.md) | Fail-closed auditing: an action may require its audit entry | Proposed |
| [0025](0025-semantic-retrieval.md) | Semantic retrieval: an `embed` parameter, not a vector store | Accepted |
| [0026](0026-trino-connections.md) | Trino as a native connection type | Accepted |
| [0027](0027-fewer-round-trips-per-query.md) | Fewer round trips per query, and a read-only mode a query can't undo | Accepted |
| [0028](0028-rust-data-plane-prototype.md) | A Rust data plane for `/q/`, as a prototype with a go/no-go decision | Accepted; decided: go |
| [0029](0029-clickhouse-run-store.md) | An optional ClickHouse run store for history, the event log and usage | Accepted; decided: go |
| [0030](0030-shipping-the-data-plane.md) | Shipping the Rust data plane in the image and the Helm chart | Accepted |
| [0031](0031-the-image-s-python.md) | The image's Python: Debian's own, not the official image's | Proposed |

Statuses: **Proposed** (under discussion) → **Accepted** (in effect) → optionally **Superseded by NNNN** or
**Deprecated**.

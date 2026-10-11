# ADR 0008: Write tools over MCP, each write approved by a person before it runs

- **Status:** Accepted
- **Date:** 2026-10-11
- **Backlog:** #88 (write tools over MCP, with a confirmation step) - the remainder of #42. Builds on ADR 0003
  (administrators and roles), ADR 0006 (agents signing in) and ADR 0007 (people signing in through SSO).

## Context

Agents can only read over MCP, deliberately: a saved query that writes is never listed as a tool, and `execute_sql` is
read-only whatever the key may do over REST. Some agents must act - file a ticket, update a status, record an order -
and a model deciding to write is a different risk from a person or a program deciding to: it can be confused, or steered
by text it read (a description, a row, a document). The question #88 left open is how a model-initiated write is
confirmed before it runs.

What already governs writes over REST, and stays the base here: a key writes through a saved query only with an explicit
per-query grant (`{"name": "submit_order", "allow_writes": true}`, never through `"*"`); `allowed_write_ops` narrows
which statements; `QUERYAPIGATE_ALLOW_WRITES` is the server's ceiling; `allowed_tables` and masking apply.

What MCP offers (2025-11-25): tool annotations (`destructiveHint`, `idempotentHint`) that a client may use to ask its
user before calling - a hint, which the specification says not to rely on; and *elicitation*, the server asking the user
through the client, in-band (a form) or out-of-band (a URL). Two facts narrow the choice:

- **A client's own confirmation proves nothing to the server.** A client can auto-approve, and the server can't tell a
  person clicked "allow" from a client that never asked.
- **This MCP server is stateless** - each HTTP request its own session - which is what makes it simple to run and scale,
  but rules out the server sending the client a request mid-call and waiting for the answer.

## Decision

### 1. What may write: saved queries an author marked for agents - never ad-hoc SQL

A saved query becomes a write tool only when all of these hold:

- its definition says so: **`agent_writes`** - `"approval"` (the default when set) or `"insert"` (section 3);
- the caller's grant includes it with `allow_writes: true`, as over REST; `allowed_write_ops` and
  `QUERYAPIGATE_ALLOW_WRITES` apply;
- its statement changes data and nothing else - one `INSERT`, `UPDATE` or `DELETE`, checked when it's saved - never
  DDL (`DROP`, `ALTER`, `TRUNCATE`, `CREATE`), whatever the grant would allow over REST;
- its connection is one that runs a statement in a transaction and reports the rows it changed: PostgreSQL, MySQL,
  SQLite, DuckDB. Others (ClickHouse's mutations, H2, JDBC, MongoDB) are refused for agent writes.

`execute_sql` stays read-only, always. Write tools carry `readOnlyHint: false`, `destructiveHint: true` (`false` for
`"insert"` ones) and `idempotentHint: false`, and their description says a person approves each call.

### 2. Every write is approved by a person, in QueryAPIGate - before it runs

A call to a write tool doesn't write. It creates a **write request** - the caller, the saved query and its version,
the exact arguments, the time - and answers at once:

```json
{"status": "approval_required", "request": "wr_7f3a9c", "approve_at": "https://data.example.com/console/approvals/wr_7f3a9c",
 "expires_at": "2026-10-11 14:30:00",
 "message": "A person must approve this write. Ask them to open the link; then call this tool again with approval: \"wr_7f3a9c\"."}
```

- **Who approves:** an administrator whose role has the new capability **`writes.approve`** (owners and admins) - signed
  in to the Console, through SSO where it's set up, so the approval is a named person with the organisation's MFA
  behind them. The **Approvals** screen shows each request: the agent (key or `jwt:<user>`), the tool, the SQL, the
  arguments, when it expires; **Approve** or **Reject**, with a reason. The person who asked for the write - an agent
  acting for `jwt:alice` - is not who approves it unless alice is herself an approver.
- **Then the agent calls the tool again** with the same arguments and `approval: "wr_7f3a9c"`. It runs only if the
  request was approved, hasn't expired (15 minutes by default, `QUERYAPIGATE_WRITE_APPROVAL_MINUTES`), was made by
  this same caller, and the arguments are the ones approved - byte for byte; approving one write never lets another run.
  It runs **once**: a second call with the same approval gets the first one's result, not a second write - so an agent
  retrying after a timeout can't write twice.
- **Why not MCP elicitation:** form mode is the client's word for it (section "Context"); URL mode needs a session this
  stateless server doesn't keep. The answer above is a plain tool result every client can show - and its link is the
  same out-of-band step URL mode would open. When the server keeps sessions, it can also send that link as a URL-mode
  elicitation; the approval itself doesn't change.

### 3. Without an approval: inserts only, and only within limits

`"agent_writes": "insert"` lets an agent run a query **without** approval, for the cases where waiting for a person
defeats the purpose (file a ticket, log an event) - and only when the query's statement is a single `INSERT` (checked
when it's saved, as `allowed_write_ops` is), so it can add rows but never change or remove one. It takes an optional
`idempotency_key` argument, kept a day, so a retried call doesn't insert twice.

### 4. Every agent write has a ceiling on what it can change

Each agent-writable query has **`max_affected_rows`** (default 1 - most agent writes change one row; set it higher on
purpose). The write runs in a transaction; when the database reports more rows changed than that, it's rolled back and
the call answers `too_many_rows_affected` - checked when it runs, not estimated beforehand, so a write approved when it
would change one row and that would change thousands by the time it runs is still stopped. On MySQL this needs
transactional tables (InnoDB, the default); a MyISAM table can't be rolled back, so the ceiling can't undo a write to
one - the guide says to keep agent-writable queries off them.

### 5. What's recorded

- **Write requests** in the store (schema 11): who asked, what, the arguments (needed to show the approver and to run
  it), the status - pending, approved, rejected, expired, done, failed - who approved or rejected it and why, and the
  result (rows changed, or the error). Kept 30 days, then removed; the run's history entry and the audit entries stay.
- **Audit:** `approve_write` and `reject_write`, by the approver, `via` their sign-in. **Run history:** the write, under
  the agent, with `transport: "mcp"`, `request` and `approved_by`.
- **Agent usage** counts each agent's write requests and how they ended; a new alert, `writes_awaiting_approval`, says
  when requests are waiting - with how long the oldest has.

## Consequences

- **New:** `agent_writes` and `max_affected_rows` on saved queries; schema 11 (`write_requests`); the `writes.approve`
  capability; `/api/v1/write-requests` (list, get, approve, reject); the Console's Approvals screen; one setting, one
  alert. Experimental in its first release.
- **Nothing changes for what exists:** no saved query becomes a tool until its author sets `agent_writes`, and no key
  writes until its grant says so - the same per-query grant REST uses.
- **The approval is the control** that a model can't satisfy by itself: it needs a named person, signed in, with the
  write's SQL and arguments in front of them. What it can't do is judge intent - an approver who approves without
  reading approves what they see; the screen shows everything the write will do except the rows it will touch, which
  `max_affected_rows` bounds.
- **Not in scope:** writes through `execute_sql`; approval by the end user an agent acts for (they don't sign in to the
  Console - a later step, through the same identity provider); a dry run that shows the rows a write would change
  (it would run the write's side effects - triggers, sequences - to show them); multi-statement writes.

## Delivery

About two weeks, in slices:

1. Definitions and grants: `agent_writes`, `max_affected_rows` (validated when saved - `"insert"` only for a single
   `INSERT`); write tools listed for callers whose grant allows them, with their annotations; `writes.approve`.
2. Write requests: schema 11, the request on a first call, the approval check on the second, single use, the row ceiling
   in a transaction, idempotency keys for inserts; `/api/v1/write-requests`; tests on PostgreSQL, MySQL, SQLite and
   DuckDB, including a changed argument, a different caller, an expired request and a write that changes too many rows.
3. The Console's Approvals screen, with a count in the sidebar; the alert; agent usage.
4. A guide (an agent that files a ticket on its own and updates an order only with approval, through a real MCP client),
   the threat model, the changelog - and a security review before release.

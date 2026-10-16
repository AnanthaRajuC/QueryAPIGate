# ADR 0023: Workspaces - delegated administration for teams

- **Status:** Accepted
- **Date:** 2026-10-16
- **Backlog:** #99 (workspaces), brought forward from "only on demand" to before 1.0.0-rc1: enterprises running one
  gateway for many teams will ask for it, and it has to fit the surface 1.0 freezes.

## Context

A QueryAPIGate server has one namespace. Every connection, saved query, collection, API key, role, destination and
export belongs to the server, and every administrator's role - owner, admin, developer, auditor (`adminroles.py`) -
applies to all of them. An administrator with a data role also runs SQL with the shared key's reach: every
connection (`admins.permission_for()`).

That is right for one team. It is wrong for the deployment an enterprise actually runs: a platform team operating one
gateway for many product teams.

- Team A's developer can change Team B's saved queries, and Team A's admin can mint keys onto Team B's databases.
- Team A's auditor reads Team B's history and audit trail.
- Names collide: two teams both want a query called `orders`.
- The only isolation today is a server per team, which is the operational burden the gateway exists to remove.

Collections (a key granted a set of queries) and scoped API keys narrow what a *caller* may run. Neither narrows what
an *administrator* may manage or see. That is the gap.

Two forces make this a pre-1.0 decision. Workspaces touch every store table, every `/api/v1` route, the Console,
configuration files, history, the audit log and events. And once 1.0.0-rc1 freezes URLs, the configuration format and
the API's shapes, workspaces must still fit in **by addition** - or wait for 2.0.

## Decision

### 1. A workspace owns things; the server keeps the rest

A **workspace** is a named boundary. It owns:

| Owned by a workspace | Server-wide, as today |
|---|---|
| connections (and their descriptions) | administrators, their tokens, sessions and SSO links |
| saved queries, their versions and collections | settings, instances, server secrets |
| API keys and roles (signed-in users' roles included) | the audit log's hash chain (entries are tagged) |
| destinations and exports | the event log (entries are tagged) |
| run history, usage counters, cached responses, write requests | metrics (gain a `workspace` label) |

A name is unique **within its workspace**: Team A's `orders` and Team B's `orders` are different queries. Workspace
names follow the rules export names do (letters, digits, `.`, `_`, `-`, up to 100).

**Nothing owned crosses a workspace.** A key's grants, a role's, an export's query and destination, a saved query's
connection: each names things in its own workspace only. Sharing a connection between workspaces is out of scope
(Alternatives).

### 2. `default` keeps today's server exactly as it is

Every store upgraded to this release puts everything it has into a workspace named `default`, which can't be renamed
or deleted. Every URL, route, key, file and Console screen that works today keeps working, and means `default`. A
server that never creates a second workspace sees no difference: no new field required anywhere, no new header, no
new step. That is what makes workspaces an addition, not a break - and what lets 1.0 freeze today's surface with
workspaces inside it.

### 3. Administrators: server-wide roles, plus memberships

- An administrator's existing role stays and becomes their **server-wide** role: an owner or admin there manages every
  workspace, as today. Every administrator who exists at the upgrade keeps exactly what they have.
- A **membership** gives an administrator one of the same four roles **in one workspace**, and nothing outside it. An
  administrator may have memberships in several workspaces, and need not have a server-wide role at all.
- Inside its workspace a role means what it means today, with one bound: a member's data role runs SQL on **its
  workspace's connections only**, never `ALL_CONNECTIONS`.
- What stays server-wide only: administrators and memberships (a workspace owner may manage its own workspace's
  members), settings, `secrets.rotate`, instances, applying a configuration file that names several workspaces, and
  creating or deleting workspaces.
- The shared `QUERYAPIGATE_API_KEY` is a server-wide owner, as it is today.

### 4. Addressing: the caller's workspace, or a prefix

- **Callers that belong to a workspace use the routes they use today.** An API key, and a signed-in user's role,
  belong to exactly one workspace; `/q/<name>`, `/catalog`, `/openapi.json`, `/execute_sql`, `/execute_mongo`,
  `/events` and the MCP server resolve every name in it. A key from Team B asking for `/q/orders` gets Team B's.
- **An explicit prefix always works:** `/w/<workspace>/q/<name>`, `/w/<workspace>/catalog`, and so on. A
  server-wide caller (the shared key, a server-wide admin) uses the prefix to reach a workspace; without it, they reach
  `default`. A caller naming a workspace that isn't theirs gets `404`, the same answer as for a workspace that doesn't
  exist, so names don't leak.
- **The Management API takes the same prefix:** `/api/v1/w/<workspace>/queries`. Unprefixed `/api/v1/...` is
  `default`, exactly as today, for every caller. A member-only administrator who calls it unprefixed is refused
  (`403 workspace_required`, naming their workspaces), never silently redirected - an automation must say where it
  writes.
- **New, server-wide:** `/api/v1/workspaces` (list, create, rename the description, delete an empty one) and
  `/api/v1/workspaces/<name>/members`.

### 5. Everything observable is per workspace

Runs in history, audit entries, events, alerts and usage counters carry their `workspace`. A member sees only its
workspace's; a server-wide auditor sees all, and can filter (`?workspace=`). The audit log stays one hash chain - an
entry's workspace is part of what is hashed - so a workspace's slice can't be edited without breaking the chain, and
the SIEM stream gains the field. Cached responses are keyed by workspace too.

### 6. Configuration files, the CLI and the Console

- **Configuration as code** (`format_version` 1, by addition): an optional top-level `workspace: team-a` makes a file
  describe that workspace; without it, a file describes `default`, as today. A member applies only its workspace's
  file.
- **The CLI** takes `--workspace` wherever it names a query, key or export (`queryapigate export orders --workspace
  team-a`), `default` without it.
- **The Console** gets a workspace switcher, shown only once a second workspace exists, and a Workspaces page for
  server-wide administrators.

### 7. What a workspace is not

A workspace is **delegated administration and logical separation inside one server**. It is not tenant isolation for
a hosted service: every workspace runs in the same process, on the same store, with passwords under the same
encryption key, and server-wide administrators see everything. A workspace protects teams from each other's mistakes
and reach, not from the people who run the server. The documentation says this in its first paragraph.

### 8. It ships experimental

Workspaces ship in **0.38 as experimental** (an `experimental.py` entry). The surface they add - `/w/<workspace>/...`,
memberships, the `workspace` fields - may still change once a real multi-team deployment uses it. What 1.0 freezes is
§2: a server with only `default` behaves exactly as today, so freezing today's surface at rc1 doesn't depend on
workspaces having graduated. They graduate on `experimental.GRADUATION`'s terms, which includes the security review a
boundary like this needs.

## Consequences

- **The largest change since the store moved to SQLite and PostgreSQL.** Every owned table gains a `workspace`
  column, and each name becomes unique per workspace: a primary key of (workspace, name), with the foreign keys that
  name it. On SQLite that is a table rebuild per table; both stores migrate in one schema version, with upgrade
  fixtures proving every earlier release's store comes up as `default`.
- **Every check that names something gets a workspace.** The authorization path (`apikeys.Permission` gains its
  workspace), every store lookup, the cache key, history and audit writes, metrics. A lookup that forgets it is a
  cross-workspace leak, so tests assert isolation per route, not just per feature.
- **Today's behavior is a contract tested on its own:** the whole existing suite runs unchanged against `default`.
- **A workspace is a security boundary**, so it gets a threat-model section and a security review before it
  graduates.
- **No new dependency.**

## Alternatives considered

- **Names unique across the server, a `workspace` field beside them.** The cheapest by far: primary keys and URLs
  unchanged, a column and a filter. But teams would have to coordinate names, and creating a name another workspace
  already has would answer `409` - telling Team A that Team B has an `orders`. Per-workspace names are what multi-team
  platforms do, and what an enterprise will expect.
- **The workspace in a header (`X-Workspace`) instead of the path.** Fewer routes, but a URL would no longer say what
  it returns: caches, logs, bookmarks and OpenAPI clients all lose it. The path keeps every response addressable.
- **Scoped administrators instead** (an admin role limited to some connections and collections, like a key's grants).
  It covers "Team A manages its own queries", but not separate names, history, audit or keys - and it would be a
  second model to keep beside workspaces once those came anyway.
- **Connections shared between workspaces.** A shared warehouse is common. But a shared connection makes the boundary
  ambiguous (whose descriptions, whose audit, who rotates the password). Out of scope now; it can be added later as a
  server-wide owner granting a connection to several workspaces, without changing anything above.
- **Wait until after 1.0.** Then any shape that turned out to need a change in today's surface would need 2.0. Settling
  §2 and §4 now is what keeps it additive.

## Settled in review

1. **Local-folder destinations** are allowed only in `default` for now: `QUERYAPIGATE_EXPORT_DIRS` is server-wide, so
   two workspaces could otherwise write into one folder. Object-storage destinations work in every workspace - their
   credentials bound them. Per-workspace folders may be added later.
2. **Signed-in users (JWT):** one issuer for the whole server. A token's role belongs to one workspace, so it picks the
   workspace. An issuer per workspace may be added later.
3. **SSO into the Console:** memberships are given in QueryAPIGate. Mapping identity-provider groups to memberships is
   left for later, by addition.
4. **Per-workspace limits** (rate limits, quotas, concurrency across a workspace's keys) are left for later, by
   addition; every per-key limit works inside a workspace as today.

## Delivery

One feature branch, in reviewable steps, each with its tests green:

1. **The store:** the `workspaces` and memberships tables, the `workspace` columns and per-workspace keys, the
   migration to `default`, upgrade fixtures - and the existing suite unchanged.
2. **Authorization:** memberships, the `Permission` bound, server-wide versus member capabilities.
3. **Addressing:** the caller's workspace at runtime, `/w/<workspace>/...`, the Management API prefix, and isolation
   tests across every route.
4. **Observability:** history, audit (in the chain), events, metrics, alerts, cache.
5. **Configuration files, the CLI and the Console.**
6. **Docs, the threat model, the changelog**, and the experimental entry. Then **0.38.0**.

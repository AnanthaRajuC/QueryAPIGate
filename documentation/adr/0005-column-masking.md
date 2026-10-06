# ADR 0005: Column masking - rules on keys, roles and exports, traced to source columns, failing closed

- **Status:** Accepted (2026-10-06)
- **Date:** 2026-10-06
- **Backlog:** #63 (column masking and PII redaction). Used by exports, [ADR 0004](0004-exports-to-object-storage.md).

## Context

QueryAPIGate decides which connections, tables, saved queries and - for signed-in users - which rows a caller
reaches. It doesn't decide which *values* leave: a key allowed to read `customers` gets `email`, `phone` and
`national_id` in full. That blocks two things people ask for - one saved query serving both internal users and a
partner, and letting an AI agent query customer tables when every value it sees goes to a model provider.

The hard part is not replacing values, it's knowing which output column holds what. A rule on the *result* column
name `email` is beaten by `SELECT email AS e`, `lower(email)` or `email || ''`, and a caller who may write SQL will
find that. So rules have to follow the data back to the table column it came from, and when that can't be done,
the safe answer is to refuse, not to guess.

Two facts about the code shape the design. `sqlglot` (already used for `allowed_tables`, `tableguard.py`) can trace
each output column of a query to the source columns it is computed from (`sqlglot.lineage`). And the response cache
(`cache_ttl`) stores *rendered* responses keyed without the caller - so masking must happen before rendering, and
be part of the cache key.

## Decision

### 1. A rule: which source column, and what to do with it

```json
{"mask": [{"column": "customers.email", "action": "hash"},
          {"column": "*.phone", "action": "mask"},
          {"column": "customers.national_id", "action": "drop"}]}
```

- `column` names a **source column**: `table.column`, with `*` as a wildcard in either part (`*.email`,
  `customers.*_id`); case-insensitive; a schema prefix is allowed and optional.
- `action`:
  - `hash` - HMAC-SHA256 of the value with a per-store secret, shown as 16 hex characters: the same input always
    gives the same output, so grouping, counting and joining on it still work; without the secret it can't be
    reversed by trying likely values;
  - `mask` - text keeps its first character (and an email its domain): `j***@example.com`, `+4*******`; any other
    type becomes null;
  - `null` - the column stays, every value null;
  - `drop` - the column is removed from the result.
- The hash secret is generated once and kept in the store (shared by every instance), or set with
  `QUERYAPIGATE_MASK_KEY`; changing it changes every hash.

### 2. Where rules live

- **On API keys and roles** (a new grant field, `mask`, alongside `allowed_tables`): a role is still a template, so a
  key created from it copies its rules; a signed-in app user gets their role's rules (`jwtauth.py` reads roles live).
- **On exports** (ADR 0004): the rules travel with the delivery, whoever triggers it.
- **Not on administrators.** Owners, admins and developers see data as it is - masking is for the callers they
  hand data to. Auditors see no data at all.
- **Not on saved queries**, for now: a query is shared by callers with different rights, so the rule belongs to the
  caller. Authors can still select `hash(...)`-like expressions themselves.

### 3. How rules are applied

For a caller with rules, on every result it receives - `/q/<name>`, `/execute_sql`, streamed exports, MCP tools,
exports:

1. **Trace** each output column to its source columns with `sqlglot` lineage, in the connection's dialect, after
   parameters are bound. A column computed from several sources is traced to all of them.
2. **Apply** each column's strictest matching rule (`drop` > `null` > `mask` > `hash`). A column computed from a masked
   source is masked as a whole, whatever the expression (`lower(email)` is hashed like `email`).
3. **Fail closed:** if any output column can't be traced - a dialect `sqlglot` can't parse, a table function, a
   column from a view or a `SELECT *` whose table the parser can't expand without the schema - the request is
   refused with `403 mask_unresolvable`, naming the column. `SELECT *` is expanded with the connection's own schema
   (`schema.py`) where it can be. A key without rules is never traced - no cost, no new refusals.
4. **MongoDB:** a `find()` can't rename fields, so rules match document fields by name (`customers.email` matches
   field `email` of collection `customers`).

Applied **before** rendering, on the rows the database returned: every output format, paging and streaming see the
same masked rows. The **cache key includes a fingerprint of the caller's rules**, so callers with different rules
never share a cached response.

### 4. Knowing it happened

- Each run's history entry records `masked`: which output columns were masked, with which action - so "was
  `national_id` ever returned to this partner?" is answerable from history.
- `GET /catalog` tells a caller which of each query's columns it gets masked, where the query's columns are known.
- The Access map marks keys and roles that carry rules.

### 5. What it doesn't cover

- **Inference.** A caller who may run arbitrary SQL can ask questions *about* a masked column without selecting it:
  `WHERE email = 'ceo@corp.com'` returns a row or not, `GROUP BY email` counts distinct values. Masking hides values
  in results; it does not stop filtering or aggregating on a column. For that, keep the column out entirely
  (`allowed_tables` on a view without it) or give the caller saved queries only. This is said plainly in the docs.
- **Automatic PII detection.** Rules are declared. Suggesting rules from column names or values may come later.

## Consequences

- **New:** a `mask` field on keys, roles and exports; `mask_unresolvable` (403); `QUERYAPIGATE_MASK_KEY`; `masked` in
  history entries; masking in the cache key. Callers without rules are unaffected in behaviour and cost.
- **Needs `sqlglot`** (the `flow` extra, in the Docker image) for any caller with rules on a SQL connection;
  without it such a caller is refused, never served unmasked.
- **Experimental in its first release**: lineage on real queries will find cases to tune, and the refusal rule may
  be too strict or not strict enough before it settles.
- **The 1.0 contract** later gains the rule shape and the actions.

## Delivery

About a week, in slices:

1. `masking.py`: rule matching, the four actions, the hash secret; lineage tracing per dialect with schema-backed
   `SELECT *` expansion, and the refusal - tested hard against aliases, expressions, CTEs, subqueries, joins, unions
   and window functions in every supported dialect.
2. Applying it: REST, streaming, MCP; the cache key; history's `masked`.
3. The `mask` field on keys and roles (API, validation, audit) and on exports.
4. Console: rules in the key and role forms; masked columns shown in the catalog and the Access map.
5. A how-to guide ("Hide personal data from a partner or an AI agent"), the threat model, changelog.

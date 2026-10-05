# Security Policy

QueryAPIGate executes SQL on behalf of whoever can reach it, so please treat it as a sensitive piece of infrastructure.
This page is an operational checklist; for the reasoning behind each control - what's actually being defended
against, how it works, and where it stops - see [documentation/THREAT_MODEL.md](documentation/THREAT_MODEL.md).

## Reporting a vulnerability

Please **do not open a public issue**. Report it privately through GitHub's
["Report a vulnerability"](https://github.com/AnanthaRajuC/QueryAPIGate/security/advisories/new) form on this repository,
or email arcswdev@gmail.com. You can expect an acknowledgement within a few days.

## Supported versions

Security fixes are made against the latest released version.

## Hardening checklist for deployments

- Set `QUERYAPIGATE_API_KEY`, and put the service behind TLS (a reverse proxy) - the key is sent in a header. It is a
  full-access admin key; give anyone or anything that only needs to run queries a scoped key instead
  (`POST /api/v1/api-keys`), limited to specific connections and, separately, to read-only access.
- Give the people (and pipelines) who manage the server their own admin tokens, with the narrowest role that fits -
  `queryapigate admins create NAME --role owner` for the first - and then keep the shared key sealed as a break-glass
  key, or remove it ([guide 43](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/how-to/43-give-your-team-their-own-admin-access.md)).
- Leave `QUERYAPIGATE_ALLOW_WRITES` unset unless you really need writes, and connect with a database account that only
  has the privileges the API should have. The read-only guard - including the single-statement check it relies on -
  is defence in depth, not a substitute for grants; it is dialect-aware (MySQL/ClickHouse honour backslash escapes
  in string literals by default, PostgreSQL/SQLite/H2 do not) and is exercised by a fuzz test suite
  (`tests/test_sql_guard_fuzz.py`), but a regex-based guard can never be a full SQL parser for every server mode.
- Keep credentials out of `db_connections.json`: use `"password": "${ENV_VAR}"` references.
- Do not enable `QUERYAPIGATE_DEBUG` on a reachable host.
- Set `QUERYAPIGATE_RATE_LIMIT` on anything reachable beyond a trusted network; it also throttles API key guessing.
- Behind a reverse proxy, set `QUERYAPIGATE_TRUST_PROXY` to the number of proxies so limits apply per real client - and
  leave it at `0` when clients connect directly, otherwise they can forge `X-Forwarded-For` to evade the limit.
- Only enable `QUERYAPIGATE_CORS_ORIGINS` for sites you control, and never `*` without an API key.
- Keep the query time limit (`QUERYAPIGATE_QUERY_TIMEOUT`, 30 seconds by default) and `QUERYAPIGATE_MAX_PAGE_SIZE` so one
  expensive request cannot monopolise the service; setting the timeout to `0` removes that protection.
- Prefer bound `:name` parameters over `{name}` text placeholders.
- Note that `GET /api/v1/connections` reveals hosts, ports, users and database names (passwords are masked).

# How-to guides

Task-oriented, step-by-step guides - "I want to do X," with runnable commands and sample
requests/responses, not a full feature reference (see [documentation/](../documentation/) and
[API.md](../documentation/API.md) for that). Each guide in this folder covers exactly one scenario end to
end.

Guides marked ✅ are written and linked. The others are planned topics, not written yet - until they are, the
[documentation](../documentation/) covers the features they are about.

## Getting started

1. ✅ **Turn your first SQL query into a REST API** - connect a database, run a query in API Designer, save
   it as an endpoint, call it with `curl`. The 10-minute on-ramp. See
   [01-turn-your-first-sql-query-into-a-rest-api.md](01-turn-your-first-sql-query-into-a-rest-api.md).
2. ✅ **Connect to MySQL, PostgreSQL, ClickHouse, SQLite, DuckDB or H2** - the Connections screen or the API,
   one worked example per dialect. See [02-connect-a-database.md](02-connect-a-database.md).
3. **Connect to anything else via generic JDBC** (experimental) - Oracle, SQL Server, Snowflake, DB2, or any JDBC-
   compliant database QueryAPIGate has no native driver for.
4. ✅ **Connect to MongoDB** (experimental) - `find()`-only saved queries, filters, projections, sort. See
   [04-connect-to-mongodb.md](04-connect-to-mongodb.md).
5. ✅ **Try it without any setup, using the built-in example APIs** - `queryapigate examples load`, what it
   installs, and how to explore it in the admin UI. See
   [05-try-the-built-in-example-apis.md](05-try-the-built-in-example-apis.md).

## Building and shaping an API

6. ✅ **Use bound parameters safely** (`:name` vs `{name}`) - what goes in a saved query, required vs.
   optional, defaults, validation rules (regex, enum, min/max). See
   [06-use-bound-parameters-safely.md](06-use-bound-parameters-safely.md).
7. ✅ **Group related queries into a collection**, and grant a key access to a whole collection at once
   instead of naming queries one by one. See
   [07-group-queries-into-a-collection.md](07-group-queries-into-a-collection.md).
8. ✅ **Cache a saved query's response** (`cache_ttl`) - when it's worth it, how a hit differs from a miss,
   `X-Cache`/`ETag` behavior. See [08-cache-a-saved-query.md](08-cache-a-saved-query.md).
9. ✅ **Export a large result without running out of memory** (`?stream=true`) - CSV/TSV/NDJSON, what makes a
   query eligible, verifying it's actually constant-memory. See
   [09-stream-a-large-export.md](09-stream-a-large-export.md).
10. ✅ **Allow a saved query to write data** (`INSERT`/`UPDATE`/`DELETE`) - `allow_writes`,
    `allowed_write_ops`, the safety rails, and why this is off by default. See
    [10-allow-a-saved-query-to-write-data.md](10-allow-a-saved-query-to-write-data.md).
11. ✅ **Browse a connection's schema before writing a query** - the API Designer schema browser, or the same
    thing over the API (`GET /connections/<name>/schema`). See
    [11-browse-a-connections-schema.md](11-browse-a-connections-schema.md).
12. ✅ **Export a Postman collection for a set of APIs** - one click, share with a team, no manual request
    building. See [12-export-a-postman-collection.md](12-export-a-postman-collection.md).

## Security and access control

13. ✅ **Set up your first scoped API key** - stop using the admin key for everything; create a key limited
    to one connection, read-only. See [13-set-up-a-scoped-api-key.md](13-set-up-a-scoped-api-key.md).
14. **Give an external partner access to exactly one query, nothing else** (`queries` grant) - the
    narrowest possible scope, no connection access at all.
15. **Create a role and stamp out several similarly-scoped keys from it** - roles vs. keys, what changing a
    role does (and doesn't) do to keys already created from it.
16. **Restrict a key to specific tables** (`allowed_tables`) - what's enforced, which dialects support it,
    what happens on an unsupported one.
17. **Rate-limit a key, or the whole server** - per-key vs. server-wide limits, reading the
    `X-RateLimit-*` headers, what `QUERYAPIGATE_TRUST_PROXY` is actually for.
18. **Restrict a key to specific source IPs** (`allowed_ips`) - IPv4/IPv6, CIDR ranges, what "fails
    closed" means here.
19. **Give a key an expiry date** - temporary contractor/trial access that revokes itself.
20. **Read the audit log to answer "who changed this and when"** - `GET /api/v1/audit`, the admin UI's Audit
    tab, exporting it externally for permanent retention.
21. **Verify who can reach what, before you find out the hard way** - the Access map, `GET /catalog`, and
    reasoning about a key's *effective* reach across `connections`/`queries`/`collections`.
22. **Encrypt connection passwords at rest** (`QUERYAPIGATE_SECRET_KEY`) - generating a Fernet key, what it
    does and doesn't protect, key rotation.
23. **Put QueryAPIGate behind a reverse proxy with real TLS** - Caddy/nginx/Traefik, `QUERYAPIGATE_TRUST_PROXY`,
    what breaks if you skip this.

## MCP (AI agent access)

24. ✅ **Let an AI agent call your saved queries via MCP** - `queryapigate mcp`, configuring Claude Desktop/
    another MCP client, listing and calling a tool. See
    [24-let-an-agent-call-your-queries-via-mcp.md](24-let-an-agent-call-your-queries-via-mcp.md).
25. **Let an agent explore your schema and run ad-hoc SQL** (`list_tables`/`execute_sql`) - always
    read-only, scoped by the same `connections` grant as REST.
26. **Read structured MCP results properly** (`outputSchema`/`structuredContent`) - what a client should
    parse instead of the text block, the `{rows, truncated}` / `{tables, truncated}` envelope.
27. **Check whether your MCP server is actually running, from the admin UI** - the Settings > MCP server
    reachability check and live tools panel.

## Live updates (SSE)

28. ✅ **Build a client that watches your saved queries run in real time** (`GET /events`, experimental) - `fetch()` over
    `EventSource`, reading `data:`/keepalive frames, reconnect-on-drop. See
    [28-build-a-client-that-watches-queries-run.md](28-build-a-client-that-watches-queries-run.md).
29. **Give each user of your own app (e.g. a mobile app) their own private activity feed** - per-key SSE
    filtering, one scoped key per login, verifying isolation between two users.

## Exporting and scheduling

30. **Run a scheduled export with cron, systemd, or a Kubernetes CronJob** - `queryapigate export`, no
    server or API key needed, `{date}`/`{name}` filename templating.
31. **Get the exact CLI command for a specific saved query** - the API Repository's CLI tab, parameter
    placeholders.

## Operating in production

32. **Deploy with Docker for real** - the production `docker-compose.yml` shape, secrets in an env file,
    persistent volume, health checks (distinct from the one-command demo compose file).
33. **Back up and restore the store** - `queryapigate backup` while the server runs, the PostgreSQL store's
    `pg_dump`, restoring onto a fresh instance, and what a backup doesn't contain.
34. **Upgrade to a new version safely** - reading the versioning policy, what "breaking" means pre-1.0,
    acting on deprecation warnings, pinning `X.Y.Z` instead of `latest`.
35. **Wire up Prometheus and Grafana** - scrape config, importing the bundled dashboard, what each panel
    means.
36. **Read structured logs and trace one request end to end** (`X-Request-Id`, `QUERYAPIGATE_JSON_LOGS`) -
    tying a slow/failed run in the admin UI back to a log line.
37. **Diagnose and tune the connection pool** - `QUERYAPIGATE_POOL_SIZE`/`POOL_IDLE_TIMEOUT`, reading
    `queryapigate_pool_idle_connections`, symptoms of it being too small or too large.
38. **Allow a browser-based frontend to call the API directly** (CORS) - `QUERYAPIGATE_CORS_ORIGINS`, why
    it's not authentication, the `*`-without-a-key trap.

## Reference walkthroughs

39. **A worked walkthrough of the example APIs** - what each of the 11 example queries, 4 collections and
    5 roles actually demonstrate, and how to read them as a template for your own setup.
40. **Read this project's threat model before deciding how to expose it** - what's actually defended
    against, what's the operator's job, where to stop trusting the regex SQL guard alone.

---

Numbering is just for reference - not a priority order. Want to write one of the planned guides? See
[CONTRIBUTING.md](../CONTRIBUTING.md): one scenario per file, named `NN-what-it-does.md`, with every command
and response run against a real server.

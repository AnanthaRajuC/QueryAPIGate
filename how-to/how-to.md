# How-to guides

Task-oriented, step-by-step guides - "I want to do X," with runnable commands and real requests and responses, not a
full feature reference (see [documentation/](../documentation/) and [API.md](../documentation/API.md) for that). Each
guide covers one scenario end to end, and every command and response in it was run against a real server.

## Getting started

1. [**Turn your first SQL query into a REST API**](01-turn-your-first-sql-query-into-a-rest-api.md) - connect, write
   a query in the API Designer, save it as an endpoint, call it with `curl`. The 10-minute on-ramp.
2. [**Connect to PostgreSQL, MySQL, SQLite, DuckDB, ClickHouse or H2**](02-connect-a-database.md) - the Connections
   screen or the API, one worked example per database.
3. [**Connect to anything else via generic JDBC**](03-connect-via-generic-jdbc.md) (experimental) - Oracle, SQL
   Server, DB2, Snowflake, or any database with a JDBC driver, and what doesn't work on it.
4. [**Connect to MongoDB**](04-connect-to-mongodb.md) (experimental) - `find()`-only saved queries, filters,
   projections, sort.
5. [**Try it without any setup, using the built-in example APIs**](05-try-the-built-in-example-apis.md) -
   `queryapigate examples load`, what it installs, and how to explore it in the Console.

## Building and shaping an API

6. [**Use bound parameters safely**](06-use-bound-parameters-safely.md) - `:name` vs `{name}`, required and optional,
   defaults, validation rules.
7. [**Group related queries into a collection**](07-group-queries-into-a-collection.md), and grant a key the whole
   collection at once.
8. [**Cache a saved query's response**](08-cache-a-saved-query.md) - `cache_ttl`, `X-Cache`/`ETag`, and when it's
   worth it.
9. [**Export a large result without running out of memory**](09-stream-a-large-export.md) - `?stream=true`, measured.
10. [**Allow a saved query to write data**](10-allow-a-saved-query-to-write-data.md) - `allow_writes`,
    `allowed_write_ops`, and the layered checks.
11. [**Browse a connection's schema before writing a query**](11-browse-a-connections-schema.md) - the schema
    browser, or `GET /connections/<name>/schema`.
12. [**Export a Postman collection for a set of APIs**](12-export-a-postman-collection.md).

## Security and access control

13. [**Set up your first scoped API key**](13-set-up-a-scoped-api-key.md) - stop using the admin key for everything.
14. [**Give an external partner access to exactly one query**](14-give-a-partner-one-query-only.md) - a `queries`
    grant, no connection access at all.
15. [**Create a role and stamp out several keys from it**](15-create-a-role.md) - and why changing the role doesn't
    change those keys.
16. [**Restrict a key to specific tables**](16-restrict-a-key-to-tables.md) - `allowed_tables`, what's enforced and
    where.
17. [**Rate-limit a key, or the whole server**](17-rate-limit-a-key.md) - the headers, the `429`, and
    `QUERYAPIGATE_TRUST_PROXY`.
18. [**Restrict a key to specific source IPs**](18-restrict-a-key-to-ips.md) - `allowed_ips`, and which address
    counts.
19. [**Give a key an expiry date**](19-give-a-key-an-expiry-date.md) - access that ends by itself.
20. [**Read the audit log to answer "who changed this, and when"**](20-read-the-audit-log.md) - and keep it longer
    than 500 entries.
21. [**Verify who can reach what**](21-verify-who-can-reach-what.md) - the Access map, `GET /catalog`, and how grants
    add up.
22. [**Keep connection passwords out of the store**](22-encrypt-passwords-at-rest.md) - `${VAR}` references, or
    `QUERYAPIGATE_SECRET_KEY` encryption, and rotating the key.
23. [**Put QueryAPIGate behind a reverse proxy with real TLS**](23-put-it-behind-a-reverse-proxy.md) - Caddy or nginx,
    verified.
43. [**Give your team their own admin access**](43-give-your-team-their-own-admin-access.md) - named administrators
    with roles, their own tokens, and the shared key retired.

## MCP (AI agent access)

24. [**Let an AI agent call your saved queries via MCP**](24-let-an-agent-call-your-queries-via-mcp.md) -
    `queryapigate mcp`, connecting a client, listing and calling tools.
25. [**Let an agent explore your schema and run ad-hoc SQL**](25-mcp-ad-hoc-tools.md) - `list_tables` and
    `execute_sql`, always read-only.
26. [**Read structured MCP results properly**](26-mcp-structured-results.md) - `structuredContent`, `truncated`,
    error codes.
27. [**Check whether your MCP server is running, from the Console**](27-check-the-mcp-server-from-the-console.md).

## Live updates (SSE)

28. [**Build a client that watches your saved queries run in real time**](28-build-a-client-that-watches-queries-run.md)
    (experimental) - parsing the stream, reconnecting.
29. [**Give each user of your app their own private activity feed**](29-per-key-live-feeds.md) (experimental) -
    per-key and per-user streams, and resuming.

## Exporting and scheduling

30. [**Run a scheduled export with cron, systemd or a Kubernetes CronJob**](30-schedule-an-export.md) -
    `queryapigate export`, and how failures are reported.
31. [**Get the exact CLI command for a saved query**](31-get-the-cli-command-for-a-query.md) - the API Repository's
    CLI tab.
44. [**Deliver a daily file to a partner's bucket**](44-deliver-a-daily-file-to-a-partners-bucket.md) (experimental) -
    a destination, an incremental export, a schedule, and what happens when a run fails.

## Operating in production

32. [**Deploy with Docker for real**](32-deploy-with-docker.md) - Compose, TLS, secrets, a volume, health checks.
33. [**Back up and restore the store**](33-back-up-and-restore.md) - `queryapigate backup`, `pg_dump`, and what a
    backup doesn't contain.
34. [**Upgrade to a new version safely**](34-upgrade-safely.md) - what to read, back up and watch, and acting on
    deprecation warnings.
35. [**Wire up Prometheus and Grafana**](35-wire-up-prometheus-and-grafana.md) - scraping, the bundled dashboard,
    alert rules.
36. [**Read the logs and trace one request end to end**](36-trace-a-request-in-the-logs.md) - `X-Request-Id`, JSON
    logs, slow queries.
37. [**Diagnose and tune the connection pool**](37-tune-the-connection-pool.md) - measured, with symptoms and fixes.
38. [**Allow a browser-based frontend to call the API directly**](38-allow-a-browser-frontend-cors.md) - CORS, and
    why it isn't authentication.

## Reference walkthroughs

39. [**Read the example APIs as a template for your own**](39-walk-through-the-example-apis.md) - why each query,
    collection and role is set up the way it is.
40. [**Use the threat model to decide how to expose QueryAPIGate**](40-read-the-threat-model.md) - from your laptop
    to the internet, and the limits to plan around.

## Files and data lakes

41. [**Publish Parquet, CSV or JSON files in S3 as an API**](41-publish-files-in-s3-as-an-api.md) (experimental) -
    DuckDB reads them where they are; `allowed_paths`, credentials, views.

## Scaling out

42. [**Run several instances behind a load balancer**](42-run-several-instances.md) - Docker Compose or Kubernetes,
    PostgreSQL and Redis shared, failover and rolling updates.

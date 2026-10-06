# QueryAPIGate

### Turn SQL queries into secure, governed REST APIs.

<div style="position:relative;padding-bottom:56.25%;height:0;overflow:hidden;max-width:100%;margin-bottom:1.2em">
<iframe src="https://www.youtube-nocookie.com/embed/WWImFj4m95o" title="QueryAPIGate: demo" style="position:absolute;top:0;left:0;width:100%;height:100%;border:0" allow="accelerometer; encrypted-media; picture-in-picture" allowfullscreen loading="lazy"></iframe>
</div>

Every feature, one by one: [the full walkthrough](https://youtu.be/cTkv6smFtWA) (6 minutes, in chapters).

**QueryAPIGate** is a self-hosted service that runs SQL against your databases - and against Parquet, CSV and JSON
files on disk, S3, GCS or the web, through DuckDB - and returns the results as JSON, NDJSON, XML, YAML, CSV, TSV, Excel
or Parquet. Save a query once and it becomes a versioned endpoint
with typed, injection-safe parameters and run history - without writing a controller, a repository layer,
pagination, auth or serialization boilerplate for it.

Write SQL. Configure the query. Apply access controls. Get an API.

## See it in one request

~~~bash
$ curl 'http://127.0.0.1:5000/q/example_film_search?text=Harbor&page_size=2' -H 'X-API-Key: demo-key'
[{"film_id":48,"title":"Broken Harbor","category":"Action","rating":"PG-13"},{"film_id":44,"title":"Electric Harbor","category":"Documentary","rating":"G"}]

$ curl 'http://127.0.0.1:5000/q/example_top_films?top_n=2&category=Comedy&format=csv' -H 'X-API-Key: demo-key'
title,category,rating,rentals,rank
Electric Signal,Comedy,PG,977,1
Crimson Garden,Comedy,PG-13,631,2
~~~

Both endpoints above come from the bundled example APIs - no setup beyond the three commands below.

## Quickstart

~~~bash
pip install queryapigate
QUERYAPIGATE_API_KEY=demo-key queryapigate examples load    # also prints one scoped key per scenario
QUERYAPIGATE_API_KEY=demo-key queryapigate serve             # http://127.0.0.1:5000 - the Console is at /console
~~~

That's a running server with a small generated database (films, customers, rentals) and four worked example APIs -
reporting, dashboard, export and partner - so the requests above work against it immediately; `queryapigate examples
unload` removes them again. For your own database, see the full [Installation and setup
guide](documentation/INSTALLATION_AND_SETUP.md); with Docker, `docker compose up`.

## Why QueryAPIGate

- **No boilerplate.** A saved query becomes a documented, versioned REST endpoint - no controller,
  repository layer, pagination or serialization code to write for it.
- **Governed, not just exposed.** Scoped API keys, reusable permission roles, per-query write curation, table
  allow-lists, rate limiting, IP allowlisting, key expiry, and named administrators with roles, so the audit log
  says who changed what - see [Authentication and permissions](documentation/API.md#authentication-and-permissions)
  and [Administrators](documentation/API.md#administrators-and-admin-roles).
- **Read-only by default.** A single-statement SQL guard blocks writes and multi-statement injection unless
  a connection or key explicitly opts in, narrowed further to specific write operations if needed.
- **Handles small and huge results the same way.** Paginated JSON/CSV/XML/YAML/XLSX/Parquet for typical results,
  constant-memory streaming exports (`?stream=true`) for exports too large to hold in memory - see
  [Streaming exports](documentation/API.md#streaming-exports).
- **Every major database, one interface.** Native drivers for MySQL, PostgreSQL, ClickHouse, SQLite, H2, DuckDB
  and MongoDB, plus generic JDBC for anything else with a driver jar - and files as tables: point a connection at a
  folder or bucket of Parquet, CSV or JSON and each file becomes a view.
- **Observable from day one.** Structured logs, request IDs, per-key metrics, a Prometheus `/metrics`
  endpoint and alerts for what needs attention - see [Observability](documentation/API.md#observability).
- **One process or several.** Runs as one container, or as several instances behind a load balancer on a shared
  PostgreSQL store and Redis - with reference deployments for Docker Compose and Kubernetes.
- **A real admin UI included.** Manage connections, saved queries, API keys and roles, run ad-hoc SQL with a
  schema browser, and review the audit log - all from `/console`, with no separate tool to install.

## Where to go next

- [Installation and setup](documentation/INSTALLATION_AND_SETUP.md) - install, configure a data folder, and
  run in production behind gunicorn or Docker.
- [API reference](documentation/API.md) - the full HTTP API: saved queries, streaming, authentication and
  permissions, caching, observability, the admin UI.
- [Database connections](documentation/DATABASE_CONNECTION_CONFIGURATION.md) and [Query metadata
  management](documentation/QUERY_METADATA_MANAGEMENT.md) - configuring connections and saved queries in
  depth.
- [The full README on GitHub](https://github.com/AnanthaRajuC/QueryAPIGate#readme) - screenshots, the complete
  feature list, and the output-format support matrix by database.
- [Roadmap](BACKLOG.md) - what's shipped and what's planned, in priority order.
- [License](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/LICENSE) - source-available under FSL-1.1-MIT: free to use,
  not to resell as a competing product, and each version becomes MIT after two years. Versions up to 0.6.1 were MIT but are no longer distributed; 0.7.0 onward is FSL-1.1-MIT.

# QueryAPIGate

### Turn SQL queries into secure, governed REST APIs.

<div style="position:relative;padding-bottom:56.25%;height:0;overflow:hidden;max-width:100%;margin-bottom:1.2em">
<iframe src="https://www.youtube-nocookie.com/embed/WWImFj4m95o" title="QueryAPIGate: demo" style="position:absolute;top:0;left:0;width:100%;height:100%;border:0" allow="accelerometer; encrypted-media; picture-in-picture" allowfullscreen loading="lazy"></iframe>
</div>

Every feature, one by one: [the full walkthrough](https://youtu.be/cTkv6smFtWA) (6 minutes, in chapters).

**QueryAPIGate** is a self-hosted, single Flask service that runs SQL against your databases and returns the
results as JSON, NDJSON, XML, YAML, CSV, TSV or Excel. Save a query once and it becomes a versioned endpoint
with typed, injection-safe parameters and run history - without writing a controller, a repository layer,
pagination, auth or serialization boilerplate for it.

Write SQL. Configure the query. Apply access controls. Get an API.

## See it in one request

~~~bash
$ curl 'http://127.0.0.1:5000/q/example_film_search?text=Harbor&page_size=2'
[{"film_id":48,"title":"Broken Harbor","category":"Action","rating":"PG-13"},{"film_id":44,"title":"Electric Harbor","category":"Documentary","rating":"G"}]

$ curl 'http://127.0.0.1:5000/q/example_top_films?top_n=2&category=Comedy&format=csv'
title,category,rating,rentals,rank
Electric Signal,Comedy,PG,977,1
Crimson Garden,Comedy,PG-13,631,2
~~~

Both endpoints above came from the bundled example database - no setup beyond the four commands below.

## Quickstart

~~~bash
pip install queryapigate
git clone https://github.com/AnanthaRajuC/QueryAPIGate.git && cd QueryAPIGate/examples
cp db_connections.example.json db_connections.json
queryapigate serve
~~~

That's a running server with two sample SQLite databases and two saved queries already configured - the
exact requests above will work against it immediately. For your own database, see the full [Installation and
setup guide](documentation/INSTALLATION_AND_SETUP.md).

## Why QueryAPIGate

- **No boilerplate.** A saved query becomes a documented, versioned REST endpoint - no controller,
  repository layer, pagination or serialization code to write for it.
- **Governed, not just exposed.** Scoped API keys, reusable permission roles, per-query write curation, rate
  limiting, IP allowlisting, key expiry and a durable audit log of every administrative change - see
  [Authentication and permissions](documentation/API.md#authentication-and-permissions).
- **Read-only by default.** A single-statement SQL guard blocks writes and multi-statement injection unless
  a connection or key explicitly opts in, narrowed further to specific write operations if needed.
- **Handles small and huge results the same way.** Paginated JSON/CSV/XML/YAML/XLSX for typical results,
  constant-memory streaming exports (`?stream=true`) for exports too large to hold in memory - see
  [Streaming exports](documentation/API.md#streaming-exports).
- **Every major database, one interface.** Native drivers for MySQL, PostgreSQL, ClickHouse, SQLite, H2 and
  DuckDB, plus generic JDBC for anything else with a driver jar.
- **Observable from day one.** Structured logs, request IDs, per-key metrics and a Prometheus `/metrics`
  endpoint - see [Observability](documentation/API.md#observability).
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

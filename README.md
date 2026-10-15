# QueryAPIGate

[![CI](https://github.com/AnanthaRajuC/QueryAPIGate/actions/workflows/ci.yml/badge.svg)](https://github.com/AnanthaRajuC/QueryAPIGate/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/AnanthaRajuC/QueryAPIGate/branch/main/graph/badge.svg)](https://codecov.io/gh/AnanthaRajuC/QueryAPIGate)
[![PyPI](https://img.shields.io/pypi/v/queryapigate.svg)](https://pypi.org/project/queryapigate/)
[![PyPI downloads](https://img.shields.io/pypi/dm/queryapigate.svg)](https://pypi.org/project/queryapigate/)
[![License: FSL-1.1-MIT](https://img.shields.io/badge/license-FSL--1.1--MIT-blue.svg)](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/LICENSE)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
[![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/AnanthaRajuC/QueryAPIGate/badge)](https://scorecard.dev/viewer/?uri=github.com/AnanthaRajuC/QueryAPIGate)
[![SLSA 2](https://slsa.dev/images/gh-badge-level2.svg)](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/SECURITY.md#slsa-level)

### Turn SQL queries into secure, governed REST APIs.

[**Full documentation**](https://AnanthaRajuC.github.io/QueryAPIGate/)

<p align="center">
  <a href="https://www.youtube.com/watch?v=WWImFj4m95o">
    <img src="https://raw.githubusercontent.com/AnanthaRajuC/QueryAPIGate/main/documentation/screenshots/demo-thumbnail.png" alt="Watch the demo: saved SQL queries as REST endpoints, scoped API keys, and the Move preview showing which keys gain or lose access" width="640">
  </a>
  <br>
  <sub>Every feature, one by one: <a href="https://youtu.be/cTkv6smFtWA"><b>the full walkthrough</b></a> (6 minutes, in chapters)</sub>
</p>

<p align="center">
  <a href="https://raw.githubusercontent.com/AnanthaRajuC/QueryAPIGate/main/documentation/screenshots/queryapigate-architecture.gif">
    <img src="https://raw.githubusercontent.com/AnanthaRajuC/QueryAPIGate/main/documentation/screenshots/queryapigate-architecture.gif"
         alt="One governed gate between your databases and everything that asks them for data: apps, partners, AI agents, people and pipelines come in through five front doors - REST endpoints, the MCP server, live events, exports and the CLI, the Management API - and every call passes the same governed core (identity, policy, execution) before it reaches PostgreSQL, MySQL, SQLite, DuckDB, ClickHouse, the experimental H2, JDBC and MongoDB, files through DuckDB, or deliveries to object storage" width="820">
  </a>
  <br>
  <sub>Every front door runs the same checks, so an AI agent's call is governed exactly like an app's. Write the SQL once; it's a REST endpoint, an MCP tool, a live event stream and a scheduled export.</sub>
</p>

<p align="center">
  <a href="https://raw.githubusercontent.com/AnanthaRajuC/QueryAPIGate/main/documentation/screenshots/what-happens-to-every-request.gif">
    <img src="https://raw.githubusercontent.com/AnanthaRajuC/QueryAPIGate/main/documentation/screenshots/what-happens-to-every-request.gif"
         alt="What happens to every request, one partner call followed through eight stages: identify the caller, apply its limits, check the grant, validate parameters, guard the SQL, run it, mask and format, record it - a stage that fails stops the call with a typed error code" width="820">
  </a>
  <br>
  <sub>One path for REST, MCP, events and exports. A stage that fails stops the call with a typed error code - except recording, which never blocks the response.</sub>
</p>

**QueryAPIGate** is a self-hosted service that runs SQL against your databases - and against Parquet, CSV and JSON files
on disk, S3, GCS or the web, through DuckDB - and returns the results as JSON, NDJSON, XML, YAML, CSV, TSV, Excel or
Parquet. Save a query once and it becomes a versioned endpoint with typed,
injection-safe parameters and run history - without writing a controller, a repository layer, pagination, auth or
serialization boilerplate for it.

Write SQL. Configure the query. Apply access controls. Get an API.

~~~bash
$ curl 'http://127.0.0.1:5000/q/example_film_search?text=Harbor&page_size=2' -H 'X-API-Key: demo-key'
[{"film_id":48,"title":"Broken Harbor","category":"Action","rating":"PG-13"},{"film_id":44,"title":"Electric Harbor","category":"Documentary","rating":"G"}]

$ curl 'http://127.0.0.1:5000/q/example_top_films?top_n=2&category=Comedy&format=csv' -H 'X-API-Key: demo-key'
title,category,rating,rentals,rank
Electric Signal,Comedy,PG,977,1
Crimson Garden,Comedy,PG-13,631,2
~~~

<p align="center">
  <a href="https://raw.githubusercontent.com/AnanthaRajuC/QueryAPIGate/main/documentation/screenshots/run-sql.png">
    <img src="https://raw.githubusercontent.com/AnanthaRajuC/QueryAPIGate/main/documentation/screenshots/run-sql.png"
         alt="The QueryAPIGate Console's API Designer: a joined SQL query with a bound :min_rentals parameter running against the example SQLite connection, the schema browser beside it and paginated results below" width="820">
  </a>
  <br>
  <sub>The built-in Console at <code>/console</code>: write SQL with completion and a schema browser, run it, then save it as an API - no separate tool to install.</sub>
</p>

More of the Console - Home, connections, keys and roles, history, the audit log, metrics, alerts, the access
map - screen by screen: [**a tour of the Console**](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/documentation/SCREENSHOTS.md).


## Contents

- [Why QueryAPIGate](#why-queryapigate)
- [Features](#features)
- [Install](#install) · [Quick start](#quick-start) · [Saving a query as an endpoint](#saving-a-query-as-an-endpoint)
- [Try it with Docker](#try-it-with-docker)
- [Documentation](#documentation)
- [Contributing](#contributing) · [Security](#security) · [License](#license) · [Contact](#contact)

---

## Why QueryAPIGate

Valuable SQL sits in reports, dashboards, ETL jobs and application code, with no way to a REST endpoint except a
backend service written around each query. QueryAPIGate makes the query itself the API: write the SQL once, and it is
a versioned endpoint with typed parameters, governed by who may call it, how often and what they may see - and the same
query is an MCP tool for AI agents, a live event stream and a scheduled export.

It doesn't replace an application backend; it removes the boilerplate when the requirement is "expose this query
safely". Use cases, how it compares to PostgREST, Hasura and others, and the thinking behind it:
[why QueryAPIGate](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/documentation/WHY.md).

## Features

- **[Saved, versioned queries as endpoints](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/documentation/API.md#save-a-query)** - `GET /q/<name>`, typed and
  injection-safe parameters, drafts and publishing, run history.
- **[Scoped API keys, roles and collections](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/documentation/API.md#authentication-and-permissions)** - per
  connection, per query or per collection; expiry, IP allow-lists, rate limits, quotas, table allow-lists.
- **[Named administrators and SSO](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/documentation/API.md#administrators-and-admin-roles)** - admin roles and
  tokens, Console sign-in through your identity provider, signed-in users' JWTs.
- **[Column masking](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/documentation/API.md#column-masking)** - hash, mask or drop what a caller mustn't see, traced
  through the SQL to the source column.
- **[An MCP server for AI agents](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/documentation/MCP.md)** - saved queries as tools, OAuth sign-in, write tools that
  wait for a person's approval.
- **[Eight database types, and files](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/documentation/DATABASE_CONNECTION_CONFIGURATION.md#support-matrix)** -
  PostgreSQL, MySQL, SQLite, DuckDB and ClickHouse; H2, JDBC and MongoDB; Parquet, CSV and JSON on S3, GCS, R2 or disk.
- **[Eight output formats, and streaming](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/documentation/API.md#streaming-exports)** - JSON, NDJSON, XML, YAML, CSV,
  TSV, Excel and Parquet; whole results streamed from the database cursor.
- **[Exports to object storage](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/documentation/API.md#exports-to-object-storage)** - scheduled, incremental, to
  S3, GCS, R2 or a folder.
- **[Live events](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/documentation/API.md#live-events-server-sent-events)** - every run as it happens, per key, with
  resume.
- **[A tamper-evident audit log](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/documentation/API.md#audit-log)** - a signed hash chain, streamed to your SIEM.
- **[Observability](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/documentation/API.md#observability)** - Prometheus metrics, OpenTelemetry tracing, alerts that
  clear themselves, a Grafana dashboard.
- **[Configuration as code](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/documentation/API.md#configuration-as-code)** and
  **[secrets from files and Vault](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/documentation/API.md#secrets-from-files-and-secrets-managers)**.
- **[OpenAPI and Postman](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/documentation/API.md#interactive-documentation)** - every saved query a typed endpoint in
  `/openapi.json` and `/docs`.
- **[Docker, Helm and several instances](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/documentation/DEPLOYMENT.md)** - one PostgreSQL store, Redis for shared
  limits; [signed releases with SLSA provenance](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/SECURITY.md#verify-a-release).

Every feature in detail: [features](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/documentation/FEATURES.md). Every setting:
[configuration](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/documentation/CONFIGURATION.md).

## Install

~~~bash
pip install "queryapigate[postgres]"         # pick the drivers you need: mysql, postgres, clickhouse, h2, duckdb
# or everything:                        pip install "queryapigate[all]"
~~~

SQLite needs no extra driver. H2 and generic JDBC connections (`queryapigate[h2]`) also need a Java runtime; H2's own
driver jar is bundled, a JDBC connection to another vendor brings its own. DuckDB (`queryapigate[duckdb]`) needs no
external runtime either - it's a native Python extension, same as SQLite. `queryapigate[encryption]` adds
[encryption at rest](documentation/API.md#encryption-at-rest-for-connection-passwords) for connection passwords -
only needed if you set `QUERYAPIGATE_SECRET_KEY`.
From a clone: `pip install -e ".[dev]"`. Or use Docker - see [below](#try-it-with-docker).

## Quick start

Fastest: `queryapigate examples load && queryapigate serve` installs four worked [example APIs](documentation/EXAMPLES.md)
(reporting, dashboard, export, partner) you can try, and `queryapigate examples unload` removes them again. Loading
them creates a real API key per scenario, which also means the server now requires a key for every request - set
`QUERYAPIGATE_API_KEY` yourself first so you have an admin key on hand for ad-hoc SQL too:

~~~bash
QUERYAPIGATE_API_KEY=demo-key queryapigate examples load    # also prints one scoped key per scenario - store them now
QUERYAPIGATE_API_KEY=demo-key queryapigate serve             # http://127.0.0.1:5000
~~~

The examples come with a small generated `examples` database (films, customers and rentals), so you can also run ad-hoc SQL
straight away:

~~~bash
curl -X POST 'http://127.0.0.1:5000/execute_sql?page_size=3' -H 'X-API-Key: demo-key' -H 'Content-Type: application/json' \
     -d '{"sql": "SELECT film_id, title, rating FROM film WHERE film_id > :min", "params": {"min": 10}, "connection_name": "examples"}'
~~~

Open <http://127.0.0.1:5000/docs> for the interactive API reference, or <http://127.0.0.1:5000/console> for the
admin UI to manage connections and saved queries and run ad-hoc SQL without leaving the browser.

For your own databases, run `queryapigate init` in an empty folder: it creates `queryapigate.db` with an
inactive template connection for every supported database type. Edit them (admin UI, or `PATCH
/api/v1/connections/<name>`), set `"active": true`, and start the server there.

## Saving a query as an endpoint

This uses the `examples` connection that `queryapigate examples load` sets up:

~~~bash
curl -X POST http://127.0.0.1:5000/api/v1/queries -H 'X-API-Key: demo-key' -H 'Content-Type: application/json' -d '{
  "name": "film_by_id",
  "sql": "SELECT * FROM film WHERE film_id = :id",
  "parameters": {"id": {"type": "int", "min": 1, "max": 60, "description": "Film id"}},
  "connection_name": "examples",
  "author": "me", "description": "Look up a film",
  "publish": true
}'

curl 'http://127.0.0.1:5000/q/film_by_id?id=7&format=yaml' -H 'X-API-Key: demo-key'
curl 'http://127.0.0.1:5000/q/film_by_id?id=0' -H 'X-API-Key: demo-key'
# {"error": "Invalid parameters: id must be at least 1", "errors": {"id": "must be at least 1"}}
~~~

Rules: `type` (`int`, `float`, `str`, `bool`), `default`, `required`, `enum`, `min`/`max`, `min_length`/`max_length`,
`pattern` and `description` - see [the API reference](documentation/API.md#parameter-rules).

`POST /api/v1/queries/film_by_id/versions` adds version 2 - a draft until published (`"publish": true`, or
`POST .../publish`); `DELETE /api/v1/queries/film_by_id/versions/1` removes one version.

Add `"cache_ttl": 60` to cache a response for that many seconds (`X-Cache: HIT`/`MISS`, `ETag`, `Cache-Control`) -
opt-in, and never used for a query that writes. See
[Response caching](documentation/API.md#response-caching).

## Try it with Docker

Every release is published for `linux/amd64` and `linux/arm64`, signed:

~~~bash
docker run -p 5000:5000 -v queryapigate-data:/data -e QUERYAPIGATE_API_KEY=change-me ghcr.io/anantharajuc/queryapigate:latest
~~~

Or start it in front of a PostgreSQL database seeded with sample films, with
[`docker-compose.yml`](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/docker-compose.yml):

~~~bash
docker compose up --build
curl -H 'X-API-Key: demo-key' 'http://127.0.0.1:5000/q/films_by_rating?rating=PG&max_length=90'
~~~

Open <http://127.0.0.1:5000/docs>, paste `demo-key` into the box at the top, and both saved queries are there as
endpoints; `docker compose down -v` cleans up.

The image tags, volumes and a real deployment - TLS, secrets, backups, Prometheus and Grafana, several instances,
Kubernetes: [deployment](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/documentation/DEPLOYMENT.md).

## Documentation

The [documentation site](https://AnanthaRajuC.github.io/QueryAPIGate/) has all of it; on GitHub:

| | |
|---|---|
| **Start** | [Installation and setup](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/documentation/INSTALLATION_AND_SETUP.md) · [Configuration](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/documentation/CONFIGURATION.md) · [Example APIs](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/documentation/EXAMPLES.md) · [How-to guides](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/how-to/how-to.md) |
| **Reference** | [API](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/documentation/API.md) · [MCP server](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/documentation/MCP.md) · [Database connections](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/documentation/DATABASE_CONNECTION_CONFIGURATION.md) · [Query metadata](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/documentation/QUERY_METADATA_MANAGEMENT.md) · [Features](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/documentation/FEATURES.md) |
| **Run it** | [Deployment](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/documentation/DEPLOYMENT.md) · [Technology stack](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/documentation/TECHNOLOGY_STACK.MD) · [A tour of the Console](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/documentation/SCREENSHOTS.md) |
| **Security** | [Security policy](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/SECURITY.md) · [Threat model](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/documentation/THREAT_MODEL.md) · [Where data goes](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/documentation/DATA_PROTECTION.md) |
| **Project** | [Why QueryAPIGate](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/documentation/WHY.md) · [Changelog](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/CHANGELOG.md) · [Roadmap](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/BACKLOG.md) · [Architecture decisions](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/documentation/adr/README.md) |

## Contributing

Contributions are welcome - database drivers, security, performance, the Console, documentation, tests. Please read
[CONTRIBUTING.md](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/CONTRIBUTING.md) first: the development setup, the tests, and how a change gets in.

## Security

Report a vulnerability privately - see [SECURITY.md](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/SECURITY.md), which also has the hardening checklist for a
deployment and how to verify a release.

## Third-party components

The wheel bundles the [H2 Database](https://h2database.com) JDBC driver (MPL 2.0 / EPL 1.0). The example data is generated
locally by `queryapigate examples load`; no third-party datasets are included.

## License

QueryAPIGate is **source-available**, under the
[Functional Source License, Version 1.1, MIT Future License](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/LICENSE)
(`FSL-1.1-MIT`) © Anantha Raju C. It is not an OSI-approved open source license.

In plain terms - the [LICENSE](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/LICENSE) is the only authoritative text:

- **You may** use, copy, modify and redistribute it for any purpose that is not a *Competing Use* - including running it
  inside your own company, non-commercial education and research, and professional services you provide to someone who
  is themselves licensed to use it.
- **You may not** make it available to others in a commercial product or service that substitutes for QueryAPIGate or
  offers the same or substantially similar functionality (for example, selling it, or hosting it as a paid service).
- **Each version becomes MIT** on the second anniversary of the date it was released, and can then be used under MIT terms.
- **Versions up to and including 0.6.1** were released under the MIT license. They are no longer distributed (and the
  repository history was reset in 0.7.0), but a copy you already obtained under MIT remains yours under those terms.

Need something the license does not cover, such as offering it as part of a hosted service? Get in touch - commercial
licensing may be available. This is a summary, not legal advice.

## Contact

Anantha Raju C - [@anantharajuc](https://twitter.com/anantharajuc) - arcswdev@gmail.com

Project link: <https://github.com/AnanthaRajuC/QueryAPIGate>

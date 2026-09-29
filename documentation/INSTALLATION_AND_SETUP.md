# Installation and setup

## Requirements

| Component | Version | Purpose |
|-----------|---------|---------|
| Python | 3.9+ | Runtime |
| Java | 11+ (only for H2) | Runs the H2 JDBC driver via JPype |

## Install

~~~bash
pip install queryapigate                     # SQLite only
pip install "queryapigate[postgres,mysql]"   # add the drivers you need
pip install "queryapigate[all]"              # every driver
~~~

Available extras: `mysql`, `postgres`, `clickhouse`, `h2`, `all`, `server` (gunicorn) and `dev`.
Each database driver is imported only when a connection of that type is used, so you never need drivers you do not use.

| Database | Driver | Extra |
|----------|--------|-------|
| MySQL | `mysql-connector-python` | `mysql` |
| PostgreSQL | `psycopg2-binary` | `postgres` |
| ClickHouse | `clickhouse-driver` | `clickhouse` |
| SQLite | `sqlite3` (standard library) | - |
| H2 | `JayDeBeApi` + `JPype1` | `h2` |

### From source

~~~bash
git clone https://github.com/AnanthaRajuC/QueryAPIGate.git && cd QueryAPIGate
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
~~~

## Set up a data folder

QueryAPIGate keeps its state in one folder - `QUERYAPIGATE_HOME`, by default the current directory:

~~~
queryapigate.db      connections and saved queries (SQLite)
api_keys.json        scoped API keys
roles.json           named permission templates
audit_log.json       a durable record of administrative changes
~~~

~~~bash
mkdir my-api && cd my-api
queryapigate init            # writes queryapigate.db (a template connection per db type, all inactive)
# edit them: admin UI, or PATCH /connections - set "active": true on the ones you want
queryapigate serve           # http://127.0.0.1:5000
~~~

To try it without any database of your own, use the bundled examples instead:
`queryapigate init && queryapigate examples load && queryapigate serve`.

## Configuration

Behaviour is controlled by environment variables - see the table in the
[README](https://github.com/AnanthaRajuC/QueryAPIGate#configuration)
(`QUERYAPIGATE_HOME`, `QUERYAPIGATE_ALLOW_WRITES`, `QUERYAPIGATE_API_KEY`, `QUERYAPIGATE_MAX_PAGE_SIZE`, `QUERYAPIGATE_QUERY_TIMEOUT`,
`QUERYAPIGATE_POOL_SIZE`, `QUERYAPIGATE_POOL_IDLE_TIMEOUT`, `QUERYAPIGATE_CORS_ORIGINS`, `QUERYAPIGATE_RATE_LIMIT`,
`QUERYAPIGATE_TRUST_PROXY`, `QUERYAPIGATE_HOST`, `QUERYAPIGATE_PORT`, `QUERYAPIGATE_DEBUG`, `QUERYAPIGATE_H2_JAR`,
`QUERYAPIGATE_REDIS_URL`).

## Running in production

`queryapigate serve` uses Flask's development server. For production use gunicorn with **one worker** (saved-query and
connection files are protected by an in-process lock) and several threads, behind a TLS-terminating reverse proxy:

~~~bash
pip install "queryapigate[server]"
QUERYAPIGATE_API_KEY=change-me gunicorn --bind 127.0.0.1:5000 --workers 1 --threads 8 --timeout 120 "queryapigate.app:create_app()"
~~~

Behind a reverse proxy or load balancer, also set `QUERYAPIGATE_TRUST_PROXY=1` (the number of proxies) so rate limits and
redirects use the real client address and scheme.

Or use the published Docker image (`ghcr.io/anantharajuc/queryapigate`, with a `-h2` variant that includes Java) or the
[Dockerfile](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/Dockerfile) - see the README. The image sets
gunicorn's worker timeout to 120 seconds; keep it above
`QUERYAPIGATE_QUERY_TIMEOUT` if you run your own gunicorn.

## Shared response cache (Redis)

A saved query's `cache_ttl` (see the README) is served from an in-process cache by default - fast, zero
setup, but wiped by every restart and never shared if you run more than one instance behind a load
balancer. For an OLTP query that gets hit hundreds of times a second, set `QUERYAPIGATE_REDIS_URL` to point
at a Redis instance instead: the cache then survives a restart and is shared across every instance that
points at the same Redis, so a cache warmed by one instance serves every instance's traffic instead of each
duplicating the same database hits.

~~~bash
pip install "queryapigate[redis]"
QUERYAPIGATE_REDIS_URL=redis://localhost:6379/0 queryapigate serve
~~~

If Redis is briefly unreachable, a cached response is simply treated as a miss - the query still runs
against the real database, it's just not served from cache for that one request. Nothing here changes the
`--workers 1` recommendation above: that limit comes from the file-based connection/saved-query store, not
the cache, and is unaffected either way.

An optional sidecar service for `docker-compose.yml`:

~~~yaml
services:
  redis:
    image: redis:7-alpine
  queryapigate:
    # ...
    environment:
      QUERYAPIGATE_REDIS_URL: redis://redis:6379/0
    depends_on:
      - redis
~~~

## Scheduled exports to a file

`queryapigate export <query> --out <path>` runs a saved query and writes its full result to a file, entirely
in-process against `QUERYAPIGATE_HOME` - no server needs to be running, no HTTP round trip, no API key. It's built
for cron, a systemd timer or a Kubernetes CronJob to call, not a scheduler itself - scheduling, retries and
failure notification stay exactly where they already work well:

~~~bash
queryapigate export top_rented_films --out '/exports/{name}_{date}.csv'
~~~

`{name}` (the saved query's name) and `{date}` (`YYYY-MM-DD`) in `--out` are filled in; the target directory
is created if missing. `--format` is `csv` (default), `tsv` or `ndjson` - the same formats `?stream=true`
supports, since this calls the same streaming code path internally rather than shelling out to `curl`
against itself. `--connection` overrides the saved query's own default connection; `--param name=value`
(repeatable) supplies a required parameter. The result is written to a temporary file in the same directory
and renamed into place only once it's complete, so a failed run never leaves a partial or corrupt file at
the final path - and exits non-zero on any failure, so cron's own failure handling (mail, an alerting
integration, whatever the operator already has) works unmodified:

~~~bash
# crontab: every morning at 6am, mail on failure (cron's own default behaviour)
0 6 * * * /usr/local/bin/queryapigate export top_rented_films --out '/exports/{name}_{date}.csv'
~~~

## Verify

~~~bash
curl http://127.0.0.1:5000/health
curl http://127.0.0.1:5000/connections
~~~

## Upgrading from SQL2API

QueryAPIGate is the new name of SQL2API - the same project, renamed because `sql2api` was shared by a dozen
unrelated projects. It is a clean break with no compatibility aliases, so an existing deployment needs these
changes before it upgrades:

| What | Before (SQL2API) | After (QueryAPIGate) |
|------|------------------|----------------------|
| PyPI package | `pip install sql2api` | `pip install queryapigate` |
| Command | `sql2api serve` / `init` / `export` | `queryapigate serve` / `init` / `export` |
| Python import | `import sql2api` | `import queryapigate` |
| gunicorn target | `"sql2api.app:create_app()"` | `"queryapigate.app:create_app()"` |
| Environment variables | `SQL2API_*` (all of them) | `QUERYAPIGATE_*` |
| Prometheus metrics | `sql2api_*` | `queryapigate_*` |
| Docker image | `ghcr.io/anantharajuc/sql2api` | `ghcr.io/anantharajuc/queryapigate` |
| JSON log `logger` field | `sql2api` | `queryapigate` |

Your data folder needs no manual change: `api_keys.json`, `roles.json` and `audit_log.json` are read exactly
as before. `db_connections.json`/`saved_sql/` (if you're upgrading from before connections and saved
queries moved to `queryapigate.db`) are imported automatically, once, the first time the server or CLI runs
against that home - nothing to run by hand, and the original files are left untouched, never deleted.

**Rename every environment variable, especially `SQL2API_API_KEY`.** The old names are not read at all, and
an unset API key means an open server - so a server that still finds any `SQL2API_*` variable in its
environment refuses to start and lists what to rename, rather than silently running unprotected:

~~~
queryapigate: these settings use the old SQL2API_ prefix, which is no longer read:
SQL2API_API_KEY -> QUERYAPIGATE_API_KEY. Rename each one; ignoring them silently could leave the server
without an API key
~~~

To find them: `env | grep '^SQL2API_'`, and check any `.env` files, systemd units, Compose files, Kubernetes
manifests and CI settings, not just your shell. Prometheus queries, alert rules and Grafana panels that name
a `sql2api_*` metric need the same one-word change (the bundled dashboard already uses the new names). The
admin UI stores your API key under a new browser-storage name, so it asks for the key once more after the
upgrade.

## Running the tests

~~~bash
pip install -e ".[dev]"
ruff check .
python -m unittest discover -s tests -t .
~~~

Integration tests against real databases are enabled by setting `QUERYAPIGATE_IT_POSTGRES`, `QUERYAPIGATE_IT_MYSQL`,
`QUERYAPIGATE_IT_CLICKHOUSE` and/or `QUERYAPIGATE_IT_H2` to a JSON connection object - see the header of
`tests/test_integration.py`. `tests/test_sql_guard_fuzz.py` fuzzes the SQL guard and parameter binder with
[Hypothesis](https://hypothesis.readthedocs.io/) and always runs as part of the suite above.

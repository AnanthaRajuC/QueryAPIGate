# Production deployment (Docker)

A production-shaped setup: the published image, a real reverse proxy terminating TLS, secrets kept out of
the compose file, a persistent volume with a backup job, and Prometheus/Grafana wired to the metrics
QueryAPIGate already exposes. [`docker-compose.yml`](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/docker-compose.yml)
in the repo root is a **one-command demo** (open to localhost, a throwaway Postgres, a demo API key) - not
this. Start from the compose file below instead.

If you're installing from source or pip rather than Docker, see
[Installation and setup](INSTALLATION_AND_SETUP.md#running-in-production) instead; the gunicorn command and
every environment variable there apply here too - this page is about the container and everything around it.

## 1. The image

~~~bash
docker pull ghcr.io/anantharajuc/queryapigate:latest
~~~

| Tag | Contents |
|-----|----------|
| `X.Y.Z`, `latest` | QueryAPIGate with the MySQL, PostgreSQL and ClickHouse drivers (SQLite is built in) |
| `X.Y.Z-h2`, `latest-h2` | The same plus Java and the H2 driver - also the variant for a generic `jdbc` connection (mount your vendor's jar) |

**Pin to `X.Y.Z` in production, not `latest`.** An upgrade should be a deliberate, one-line version bump you
control, not something that happens on the next `docker compose pull`. Bump it the same way you'd bump any
other dependency - read the [Changelog](../CHANGELOG.md) first, especially around a minor version bump
(this project's pre-1.0 [equivalent of a major one](../CHANGELOG.md#versioning-and-compatibility)).

The image already does the things a production container should: runs as a non-root user (`uid 1000`),
ships a `HEALTHCHECK` against `/health`, and starts gunicorn with `--worker-class gthread` and one worker
(see [Why one worker](#why-one-worker-not-a-replica-count) below) - none of that needs reconfiguring.

## 2. Compose file

~~~yaml
# docker-compose.yml
services:
  queryapigate:
    image: ghcr.io/anantharajuc/queryapigate:1.2.3   # pin a real version - see above
    restart: unless-stopped
    env_file: .env                                    # secrets live here, never inline - see below
    environment:
      QUERYAPIGATE_TRUST_PROXY: "1"                    # one hop: the reverse proxy below
    volumes:
      - queryapigate-data:/data
    networks:
      - internal
    deploy:
      resources:
        limits:
          cpus: "1.0"
          memory: 512M

  # Reverse proxy: TLS termination, the only container with a published port. Caddy shown here for its
  # automatic HTTPS; nginx/Traefik work the same way - proxy to queryapigate:5000 with the real client
  # address forwarded.
  proxy:
    image: caddy:2-alpine
    restart: unless-stopped
    ports:
      - "443:443"
      - "80:80"                                        # ACME HTTP-01 challenge + HTTP->HTTPS redirect
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy-data:/data
    networks:
      - internal
    depends_on:
      - queryapigate

  # Optional - see "Shared response cache" below. Skip both this service and QUERYAPIGATE_REDIS_URL if
  # cache_ttl either isn't used or the built-in per-process cache is enough.
  redis:
    image: redis:7-alpine
    restart: unless-stopped
    networks:
      - internal

networks:
  internal:

volumes:
  queryapigate-data:
  caddy-data:
~~~

~~~caddyfile
# Caddyfile
api.example.com {
    reverse_proxy queryapigate:5000
}
~~~

That's it for TLS - Caddy requests and renews a real certificate automatically for `api.example.com` the
first time it starts, as long as DNS already points at this host and ports 80/443 are reachable from the
internet (for the ACME challenge). Swap in nginx or Traefik if you already run one of those; the only
requirement is that whatever terminates TLS forwards to `queryapigate:5000` and QueryAPIGate is told how
many proxy hops sit in front of it via `QUERYAPIGATE_TRUST_PROXY`, so rate limiting and redirects use the
real client address and scheme rather than the proxy's.

## 3. Secrets (`.env`)

~~~bash
# .env - not committed; chmod 600
QUERYAPIGATE_API_KEY=<a long random string>
PROD_DB_PASSWORD=<your database password>
~~~

`QUERYAPIGATE_API_KEY` is the admin key - full access, sent as the `X-API-Key` header. Generate a real one
(`openssl rand -hex 32`), not a word you'll remember; create scoped keys for anything that only needs to run
queries (`POST /api_keys`, from the admin UI or the API - see [API.md](API.md#permission-roles-templates)).

A connection's own password never goes in `db_connections.json` (or the admin UI's connection form) as
plain text - reference an environment variable instead, resolved from whatever's in the container's
environment (i.e. anything in `.env` above):

~~~json
{"connections": {"prod": {"db": "postgres", "host": "db.internal", "user": "app",
  "password": "${PROD_DB_PASSWORD}", "database": "app", "active": true}}}
~~~

`QUERYAPIGATE_SECRET_KEY` (a Fernet key) additionally encrypts every connection password *at rest* in
`queryapigate.db` itself, independent of the `${VAR}` convention above - worth setting if the volume or its
backups might be read by someone who shouldn't see connection credentials. It needs the `cryptography`
package, which **the published image does not include** (the server refuses to start with a clear error if
the key is set without it, rather than silently skipping encryption) - build your own image with the
`encryption` extra added to get it:

~~~dockerfile
# Dockerfile.encrypted - one line on top of the published image
FROM ghcr.io/anantharajuc/queryapigate:1.2.3
RUN pip install --no-cache-dir cryptography
~~~

~~~bash
docker build -t queryapigate-encrypted -f Dockerfile.encrypted .
# generate the key - no cryptography needed for this part, a Fernet key is just 32 random bytes, base64url-encoded
python3 -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"
~~~

Read the full [hardening checklist](../SECURITY.md#hardening-checklist-for-deployments) before going live -
`QUERYAPIGATE_ALLOW_WRITES`, `QUERYAPIGATE_RATE_LIMIT`, `QUERYAPIGATE_CORS_ORIGINS` and the rest all matter
for a reachable production instance and aren't repeated here.

## 4. First run

~~~bash
docker compose run --rm queryapigate queryapigate init   # writes a starter queryapigate.db into the volume
docker compose up -d
curl https://api.example.com/health
~~~

Run `init` *before* the first `up`, as a one-off (`run --rm`), not `exec` against the already-running
service: `queryapigate serve` initializes an empty `queryapigate.db` the moment it starts, and won't
retroactively pick up `init`'s template file once it already has - `docker compose exec ... init` after
`up` writes the file, but the connections it describes then only actually appear once you restart the
container. Running `init` first avoids that ordering trap entirely.

`init` writes one template connection per supported database type, all inactive - activate the ones you
need from the admin UI (`https://api.example.com/ui`) or `PATCH /connections`. Do **not** run
`queryapigate examples load` against a production instance; it's meant for trying the product, seeds a
throwaway SQLite database and five example API keys, and is documented as such in
[EXAMPLES.md](EXAMPLES.md).

## 5. Persistent data and backups

Everything QueryAPIGate owns - connections, saved queries, API keys, roles, the audit log - lives in one
file, `queryapigate.db`, inside the `/data` volume. It's SQLite in WAL mode, so a plain `cp` of that one
file can miss data still sitting in the `-wal` companion file; back it up with SQLite's own backup API
instead, which is WAL-aware and safe to run against a live database with no downtime. The image doesn't
include the standalone `sqlite3` CLI (it's a slim Python base), but Python's own `sqlite3` module - already
there, since QueryAPIGate itself depends on it - does the same job in one line:

~~~bash
docker compose exec queryapigate python3 -c "
import sqlite3
src = sqlite3.connect('/data/queryapigate.db')
dst = sqlite3.connect('/data/backup.db')
src.backup(dst)
dst.close(); src.close()"
docker compose cp queryapigate:/data/backup.db "./backups/queryapigate-$(date +%F).db"
docker compose exec queryapigate rm /data/backup.db
~~~

Run that on a schedule (host cron calling the two lines above, or a small sidecar container with its own
cron) and ship the result somewhere off the host. There's nothing else to back up - no separate config
files, no secrets in the volume (those live in `.env`, outside it).

## 6. Observability

`/metrics` (Prometheus text format) and the admin UI's own **Metrics** tab need no setup - see
[API.md's Observability section](API.md#observability) for the full metric list and what each one means.
For real history, trends and alerting, add a Prometheus scrape target and import the bundled Grafana
dashboard:

~~~yaml
# prometheus.yml
scrape_configs:
  - job_name: queryapigate
    static_configs:
      - targets: ["queryapigate:5000"]
~~~

Then *Dashboards → New → Import* in Grafana, uploading
[`documentation/grafana-dashboard.json`](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/documentation/grafana-dashboard.json)
from this repo - panels for request/query rate and latency (p50/p95/p99), error rate, rows returned, active
queries, pool occupancy and rate-limit rejections, built on exactly the metric names `/metrics` already
exposes. `/metrics` is public (no API key needed) precisely so a scraper doesn't need one either.

## 7. Shared response cache (optional)

A saved query's `cache_ttl` is served from an in-process cache by default - zero setup, but wiped on every
restart. If a query is hit often enough that a warm cache actually matters, point it at the `redis` service
in the compose file above instead:

~~~bash
# .env
QUERYAPIGATE_REDIS_URL=redis://redis:6379/0
~~~

See [Shared response cache](INSTALLATION_AND_SETUP.md#shared-response-cache-redis) for what changes (a
Redis outage degrades to a cache miss, never a failed request) and browse what's actually cached from the
admin UI's **Caching** tab.

## Why one worker (not a replica count)

The image runs gunicorn with **one worker** on purpose, and that's not a knob to turn up for more capacity.
The rate limiter and `/metrics` are per-process, in-memory state with no cross-worker or cross-replica
aggregation - two workers (or two containers) would each enforce rate limits and count metrics
independently, silently doubling effective limits and splitting the numbers Grafana shows. Every *durable*
store (`queryapigate.db`) is safe to read concurrently, but nothing here makes a second full instance a
horizontal-scaling story: two replicas each with their own `/data` volume are two independent servers with
their own connections, keys and audit log, not one logical service.

For more headroom on one instance, raise `--threads` (`gthread` already lets a slow request - a large
export, a slow query - not block every other connection) or give the container more CPU. The image's `CMD`
is fixed, so change the thread count with a `command:` override in the compose file rather than an
environment variable:

~~~yaml
    command: ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "1", "--worker-class", "gthread",
              "--threads", "16", "--timeout", "120", "--graceful-timeout", "30", "--access-logfile", "-",
              "queryapigate.app:create_app()"]
~~~

If you genuinely need redundancy, run a second, fully independent instance (its own volume, its own DNS
record or a failover-only load balancer in front of both) rather than a shared-state replica pair - and
keep the [Redis-backed cache](#7-shared-response-cache-optional) if the two need to agree on cached
responses at least, since that's the one piece of state this setup can actually share.

## Upgrading

1. Read the [Changelog](../CHANGELOG.md) entry for the target version, especially anything under a
   "Breaking" or "Upgrading" heading.
2. [Back up](#5-persistent-data-and-backups) first - always, even for a patch version.
3. Bump the pinned tag in `docker-compose.yml`, then `docker compose pull && docker compose up -d`.
4. Check `docker compose logs queryapigate` and `curl .../health`.

The `queryapigate.db` schema is migrated automatically on the container's first start against it - nothing
to run by hand. If you're upgrading from the project's old name (SQL2API), see
[Upgrading from SQL2API](INSTALLATION_AND_SETUP.md#upgrading-from-sql2api) first; environment variables
renamed and the server refuses to start until they're renamed too.

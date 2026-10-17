# Stage 1: build the QueryAPIGate Console (frontend/, ADR 0001). Only its static output is carried into the final
# image, so the image itself contains no Node.js. frontend/openapi.json is committed, so this stage needs no Python.
# Base images by digest (ADR 0018): a rebuild uses the same bytes; Dependabot proposes each new digest as a commit.
FROM node:22-slim@sha256:c3de60bf2f9dd0ac6370e6117950ff62d6e339527e7472301c9c78a017978392 AS console
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
# vite.config.ts writes to ../queryapigate/console_dist
RUN npm run build

FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1

# Build with --build-arg WITH_H2=true to add Java and the H2 driver.
ARG WITH_H2=false

LABEL org.opencontainers.image.title="QueryAPIGate" \
      org.opencontainers.image.description="Turn saved SQL queries into secure REST APIs and MCP tools, on PostgreSQL, MySQL, SQLite, DuckDB, ClickHouse and more." \
      org.opencontainers.image.source="https://github.com/AnanthaRajuC/QueryAPIGate" \
      org.opencontainers.image.licenses="Apache-2.0"

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN if [ "$WITH_H2" = "true" ]; then \
        apt-get update && apt-get install -y --no-install-recommends default-jre-headless \
        && rm -rf /var/lib/apt/lists/*; \
    fi

WORKDIR /app
COPY pyproject.toml README.md ./
COPY queryapigate ./queryapigate
COPY --from=console /queryapigate/console_dist ./queryapigate/console_dist
# The H2 driver the package bundles - not committed. One already in the build context with the pinned SHA-256 is
# used as it is; otherwise it's fetched from Maven Central or a mirror of it, and the build fails unless the hash
# matches (.github/scripts/fetch_h2_jar.py: the hash, the mirrors, the retries).
COPY .github/scripts/fetch_h2_jar.py /tmp/fetch_h2_jar.py
RUN python /tmp/fetch_h2_jar.py --dest ./queryapigate/lib && rm /tmp/fetch_h2_jar.py
# Every optional extra the documentation relies on, except H2 (its Java runtime is the -h2 variant's): DuckDB is a
# tier 1 database, sqlglot enforces allowed_tables, and redis, mcp, encryption, jwt and otel back features a deployment
# turns on with an environment variable or a second command - none of them should need a custom image.
RUN pip install ".[mysql,postgres,clickhouse,duckdb,mongo,flow,encryption,jwt,redis,mcp,otel,aws,server]" "gunicorn>=26" \
    && if [ "$WITH_H2" = "true" ]; then pip install ".[h2]"; fi

RUN useradd --create-home app && mkdir /data && chown app /data
USER app
# DuckDB's httpfs extension, for s3://, gs://, r2:// and http(s):// files in a connection's allowed_paths - installed now,
# so the container never downloads code at runtime (and works where it can't).
RUN python -c "import duckdb; duckdb.connect().execute('INSTALL httpfs')"

# queryapigate.db lives here (connections, saved queries, API keys, roles, audit log) - mount a volume to keep it.
ENV QUERYAPIGATE_HOME=/data
VOLUME /data
EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/health', timeout=3)"]

# One worker: /metrics is per process, so a scraper reaching a container with several workers would get one at random
# (and without Redis, rate limits are per process too) - scale out with more containers instead (DEPLOYMENT.md,
# Scaling out). --worker-class gthread makes --threads actually take effect (gunicorn's default "sync" class
# ignores it, handling one connection at a time) - required so a long-lived /events SSE connection (BACKLOG
# #43) never blocks every other request to the single worker for as long as that one client stays connected.
# The worker timeout must stay above QUERYAPIGATE_QUERY_TIMEOUT (30 s by default), or gunicorn would kill the worker
# at the same moment the database cancels a slow query. --no-control-socket: gunicorn 26's control interface, unused
# here, would otherwise write under the home folder - the only write outside /data and /tmp, so with it off the image
# runs on a read-only root filesystem (the Helm chart's default, ADR 0013).
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "1", "--worker-class", "gthread", "--threads", "8", \
     "--timeout", "120", "--graceful-timeout", "30", "--access-logfile", "-", "--no-control-socket", \
     "-c", "python:queryapigate.gunicorn_conf", \
     "queryapigate.app:create_app()"]

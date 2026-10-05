# Stage 1: build the QueryAPIGate Console (frontend/, ADR 0001). Only its static output is carried into the final
# image, so the image itself contains no Node.js. frontend/openapi.json is committed, so this stage needs no Python.
FROM node:26-slim AS console
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
# vite.config.ts writes to ../queryapigate/console_dist
RUN npm run build

FROM python:3.12-slim

# Build with --build-arg WITH_H2=true to add Java and the H2 driver.
ARG WITH_H2=false

LABEL org.opencontainers.image.title="QueryAPIGate" \
      org.opencontainers.image.description="Turn saved SQL queries into secure REST APIs and MCP tools, on PostgreSQL, MySQL, SQLite, DuckDB, ClickHouse and more." \
      org.opencontainers.image.source="https://github.com/AnanthaRajuC/QueryAPIGate" \
      org.opencontainers.image.licenses="FSL-1.1-MIT"

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
# Every optional extra the documentation relies on, except H2 (its Java runtime is the -h2 variant's): DuckDB is a
# tier 1 database, sqlglot enforces allowed_tables, and redis, mcp, encryption and jwt back features a deployment turns
# on with an environment variable or a second command - none of them should need a custom image.
RUN pip install ".[mysql,postgres,clickhouse,duckdb,mongo,flow,encryption,jwt,redis,mcp,server]" \
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
# at the same moment the database cancels a slow query.
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "1", "--worker-class", "gthread", "--threads", "8", \
     "--timeout", "120", "--graceful-timeout", "30", "--access-logfile", "-", "queryapigate.app:create_app()"]

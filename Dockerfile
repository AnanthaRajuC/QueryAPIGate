# Stage 1: build the QueryAPIGate Console (frontend/, ADR 0001). Only its static output is carried into the final
# image, so the image itself contains no Node.js. frontend/openapi.json is committed, so this stage needs no Python.
# Base images by digest (ADR 0018): a rebuild uses the same bytes; Dependabot proposes each new digest as a commit.
FROM node:22-slim@sha256:de170b39439eb0e5a3444ad7d60b015614a381e10fecae2e30ec9e735122f38d AS console
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
# vite.config.ts writes to ../queryapigate/console_dist
RUN npm run build

# Stage 2: the Rust data plane (ADR 0030), compiled on the build machine for the image's architecture - cross-compiled,
# not run under QEMU, where it would take most of an hour. Off unless started (guide 64); only the binary is carried
# over. The toolchain is the one dataplane/rust-toolchain.toml names (tests/test_dataplane.py checks), on the Debian
# release the Python image below is on, so the binary links against the same glibc. cargo-auditable records the
# crates in the binary, for the image's SBOM and scanners.
FROM --platform=$BUILDPLATFORM rust:1.98.1-slim-trixie@sha256:4cd829461bd5c4d511c32e269da9cb8929223b666519d8004e35fc8d1d771ab7 AS dataplane
ARG TARGETARCH
ARG BUILDARCH
RUN case "$TARGETARCH" in \
        amd64) echo x86_64-unknown-linux-gnu > /target; gnu=x86_64-linux-gnu; deb=amd64 ;; \
        arm64) echo aarch64-unknown-linux-gnu > /target; gnu=aarch64-linux-gnu; deb=arm64 ;; \
        *) echo "no data plane build for $TARGETARCH" >&2; exit 1 ;; \
    esac \
    && if [ "$TARGETARCH" != "$BUILDARCH" ]; then \
        apt-get update && apt-get install -y --no-install-recommends "gcc-$(echo $gnu | tr _ -)" "libc6-dev-$deb-cross" \
        && rm -rf /var/lib/apt/lists/* \
        && echo "$gnu" > /cross; \
    fi \
    && rustup target add "$(cat /target)" \
    && cargo install cargo-auditable --version 0.7.7 --locked
WORKDIR /dataplane
COPY dataplane/Cargo.toml dataplane/Cargo.lock ./
COPY dataplane/src ./src
# ring's C, and the link, with the target's compiler when cross-compiling; symbols stripped, the crate list kept
RUN target="$(cat /target)" \
    && if [ -f /cross ]; then \
        gnu="$(cat /cross)"; upper="$(echo "$target" | tr a-z- A-Z_)"; \
        export "CARGO_TARGET_${upper}_LINKER=$gnu-gcc" "CC_$(echo "$target" | tr - _)=$gnu-gcc"; \
    fi \
    && CARGO_PROFILE_RELEASE_STRIP=symbols cargo auditable build --release --locked --target "$target" \
    && cp "target/$target/release/queryapigate-dataplane" /queryapigate-dataplane

FROM python:3.12-slim@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704

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
COPY --from=dataplane /queryapigate-dataplane /usr/local/bin/queryapigate-dataplane
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
# Where gunicorn listens (gunicorn_conf.py). Behind the data plane, in a second container sharing this one's network,
# it's 127.0.0.1:5001 and the data plane takes 5000 - the health check below then reaches both (ADR 0030, guide 64).
ENV QUERYAPIGATE_BIND=0.0.0.0:5000
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
# runs on a read-only root filesystem (the Helm chart's default, ADR 0013). The threads (QUERYAPIGATE_THREADS, 8) and
# the access log (QUERYAPIGATE_ACCESS_LOG, on, to stdout) and the address (QUERYAPIGATE_BIND) come from
# queryapigate/gunicorn_conf.py, not flags here: a flag would beat the setting, and changing one would mean replacing
# this whole command.
CMD ["gunicorn", "--workers", "1", "--worker-class", "gthread", \
     "--timeout", "120", "--graceful-timeout", "30", "--no-control-socket", \
     "-c", "python:queryapigate.gunicorn_conf", \
     "queryapigate.app:create_app()"]

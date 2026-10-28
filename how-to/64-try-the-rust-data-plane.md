# How to try the Rust data plane in front of `/q/` (a prototype)

**Time:** 20 minutes, plus a Rust toolchain. **You'll end up with:** the data plane on your public port, serving your
saved-query reads itself, Python behind it doing everything else, and a way to see which calls it served.

The data plane is a second process, built from `dataplane/` in the repository. For the calls it serves, it measured 3
to 9 times the calls a second per core of Python on its own, with p99 2 to 20 times lower
([Performance, v5](../documentation/PERFORMANCE_V5.md)). It is a **prototype**
([ADR 0028](../documentation/adr/0028-rust-data-plane-prototype.md)): it isn't in the Docker image, the PyPI package
or the Helm chart yet. Try it on a test deployment, not in production.

## What it serves, and what it leaves to Python

**On a server with `QUERYAPIGATE_RATE_LIMIT`, `QUERYAPIGATE_TRUST_PROXY` or OpenTelemetry tracing
(`OTEL_EXPORTER_OTLP_ENDPOINT`) set, it serves nothing:** it forwards every call, since those need state or
headers it doesn't handle yet. Otherwise it serves a call itself when all of these hold, and forwards it to Python,
unchanged, when any doesn't:

- a `GET` or `POST` to `/q/<name>`, answered as JSON (no `?format=`, no streaming);
- with a **scoped API key** in the `default` workspace, with no rate limit, quota, concurrency, time or row limit of its
  own. Administrators, signed-in users, MCP and other workspaces always go to Python;
- on a **PostgreSQL or MySQL** connection, for a read-only saved query with bound parameters (no `{name}` text
  placeholders, no `embed` parameters, no response cache of its own);
- and Python has answered the **same call with the same key once already**. That first call gives the data plane its
  plan: the SQL to send, the masking to apply, and digests of the key, query and connection rows it was made from.
  Every later call reads those rows again and is served only while they're unchanged.

Everything else - the Console, the Management API, writes, MCP, `/events`, `/metrics` - passes through to Python as if
the data plane weren't there. Python still records every run the data plane serves in run history, the event log and
`/metrics`.

## 1. Build it

You need Rust; `dataplane/rust-toolchain.toml` names the version, and `rustup` installs it on first use.

```bash
git clone https://github.com/AnanthaRajuC/QueryAPIGate && cd QueryAPIGate
cargo build --release --manifest-path dataplane/Cargo.toml
ls dataplane/target/release/queryapigate-dataplane
```

## 2. Move Python to an inner port, with a shared secret

Python moves off the public port, to one only the data plane reaches, and gets a secret the two share. Use at least
**two** gunicorn workers: one Python process records about 17,000 runs a second, and the data plane serves more than
that.

```bash
export QUERYAPIGATE_DATAPLANE_SECRET="$(openssl rand -hex 32)"   # at least 16 characters; the same for both
export QUERYAPIGATE_API_KEY=...                                  # and the rest of your settings, as before
gunicorn --bind 127.0.0.1:5001 --workers 2 --worker-class gthread --threads 8 \
    -c python:queryapigate.gunicorn_conf 'queryapigate.app:create_app()'
```

Keep `127.0.0.1:5001` unreachable from outside the host: a request that reaches Python directly skips the data plane,
which is harmless, but the data plane should be the only way in.

## 3. Start the data plane on the public port

Same environment, same user, same store (`QUERYAPIGATE_HOME` or `QUERYAPIGATE_DATABASE_URL`), so it reads the rows
Python reads:

```bash
QUERYAPIGATE_DATAPLANE_LISTEN=0.0.0.0:5000 \
QUERYAPIGATE_DATAPLANE_UPSTREAM=http://127.0.0.1:5001 \
    dataplane/target/release/queryapigate-dataplane
```

It logs `queryapigate-dataplane: listening on 0.0.0.0:5000, in front of http://127.0.0.1:5001/ (8 threads)`. It
refuses to start without a secret of at least 16 characters.

| Setting | Default | What it does |
|---|---|---|
| `QUERYAPIGATE_DATAPLANE_SECRET` | none: required | Shared with Python. Without it, Python gives no plans and the data plane can't start |
| `QUERYAPIGATE_DATAPLANE_LISTEN` | `127.0.0.1:5000` | Where clients connect |
| `QUERYAPIGATE_DATAPLANE_UPSTREAM` | `http://127.0.0.1:5001` | The Python server |
| `QUERYAPIGATE_DATAPLANE_THREADS` | the machine's cores | Tokio worker threads |
| `QUERYAPIGATE_DATAPLANE_POOL_SIZE` | 16 | Connections per database it serves from |

It also reads Python's own settings that change an answer or a log line, and applies them as Python does:
`QUERYAPIGATE_API_KEY`, `QUERYAPIGATE_SECRET_KEY`, `QUERYAPIGATE_MASK_KEY`, `QUERYAPIGATE_QUERY_TIMEOUT`,
`QUERYAPIGATE_MAX_PAGE_SIZE`, `QUERYAPIGATE_METADATA_CACHE_TTL`, `QUERYAPIGATE_HISTORY_FLUSH_INTERVAL`,
`QUERYAPIGATE_LOG_REQUESTS`, `QUERYAPIGATE_ACCESS_LOG`, `QUERYAPIGATE_JSON_LOGS`, `QUERYAPIGATE_SLOW_QUERY_THRESHOLD` and
`QUERYAPIGATE_HISTORY_ADHOC_SQL`. The metadata cache is where much of its speed comes from: with
`QUERYAPIGATE_METADATA_CACHE_TTL=5`, a call reuses the key, query and connection rows it read in the last 5 seconds
instead of reading the store, and a revoked key keeps working for up to 5 seconds, as it does on Python alone.

## 4. See it serving

Call the same saved query twice with a scoped key:

```bash
curl -s -H "X-API-Key: $KEY" "http://localhost:5000/q/customer?id=7"   # Python answers, and gives a plan
curl -s -H "X-API-Key: $KEY" "http://localhost:5000/q/customer?id=8"   # the data plane answers
```

The answers are byte for byte what Python would send. The second shows up in the **data plane's** log, not Python's:
an `Executing on ...` line and the request line, in Python's format. Its run appears in run history and the Console's
live feed within one flush interval (1 s by default), and `/metrics` counts it as Python would.

If a call you expect to be served keeps going to Python, check it against the list above: an administrator's key, a
key with a rate limit, `?format=csv` or a text placeholder are the usual reasons.

## 5. To stop using it

Point clients at Python's port again (or move gunicorn back to the public port) and stop the data plane. Nothing is
stored on its side: plans live in its memory only.

## What to watch for

- **Runs it can't queue are dropped and counted** in `queryapigate_history_runs_total{outcome="dropped"}`. More than
  zero means Python can't keep up with recording: add a gunicorn worker.
- **The run history and event log grow with its rate.** The event log keeps every run for
  `QUERYAPIGATE_EVENTS_RETENTION_HOURS`; at tens of thousands of calls a second, that is more than a SQLite store
  should hold. Shorten it, or keep runs in ClickHouse instead
  ([guide 65](65-keep-runs-in-clickhouse.md)) - the data plane then writes its runs there itself.
- **Writes and other forwarded calls pay one extra hop:** about 0.35 ms at p50 on the benchmark laptop.

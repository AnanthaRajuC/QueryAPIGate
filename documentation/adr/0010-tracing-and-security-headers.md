# ADR 0010: Distributed tracing with OpenTelemetry, and security headers on every response

- **Status:** Proposed
- **Date:** 2026-10-11
- **Backlog:** #92 (distributed tracing) and #93 (security headers on every response, not only the Console's).

## Context

**Tracing.** Each request gets an id (`X-Request-Id`, taken from the caller's when given), and every log line and run
history entry carries it. There are Prometheus metrics. But nothing reads an incoming W3C `traceparent` header or
exports spans. In Datadog, Tempo, Honeycomb or Jaeger, a request that goes app → QueryAPIGate → database shows a gap
where QueryAPIGate is: nobody can see that the time went to the query rather than to authentication or serialising
the result.

A span also leaves the server, to a collector the deployer runs, and it gets read by people who have no data access.
So what goes into a span matters as much as what goes into the audit log, where we already keep parameter names but
never their values.

**Headers.** The Console's responses carry a Content-Security-Policy, `X-Content-Type-Options` and `Referrer-Policy`
(`console.py`). The API and `/docs` carry none and rely on the reverse proxy, which DEPLOYMENT.md recommends but
nothing checks. Two findings came up while preparing this ADR:

- **`/docs` is where people type their API key**, and it loads Swagger UI from a CDN pinned only to its major version
  (`swagger-ui-dist@5`), with no integrity hashes. A compromised CDN, or a bad 5.x release, could read every key typed
  there.
- **Cached responses were shareable** (`Cache-Control: max-age`, without `private`). Fixed on its own in 0.22.1,
  since it was a live problem. This ADR makes the cache policy for every response explicit, so it can't recur.

## Decision

### 1. Tracing is OpenTelemetry, optional, and configured the standard way

- **An extra:** `pip install "queryapigate[otel]"` (`opentelemetry-sdk` and the OTLP/HTTP exporter). Without it,
  nothing is imported and nothing changes.
- **On when an endpoint is set,** through OpenTelemetry's own variables, so a deployer configures QueryAPIGate like
  every other service: `OTEL_EXPORTER_OTLP_ENDPOINT` (or `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`), `OTEL_SERVICE_NAME`
  (default `queryapigate`), `OTEL_RESOURCE_ATTRIBUTES`, `OTEL_TRACES_SAMPLER`, and the exporter's headers and timeout.
  An endpoint set without the extra installed stops the server at startup with the install command, as Redis does.
- **Resource:** `service.name`, `service.version`, and `service.instance.id` as the instance id the Instances screen
  shows, so a span leads to the process that made it.
- **Exported in the background** (batch span processor), and flushed on shutdown. A collector that's down costs dropped
  spans and a warning in the log, never a slow or failed request.

### 2. Spans are written by hand, not by auto-instrumentation

Auto-instrumentation for Flask and the database drivers would be less code. But it records what those libraries see:
query strings with parameter values, statement text, headers. Our own spans record what we choose:

| Span | Kind | Attributes |
|---|---|---|
| `GET /q/{name}` (the route rule, never the path with values) | server | `http.request.method`, `http.route`, `http.response.status_code`, `url.scheme`, `client.address`, `queryapigate.request_id`, `queryapigate.caller` (key, administrator or `jwt:<subject>`, as in run history), `error.type` (the error `code`) |
| `query {name}` | internal | `queryapigate.query`, `queryapigate.version`, `queryapigate.connection`, `queryapigate.rows`, `queryapigate.masked`, `queryapigate.cache` (`hit`, `miss`) |
| `{SELECT,INSERT,...} {connection}` | client | `db.system.name`, `db.operation.name`, `db.namespace` (the database), `server.address`, `server.port`, `db.response.returned_rows` |
| `serialize {format}` | internal | `queryapigate.format`, `queryapigate.bytes` when known |
| `tools/call {tool}` (MCP) | server | `gen_ai.tool.name`, `queryapigate.caller`, `queryapigate.agent_write` (`approval`, `insert`) |
| `export {name}` | internal (root, or the parent from `TRACEPARENT`) | `queryapigate.export`, `queryapigate.destination`, `queryapigate.rows`, `queryapigate.format` |

- **Never in a span:** parameter values, rows or any value from them, statement text, query strings, headers (API
  keys, tokens, cookies), connection passwords or URLs with credentials, write requests' arguments. The statement
  text is left out even though saved SQL is written by administrators: it can hold literal values, and the span's
  readers aren't its authors. The span names the saved query instead, which leads to the SQL in the Console.
  - A test fails if any of these appears in an exported span. It runs the requests, MCP calls and exports the suite
    already makes, with an in-memory exporter.
- **Live events** (`/events`): one span for opening the stream, not one per event. A stream can stay open for hours.
- **Errors:** a span's status is `ERROR` for a 5xx and for a database failure. A 4xx is the caller's mistake: it keeps
  `error.type`, and its status stays unset, as the HTTP conventions say.

### 3. Context comes in through `traceparent`, and goes out through the trace id

- **Incoming:** W3C `traceparent` and `tracestate` on REST and MCP requests continue the caller's trace. A
  `queryapigate export` run continues `TRACEPARENT` from its environment, so a scheduler that sets it (Airflow, a CI
  job) links the run to its own trace.
- **Sampling is the deployer's choice:** `OTEL_TRACES_SAMPLER`, default `parentbased_always_on`, as the SDK defaults.
  The default lets a caller's sampled flag decide, which is what joins the traces up. A deployer who doesn't want
  callers deciding how much is recorded sets `traceidratio` instead. The threat model says so.
- **Outgoing, the trace id goes where the request id already goes:** run history entries (`trace_id`, so a slow run in
  the Console links to its trace), JSON log lines (`trace_id`, `span_id`), and error bodies beside `request_id`.
- **Not to the database:** a `traceparent` comment on SQL (sqlcommenter) would link database-side traces, but it
  changes the statement text, which affects plan caches and statement statistics. Left for later, opt-in.

### 4. Security headers on every response

Set once in `after_request`, for every route and every front door served by the main server:

- **Everywhere:** `X-Content-Type-Options: nosniff`.
- **API responses** (JSON, CSV, Parquet: everything but an HTML page): `Content-Security-Policy: default-src 'none';
  frame-ancestors 'none'` and `Referrer-Policy: no-referrer`. A response that a browser is somehow led to render can
  then run nothing and be framed nowhere.
- **`/docs`:** its own CSP, allowing exactly the pinned CDN files. Swagger UI is pinned to an exact version and loaded
  with Subresource Integrity hashes, checked by a test against the pinned files. Version and hashes change together,
  in one place.
- **The Console** keeps its CSP, unchanged.
- **Caching, explicit:** a response with data is `Cache-Control: no-store`, unless its saved query caches (then
  `private, max-age=<cache_ttl>`, as 0.22.1 made it). Management API responses are `no-store` too. The Console's
  assets keep their own caching.
- **HSTS, only when asked:** `QUERYAPIGATE_HSTS_MAX_AGE` (seconds, e.g. `31536000`), off by default. It is sent only on a
  request that arrived over HTTPS, which `QUERYAPIGATE_TRUST_PROXY` already lets the server tell behind a proxy. Sent
  on plain HTTP, HSTS would lock browsers out of a server that has no HTTPS.
- **`Server`:** the WSGI server's version banner is removed (gunicorn's `Server: gunicorn`, Werkzeug's version).

DEPLOYMENT.md's reverse-proxy section then lists only what the proxy still has to do: TLS itself, and HSTS when TLS
ends at the proxy.

### 5. Stability

- **Tracing is experimental** in 0.23: span names and attributes may still change as the OpenTelemetry semantic
  conventions for databases and GenAI settle. The rule about what never goes into a span is not experimental.
- **The headers are stable.** They add to responses and remove nothing a client reads.

## Consequences

- **New:** the `otel` extra; the standard `OTEL_*` variables honoured; spans on REST, MCP, live events and exports;
  `trace_id` in run history, JSON logs and error bodies; security headers on every response; a pinned Swagger UI with
  integrity hashes; `QUERYAPIGATE_HSTS_MAX_AGE`.
- **Nothing changes without the extra** except the headers and the pinned `/docs` assets.
- **Cost when on:** a few spans per request, exported in the background. The guide will measure it on a real request.
- **A client that cached an API response** (a browser that kept a GET) no longer may. No supported client relied on
  that.
- **Not in scope:** OpenTelemetry metrics and logs export (Prometheus `/metrics` and the JSON logs stay the way to get
  those); trace context to databases (sqlcommenter); spans for the Console's own requests in the browser.

## Delivery

About a week, in three slices:

1. Security headers: the policy on every response, `/docs` pinned with integrity hashes, `no-store` and the cache
   policy, HSTS behind a proxy, the `Server` banner. Tests per response type; DEPLOYMENT.md's proxy section.
2. Tracing: the extra and its startup check, the spans and their attributes, `traceparent` in and `trace_id` out (run
   history, logs, error bodies), MCP, live events and exports. Tests with an in-memory exporter, including one that
   fails if a parameter value, row value, statement or secret ever appears in a span.
3. A guide that sends real traces to a real collector (Jaeger, with the OTLP endpoint) through an app calling
   QueryAPIGate, with the overhead measured; the threat model; the changelog; 0.23.0 prep.

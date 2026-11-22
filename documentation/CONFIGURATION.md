# Configuration

Everything is configured through environment variables (all optional). Each is checked at startup: a value the server
can't read - `QUERYAPIGATE_QUERY_TIMEOUT=5m`, `QUERYAPIGATE_TRUST_PROXY=true`, `QUERYAPIGATE_LOG_REQUESTS=disabled` -
stops it with a message naming the setting, rather than running with the default. A yes/no setting takes `1`/`0`,
`true`/`false`, `yes`/`no` or `on`/`off`.

| Variable | Default | Effect |
|----------|---------|--------|
| `QUERYAPIGATE_HOME` | current directory | Folder holding `queryapigate.db` (connections, saved queries, API keys, roles and the audit log). |
| `QUERYAPIGATE_DATABASE_URL` | unset | A `postgresql://` URL: keep connections, saved queries, run history, API keys, roles and the audit log in that PostgreSQL database instead of `queryapigate.db`, so several instances can share them. Needs `queryapigate[postgres]`; copy an existing store across with `queryapigate migrate-to-postgres`. See [the setup guide](INSTALLATION_AND_SETUP.md#shared-metadata-store-postgresql). |
| `QUERYAPIGATE_ALLOW_WRITES` | off | Allow `INSERT`/`UPDATE`/DDL. Otherwise only single read-only statements are accepted. |
| `QUERYAPIGATE_API_KEY` | unset | The shared admin key, with the owner role. When set (or once a scoped key or a [named administrator](API.md#administrators-and-admin-roles) exists), every request except `/health`, `/docs`, `/console`, `/openapi.json` and `/metrics` needs an `X-API-Key` header. Once named owners exist it is the break-glass key: every use is logged and alerted, and it can be removed. |
| `QUERYAPIGATE_EXPORT_DIRS` | unset | Comma-separated absolute folders a local-folder export destination may write under. Unset: only object-storage destinations (`s3://`, `gs://`, `r2://`) are allowed. Never the server's own home. |
| `QUERYAPIGATE_MASK_KEY` | unset | The key column masking's `hash` action uses. Unset: one is made once in the store and shared by every instance. Changing it changes every hashed value. |
| `QUERYAPIGATE_REDIS_URL` | unset | A `redis://` URL: share rate limits and the response cache across every instance and process (`serve`, `mcp`, `events`). Needs `queryapigate[redis]`. Without it, each process counts and caches on its own. |
| `QUERYAPIGATE_MAX_PAGE_SIZE` | `1000` | Upper limit for `page_size`. |
| `QUERYAPIGATE_STREAM_MAX_ROWS` | unset | Row cap for a `?stream=true` export. Off (unbounded) by default; a malformed value stops startup. |
| `QUERYAPIGATE_CORS_ORIGINS` | unset | Websites allowed to call the API from a browser: comma-separated origins such as `https://app.example.com`, or `*`. Off by default. |
| `QUERYAPIGATE_RATE_LIMIT` | unset | Requests allowed per client address, e.g. `60/minute` (also `second`, `hour`, `day`). Off by default; a malformed value stops startup. |
| `QUERYAPIGATE_TRUST_PROXY` | `0` | Number of reverse proxies in front of the app whose `X-Forwarded-*` headers are trusted. Set it (usually `1`) behind nginx, a load balancer or a platform router, or every client looks like the proxy. |
| `QUERYAPIGATE_AUDIT_SYSLOG_URL` | *(unset)* | Stream every audit entry to a SIEM over syslog: `tls://host:6514` or `tcp://host:514`. With `QUERYAPIGATE_AUDIT_SYSLOG_CA`, and `_CERT`/`_KEY` for mutual TLS. |
| `QUERYAPIGATE_AUDIT_OTLP` | `false` | Stream every audit entry as OTLP log records to the OTLP logs endpoint. |
| `QUERYAPIGATE_AUDIT_KEY` | *(unset)* | HMAC key (32+ characters) the audit log's hash chain is signed with, so an edit made with only the store can't be re-signed. Several, comma-separated, while rotating it. Unset: plain SHA-256. |
| `QUERYAPIGATE_HSTS_MAX_AGE` | *(unset)* | Seconds browsers should use only HTTPS for this host (`Strict-Transport-Security`), sent only on requests that arrived over HTTPS. Unset: not sent - set it once every client reaches the server over HTTPS. |
| `QUERYAPIGATE_THREADS` | `8` | Requests one server process serves at once: the Docker image's gunicorn threads (`queryapigate/gunicorn_conf.py`). 1 to 256. The pool size and the `/events` stream cap default from it. See [Tune a deployment for throughput](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/how-to/63-tune-a-deployment-for-throughput.md). |
| `QUERYAPIGATE_ACCESS_LOG` | on | The Docker image's gunicorn access log, one line per request on stdout. `off` writes none - for a deployment whose proxy or load balancer logs requests already. `QUERYAPIGATE_LOG_REQUESTS` is QueryAPIGate's own request log, separate from it. |
| `QUERYAPIGATE_BIND` | `0.0.0.0:5000` in the image | Where the Docker image's gunicorn listens (`queryapigate/gunicorn_conf.py`). Behind the Rust data plane, which then takes port 5000 in a second container sharing the network, `127.0.0.1:5001` ([guide 64](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/how-to/64-try-the-rust-data-plane.md)). Unset outside the image: gunicorn's own default, or its `--bind`. |
| `QUERYAPIGATE_POOL_SIZE` | one per thread | Idle connections kept per distinct connection setting - by default `QUERYAPIGATE_THREADS` (8). `0` turns pooling off. |
| `QUERYAPIGATE_POOL_IDLE_TIMEOUT` | `300` | Seconds an idle pooled connection is kept before it is closed. |
| `QUERYAPIGATE_METADATA_CACHE_TTL` | `0` | Seconds the API key, saved query and connection a request reads from the store are reused, instead of read again each time - see [Performance](PERFORMANCE.md#the-metadata-cache). A change made through this server is seen at once; one made through another instance up to this long later. `0` turns it off. |
| `QUERYAPIGATE_QUERY_TIMEOUT` | `30` | Seconds a query may run before it is cancelled (HTTP 504). `0` disables the limit. A request can lower it with `?timeout=`, never raise it. |
| `QUERYAPIGATE_HOST` / `QUERYAPIGATE_PORT` | `127.0.0.1` / `5000` | Bind address for `queryapigate serve` - and the host for `queryapigate mcp` and `events` too, each on its own port. The image's gunicorn listens on `QUERYAPIGATE_BIND` instead. |
| `QUERYAPIGATE_DEBUG` | off | Flask debug mode (`queryapigate serve --debug`), with Werkzeug's interactive debugger, which runs code typed into an error page. Only with a loopback `--host` (`127.0.0.1`, `localhost`, `::1`): on any other, `serve` refuses to start. |
| `QUERYAPIGATE_H2_JAR` | bundled | Path to a different H2 JDBC jar. |
| `QUERYAPIGATE_ENCRYPTION_KEY` | unset | A Fernet key encrypting connection and destination passwords at rest - several, comma-separated, while rotating it, the first encrypting. Off by default (stored as given); needs `queryapigate[encryption]`. A malformed value stops startup. Before 1.0 it was named `QUERYAPIGATE_SECRET_KEY`, which is still read: set one - both set to different keys stops startup. |
| `QUERYAPIGATE_JSON_LOGS` | off | Emit one JSON object per log line, tagged with the request ID, instead of plain text. |
| `QUERYAPIGATE_LOG_REQUESTS` | on | A log line for every request, and one for each query it runs (`Executing on ...`). `off` leaves those INFO lines out - errors, warnings, slow queries and agent writes are still logged - for a server whose proxy already logs requests: the lines cost a share of the CPU, and every thread queues on the one handler that writes them. See [Performance, v3](PERFORMANCE_V3.md#the-cpu). |
| `QUERYAPIGATE_SLOW_QUERY_THRESHOLD` | `1` | Seconds a query may take before it is logged as a warning. `0` disables it. |
| `QUERYAPIGATE_HISTORY_LIMIT` | `50` | Runs kept per saved-query version (and shown in lists). |
| `QUERYAPIGATE_HISTORY_RETENTION_DAYS` | unset | Keep every run for this many days instead of a per-version count - browse it with `GET /api/v1/history`. Best with `QUERYAPIGATE_DATABASE_URL`. |
| `QUERYAPIGATE_HISTORY_SAMPLE_RATE` | `1` | The **fraction** of successful runs recorded (`0.1`: one in ten); failed runs always are. |
| `QUERYAPIGATE_HISTORY_ADHOC_LIMIT` | `1000` | Ad-hoc SQL runs (`/execute_sql`, MCP) kept in all, unless a retention period is set. |
| `QUERYAPIGATE_HISTORY_ADHOC_SQL` | `text` | What an ad-hoc run keeps of its SQL: `text`, `hash` (a SHA-256 only) or `none` - choose `hash` or `none` if SQL may embed sensitive literals. |
| `QUERYAPIGATE_HISTORY_FLUSH_INTERVAL` | `1` | Seconds between batched history writes; `0` writes inside each request. See [Run history](API.md#run-history). |
| `QUERYAPIGATE_AUDIT_LOG_LIMIT` | `500` | Administrative-change entries kept in `queryapigate.db`'s audit log; older ones roll off. Always a positive count; a malformed value stops startup. |
| `QUERYAPIGATE_LOAD_EXAMPLES` | unset | `yes` loads the [example APIs](EXAMPLES.md) (reporting, dashboard, export, partner) at startup - idempotent; a malformed value stops startup. Never removes anything: use `queryapigate examples unload`. |
| `QUERYAPIGATE_AUDIT_LOG_EXPORT_FILE` | unset | Path to also append every audit entry to, one JSON object per line, never capped - for retention beyond the rolling window above. |
| `QUERYAPIGATE_JWT_JWKS_URL` / `QUERYAPIGATE_JWT_SECRET` | unset | Accept signed-in users' tokens (`Authorization: Bearer`) from your identity provider or your own backend, with a role (`QUERYAPIGATE_JWT_ROLE` / `QUERYAPIGATE_JWT_ROLE_CLAIM`) for what they may do, and parameters bound to their claims (`from_claim`). See [Signed-in users](API.md#signed-in-users-jwt) for the related `QUERYAPIGATE_JWT_*` settings. |
| `QUERYAPIGATE_EVENTS_MAX_STREAMS` | `4` | Concurrent `GET /events` streams the main server holds open (each takes a request thread); beyond it, `503`. `0` turns it off. Unset, never more than half of `QUERYAPIGATE_THREADS`. |
| `QUERYAPIGATE_EVENTS_PORT` | `5002` | Port of `queryapigate events`, the separate live-events server for many clients - see [Live events](API.md#live-events-server-sent-events). |
| `QUERYAPIGATE_EVENTS_MAX_CONNECTIONS` | `10000` | Open streams one `queryapigate events` process accepts. |
| `QUERYAPIGATE_EVENTS_POLL_INTERVAL` | `1` | Seconds between `queryapigate events` checks for new runs (on PostgreSQL a notification usually arrives first). |
| `QUERYAPIGATE_EVENTS_RETENTION_HOURS` | `24` | Hours the event log keeps an event: how far back a `queryapigate events` client can resume with `Last-Event-ID`. A whole number, at least 1. |
| `QUERYAPIGATE_MCP_PORT` | `5001` | Bind port for `queryapigate mcp` - see [documentation/MCP.md](MCP.md). |
| `QUERYAPIGATE_MCP_MAX_ROWS` | `200` | Row cap for an MCP tool call's result. Always a positive count; a malformed value stops startup. |
| `QUERYAPIGATE_SSO_ISSUER` / `QUERYAPIGATE_SSO_CLIENT_ID` / `QUERYAPIGATE_SSO_CLIENT_SECRET` / `QUERYAPIGATE_SSO_REDIRECT_URL` | unset | Administrators sign in to the Console through your identity provider, their role from their groups (`QUERYAPIGATE_SSO_ROLES`). See [Console sign-in through SSO](API.md#console-sign-in-through-sso). |
| `QUERYAPIGATE_MCP_RESOURCE_URL` / `QUERYAPIGATE_MCP_AUTH_SERVER` | unset | Let hosted AI clients sign in through your identity provider (OAuth) - the MCP server's own URL and the provider's issuer, with `QUERYAPIGATE_MCP_ROLE` / `QUERYAPIGATE_MCP_ROLE_CLAIM`, `QUERYAPIGATE_MCP_WORKSPACE_CLAIM` and `QUERYAPIGATE_MCP_SCOPE`. See [Sign-in for AI clients](MCP.md#sign-in-for-ai-clients-oauth). |

### Every other setting

The rest, by what they turn on - each explained where its feature is. Together with the table above, this is every
setting the server, `queryapigate mcp`, `queryapigate events` and the data plane read: a test checks the code reads
nothing that isn't listed here.

**Configuration as code** ([API.md](API.md#configuration-as-code))

| Variable | Default | Effect |
|----------|---------|--------|
| `QUERYAPIGATE_CONFIG_FILE` | unset | A configuration file to apply at startup, once per file. |
| `QUERYAPIGATE_CONFIG_PRUNE` | off | Applying it also deletes what a file manages and this one no longer has. |
| `QUERYAPIGATE_CONFIG_LOCKED` | off | What a file manages changes only through a file - not the Console or the API. |
| `QAG_API_KEY` | unset | Not the server's: the key `queryapigate config diff/apply --server` sends, on the machine that runs it. It has its own prefix because it isn't a server setting, and a `${VAR}` can never name it. |

**Secrets** ([API.md](API.md#secrets-from-files-and-secrets-managers))

| Variable | Default | Effect |
|----------|---------|--------|
| `QUERYAPIGATE_SECRET_ENV_VARS` | unset - none | The variables a `${VAR}` reference may read: names, or prefixes ending in `*`. |
| `QUERYAPIGATE_SECRET_FILE_DIRS` | unset - none | Absolute folders a `${file:...}` reference may read. |
| `QUERYAPIGATE_SECRET_CACHE_SECONDS` | `300` | Seconds a secret from a provider is kept at most (1-86400) - the one duration named with its unit. |
| `QUERYAPIGATE_VAULT_ADDR`, `QUERYAPIGATE_VAULT_NAMESPACE`, `QUERYAPIGATE_VAULT_CACERT` | unset | Vault's address, an Enterprise namespace, a CA for its certificate. Unset address: `${vault:...}` and `vault-database` are refused. |
| `QUERYAPIGATE_VAULT_AUTH`, `QUERYAPIGATE_VAULT_AUTH_MOUNT` | `token`, the method's name | How the server signs in to Vault - `token`, `kubernetes` or `approle` - and where that method is mounted. |
| `QUERYAPIGATE_VAULT_TOKEN`, `QUERYAPIGATE_VAULT_TOKEN_FILE` | unset | `token`: the token, or a file read again at each sign-in (what Vault Agent keeps current). |
| `QUERYAPIGATE_VAULT_K8S_ROLE`, `QUERYAPIGATE_VAULT_K8S_TOKEN_FILE` | unset, the service account token | `kubernetes`: the Vault role, and the token signed in with. |
| `QUERYAPIGATE_VAULT_ROLE_ID`, `QUERYAPIGATE_VAULT_SECRET_ID` | unset | `approle`: its credentials. |
| `QUERYAPIGATE_VAULT_PATHS` | unset - none | The path prefixes `${vault:...}` may read, comma-separated (`secret/data/reporting/`). |
| `QUERYAPIGATE_VAULT_DATABASE_MOUNT`, `QUERYAPIGATE_VAULT_DATABASE_ROLES` | `database`, none | Where the database secrets engine is mounted, and the roles a connection may ask it for - each optionally bound to the host its credentials may go to (`reporting-ro@warehouse.internal`). |
| `QUERYAPIGATE_AWS_REGION` | boto3's | The region for Secrets Manager and RDS IAM tokens (experimental). |
| `QUERYAPIGATE_AWS_SECRET_PREFIXES` | unset - none | Secret names `${aws-sm:...}` may read begin with one of these. |
| `QUERYAPIGATE_AWS_RDS_HOSTS` | unset - none | Hosts `aws-rds-iam` credentials may be made for: a name, or a suffix starting with a dot. |

**Signed-in users and the Console's sign-in** ([JWT](API.md#signed-in-users-jwt), [SSO](API.md#console-sign-in-through-sso))

| Variable | Default | Effect |
|----------|---------|--------|
| `QUERYAPIGATE_JWT_ISSUER`, `QUERYAPIGATE_JWT_AUDIENCE` | unset | What a signed-in user's token must name. |
| `QUERYAPIGATE_JWT_ALGORITHMS` | `RS256` with a JWKS URL, else `HS256` | The algorithms accepted, comma-separated. |
| `QUERYAPIGATE_JWT_USER_CLAIM` | `sub` | The claim naming the user. |
| `QUERYAPIGATE_JWT_WORKSPACE_CLAIM` | unset | The claim naming the user's workspace (experimental, with workspaces). |
| `QUERYAPIGATE_SSO_GROUPS_CLAIM` | `groups` | The ID token's claim listing the groups roles are mapped from. |
| `QUERYAPIGATE_SSO_ALLOWED_DOMAINS`, `QUERYAPIGATE_SSO_DEFAULT_ROLE` | unset | For providers whose tokens carry no groups: verified emails at these domains get this role. |
| `QUERYAPIGATE_SSO_SESSION_HOURS` | `8` | How long a Console session lasts. |
| `QUERYAPIGATE_SSO_ONLY` | off | Administrators who sign in through SSO have no personal tokens. |
| `QUERYAPIGATE_CONSOLE_URL` | the SSO redirect's origin | Where people reach the Console, for links the server hands out (a write to approve). |

**Alerts and agents** ([Alerts](API.md#alerts), [write curation](API.md#per-query-write-curation))

| Variable | Default | Effect |
|----------|---------|--------|
| `QUERYAPIGATE_ALERT_ERROR_RATE` | `20` | A **percent** of a saved query's recent runs failing before it's an alert; `0` turns it off. (`QUERYAPIGATE_HISTORY_SAMPLE_RATE`, above, is a fraction.) |
| `QUERYAPIGATE_ALERT_KEY_UNUSED_DAYS` | `90` | Days an active key may go unused before it's an alert; `0` turns it off. |
| `QUERYAPIGATE_ALERT_AGENT_ROWS` | `10000` | Rows an AI agent may read over MCP in an hour before a sudden rise is an alert; `0` turns it off. |
| `QUERYAPIGATE_WRITE_APPROVAL_MINUTES` | `15` | How long an agent's write request waits for approval (1-1440). |

**Logs, metrics and the audit log** ([SIEM](API.md#streaming-the-audit-log-to-a-siem))

| Variable | Default | Effect |
|----------|---------|--------|
| `QUERYAPIGATE_ACCESS_LOG_QUERY` | off | The access log keeps each request's query string - its parameter values. |
| `QUERYAPIGATE_METRICS_TOKEN` | unset | The bearer token `/metrics` needs (16 characters at least). Unset, a server that needs a credential answers only a client on its own machine. |
| `QUERYAPIGATE_AUDIT_SYSLOG_CERT`, `QUERYAPIGATE_AUDIT_SYSLOG_KEY` | unset | A client certificate and its key for `QUERYAPIGATE_AUDIT_SYSLOG_URL` over TLS - both or neither. |

**Runs, live queries and semantic search** ([live queries](API.md#live-queries), [semantic search](API.md#semantic-search))

| Variable | Default | Effect |
|----------|---------|--------|
| `QUERYAPIGATE_RUN_STORE_URL` | unset | Keep run history, the event log and usage in ClickHouse, at this URL ([DEPLOYMENT.md](DEPLOYMENT.md#a-clickhouse-run-store)). |
| `QUERYAPIGATE_LIVE_QUERIES` | off | Live queries (experimental). |
| `QUERYAPIGATE_LIVE_MIN_INTERVAL` | `1` | Seconds between two re-runs of one live query, however often its tables change. |
| `QUERYAPIGATE_CDC_SOURCES` | unset | Debezium sources as JSON, for deployments configured only by environment; `/api/v1/cdc-sources` manages them otherwise. |
| `QUERYAPIGATE_EMBEDDING_URL`, `QUERYAPIGATE_EMBEDDING_MODEL`, `QUERYAPIGATE_EMBEDDING_API_KEY` | unset | An OpenAI-compatible embeddings endpoint, its model and token, for `embed` parameters (experimental). |

**The Rust data plane** (experimental - [DEPLOYMENT.md](DEPLOYMENT.md#a-rust-data-plane-in-front-of-q))

| Variable | Default | Effect |
|----------|---------|--------|
| `QUERYAPIGATE_DATAPLANE_SECRET` | unset | Shared by the data plane and the server it forwards to (16 characters at least): the data plane doesn't start without it, and the server gives a plan only to a request carrying it. |
| `QUERYAPIGATE_DATAPLANE_LISTEN` | `127.0.0.1:5000` | Where the data plane listens. |
| `QUERYAPIGATE_DATAPLANE_UPSTREAM` | `http://127.0.0.1:5001` | The Python server it forwards to. |
| `QUERYAPIGATE_DATAPLANE_THREADS` | the machine's cores | Its worker threads (1-256). |
| `QUERYAPIGATE_DATAPLANE_POOL_SIZE` | `16` | Database connections it keeps open, per connection. |
| `QUERYAPIGATE_DATAPLANE_RUN_STORE_URL` | `QUERYAPIGATE_RUN_STORE_URL` | The run store the data plane writes its runs to - an insert-only ClickHouse user, ideally. |

Every setting that holds a secret can also be read from a file named by `NAME_FILE` -
`QUERYAPIGATE_ENCRYPTION_KEY_FILE`, `QUERYAPIGATE_API_KEY_FILE` and so on.

The Console's **Settings** screen shows every one of these with its effective value and whether it was set or is the default (secrets are reported as configured or not, never shown). Settings that hold secrets can also be read from a file - `QUERYAPIGATE_ENCRYPTION_KEY_FILE` and the like: see [the server's own secrets, from files](API.md#secrets-from-files-and-secrets-managers).

## Calling the API from a browser

Browsers refuse cross-origin JSON calls unless the server allows them. List the sites that may call the API:

~~~bash
QUERYAPIGATE_API_KEY=change-me QUERYAPIGATE_CORS_ORIGINS=https://app.example.com queryapigate serve
~~~

Preflight checks are answered automatically, and the pagination headers (`X-Has-More` etc.) are exposed to the page's
JavaScript. CORS only tells the *browser* which sites may call; it is not authentication, so keep the API key. Avoid
`*` without a key: any website a visitor opens could then reach your databases through their browser (the server logs
a warning if you start that way).

## Rate limiting

`QUERYAPIGATE_RATE_LIMIT=60/minute` gives each client address a bucket of 60 requests that refills steadily, so short bursts
work but the sustained rate is capped. Over the limit, requests get `429` with a `Retry-After` header, and every
response carries `X-RateLimit-Limit` and `X-RateLimit-Remaining`. The limit is applied before the API key check, so
guessing keys is throttled too; `/health` and CORS preflights are never counted. With `QUERYAPIGATE_REDIS_URL` set, limits are counted in Redis - one budget per client address and per key across every instance and process (`serve`, `mcp`, `events`); if Redis fails, each process falls back to counting on its own until it's back, and an alert says so. Without Redis,
state is per process: with several workers or instances, the effective limit is multiplied by their number.

An API key can also carry its own `rate_limit` (same grammar), checked in addition to the server-wide limit, never
instead of it - a request has to pass both. See [Per-key rate limiting](API.md#per-key-rate-limiting).

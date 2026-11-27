# Command-line reference

`queryapigate` and its commands, as the CLI itself defines them - this page is written from its parser
(`python -m queryapigate.clidoc`), and a test fails while the two differ.

**What's covered by the compatibility promise** ([Versioning and compatibility](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/CHANGELOG.md#versioning-and-compatibility)):
the commands, their arguments and choices below; output written for a program - every `--json`, `config export` and
`collection export`'s files, an `export`'s data; and the exit codes. **What isn't:** output written for a person - the
wording of a summary, a table, an error message - which can change in any release.

**Exit codes**, one convention: `0` it did what was asked, `1` it ran and failed, `2` usage or setup (a mistyped flag,
a setting the server can't read, a store it can't open). A few give a code its own meaning, said with them: `config
diff` exits `2` for "there are changes" (every `config` failure is `1`), `audit verify` `1` for a broken chain and `2`
when it can't be checked, `rekey --dry-run` `1` while a password can't be decrypted.

Defaults below are with no `QUERYAPIGATE_*` setting: `serve --host` and `--debug` take theirs from
`QUERYAPIGATE_HOST` and `QUERYAPIGATE_DEBUG` ([Configuration](CONFIGURATION.md)).


### `queryapigate serve`

start the HTTP server (default)

| Argument | Meaning |
|---|---|
| `--host` `HOST` | the address to listen on (QUERYAPIGATE_HOST) (default `127.0.0.1`) |
| `--port` `PORT` | the port to listen on (default: QUERYAPIGATE_PORT, or 5000) |
| `--debug` | Flask debug mode, with an interactive debugger - only on a loopback --host |
| `--home` `HOME` | folder holding queryapigate.db (default: $QUERYAPIGATE_HOME or the current directory) |

### `queryapigate mcp`

start an MCP server exposing read-only saved queries as tools (needs `pip install "queryapigate[mcp]"`)

| Argument | Meaning |
|---|---|
| `--host` `HOST` | the address to listen on (QUERYAPIGATE_HOST) (default `127.0.0.1`) |
| `--port` `PORT` | the port to listen on (default: QUERYAPIGATE_MCP_PORT, or 5001) |

### `queryapigate events`

serve GET /events to many clients at once - apps, phones, dashboards - with resume (Last-Event-ID) and every instance's runs; run beside `queryapigate serve`

| Argument | Meaning |
|---|---|
| `--host` `HOST` | the address to listen on (QUERYAPIGATE_HOST) (default `127.0.0.1`) |
| `--port` `PORT` | the port to listen on (default: QUERYAPIGATE_EVENTS_PORT, or 5002) |

### `queryapigate backup`

copy queryapigate.db to FILE, consistently, while the server runs (a PostgreSQL store: prints the pg_dump command)

| Argument | Meaning |
|---|---|
| `FILE` | where to write the copy |
| `--force` | replace FILE if it exists |

### `queryapigate init`

create queryapigate.db in the home folder, with an inactive template connection per database type

| Argument | Meaning |
|---|---|
| `--home` `HOME` | folder holding queryapigate.db (default: $QUERYAPIGATE_HOME or the current directory) |

### `queryapigate migrate-to-postgres`

copy queryapigate.db (connections, saved queries and their history, API keys, roles, audit log) into the empty PostgreSQL database QUERYAPIGATE_DATABASE_URL names

| Argument | Meaning |
|---|---|
| `--from` `PATH` | the queryapigate.db to copy (default: the one in the home folder) |

### `queryapigate export`

run a saved query and write the full result to a file (for cron/systemd/Kubernetes CronJob, not a scheduler itself)

| Argument | Meaning |
|---|---|
| `query` | saved query name (as used in a GET /q/<name> request) |
| `--format` `FORMAT` | the file format, streamed as rows arrive (one of `csv`, `tsv`, `ndjson`, `parquet`; default `csv`) |
| `--connection` `CONNECTION` | overrides the saved query's own default connection |
| `--out` `OUT` | output path; {date} (YYYY-MM-DD) and {name} are filled in, e.g. /exports/{name}_{date}.csv - written to a temp file and renamed into place only on success, so a failed run never leaves a partial or missing file there. With --to, a path inside the destination, which may also use {time}, {run} and {a_parameter}: orders/{date}/orders_{run}.parquet (required) |
| `--to` `DESTINATION` | write under this destination (an s3://, gs://, r2:// prefix or a folder, defined with /api/v1/destinations) instead of a local path; --format parquet, csv or ndjson |
| `--param` `name=value` | a query parameter, e.g. --param customer_id=42 (repeatable) (repeatable) |
| `--timeout` `TIMEOUT` | seconds allowed for the query (default: the server setting, QUERYAPIGATE_QUERY_TIMEOUT) |
| `--home` `HOME` | folder holding queryapigate.db (default: $QUERYAPIGATE_HOME or the current directory) |
| `--workspace` `NAME` | experimental, with workspaces: the workspace whose queries, connections, keys and exports this acts on (default: default) |

### `queryapigate collection`

export or import a collection of saved queries as one portable JSON bundle


#### `queryapigate collection export`

write a collection's queries (latest version of each: SQL, description, tags, parameters, default connection - no history, keys or credentials) as a JSON bundle

| Argument | Meaning |
|---|---|
| `name` | collection name |
| `--format` `FORMAT` | bundle (default): re-importable with `collection import`. postman: a Postman Collection v2.1 file with one request per query (export only) (one of `bundle`, `postman`; default `bundle`) |
| `--base-url` `BASE_URL` | the {{baseUrl}} variable of a postman export (default: %(default)s) (default `http://127.0.0.1:5000`) |
| `--out` `OUT` | write to this file instead of stdout (written to a temp file and renamed into place, so a failure leaves nothing behind) |
| `--home` `HOME` | folder holding queryapigate.db (default: $QUERYAPIGATE_HOME or the current directory) |
| `--workspace` `NAME` | experimental, with workspaces: the workspace whose queries, connections, keys and exports this acts on (default: default) |

#### `queryapigate collection import`

save every query in a bundle into this home folder, into its collection - validated exactly like saving a query, and all-or-nothing before anything is written

| Argument | Meaning |
|---|---|
| `file` | a bundle written by `collection export` |
| `--on-conflict` `ON_CONFLICT` | a query name that already exists: fail (default - import nothing), skip it, or save the bundle's as a new version of it (one of `fail`, `skip`, `new-version`; default `fail`) |
| `--collection` `COLLECTION` | import into this collection instead of the bundle's own |
| `--dry-run` | report what would happen; write nothing |
| `--home` `HOME` | folder holding queryapigate.db (default: $QUERYAPIGATE_HOME or the current directory) |
| `--workspace` `NAME` | experimental, with workspaces: the workspace whose queries, connections, keys and exports this acts on (default: default) |

### `queryapigate config`

the server's configuration as a file (ADR 0014): export it, see what applying one would change, apply it


#### `queryapigate config export`

write connections, destinations, roles, saved queries, API keys, descriptions and exports as one YAML file - passwords as ${VAR} references, keys as their hashes, never a secret

| Argument | Meaning |
|---|---|
| `--kinds` `KINDS` | only these, comma-separated (of: connections, destinations, queries, roles, api_keys, descriptions, exports) |
| `--examples` | include the bundled examples too |
| `--out` `OUT` | write to this file instead of stdout |
| `--workspace` `NAME` | experimental, with workspaces: the workspace whose queries, connections, keys and exports this acts on (default: default) |

#### `queryapigate config diff`

what applying FILE would create and change, field by field - writes nothing. Exit 0: no changes; 2: changes; 1: the file has problems

| Argument | Meaning |
|---|---|
| `file` | a configuration file (YAML or JSON), or - for stdin |
| `--json` | the changes as JSON, for a script |
| `--prune` | also show what --prune would delete |
| `--server` `URL` | a running server's base URL, instead of this home's store - the key in QAG_API_KEY (CI against prod) |

#### `queryapigate config apply`

make the server match FILE, all or nothing - every change through the same rules as the API, audited; what the file does not name is left alone

| Argument | Meaning |
|---|---|
| `file` | a configuration file (YAML or JSON), or - for stdin |
| `--prune` | also delete what a file manages (created, changed or named before) and this one no longer has - never what was made in the Console or the API |
| `--dry-run` | say what would change; change nothing |
| `--json` | the result as JSON, for a script |
| `--server` `URL` | a running server's base URL, instead of this home's store - the key in QAG_API_KEY (an owner's) |

#### `queryapigate config new-key`

a fresh API key secret and its secret_sha256, for a key declared in a file - nothing stored


### `queryapigate examples`

load, remove or check the bundled example APIs (reporting, dashboard, export, partner)


#### `queryapigate examples load`

install four worked example scenarios - queries in collections, roles, and a small generated SQLite database - and an example team's workspace with three members who sign in to it, all marked as examples; idempotent, never overwrites anything of yours

| Argument | Meaning |
|---|---|
| `--home` `HOME` | folder holding queryapigate.db (default: $QUERYAPIGATE_HOME or the current directory) |
| `--workspace` `NAME` | experimental, with workspaces: the workspace whose queries, connections, keys and exports this acts on (default: default) |

#### `queryapigate examples unload`

remove exactly what `examples load` installed (and nothing else)

| Argument | Meaning |
|---|---|
| `--home` `HOME` | folder holding queryapigate.db (default: $QUERYAPIGATE_HOME or the current directory) |
| `--workspace` `NAME` | experimental, with workspaces: the workspace whose queries, connections, keys and exports this acts on (default: default) |

#### `queryapigate examples status`

say whether the examples are loaded

| Argument | Meaning |
|---|---|
| `--home` `HOME` | folder holding queryapigate.db (default: $QUERYAPIGATE_HOME or the current directory) |
| `--workspace` `NAME` | experimental, with workspaces: the workspace whose queries, connections, keys and exports this acts on (default: default) |

### `queryapigate admins`

create named administrators and issue their admin tokens, straight in the store - no server or key needed


#### `queryapigate admins create`

create an administrator and print its first token

| Argument | Meaning |
|---|---|
| `name` | their name: letters, digits, spaces, ., _ or - |
| `--role` `ROLE` | what they may do, server-wide (one of `owner`, `admin`, `developer`, `auditor`; required) |
| `--email` `EMAIL` | their email, for SSO to match them by |
| `--label` `LABEL` | what the token is for, e.g. 'laptop' or 'ci' |
| `--expires` `EXPIRES` | YYYY-MM-DD, or 'never' (default: 90 days from today) |
| `--home` `HOME` | folder holding queryapigate.db (default: $QUERYAPIGATE_HOME or the current directory) |

#### `queryapigate admins token`

issue another token for an administrator (one who lost theirs, or a new CI job)

| Argument | Meaning |
|---|---|
| `name` | the administrator |
| `--label` `LABEL` | what the token is for, e.g. 'laptop' or 'ci' |
| `--expires` `EXPIRES` | YYYY-MM-DD, or 'never' (default: 90 days from today) |
| `--home` `HOME` | folder holding queryapigate.db (default: $QUERYAPIGATE_HOME or the current directory) |

#### `queryapigate admins list`

list administrators

| Argument | Meaning |
|---|---|
| `--home` `HOME` | folder holding queryapigate.db (default: $QUERYAPIGATE_HOME or the current directory) |

### `queryapigate audit`

check the audit log's hash chain


#### `queryapigate audit verify`

walk the chain and say whether it is intact, or where it first breaks (exit 0 or 1)

| Argument | Meaning |
|---|---|
| `--file` `FILE` | a copy instead of the store: an export file (QUERYAPIGATE_AUDIT_LOG_EXPORT_FILE), or what a SIEM received - JSON or syslog lines, repeats allowed |
| `--home` `HOME` | folder holding queryapigate.db (default: $QUERYAPIGATE_HOME or the current directory) |

### `queryapigate history`

run history: forget a person (ADR 0016)


#### `queryapigate history forget`

delete every run by CALLER, its events and its usage counts - a person's data, on request; recorded in the audit log by its hash, not its name

| Argument | Meaning |
|---|---|
| `caller` | a key name, or jwt:<subject> for a signed-in person |
| `--dry-run` | say how much would go; delete nothing |

### `queryapigate runs`

where run history and the event log are kept: a ClickHouse run store (ADR 0029)


#### `queryapigate runs copy-to-clickhouse`

copy this store's run history into the run store, once


#### `queryapigate runs leave-clickhouse`

keep runs in the metadata store again, its event ids continuing above the run store's


### `queryapigate rekey`

re-encrypt every stored password under the first key in QUERYAPIGATE_ENCRYPTION_KEY, so the others can be removed (rotating the key)

| Argument | Meaning |
|---|---|
| `--dry-run`, `--check` | only report how many passwords are under each key - change nothing |
| `--home` `HOME` | folder holding queryapigate.db (default: $QUERYAPIGATE_HOME or the current directory) |

### `queryapigate exports`

run or list saved exports (deliveries of a saved query to a destination) - for cron on the host, no server needed


#### `queryapigate exports run`

run a saved export once, now

| Argument | Meaning |
|---|---|
| `name` | the export |
| `--home` `HOME` | folder holding queryapigate.db (default: $QUERYAPIGATE_HOME or the current directory) |
| `--workspace` `NAME` | experimental, with workspaces: the workspace whose queries, connections, keys and exports this acts on (default: default) |

#### `queryapigate exports list`

list saved exports

| Argument | Meaning |
|---|---|
| `--home` `HOME` | folder holding queryapigate.db (default: $QUERYAPIGATE_HOME or the current directory) |
| `--workspace` `NAME` | experimental, with workspaces: the workspace whose queries, connections, keys and exports this acts on (default: default) |

# Query metadata

Each saved query lives in `queryapigate.db`, with one entry per version (`"1"`, `"2"`, ...) - the same shape
it always was as a `saved_sql/*.json` file, and the shape a version-1 [bundle](API.md#exporting-and-importing-a-collection)
carries. Adding a version never changes an earlier one; the published version is what callers run unless they ask
for another (see [Drafts and publishing](API.md#drafts-and-publishing)). The Management API, configuration files and
version-2 bundles present the same fields with a few renamed - `sql` for `sql_query`, `parameters` for
`query_parameters`, `columns` for `column_descriptions` - and its timestamps as RFC 3339 with the server's offset;
see [Save a query](API.md#save-a-query).

| Field | Type | Purpose | Example |
|-------|------|---------|---------|
| `uuid` | string | Unique id of this version | `"c031f3ba-8b49-4e74-adf3-b085a12581f8"` |
| `sql_query` | string | The SQL, with optional `:name` parameters | `"SELECT * FROM film WHERE film_id = :id"` |
| `author` | string | Who saved it | `"anantha"` |
| `description` | string | What it is for | `"Look up a film"` |
| `tags` | string or array | Labels | `["example"]` |
| `query_type` | string | `sql` (the default) or `mongo` | `"sql"` |
| `mongo_collection`, `mongo_filter`, `mongo_projection`, `mongo_sort` | string, objects | A Mongo `find()` instead of SQL - see [MongoDB](DATABASE_CONNECTION_CONFIGURATION.md#mongodb-connections) | `"orders"` |
| `query_parameters` | object | Parameter definitions: a type, or an object of rules (`type`, `required`, `default`, `enum`, `min`, `max`, `min_length`, `max_length`, `pattern`, `description`, `example`, `from_claim`, `embed`) - see [Parameter rules](API.md#parameter-rules) | `{"id": {"type": "int", "min": 1}}` |
| `connection_name` | string | Optional default connection for `/q/<name>` | `"examples"` |
| `cache_ttl` | integer | Optional response-cache lifetime in seconds - see [Response caching](API.md#response-caching) | `60` |
| `column_descriptions` | object | What each result column means, by name - in its MCP tool's output schema | `{"amt": "Net revenue in EUR"}` |
| `mask` | array | Column masking for every caller - see [Column masking](API.md#column-masking) | `[{"column": "customers.email", "action": "hash"}]` |
| `mcp` | boolean | Whether it's an MCP tool (default `true`) | `false` |
| `agent_writes`, `max_affected_rows` | string, integer | A query that writes, curated for AI agents - see [MCP](MCP.md#write-tools-agents-that-write-approved-by-a-person) | `"approval"`, `1` |
| `created_at`, `last_modified_at` | string | Timestamps, the server's local time as stored | `"2024-03-23 23:50:57"` |
| `status` | string | Query state | `"active"` |
| `version` | integer | Version number | `2` |
| `execution_history` | array | The last 50 runs of this version | see below |

An `execution_history` entry looks like:

~~~json
{"executed_at": "2024-03-23 23:51:02", "connection_name": "examples", "status": "success", "rows": 1, "duration_ms": 4}
~~~

Failed runs are recorded with `"status": "error"` and an `"error"` message instead of `rows` and `duration_ms`.

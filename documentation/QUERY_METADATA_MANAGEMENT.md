# Query metadata

Each saved query lives in `queryapigate.db`, with one entry per version (`"1"`, `"2"`, ...) - the same shape
it always was as a `saved_sql/*.json` file (`GET /view_file_content` still hands you exactly this, for the
admin UI's "Show raw file" view). Saving under an existing name adds the next version; the latest version
runs unless one is requested.

| Field | Type | Purpose | Example |
|-------|------|---------|---------|
| `uuid` | string | Unique id of this version | `"c031f3ba-8b49-4e74-adf3-b085a12581f8"` |
| `sql_query` | string | The SQL, with optional `:name` parameters | `"SELECT * FROM film WHERE film_id = :id"` |
| `author` | string | Who saved it | `"anantha"` |
| `description` | string | What it is for | `"Look up a film"` |
| `tags` | string or array | Labels | `["example"]` |
| `query_parameters` | object | Parameter definitions: a type, or an object of rules (`type`, `required`, `default`, `enum`, `min`, `max`, `min_length`, `max_length`, `pattern`, `description`) - see [Parameter rules](API.md#parameter-rules) | `{"id": {"type": "int", "min": 1}}` |
| `connection_name` | string | Optional default connection for `/q/<name>` | `"examples"` |
| `cache_ttl` | integer | Optional response-cache lifetime in seconds - see [Response caching](API.md#response-caching) | `60` |
| `created_at`, `last_modified_at` | string | Timestamps | `"2024-03-23 23:50:57"` |
| `status` | string | Query state | `"active"` |
| `version` | integer | Version number | `2` |
| `execution_history` | array | The last 50 runs of this version | see below |

An `execution_history` entry looks like:

~~~json
{"executed_at": "2024-03-23 23:51:02", "connection_name": "examples", "status": "success", "rows": 1, "duration_ms": 4}
~~~

Failed runs are recorded with `"status": "error"` and an `"error"` message instead of `rows` and `duration_ms`.

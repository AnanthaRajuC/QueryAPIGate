# How to export a Postman collection for a set of APIs

**Time:** 2 minutes. **You'll end up with:** a real, importable Postman Collection (v2.1) covering every
saved query in a collection - correct URLs, example parameter values, and documentation per field - that
you can hand to a teammate without them writing a single request by hand.

## From the admin UI

Open **API Repository**, click **Collections**, pick a collection, then click **Postman** (next to the key
count and **Rename**, in the collection's own query list) - downloads a `.json` file ready to import into
Postman (File → Import).

## Over the API

```bash
curl http://127.0.0.1:5000/api/v1/collections/catalog/postman -H 'X-API-Key: demo-key' -o catalog.postman_collection.json
```

Admin only - verified: a scoped key gets the same "not authorized to manage the server configuration" error
every other admin-only endpoint gives. An empty or nonexistent collection name is a clean 404
(`"Collection 'does-not-exist' not found or empty"`).

## What's actually in the file, verified against a real export

```json
{
  "info": {"name": "catalog", "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
           "description": "...Set the apiKey variable to a key that may run them."},
  "auth": {"type": "apikey", "apikey": [{"key": "key", "value": "X-API-Key", "type": "string"},
                                        {"key": "value", "value": "{{apiKey}}", "type": "string"},
                                        {"key": "in", "value": "header", "type": "string"}]},
  "variable": [{"key": "baseUrl", "value": "http://127.0.0.1:5000"}, {"key": "apiKey", "value": ""}],
  "item": [ /* one request per saved query */ ]
}
```

**No credential is ever in the file** - `apiKey` is a collection variable left blank, for whoever imports
it to fill in themselves in Postman. `baseUrl` is picked up from whatever host you actually requested the
export from - verified: exporting from `http://127.0.0.1:5000` produced exactly that as `baseUrl`, not a
hardcoded value.

**Each query becomes a real, runnable request with sensible example values**, derived from the parameter's
own validation rules - verified against two real saved queries:

| Parameter's rules | Example value Postman gets |
|---|---|
| `"enum": ["tools", "electronics"]` | `tools` - the first enum value |
| `"default": 10` | that default |
| `"min": 1` (no default) | `1` |
| `"pattern": "[A-Za-z%]+"` | left **empty**, with the description `must match /[A-Za-z%]+/ - fill this in` - there's no safe way to synthesize a string that matches an arbitrary regex, so this is the one honest exception |

Every optional parameter is added to the request but **disabled** by default (Postman shows it, doesn't
send it, until you check the box) - including `format`/`page`/`page_size`, added to every request the same
way regardless of the query's own parameters, each with a one-line description of what it does.

## What's not included

- **Mongo saved queries are silently skipped** - verified: a Mongo query filed in the same collection as
  two SQL queries simply doesn't appear in the exported `item` list, no error, no placeholder. Postman
  export only understands SQL saved queries today.
- **It's a snapshot of the latest version, not a live link** - the collection's own description says so
  explicitly ("export again after queries change"). Editing a saved query afterward doesn't update
  anyone's already-imported file.
- **Nothing from a different collection, or an uncollected query** - exactly the queries filed under the
  one collection name you asked for; see
  [Group queries into a collection](07-group-queries-into-a-collection.md) if you haven't organized yours
  into one yet.

## Next steps

- [Group queries into a collection](07-group-queries-into-a-collection.md) - if the queries you want to
  share aren't filed under one yet.
- [Set up your first scoped API key](13-set-up-a-scoped-api-key.md) - the key your teammate pastes into
  Postman's `apiKey` variable should usually be a scoped one, not the admin key.

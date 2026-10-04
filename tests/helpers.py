"""Shared test fixture helpers for the SQLite-backed store (db.py/store.py) - replaces the old pattern of
hand-writing db_connections.json directly, which the app no longer reads (Phase 1 of the JSON-to-SQLite
migration: connections and saved queries are SQLite-backed; api_keys.json/roles.json/audit_log.json are
untouched and still plain JSON files, written directly as before).

Call `write_connections()` after QUERYAPIGATE_HOME is patched to the test's temp directory, before
`create_app()` - the same place a test used to open db_connections.json and json.dump into it.
"""
from queryapigate import db, store


def write_connections(connections):
    """connections: {name: {db, ..., active, ...}} - the exact same shape a hand-written db_connections.json
    used to hold. A full replace (like overwriting the old file was), not a merge - existing connections are
    cleared first, then store.update_connections() (validated, encrypted-if-configured, timestamped) adds
    the given ones, the same path the real API/UI uses rather than poking the database directly."""
    db.init_schema()
    with db.transaction() as conn:
        conn.execute('DELETE FROM connections')
    if connections:
        store.update_connections(connections)


# --------------------------------------------------------------------------------------
# The Management API (/api/v1) for test setup - what the removed legacy routes (PATCH /save_sql_to_file,
# PATCH /connections, POST /api_keys) used to do in one call.
# --------------------------------------------------------------------------------------

_QUERY_FIELDS = {'filename': 'name', 'sql_query': 'sql', 'query_parameters': 'parameters'}


def save_query(client, body, headers=None):
    """Save a query the way PATCH /save_sql_to_file did - a new query, or a new version of an existing one, published
    at once - given that route's body (`filename`, `sql_query`, `query_parameters`, ...). Returns the last response:
    201 when saved, the API's own error otherwise."""
    data = {_QUERY_FIELDS.get(k, k): v for k, v in body.items()}
    data.setdefault('description', '')
    data['publish'] = True
    name = data.get('name')
    collection_given = 'collection' in data
    collection = data.pop('collection', None)
    exists = isinstance(name, str) and name and client.get(
        f'/api/v1/queries/{name}', headers=headers).status_code == 200
    if not exists:
        if collection_given:
            data['collection'] = collection
        return client.post('/api/v1/queries', json=data, headers=headers)
    data.pop('name')
    res = client.post(f'/api/v1/queries/{name}/versions', json=data, headers=headers)
    if res.status_code == 201 and collection_given:
        moved = client.patch(f'/api/v1/queries/{name}', json={'collection': collection}, headers=headers)
        if moved.status_code != 200:
            return moved
    return res


def put_connections(client, connections, headers=None):
    """Create or change connections the way PATCH /connections did: {name: {db, host, ...}}. Returns the last
    response."""
    res = None
    for name, details in connections.items():
        if client.get(f'/api/v1/connections/{name}', headers=headers).status_code == 200:
            res = client.patch(f'/api/v1/connections/{name}', json=details, headers=headers)
        else:
            res = client.post('/api/v1/connections', json={'name': name, **details}, headers=headers)
    return res


def create_key(client, headers=None, **fields):
    """A new API key from its grants (POST /api/v1/api-keys); returns the response - its `secret` is the key."""
    return client.post('/api/v1/api-keys', json=fields, headers=headers)


def secret_of(res):
    """The new key from a create_key() response, failing loudly if it wasn't created."""
    assert res.status_code == 201, res.get_data(as_text=True)
    return res.get_json()['secret']

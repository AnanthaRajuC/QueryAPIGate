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

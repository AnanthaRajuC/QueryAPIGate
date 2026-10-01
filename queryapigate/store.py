"""Persistence: the connection registry, saved queries, and the audit log - all SQLite, via db.py (see
db.py's own docstring for why). API keys and roles live in the same queryapigate.db but are owned by
apikeys.py, which has its own read/write functions against the same db.py connection.

Every saved-query function still speaks in terms of a "path" for backward compatibility with every
caller - resolve_saved_file()'s own docstring explains why that's now just the query's name, not a real
filesystem path.
"""
import json
import os
import re
import threading
import uuid
from datetime import datetime

from . import config, db, history
from .errors import ApiError

# Serialises read-modify-write cycles on the JSON files this app manages.
lock = threading.RLock()

_ENV_REF_RE = re.compile(r'\$\{(\w+)\}')
_FILENAME_RE = re.compile(r'^[\w .\-]{1,100}$')
_ENC_PREFIX = 'enc:'  # marks a connection password as Fernet-encrypted - see _encrypt_password()
# One canonical spelling, rejected (never silently case-folded) when violated, so "Reporting" and "reporting"
# can never both exist as two collections.
_COLLECTION_RE = re.compile(r'^[a-z0-9][a-z0-9._-]{0,62}$')
_UNSET = object()  # "argument not passed" - distinct from an explicit None, which clears a collection


def now():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


# --------------------------------------------------------------------------------------
# Audit log: a durable record of administrative changes (API keys, connections, saved
# queries) - distinct from execution_history below (query *runs*) and from the ephemeral,
# stdout-only access log (app.decorate()). See app.py's route handlers for what calls this
# and with what `changes` shape; this module only persists whatever it's given.
# --------------------------------------------------------------------------------------

def record_audit(actor, action, target, changes=None):
    """Append one entry (oldest first, capped at config.audit_log_limit() - same per-insert SQL trim
    record_execution() below uses for execution_history) and, when QUERYAPIGATE_AUDIT_LOG_EXPORT_FILE is
    set, also append it to that plain file - a second, never-capped copy for retention beyond the SQLite
    table's own rolling window, independent of where the primary log lives. Never raises: an audit-log
    write failing should not block the action it's recording, the same trade-off record_execution() already
    makes for query-run history; the two writes are independently fault-tolerant so a problem with one
    (e.g. the export path's directory missing) never suppresses the other."""
    entry = {'timestamp': now(), 'actor': actor, 'action': action, 'target': target, 'changes': changes}
    try:
        with db.transaction() as conn:
            conn.execute('INSERT INTO audit_log (timestamp, entry_json) VALUES (?, ?)',
                        (entry['timestamp'], json.dumps(entry, default=str)))
            conn.execute("""
                DELETE FROM audit_log WHERE rowid IN (
                    SELECT rowid FROM audit_log ORDER BY timestamp DESC, rowid DESC LIMIT -1 OFFSET ?
                )
            """, (config.audit_log_limit(),))
    except (OSError, *db.Error):
        pass
    export_path = config.audit_log_export_file()
    if export_path is not None:
        try:
            with lock, open(export_path, 'a') as f:
                f.write(json.dumps(entry, default=str) + '\n')
        except OSError:
            pass


def read_audit_log():
    """Every stored entry, oldest first (the same order the JSON file's list was always in) - app.py's
    /audit_log route reverses this to present newest first."""
    rows = db.connection().execute('SELECT entry_json FROM audit_log ORDER BY id').fetchall()
    return [json.loads(row['entry_json']) for row in rows]


# --------------------------------------------------------------------------------------
# Connections - SQLite-backed (queryapigate.db, see db.py). `db`/`active`/`created_at`/`updated_at` are
# real columns (queried/filtered directly); everything else (password, host, port, user, database,
# example, ...) lives in `details_json`, exactly as flexible as it was as a raw JSON dict, and is merged
# back into one dict by _connection_row_to_dict() so every caller sees the same shape as before.
# --------------------------------------------------------------------------------------

def _connection_row_to_dict(row):
    details = json.loads(row['details_json'])
    details['db'] = row['db']
    details['active'] = bool(row['active'])
    details['created_at'] = row['created_at']
    details['updated_at'] = row['updated_at']
    return details


def read_connections():
    rows = db.connection().execute(
        'SELECT name, db, active, created_at, updated_at, details_json FROM connections').fetchall()
    return {row['name']: _connection_row_to_dict(row) for row in rows}


def import_legacy_connections_if_empty():
    """First-boot bootstrap: the connections table is now the only place a connection is read from - a
    still-present db_connections.json (an upgrade from before this table existed, or a read-only seed file
    like the docker-compose demo's) would otherwise just go silently unread. Only runs when the table is
    completely empty, so it's safe to call on every startup: once anything exists in SQLite (imported here,
    or created through the API) this never looks at the JSON file again. Never raises - a missing or
    corrupt legacy file just means there is nothing to import, the same "nothing there yet" state as a
    genuinely fresh install."""
    if db.connection().execute('SELECT 1 FROM connections LIMIT 1').fetchone() is not None:
        return
    path = config.connections_file()
    if not path.exists():
        return
    try:
        with open(path, 'r') as f:
            connections = json.load(f).get('connections', {})
    except (OSError, json.JSONDecodeError):
        return
    if connections:
        update_connections(connections)


def _expand_env(value):
    """Replace ${VAR} references in a connection value with the environment variable's content."""
    if not isinstance(value, str):
        return value

    def replace(match):
        name = match.group(1)
        if name not in os.environ:
            raise ApiError(f"Environment variable '{name}' referenced by the connection is not set", 500)
        return os.environ[name]

    return _ENV_REF_RE.sub(replace, value)


def _is_env_ref(value):
    return isinstance(value, str) and bool(_ENV_REF_RE.fullmatch(value))


def _is_encrypted(value):
    return isinstance(value, str) and value.startswith(_ENC_PREFIX)


def _get_fernet():
    """The configured Fernet instance for connection-password encryption at rest (QUERYAPIGATE_SECRET_KEY), or
    None when unset - encryption at rest is opt-in. The key's format is already validated at startup
    (config.check_settings()), so a Fernet() call here should not itself fail."""
    key = config.secret_key()
    if not key:
        return None
    from cryptography.fernet import Fernet
    return Fernet(key.encode('utf-8'))


def _encrypt_password(value):
    """Encrypt a literal connection password for storage, if QUERYAPIGATE_SECRET_KEY is set - a ${VAR} reference
    or an already-encrypted value passes through unchanged (never double-encrypted, and a ${VAR} reference
    is not a secret stored in this file at all). Returns the value unchanged when no key is configured -
    today's behaviour, unaffected without opting in."""
    if not isinstance(value, str) or not value or _is_env_ref(value) or _is_encrypted(value):
        return value
    fernet = _get_fernet()
    if fernet is None:
        return value
    return _ENC_PREFIX + fernet.encrypt(value.encode('utf-8')).decode('ascii')


def _decrypt_password(value):
    """Decrypt a stored connection password for actual use, at the moment a connection is opened
    (get_connection()) - never held decrypted anywhere else. Fails clearly rather than silently: a missing
    or rotated QUERYAPIGATE_SECRET_KEY must not pass ciphertext to the driver, or silently fall back to treating
    it as a literal, the same "fail closed, say why" precedent is_expired() already sets for a malformed
    stored value that could otherwise fail dangerously quiet."""
    if not _is_encrypted(value):
        return value
    fernet = _get_fernet()
    if fernet is None:
        raise ApiError("This connection's password is encrypted but QUERYAPIGATE_SECRET_KEY is not set - it "
                       'cannot be decrypted', 500)
    from cryptography.fernet import InvalidToken
    try:
        return fernet.decrypt(value[len(_ENC_PREFIX):].encode('ascii')).decode('utf-8')
    except InvalidToken:
        raise ApiError("This connection's password could not be decrypted - QUERYAPIGATE_SECRET_KEY may have "
                       'been rotated since it was encrypted', 500) from None


def resolve_ad_hoc(details):
    """Like get_connection(), but for connection fields given directly rather than a saved, named connection
    - used only by ``POST /connections/test`` (the New/Edit connection form's "Test connection" button)
    to try fields that may never be saved. Expands ${VAR} references and decrypts an already-encrypted
    password the same way; a literal password already in ``details`` (the common case - a value just typed
    into the form) passes through unchanged."""
    resolved = {key: _expand_env(value) for key, value in details.items()}
    password = resolved.get('password')
    if password and _is_encrypted(password):
        resolved['password'] = _decrypt_password(password)
    return resolved


def get_connection(connection_name):
    """Return the usable (active, env-expanded, password-decrypted) details of a named connection."""
    if not isinstance(connection_name, str) or not connection_name:
        raise ApiError('Connection name is missing')
    row = db.connection().execute(
        'SELECT db, active, created_at, updated_at, details_json FROM connections WHERE name = ?',
        (connection_name,)).fetchone()
    if not row:
        raise ApiError(f"Connection '{connection_name}' not found", 404)
    if not row['active']:
        raise ApiError(f"The connection '{connection_name}' is currently not active. "
                       "To use it, it must be set to active.", 403)
    if row['db'] not in config.SUPPORTED_DB_TYPES:
        raise ApiError('Unsupported database type')
    details = _connection_row_to_dict(row)
    resolved = {key: _expand_env(value) for key, value in details.items()}
    if 'password' in resolved:
        resolved['password'] = _decrypt_password(resolved['password'])
    return resolved


def _is_plaintext_password(password):
    """A literal, unprotected password - not a ${VAR} reference and not already encrypted at rest. Used
    only for the startup warning and the auto-migration on start (plaintext_password_connections(),
    encrypt_plaintext_passwords_in_place()) - see mask_passwords() below for the broader "is this a real
    secret value" check used for API responses, which masks an encrypted value too."""
    return bool(password) and not _is_env_ref(password) and not _is_encrypted(password)


def mask_passwords(connections):
    """Hide any real secret value in API responses - a literal password and an encrypted one both mask to
    the same PASSWORD_MASK; only a ${VAR} reference (never a secret itself, just a pointer to one) is shown
    as written."""
    def mask(conn):
        password = conn.get('password')
        if password and not _is_env_ref(password):
            return {**conn, 'password': config.PASSWORD_MASK}
        return conn

    return {name: mask(conn) for name, conn in connections.items()}


def plaintext_password_connections():
    """Names of connections whose password is a literal string on disk rather than a ${VAR} reference to
    an environment variable *or* already encrypted at rest - used only for the startup warning in
    app.create_app(). An empty password (common for local/dev databases with none) is not flagged."""
    return sorted(name for name, conn in read_connections().items() if _is_plaintext_password(conn.get('password')))


def encrypt_plaintext_passwords_in_place():
    """Called once at startup when QUERYAPIGATE_SECRET_KEY is set: encrypts any connection password that's still
    a literal, so a connection saved before the key existed benefits immediately rather than waiting for its
    next PATCH /connections - "don't require a one-time manual migration step" from the start."""
    with db.transaction() as conn:
        rows = conn.execute('SELECT name, details_json FROM connections').fetchall()
        for row in rows:
            details = json.loads(row['details_json'])
            password = details.get('password')
            if _is_plaintext_password(password):
                details['password'] = _encrypt_password(password)
                conn.execute('UPDATE connections SET details_json = ? WHERE name = ?',
                            (json.dumps(details), row['name']))


def encrypted_password_connections():
    """Names of connections whose password is encrypted at rest - used only for the startup warning that
    fires when QUERYAPIGATE_SECRET_KEY is missing but encrypted passwords already exist on disk (a rotated-out
    or removed key, most likely): those connections cannot be used until the key is restored."""
    return sorted(name for name, conn in read_connections().items() if _is_encrypted(conn.get('password')))


def update_connections(connections):
    for name, details in connections.items():
        if not isinstance(details, dict) or details.get('db') not in config.SUPPORTED_DB_TYPES:
            raise ApiError(f"Connection '{name}' must be an object whose 'db' is one of: "
                           f"{', '.join(config.SUPPORTED_DB_TYPES)}")
    with db.transaction() as conn:
        timestamp = now()
        for name, details in connections.items():
            existing_row = conn.execute(
                'SELECT created_at, details_json FROM connections WHERE name = ?', (name,)).fetchone()
            existing_details = json.loads(existing_row['details_json']) if existing_row else {}
            # GET masks passwords, so a client echoing the mask back means "keep the current one" - reused
            # exactly as stored (already encrypted, if it was), never re-encrypted.
            if details.get('password') == config.PASSWORD_MASK and existing_row:
                details = {**details, 'password': existing_details.get('password', '')}
            elif 'password' in details:
                # A genuinely new literal password (or ${VAR} reference, which _encrypt_password() passes
                # through unchanged) - encrypted here if QUERYAPIGATE_SECRET_KEY is set, stored as given otherwise.
                details = {**details, 'password': _encrypt_password(details['password'])}
            # created_at/updated_at are server-controlled, never taken from the request (a client echoing back
            # what GET /connections returned must not be able to fake either one). A connection that already
            # existed keeps its created_at (the column is NOT NULL, so any existing row already has a real
            # one - no legacy-data backfill needed here, only in the one-time JSON migration itself).
            created_at = existing_row['created_at'] if existing_row else timestamp
            dialect = details.get('db')
            active = bool(details.get('active', False))
            extra = {k: v for k, v in details.items() if k not in ('db', 'active', 'created_at', 'updated_at')}
            conn.execute("""
                INSERT INTO connections (name, db, active, created_at, updated_at, details_json)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    db = excluded.db, active = excluded.active,
                    created_at = excluded.created_at, updated_at = excluded.updated_at,
                    details_json = excluded.details_json
            """, (name, dialect, int(active), created_at, timestamp, json.dumps(extra)))


def delete_connection(name):
    with db.transaction() as conn:
        cur = conn.execute('DELETE FROM connections WHERE name = ?', (name,))
        if cur.rowcount == 0:
            raise ApiError(f"Connection '{name}' not found", 404)


# --------------------------------------------------------------------------------------
# Saved queries (all access is confined to the saved_sql folder)
# --------------------------------------------------------------------------------------

def saved_path_for_name(filename):
    """Validate a client-supplied saved-query name; returns it unchanged. No real file path is involved
    once saved queries live in SQLite - kept as the one shared name-shape validator (bundle.py also calls
    this purely for its validation side effect) so a name is held to the same rule everywhere."""
    if not isinstance(filename, str) or not _FILENAME_RE.match(filename) or filename.startswith('.'):
        raise ApiError("Filename may only contain letters, digits, spaces, '.', '_' and '-'")
    return filename


def saved_query_exists(name):
    """A plain existence check by exact name - used by examples.py's conflict detection, which needs to
    know "is something already using this name" without resolve_saved_file()'s reference-parsing/403
    semantics (a conflict is never a client-facing path, just an internal decision)."""
    return db.connection().execute('SELECT 1 FROM saved_queries WHERE name = ?', (name,)).fetchone() is not None


def resolve_saved_file(ref):
    """Resolve a client-supplied reference to an existing saved query, returning its canonical name.

    No real file path is involved once saved queries live in SQLite - but every caller already treats this
    return value as opaque (passing it straight to load_versions()/query_name()/record_execution()), so
    nothing outside this module needs to change. Still accepts every form it always has - a bare name
    ("cht" or "cht.json"), a path relative to the home folder ("saved_sql/cht.json") or an absolute path -
    for backward compatibility; anything that doesn't reduce to a single valid name (a path escaping
    saved_sql/, a name containing a separator) is rejected the same way an out-of-bounds path used to be.
    """
    if not isinstance(ref, str) or not ref.strip():
        raise ApiError('Filename is missing')
    candidate = ref
    if os.path.isabs(candidate):
        home = str(config.home())
        if not candidate.startswith(home + os.sep):
            raise ApiError('Only .json files inside the saved_sql folder can be accessed', 403)
        candidate = os.path.relpath(candidate, home)
    candidate = candidate.replace(os.sep, '/')
    if candidate.startswith('saved_sql/'):
        candidate = candidate[len('saved_sql/'):]
    if candidate.endswith('.json'):
        candidate = candidate[:-len('.json')]
    if '/' in candidate or not _FILENAME_RE.match(candidate) or candidate.startswith('.'):
        raise ApiError('Only .json files inside the saved_sql folder can be accessed', 403)
    if db.connection().execute('SELECT 1 FROM saved_queries WHERE name = ?', (candidate,)).fetchone() is None:
        raise ApiError('File not found', 404)
    return candidate


def query_name(path):
    """The canonical saved-query name for a value resolve_saved_file() returned - already the name itself
    (see resolve_saved_file()'s own docstring for why "path" is now just a name)."""
    return path


def load_versions(name, with_history=True):
    """Reconstruct the same nested dict shape a saved-query JSON file used to be:
    {"<version>": {...fields, execution_history}, ..., "collection"?: str, "example"?: bool} - every pure
    function downstream (version_numbers(), select_version(), read_collection(), read_example(),
    _apply_collection(), and so list_saved()/latest_versions() built on top of them) still operates on
    exactly this shape unchanged, so this is the one function that needs to bridge SQL to it.

    Each version's execution_history holds its newest config.history_limit() runs, oldest first, however many
    are stored - with a retention period (history.py) a version can keep far more, paged through GET /history
    instead. Runs this process has queued but not written yet are written first, so a caller always sees its
    own. ``with_history=False`` leaves every execution_history empty instead - for the request path that only
    runs a query (app.run_saved()), which never reads its history but would otherwise load it on every call."""
    if with_history:
        history.flush()
    row = db.connection().execute(
        'SELECT collection, example FROM saved_queries WHERE name = ?', (name,)).fetchone()
    if row is None:
        raise ApiError('File not found', 404)
    content = {}
    if row['collection'] is not None:
        content['collection'] = row['collection']
    if row['example']:
        content['example'] = True
    version_rows = db.connection().execute(
        'SELECT version, uuid, status, created_at, last_modified_at, fields_json '
        'FROM saved_query_versions WHERE query_name = ? ORDER BY version', (name,)).fetchall()
    for v in version_rows:
        try:
            fields = json.loads(v['fields_json'])
        except json.JSONDecodeError:
            raise ApiError('Saved query data is not valid JSON', 500) from None
        if not isinstance(fields, dict):
            raise ApiError('Saved query data has an unexpected structure', 500)
        history_rows = db.connection().execute(
            'SELECT entry_json FROM (SELECT entry_json, executed_at, rowid FROM execution_history '
            'WHERE query_name = ? AND version = ? ORDER BY executed_at DESC, rowid DESC LIMIT ?) AS newest '
            'ORDER BY executed_at, rowid', (name, v['version'], config.history_limit())
        ).fetchall() if with_history else []
        content[str(v['version'])] = {
            'uuid': v['uuid'],
            **fields,
            'created_at': v['created_at'],
            'last_modified_at': v['last_modified_at'],
            'status': v['status'],
            'version': v['version'],
            'execution_history': [json.loads(h['entry_json']) for h in history_rows],
        }
    return content


def version_numbers(content):
    return [int(v) for v in content if v.isdigit()]


def select_version(content, version=None):
    """Return (number, data) for the requested version, or the latest one when none is given."""
    if version is None:
        numbers = version_numbers(content)
        if not numbers:
            raise ApiError('No valid versions found in the file')
        version = max(numbers)
    data = content.get(str(version))
    if not isinstance(data, dict):
        raise ApiError(f'Version {version} not found', 404)
    return int(version), data


def validate_collection_name(name):
    """The one place a collection name is checked - saving a query, moving it, granting a key access and
    importing a bundle all go through here, so they cannot disagree about what a valid name is."""
    if not isinstance(name, str) or not _COLLECTION_RE.match(name):
        raise ApiError("A collection name must be 1-63 characters: lowercase letters, digits, '.', '_' and '-', "
                       'starting with a letter or digit')
    return name


def read_collection(content):
    """The collection a loaded saved-query file belongs to, or None. A hand-edited value that is not a valid
    name reads as *no collection* rather than being trusted: a grant on a collection must never reach a query
    through a spelling validate_collection_name() would have refused."""
    value = content.get('collection')
    return value if isinstance(value, str) and _COLLECTION_RE.match(value) else None


def read_example(content):
    """Whether a loaded saved-query file was installed by ``queryapigate examples load``."""
    return content.get('example') is True


def _apply_collection(content, collection):
    if collection is None:
        content.pop('collection', None)
    else:
        content['collection'] = collection


def save_version(filename, fields, collection=_UNSET, example=False):
    """Store ``fields`` as the next version of a saved query; returns (uuid, version number).

    ``collection`` belongs to the query, not to a version: left out, the query's current collection is kept;
    a name sets it; None clears it. It is written in the same transaction as the new version, so a query is
    never saved without the collection it was saved with.

    ``example=True`` marks the query as installed by ``queryapigate examples load`` (an ``example`` flag on
    the query row), which is how ``examples unload`` knows exactly what is its own to remove. It never
    clears the mark."""
    name = saved_path_for_name(filename)
    if collection is not _UNSET and collection is not None:
        validate_collection_name(collection)
    query_uuid = str(uuid.uuid4())
    timestamp = now()
    with db.transaction() as conn:
        exists = conn.execute('SELECT 1 FROM saved_queries WHERE name = ?', (name,)).fetchone()
        if exists is None:
            conn.execute('INSERT INTO saved_queries (name, collection, example) VALUES (?, NULL, 0)', (name,))
        row = conn.execute('SELECT MAX(version) AS m FROM saved_query_versions WHERE query_name = ?',
                           (name,)).fetchone()
        number = (row['m'] or 0) + 1
        conn.execute("""
            INSERT INTO saved_query_versions
                (query_name, version, uuid, status, created_at, last_modified_at, fields_json)
            VALUES (?, ?, ?, 'active', ?, ?, ?)
        """, (name, number, query_uuid, timestamp, timestamp, json.dumps(fields)))
        if collection is not _UNSET:
            conn.execute('UPDATE saved_queries SET collection = ? WHERE name = ?', (collection, name))
        if example:
            conn.execute('UPDATE saved_queries SET example = 1 WHERE name = ?', (name,))
    return query_uuid, number


def set_collection(ref, collection):
    """Move a saved query into ``collection`` (None removes it from any). Returns the previous collection.
    Not a new version - the SQL did not change - and nothing else about the query is touched."""
    if collection is not None:
        validate_collection_name(collection)
    name = resolve_saved_file(ref)
    with db.transaction() as conn:
        row = conn.execute('SELECT collection FROM saved_queries WHERE name = ?', (name,)).fetchone()
        previous = row['collection'] if row else None
        if previous != collection:
            conn.execute('UPDATE saved_queries SET collection = ? WHERE name = ?', (collection, name))
    return previous


def set_cache_ttl(ref, version, ttl):
    """Set (a positive `ttl`) or clear (0 or None) one version's cache_ttl in place. Not a new version -
    same treatment as set_collection() above - and, like save_version()'s own cache_ttl handling, only ever
    stores the key when it's truthy rather than an explicit 0. Returns the resolved version number (the one
    actually changed, whether the caller asked for a specific one or the latest)."""
    name = resolve_saved_file(ref)
    with db.transaction() as conn:
        if version is None:
            row = conn.execute(
                'SELECT version, fields_json FROM saved_query_versions WHERE query_name = ? '
                'ORDER BY version DESC LIMIT 1', (name,)).fetchone()
        else:
            row = conn.execute(
                'SELECT version, fields_json FROM saved_query_versions WHERE query_name = ? AND version = ?',
                (name, version)).fetchone()
        if row is None:
            raise ApiError(f'Version {version} not found', 404)
        fields = json.loads(row['fields_json'])
        if ttl:
            fields['cache_ttl'] = ttl
        else:
            fields.pop('cache_ttl', None)
        conn.execute('UPDATE saved_query_versions SET fields_json = ? WHERE query_name = ? AND version = ?',
                    (json.dumps(fields), name, row['version']))
        return row['version']


def _saved_files():
    """(name, path, content) for every saved query - "path" is just the name itself (see
    resolve_saved_file()'s docstring for why); content is the same nested dict shape load_versions()
    returns, reconstructed from SQL. The one shared query everything below iterates over."""
    rows = db.connection().execute('SELECT name FROM saved_queries ORDER BY name').fetchall()
    for row in rows:
        name = row['name']
        try:
            content = load_versions(name)
        except ApiError:
            continue
        yield name, name, content


def example_query_names():
    """Names of the saved queries marked as examples - the only ones ``examples unload`` may remove."""
    return [name for name, _, content in _saved_files() if read_example(content)]


def collection_members():
    """{collection name: sorted names of the queries in it} - derived from the query files themselves, the only
    place membership is stored, so it cannot disagree with them."""
    members = {}
    for name, _, content in _saved_files():
        collection = read_collection(content)
        if collection is not None:
            members.setdefault(collection, []).append(name)
    return members


def move_collection(old, new):
    """Re-file every query in ``old`` under ``new``; returns the names moved. The whole move is one
    transaction now (a real improvement over the old per-file-atomic-but-not-overall approach) - an
    interruption leaves every query still under ``old``, never a mix, and running it again is still exactly
    as safe/idempotent as collection_admin.rename_collection() already assumes."""
    validate_collection_name(new)
    moved = []
    with db.transaction() as conn:
        rows = conn.execute('SELECT name FROM saved_queries WHERE collection = ?', (old,)).fetchall()
        for row in rows:
            conn.execute('UPDATE saved_queries SET collection = ? WHERE name = ?', (new, row['name']))
            moved.append(row['name'])
    return moved


def delete_saved(ref, version=None):
    """Delete a saved query, or a single version of it (the query goes with its last version) - cascades to
    that version's/query's own execution_history via the schema's own ON DELETE CASCADE."""
    name = resolve_saved_file(ref)
    with db.transaction() as conn:
        if version is None:
            conn.execute('DELETE FROM saved_queries WHERE name = ?', (name,))
            return
        row = conn.execute(
            'SELECT 1 FROM saved_query_versions WHERE query_name = ? AND version = ?',
            (name, version)).fetchone()
        if row is None:
            raise ApiError(f'Version {version} not found', 404)
        conn.execute('DELETE FROM saved_query_versions WHERE query_name = ? AND version = ?', (name, version))
        remaining = conn.execute(
            'SELECT 1 FROM saved_query_versions WHERE query_name = ? LIMIT 1', (name,)).fetchone()
        if remaining is None:
            conn.execute('DELETE FROM saved_queries WHERE name = ?', (name,))


def record_execution(path, version, entry, sample=True):
    """Record one run in a saved version's execution_history - queued and written in the background, sampled
    and kept as configured: see history.py. Never raises and never blocks the request it's logging for, the
    same trade-off record_audit() makes."""
    history.record(path, version, entry, sample=sample)


def import_legacy_saved_queries_if_empty():
    """First-boot bootstrap for saved_sql/*.json, the same shape as import_legacy_connections_if_empty()
    above and for the same reason: only runs while saved_queries is completely empty, only ever adds, and
    the legacy folder itself is never written to or consulted again once anything exists in SQLite. Reads
    the legacy file shape directly (not through load_versions(), which now expects SQL to already have the
    data this function's whole job is to put there) - a version or history entry that doesn't parse as
    expected is skipped rather than failing the whole import, the same tolerance _saved_files() used to
    give a corrupt file."""
    if db.connection().execute('SELECT 1 FROM saved_queries LIMIT 1').fetchone() is not None:
        return
    saved_dir = config.saved_sql_dir()
    if not saved_dir.is_dir():
        return
    with db.transaction() as conn:
        for filename in sorted(os.listdir(str(saved_dir))):
            path = saved_dir / filename
            if not (filename.endswith('.json') and path.is_file()):
                continue
            name = filename[:-len('.json')]
            try:
                with open(path, 'r') as f:
                    content = json.load(f)
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(content, dict):
                continue
            collection = read_collection(content)
            example = read_example(content)
            conn.execute('INSERT OR IGNORE INTO saved_queries (name, collection, example) VALUES (?, ?, ?)',
                        (name, collection, int(example)))
            for key, data in content.items():
                if not (key.isdigit() and isinstance(data, dict)):
                    continue
                version = int(key)
                fields = {k: v for k, v in data.items() if k not in
                         ('uuid', 'created_at', 'last_modified_at', 'status', 'version', 'execution_history')}
                conn.execute("""
                    INSERT OR IGNORE INTO saved_query_versions
                        (query_name, version, uuid, status, created_at, last_modified_at, fields_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (name, version, data.get('uuid') or str(uuid.uuid4()), data.get('status') or 'active',
                      data.get('created_at') or now(), data.get('last_modified_at') or now(),
                      json.dumps(fields)))
                history = data.get('execution_history')
                for entry in history if isinstance(history, list) else []:
                    if not isinstance(entry, dict):
                        continue
                    conn.execute(
                        'INSERT INTO execution_history (query_name, version, executed_at, entry_json, status, '
                        'key_name) VALUES (?, ?, ?, ?, ?, ?)',
                        (name, version, entry.get('executed_at') or now(), json.dumps(entry, default=str),
                         entry.get('status'), entry.get('key_name')))


def import_legacy_audit_log_if_empty():
    """First-boot bootstrap for audit_log.json, same shape as the two above: only runs while audit_log is
    completely empty, reads the legacy file directly, and never touches it again afterward. A straight
    one-time copy of whatever entries are already there (already capped as of the file's last write) - the
    normal per-insert cap in record_audit() takes over from the next write onward."""
    if db.connection().execute('SELECT 1 FROM audit_log LIMIT 1').fetchone() is not None:
        return
    try:
        with open(config.audit_log_file(), 'r') as f:
            entries = json.load(f).get('entries', [])
    except (FileNotFoundError, json.JSONDecodeError):
        return
    with db.transaction() as conn:
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            conn.execute('INSERT INTO audit_log (timestamp, entry_json) VALUES (?, ?)',
                        (entry.get('timestamp') or now(), json.dumps(entry, default=str)))


def import_legacy_data_if_empty():
    """Called once at startup (app.create_app(), cli.main()): the single entry point for every first-boot
    bootstrap this module owns, so every caller only needs to remember one name. apikeys.py's own
    import_legacy_keys_if_empty()/import_legacy_roles_if_empty() are called separately by the same
    callers - store.py cannot import apikeys here without creating an import cycle (apikeys.py already
    imports store).

    Skipped entirely on Postgres: those legacy files predate SQLite and are never deleted after import, so an
    old home folder still holds a stale copy - data reaches Postgres from queryapigate.db, through
    `queryapigate migrate-to-postgres`, never from them."""
    if db.is_postgres():
        return
    import_legacy_connections_if_empty()
    import_legacy_saved_queries_if_empty()
    import_legacy_audit_log_if_empty()


def latest_versions():
    """(name, version number, data, collection) for the newest version of every saved query; unreadable
    files are skipped."""
    found = []
    for name, _, content in _saved_files():
        try:
            number, data = select_version(content)
        except ApiError:
            continue
        found.append((name, number, data, read_collection(content)))
    return found


def list_saved():
    """Return every saved query with its versions' metadata (SQL text is not included).

    A saved_sql folder that does not exist yet (nothing has ever been saved) is an empty list, not an
    error - consistent with latest_versions() above and with how a list endpoint should behave.
    """
    files = []
    for name, _, content in _saved_files():
        versions = [{
            'version': int(version),
            'author': data.get('author'),
            'description': data.get('description'),
            'tags': data.get('tags', []),
            'query_parameters': data.get('query_parameters', {}),
            'connection_name': data.get('connection_name'),
            'created_at': data.get('created_at'),
            'last_modified_at': data.get('last_modified_at'),
            'status': data.get('status'),
            'execution_history': data.get('execution_history', []),
            'cache_ttl': data.get('cache_ttl') or None,
        } for version, data in content.items() if version.isdigit() and isinstance(data, dict)]
        versions.sort(key=lambda v: v['version'])
        files.append({'filename': name, 'collection': read_collection(content), 'example': read_example(content),
                      'versions': versions})
    return files

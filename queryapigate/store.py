"""Persistence: the connection registry and the saved-query files (plain JSON on disk).

Everything that touches those files goes through here so that access stays confined to the
configured home folder and read-modify-write cycles are serialised.
"""
import json
import os
import re
import threading
import uuid
from datetime import datetime

from . import config, db
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


def write_json_atomic(path, data):
    tmp_path = f'{path}.{uuid.uuid4().hex}.tmp'
    with open(tmp_path, 'w') as f:
        json.dump(data, f, indent=4)
    os.replace(tmp_path, path)


def now():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


# --------------------------------------------------------------------------------------
# Audit log: a durable record of administrative changes (API keys, connections, saved
# queries) - distinct from execution_history below (query *runs*) and from the ephemeral,
# stdout-only access log (app.decorate()). See app.py's route handlers for what calls this
# and with what `changes` shape; this module only persists whatever it's given.
# --------------------------------------------------------------------------------------

def record_audit(actor, action, target, changes=None):
    """Append one entry (newest last, capped at config.audit_log_limit() - same shape as execution_history
    below) and, when QUERYAPIGATE_AUDIT_LOG_EXPORT_FILE is set, also append it there - a second, never-capped
    copy for retention beyond audit_log.json's own rolling window. Never raises: an audit-log write failing
    should not block the action it's recording, the same trade-off record_execution() already makes for
    query-run history; the two writes are independently fault-tolerant so a problem with one (e.g. the
    export path's directory missing) never suppresses the other."""
    entry = {'timestamp': now(), 'actor': actor, 'action': action, 'target': target, 'changes': changes}
    try:
        with lock:
            path = config.audit_log_file()
            try:
                with open(path, 'r') as f:
                    entries = json.load(f).get('entries', [])
            except (FileNotFoundError, json.JSONDecodeError):
                entries = []
            entries.append(entry)
            del entries[:-config.audit_log_limit()]
            config.home().mkdir(parents=True, exist_ok=True)
            write_json_atomic(path, {'entries': entries})
    except OSError:
        pass
    export_path = config.audit_log_export_file()
    if export_path is not None:
        try:
            with lock, open(export_path, 'a') as f:
                f.write(json.dumps(entry, default=str) + '\n')
        except OSError:
            pass


def read_audit_log():
    try:
        with open(config.audit_log_file(), 'r') as f:
            return json.load(f).get('entries', [])
    except (FileNotFoundError, json.JSONDecodeError):
        return []


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
    """Path of the saved-query file for a client-supplied name (without extension)."""
    if not isinstance(filename, str) or not _FILENAME_RE.match(filename) or filename.startswith('.'):
        raise ApiError("Filename may only contain letters, digits, spaces, '.', '_' and '-'")
    return os.path.join(config.saved_sql_dir(), f'{filename}.json')


def resolve_saved_file(ref):
    """Resolve a client-supplied file reference to an existing .json file inside the saved_sql folder.

    Accepts a bare name ("cht" or "cht.json"), a path relative to the home folder
    ("saved_sql/cht.json") or an absolute path - as long as it ends up in the saved_sql folder.
    """
    if not isinstance(ref, str) or not ref.strip():
        raise ApiError('Filename is missing')
    saved_dir = str(config.saved_sql_dir())
    if os.path.isabs(ref):
        candidate = ref
    elif os.sep in ref or '/' in ref:
        candidate = os.path.join(config.home(), ref)
    else:
        candidate = os.path.join(saved_dir, ref if ref.endswith('.json') else f'{ref}.json')
    candidate = os.path.realpath(candidate)
    if os.path.dirname(candidate) != os.path.realpath(saved_dir) or not candidate.endswith('.json'):
        raise ApiError('Only .json files inside the saved_sql folder can be accessed', 403)
    if not os.path.isfile(candidate):
        raise ApiError('File not found', 404)
    return candidate


def query_name(path):
    """The canonical saved-query name for a path resolve_saved_file() returned - the same string an admin
    types into an API key's ``queries`` grant (see apikeys.py), regardless of which of resolve_saved_file()'s
    accepted reference forms (bare name, relative path, absolute path) the caller originally used."""
    return os.path.splitext(os.path.basename(path))[0]


def load_versions(path):
    try:
        with open(path, 'r') as f:
            content = json.load(f)
    except json.JSONDecodeError:
        raise ApiError('Saved query file is not valid JSON', 500) from None
    if not isinstance(content, dict):
        raise ApiError('Saved query file has an unexpected structure', 500)
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
    a name sets it; None clears it. It is written in the same atomic file write as the new version, so a
    query is never saved without the collection it was saved with.

    ``example=True`` marks the query as installed by ``queryapigate examples load`` (a top-level ``"example":
    true`` on the file, written in the same atomic write), which is how ``examples unload`` knows exactly what is
    its own to remove. It never clears the mark."""
    path = saved_path_for_name(filename)
    if collection is not _UNSET and collection is not None:
        validate_collection_name(collection)
    query_uuid = str(uuid.uuid4())
    timestamp = now()
    with lock:
        os.makedirs(config.saved_sql_dir(), exist_ok=True)
        versions = load_versions(path) if os.path.exists(path) else {}
        number = max(version_numbers(versions), default=0) + 1
        versions[str(number)] = {
            'uuid': query_uuid,
            **fields,
            'created_at': timestamp,
            'last_modified_at': timestamp,
            'status': 'active',
            'version': number,
            'execution_history': [],
        }
        if collection is not _UNSET:
            _apply_collection(versions, collection)
        if example:
            versions['example'] = True
        write_json_atomic(path, versions)
    return query_uuid, number


def set_collection(ref, collection):
    """Move a saved query into ``collection`` (None removes it from any). Returns the previous collection.
    Not a new version - the SQL did not change - and nothing else in the file is touched."""
    if collection is not None:
        validate_collection_name(collection)
    path = resolve_saved_file(ref)
    with lock:
        content = load_versions(path)
        previous = read_collection(content)
        if previous != collection or ('collection' in content) != (collection is not None):
            _apply_collection(content, collection)
            write_json_atomic(path, content)
    return previous


def set_cache_ttl(ref, version, ttl):
    """Set (a positive `ttl`) or clear (0 or None) one version's cache_ttl in place. Not a new version -
    same treatment as set_collection() above - and, like save_version()'s own cache_ttl handling, only ever
    writes the key when it's truthy rather than storing an explicit 0. Returns the resolved version number
    (the one actually changed, whether the caller asked for a specific one or the latest)."""
    path = resolve_saved_file(ref)
    with lock:
        content = load_versions(path)
        number, data = select_version(content, version)
        if ttl:
            data['cache_ttl'] = ttl
        else:
            data.pop('cache_ttl', None)
        write_json_atomic(path, content)
    return number


def _saved_files():
    """(name, path, content) for every readable saved-query file; a folder that does not exist yet (nothing
    saved) is empty and an unreadable file is skipped - the one directory walk everything below shares."""
    saved_dir = str(config.saved_sql_dir())
    if not os.path.isdir(saved_dir):
        return
    for filename in sorted(os.listdir(saved_dir)):
        path = os.path.join(saved_dir, filename)
        if not (filename.endswith('.json') and os.path.isfile(path)):
            continue
        try:
            content = load_versions(path)
        except ApiError:
            continue
        yield filename[:-5], path, content


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
    """Re-file every query in ``old`` under ``new``; returns the names moved. Each file is rewritten
    atomically, so an interruption leaves some queries under ``old`` and the rest under ``new`` - never a
    half-written file - and running it again finishes the job (collection_admin.rename_collection())."""
    validate_collection_name(new)
    moved = []
    with lock:
        for name, path, content in _saved_files():
            if read_collection(content) == old:
                content['collection'] = new
                write_json_atomic(path, content)
                moved.append(name)
    return moved


def delete_saved(ref, version=None):
    """Delete a saved query file, or a single version of it (the file goes with its last version)."""
    path = resolve_saved_file(ref)
    with lock:
        if version is None:
            os.remove(path)
            return
        content = load_versions(path)
        if str(version) not in content:
            raise ApiError(f'Version {version} not found', 404)
        del content[str(version)]
        if version_numbers(content):
            write_json_atomic(path, content)
        else:
            os.remove(path)


def record_execution(path, version, entry):
    """Append to a saved version's execution_history (newest last, capped). Never raises."""
    try:
        with lock:
            content = load_versions(path)
            data = content.get(str(version))
            if not isinstance(data, dict):
                return
            history = data.setdefault('execution_history', [])
            history.append(entry)
            del history[:-config.HISTORY_LIMIT]
            write_json_atomic(path, content)
    except (OSError, ApiError):
        pass


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

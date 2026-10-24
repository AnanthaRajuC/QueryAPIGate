"""Persistence: the connection registry, saved queries, and the audit log - all SQLite, via db.py (see
db.py's own docstring for why). API keys and roles live in the same queryapigate.db but are owned by
apikeys.py, which has its own read/write functions against the same db.py connection.

Every saved-query function still speaks in terms of a "path" for backward compatibility with every
caller - resolve_saved_file()'s own docstring explains why that's now just the query's name, not a real
filesystem path.
"""
import contextlib
import json
import logging
import os
import re
import threading
import uuid
from datetime import datetime

from . import config, db, duckfiles, history, metacache, scope
from .errors import ApiError

# Serialises read-modify-write cycles on the JSON files this app manages.
lock = threading.RLock()

_FILENAME_RE = re.compile(r'^[\w .\-]{1,100}$')
_ENC_PREFIX = 'enc:'  # marks a connection password as Fernet-encrypted - see _encrypt_password()
# One canonical spelling, rejected (never silently case-folded) when violated, so "Reporting" and "reporting"
# can never both exist as two collections.
_COLLECTION_RE = re.compile(r'^[a-z0-9][a-z0-9._-]{0,62}$')
_UNSET = object()  # "argument not passed" - distinct from an explicit None, which clears a collection
_SERVER_DB_TYPES = frozenset({'postgres', 'mysql', 'clickhouse', 'mongo', 'trino'})  # see refuse_store_access()


def now():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def server_time_zone(moment=None):
    """The zone now()'s wall-clock times are in - every timestamp this server stores and returns - so a client can
    show them in its own: (IANA name or None, current UTC offset like '+05:30'). The name comes from TZ,
    /etc/localtime or /etc/timezone, and only if it agrees with the offset in effect; without one, a client can only
    apply the current offset, which is off by an hour for times on the other side of a daylight-saving change."""
    local = (moment or datetime.now()).astimezone()
    offset = local.utcoffset()
    minutes = int(offset.total_seconds() // 60) if offset is not None else 0
    sign = '-' if minutes < 0 else '+'
    text = f'{sign}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}'
    return _zone_name(local), text


def _zone_name(local):
    from zoneinfo import ZoneInfo

    candidates = [os.environ.get('TZ', '').lstrip(':')]
    link = os.path.realpath('/etc/localtime')
    if '/zoneinfo/' in link:
        candidates.append(link.split('/zoneinfo/', 1)[1])
    try:
        with open('/etc/timezone') as f:
            candidates.append(f.read().strip())
    except OSError:
        pass
    for name in filter(None, candidates):
        try:
            if local.astimezone(ZoneInfo(name)).utcoffset() == local.utcoffset():
                return name
        except (ValueError, OSError, KeyError):  # not a zone name this system knows
            continue
    return None


# --------------------------------------------------------------------------------------
# Audit log: a durable record of administrative changes (API keys, connections, saved
# queries) - distinct from execution_history below (query *runs*) and from the ephemeral,
# stdout-only access log (app.decorate()). See app.py's route handlers for what calls this
# and with what `changes` shape; this module only persists whatever it's given.
# --------------------------------------------------------------------------------------

_audit_local = threading.local()


@contextlib.contextmanager
def audit_via(via):
    """Every audit entry this thread records in the block says it came `via` this - 'config' for the changes a
    configuration file makes (ADR 0014), however the caller who applied it authenticated."""
    previous = getattr(_audit_local, 'via', None)
    _audit_local.via = via
    try:
        yield
    finally:
        _audit_local.via = previous


def _audit_via(actor):
    """How the actor authenticated (ADR 0003): 'token', 'break-glass' or 'open' from the request's caller, whoever
    they are; outside a request, 'cli' or 'startup' for the server's own changes. The request comes first, so an
    actor's name can never stand in for how they signed in - unless audit_via() says how the change was made."""
    if getattr(_audit_local, 'via', None) is not None:
        return _audit_local.via
    from flask import g, has_request_context  # a core dependency; imported here to keep the store Flask-free
    permission = g.get('permission') if has_request_context() else None
    if permission is not None:
        return permission.via
    return actor if actor in ('cli', 'startup') else None


# Audit actions about the server and the people who run it, not one workspace's things (ADR 0023): no workspace.
SERVER_AUDIT_ACTIONS = frozenset({
    'create_admin', 'update_admin', 'delete_admin', 'issue_admin_token', 'revoke_admin_token', 'link_admin_sso',
    'sign_in', 'sign_in_refused', 'sign_out', 'break_glass_used', 'rekey', 'forget_caller', 'audit_key_rotated',
    'audit_key_set'})
# ... and those about a workspace itself, which belong to it: its target is the workspace's name.
WORKSPACE_AUDIT_ACTIONS = frozenset({'create_workspace', 'update_workspace', 'delete_workspace',
                                     'set_workspace_member', 'remove_workspace_member'})


def _audit_workspace(action, target):
    if action in SERVER_AUDIT_ACTIONS:
        return None
    if action in WORKSPACE_AUDIT_ACTIONS:
        return target
    return scope.current()


def record_audit(actor, action, target, changes=None, via=None):
    """Append one entry (oldest first, capped at config.audit_log_limit() - same per-insert SQL trim
    record_execution() below uses for execution_history) and, when QUERYAPIGATE_AUDIT_LOG_EXPORT_FILE is
    set, also append it to that plain file - a second, never-capped copy for retention beyond the SQLite
    table's own rolling window, independent of where the primary log lives. Never raises: an audit-log
    write failing should not block the action it's recording, the same trade-off record_execution() already
    makes for query-run history; the two writes are independently fault-tolerant so a problem with one
    (e.g. the export path's directory missing) never suppresses the other."""
    # `workspace`: where it happened - in the entry, so the hash chain covers it (ADR 0023 §5); None for the server
    entry = {'timestamp': now(), 'actor': actor, 'via': via or _audit_via(actor), 'action': action, 'target': target,
             'changes': changes, 'workspace': _audit_workspace(action, target)}
    from . import auditchain
    link = None
    try:
        with db.transaction() as conn:
            # the next link of the hash chain (ADR 0012), then the rolling cap - under the store's write lock, so
            # several instances still make one chain
            link = auditchain.append(conn, entry, config.audit_log_limit())
    except (OSError, *db.Error):
        # The action stands, but it has no entry: said loudly, never silently (review, 2026-10-15)
        logging.getLogger('queryapigate').error('Audit log: %s on %s by %s happened, but its entry could not be '
                                                'written', action, target, actor, exc_info=True)

    def deliver():
        from . import auditstream
        auditstream.wake()  # to the SIEM soon, not at the next round
        export_path = config.audit_log_export_file()
        if export_path is not None:
            chain = {'seq': link[0], 'prev_hash': link[1], 'hash': link[2]} if link else {}
            try:
                with lock, open(export_path, 'a') as f:
                    f.write(json.dumps({**entry, **chain}, default=str) + '\n')
            except OSError:
                pass

    # Inside a larger transaction (a configuration file applied, ADR 0014), only once it commits: rolled back, the
    # entry never existed - and its seq is used again by the next one.
    db.after_commit(deliver)


def read_audit_entries():
    """Every stored entry with its place in the hash chain (`seq`, `hash`), oldest first - for /api/v1/audit."""
    rows = db.connection().execute('SELECT entry_json, seq, hash FROM audit_log ORDER BY id').fetchall()
    return [{**json.loads(row['entry_json']), 'seq': row['seq'], 'hash': row['hash']} for row in rows]


def read_audit_log():
    """Every stored entry, oldest first (the same order the JSON file's list was always in) - app.py's
    /api/v1/audit (services/audit.py) reverses this to present newest first."""
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


def workspace_names():
    """Every workspace's name (ADR 0023) - `default` always among them, also on a store not yet upgraded to have
    them (read before db.init_schema() by `queryapigate events`)."""
    try:
        return [row[0] for row in db.connection().execute('SELECT name FROM workspaces ORDER BY name').fetchall()]
    except db.Error:
        return [db.DEFAULT_WORKSPACE]


def read_connections():
    """Every connection of the workspace this code acts in (scope.py), by name."""
    rows = db.connection().execute(
        'SELECT name, db, active, created_at, updated_at, details_json FROM connections WHERE workspace = ?',
        (scope.current(),)).fetchall()
    return {row['name']: _connection_row_to_dict(row) for row in rows}


def _server_setting(name):
    from . import secretrefs
    return secretrefs.server_setting(name)


def refuse_server_settings(fields):
    """Refuse - when a connection or destination is saved or tried - a reference it may not hold (secretrefs.py, ADR
    0015): to one of the server's own QUERYAPIGATE_* or OTEL_* settings, to a secret provider there is none of, or to
    what a provider's allow-list doesn't reach."""
    from . import secretrefs
    secretrefs.check_fields(fields)


def refuse_store_access(details):
    """Refuse a connection that would open this server's own store - a SQLite/DuckDB/H2 `database` that is
    queryapigate.db (or its -wal/-shm/-journal), or a DuckDB `allowed_paths` entry covering it. Through one, an
    administrator could read key and token hashes and secrets, or - with writes on - give themselves an owner's token.
    A PostgreSQL store is kept apart by its own credentials: don't give administrators them.

    A server database (PostgreSQL, MySQL, ClickHouse, MongoDB, Trino) opens no file, whatever its `database` says, so
    it skips the path checks - they resolve paths on disk, several system calls, and this runs on every call. Any
    other type, including one this list doesn't know, is checked."""
    if not isinstance(details, dict) or details.get('db') in _SERVER_DB_TYPES:
        return
    store_file = os.path.realpath(str(config.db_file()))
    guarded = {store_file, *(store_file + suffix for suffix in ('-wal', '-shm', '-journal'))}
    database = details.get('database')
    if isinstance(database, str) and database not in ('', ':memory:'):
        path = database[len('file:'):] if database.startswith('file:') else database
        if os.path.realpath(os.path.expanduser(path.split('?', 1)[0])) in guarded:
            raise ApiError("A connection can't open this server's own store", 403, code='store_not_allowed')
    for entry in details.get('allowed_paths') or ():
        if isinstance(entry, str) and not entry.startswith(duckfiles.REMOTE_SCHEMES):
            real = os.path.realpath(entry)
            if real in guarded or store_file.startswith(real.rstrip(os.sep) + os.sep):
                raise ApiError(f"allowed_paths entry '{entry}' would let the connection read this server's own store "
                               '- allow a folder that does not contain it', 403, code='store_not_allowed')


def _expand_env(value):
    """Replace the references in a connection value - ${VAR}, ${file:...} - with what they name (secretrefs.py)."""
    from . import secretrefs
    return secretrefs.expand(value)


def _is_env_ref(value):
    """A whole value that is one reference: shown as written, never encrypted - it isn't a secret, only names one."""
    from . import secretrefs
    return secretrefs.is_reference(value)


def _is_encrypted(value):
    return isinstance(value, str) and value.startswith(_ENC_PREFIX)


def _get_fernet():
    """What encrypts and decrypts passwords at rest (QUERYAPIGATE_SECRET_KEY), or None when unset - encryption at
    rest is opt-in. With several keys (rotating, ADR 0009) a MultiFernet: it encrypts with the first and decrypts
    with whichever works. The keys are already validated at startup (config.check_secret_keys())."""
    keys = config.secret_keys()
    if not keys:
        return None
    from cryptography.fernet import Fernet, MultiFernet
    fernets = [Fernet(key.encode('utf-8')) for key in keys]
    return fernets[0] if len(fernets) == 1 else MultiFernet(fernets)


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
                       'cannot be decrypted', 500, code='secret_key_required')
    from cryptography.fernet import InvalidToken
    try:
        return fernet.decrypt(value[len(_ENC_PREFIX):].encode('ascii')).decode('utf-8')
    except InvalidToken:
        keys = len(config.secret_keys())
        which = 'QUERYAPIGATE_SECRET_KEY' if keys == 1 else f'any of the {keys} keys in QUERYAPIGATE_SECRET_KEY'
        raise ApiError(f"This connection's password could not be decrypted with {which} - it was encrypted with a "
                       'key that is no longer listed. Add that key back (after the first), or enter the password '
                       'again', 500, code='password_undecryptable') from None


def resolve_ad_hoc(details):
    """Like get_connection(), but for connection fields given directly rather than a saved, named connection
    - used only by ``POST /api/v1/connections/test`` (the New/Edit connection form's "Test connection" button)
    to try fields that may never be saved. Expands ${VAR} references and decrypts an already-encrypted
    password the same way; a literal password already in ``details`` (the common case - a value just typed
    into the form) passes through unchanged."""
    refuse_server_settings(details)
    refuse_store_access(details)
    from . import secretrefs
    resolved = secretrefs.resolve_credentials({key: _expand_env(value) for key, value in details.items()})
    password = resolved.get('password')
    if password and _is_encrypted(password):
        resolved['password'] = _decrypt_password(password)
    return resolved


def get_connection(connection_name):
    """Return the usable (active, env-expanded, password-decrypted) details of a named connection."""
    if not isinstance(connection_name, str) or not connection_name:
        raise ApiError('Connection name is missing', code='connection_required')
    workspace = scope.current()
    found = metacache.rows('connection', (workspace, connection_name), lambda: db.connection().execute(
        'SELECT db, active, created_at, updated_at, details_json FROM connections WHERE workspace = ? AND name = ?',
        (workspace, connection_name)).fetchall())
    row = found[0] if found else None
    if not row:
        raise ApiError(f"Connection '{connection_name}' not found", 404, code='connection_not_found')
    if not row['active']:
        raise ApiError(f"The connection '{connection_name}' is currently not active. "
                       "To use it, it must be set to active.", 403, code='connection_inactive')
    if row['db'] not in config.SUPPORTED_DB_TYPES:
        raise ApiError('Unsupported database type', code='unsupported_database')
    details = _connection_row_to_dict(row)
    refuse_store_access(details)  # one saved before this check existed, too
    from . import secretrefs
    resolved = {key: _expand_env(value) for key, value in details.items()}
    if 'password' in resolved:
        resolved['password'] = _decrypt_password(resolved['password'])
    return secretrefs.resolve_credentials(resolved)  # a lease's user and password, when it names a source


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
    next save of it - "don't require a one-time manual migration step" from the start."""
    with db.transaction() as conn:
        # destinations too (ADR 0004): their secret is stored the same way, and one saved before the key existed
        # would otherwise stay literal for good - `rekey` only moves values that are already encrypted
        for table in ('connections', 'destinations'):
            for row in conn.execute(f'SELECT name, details_json FROM {table}').fetchall():
                details = json.loads(row['details_json'])
                password = details.get('password')
                if _is_plaintext_password(password):
                    details['password'] = _encrypt_password(password)
                    conn.execute(f'UPDATE {table} SET details_json = ? WHERE name = ?',
                                 (json.dumps(details), row['name']))


def encrypted_password_connections():
    """Names of connections whose password is encrypted at rest - used only for the startup warning that
    fires when QUERYAPIGATE_SECRET_KEY is missing but encrypted passwords already exist on disk (a rotated-out
    or removed key, most likely): those connections cannot be used until the key is restored."""
    return sorted(name for name, conn in read_connections().items() if _is_encrypted(conn.get('password')))


def update_connections(connections):
    for name, details in connections.items():
        refuse_server_settings(details if isinstance(details, dict) else {})
        refuse_store_access(details)
        if not isinstance(details, dict) or details.get('db') not in config.SUPPORTED_DB_TYPES:
            raise ApiError(f"Connection '{name}' must be an object whose 'db' is one of: "
                           f"{', '.join(config.SUPPORTED_DB_TYPES)}")
        if details.get('db') == 'duckdb':
            duckfiles.validate(details)
    workspace = scope.current()
    with db.transaction() as conn:
        timestamp = now()
        for name, details in connections.items():
            existing_row = conn.execute(
                'SELECT created_at, details_json FROM connections WHERE workspace = ? AND name = ?',
                (workspace, name)).fetchone()
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
            # what GET /api/v1/connections returned must not be able to fake either one). A connection that already
            # existed keeps its created_at (the column is NOT NULL, so any existing row already has a real
            # one - no legacy-data backfill needed here, only in the one-time JSON migration itself).
            created_at = existing_row['created_at'] if existing_row else timestamp
            dialect = details.get('db')
            active = bool(details.get('active', False))
            extra = {k: v for k, v in details.items() if k not in ('db', 'active', 'created_at', 'updated_at')}
            conn.execute("""
                INSERT INTO connections (workspace, name, db, active, created_at, updated_at, details_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(workspace, name) DO UPDATE SET
                    db = excluded.db, active = excluded.active,
                    created_at = excluded.created_at, updated_at = excluded.updated_at,
                    details_json = excluded.details_json
            """, (workspace, name, dialect, int(active), created_at, timestamp, json.dumps(extra)))


def delete_connection(name):
    with db.transaction() as conn:
        cur = conn.execute('DELETE FROM connections WHERE workspace = ? AND name = ?', (scope.current(), name))
        if cur.rowcount == 0:
            raise ApiError(f"Connection '{name}' not found", 404, code='connection_not_found')


# --------------------------------------------------------------------------------------
# Saved queries (all access is confined to the saved_sql folder)
# --------------------------------------------------------------------------------------

def saved_path_for_name(filename):
    """Validate a client-supplied saved-query name; returns it unchanged. No real file path is involved
    once saved queries live in SQLite - kept as the one shared name-shape validator (bundle.py also calls
    this purely for its validation side effect) so a name is held to the same rule everywhere."""
    if not isinstance(filename, str) or not _FILENAME_RE.match(filename) or filename.startswith('.'):
        raise ApiError("Filename may only contain letters, digits, spaces, '.', '_' and '-'", code='invalid_name')
    return filename


def saved_query_exists(name):
    """A plain existence check by exact name - used by examples.py's conflict detection, which needs to
    know "is something already using this name" without resolve_saved_file()'s reference-parsing/403
    semantics (a conflict is never a client-facing path, just an internal decision)."""
    return db.connection().execute('SELECT 1 FROM saved_queries WHERE workspace = ? AND name = ?',
                                   (scope.current(), name)).fetchone() is not None


def resolve_saved_file(ref, check_exists=True):
    """Resolve a client-supplied reference to an existing saved query, returning its canonical name.

    No real file path is involved once saved queries live in SQLite - but every caller already treats this
    return value as opaque (passing it straight to load_versions()/query_name()/record_execution()), so
    nothing outside this module needs to change. Still accepts every form it always has - a bare name
    ("cht" or "cht.json"), a path relative to the home folder ("saved_sql/cht.json") or an absolute path -
    for backward compatibility; anything that doesn't reduce to a single valid name (a path escaping
    saved_sql/, a name containing a separator) is rejected the same way an out-of-bounds path used to be.
    ``check_exists=False`` skips the lookup for a caller that loads the query next anyway (load_versions() raises the
    same 404), saving a round trip on every run.
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
    if check_exists and db.connection().execute('SELECT 1 FROM saved_queries WHERE workspace = ? AND name = ?',
                                                (scope.current(), candidate)).fetchone() is None:
        raise ApiError(f"Saved query '{candidate}' not found", 404, code='query_not_found')
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
    are stored - with a retention period (history.py) a version can keep far more, paged through GET /api/v1/history
    instead. Runs this process has queued but not written yet are written first, so a caller always sees its
    own. ``with_history=False`` leaves every execution_history empty instead - for the request path that only
    runs a query (app.run_saved()), which never reads its history but would otherwise load it on every call."""
    if with_history:
        history.flush()
    workspace = scope.current()
    # The query and its versions in one round trip: a run reads this on every call. A query with no versions yet
    # still has its one row, with every v.* column NULL.
    def load():
        return db.connection().execute(
            'SELECT q.collection, q.example, q.published_version, v.version, v.uuid, v.status, v.created_at, '
            'v.last_modified_at, v.fields_json FROM saved_queries q LEFT JOIN saved_query_versions v '
            'ON v.workspace = q.workspace AND v.query_name = q.name WHERE q.workspace = ? AND q.name = ? '
            'ORDER BY v.version', (workspace, name)).fetchall()
    # Only a run (no history) may be served from metacache.py: what the Console reads while editing is always fresh.
    rows = metacache.rows('saved_query', (workspace, name), load) if not with_history else load()
    if not rows:
        raise ApiError(f"Saved query '{name}' not found", 404, code='query_not_found')
    row = rows[0]
    content = {}
    if row['collection'] is not None:
        content['collection'] = row['collection']
    if row['example']:
        content['example'] = True
    if row['published_version'] is not None:
        content['published_version'] = row['published_version']
    for v in rows:
        if v['version'] is None:
            continue
        try:
            fields = json.loads(v['fields_json'])
        except json.JSONDecodeError:
            raise ApiError('Saved query data is not valid JSON', 500) from None
        if not isinstance(fields, dict):
            raise ApiError('Saved query data has an unexpected structure', 500)
        history_rows = db.connection().execute(
            'SELECT entry_json FROM (SELECT entry_json, executed_at, rowid FROM execution_history '
            'WHERE workspace = ? AND query_name = ? AND version = ? ORDER BY executed_at DESC, rowid DESC LIMIT ?) '
            'AS newest ORDER BY executed_at, rowid', (workspace, name, v['version'], config.history_limit())
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


def read_published(content):
    """The version number a loaded saved query serves (/q/<name>, MCP, the catalog), or None when it has no
    published version - every newer version is a draft until published."""
    value = content.get('published_version')
    return value if isinstance(value, int) and str(value) in content else None


def is_draft(content, version):
    """Whether ``version`` was never live: newer than the published version, or the query has none. Versions older
    than the published one were each live once (the newest version used to always be served), so they stay
    runnable with ?version=, as before."""
    published = read_published(content)
    return published is None or version > published


def select_version(content, version=None, default='published'):
    """Return (number, data) for the requested version. With none requested: the published version
    (``default='published'``, for everything that serves a query), or the newest one (``default='latest'``, for
    admin tooling that works on the version being edited)."""
    if version is None:
        numbers = version_numbers(content)
        if not numbers:
            raise ApiError('No valid versions found in the file')
        if default == 'latest':
            version = max(numbers)
        else:
            version = read_published(content)
            if version is None:
                raise ApiError('This query has no published version', 404, code='not_published')
    data = content.get(str(version))
    if not isinstance(data, dict):
        raise ApiError(f'Version {version} not found', 404, code='version_not_found')
    return int(version), data


def validate_collection_name(name):
    """The one place a collection name is checked - saving a query, moving it, granting a key access and
    importing a bundle all go through here, so they cannot disagree about what a valid name is."""
    if not isinstance(name, str) or not _COLLECTION_RE.match(name):
        raise ApiError("A collection name must be 1-63 characters: lowercase letters, digits, '.', '_' and '-', "
                       'starting with a letter or digit', code='invalid_name')
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


def save_version(filename, fields, collection=_UNSET, example=False, publish=True):
    """Store ``fields`` as the next version of a saved query; returns (uuid, version number).

    ``publish=True`` (every caller before ADR 0001's Management API: the legacy save route, collection import,
    examples) makes the new version the one served at once - the behaviour those callers always had.
    ``publish=False`` stores it as a draft, leaving the published version as it is.

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
    ws = scope.current()
    with db.transaction() as conn:
        exists = conn.execute('SELECT 1 FROM saved_queries WHERE workspace = ? AND name = ?', (ws, name)).fetchone()
        if exists is None:
            conn.execute('INSERT INTO saved_queries (workspace, name, collection, example) VALUES (?, ?, NULL, 0)',
                         (ws, name))
        row = conn.execute('SELECT MAX(version) AS m FROM saved_query_versions WHERE workspace = ? AND query_name = ?',
                           (ws, name)).fetchone()
        number = (row['m'] or 0) + 1
        conn.execute("""
            INSERT INTO saved_query_versions
                (workspace, query_name, version, uuid, status, created_at, last_modified_at, fields_json)
            VALUES (?, ?, ?, ?, 'active', ?, ?, ?)
        """, (ws, name, number, query_uuid, timestamp, timestamp, json.dumps(fields)))
        if collection is not _UNSET:
            conn.execute('UPDATE saved_queries SET collection = ? WHERE workspace = ? AND name = ?',
                         (collection, ws, name))
        if example:
            conn.execute('UPDATE saved_queries SET example = 1 WHERE workspace = ? AND name = ?', (ws, name))
        if publish:
            conn.execute('UPDATE saved_queries SET published_version = ? WHERE workspace = ? AND name = ?',
                         (number, ws, name))
    return query_uuid, number


def publish_version(ref, version):
    """Make ``version`` the one a saved query serves. Returns the previously published version (or None).
    Any existing version can be published, older ones included - that is how a bad release is rolled back."""
    name = resolve_saved_file(ref)
    ws = scope.current()
    with db.transaction() as conn:
        row = conn.execute('SELECT 1 FROM saved_query_versions WHERE workspace = ? AND query_name = ? AND version = ?',
                           (ws, name, version)).fetchone()
        if row is None:
            raise ApiError(f'Version {version} not found', 404, code='version_not_found')
        previous = conn.execute('SELECT published_version FROM saved_queries WHERE workspace = ? AND name = ?',
                                (ws, name)).fetchone()['published_version']
        conn.execute('UPDATE saved_queries SET published_version = ? WHERE workspace = ? AND name = ?',
                     (version, ws, name))
    return previous


def unpublish(ref):
    """Stop serving a saved query at all (it keeps every version). Returns the previously published version."""
    name = resolve_saved_file(ref)
    ws = scope.current()
    with db.transaction() as conn:
        previous = conn.execute('SELECT published_version FROM saved_queries WHERE workspace = ? AND name = ?',
                                (ws, name)).fetchone()['published_version']
        conn.execute('UPDATE saved_queries SET published_version = NULL WHERE workspace = ? AND name = ?', (ws, name))
    return previous


def set_collection(ref, collection):
    """Move a saved query into ``collection`` (None removes it from any). Returns the previous collection.
    Not a new version - the SQL did not change - and nothing else about the query is touched."""
    if collection is not None:
        validate_collection_name(collection)
    name = resolve_saved_file(ref)
    ws = scope.current()
    with db.transaction() as conn:
        row = conn.execute('SELECT collection FROM saved_queries WHERE workspace = ? AND name = ?',
                           (ws, name)).fetchone()
        previous = row['collection'] if row else None
        if previous != collection:
            conn.execute('UPDATE saved_queries SET collection = ? WHERE workspace = ? AND name = ?',
                         (collection, ws, name))
    return previous


def set_cache_ttl(ref, version, ttl):
    """Set (a positive `ttl`) or clear (0 or None) one version's cache_ttl in place. Not a new version -
    same treatment as set_collection() above - and, like save_version()'s own cache_ttl handling, only ever
    stores the key when it's truthy rather than an explicit 0. Returns the resolved version number (the one
    actually changed, whether the caller asked for a specific one or the latest)."""
    name = resolve_saved_file(ref)
    ws = scope.current()
    with db.transaction() as conn:
        if version is None:
            row = conn.execute(
                'SELECT version, fields_json FROM saved_query_versions WHERE workspace = ? AND query_name = ? '
                'ORDER BY version DESC LIMIT 1', (ws, name)).fetchone()
        else:
            row = conn.execute(
                'SELECT version, fields_json FROM saved_query_versions WHERE workspace = ? AND query_name = ? '
                'AND version = ?', (ws, name, version)).fetchone()
        if row is None:
            raise ApiError(f'Version {version} not found', 404, code='version_not_found')
        fields = json.loads(row['fields_json'])
        if ttl:
            fields['cache_ttl'] = ttl
        else:
            fields.pop('cache_ttl', None)
        conn.execute('UPDATE saved_query_versions SET fields_json = ? WHERE workspace = ? AND query_name = ? '
                     'AND version = ?', (json.dumps(fields), ws, name, row['version']))
        return row['version']


def set_mcp(ref, version, enabled):
    """Turn one version's MCP exposure on or off in place (BACKLOG #54) - not a new version, same treatment as
    set_cache_ttl() above. Stored only when off (`mcp: false`), so a query exposed over MCP - the default - keeps its
    stored shape unchanged. Returns the resolved version number actually changed."""
    name = resolve_saved_file(ref)
    ws = scope.current()
    with db.transaction() as conn:
        if version is None:
            row = conn.execute(
                'SELECT version, fields_json FROM saved_query_versions WHERE workspace = ? AND query_name = ? '
                'ORDER BY version DESC LIMIT 1', (ws, name)).fetchone()
        else:
            row = conn.execute(
                'SELECT version, fields_json FROM saved_query_versions WHERE workspace = ? AND query_name = ? '
                'AND version = ?', (ws, name, version)).fetchone()
        if row is None:
            raise ApiError(f'Version {version} not found', 404, code='version_not_found')
        fields = json.loads(row['fields_json'])
        if enabled:
            fields.pop('mcp', None)
        else:
            fields['mcp'] = False
        conn.execute('UPDATE saved_query_versions SET fields_json = ? WHERE workspace = ? AND query_name = ? '
                     'AND version = ?', (json.dumps(fields), ws, name, row['version']))
        return row['version']


def _saved_files():
    """(name, path, content) for every saved query - "path" is just the name itself (see
    resolve_saved_file()'s docstring for why); content is the same nested dict shape load_versions()
    returns, reconstructed from SQL. The one shared query everything below iterates over - in the workspace this
    code acts in."""
    rows = db.connection().execute('SELECT name FROM saved_queries WHERE workspace = ? ORDER BY name',
                                   (scope.current(),)).fetchall()
    for row in rows:
        name = row['name']
        try:
            content = load_versions(name)
        except ApiError:
            continue
        yield name, name, content


def iter_saved(with_history=False):
    """(name, content) for every saved query, by name - the public form of _saved_files() for the Management API,
    which lists queries without their run history (load_versions()'s with_history)."""
    rows = db.connection().execute('SELECT name FROM saved_queries WHERE workspace = ? ORDER BY name',
                                   (scope.current(),)).fetchall()
    for row in rows:
        try:
            yield row['name'], load_versions(row['name'], with_history=with_history)
        except ApiError:
            continue


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
    ws = scope.current()
    with db.transaction() as conn:
        rows = conn.execute('SELECT name FROM saved_queries WHERE workspace = ? AND collection = ?',
                            (ws, old)).fetchall()
        for row in rows:
            conn.execute('UPDATE saved_queries SET collection = ? WHERE workspace = ? AND name = ?',
                         (new, ws, row['name']))
            moved.append(row['name'])
    return moved


def delete_saved(ref, version=None):
    """Delete a saved query, or a single version of it (the query goes with its last version) - cascades to
    that version's/query's own execution_history via the schema's own ON DELETE CASCADE."""
    name = resolve_saved_file(ref)
    ws = scope.current()
    with db.transaction() as conn:
        if version is None:
            conn.execute('DELETE FROM saved_queries WHERE workspace = ? AND name = ?', (ws, name))
            return
        row = conn.execute(
            'SELECT 1 FROM saved_query_versions WHERE workspace = ? AND query_name = ? AND version = ?',
            (ws, name, version)).fetchone()
        if row is None:
            raise ApiError(f'Version {version} not found', 404, code='version_not_found')
        conn.execute('DELETE FROM saved_query_versions WHERE workspace = ? AND query_name = ? AND version = ?',
                     (ws, name, version))
        remaining = conn.execute(
            'SELECT 1 FROM saved_query_versions WHERE workspace = ? AND query_name = ? LIMIT 1',
            (ws, name)).fetchone()
        if remaining is None:
            conn.execute('DELETE FROM saved_queries WHERE workspace = ? AND name = ?', (ws, name))
            return
        # Deleting the published version falls back to the newest version *older* than it - one that was live
        # before - never to a newer draft; with none older, the query is left unpublished.
        conn.execute('UPDATE saved_queries SET published_version = (SELECT MAX(version) FROM saved_query_versions '
                     'WHERE workspace = ? AND query_name = ? AND version < ?) '
                     'WHERE workspace = ? AND name = ? AND published_version = ?',
                     (ws, name, version, ws, name, version))


def record_execution(path, version, entry, live=True):
    """Record one run in a saved version's execution_history, and as an event - queued and written in the
    background, sampled and kept as configured: see history.py. ``live=False``: not live traffic (the examples'
    seeded runs), so kept whatever the sample rate and not an event. Never raises and never blocks the request it's
    logging for, the same trade-off record_audit() makes."""
    history.record(path, version, entry, live=live)


def refuse_legacy_home():
    """Refuse to start on a home that still holds data only in the pre-SQLite JSON files - a home last run by
    QueryAPIGate 0.10 or older (0.10 had moved connections into queryapigate.db, but not yet saved queries, keys,
    roles or the audit log). 0.14 was the last release to import them; starting without them would look like data
    loss. Each file counts only if it has entries its table lacks: files left behind after 0.11+ imported them, or
    empty ones, are ignored, as is every PostgreSQL store."""
    if db.is_postgres():
        return
    conn = db.connection()

    def empty(table):
        return conn.execute(f'SELECT 1 FROM {table} LIMIT 1').fetchone() is None

    def entries(path, key):
        try:
            with open(path) as f:
                return bool(json.load(f).get(key))
        except (OSError, ValueError, AttributeError):
            return False

    found = [path.name for path, key, table in (
        (config.connections_file(), 'connections', 'connections'), (config.api_keys_file(), 'keys', 'api_keys'),
        (config.roles_file(), 'roles', 'roles'), (config.audit_log_file(), 'entries', 'audit_log'))
        if empty(table) and entries(path, key)]
    saved_dir = config.saved_sql_dir()
    if empty('saved_queries') and saved_dir.is_dir() and any(n.endswith('.json') for n in os.listdir(str(saved_dir))):
        found.append('saved_sql/')
    if found:
        raise ValueError(f"{config.home()} holds data from QueryAPIGate 0.10 or older ({', '.join(found)}) that this "
                         'version no longer reads. Start QueryAPIGate 0.14 on this folder once - it brings it into '
                         'queryapigate.db - then this version.')


def latest_versions():
    """(name, version number, data, collection) for the newest version of every saved query, published or
    not - for admin tooling. Unreadable files are skipped."""
    return _versions('latest')


def live_versions():
    """(name, version number, data, collection) for the published version of every saved query that has one -
    what callers can actually run, so what the catalog, OpenAPI, MCP, Postman and bundle export describe."""
    return _versions('published')


def _versions(default):
    found = []
    for name, _, content in _saved_files():
        try:
            number, data = select_version(content, default=default)
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
                      'published_version': read_published(content), 'versions': versions})
    return files

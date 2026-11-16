"""CDC sources (ADR 0033 §11): each Debezium Server that tells live queries what changed, as a workspace object.

A source is a name, the connection its changes are about, optionally Debezium's `topic.prefix` (`server_name`: a post
from any other server is refused), and the secret its posts are signed with (Standard Webhooks, live.ingest()).

**The secret is generated here and shown once** - in the response that creates the source, or the one that rotates
its secret - never accepted from a caller, so no source has a weak one. It must be usable to check a signature, so it
can't be hashed as an API key's is: it is stored like a connection password, encrypted with QUERYAPIGATE_SECRET_KEY
when that's set (store._encrypt_password(), rekey.py). Rotating keeps the old one working for a grace period (ADR
0009), during which a post signed with either is accepted, so Debezium can be restarted with the new one at any point
in it.

**Posting to it:** `/ingest/cdc/<name>` in `default`, `/w/<workspace>/ingest/cdc/<name>` elsewhere - the prefix every
route takes (scope.WorkspacePrefix), since no API key says which workspace a post is for.

**QUERYAPIGATE_CDC_SOURCES**, the prototype's setting, still works: its sources are listed read-only (`managed_by`),
keep their `/ingest/cdc/<name>` whatever their workspace, and win over a stored source of the same name there - so a
stored source those would hide can't be made.
"""
import base64
import json
import re
import secrets
from datetime import datetime, timedelta

from . import config, db, scope, store
from .errors import ApiError

FIELDS = ('connection', 'server_name')
SETTING = 'QUERYAPIGATE_CDC_SOURCES'
DEFAULT_GRACE_MINUTES = 60
MAX_GRACE_MINUTES = 7 * 24 * 60  # how long an old secret may keep working after a rotation, as an API key's
RECEIVED_EVERY = 60  # seconds: last_received_at moves at most this often, so a busy source costs one write a minute
_NAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$')
_SERVER_NAME_MAX = 255
_TIME = '%Y-%m-%d %H:%M:%S'


def new_secret():
    """`whsec_` and 32 random bytes in base64: what Debezium's `authentication.webhook.secret` and every Standard
    Webhooks library take."""
    return 'whsec_' + base64.b64encode(secrets.token_bytes(32)).decode('ascii')


def ingest_path(name, workspace=None):
    """Where Debezium posts, relative to the server: what `debezium.sink.http.url` ends with."""
    workspace = workspace or scope.current()
    prefix = '' if workspace == db.DEFAULT_WORKSPACE else f'/w/{workspace}'
    return f'{prefix}/ingest/cdc/{name}'


def _not_found(name):
    return ApiError(f"No CDC source named '{name}'", 404, code='cdc_source_not_found')


def _check_name(name):
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise ApiError("A CDC source's name is 1-100 letters, digits, '.', '_' or '-', starting with a letter or digit",
                       code='invalid_name')


def validate(fields, partial=False):
    """The source's stored fields, normalised; raises naming the problem. The connection must be one in this
    workspace (active or not)."""
    if not isinstance(fields, dict):
        raise ApiError('The request body must be a JSON object', code='invalid_body')
    unknown = sorted(set(fields) - set(FIELDS))
    if 'secret' in unknown:
        raise ApiError("A CDC source's secret is generated, never given: it is in the response that creates the "
                       'source, and POST .../rotate makes a new one', code='unknown_field')
    if unknown:
        raise ApiError(f"Unknown field(s): {', '.join(unknown)}", code='unknown_field')
    out = {}
    connection = fields.get('connection')
    if connection is not None or not partial:
        if not isinstance(connection, str) or not connection:
            raise ApiError('connection is required: the connection whose changes this source reports',
                           code='invalid_body')
        found = db.connection().execute('SELECT 1 FROM connections WHERE workspace = ? AND name = ?',
                                        (scope.current(), connection)).fetchone()
        if found is None:
            raise ApiError(f"Connection '{connection}' not found", 404, code='connection_not_found')
        out['connection'] = connection
    if 'server_name' in fields:
        server_name = fields['server_name']
        if server_name is not None and (not isinstance(server_name, str) or not server_name.strip()
                                        or len(server_name) > _SERVER_NAME_MAX):
            raise ApiError(f"server_name is Debezium's topic.prefix: text, at most {_SERVER_NAME_MAX} characters - or "
                           'null to accept any', code='invalid_body')
        out['server_name'] = server_name.strip() if server_name else None
    return out


def _validate_grace(grace_minutes):
    if grace_minutes is None:
        return DEFAULT_GRACE_MINUTES
    if isinstance(grace_minutes, bool) or not isinstance(grace_minutes, int) \
            or not 0 <= grace_minutes <= MAX_GRACE_MINUTES:
        raise ApiError(f'grace_minutes is a whole number of minutes from 0 to {MAX_GRACE_MINUTES} (a week)',
                       code='invalid_body')
    return grace_minutes


# --------------------------------------------------------------------------------------
# The setting's sources (read-only)
# --------------------------------------------------------------------------------------

def _setting_sources(workspace=None):
    """{name: source} QUERYAPIGATE_CDC_SOURCES gives `workspace` (each source's own, `default` when it names none),
    or every workspace's when `workspace` is None."""
    found = {}
    for name, source in config.cdc_sources().items():
        if isinstance(source, dict) and (workspace is None
                                         or (source.get('workspace') or db.DEFAULT_WORKSPACE) == workspace):
            found[name] = source
    return found


def _from_setting(name, source):
    workspace = source.get('workspace') or db.DEFAULT_WORKSPACE
    return {'name': name, 'connection': source.get('connection'), 'server_name': source.get('server_name'),
            'ingest_path': f'/ingest/cdc/{name}', 'managed_by': SETTING, 'created_at': None, 'updated_at': None,
            'last_received_at': None, 'previous_expires_at': None, '_workspace': workspace}


def _refuse_hidden(name):
    """A stored source the setting's would hide: one named as the setting's in its own workspace, or in `default`
    (whose path, /ingest/cdc/<name>, the setting's sources keep whatever their workspace)."""
    hiding = config.cdc_sources().get(name)
    if not isinstance(hiding, dict):
        return
    workspace = scope.current()
    if workspace == db.DEFAULT_WORKSPACE or (hiding.get('workspace') or db.DEFAULT_WORKSPACE) == workspace:
        raise ApiError(f"{SETTING} already has a source named '{name}' - pick another name, or remove it from the "
                       'setting first', 409, code='cdc_source_exists')


# --------------------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------------------

_COLUMNS = 'name, created_at, updated_at, last_received_at, details_json'


def _previous_live(details):
    until = details.get('previous_expires_at')
    return bool(details.get('previous_secret') and until and until > store.now())


def _row(row, workspace):
    details = json.loads(row['details_json'])
    return {'name': row['name'], 'connection': details.get('connection'), 'server_name': details.get('server_name'),
            'ingest_path': ingest_path(row['name'], workspace), 'managed_by': None, 'created_at': row['created_at'],
            'updated_at': row['updated_at'], 'last_received_at': row['last_received_at'],
            'previous_expires_at': details['previous_expires_at'] if _previous_live(details) else None}


def _load(conn, name, workspace=None):
    """(the stored row's details, the row) - or (None, None)."""
    workspace = workspace or scope.current()
    row = conn.execute(f'SELECT {_COLUMNS} FROM cdc_sources WHERE workspace = ? AND name = ?',
                       (workspace, name)).fetchone()
    return (json.loads(row['details_json']), row) if row is not None else (None, None)


def list_all():
    """This workspace's sources, stored and from the setting, by name. Never a secret."""
    workspace = scope.current()
    rows = db.connection().execute(f'SELECT {_COLUMNS} FROM cdc_sources WHERE workspace = ? ORDER BY name',
                                   (workspace,)).fetchall()
    found = {row['name']: _row(row, workspace) for row in rows}
    for name, source in _setting_sources(workspace).items():
        found[name] = _from_setting(name, source)  # the setting's wins, as it does at ingest
    return [{k: v for k, v in found[name].items() if not k.startswith('_')} for name in sorted(found)]


def get(name):
    setting = _setting_sources(scope.current()).get(name)
    if setting is not None:
        return {k: v for k, v in _from_setting(name, setting).items() if not k.startswith('_')}
    _details, row = _load(db.connection(), name)
    if row is None:
        raise _not_found(name)
    return _row(row, scope.current())


def _refuse_managed(name):
    if name in _setting_sources(scope.current()):
        raise ApiError(f"CDC source '{name}' comes from {SETTING} - change it there", 409,
                       code='cdc_source_managed')


def _write(conn, name, details, created_at, timestamp, last_received_at=None):
    conn.execute("""
        INSERT INTO cdc_sources (workspace, name, created_at, updated_at, last_received_at, details_json)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(workspace, name) DO UPDATE SET updated_at = excluded.updated_at,
          details_json = excluded.details_json
    """, (scope.current(), name, created_at, timestamp, last_received_at, json.dumps(details)))


def create(name, fields):
    """Save a new source with a new secret. Returns (the source, the secret) - the only time the secret is shown."""
    _check_name(name)
    fields = validate(fields)
    _refuse_hidden(name)
    secret = new_secret()
    with db.transaction() as conn:
        if _load(conn, name)[1] is not None:
            raise ApiError(f"A CDC source named '{name}' already exists", 409, code='cdc_source_exists')
        timestamp = store.now()
        _write(conn, name, {**fields, 'secret': store._encrypt_password(secret)}, timestamp, timestamp)
    return get(name), secret


def update(name, changes):
    """Change `connection` or `server_name` (null: any server). The secret isn't a field - rotate() it."""
    _refuse_managed(name)
    fields = validate(changes, partial=True)
    with db.transaction() as conn:
        details, row = _load(conn, name)
        if row is None:
            raise _not_found(name)
        details.update(fields)
        if details.get('server_name') is None:
            details.pop('server_name', None)
        _write(conn, name, details, row['created_at'], store.now())
    return get(name)


def rotate(name, grace_minutes=None):
    """A new secret, returned once; the old one keeps working `grace_minutes` (0: it stops at once - for a leaked
    one). Refused while the secret before it is still in its grace period, so a second rotation can't cut off the
    one Debezium may still be using. Returns (the source, the secret)."""
    _refuse_managed(name)
    grace = _validate_grace(grace_minutes)
    secret = new_secret()
    with db.transaction() as conn:
        details, row = _load(conn, name)
        if row is None:
            raise _not_found(name)
        if _previous_live(details):
            raise ApiError(f"The secret this source had before its last rotation works until "
                           f"{details['previous_expires_at']} - end that first (DELETE .../previous), or wait for it",
                           409, code='cdc_source_rotation_pending', previous_expires_at=details['previous_expires_at'])
        details.pop('previous_secret', None)
        details.pop('previous_expires_at', None)
        if grace:
            details['previous_secret'] = details['secret']
            details['previous_expires_at'] = (datetime.now() + timedelta(minutes=grace)).strftime(_TIME)
        details['secret'] = store._encrypt_password(secret)
        _write(conn, name, details, row['created_at'], store.now())
    return get(name), secret


def end_previous(name):
    """Stop the old secret now, before its grace period ends. Returns when it would have stopped (None: there was
    none still working)."""
    _refuse_managed(name)
    with db.transaction() as conn:
        details, row = _load(conn, name)
        if row is None:
            raise _not_found(name)
        until = details['previous_expires_at'] if _previous_live(details) else None
        details.pop('previous_secret', None)
        details.pop('previous_expires_at', None)
        _write(conn, name, details, row['created_at'], store.now())
    return until


def delete(name):
    _refuse_managed(name)
    with db.transaction() as conn:
        if _load(conn, name)[1] is None:
            raise _not_found(name)
        conn.execute('DELETE FROM cdc_sources WHERE workspace = ? AND name = ?', (scope.current(), name))


# --------------------------------------------------------------------------------------
# Ingest
# --------------------------------------------------------------------------------------

def find(name, workspace=None):
    """What a post to `name` is checked against: {'secrets', 'connection', 'server_name', 'workspace', 'stored',
    'last_received_at'} - or None. `workspace` is the one the path named (None: unprefixed, `default`). The setting's
    sources answer unprefixed whatever their workspace, and in their own workspace, before a stored one."""
    setting = config.cdc_sources().get(name)
    if isinstance(setting, dict):
        own = setting.get('workspace') or db.DEFAULT_WORKSPACE
        if workspace is None or workspace == own:
            return {'secrets': [setting.get('secret')], 'connection': setting.get('connection'),
                    'server_name': setting.get('server_name'), 'workspace': own, 'stored': False,
                    'last_received_at': None}
    workspace = workspace or db.DEFAULT_WORKSPACE
    details, row = _load(db.connection(), name, workspace)
    if row is None:
        return None
    found = [store._decrypt_password(details['secret'])]
    if _previous_live(details):
        found.append(store._decrypt_password(details['previous_secret']))
    return {'secrets': found, 'connection': details.get('connection'), 'server_name': details.get('server_name'),
            'workspace': workspace, 'stored': True, 'last_received_at': row['last_received_at']}


def mark_received(source, name):
    """A verified post arrived: move last_received_at, if it's more than RECEIVED_EVERY old - so the Console can tell
    a connected source from a silent one at the cost of at most one write a minute. Never fails the post."""
    if not source['stored']:
        return
    now = store.now()
    last = source['last_received_at']
    if last and (datetime.strptime(now, _TIME) - datetime.strptime(last, _TIME)).total_seconds() < RECEIVED_EVERY:
        return
    try:
        with db.transaction() as conn:
            conn.execute('UPDATE cdc_sources SET last_received_at = ? WHERE workspace = ? AND name = ?',
                         (now, source['workspace'], name))
    except (OSError, *db.Error):
        pass  # only a status: the signal itself is what must be kept

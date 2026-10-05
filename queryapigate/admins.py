"""Named administrators and their personal admin tokens (ADR 0003, BACKLOG #84 Phase 1).

An administrator is a person or a pipeline (`alice`, `ci-deploy`) with one role (adminroles.py). It signs in with
a personal admin token - `qagadm_...`, sent as `X-API-Key` like any key - of which it may hold several (a laptop, a CI
job), each revocable alone. Only a token's SHA-256 hash is stored; the secret is shown once, when issued.

Deactivating an administrator stops all of its tokens at once. An expired token (day granularity, like an API key's
`expires_at`) stops by itself. Both tables live in the store, so every instance sees a change on its next request.

The shared QUERYAPIGATE_API_KEY stays an owner - the break-glass key. While no active owner exists it is the only
way in, so this module refuses a change that would leave no active owner when the shared key isn't set.
"""
import hashlib
import hmac
import json
import logging
import re
import secrets
import time

from . import apikeys, config, db, store
from .adminroles import DATA_ROLES, ROLE_NAMES
from .errors import ApiError

TOKEN_PREFIX = 'qagadm_'
RESERVED_NAMES = {'admin', 'cli', '-'}  # the shared key's and the CLI's names in the audit log and run history
_EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+$')
_UNSET = object()
_USE_RECORD_INTERVAL = 60.0  # seconds between last_used_at/last_seen_at writes per token, as for API keys
_last_recorded_use: dict[str, float] = {}
BREAK_GLASS_AUDIT_INTERVAL = 600.0  # seconds between this process's audit entries for the shared key's use
_OWNER_CHECK_INTERVAL = 30.0  # how long this process trusts its "an active owner exists" answer
_break_glass = {'audited': None, 'owner_checked': None, 'owner': False}
log = logging.getLogger('queryapigate')


def _hash(secret):
    return hashlib.sha256(secret.encode('utf-8')).hexdigest()


# --------------------------------------------------------------------------------------
# Administrators
# --------------------------------------------------------------------------------------

def _row_to_admin(row):
    details = json.loads(row['details_json'])
    return {'name': row['name'], 'role': row['role'], 'active': bool(row['active']), 'created_at': row['created_at'],
            'email': details.get('email'), 'created_by': details.get('created_by'),
            'last_seen_at': details.get('last_seen_at')}


def _write_admin(conn, admin):
    details = {k: admin.get(k) for k in ('email', 'created_by', 'last_seen_at')}
    conn.execute("""
        INSERT INTO administrators (name, role, active, created_at, details_json) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET role = excluded.role, active = excluded.active,
            details_json = excluded.details_json
    """, (admin['name'], admin['role'], int(admin['active']), admin['created_at'], json.dumps(details)))


def _get(conn, name):
    row = conn.execute('SELECT name, role, active, created_at, details_json FROM administrators WHERE name = ?',
                       (name,)).fetchone()
    return _row_to_admin(row) if row is not None else None


def any_configured():
    return db.connection().execute('SELECT 1 FROM administrators LIMIT 1').fetchone() is not None


def active_owner_exists():
    return db.connection().execute("SELECT 1 FROM administrators WHERE role = 'owner' AND active = 1 "
                                   'LIMIT 1').fetchone() is not None


def list_admins():
    rows = db.connection().execute(
        'SELECT name, role, active, created_at, details_json FROM administrators ORDER BY name').fetchall()
    return [_row_to_admin(row) for row in rows]


def get_admin(name):
    admin = _get(db.connection(), name)
    if admin is None:
        raise ApiError(f"No administrator named '{name}'", 404, code='admin_not_found')
    return admin


def _validate_role(role):
    if role not in ROLE_NAMES:
        raise ApiError(f"role must be one of {', '.join(ROLE_NAMES)}", code='invalid_body')
    return role


def _validate_email(email):
    if email is None:
        return None
    if not isinstance(email, str) or not _EMAIL_RE.match(email) or len(email) > 254:
        raise ApiError('email must be an email address, or null', code='invalid_body')
    return email


def refuse_taken_name(name):
    """An API key may not take an administrator's name (and the reverse, in create_admin()): a name in the logs,
    /metrics and run history then always means one caller."""
    if db.connection().execute('SELECT 1 FROM administrators WHERE name = ?', (name,)).fetchone() is not None:
        raise ApiError(f"'{name}' is an administrator's name - choose another", 409, code='name_taken')


def create_admin(name, role, email=None, created_by=None):
    if not isinstance(name, str) or not apikeys._NAME_RE.match(name):
        raise ApiError("An administrator's name may only contain letters, digits, spaces, '.', '_' and '-'",
                       code='invalid_name')
    if name in RESERVED_NAMES:
        raise ApiError(f"'{name}' is reserved - choose another name", code='invalid_name')
    admin = {'name': name, 'role': _validate_role(role), 'active': True, 'created_at': store.now(),
             'email': _validate_email(email), 'created_by': created_by, 'last_seen_at': None}
    with db.transaction() as conn:
        if _get(conn, name) is not None:
            raise ApiError(f"An administrator named '{name}' already exists", 409, code='admin_exists')
        if conn.execute('SELECT 1 FROM api_keys WHERE name = ?', (name,)).fetchone() is not None:
            raise ApiError(f"'{name}' is an API key's name - choose another", 409, code='name_taken')
        _write_admin(conn, admin)
    return admin


def _refuse_ownerless(conn):
    """Called inside a change's transaction, after it: refuse when no active owner is left and there is no
    shared key to fall back on - nobody could then manage administrators."""
    if config.api_key() is not None:
        return
    if conn.execute('SELECT 1 FROM administrators LIMIT 1').fetchone() is None:
        return  # no administrators at all: authentication is back to how it was before any existed
    if conn.execute("SELECT 1 FROM administrators WHERE role = 'owner' AND active = 1 LIMIT 1").fetchone() is None:
        raise ApiError('This would leave no active owner, and QUERYAPIGATE_API_KEY is not set - nobody could manage '
                       'administrators any more. Make someone else an owner first.', 409, code='last_owner')


def update_admin(name, role=None, email=_UNSET, active=None):
    with db.transaction() as conn:
        admin = _get(conn, name)
        if admin is None:
            raise ApiError(f"No administrator named '{name}'", 404, code='admin_not_found')
        if role is not None:
            admin['role'] = _validate_role(role)
        if email is not _UNSET:
            admin['email'] = _validate_email(email)
        if active is not None:
            if not isinstance(active, bool):
                raise ApiError('active must be true or false', code='invalid_body')
            admin['active'] = active
        _write_admin(conn, admin)
        _refuse_ownerless(conn)
    return admin


def delete_admin(name):
    """Remove an administrator and every token it holds. Its name stays in the audit log as written."""
    with db.transaction() as conn:
        if _get(conn, name) is None:
            raise ApiError(f"No administrator named '{name}'", 404, code='admin_not_found')
        conn.execute('DELETE FROM admin_tokens WHERE admin_name = ?', (name,))
        conn.execute('DELETE FROM administrators WHERE name = ?', (name,))
        _refuse_ownerless(conn)


# --------------------------------------------------------------------------------------
# Tokens
# --------------------------------------------------------------------------------------

def _row_to_token(row):
    details = json.loads(row['details_json'])
    return {'id': row['id'], 'admin': row['admin_name'], 'label': details.get('label'),
            'created_at': row['created_at'], 'expires_at': row['expires_at'],
            'last_used_at': details.get('last_used_at'),
            'expired': apikeys.is_expired({'expires_at': row['expires_at']})}


_TOKEN_COLUMNS = 'id, admin_name, hash, expires_at, created_at, details_json'


def list_tokens(admin_name):
    get_admin(admin_name)
    rows = db.connection().execute(f'SELECT {_TOKEN_COLUMNS} FROM admin_tokens WHERE admin_name = ? '
                                   'ORDER BY created_at, id', (admin_name,)).fetchall()
    return [_row_to_token(row) for row in rows]


def issue_token(admin_name, label=None, expires_at=None):
    """A new token for `admin_name`: (its listing, the secret). The secret is never stored or shown again."""
    if label is not None and (not isinstance(label, str) or len(label) > 100):
        raise ApiError('label must be text of at most 100 characters', code='invalid_body')
    expires_at = apikeys._validate_expiry(expires_at)
    secret = TOKEN_PREFIX + secrets.token_urlsafe(32)
    token_id = 'tok_' + secrets.token_hex(6)
    with db.transaction() as conn:
        if _get(conn, admin_name) is None:
            raise ApiError(f"No administrator named '{admin_name}'", 404, code='admin_not_found')
        conn.execute(f'INSERT INTO admin_tokens ({_TOKEN_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?)',
                     (token_id, admin_name, _hash(secret), expires_at, store.now(),
                      json.dumps({'label': label, 'last_used_at': None})))
        row = conn.execute(f'SELECT {_TOKEN_COLUMNS} FROM admin_tokens WHERE id = ?', (token_id,)).fetchone()
    return _row_to_token(row), secret


def revoke_token(admin_name, token_id):
    with db.transaction() as conn:
        deleted = conn.execute('DELETE FROM admin_tokens WHERE id = ? AND admin_name = ?',
                               (token_id, admin_name)).rowcount
    if not deleted:
        raise ApiError(f"Administrator '{admin_name}' has no token '{token_id}'", 404, code='token_not_found')


# --------------------------------------------------------------------------------------
# Authentication
# --------------------------------------------------------------------------------------

def permission_for(admin, via):
    """The Permission an administrator acts with: its role for the Management API, and - for every role but
    auditor - the same reach as the shared key for running SQL."""
    data = admin['role'] in DATA_ROLES
    return apikeys.Permission(
        name=admin['name'], admin=data, connections=apikeys.ALL_CONNECTIONS if data else frozenset(),
        allow_writes=data, queries=apikeys.ALL_QUERIES if data else {}, rate_limit=None, allowed_write_ops=None,
        allowed_tables=None, role=admin['role'], via=via)


def authenticate(supplied):
    """The Permission for an admin token, or None - for a wrong, revoked or expired token, or one whose
    administrator is inactive, all alike."""
    supplied_hash = _hash(supplied)
    conn = db.connection()
    row = conn.execute(f'SELECT {_TOKEN_COLUMNS} FROM admin_tokens WHERE hash = ?', (supplied_hash,)).fetchone()
    if row is None or not hmac.compare_digest(supplied_hash, row['hash']) \
            or apikeys.is_expired({'expires_at': row['expires_at']}):
        return None
    admin = _get(conn, row['admin_name'])
    if admin is None or not admin['active']:
        return None
    _record_use(row['id'], admin['name'])
    return permission_for(admin, 'token')


def _record_use(token_id, admin_name):
    """last_used_at on the token and last_seen_at on its administrator, at most once a minute per token."""
    now = time.monotonic()
    last = _last_recorded_use.get(token_id)
    if last is not None and now - last < _USE_RECORD_INTERVAL:
        return
    _last_recorded_use[token_id] = now
    stamp = store.now()
    try:
        with db.transaction() as conn:
            row = conn.execute('SELECT details_json FROM admin_tokens WHERE id = ?', (token_id,)).fetchone()
            if row is not None:
                conn.execute('UPDATE admin_tokens SET details_json = ? WHERE id = ?',
                             (json.dumps({**json.loads(row['details_json']), 'last_used_at': stamp}), token_id))
            admin = _get(conn, admin_name)
            if admin is not None:
                _write_admin(conn, {**admin, 'last_seen_at': stamp})
    except db.Error:
        pass  # visibility only - never fail the request over it


# --------------------------------------------------------------------------------------
# The break-glass key
# --------------------------------------------------------------------------------------

def _owner_exists_cached():
    now = time.monotonic()
    checked = _break_glass['owner_checked']
    if checked is None or now - checked >= _OWNER_CHECK_INTERVAL:
        try:
            _break_glass['owner'] = active_owner_exists()
        except db.Error:
            return _break_glass['owner']
        _break_glass['owner_checked'] = now
    return _break_glass['owner']


def note_break_glass():
    """The shared key was used. Once an active owner exists it is the break-glass key: every use is logged, and an
    audit entry (`break_glass_used`, at most one per BREAK_GLASS_AUDIT_INTERVAL per process) raises the alert - from
    the store, so every instance's Alerts screen shows it."""
    if not _owner_exists_cached():
        return
    where = None
    try:
        from flask import has_request_context, request
        if has_request_context():
            where = f'{request.method} {request.path}'
    except ImportError:  # pragma: no cover
        pass
    log.warning('The shared QUERYAPIGATE_API_KEY (break-glass key) was used%s although named administrators exist',
                f' for {where}' if where else '')
    now = time.monotonic()
    audited = _break_glass['audited']
    if audited is None or now - audited >= BREAK_GLASS_AUDIT_INTERVAL:
        _break_glass['audited'] = now
        store.record_audit('admin', 'break_glass_used', where or '-', None, via='break-glass')


def last_break_glass_use(since):
    """The newest `break_glass_used` audit entry at or after `since` (a store timestamp), or None."""
    row = db.connection().execute(
        "SELECT entry_json FROM audit_log WHERE timestamp >= ? AND entry_json LIKE '%\"break_glass_used\"%' "
        'ORDER BY id DESC LIMIT 1', (since,)).fetchone()
    return json.loads(row['entry_json']) if row is not None else None

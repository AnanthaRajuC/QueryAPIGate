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

from . import apikeys, config, db, store, workspaces
from .adminroles import DATA_ROLES, ROLE_NAMES
from .db import DEFAULT_WORKSPACE
from .errors import ApiError

TOKEN_PREFIX = 'qagadm_'
RESERVED_NAMES = {'admin', 'cli', 'startup', '-'}  # the shared key's, the CLI's and start-up's names in the audit log
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
            'last_seen_at': details.get('last_seen_at'), 'sso': details.get('sso')}


def _write_admin(conn, admin):
    details = {k: admin.get(k) for k in ('email', 'created_by', 'last_seen_at', 'sso')}
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
    """A server-wide role, or None: no role here - only the ones their workspace memberships give them."""
    if role is not None and role not in ROLE_NAMES:
        raise ApiError(f"role must be one of {', '.join(ROLE_NAMES)}, or null (only roles in workspaces)",
                       code='invalid_body')
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
    shared key to fall back on - nobody could then manage administrators. That includes removing the last
    administrator of all: with no shared key and no scoped keys, the server would then be open to anyone."""
    if config.api_key() is not None:
        return
    if conn.execute("SELECT 1 FROM administrators WHERE role = 'owner' AND active = 1 LIMIT 1").fetchone() is None:
        raise ApiError('This would leave no active owner, and QUERYAPIGATE_API_KEY is not set - nobody could manage '
                       'administrators any more. Make someone else an owner first.', 409, code='last_owner')


def update_admin(name, role=_UNSET, email=_UNSET, active=None):
    with db.transaction() as conn:
        admin = _get(conn, name)
        if admin is None:
            raise ApiError(f"No administrator named '{name}'", 404, code='admin_not_found')
        if role is not _UNSET and _validate_role(role) != admin['role']:
            if linked(admin):
                raise ApiError(f"{name} signs in through SSO: their role comes from their groups at the identity "
                               'provider - change it there', 409, code='sso_managed')
            admin['role'] = role
        if email is not _UNSET:
            admin['email'] = _validate_email(email)
        if active is not None:
            if not isinstance(active, bool):
                raise ApiError('active must be true or false', code='invalid_body')
            admin['active'] = active
            if not active:
                _end_sessions(conn, name)  # switched off: out of the Console now, not when a session expires
        _write_admin(conn, admin)
        _refuse_ownerless(conn)
    return admin


def delete_admin(name):
    """Remove an administrator, every token it holds and its workspace memberships. Its name stays in the audit log
    as written."""
    with db.transaction() as conn:
        if _get(conn, name) is None:
            raise ApiError(f"No administrator named '{name}'", 404, code='admin_not_found')
        conn.execute('DELETE FROM admin_tokens WHERE admin_name = ?', (name,))
        conn.execute('DELETE FROM workspace_members WHERE admin_name = ?', (name,))  # their roles in workspaces too
        _end_sessions(conn, name)
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
        admin = _get(conn, admin_name)
        if admin is None:
            raise ApiError(f"No administrator named '{admin_name}'", 404, code='admin_not_found')
        if config.sso_only() and linked(admin):
            raise ApiError(f'{admin_name} signs in through SSO, and QUERYAPIGATE_SSO_ONLY is on: no personal tokens',
                           409, code='sso_only')
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

def permission_for(admin, via, workspace=DEFAULT_WORKSPACE):
    """The Permission an administrator acts with in `workspace`: their server-wide role, or else the role a
    membership gives them there (ADR 0023 §3) - or none, when they have neither. For every role but auditor, the
    same reach as the shared key for running SQL: every connection *of that workspace*."""
    memberships = workspaces.memberships(admin['name'])
    role = admin['role'] or memberships.get(workspace)
    data = role in DATA_ROLES
    return apikeys.Permission(
        name=admin['name'], admin=data, connections=apikeys.ALL_CONNECTIONS if data else frozenset(),
        allow_writes=data, queries=apikeys.ALL_QUERIES if data else {}, rate_limit=None, allowed_write_ops=None,
        allowed_tables=None, role=role, via=via, workspace=workspace, memberships=memberships,
        server_wide=admin['role'] is not None)


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
    if admin is None or not admin['active'] or (config.sso_only() and linked(admin)):
        return None
    _record_use(row['id'], admin['name'])
    from . import scope
    return permission_for(admin, 'token', scope.current())  # the workspace the request names (app.gate())


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
    # The action field itself: a role or query merely *named* break_glass_used must not raise the alert.
    row = db.connection().execute(
        'SELECT entry_json FROM audit_log WHERE timestamp >= ? AND entry_json LIKE ? ORDER BY id DESC LIMIT 1',
        (since, '%"action": "break_glass_used"%')).fetchone()
    return json.loads(row['entry_json']) if row is not None else None


# --------------------------------------------------------------------------------------
# Signed in through SSO (sso.py, ADR 0007)
# --------------------------------------------------------------------------------------

def linked(admin):
    """Whether this administrator signs in through SSO - bound to a person at the provider."""
    return bool((admin.get('sso') or {}).get('subject'))


def sso_status(admin):
    """How the Management API shows an administrator's SSO: None, pending (bound at their next SSO sign-in) or
    linked, with the provider's issuer - never the subject."""
    link = admin.get('sso') or {}
    if link.get('subject'):
        return {'status': 'linked', 'issuer': link.get('issuer')}
    return {'status': 'pending'} if link.get('pending') else None


def set_sso_pending(name, pending):
    """Mark an existing administrator to be bound at their next SSO sign-in - by a verified email matching theirs -
    or undo that. A linked one is unlinked by False: their next SSO sign-in makes a new administrator."""
    with db.transaction() as conn:
        admin = _get(conn, name)
        if admin is None:
            raise ApiError(f"No administrator named '{name}'", 404, code='admin_not_found')
        if pending:
            if linked(admin):
                return admin
            if not admin['email']:
                raise ApiError(f'{name} needs an email first: their SSO sign-in is matched to it, once', 409,
                               code='email_required')
            admin['sso'] = {'pending': True}
        else:
            admin['sso'] = None
            _end_sessions(conn, name)
        _write_admin(conn, admin)
    return admin


def _end_sessions(conn, name):
    conn.execute('DELETE FROM admin_sessions WHERE admin_name = ?', (name,))


def find_by_sso(issuer, subject):
    """The administrator bound to this provider's person, or None. The binding is (issuer, subject) - never an
    email, which a provider may reassign or not have verified."""
    for row in db.connection().execute('SELECT name, role, active, created_at, details_json FROM administrators'):
        admin = _row_to_admin(row)
        if admin['sso'] == {'issuer': issuer, 'subject': subject}:
            return admin
    return None


def _free_name(conn, wanted):
    base = re.sub(r'[^\w.\-]+', '-', wanted).strip('-.')[:90] or 'admin'
    if base in RESERVED_NAMES:
        base += '-sso'
    for n in range(1, 1000):
        name = base if n == 1 else f'{base}-{n}'
        taken = _get(conn, name) is not None or conn.execute('SELECT 1 FROM api_keys WHERE name = ?',
                                                              (name,)).fetchone() is not None
        if not taken:
            return name
    raise ApiError('Could not find a free administrator name', 409, code='admin_exists')


def sign_in_sso(issuer, subject, role, wanted_name, email):
    """The administrator for someone who just signed in through SSO, created on first sign-in - (admin, change)
    where change is 'created', 'linked' (an administrator waiting for this verified email, now bound - 'linked_role'
    when their groups also gave them a new role), 'role' (their groups gave a new role) or None. The provider decides
    the role: one moved out of the owners' group is no longer an owner, last one or not - owners come from that
    group."""
    with db.transaction() as conn:
        found = find_by_sso(issuer, subject)
        if found is None and email:
            waiting = [a for a in list_admins() if (a.get('sso') or {}).get('pending')
                       and (a['email'] or '').lower() == email.lower()]
            if len(waiting) > 1:
                raise ApiError(f'More than one administrator waits for an SSO sign-in as {email}', 409,
                               code='sso_ambiguous')
            if waiting:
                found = {**waiting[0], 'sso': {'issuer': issuer, 'subject': subject}}
                change = 'linked' if found['role'] == role else 'linked_role'
                found['role'] = role
                _write_admin(conn, found)
                return found, change
        if found is not None:
            if found['role'] == role:
                return found, None
            found = {**found, 'role': role}
            _write_admin(conn, found)
            return found, 'role'
        admin = {'name': _free_name(conn, wanted_name), 'role': role, 'active': True, 'created_at': store.now(),
                 'email': email if email and _EMAIL_RE.match(email) else None, 'created_by': 'sso',
                 'last_seen_at': None, 'sso': {'issuer': issuer, 'subject': subject}}
        _write_admin(conn, admin)
        return admin, 'created'


def note_seen(name):
    """last_seen_at for an administrator acting through a session - the caller throttles it. Never raises."""
    try:
        with db.transaction() as conn:
            admin = _get(conn, name)
            if admin is not None:
                _write_admin(conn, {**admin, 'last_seen_at': store.now()})
    except db.Error:
        log.warning('Could not record when %s was last seen', name, exc_info=True)

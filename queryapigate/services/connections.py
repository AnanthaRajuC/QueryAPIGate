"""Connections as the Management API presents them (/api/v1/connections, BACKLOG #72).

A connection is stored as its database type, active flag and a details object (host, port, user, password,
database, plus any driver option the runner passes through - sslmode, jdbc_url, jar, ...). Passwords never leave
the server: responses mask a literal or encrypted one, and echoing the mask back keeps the stored value. Every
change is audited exactly as the legacy routes audit theirs (the password only ever as "changed").
"""
import hashlib
import json
import re

from .. import config, engine, metrics, pool, schema, store
from ..errors import ApiError

# Fields the server owns or computes; never stored from a request.
_SERVER_FIELDS = ('name', 'created_at', 'updated_at', 'usage', 'example')
_NAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9 ._-]{0,99}$')


def audit_changes(before, after):
    """A connection's changed fields for the audit log: the password only ever as 'changed', and the store's own
    updated_at left out (it differs whenever two saves fall in different seconds)."""
    keys = set(before or {}) | set(after or {})
    diff = {k: {'from': (before or {}).get(k), 'to': (after or {}).get(k)} for k in sorted(keys)
            if (before or {}).get(k) != (after or {}).get(k)}
    diff.pop('updated_at', None)
    if 'password' in diff:
        diff['password'] = 'changed'
    return diff


def _masked(name, details):
    return store.mask_passwords({name: details})[name]


def to_item(name, details):
    """The list shape: what identifies a connection and its live usage - never credentials or driver options."""
    usage = metrics.summary_for_connection(name)
    return {
        'name': name, 'db': details.get('db'), 'active': bool(details.get('active', True)),
        'host': details.get('host') or None, 'port': details.get('port') or None,
        'database': details.get('database') if isinstance(details.get('database'), str) else None,
        'user': details.get('user') or None, 'example': details.get('example') is True,
        'created_at': details.get('created_at'), 'updated_at': details.get('updated_at'),
        'usage': {'queries': usage.get('queries', 0), 'errors': usage.get('errors', 0), 'rows': usage.get('rows', 0),
                  'avg_duration_ms': usage.get('avg_duration_ms')},
    }


def to_detail(name, details):
    """One connection with every field, the password masked; driver options under `options`."""
    masked = _masked(name, details)
    known = ('db', 'active', 'host', 'port', 'user', 'password', 'database', 'created_at', 'updated_at', 'example')
    return {**to_item(name, details), 'password': masked.get('password'),
            'options': {k: v for k, v in masked.items() if k not in known}}


def etag(details):
    state = json.dumps({k: v for k, v in details.items()}, sort_keys=True, default=str)
    return '"' + hashlib.sha256(state.encode()).hexdigest()[:32] + '"'


def load(name):
    details = store.read_connections().get(name) if isinstance(name, str) else None
    if details is None:
        raise ApiError(f"Connection '{name}' not found", 404, code='connection_not_found')
    return details


def list_items():
    return [to_item(name, details) for name, details in sorted(store.read_connections().items())]


def _fields(data):
    """A request body's connection fields: everything but the server-owned ones, with `options` flattened in."""
    if not isinstance(data, dict):
        raise ApiError('The request body must be a JSON object', code='invalid_body')
    options = data.get('options') or {}
    if not isinstance(options, dict):
        raise ApiError('options must be a JSON object', code='invalid_body')
    fields = {k: v for k, v in data.items() if k not in (*_SERVER_FIELDS, 'options')}
    clash = sorted(set(options) & set(fields))
    if clash:
        raise ApiError(f"Set {', '.join(clash)} directly, not inside options", code='invalid_body')
    if 'active' in fields and not isinstance(fields['active'], bool):
        raise ApiError('active must be true or false', code='invalid_body')
    if 'db' in fields and fields['db'] not in config.SUPPORTED_DB_TYPES:
        raise ApiError(f"db must be one of: {', '.join(config.SUPPORTED_DB_TYPES)}", code='invalid_body')
    return {**options, **fields}


def create(data, actor):
    name = data.get('name') if isinstance(data, dict) else None
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise ApiError("A connection name is 1-100 letters, digits, spaces, '.', '_' or '-', starting with a letter or "
                       'digit', code='invalid_name')
    if name in store.read_connections():
        raise ApiError(f"A connection named '{name}' already exists", 409, code='connection_exists')
    fields = _fields(data)
    if 'db' not in fields:
        raise ApiError(f"db is required - one of: {', '.join(config.SUPPORTED_DB_TYPES)}", code='invalid_body')
    fields.setdefault('active', True)
    store.update_connections({name: fields})
    after = store.read_connections()[name]
    store.record_audit(actor, 'create_connection', name, _masked(name, after))
    pool.close_pooled_connections()  # a name used before (deleted, then re-created) must not reuse its old sockets
    return name


def update(name, data, actor):
    """Merge the given fields into the stored connection - anything not mentioned (driver options included) is
    kept; a field set to null is removed; the password mask keeps the stored password."""
    before = load(name)
    fields = _fields(data)
    merged = {k: v for k, v in before.items() if k not in ('created_at', 'updated_at')}
    for key, value in fields.items():
        if value is None and key not in ('db', 'active'):
            merged.pop(key, None)
        else:
            merged[key] = value
    if 'password' not in fields and 'password' in before:
        merged['password'] = config.PASSWORD_MASK  # keep the stored (possibly encrypted) value as it is
    store.update_connections({name: merged})
    after = store.read_connections()[name]
    changes = audit_changes(before, after)
    if changes:
        store.record_audit(actor, 'update_connection', name, changes)
    pool.close_pooled_connections()  # new settings or credentials must not be served by old connections


def delete(name, reason, actor):
    before = load(name)
    if not isinstance(reason, str) or not reason.strip():
        raise ApiError('A reason is required to delete a connection', code='reason_required')
    store.delete_connection(name)
    store.record_audit(actor, 'delete_connection', name, {**_masked(name, before), 'deleted_reason': reason.strip()})
    pool.close_pooled_connections()  # a removed connection must not keep serving from idle sockets


def _probe_details(data):
    """Connection fields to try: given directly (a form not saved yet), or a saved connection's own when only its
    name is given - with a masked password resolved to the stored one."""
    if not isinstance(data, dict):
        raise ApiError('The request body must be a JSON object', code='invalid_body')
    name = data.get('name')
    if name and not data.get('db'):
        return None, name
    fields = _fields(data)
    if fields.get('db') not in config.SUPPORTED_DB_TYPES:
        raise ApiError(f"db must be one of: {', '.join(config.SUPPORTED_DB_TYPES)}", code='invalid_body')
    if fields.get('password') == config.PASSWORD_MASK:
        fields['password'] = store.read_connections().get(name or '', {}).get('password', '')
    return store.resolve_ad_hoc(fields), None


def _unreachable(error):
    """A probe that could not reach the database (502): the same error, with a stable code for the Management API."""
    if error.status == 502:
        return ApiError(error.message, 502, code='connection_failed', **error.extra)
    return error


def test(data):
    details, name = _probe_details(data)
    if details is None:
        details = store.resolve_ad_hoc(load(name))
    try:
        return engine.test_connection(details)
    except ApiError as error:
        raise _unreachable(error) from error


def databases(data):
    details, name = _probe_details(data)
    try:
        return schema.list_databases(name) if details is None else engine.list_databases(details)
    except ApiError as error:
        raise _unreachable(error) from error


def deleted():
    """Every deleted connection, newest first - from the audit log, the only record of one (there is no separate
    deleted-connections store)."""
    out = []
    for entry in reversed(store.read_audit_log()):
        if entry.get('action') != 'delete_connection':
            continue
        c = entry.get('changes') or {}
        out.append({'name': entry.get('target'), 'db': c.get('db'), 'host': c.get('host') or None,
                    'port': c.get('port') or None, 'database': c.get('database') if isinstance(c.get('database'), str)
                    else None, 'deleted_at': entry.get('timestamp'), 'deleted_by': entry.get('actor'),
                    'reason': c.get('deleted_reason')})
    return out

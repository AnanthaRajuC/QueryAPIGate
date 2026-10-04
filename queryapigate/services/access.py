"""API keys and roles as the Management API presents them (/api/v1/api-keys, /api/v1/roles - BACKLOG #72).

The grant model and its validation live in apikeys.py and are unchanged; this module gives them the v1 shapes and
conventions: a key's secret is shown once, in the create response, and never again (only its hash is stored);
duplicate names are 409s; unknown fields are refused; and every change is audited exactly as the legacy routes audit
theirs. A role is a template: a key created "from" it copies its grants once, and editing or deleting the role later
never touches that key.
"""
import hashlib
import json
from datetime import date

from .. import apikeys, metrics, store
from ..errors import ApiError

# A grant's fields, shared by keys and roles.
GRANT_FIELDS = ('connections', 'queries', 'collections', 'allow_writes', 'allowed_write_ops', 'allowed_tables',
                'rate_limit', 'allowed_ips')
# Fields where an explicit null means "clear it" (apikeys' _UNSET sentinel), so presence matters, not just value.
_CLEARABLE = ('expires_at', 'rate_limit', 'allowed_ips', 'allowed_write_ops', 'allowed_tables')


def _diff(before, after):
    keys = set(before or {}) | set(after or {})
    return {k: {'from': (before or {}).get(k), 'to': (after or {}).get(k)} for k in sorted(keys)
            if (before or {}).get(k) != (after or {}).get(k)}


def _grants(entry):
    return {
        'connections': entry.get('connections', '*'),
        'queries': entry.get('queries') or [],
        'collections': entry.get('collections') or [],
        'allow_writes': bool(entry.get('allow_writes')),
        'allowed_write_ops': entry.get('allowed_write_ops'),
        'allowed_tables': entry.get('allowed_tables'),
        'rate_limit': entry.get('rate_limit'),
        'allowed_ips': entry.get('allowed_ips'),
    }


def etag(entry):
    return '"' + hashlib.sha256(json.dumps(entry, sort_keys=True, default=str).encode()).hexdigest()[:32] + '"'


def _check_name(name, what):
    if not isinstance(name, str) or not apikeys._NAME_RE.match(name):
        raise ApiError(f"{what} name is 1-100 letters, digits, spaces, '.', '_' or '-'", code='invalid_name')


def _refuse_unknown(data, allowed):
    if not isinstance(data, dict):
        raise ApiError('The request body must be a JSON object', code='invalid_body')
    unknown = sorted(set(data) - set(allowed))
    if unknown:
        raise ApiError(f"Unknown field(s): {', '.join(unknown)}", code='unknown_field')


# --------------------------------------------------------------------------------------
# API keys
# --------------------------------------------------------------------------------------

def to_key(name, entry):
    expires = entry.get('expires_at')
    usage = metrics.summary_for_key(name)
    return {
        'name': name,
        **_grants(entry),
        'active': entry.get('active', True) is not False,
        'expires_at': expires,
        'expired': bool(expires and expires < date.today().isoformat()),
        'created_at': entry.get('created_at'),
        'created_from_role': entry.get('created_from_role'),
        'last_used_at': entry.get('last_used_at'),
        'example': entry.get('example') is True,
        'usage': {'queries': usage.get('queries', 0), 'errors': usage.get('errors', 0), 'rows': usage.get('rows', 0)},
    }


def load_key(name):
    entry = apikeys.list_keys().get(name) if isinstance(name, str) else None
    if entry is None:
        raise ApiError(f"API key '{name}' not found", 404, code='key_not_found')
    return entry


def list_keys():
    return [to_key(name, entry) for name, entry in sorted(apikeys.list_keys().items())]


def create_key(data, actor):
    """A new key - its grants given directly, or copied from `role` - and its secret, returned once, here only."""
    _refuse_unknown(data, ('name', 'role', 'expires_at', *GRANT_FIELDS))
    name = data.get('name')
    _check_name(name, 'An API key')
    if name in apikeys.list_keys():
        raise ApiError(f"An API key named '{name}' already exists", 409, code='key_exists')
    if data.get('role') is not None and data['role'] not in apikeys.list_roles():
        raise ApiError(f"Role '{data['role']}' not found", 404, code='role_not_found')
    secret = apikeys.create_key(name, role=data.get('role'), expires_at=data.get('expires_at'),
                                **{field: data.get(field) for field in GRANT_FIELDS})
    store.record_audit(actor, 'create_key', name, apikeys.list_keys().get(name))
    return name, secret


def update_key(name, data, actor):
    """Change some of a key's grants, its expiry or `active` (false revokes it at once); the rest are kept."""
    _refuse_unknown(data, ('active', 'expires_at', *GRANT_FIELDS))
    before = load_key(name)
    clearable = {k: data[k] for k in _CLEARABLE if k in data}
    apikeys.update_key(name, connections=data.get('connections'), allow_writes=data.get('allow_writes'),
                       active=data.get('active'), queries=data.get('queries'), collections=data.get('collections'),
                       **clearable)
    changes = _diff(before, apikeys.list_keys().get(name))
    if changes:
        store.record_audit(actor, 'update_key', name, changes)


def delete_key(name, actor):
    before = load_key(name)
    apikeys.delete_key(name)
    store.record_audit(actor, 'delete_key', name, before)


# --------------------------------------------------------------------------------------
# Roles
# --------------------------------------------------------------------------------------

def to_role(name, entry, keys=None):
    keys = apikeys.list_keys() if keys is None else keys
    return {
        'name': name,
        **_grants(entry),
        'created_at': entry.get('created_at'),
        'example': entry.get('example') is True,
        'keys_created': sum(1 for k in keys.values() if k.get('created_from_role') == name),
    }


def load_role(name):
    entry = apikeys.list_roles().get(name) if isinstance(name, str) else None
    if entry is None:
        raise ApiError(f"Role '{name}' not found", 404, code='role_not_found')
    return entry


def list_roles():
    keys = apikeys.list_keys()
    return [to_role(name, entry, keys) for name, entry in sorted(apikeys.list_roles().items())]


def create_role(data, actor):
    _refuse_unknown(data, ('name', *GRANT_FIELDS))
    name = data.get('name')
    _check_name(name, 'A role')
    if name in apikeys.list_roles():
        raise ApiError(f"A role named '{name}' already exists", 409, code='role_exists')
    apikeys.create_role(name, connections=data.get('connections'), allow_writes=bool(data.get('allow_writes', False)),
                        queries=data.get('queries'), rate_limit=data.get('rate_limit'),
                        allowed_ips=data.get('allowed_ips'), allowed_write_ops=data.get('allowed_write_ops'),
                        collections=data.get('collections'), allowed_tables=data.get('allowed_tables'))
    store.record_audit(actor, 'create_role', name, apikeys.list_roles().get(name))
    return name


def update_role(name, data, actor):
    _refuse_unknown(data, GRANT_FIELDS)
    before = load_role(name)
    clearable = {k: data[k] for k in _CLEARABLE if k in data and k != 'expires_at'}
    apikeys.update_role(name, connections=data.get('connections'), allow_writes=data.get('allow_writes'),
                        queries=data.get('queries'), collections=data.get('collections'), **clearable)
    changes = _diff(before, apikeys.list_roles().get(name))
    if changes:
        store.record_audit(actor, 'update_role', name, changes)


def delete_role(name, actor):
    """Removes the template only - keys already created from it keep their grants."""
    before = load_role(name)
    apikeys.delete_role(name)
    store.record_audit(actor, 'delete_role', name, before)

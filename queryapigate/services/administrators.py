"""Administrators and their admin tokens as the Management API presents them (/api/v1/administrators - ADR 0003).

admins.py holds the rules (names, roles, the last owner, token hashing); this module gives them the v1 shapes and
conventions, and audits every change. A token's secret is in the response that issues it and never again.

Anyone signed in as an administrator may list, issue and revoke their *own* tokens, whatever their role; doing so for
someone else takes `admins.read` / `admins.write` (owners). The route checks that with `require_self_or()`.
"""
from .. import admins, store
from ..adminroles import ROLES
from ..errors import ApiError
from .access import _diff, _refuse_unknown, etag  # noqa: F401 - etag re-exported for the routes


def require_self_or(permission, name, capability):
    """Allow `permission` to act on `name`'s tokens: it is that administrator, or its role holds `capability`."""
    if permission.via == 'token' and permission.name == name:
        return
    if capability not in ROLES.get(permission.role, ()):
        raise ApiError("Only an owner may manage another administrator's tokens", 403, code='role_forbidden',
                       capability=capability, role=permission.role)


def to_admin(admin):
    return {**admin, 'tokens': len(admins.list_tokens(admin['name']))}


def list_admins():
    return [to_admin(admin) for admin in admins.list_admins()]


def load(name):
    return to_admin(admins.get_admin(name))


def create(data, actor):
    _refuse_unknown(data, ('name', 'role', 'email'))
    admin = admins.create_admin(data.get('name'), data.get('role'), email=data.get('email'), created_by=actor)
    store.record_audit(actor, 'create_admin', admin['name'], {'role': admin['role'], 'email': admin['email']})
    return admin['name']


def update(name, data, actor):
    """Change the role, email or `active` (false stops every token of theirs at once)."""
    _refuse_unknown(data, ('role', 'email', 'active'))
    before = admins.get_admin(name)
    after = admins.update_admin(name, role=data.get('role'), active=data.get('active'),
                                **({'email': data['email']} if 'email' in data else {}))
    fields = ('role', 'email', 'active')
    changes = _diff({k: before[k] for k in fields}, {k: after[k] for k in fields})
    if changes:
        store.record_audit(actor, 'update_admin', name, changes)


def delete(name, actor):
    before = admins.get_admin(name)
    admins.delete_admin(name)
    store.record_audit(actor, 'delete_admin', name, {'role': before['role'], 'email': before['email']})


def list_tokens(name):
    return admins.list_tokens(name)


def issue_token(name, data, actor):
    _refuse_unknown(data, ('label', 'expires_at'))
    token, secret = admins.issue_token(name, label=data.get('label'), expires_at=data.get('expires_at'))
    store.record_audit(actor, 'issue_admin_token', name, {'token': token['id'], 'label': token['label'],
                                                         'expires_at': token['expires_at']})
    return {**token, 'secret': secret}


def revoke_token(name, token_id, actor):
    admins.revoke_token(name, token_id)
    store.record_audit(actor, 'revoke_admin_token', name, {'token': token_id})

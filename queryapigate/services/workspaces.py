"""Workspaces and their members as the Management API presents them (/api/v1/workspaces - ADR 0023).

workspaces.py holds the rules; this module gives them the v1 shapes, decides who sees and changes what, and audits
every change.

- **Seeing:** a server-wide owner, admin or auditor (`workspaces.read`) sees every workspace; any other
  administrator, the ones they are a member of. A workspace they may not see answers 404, as one that doesn't exist -
  so a name isn't confirmed to someone outside it.
- **Changing members:** an owner (`workspaces.write`), or that workspace's own owner - a membership as `owner` there.
- **Creating, describing, removing a workspace:** owners only (`workspaces.write`, checked before the route).
"""
from .. import adminroles, store, workspaces
from ..errors import ApiError
from .access import _refuse_unknown


def _sees_all(permission):
    return adminroles.allows(permission, 'workspaces.read')


def _visible(permission, name):
    """The workspace, if `permission` may see it; 404 otherwise - the same answer as for one that doesn't exist."""
    if not _sees_all(permission) and name not in (permission.memberships or {}):
        raise ApiError(f"No workspace named '{name}'", 404, code='workspace_not_found')
    return workspaces.get_workspace(name)


def _require_member_manager(permission, name):
    if adminroles.allows(permission, 'workspaces.write'):
        return
    if (permission.memberships or {}).get(name) == 'owner':
        return
    raise ApiError(f"Only an owner - of the server, or of workspace '{name}' - may change who belongs to it", 403,
                   code='role_forbidden', capability='workspaces.write', role=permission.role)


def list_visible(permission):
    if _sees_all(permission):
        return workspaces.list_workspaces()
    return workspaces.list_workspaces(names=set(permission.memberships or {}))


def load(permission, name):
    return _visible(permission, name)


def create(data, actor):
    _refuse_unknown(data, ('name', 'description'))
    workspace = workspaces.create_workspace(data.get('name'), data.get('description'), created_by=actor)
    store.record_audit(actor, 'create_workspace', workspace['name'], {'description': workspace['description']})
    return workspace


def update(name, data, actor):
    _refuse_unknown(data, ('description',))
    if 'description' not in data:
        raise ApiError('Nothing to change: a workspace has only its description to change', code='invalid_body')
    before = workspaces.get_workspace(name)
    after = workspaces.update_workspace(name, data['description'])
    if before['description'] != after['description']:
        store.record_audit(actor, 'update_workspace', name,
                           {'description': {'from': before['description'], 'to': after['description']}})
    return after


def delete(name, actor):
    workspaces.delete_workspace(name)
    store.record_audit(actor, 'delete_workspace', name, None)


def list_members(permission, name):
    _visible(permission, name)
    return workspaces.list_members(name)


def set_member(permission, name, admin_name, data, actor):
    _visible(permission, name)
    _require_member_manager(permission, name)
    _refuse_unknown(data, ('role',))
    member, before = workspaces.set_member(name, admin_name, data.get('role'), added_by=actor)
    if before != member['role']:
        store.record_audit(actor, 'set_workspace_member', name,
                           {'admin': admin_name, 'role': {'from': before, 'to': member['role']}})
    return member


def remove_member(permission, name, admin_name, actor):
    _visible(permission, name)
    _require_member_manager(permission, name)
    role = workspaces.remove_member(name, admin_name)
    store.record_audit(actor, 'remove_workspace_member', name, {'admin': admin_name, 'role': role})

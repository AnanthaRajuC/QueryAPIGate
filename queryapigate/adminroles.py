"""What each administrator role may do in the Management API (ADR 0003, BACKLOG #84).

Every /api/v1 operation needs one capability (OPERATIONS, by Flask endpoint); every role is a fixed set of them
(ROLES). `authorize()` applies the two in `v1.admin_only()`, before any route runs. An operation missing from
OPERATIONS is refused to everyone but an owner - and tests/test_adminroles.py fails while one is missing, so a new
route can't ship without a decision about who may call it.

The capability names and the four roles are part of the 1.0 contract: a later custom role is just another set of
these names.
"""
from .errors import ApiError

ROLE_NAMES = ('owner', 'admin', 'developer', 'auditor')

CAPABILITIES = {
    'queries.read': 'read saved queries, their versions, history and collections',
    'queries.write': 'create, change, publish and delete saved queries; rename collections',
    'connections.read': 'read connections (passwords are always masked) and browse their schemas',
    'connections.write': 'create, change, delete and test connections',
    'access.read': 'read API keys and roles',
    'access.write': 'create, change and delete API keys and roles',
    'cache.read': 'read cached responses',
    'cache.write': 'clear cached responses',
    'examples.write': 'load and remove the bundled example APIs',
    'observe': 'read the audit log, run history, alerts, instances and MCP status',
    'settings.read': "read the server's settings",
    'secrets.rotate': 're-encrypt stored passwords under a new encryption key (ADR 0009)',
    'admins.read': 'read administrators and their tokens',
    'admins.write': 'create, change and delete administrators, and issue or revoke anyone\'s tokens',
    'self': 'see who you are, and manage your own admin tokens',
    'writes.approve': 'approve or reject a write an AI agent asked to make over MCP (ADR 0008)',
    'exports.read': 'read destinations (secrets masked), exports and their runs',
    'exports.write': 'create, change and delete exports',
    'exports.run': 'run an export',
    'destinations.write': 'create, change, test and delete destinations',
    'config.apply': 'see what a configuration file would change, and apply it - any of the above (ADR 0014)',
    'history.forget': "delete a person's runs and usage counts, on request (ADR 0016)",
    'workspaces.read': 'list every workspace and its members (ADR 0023)',
    'workspaces.write': 'create and remove workspaces, and give administrators roles in any of them',
}

# What only a server-wide role gives (ADR 0023 §3): the server itself, and the people who run it. A role from a
# workspace membership is that role without these - whatever it is called, it administers one workspace.
SERVER_ONLY = frozenset({'admins.read', 'admins.write', 'settings.read', 'secrets.rotate', 'config.apply',
                         'history.forget', 'workspaces.read', 'workspaces.write'})

_EVERYONE = {'queries.read', 'connections.read', 'observe', 'self'}
ROLES = {
    'owner': set(CAPABILITIES),
    'admin': set(CAPABILITIES) - {'admins.read', 'admins.write', 'secrets.rotate', 'config.apply', 'history.forget'},
    'developer': _EVERYONE | {'queries.write', 'exports.read', 'exports.run'},
    'auditor': _EVERYONE | {'access.read', 'settings.read', 'exports.read', 'workspaces.read'},
}
ROLES['admin'] -= {'workspaces.write'}  # like administrators: who belongs where is an owner's to decide

# Which roles may run SQL - ad-hoc or saved - on every connection, as the shared key can. An auditor reads what
# happened and needs no rows.
DATA_ROLES = {'owner', 'admin', 'developer'}

OPERATIONS = {
    'v1.list_queries': 'queries.read', 'v1.get_query': 'queries.read', 'v1.query_history': 'queries.read',
    'v1.get_version': 'queries.read', 'v1.version_flow': 'queries.read', 'v1.list_collections': 'queries.read',
    'v1.collection_postman': 'queries.read', 'v1.examples_status': 'queries.read',
    'v1.create_query': 'queries.write', 'v1.update_query': 'queries.write', 'v1.delete_query': 'queries.write',
    'v1.publish': 'queries.write', 'v1.unpublish': 'queries.write', 'v1.add_version': 'queries.write',
    'v1.update_version': 'queries.write', 'v1.delete_version': 'queries.write', 'v1.validate_query': 'queries.write',
    'v1.rename_collection': 'queries.write',
    'v1.list_connections': 'connections.read', 'v1.get_connection': 'connections.read',
    'v1.deleted_connections': 'connections.read', 'v1.connection_schema': 'connections.read',
    'v1.create_connection': 'connections.write', 'v1.update_connection': 'connections.write',
    'v1.delete_connection': 'connections.write', 'v1.test_connection': 'connections.write',
    'v1.connection_databases': 'connections.write',
    'v1.list_descriptions': 'connections.read', 'v1.update_descriptions': 'connections.write',
    'v1.import_descriptions': 'connections.write',
    'v1.list_api_keys': 'access.read', 'v1.get_api_key': 'access.read', 'v1.list_roles': 'access.read',
    'v1.get_role': 'access.read',
    'v1.create_api_key': 'access.write', 'v1.update_api_key': 'access.write', 'v1.delete_api_key': 'access.write',
    'v1.rotate_api_key': 'access.write', 'v1.end_previous_secret': 'access.write',
    'v1.create_role': 'access.write', 'v1.update_role': 'access.write', 'v1.delete_role': 'access.write',
    'v1.list_cache_entries': 'cache.read', 'v1.get_cache_entry': 'cache.read',
    'v1.clear_cache': 'cache.write', 'v1.delete_cache_entry': 'cache.write',
    'v1.load_examples': 'examples.write', 'v1.unload_examples': 'examples.write',
    'v1.list_alerts': 'observe', 'v1.list_audit': 'observe', 'v1.search_history': 'observe',
    'v1.verify_audit': 'observe', 'v1.audit_delivery': 'observe',
    'v1.list_instances': 'observe', 'v1.agent_usage': 'observe', 'v1.quota_usage': 'observe',
    'v1.list_write_requests': 'writes.approve', 'v1.get_write_request': 'writes.approve',
    'v1.approve_write_request': 'writes.approve', 'v1.reject_write_request': 'writes.approve',
    'v1.mcp_status': 'observe', 'v1.mcp_tools': 'observe',
    'v1.get_settings': 'settings.read', 'v1.rekey': 'secrets.rotate',
    'v1.list_administrators': 'admins.read', 'v1.get_administrator': 'admins.read',
    'v1.create_administrator': 'admins.write', 'v1.update_administrator': 'admins.write',
    'v1.delete_administrator': 'admins.write',
    # your own tokens, whatever your role; someone else's take admins.read/write, checked in the route
    'v1.list_admin_tokens': 'self', 'v1.issue_admin_token': 'self', 'v1.revoke_admin_token': 'self',
    'v1.me': 'self',
    'v1.list_destinations': 'exports.read', 'v1.get_destination': 'exports.read',
    'v1.create_destination': 'destinations.write', 'v1.update_destination': 'destinations.write',
    'v1.delete_destination': 'destinations.write', 'v1.test_destination': 'destinations.write',
    'v1.test_destination_fields': 'destinations.write',
    'v1.list_exports': 'exports.read', 'v1.get_export': 'exports.read', 'v1.list_export_runs': 'exports.read',
    'v1.create_export': 'exports.write', 'v1.update_export': 'exports.write', 'v1.delete_export': 'exports.write',
    'v1.run_export': 'exports.run',
    'v1.config_diff': 'config.apply', 'v1.config_apply': 'config.apply', 'v1.config_managed': 'observe',
    'v1.forget_history': 'history.forget',
    # every administrator sees the workspaces they belong to (all of them with workspaces.read) - the route narrows
    'v1.list_workspaces': 'self', 'v1.get_workspace': 'self', 'v1.list_workspace_members': 'self',
    # a workspace's own owner may also manage its members - checked in the route
    'v1.set_workspace_member': 'self', 'v1.remove_workspace_member': 'self',
    'v1.create_workspace': 'workspaces.write', 'v1.update_workspace': 'workspaces.write',
    'v1.delete_workspace': 'workspaces.write',
}


def require_to_widen_access(permission, gained, what):
    """A change that gives API keys or roles reach they didn't have - moving a query into a collection they're granted,
    merging collections - is an access decision, so it takes `access.write` (owners and admins), whoever may otherwise
    make it. `gained` is {'keys': [...], 'roles': [...]}; nothing gained, nothing to check."""
    if not (gained.get('keys') or gained.get('roles')):
        return
    role = getattr(permission, 'role', None)
    if allows(permission, 'access.write'):
        return
    who = ', '.join([*(f"key '{k}'" for k in gained.get('keys', [])),
                     *(f"role '{r}'" for r in gained.get('roles', []))])
    raise ApiError(f'{what} would give {who} access it does not have now - that takes an owner or admin', 403,
                   code='role_forbidden', capability='access.write', role=role, gaining=gained)


def capabilities(role, server_wide=True):
    """What `role` may do - without SERVER_ONLY when it comes from a workspace membership."""
    granted = ROLES.get(role, set())
    return sorted(granted if server_wide else granted - SERVER_ONLY)


def allows(permission, capability):
    """Whether `permission`'s role holds `capability` - in the workspace it acts in, for a membership's role."""
    role = getattr(permission, 'role', None)
    if role not in ROLES or capability not in ROLES[role]:
        return False
    return getattr(permission, 'server_wide', True) or capability not in SERVER_ONLY


def is_administrator(permission):
    """An administrator of something: a role here, or a membership in some workspace."""
    return getattr(permission, 'role', None) is not None or bool(getattr(permission, 'memberships', None))


def authorize(permission, endpoint):
    """Raise unless `permission` may call the /api/v1 operation `endpoint`: 403 `admin_only` for a caller that
    isn't an administrator at all (a scoped key, a signed-in app user), 403 `workspace_required` for an administrator
    with no role in the workspace the call is for - only in others - and 403 `role_forbidden` for one whose role
    lacks the capability. `self` operations are any administrator's, role or not."""
    needed = OPERATIONS.get(endpoint, 'admins.write')  # unmapped: owners only, and the test says so
    if not is_administrator(permission):
        raise ApiError('This API key is not authorized to manage the server configuration', 403, code='admin_only')
    if needed == 'self':
        return
    role = getattr(permission, 'role', None)
    if role is None:
        workspace = getattr(permission, 'workspace', None)
        names = ', '.join(sorted(permission.memberships))
        raise ApiError(f"You have no role in the workspace '{workspace}' - only in {names}: name the workspace you "
                       'mean', 403, code='workspace_required', workspace=workspace,
                       workspaces=sorted(permission.memberships))
    if not allows(permission, needed):
        where = '' if getattr(permission, 'server_wide', True) else ' in a workspace (it is server-wide)'
        raise ApiError(f"The {role} role may not {CAPABILITIES[needed]}{where}", 403, code='role_forbidden',
                       capability=needed, role=role)

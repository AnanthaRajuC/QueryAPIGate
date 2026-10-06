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
    'admins.read': 'read administrators and their tokens',
    'admins.write': 'create, change and delete administrators, and issue or revoke anyone\'s tokens',
    'self': 'see who you are, and manage your own admin tokens',
    'exports.read': 'read destinations (secrets masked), exports and their runs',
    'exports.write': 'create, change and delete exports',
    'exports.run': 'run an export',
    'destinations.write': 'create, change, test and delete destinations',
}

_EVERYONE = {'queries.read', 'connections.read', 'observe', 'self'}
ROLES = {
    'owner': set(CAPABILITIES),
    'admin': set(CAPABILITIES) - {'admins.read', 'admins.write'},
    'developer': _EVERYONE | {'queries.write', 'exports.read', 'exports.run'},
    'auditor': _EVERYONE | {'access.read', 'settings.read', 'exports.read'},
}

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
    'v1.list_api_keys': 'access.read', 'v1.get_api_key': 'access.read', 'v1.list_roles': 'access.read',
    'v1.get_role': 'access.read',
    'v1.create_api_key': 'access.write', 'v1.update_api_key': 'access.write', 'v1.delete_api_key': 'access.write',
    'v1.create_role': 'access.write', 'v1.update_role': 'access.write', 'v1.delete_role': 'access.write',
    'v1.list_cache_entries': 'cache.read', 'v1.get_cache_entry': 'cache.read',
    'v1.clear_cache': 'cache.write', 'v1.delete_cache_entry': 'cache.write',
    'v1.load_examples': 'examples.write', 'v1.unload_examples': 'examples.write',
    'v1.list_alerts': 'observe', 'v1.list_audit': 'observe', 'v1.search_history': 'observe',
    'v1.list_instances': 'observe', 'v1.mcp_status': 'observe', 'v1.mcp_tools': 'observe',
    'v1.get_settings': 'settings.read',
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
}


def require_to_widen_access(permission, gained, what):
    """A change that gives API keys or roles reach they didn't have - moving a query into a collection they're granted,
    merging collections - is an access decision, so it takes `access.write` (owners and admins), whoever may otherwise
    make it. `gained` is {'keys': [...], 'roles': [...]}; nothing gained, nothing to check."""
    if not (gained.get('keys') or gained.get('roles')):
        return
    role = getattr(permission, 'role', None)
    if role is not None and 'access.write' in ROLES[role]:
        return
    who = ', '.join([*(f"key '{k}'" for k in gained.get('keys', [])),
                     *(f"role '{r}'" for r in gained.get('roles', []))])
    raise ApiError(f'{what} would give {who} access it does not have now - that takes an owner or admin', 403,
                   code='role_forbidden', capability='access.write', role=role, gaining=gained)


def capabilities(role):
    return sorted(ROLES.get(role, ()))


def authorize(permission, endpoint):
    """Raise unless `permission` may call the /api/v1 operation `endpoint`: 403 `admin_only` for a caller that
    isn't an administrator at all (a scoped key, a signed-in app user), 403 `role_forbidden` for one whose role
    lacks the capability."""
    role = getattr(permission, 'role', None)
    if role is None:
        raise ApiError('This API key is not authorized to manage the server configuration', 403, code='admin_only')
    needed = OPERATIONS.get(endpoint, 'admins.write')  # unmapped: owners only, and the test says so
    if needed not in ROLES[role]:
        raise ApiError(f"The {role} role may not {CAPABILITIES[needed]}", 403, code='role_forbidden',
                       capability=needed, role=role)

"""Workspaces and their members (ADR 0023): delegated administration for teams on one server.

A workspace owns connections, saved queries, API keys, roles, destinations and exports - each name unique within it
(db.py, schema 17). `default` holds everything a server had before workspaces, and what is made without naming one;
it can't be removed. A workspace is removed only once it owns nothing - deleting it never deletes a team's work.

A **membership** gives an administrator one of the four roles (adminroles.py) in one workspace, and nothing outside
it. An administrator's own `role` (administrators.role) is their server-wide one; with none (NULL), memberships are
all they have. admins.permission_for() works out which applies to a request.

The rules are here; services/workspaces.py gives them the Management API's shapes and audits each change.
"""
import json
import re

from . import db, store
from .adminroles import ROLE_NAMES
from .db import DEFAULT_WORKSPACE
from .errors import ApiError

_NAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$')
_DESCRIPTION_MAX = 500
# What a workspace owns, as the store keeps it: a workspace with a row in any of these isn't empty.
OWNED = (('connections', 'connections'), ('saved_queries', 'queries'), ('api_keys', 'API keys'), ('roles', 'roles'),
         ('destinations', 'destinations'), ('exports', 'exports'))
_ONE = {'connections': 'connection', 'queries': 'query', 'API keys': 'API key', 'roles': 'role',
        'destinations': 'destination', 'exports': 'export'}


def _not_found(name):
    return ApiError(f"No workspace named '{name}'", 404, code='workspace_not_found')


def _row(row, members):
    details = json.loads(row['details_json'])
    return {'name': row['name'], 'description': details.get('description'), 'created_at': row['created_at'],
            'created_by': details.get('created_by'), 'members': members}


def _member_counts(conn):
    return {row[0]: row[1] for row in conn.execute(
        'SELECT workspace, COUNT(*) FROM workspace_members GROUP BY workspace').fetchall()}


def list_workspaces(names=None):
    """Every workspace, by name - or only those in `names`."""
    conn = db.connection()
    counts = _member_counts(conn)
    rows = conn.execute('SELECT name, created_at, details_json FROM workspaces ORDER BY name').fetchall()
    return [_row(row, counts.get(row['name'], 0)) for row in rows if names is None or row['name'] in names]


def _get(conn, name):
    row = conn.execute('SELECT name, created_at, details_json FROM workspaces WHERE name = ?', (name,)).fetchone()
    if row is None:
        return None
    count = conn.execute('SELECT COUNT(*) FROM workspace_members WHERE workspace = ?', (name,)).fetchone()[0]
    return _row(row, count)


def exists(name):
    return isinstance(name, str) and _get(db.connection(), name) is not None


def get_workspace(name):
    workspace = _get(db.connection(), name)
    if workspace is None:
        raise _not_found(name)
    return workspace


def _validate_description(description):
    if description is None:
        return None
    if not isinstance(description, str) or len(description) > _DESCRIPTION_MAX:
        raise ApiError(f'description must be text of at most {_DESCRIPTION_MAX} characters, or null',
                       code='invalid_body')
    return description


def create_workspace(name, description=None, created_by=None):
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise ApiError("A workspace's name is 1-100 letters, digits, '.', '_' or '-', starting with a letter or digit",
                       code='invalid_name')
    details = {'description': _validate_description(description), 'created_by': created_by}
    with db.transaction() as conn:
        if _get(conn, name) is not None:
            raise ApiError(f"A workspace named '{name}' already exists", 409, code='workspace_exists')
        conn.execute('INSERT INTO workspaces (name, created_at, details_json) VALUES (?, ?, ?)',
                     (name, store.now(), json.dumps(details)))
        return _get(conn, name)


def update_workspace(name, description):
    """Its description - the only thing about a workspace that changes; its name is what everything in it is under."""
    with db.transaction() as conn:
        row = conn.execute('SELECT details_json FROM workspaces WHERE name = ?', (name,)).fetchone()
        if row is None:
            raise _not_found(name)
        details = {**json.loads(row['details_json']), 'description': _validate_description(description)}
        conn.execute('UPDATE workspaces SET details_json = ? WHERE name = ?', (json.dumps(details), name))
        return _get(conn, name)


def contents(conn, name):
    """{what: how many} the workspace owns, of OWNED - empty when it owns nothing."""
    counts = {}
    for table, label in OWNED:
        count = conn.execute(f'SELECT COUNT(*) FROM {table} WHERE workspace = ?', (name,)).fetchone()[0]
        if count:
            counts[label] = count
    return counts


def delete_workspace(name):
    """Remove an empty workspace, and its memberships. Its runs stay in history, and its name in the audit log."""
    if name == DEFAULT_WORKSPACE:
        raise ApiError("The default workspace can't be removed: it holds what is made without naming a workspace",
                       409, code='workspace_protected')
    with db.transaction() as conn:
        if _get(conn, name) is None:
            raise _not_found(name)
        owned = contents(conn, name)
        if owned:
            listed = ', '.join(f'{count} {_ONE[label] if count == 1 else label}' for label, count in owned.items())
            raise ApiError(f"Workspace '{name}' still has {listed} - delete or move them first", 409,
                           code='workspace_not_empty', contents=owned)
        conn.execute('DELETE FROM workspace_members WHERE workspace = ?', (name,))
        conn.execute('DELETE FROM workspaces WHERE name = ?', (name,))


# --------------------------------------------------------------------------------------
# Members
# --------------------------------------------------------------------------------------

def _member_row(row):
    details = json.loads(row['details_json'])
    return {'admin': row['admin_name'], 'role': row['role'], 'created_at': row['created_at'],
            'added_by': details.get('added_by')}


def list_members(name):
    get_workspace(name)
    rows = db.connection().execute('SELECT admin_name, role, created_at, details_json FROM workspace_members '
                                   'WHERE workspace = ? ORDER BY admin_name', (name,)).fetchall()
    return [_member_row(row) for row in rows]


def set_member(name, admin_name, role, added_by=None):
    """Give `admin_name` `role` in the workspace, or change it. Returns (member, the role they had - or None)."""
    if role not in ROLE_NAMES:
        raise ApiError(f"role must be one of {', '.join(ROLE_NAMES)}", code='invalid_body')
    with db.transaction() as conn:
        if _get(conn, name) is None:
            raise _not_found(name)
        if conn.execute('SELECT 1 FROM administrators WHERE name = ?', (admin_name,)).fetchone() is None:
            raise ApiError(f"No administrator named '{admin_name}'", 404, code='admin_not_found')
        before = conn.execute('SELECT role FROM workspace_members WHERE workspace = ? AND admin_name = ?',
                              (name, admin_name)).fetchone()
        if before is None:
            conn.execute('INSERT INTO workspace_members (workspace, admin_name, role, created_at, details_json) '
                         'VALUES (?, ?, ?, ?, ?)', (name, admin_name, role, store.now(),
                                                    json.dumps({'added_by': added_by})))
        else:
            conn.execute('UPDATE workspace_members SET role = ? WHERE workspace = ? AND admin_name = ?',
                         (role, name, admin_name))
        row = conn.execute('SELECT admin_name, role, created_at, details_json FROM workspace_members '
                           'WHERE workspace = ? AND admin_name = ?', (name, admin_name)).fetchone()
        return _member_row(row), (before[0] if before is not None else None)


def remove_member(name, admin_name):
    """Take `admin_name` out of the workspace. Returns the role they had."""
    with db.transaction() as conn:
        if _get(conn, name) is None:
            raise _not_found(name)
        row = conn.execute('SELECT role FROM workspace_members WHERE workspace = ? AND admin_name = ?',
                           (name, admin_name)).fetchone()
        if row is None:
            raise ApiError(f"'{admin_name}' isn't a member of workspace '{name}'", 404, code='member_not_found')
        conn.execute('DELETE FROM workspace_members WHERE workspace = ? AND admin_name = ?', (name, admin_name))
        return row[0]


def memberships(admin_name, conn=None):
    """{workspace: role} for every workspace `admin_name` is a member of."""
    conn = conn or db.connection()
    return {row[0]: row[1] for row in conn.execute(
        'SELECT workspace, role FROM workspace_members WHERE admin_name = ? ORDER BY workspace',
        (admin_name,)).fetchall()}

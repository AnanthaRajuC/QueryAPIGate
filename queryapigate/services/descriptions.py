"""Descriptions as the Management API presents them (/api/v1/connections/<name>/descriptions - BACKLOG #86).

descriptions.py holds the rules and the store; this module audits every change - which descriptions changed, and to
what, since a description is what an agent is told a column means.
"""
from .. import descriptions, store
from ..errors import ApiError
from . import connections


def _label(table, column):
    return (table or '(connection)') + (f'.{column}' if column else '')


def _audit(actor, action, name, changed, **extra):
    if changed:
        store.record_audit(actor, action, name, {
            **extra, 'descriptions': {_label(t, c): {'from': before, 'to': after}
                                      for (t, c), (before, after) in sorted(changed.items())}})


def list_for(name):
    connections.load(name)
    return {'items': descriptions.list_for(name)}


def update(name, data, actor):
    connections.load(name)
    if not isinstance(data, dict) or set(data) != {'items'}:
        raise ApiError('The body is {"items": [{"table", "column", "description"}, ...]}', code='invalid_body')
    items = descriptions.validate_items(data['items'])
    _audit(actor, 'update_descriptions', name, descriptions.write(name, items, actor))
    return list_for(name)


def import_comments(name, data, actor):
    """The database's own comments, as descriptions with source `database`. A written description is kept unless
    `overwrite` is true; a comment since removed from the database is left as it was."""
    connections.load(name)
    data = data or {}
    overwrite = data.get('overwrite', False)
    if not isinstance(overwrite, bool) or not set(data) <= {'overwrite'}:
        raise ApiError('The body is {"overwrite": true|false}, or empty', code='invalid_body')
    found = descriptions.database_comments(name)
    changed = descriptions.write(name, found, actor, source='database', overwrite=overwrite)
    _audit(actor, 'import_descriptions', name, changed, overwrite=overwrite)
    return {'found': len(found), 'imported': len(changed), 'kept': len(found) - len(changed),
            **list_for(name)}

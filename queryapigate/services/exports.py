"""Saved exports as the Management API presents them (/api/v1/exports - ADR 0004). exports.py holds the rules; this
module gives them the v1 shapes and audits every change and every watermark move made by hand."""
from .. import exports, history, store
from .access import _diff, etag  # noqa: F401 - etag re-exported for the routes

_STATE = ('name', 'created_at', 'updated_at', 'watermark', 'running')


def list_all():
    return exports.list_all()


def load(name):
    return exports.get(name)


def create(data, actor):
    fields = {k: v for k, v in data.items() if k != 'name'}
    created = exports.create(data.get('name'), fields)
    store.record_audit(actor, 'create_export', created['name'],
                       {k: v for k, v in created.items() if k not in _STATE})
    return created['name']


def update(name, data, actor):
    before = exports.get(name)
    after = exports.update(name, data)
    changes = _diff({k: v for k, v in before.items() if k not in ('updated_at', 'running')},
                    {k: v for k, v in after.items() if k not in ('updated_at', 'running')})
    if changes:
        store.record_audit(actor, 'update_export', name, changes)


def delete(name, actor):
    before = exports.get(name)
    exports.delete(name)
    store.record_audit(actor, 'delete_export', name, {k: v for k, v in before.items() if k not in _STATE})


def run(name, actor):
    return exports.run(name, actor)


def runs(name, limit=50):
    """The export's newest runs, from run history."""
    export = exports.get(name)
    history.flush()
    found = []
    for filters in ({'query': export['query']}, {'kind': 'adhoc'}):  # adhoc: runs that failed before the query ran
        cursor = None
        for _ in range(5):  # newest pages only - the runs list is for a glance, history keeps the rest
            page, cursor = history.search(limit=200, cursor=cursor, **filters)
            found += [entry for entry in page if entry.get('transport') == 'export' and entry.get('export') == name]
            if cursor is None or len(found) >= limit:
                break
    found.sort(key=lambda entry: entry['executed_at'], reverse=True)
    return found[:limit]

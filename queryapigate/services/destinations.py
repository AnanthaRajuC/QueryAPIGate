"""Destinations as the Management API presents them (/api/v1/destinations - ADR 0004).

destinations.py holds the rules (validation, the secret, the locked writer); this module gives them the v1 shapes
and audits every change - with the secret masked, and a changed one recorded only as "changed".
"""
from .. import config, destinations, store
from .access import _diff, etag  # noqa: F401 - etag re-exported for the routes


def _shown(destination):
    return destinations.masked({k: v for k, v in destination.items()})


def _audited(destination):
    """What the audit log keeps: the fields, never the secret."""
    shown = _shown(destination)
    return {k: v for k, v in shown.items() if k not in ('name', 'created_at', 'updated_at')}


def list_all():
    return [_shown(d) for d in destinations.list_all()]


def load(name):
    return _shown(destinations.get(name))


def create(data, actor):
    fields = {k: v for k, v in data.items() if k != 'name'}
    created = destinations.create(data.get('name'), fields)
    store.record_audit(actor, 'create_destination', created['name'], _audited(created))
    return created['name']


def update(name, data, actor):
    before = destinations.get(name)
    after = destinations.update(name, data)
    changes = _diff(_audited(before), _audited(after))
    if before.get('password') != after.get('password'):
        changes['password'] = 'changed'
    if changes:
        store.record_audit(actor, 'update_destination', name, changes)


def delete(name, actor, used_by=()):
    before = destinations.get(name)
    destinations.delete(name, used_by)
    store.record_audit(actor, 'delete_destination', name, _audited(before))


def test(name=None, fields=None):
    """Write the probe object to a saved destination, or to fields not saved yet (the form's Test button)."""
    if name is not None:
        return destinations.test(destinations.get(name))
    fields = destinations.validate({k: v for k, v in (fields or {}).items() if k != 'name'})
    if fields.get('password') == config.PASSWORD_MASK:
        fields.pop('password')
    return destinations.test(fields)

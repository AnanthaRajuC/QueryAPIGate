"""CDC sources as the Management API presents them (/api/v1/cdc-sources - ADR 0033 §11).

cdcsources.py holds the rules (validation, the secret and its rotation, the setting's sources); this module audits
every change - never with the secret, which is only ever in the response that made it.
"""
from .. import cdcsources, store
from .access import _diff, etag  # noqa: F401 - etag re-exported for the routes


def _audited(source):
    return {k: source[k] for k in ('connection', 'server_name') if source.get(k) is not None}


def list_all():
    return cdcsources.list_all()


def load(name):
    return cdcsources.get(name)


def create(data, actor):
    """Returns the source with its secret - shown this once."""
    source, secret = cdcsources.create(data.get('name'), {k: v for k, v in data.items() if k != 'name'})
    store.record_audit(actor, 'create_cdc_source', source['name'], _audited(source))
    return {**source, 'secret': secret}


def update(name, data, actor):
    before = cdcsources.get(name)
    after = cdcsources.update(name, data)
    changes = _diff(_audited(before), _audited(after))
    if changes:
        store.record_audit(actor, 'update_cdc_source', name, changes)


def rotate(name, grace_minutes, actor):
    """Returns the source with its new secret - shown this once."""
    source, secret = cdcsources.rotate(name, grace_minutes)
    store.record_audit(actor, 'rotate_cdc_source_secret', name,
                       {'grace_minutes': cdcsources._validate_grace(grace_minutes),
                        'previous_expires_at': source['previous_expires_at']})
    return {**source, 'secret': secret}


def end_previous(name, actor):
    until = cdcsources.end_previous(name)
    if until:
        store.record_audit(actor, 'end_cdc_source_previous_secret', name, {'would_have_worked_until': until})
    return cdcsources.get(name)


def delete(name, actor):
    before = cdcsources.get(name)
    cdcsources.delete(name)
    store.record_audit(actor, 'delete_cdc_source', name, _audited(before))

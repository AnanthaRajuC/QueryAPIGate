"""Collections and the example APIs as the Management API presents them (/api/v1/collections, /api/v1/examples -
BACKLOG #72).

A collection is a name a saved query is filed under (moving a query is a change to the query - PATCH
/api/v1/queries/{name}); what lives here is the view across all of them, with who can reach each, plus renaming one
and its Postman export. The example APIs are a marked set of connection, queries, roles and keys that can be installed
and removed as a unit. Every change is audited exactly as the legacy routes audit theirs.
"""
from .. import collection_admin, examples, postman, store
from ..errors import ApiError


def listing():
    """Every collection by name - its queries and the keys and roles whose grants reach it - and the queries in none."""
    described = collection_admin.describe()
    return {'items': [{'name': name, **entry} for name, entry in described['collections'].items()],
            'uncollected': described['uncollected']}


def _valid(name, what):
    try:
        store.validate_collection_name(name)
    except ApiError as error:
        raise ApiError(f'{what}: {error.message}', code='invalid_name') from error


def rename(old, data, actor):
    """Rename `old` to `data['name']`; into an existing collection only with `merge: true` (which also finishes a
    rename that was interrupted). Never narrows anyone's access part-way - see collection_admin.rename_collection()."""
    if not isinstance(data, dict):
        raise ApiError('The request body must be a JSON object', code='invalid_body')
    unknown = sorted(set(data) - {'name', 'merge'})
    if unknown:
        raise ApiError(f"Unknown field(s): {', '.join(unknown)}", code='unknown_field')
    new, merge = data.get('name'), data.get('merge', False)
    if not isinstance(merge, bool):
        raise ApiError('merge must be true or false', code='invalid_body')
    _valid(old, 'Collection')
    _valid(new, 'New name')
    known = collection_admin.describe()['collections']
    if old not in known:
        raise ApiError(f"Collection '{old}' not found", 404, code='collection_not_found')
    if old == new:
        raise ApiError('The new name is the same as the current one', code='invalid_name')
    if new in known and not merge:
        raise ApiError(f"Collection '{new}' already exists - pass merge: true to merge '{old}' into it (which also "
                       'finishes a rename that was interrupted part-way)', 409, code='collection_exists')
    result = collection_admin.rename_collection(old, new, merge=merge)
    store.record_audit(actor, 'rename_collection', old, {'to': new, **result})
    return {'name': new, 'moved': result}


def postman_export(name, base_url):
    _valid(name, 'Collection')
    if name not in collection_admin.describe()['collections']:
        raise ApiError(f"Collection '{name}' not found", 404, code='collection_not_found')
    return postman.build_collection(name, base_url)


def examples_status():
    return examples.status()


def load_examples(actor):
    """Install the examples (idempotent; completes an interrupted load). The example keys' secrets are in the result,
    shown this once."""
    try:
        added = examples.load()
    except ApiError as error:
        if error.status == 409:
            raise ApiError(error.message, 409, code='examples_conflict') from error
        raise
    if added['connection'] or added['queries'] or added['roles'] or added['key_secrets']:
        store.record_audit(actor, 'load_examples', 'examples', examples.redact_for_audit(added))
    return {'added': {'connection': added['connection'], 'queries': added['queries'], 'roles': added['roles'],
                      'keys': sorted(added['key_secrets'])},
            'key_secrets': added['key_secrets'], 'status': examples.status()}


def unload_examples(actor):
    """Remove exactly what is marked as an example. `keys_still_granted`: other keys granted an example collection,
    whose grant is now inert."""
    removed = examples.unload()
    if removed['connection'] or removed['queries'] or removed['roles'] or removed['keys']:
        store.record_audit(actor, 'unload_examples', 'examples', removed)
    return {'removed': {k: removed[k] for k in ('connection', 'queries', 'roles', 'keys')},
            'keys_still_granted': removed['keys_still_granted'], 'status': examples.status()}

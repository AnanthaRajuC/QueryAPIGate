"""Operations that span saved queries *and* the keys/roles granted them - kept out of store.py (which knows
nothing about keys) and apikeys.py (which knows nothing about query files) so neither has to import the other's
concerns. Membership itself lives on the query files (store.py); this module only combines it with grants."""
from . import apikeys, store
from .errors import ApiError


def describe():
    """Every collection with the queries in it and the keys and roles that reach it - the "who can see this?"
    answer an admin needs *before* moving a query in or out, since a collection grant is live (see
    apikeys.py). A collection that only a grant still names (no queries left) is listed with none, so an
    inert grant is visible rather than silent. Also the queries in no collection."""
    members = store.collection_members()
    grants = apikeys.collection_grants()
    collections = {}
    for name in sorted(set(members) | set(grants)):
        granted = grants.get(name, {'keys': [], 'roles': []})
        collections[name] = {'queries': members.get(name, []), 'keys': granted['keys'], 'roles': granted['roles']}
    in_a_collection = {q for names in members.values() for q in names}
    uncollected = [name for name, _, _, _ in store.latest_versions() if name not in in_a_collection]
    return {'collections': collections, 'uncollected': uncollected}


def access_change(previous, current):
    """The keys and roles that gain or lose reach to one query when it moves from collection ``previous`` to
    ``current`` (either may be None). A key that reaches *both* collections gains and loses nothing."""
    grants = apikeys.collection_grants()

    def reaching(collection, kind):
        return set(grants.get(collection, {}).get(kind, [])) if collection is not None else set()

    return {kind: {'gain': sorted(reaching(current, kind) - reaching(previous, kind)),
                   'lose': sorted(reaching(previous, kind) - reaching(current, kind))}
            for kind in ('keys', 'roles')}


def rename_collection(old, new, merge=False):
    """Rename ``old`` to ``new``, resumable and never narrowing access part-way: (1) widen every grant on
    ``old`` to also hold ``new``, (2) re-file the queries, (3) drop ``old`` from the grants. An interruption
    at any point leaves grants that are a superset of the intended result and queries under one name or the
    other, and running the same rename again completes it - which is why an already-existing target needs an
    explicit ``merge`` (a half-finished rename looks exactly like one). Returns what changed."""
    store.validate_collection_name(old)
    store.validate_collection_name(new)
    if old == new:
        raise ApiError('The new name is the same as the current one', code='invalid_body')
    members = store.collection_members()
    grants = apikeys.collection_grants()
    if old not in members and old not in grants:
        raise ApiError(f"Collection '{old}' not found", 404, code='collection_not_found')
    if (new in members or new in grants) and not merge:
        raise ApiError(f"Collection '{new}' already exists. Pass merge=true to merge '{old}' into it - the same "
                       'call also finishes a rename that was interrupted part-way.', 409, code='collection_exists')
    with store.lock:
        apikeys.rewrite_collection_grants(old, new, drop_old=False)
        moved = store.move_collection(old, new)
        keys, roles = apikeys.rewrite_collection_grants(old, new, drop_old=True)
    return {'queries': moved, 'keys': keys, 'roles': roles}

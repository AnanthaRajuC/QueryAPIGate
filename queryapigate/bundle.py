"""Collection bundles: a collection's queries as one portable JSON document (``queryapigate collection export``)
and back into a home folder (``... import``) - for moving a set of queries between environments or sharing
them. A bundle carries *definitions only*: the latest version of each query's SQL, description, tags, declared
parameters, default connection name and cache TTL. Never execution history, API keys, roles or connection
details (a connection is referenced by name and must exist where the bundle is imported).

Import is held to exactly the rules saving a query is - every entry goes through
``definitions.validate_definition()`` - and is all-or-nothing up to the point of writing: the whole bundle is
validated and checked for conflicts first, so a problem in the tenth query stops the import before the first is
written. The writes themselves are one atomic file each; if the process dies part-way, re-running with
``--on-conflict skip`` completes it, since the queries already written are left alone.
"""
from . import definitions, store
from .errors import ApiError

FORMAT = 'queryapigate-collection'
FORMAT_VERSION = 1
POLICIES = ('fail', 'skip', 'new-version')
# Everything an entry may carry. Anything else is an error rather than silently dropped: a field the importer
# ignores is a field whose loss nobody notices.
ENTRY_FIELDS = {'name', 'sql_query', 'query_type', 'mongo_collection', 'mongo_filter', 'mongo_projection',
                'mongo_sort', 'author', 'description', 'tags', 'query_parameters', 'connection_name',
                'cache_ttl'}


def export_bundle(collection):
    store.validate_collection_name(collection)
    queries = []
    for name, _, data, member_of in store.live_versions():
        if member_of != collection:
            continue
        entry = {'name': name}
        for field in sorted(ENTRY_FIELDS - {'name'}):
            if data.get(field) not in (None, {}, []):
                entry[field] = data[field]
        queries.append(entry)
    if not queries:
        raise ApiError(f"Collection '{collection}' not found or empty", 404, code='collection_not_found')
    return {'format': FORMAT, 'format_version': FORMAT_VERSION, 'collection': collection, 'exported_at': store.now(),
            'queries': queries}


def _parse(bundle, collection_override):
    """Validate a decoded bundle; return (collection, [(name, fields)])."""
    if not isinstance(bundle, dict) or bundle.get('format') != FORMAT:
        raise ApiError(f"Not a QueryAPIGate collection bundle (expected format '{FORMAT}')", code='invalid_bundle')
    if bundle.get('format_version') != FORMAT_VERSION:
        raise ApiError(f"Unsupported bundle format_version {bundle.get('format_version')!r} "
                       f'(this version reads {FORMAT_VERSION})', code='invalid_bundle')
    collection = collection_override or bundle.get('collection')
    store.validate_collection_name(collection)
    entries = bundle.get('queries')
    if not isinstance(entries, list) or not entries:
        raise ApiError('The bundle has no queries', code='invalid_bundle')
    parsed, seen = [], set()
    for position, entry in enumerate(entries, 1):
        if not isinstance(entry, dict) or not isinstance(entry.get('name'), str):
            raise ApiError(f'Query #{position} has no name', code='invalid_bundle')
        name = entry['name']
        try:
            extra = sorted(set(entry) - ENTRY_FIELDS)
            if extra:
                raise ApiError(f"unexpected field(s): {', '.join(extra)}", code='invalid_bundle')
            store.saved_path_for_name(name)
            if name in seen:
                raise ApiError('listed more than once in the bundle', code='invalid_bundle')
            fields, _ = definitions.validate_definition({**{k: v for k, v in entry.items() if k != 'name'},
                                                         'filename': name})
        except ApiError as error:
            raise ApiError(f"Query '{name}': {error.message}", error.status, code=error.code) from None
        seen.add(name)
        parsed.append((name, fields))
    return collection, parsed


def _existing_collection(name):
    """(exists, its collection) for a saved query."""
    try:
        path = store.resolve_saved_file(name)
    except ApiError:
        return False, None
    return True, store.read_collection(store.load_versions(path))


def import_bundle(bundle, on_conflict='fail', collection_override=None, dry_run=False, example=False, audit=True):
    """Import ``bundle``; returns ``{'collection', 'created', 'new_versions', 'skipped', 'missing_connections'}``
    (names / connection names). Nothing is written if anything is wrong or, with ``on_conflict='fail'``,
    anything already exists. ``new-version`` never moves a query out of a different collection - that would
    silently change which keys can reach it - so that is a conflict too."""
    if on_conflict not in POLICIES:
        raise ApiError(f"--on-conflict must be one of: {', '.join(POLICIES)}")
    collection, parsed = _parse(bundle, collection_override)
    created, new_versions, skipped, blocked = [], [], [], []
    for name, fields in parsed:
        exists, current = _existing_collection(name)
        if not exists:
            created.append((name, fields))
        elif on_conflict == 'skip':
            skipped.append(name)
        elif on_conflict == 'new-version' and current in (None, collection):
            new_versions.append((name, fields))
        elif on_conflict == 'new-version':
            blocked.append(f"{name} (in collection '{current}')")
        else:
            blocked.append(name)
    if blocked:
        hint = ('use --on-conflict skip to leave existing queries alone, or new-version to add a version'
                if on_conflict == 'fail' else 'move it out of that collection first, or use --on-conflict skip')
        raise ApiError(f"Nothing imported. Already exists: {', '.join(blocked)} - {hint}", 409)
    known = store.read_connections()
    missing = sorted({f['connection_name'] for _, f in created + new_versions if f.get('connection_name')
                      and f['connection_name'] not in known})
    if not dry_run:
        for name, fields in created + new_versions:
            _, version = store.save_version(name, fields, collection, example=example)
            if audit:  # examples.load() records one summary entry instead of one per query
                store.record_audit('cli', 'save_query', name,
                                   {'version': version, 'connection_name': fields.get('connection_name'),
                                    'author': fields['author'], 'collection': collection, 'source': 'import'})
    return {'collection': collection, 'created': [n for n, _ in created],
            'new_versions': [n for n, _ in new_versions], 'skipped': skipped, 'missing_connections': missing}


def summary_lines(result, dry_run):
    verb = 'Would import' if dry_run else 'Imported'
    lines = [f"{verb} into collection '{result['collection']}': {len(result['created'])} new, "
             f"{len(result['new_versions'])} new version(s), {len(result['skipped'])} skipped"]
    if result['skipped']:
        lines.append('  skipped (already exist): ' + ', '.join(result['skipped']))
    if result['missing_connections']:
        lines.append('  warning - connections not defined here (queries referencing them will fail until they '
                     'are): ' + ', '.join(result['missing_connections']))
    return lines



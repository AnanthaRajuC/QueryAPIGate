"""Saved queries as the Management API presents them (/api/v1/queries, BACKLOG #72).

The store keeps a saved query in the shape its JSON files used to have (store.load_versions()); this module turns
that into the v1 resource shapes - a query, its versions and their parameter rules - and implements the v1
operations on top of the store: create a query, add a draft version, publish, unpublish, delete, and validate a
definition without saving it. Every change is audited the same way the legacy routes audit theirs.

v1 names fields for what they are, not for the file-based store they came from: `sql` (not `sql_query`),
`parameters` (not `query_parameters`), and no `filename` - `name` is the identity.
"""
import hashlib
import json

from .. import definitions, mongotools, sqlflow, sqltools, store
from .. import params as param_rules
from ..errors import ApiError

ENDPOINT_PREFIX = '/q/'
_MONGO_FIELDS = ('mongo_collection', 'mongo_filter', 'mongo_projection', 'mongo_sort')
_OPTIONAL_RULES = ('enum', 'min', 'max', 'min_length', 'max_length', 'pattern', 'description', 'from_claim')


# --------------------------------------------------------------------------------------
# Shapes
# --------------------------------------------------------------------------------------

def parameter_rule(spec):
    """A stored parameter definition (a type name or an object of rules, as saved) as the v1 rule object: `type`
    and `required` always, every other rule only when set. Type names are the JSON ones (integer, number, string,
    boolean), which v1 also accepts on input."""
    rule = param_rules.read_definition(spec)
    out = {'type': rule['type'], 'required': rule['required']}
    if rule['has_default']:
        out['default'] = rule['default']
    for key in _OPTIONAL_RULES:
        if rule[key] is not None:
            out[key] = rule[key]
    return out


def _placeholders(data):
    if data.get('query_type') == 'mongo':
        return list(mongotools.placeholder_names(data.get('mongo_filter') or {}))
    sql = data.get('sql_query')
    return list(sqltools.placeholder_names(sql)) if isinstance(sql, str) else []


def version_status(content, number):
    published = store.read_published(content)
    if published == number:
        return 'published'
    return 'draft' if store.is_draft(content, number) else 'previous'


def to_version(content, number, data):
    query_type = 'mongo' if data.get('query_type') == 'mongo' else 'sql'
    stored = data.get('query_parameters')
    return {
        'version': number,
        'status': version_status(content, number),
        'uuid': data.get('uuid'),
        'author': data.get('author'),
        'description': data.get('description') or '',
        'tags': _tags(data.get('tags')),
        'query_type': query_type,
        'sql': data.get('sql_query') if query_type == 'sql' else None,
        'mongo': {k: data[k] for k in _MONGO_FIELDS if data.get(k) is not None} if query_type == 'mongo' else None,
        'connection_name': data.get('connection_name'),
        'parameters': {name: parameter_rule(spec) for name, spec in stored.items()} if isinstance(stored, dict)
        else {},
        'placeholders': _placeholders(data),
        'cache_ttl': data.get('cache_ttl') or None,
        'created_at': data.get('created_at'),
        'last_modified_at': data.get('last_modified_at'),
    }


def _tags(value):
    if isinstance(value, str):
        return [t.strip() for t in value.split(',') if t.strip()]
    return [t for t in value if isinstance(t, str)] if isinstance(value, list) else []


def _versions(content):
    return sorted((int(k), v) for k, v in content.items() if k.isdigit() and isinstance(v, dict))


def to_summary(name, content):
    versions = _versions(content)
    latest_number, latest = versions[-1]
    published = store.read_published(content)
    shown = dict(versions).get(published, latest)  # what callers get, or the newest draft when nothing is live
    return {
        'name': name,
        'description': shown.get('description') or '',
        'query_type': 'mongo' if shown.get('query_type') == 'mongo' else 'sql',
        'connection_name': shown.get('connection_name'),
        'collection': store.read_collection(content),
        'tags': _tags(shown.get('tags')),
        'published_version': published,
        'latest_version': latest_number,
        'has_draft': latest_number > (published or 0),
        'version_count': len(versions),
        'example': store.read_example(content),
        'endpoint': ENDPOINT_PREFIX + name,
        'updated_at': max((v.get('last_modified_at') or '' for _, v in versions), default=None) or None,
    }


def to_detail(name, content):
    return {**to_summary(name, content),
            'versions': [to_version(content, number, data) for number, data in _versions(content)]}


def etag(content):
    """A validator for a query's whole state - which version is published, its collection, and every version's
    identity and last change. Any edit through any route changes it, so If-Match catches edits made elsewhere."""
    state = {'published': store.read_published(content), 'collection': store.read_collection(content),
             'versions': [(n, v.get('uuid'), v.get('last_modified_at'), v.get('cache_ttl')) for n, v in
                          _versions(content)]}
    return '"' + hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()[:32] + '"'


# --------------------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------------------

def load(name):
    """(canonical name, content) for an existing query, or a 404 with code query_not_found."""
    if not isinstance(name, str) or not store.saved_query_exists(name):
        raise ApiError(f"Saved query '{name}' not found", 404, code='query_not_found')
    return name, store.load_versions(name, with_history=False)


def list_queries(collection=None, connection=None, search=None, status=None):
    """Every query's summary, by name, filtered. ``status``: published (has a published version), unpublished
    (has none) or draft (has a version newer than the published one)."""
    if status not in (None, '', 'published', 'unpublished', 'draft'):
        raise ApiError("status must be 'published', 'unpublished' or 'draft'", code='invalid_filter')
    needle = (search or '').strip().lower()
    found = []
    for name, content in store.iter_saved():
        try:
            summary = to_summary(name, content)
        except (ValueError, IndexError):  # a query with no readable version
            continue
        if collection and summary['collection'] != collection:
            continue
        if connection and summary['connection_name'] != connection:
            continue
        if needle and needle not in name.lower() and needle not in summary['description'].lower() \
                and not any(needle in t.lower() for t in summary['tags']):
            continue
        if status == 'published' and summary['published_version'] is None:
            continue
        if status == 'unpublished' and summary['published_version'] is not None:
            continue
        if status == 'draft' and not summary['has_draft']:
            continue
        found.append(summary)
    return sorted(found, key=lambda s: s['name'])


# --------------------------------------------------------------------------------------
# Writes
# --------------------------------------------------------------------------------------

def _legacy_body(data, name, caller):
    """A v1 version body as the shape definitions.validate_definition() checks - the one set of saving rules
    shared with the legacy route and collection import."""
    if not isinstance(data, dict):
        raise ApiError('The request body must be a JSON object', code='invalid_body')
    unknown = sorted(set(data) - {'name', 'description', 'author', 'tags', 'query_type', 'sql', 'connection_name',
                                  'parameters', 'cache_ttl', 'collection', 'publish', *_MONGO_FIELDS})
    if unknown:
        raise ApiError(f"Unknown field(s): {', '.join(unknown)}", code='unknown_field')
    # caller_key_name() is '-' on a server with no keys at all - which is the admin acting, so name them that way
    body = {'filename': name, 'author': data.get('author') or (caller if caller not in (None, '', '-') else 'admin'),
            'description': data.get('description'), 'tags': data.get('tags', []),
            'query_type': data.get('query_type', 'sql'), 'query_parameters': data.get('parameters') or {},
            'connection_name': data.get('connection_name'), 'cache_ttl': data.get('cache_ttl')}
    if body['query_type'] == 'sql':
        body['sql_query'] = data.get('sql')
    for key in _MONGO_FIELDS:
        if key in data:
            body[key] = data[key]
    if 'collection' in data:
        body['collection'] = data['collection']
    return body


def _publish_flag(data):
    value = data.get('publish', False)
    if not isinstance(value, bool):
        raise ApiError('publish must be true or false', code='invalid_body')
    return value


def create(data, caller):
    """A new saved query with its first version - a draft unless ``publish`` is true. 409 if the name is taken."""
    name = data.get('name') if isinstance(data, dict) else None
    store.saved_path_for_name(name)
    if store.saved_query_exists(name):
        raise ApiError(f"A saved query named '{name}' already exists - add a version to it instead", 409,
                       code='query_exists')
    publish = _publish_flag(data)
    fields, collection = definitions.validate_definition(_legacy_body(data, name, caller))
    _, number = store.save_version(name, fields, collection, publish=publish)
    store.record_audit(caller, 'save_query', name, {'version': number, 'connection_name': fields.get('connection_name'),
                                                   'published': publish, 'via': 'api/v1'})
    return number


def add_version(name, data, caller):
    """The next version of an existing query - a draft unless ``publish`` is true. Its collection is the query's,
    changed through update() rather than here."""
    name, _ = load(name)
    if isinstance(data, dict) and ('name' in data or 'collection' in data):
        raise ApiError("A new version can't change the query's name or collection - PATCH the query for its "
                       'collection', code='invalid_body')
    publish = _publish_flag(data)
    fields, _ = definitions.validate_definition(_legacy_body(data, name, caller))
    _, number = store.save_version(name, fields, publish=publish)
    store.record_audit(caller, 'save_query', name, {'version': number, 'connection_name': fields.get('connection_name'),
                                                   'published': publish, 'via': 'api/v1'})
    return number


def publish(name, version, caller):
    name, _ = load(name)
    if not isinstance(version, int) or isinstance(version, bool):
        raise ApiError('version must be an integer', code='invalid_body')
    previous = store.publish_version(name, version)
    if previous != version:
        store.record_audit(caller, 'publish_query', name, {'version': {'from': previous, 'to': version}})


def unpublish(name, caller):
    name, _ = load(name)
    previous = store.unpublish(name)
    if previous is not None:
        store.record_audit(caller, 'unpublish_query', name, {'version': previous})


def validate(data, caller):
    """Check a definition without saving it - the editor's Validate button. Never raises for a problem with the
    definition itself: every problem found is returned, alongside what the SQL uses (placeholders, tables)."""
    errors = []
    sql = data.get('sql') if isinstance(data, dict) else None
    try:
        definitions.validate_definition(_legacy_body(data, 'draft', caller))
    except ApiError as error:
        errors.append({'message': error.message, **({'parameters': error.extra['errors']}
                                                     if isinstance(error.extra.get('errors'), dict) else {})})
    dialect = None
    connection_name = data.get('connection_name') if isinstance(data, dict) else None
    if isinstance(connection_name, str) and connection_name:
        try:
            dialect = store.get_connection(connection_name)['db']
        except ApiError:
            errors.append({'message': f"Connection '{connection_name}' not found", 'field': 'connection_name'})
    tables = []
    if isinstance(sql, str) and sql.strip():
        try:
            sqltools.validate_sql(sql, dialect=dialect)
        except ApiError as error:
            errors.append({'message': error.message, 'field': 'sql'})
        flow = sqlflow.extract_flow(sql, dialect)
        tables = flow.get('tables') or []
    return {'valid': not errors, 'errors': errors,
            'placeholders': list(sqltools.placeholder_names(sql)) if isinstance(sql, str) else [],
            'tables': tables}

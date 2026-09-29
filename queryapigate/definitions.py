"""Validation of a saved-query definition - shared by everything that creates one (``PATCH /save_sql_to_file``
and ``queryapigate collection import``), so a query is held to the same rules however it arrives."""
from . import mongotools, sqltools, store
from . import params as param_rules
from .errors import ApiError

NO_COLLECTION_GIVEN = store._UNSET  # the request said nothing about a collection: leave the query's as it is


def as_object(value, label):
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ApiError(f'{label} must be a JSON object')
    return value


def validate_cache_ttl(cache_ttl):
    """The one rule a cache_ttl must follow, however it arrives - saving a new version here, or editing an
    existing version's in place via PUT /saved_sql/<name>/cache_ttl (app.py)."""
    if cache_ttl is not None and (not isinstance(cache_ttl, int) or isinstance(cache_ttl, bool) or cache_ttl < 0):
        raise ApiError('cache_ttl must be a non-negative integer number of seconds')


def validate_definition(data):
    """Check ``data`` (a decoded request body or bundle entry) and return ``(fields, collection)``: the fields
    to store on the new version, and the collection to file the query under - ``NO_COLLECTION_GIVEN`` when
    ``data`` has no ``collection`` key, None to remove it from any, or a valid collection name.

    ``query_type`` (default ``'sql'``) picks which shape the query itself takes: ``sql_query`` (a SQL
    string) or, for a Mongo find query, ``mongo_collection`` + ``mongo_filter`` (a JSON filter document,
    optionally ``mongo_projection``/``mongo_sort``) - see mongotools.py and BACKLOG #36. Everything else
    (author/description/tags/query_parameters/connection_name/cache_ttl/collection) is shared between the
    two, unchanged either way.
    """
    query_type = data.get('query_type', 'sql')
    if query_type not in ('sql', 'mongo'):
        raise ApiError("query_type must be 'sql' or 'mongo'")
    required = [('author', 'Author'), ('description', 'Description'), ('filename', 'Filename')]
    required.append(('sql_query', 'SQL query') if query_type == 'sql' else ('mongo_collection', 'Mongo collection'))
    for field, label in required:
        if not data.get(field) or not isinstance(data[field], str):
            raise ApiError(f'{label} is missing')
    tags = data.get('tags', [])
    if not isinstance(tags, (list, str)):
        raise ApiError('tags must be a string or a list')
    query_parameters = as_object(data.get('query_parameters'), 'query_parameters')
    param_rules.parse_definitions(query_parameters)
    if query_type == 'mongo':
        mongo_filter = mongotools.validate_filter(as_object(data.get('mongo_filter'), 'mongo_filter'))
        used = set(mongotools.placeholder_names(mongo_filter))
    else:
        used = set(sqltools.placeholder_names(data['sql_query']))
    unused = sorted(set(query_parameters) - used)
    if unused:
        where = 'the filter' if query_type == 'mongo' else 'sql_query'
        raise ApiError(f"query_parameters declares {', '.join(unused)}, which {where} does not use "
                       '(write :name in it, or remove the declaration)')
    connection_name = data.get('connection_name')
    if connection_name is not None and not isinstance(connection_name, str):
        raise ApiError('connection_name must be a string')
    cache_ttl = data.get('cache_ttl')
    validate_cache_ttl(cache_ttl)
    collection = NO_COLLECTION_GIVEN
    if 'collection' in data:
        collection = data['collection']
        if collection is not None:
            store.validate_collection_name(collection)

    fields = {
        'author': data['author'],
        'description': data['description'],
        'tags': tags,
        'query_parameters': query_parameters,
        **({'connection_name': connection_name} if connection_name else {}),
        **({'cache_ttl': cache_ttl} if cache_ttl else {}),
    }
    if query_type == 'mongo':
        # No 'query_type' field at all for a plain SQL query (query_type's own default) - every saved query
        # ever written before this version is missing it too, and every check of it elsewhere
        # (saved.get('query_type') == 'mongo') already treats that absence as "sql", so this keeps a SQL
        # query's stored shape byte-for-byte unchanged, not just behaviourally equivalent.
        fields['query_type'] = 'mongo'
        fields['mongo_collection'] = data['mongo_collection']
        fields['mongo_filter'] = mongo_filter
        for extra in ('mongo_projection', 'mongo_sort'):
            if data.get(extra) is not None:
                fields[extra] = as_object(data.get(extra), extra)
    else:
        fields['sql_query'] = data['sql_query']
    return fields, collection


def effective_parameters(saved):
    """A saved version's parameters as callers must supply them: every placeholder its query actually uses, in
    the order it uses them, with the declared rules (an undeclared one is plain required text). The one place
    this is derived - the OpenAPI document, the catalogue and the Postman export all describe a query's
    parameters, and must not each work them out separately."""
    declared = param_rules.read_definitions(saved.get('query_parameters'))
    if saved.get('query_type') == 'mongo':
        names = mongotools.placeholder_names(saved.get('mongo_filter') or {})
    else:
        names = sqltools.placeholder_names(saved['sql_query'])
    return {name: declared.get(name) or param_rules.read_definition({}) for name in names}

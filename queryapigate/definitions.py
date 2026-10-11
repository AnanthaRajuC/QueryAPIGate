"""Validation of a saved-query definition - shared by everything that creates one (``POST /api/v1/queries``
and ``queryapigate collection import``), so a query is held to the same rules however it arrives."""
from . import mongotools, sqltools, store
from . import params as param_rules
from .errors import ApiError

NO_COLLECTION_GIVEN = store._UNSET  # the request said nothing about a collection: leave the query's as it is


def as_object(value, label):
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ApiError(f'{label} must be a JSON object', code='invalid_body')
    return value


def validate_cache_ttl(cache_ttl):
    """The one rule a cache_ttl must follow, however it arrives - saving a new version here, or editing an
    existing version's in place via PATCH /api/v1/queries/<name>/versions/<n> (v1.py)."""
    if cache_ttl is not None and (not isinstance(cache_ttl, int) or isinstance(cache_ttl, bool) or cache_ttl < 0):
        raise ApiError('cache_ttl must be a non-negative integer number of seconds', code='invalid_body')


MAX_COLUMN_DESCRIPTIONS = 500

# Write tools over MCP (ADR 0008): which saved queries an agent may write through, and on what.
AGENT_WRITE_MODES = ('approval', 'insert')    # every call approved by a person / inserts only, without one
AGENT_WRITE_STATEMENTS = ('insert', 'update', 'delete')
AGENT_WRITE_DIALECTS = ('postgres', 'mysql', 'sqlite', 'duckdb')  # a transaction, and the rows a statement changed
MAX_AGENT_AFFECTED_ROWS = 100_000


def validate_agent_writes(data, query_type):
    """`agent_writes` and `max_affected_rows` as stored, or {} - refused unless the query is one statement that only
    changes data (INSERT, UPDATE or DELETE; INSERT alone for "insert"), on a connection type that can run it in a
    transaction and say how many rows it changed. Never DDL, whatever a grant would allow over REST."""
    mode, ceiling = data.get('agent_writes'), data.get('max_affected_rows')
    if mode is None:
        if ceiling is not None:
            raise ApiError('max_affected_rows is for agent-writable queries - set agent_writes too',
                           code='invalid_body')
        return {}
    if mode not in AGENT_WRITE_MODES:
        raise ApiError(f"agent_writes must be one of: {', '.join(AGENT_WRITE_MODES)}", code='invalid_body')
    if query_type != 'sql':
        raise ApiError('Only SQL saved queries can be agent-writable', code='invalid_body')
    sql = data['sql_query'].strip().rstrip(';').strip()
    if ';' in sqltools._literals_re(None).sub(' ', sql):
        raise ApiError('An agent-writable query is a single statement', code='invalid_body')
    keyword = sqltools.first_keyword(sql)
    allowed = ('insert',) if mode == 'insert' else AGENT_WRITE_STATEMENTS
    if keyword not in allowed or sqltools.write_operations(sql) != {keyword}:
        what = 'an INSERT' if mode == 'insert' else 'an INSERT, UPDATE or DELETE'
        raise ApiError(f'An agent-writable query{" in insert mode" if mode == "insert" else ""} must be {what} - one '
                       'statement that changes data and nothing else', code='invalid_body')
    if ceiling is None:
        ceiling = 1
    if not isinstance(ceiling, int) or isinstance(ceiling, bool) or not 1 <= ceiling <= MAX_AGENT_AFFECTED_ROWS:
        raise ApiError(f'max_affected_rows must be a whole number from 1 to {MAX_AGENT_AFFECTED_ROWS}',
                       code='invalid_body')
    connection = store.read_connections().get(data.get('connection_name') or '')
    if connection is not None and connection.get('db') not in AGENT_WRITE_DIALECTS:
        raise ApiError(f"Agents can't write on '{connection.get('db')}' connections - only "
                       f"{', '.join(AGENT_WRITE_DIALECTS)}", code='invalid_body')
    return {'agent_writes': mode, 'max_affected_rows': ceiling}


def validate_column_descriptions(value):
    """What a saved query's result columns mean (BACKLOG #86), {"column": "description"} - served to agents in its MCP
    tool's output schema."""
    value = as_object(value, 'columns')
    if len(value) > MAX_COLUMN_DESCRIPTIONS:
        raise ApiError(f'At most {MAX_COLUMN_DESCRIPTIONS} column descriptions', code='invalid_body')
    out = {}
    for column, text in value.items():
        if not column or len(column) > 128 or not isinstance(text, str) or len(text) > 2000:
            raise ApiError('columns maps each result column (up to 128 characters) to its description (text, up to '
                           '2000 characters)', code='invalid_body')
        if text.strip():
            out[column] = text.strip()
    return out


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
        raise ApiError("query_type must be 'sql' or 'mongo'", code='invalid_body')
    required = [('author', 'Author'), ('description', 'Description'), ('filename', 'Filename')]
    required.append(('sql_query', 'SQL query') if query_type == 'sql' else ('mongo_collection', 'Mongo collection'))
    for field, label in required:
        if not data.get(field) or not isinstance(data[field], str):
            raise ApiError(f'{label} is missing', code='invalid_body')
    tags = data.get('tags', [])
    if not isinstance(tags, (list, str)):
        raise ApiError('tags must be a string or a list', code='invalid_body')
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
                       '(write :name in it, or remove the declaration)', code='invalid_body')
    connection_name = data.get('connection_name')
    if connection_name is not None and not isinstance(connection_name, str):
        raise ApiError('connection_name must be a string', code='invalid_body')
    cache_ttl = data.get('cache_ttl')
    validate_cache_ttl(cache_ttl)
    column_descriptions = validate_column_descriptions(data.get('column_descriptions'))
    agent_writes = validate_agent_writes(data, query_type)
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
        **({'column_descriptions': column_descriptions} if column_descriptions else {}),
        **agent_writes,
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
    # A `from_claim` parameter is filled from the caller's sign-in token (app.bind_claims()), never supplied by
    # the caller - so it is no part of what they are told to send.
    return {name: declared.get(name) or param_rules.read_definition({}) for name in names
            if not (declared.get(name) or {}).get('from_claim')}

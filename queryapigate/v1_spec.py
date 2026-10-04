"""The OpenAPI description of the Management API (/api/v1, v1.py) - merged into /openapi.json by openapi.build_spec().

Unlike the legacy routes' entries, every v1 operation describes its responses in full: the Console's TypeScript
types are generated from this (ADR 0001), and tests/test_api_v1.py validates real responses against it.
"""

TAG = 'Management API v1'


def _ref(name):
    return {'$ref': f'#/components/schemas/{name}'}


def _json(schema):
    return {'content': {'application/json': {'schema': schema}}}


def _ok(schema, description='OK', etag=False):
    response = {'description': description, **_json(schema)}
    if etag:
        response['headers'] = {'ETag': {'description': 'Send back as If-Match to make a change conditional.',
                                        'schema': {'type': 'string'}}}
    return response


_ERRORS = {code: {'description': text, **_json(_ref('V1Error'))} for code, text in (
    ('400', 'Invalid request'), ('401', 'Missing or invalid credentials'), ('403', 'Not the admin key'),
    ('404', 'Not found'))}
_CONFLICT = {'409': {'description': 'Already exists', **_json(_ref('V1Error'))}}
_PRECONDITION = {'412': {'description': 'Changed since read (If-Match)', **_json(_ref('V1Error'))}}
_IF_MATCH = {'name': 'If-Match', 'in': 'header', 'required': False, 'schema': {'type': 'string'},
             'description': "The query's ETag from a previous read; the change is refused with 412 if it changed."}
_NAME = {'name': 'name', 'in': 'path', 'required': True, 'schema': {'type': 'string'}}
_VERSION = {'name': 'version', 'in': 'path', 'required': True, 'schema': {'type': 'integer'}}

_NULLABLE_STRING = {'type': 'string', 'nullable': True}

SCHEMAS = {
    'V1Error': {
        'type': 'object', 'required': ['error', 'code', 'request_id'],
        'properties': {
            'error': {'type': 'string', 'description': 'Human-readable; wording may change between releases.'},
            'code': {'type': 'string', 'description': 'Stable and machine-readable, e.g. query_not_found, '
                                                      'query_exists, precondition_failed, unknown_field.'},
            'request_id': {'type': 'string', 'description': 'Matches the X-Request-Id header and the server log.'},
            'errors': {'type': 'object', 'additionalProperties': {'type': 'string'},
                       'description': 'Per-parameter problems, when the parameter rules were invalid.'},
            'detail': {'type': 'string', 'description': "The database driver's own message, where there is one."},
        },
    },
    'ParameterRule': {
        'type': 'object', 'required': ['type', 'required'],
        'properties': {
            'type': {'type': 'string', 'nullable': True, 'enum': ['integer', 'number', 'string', 'boolean', None]},
            'required': {'type': 'boolean'},
            'default': {'description': 'Present only when the parameter has a default.'},
            'enum': {'type': 'array', 'items': {}},
            'min': {'type': 'number'}, 'max': {'type': 'number'},
            'min_length': {'type': 'integer'}, 'max_length': {'type': 'integer'},
            'pattern': {'type': 'string'},
            'description': {'type': 'string'},
            'from_claim': {'type': 'string', 'description': "Bound from the signed-in caller's token claim."},
        },
    },
    'QueryVersion': {
        'type': 'object',
        'required': ['version', 'status', 'uuid', 'author', 'description', 'tags', 'query_type', 'sql', 'mongo',
                     'connection_name', 'parameters', 'placeholders', 'cache_ttl', 'created_at', 'last_modified_at'],
        'properties': {
            'version': {'type': 'integer'},
            'status': {'type': 'string', 'enum': ['published', 'draft', 'previous'],
                       'description': 'published: served now; draft: never served (newer than the published '
                                      'version, or nothing is published); previous: served once, still runnable '
                                      'with ?version='},
            'uuid': _NULLABLE_STRING,
            'author': _NULLABLE_STRING,
            'description': {'type': 'string'},
            'tags': {'type': 'array', 'items': {'type': 'string'}},
            'query_type': {'type': 'string', 'enum': ['sql', 'mongo']},
            'sql': _NULLABLE_STRING,
            'mongo': {'type': 'object', 'nullable': True, 'additionalProperties': True},
            'connection_name': _NULLABLE_STRING,
            'parameters': {'type': 'object', 'additionalProperties': _ref('ParameterRule')},
            'placeholders': {'type': 'array', 'items': {'type': 'string'},
                             'description': 'Every parameter the query uses, in order of first use.'},
            'cache_ttl': {'type': 'integer', 'nullable': True},
            'created_at': _NULLABLE_STRING,
            'last_modified_at': _NULLABLE_STRING,
        },
    },
    'QuerySummary': {
        'type': 'object',
        'required': ['name', 'description', 'query_type', 'connection_name', 'collection', 'tags',
                     'published_version', 'latest_version', 'has_draft', 'version_count', 'example', 'endpoint',
                     'updated_at'],
        'properties': {
            'name': {'type': 'string'},
            'description': {'type': 'string', 'description': "The published version's, or the newest draft's."},
            'query_type': {'type': 'string', 'enum': ['sql', 'mongo']},
            'connection_name': _NULLABLE_STRING,
            'collection': _NULLABLE_STRING,
            'tags': {'type': 'array', 'items': {'type': 'string'}},
            'published_version': {'type': 'integer', 'nullable': True},
            'latest_version': {'type': 'integer'},
            'has_draft': {'type': 'boolean'},
            'version_count': {'type': 'integer'},
            'example': {'type': 'boolean'},
            'endpoint': {'type': 'string', 'description': 'Where the published version is served, e.g. /q/name.'},
            'updated_at': _NULLABLE_STRING,
        },
    },
    'Query': {
        'allOf': [_ref('QuerySummary'), {
            'type': 'object', 'required': ['versions'],
            'properties': {'versions': {'type': 'array', 'items': _ref('QueryVersion')}},
        }],
    },
    'QueryList': {
        'type': 'object', 'required': ['items'],
        'properties': {'items': {'type': 'array', 'items': _ref('QuerySummary')}},
    },
    'QueryVersionInput': {
        'type': 'object', 'required': ['description'],
        'properties': {
            'description': {'type': 'string'},
            'author': {'type': 'string', 'description': "Defaults to the calling key's name."},
            'tags': {'type': 'array', 'items': {'type': 'string'}},
            'query_type': {'type': 'string', 'enum': ['sql', 'mongo'], 'default': 'sql'},
            'sql': {'type': 'string', 'description': "Required for query_type 'sql'."},
            'connection_name': {'type': 'string'},
            'parameters': {'type': 'object', 'additionalProperties': _ref('ParameterRule')},
            'cache_ttl': {'type': 'integer', 'nullable': True},
            'mongo_collection': {'type': 'string'}, 'mongo_filter': {'type': 'object'},
            'mongo_projection': {'type': 'object'}, 'mongo_sort': {'type': 'object'},
            'publish': {'type': 'boolean', 'default': False,
                        'description': 'Publish the new version at once instead of keeping it as a draft.'},
        },
    },
    'QueryCreateInput': {
        'allOf': [_ref('QueryVersionInput'), {
            'type': 'object', 'required': ['name'],
            'properties': {'name': {'type': 'string'}, 'collection': _NULLABLE_STRING},
        }],
    },
    'ValidationResult': {
        'type': 'object', 'required': ['valid', 'errors', 'placeholders', 'tables'],
        'properties': {
            'valid': {'type': 'boolean'},
            'errors': {'type': 'array', 'items': {
                'type': 'object', 'required': ['message'],
                'properties': {'message': {'type': 'string'}, 'field': {'type': 'string'},
                               'parameters': {'type': 'object', 'additionalProperties': {'type': 'string'}}}}},
            'placeholders': {'type': 'array', 'items': {'type': 'string'}},
            'tables': {'type': 'array', 'items': {'type': 'string'}},
        },
    },
    'HistoryEntry': {
        'type': 'object', 'required': ['query', 'version', 'executed_at'],
        'additionalProperties': True,
        'properties': {
            'query': {'type': 'string'}, 'version': {'type': 'integer'}, 'executed_at': {'type': 'string'},
            'status': {'type': 'string', 'enum': ['success', 'error']},
            'connection_name': {'type': 'string'}, 'key_name': {'type': 'string', 'nullable': True},
            'request_id': {'type': 'string', 'nullable': True}, 'rows': {'type': 'integer', 'nullable': True},
            'duration_ms': {'type': 'number', 'nullable': True}, 'error': {'type': 'string', 'nullable': True},
        },
    },
    'HistoryPage': {
        'type': 'object', 'required': ['items', 'next_cursor'],
        'properties': {'items': {'type': 'array', 'items': _ref('HistoryEntry')},
                       'next_cursor': {'type': 'string', 'nullable': True,
                                       'description': 'Pass as ?cursor= for the next page; null on the last.'}},
    },
    'Connection': {
        'type': 'object',
        'required': ['name', 'db', 'active', 'host', 'port', 'database', 'user', 'example', 'created_at', 'updated_at',
                     'usage'],
        'properties': {
            'name': {'type': 'string'}, 'db': {'type': 'string'}, 'active': {'type': 'boolean'},
            'host': _NULLABLE_STRING,
            'port': {'oneOf': [{'type': 'integer'}, {'type': 'string'}], 'nullable': True},
            'database': {'type': 'string', 'nullable': True,
                         'description': 'The database name, or the file path for SQLite/DuckDB/H2.'},
            'user': _NULLABLE_STRING,
            'example': {'type': 'boolean', 'description': 'Installed by the example APIs.'},
            'created_at': _NULLABLE_STRING, 'updated_at': _NULLABLE_STRING,
            'usage': {'type': 'object', 'required': ['queries', 'errors', 'rows', 'avg_duration_ms'],
                      'description': 'Since this process started.',
                      'properties': {'queries': {'type': 'integer'}, 'errors': {'type': 'integer'},
                                     'rows': {'type': 'integer'},
                                     'avg_duration_ms': {'type': 'number', 'nullable': True}}},
        },
    },
    'ConnectionDetail': {
        'allOf': [_ref('Connection'), {
            'type': 'object', 'required': ['password', 'options'],
            'properties': {
                'password': {'type': 'string', 'nullable': True,
                             'description': "Masked as ******** unless it is a ${ENV_VAR} reference; send the mask "
                                            'back (or leave password out) to keep it.'},
                'options': {'type': 'object', 'additionalProperties': True,
                            'description': 'Driver options passed through to the connection (sslmode, jdbc_url, ...).'},
            }}],
    },
    'ConnectionInput': {
        'type': 'object',
        'properties': {
            'name': {'type': 'string', 'description': 'Create only.'},
            'db': {'type': 'string', 'description': 'Required on create.'},
            'active': {'type': 'boolean', 'default': True},
            'host': _NULLABLE_STRING,
            'port': {'oneOf': [{'type': 'integer'}, {'type': 'string'}], 'nullable': True},
            'user': _NULLABLE_STRING,
            'password': {'type': 'string', 'nullable': True},
            'database': _NULLABLE_STRING,
            'options': {'type': 'object', 'additionalProperties': True},
        },
        'additionalProperties': True,
    },
    'ConnectionList': {
        'type': 'object', 'required': ['items'],
        'properties': {'items': {'type': 'array', 'items': _ref('Connection')}},
    },
    'DeletedConnection': {
        'type': 'object', 'required': ['name', 'db', 'host', 'port', 'database', 'deleted_at', 'deleted_by', 'reason'],
        'properties': {
            'name': {'type': 'string'}, 'db': _NULLABLE_STRING, 'host': _NULLABLE_STRING,
            'port': {'oneOf': [{'type': 'integer'}, {'type': 'string'}], 'nullable': True},
            'database': _NULLABLE_STRING, 'deleted_at': _NULLABLE_STRING, 'deleted_by': _NULLABLE_STRING,
            'reason': _NULLABLE_STRING,
        },
    },
    'DeletedConnectionList': {
        'type': 'object', 'required': ['items'],
        'properties': {'items': {'type': 'array', 'items': _ref('DeletedConnection')}},
    },
    'ConnectionTest': {
        'type': 'object', 'required': ['elapsed_ms'], 'properties': {'elapsed_ms': {'type': 'number'}},
    },
    'DatabaseList': {
        'type': 'object', 'required': ['databases'],
        'properties': {'databases': {'type': 'array', 'items': {'type': 'string'}}},
    },
    'Schema': {
        'type': 'object', 'required': ['tables', 'truncated'],
        'properties': {
            'truncated': {'type': 'boolean'},
            'tables': {'type': 'array', 'items': {
                'type': 'object', 'required': ['name', 'type', 'columns'],
                'properties': {
                    'name': {'type': 'string'}, 'schema': {'type': 'string', 'nullable': True},
                    'type': {'type': 'string'},
                    'columns': {'type': 'array', 'items': {
                        'type': 'object', 'required': ['name', 'type'],
                        'properties': {'name': {'type': 'string'}, 'type': {'type': 'string', 'nullable': True},
                                       'nullable': {'type': 'boolean'}, 'position': {'type': 'integer'},
                                       'primary_key': {'type': 'boolean'},
                                       'foreign_key': {'type': 'object', 'nullable': True,
                                                       'additionalProperties': True}}}},
                }}},
        },
    },
}


def _op(summary, responses, parameters=(), body=None, description=None):
    op = {'summary': summary, 'tags': [TAG], 'responses': {**responses, **_ERRORS}}
    if parameters:
        op['parameters'] = list(parameters)
    if body is not None:
        op['requestBody'] = {'required': True, **_json(body)}
    if description:
        op['description'] = description
    return op


_QUERY = _ok(_ref('Query'), etag=True)
_CONNECTION = _ok(_ref('ConnectionDetail'), etag=True)
_UNREACHABLE = {'502': {'description': "Couldn't reach the database (code connection_failed)",
                        **_json(_ref('V1Error'))}}

PATHS = {
    '/api/v1/queries': {
        'get': _op('List saved queries', {'200': _ok(_ref('QueryList'))}, parameters=[
            {'name': 'search', 'in': 'query', 'schema': {'type': 'string'},
             'description': 'Matches name, description or a tag.'},
            {'name': 'collection', 'in': 'query', 'schema': {'type': 'string'}},
            {'name': 'connection', 'in': 'query', 'schema': {'type': 'string'}},
            {'name': 'status', 'in': 'query', 'schema': {'type': 'string', 'enum': ['published', 'unpublished',
                                                                                     'draft']}}]),
        'post': _op('Create a saved query (its first version is a draft unless publish is true)',
                    {'201': _QUERY, **_CONFLICT}, body=_ref('QueryCreateInput')),
    },
    '/api/v1/queries/validate': {
        'post': _op('Check a definition without saving it', {'200': _ok(_ref('ValidationResult'))},
                    body=_ref('QueryVersionInput')),
    },
    '/api/v1/queries/{name}': {
        'get': _op('Get a saved query with all its versions', {'200': _QUERY}, parameters=[_NAME]),
        'patch': _op("Change a query's collection", {'200': _QUERY, **_PRECONDITION}, parameters=[_NAME, _IF_MATCH],
                     body={'type': 'object', 'properties': {'collection': _NULLABLE_STRING}}),
        'delete': _op('Delete a saved query and every version', {'204': {'description': 'Deleted'}, **_PRECONDITION},
                      parameters=[_NAME, _IF_MATCH]),
    },
    '/api/v1/queries/{name}/versions': {
        'post': _op('Add a version (a draft unless publish is true)', {'201': _QUERY, **_PRECONDITION},
                    parameters=[_NAME, _IF_MATCH], body=_ref('QueryVersionInput')),
    },
    '/api/v1/queries/{name}/versions/{version}': {
        'get': _op('Get one version', {'200': _ok(_ref('QueryVersion'))}, parameters=[_NAME, _VERSION]),
        'patch': _op("Change a version's cache_ttl", {'200': _QUERY, **_PRECONDITION},
                     parameters=[_NAME, _VERSION, _IF_MATCH],
                     body={'type': 'object', 'properties': {'cache_ttl': {'type': 'integer', 'nullable': True}}}),
        'delete': _op('Delete one version (publishing an older one if it was the published version)',
                      {'200': _QUERY, '204': {'description': 'Deleted the last version, and so the query'},
                       **_PRECONDITION}, parameters=[_NAME, _VERSION, _IF_MATCH]),
    },
    '/api/v1/queries/{name}/publish': {
        'post': _op('Publish a version - a draft, or an older version to roll back', {'200': _QUERY, **_PRECONDITION},
                    parameters=[_NAME, _IF_MATCH],
                    body={'type': 'object', 'required': ['version'], 'properties': {'version': {'type': 'integer'}}}),
    },
    '/api/v1/queries/{name}/unpublish': {
        'post': _op('Stop serving the query (every version is kept)', {'200': _QUERY, **_PRECONDITION},
                    parameters=[_NAME, _IF_MATCH]),
    },
    '/api/v1/queries/{name}/history': {
        'get': _op("This query's runs, newest first", {'200': _ok(_ref('HistoryPage'))}, parameters=[
            _NAME,
            {'name': 'version', 'in': 'query', 'schema': {'type': 'integer'}},
            {'name': 'status', 'in': 'query', 'schema': {'type': 'string', 'enum': ['success', 'error']}},
            {'name': 'key', 'in': 'query', 'schema': {'type': 'string'}},
            {'name': 'since', 'in': 'query', 'schema': {'type': 'string'}},
            {'name': 'until', 'in': 'query', 'schema': {'type': 'string'}},
            {'name': 'limit', 'in': 'query', 'schema': {'type': 'integer', 'default': 50}},
            {'name': 'cursor', 'in': 'query', 'schema': {'type': 'string'}}]),
    },
    '/api/v1/connections': {
        'get': _op('List connections (identity, status and live usage - never credentials)',
                   {'200': _ok(_ref('ConnectionList'))}),
        'post': _op('Create a connection', {'201': _CONNECTION, **_CONFLICT}, body=_ref('ConnectionInput')),
    },
    '/api/v1/connections/deleted': {
        'get': _op('Deleted connections, newest first, with who deleted them and why',
                   {'200': _ok(_ref('DeletedConnectionList'))}),
    },
    '/api/v1/connections/test': {
        'post': _op('Try to connect with the given fields, or a saved connection by name (nothing is saved)',
                    {'200': _ok(_ref('ConnectionTest')), **_UNREACHABLE}, body=_ref('ConnectionInput')),
    },
    '/api/v1/connections/databases': {
        'post': _op("The databases on the server the given fields (or a saved connection, by name) point at",
                    {'200': _ok(_ref('DatabaseList')), **_UNREACHABLE}, body=_ref('ConnectionInput')),
    },
    '/api/v1/connections/{name}': {
        'get': _op('Get a connection (password masked)', {'200': _CONNECTION}, parameters=[_NAME]),
        'patch': _op('Change some of its fields; the rest are kept', {'200': _CONNECTION, **_PRECONDITION},
                     parameters=[_NAME, _IF_MATCH], body=_ref('ConnectionInput')),
        'delete': _op('Delete it (a reason is required, kept in the audit log)',
                      {'204': {'description': 'Deleted'}, **_PRECONDITION}, parameters=[_NAME, _IF_MATCH],
                      body={'type': 'object', 'required': ['reason'], 'properties': {'reason': {'type': 'string'}}}),
    },
    '/api/v1/connections/{name}/schema': {
        'get': _op("A connection's tables and columns", {'200': _ok(_ref('Schema'))}, parameters=[
            _NAME, {'name': 'database', 'in': 'query', 'required': False, 'schema': {'type': 'string'},
                    'description': "Another database on the same server than the connection's own."}]),
    },
}

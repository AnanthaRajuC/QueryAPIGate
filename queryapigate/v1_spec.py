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
    ('400', 'Invalid request'), ('401', 'Missing or invalid credentials'),
    ('403', 'Not an administrator (admin_only), or not one whose role may (role_forbidden)'), ('404', 'Not found'))}
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
                                                      'param_invalid, table_not_allowed, rate_limited - every code '
                                                      'is listed in API.md under Errors.'},
            'request_id': {'type': 'string', 'description': 'Matches the X-Request-Id header and the server log.'},
            'errors': {'type': 'object', 'additionalProperties': {'type': 'string'},
                       'description': 'Per-parameter problems, when the parameter rules were invalid.'},
            'detail': {'type': 'string', 'description': "The database driver's own message, where there is one."},
            'retry_after': {'type': 'integer', 'description': 'Seconds to wait, when rate_limited.'},
            'timeout': {'type': 'number', 'description': 'The time limit that was exceeded, when query_timeout.'},
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
            'run_count': {'type': 'integer', 'description': "Runs of this version stored in history - in a "
                                                            "query's detail only."},
        },
    },
    'QuerySummary': {
        'type': 'object',
        'required': ['name', 'description', 'query_type', 'connection_name', 'collection', 'tags',
                     'published_version', 'latest_version', 'has_draft', 'version_count', 'example', 'endpoint',
                     'updated_at', 'created_at', 'last_used_at', 'cache_ttl'],
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
            'created_at': {'type': 'string', 'nullable': True, 'description': 'When its first version was saved.'},
            'last_used_at': {'type': 'string', 'nullable': True,
                             'description': 'Its newest stored run, of any version; null if it never ran.'},
            'cache_ttl': {'type': 'integer', 'nullable': True,
                          'description': "The published version's (or newest draft's) response cache TTL, "
                                         'in seconds.'},
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
        'type': 'object', 'required': ['query', 'version', 'kind', 'executed_at'],
        'additionalProperties': True,
        'properties': {
            'query': {'type': 'string', 'nullable': True, 'description': 'The saved query; null for ad-hoc SQL.'},
            'version': {'type': 'integer', 'nullable': True},
            'kind': {'type': 'string', 'enum': ['saved', 'adhoc'],
                     'description': 'A saved query run, or ad-hoc SQL (/execute_sql, /execute_mongo, MCP '
                                    'execute_sql).'},
            'transport': {'type': 'string', 'enum': ['rest', 'mcp'], 'description': 'The front door it came through.'},
            'sql': {'type': 'string', 'description': "Ad-hoc only, unless QUERYAPIGATE_HISTORY_ADHOC_SQL says 'hash' "
                                                     "or 'none'; at most 4000 characters (sql_truncated)."},
            'sql_sha256': {'type': 'string', 'description': "Ad-hoc only, with QUERYAPIGATE_HISTORY_ADHOC_SQL=hash."},
            'params': {'type': 'array', 'items': {'type': 'string'},
                       'description': "Ad-hoc only: the parameters' names - never their values."},
            'executed_at': {'type': 'string'},
            'status': {'type': 'string', 'enum': ['success', 'error']},
            'connection_name': {'type': 'string'}, 'key_name': {'type': 'string', 'nullable': True},
            'request_id': {'type': 'string', 'nullable': True}, 'rows': {'type': 'integer', 'nullable': True},
            'duration_ms': {'type': 'number', 'nullable': True}, 'error': {'type': 'string', 'nullable': True},
            'code': {'type': 'string', 'description': "A failed run's error code (API.md, Errors), from 0.13 on."},
        },
    },
    'Alert': {
        'type': 'object', 'required': ['id', 'severity', 'kind', 'title', 'detail', 'since', 'target'],
        'properties': {
            'id': {'type': 'string', 'description': 'Stable while the condition holds: kind and subject.'},
            'severity': {'type': 'string', 'enum': ['critical', 'warning', 'info']},
            'kind': {'type': 'string', 'enum': [
                'open_server', 'key_expired', 'key_expiring', 'key_unused', 'connection_failing', 'query_errors',
                'query_timeouts', 'query_slow', 'key_rate_limited', 'client_rate_limited', 'history_failed',
                'history_dropped']},
            'title': {'type': 'string'}, 'detail': {'type': 'string'},
            'since': {'type': 'string', 'nullable': True,
                      'description': 'When the condition began or will begin, where known (server time).'},
            'target': {'type': 'object', 'nullable': True, 'required': ['type', 'name'], 'properties': {
                'type': {'type': 'string', 'enum': ['key', 'query', 'connection', 'settings']},
                'name': {'type': 'string'}}},
        },
    },
    'AlertList': {
        'type': 'object', 'required': ['items', 'checked_at'],
        'properties': {'items': {'type': 'array', 'items': _ref('Alert')},
                       'checked_at': {'type': 'string', 'description': 'Server time the checks ran.'}},
    },
    'Instance': {
        'type': 'object',
        'required': ['id', 'host', 'pid', 'role', 'version', 'shared_limits', 'started_at', 'last_seen', 'this'],
        'properties': {
            'id': {'type': 'string'}, 'host': {'type': 'string'}, 'pid': {'type': 'integer'},
            'role': {'type': 'string', 'enum': ['serve', 'mcp', 'events']},
            'version': {'type': 'string'},
            'shared_limits': {'type': 'boolean',
                              'description': 'Whether it counts rate limits in Redis (QUERYAPIGATE_REDIS_URL).'},
            'started_at': {'type': 'string'},
            'last_seen': {'type': 'string', 'description': 'Refreshed at most every 30 seconds while it serves.'},
            'this': {'type': 'boolean', 'description': 'The process answering this request.'},
        },
    },
    'InstanceList': {
        'type': 'object', 'required': ['items', 'problems'],
        'properties': {
            'items': {'type': 'array', 'items': _ref('Instance')},
            'problems': {'type': 'array', 'items': {
                'type': 'object', 'required': ['kind', 'message'],
                'properties': {'kind': {'type': 'string', 'enum': ['instances_not_shared',
                                                                   'instances_versions_differ']},
                               'message': {'type': 'string'}}}},
        },
    },
    'HistoryPage': {
        'type': 'object', 'required': ['items', 'next_cursor'],
        'properties': {'items': {'type': 'array', 'items': _ref('HistoryEntry')},
                       'next_cursor': {'type': 'string', 'nullable': True,
                                       'description': 'Pass as ?cursor= for the next page; null on the last.'}},
    },
    'AuditEntry': {
        'type': 'object', 'required': ['timestamp', 'actor', 'action', 'target', 'changes'],
        'properties': {
            'timestamp': {'type': 'string'}, 'actor': _NULLABLE_STRING, 'action': {'type': 'string'},
            'via': {'type': 'string', 'nullable': True,
                    'enum': ['token', 'break-glass', 'open', 'cli', 'startup', None],
                    'description': "How the actor authenticated: a personal admin token, the shared "
                                   "QUERYAPIGATE_API_KEY, an open server, a CLI command or the server's own start-up. "
                                   'Absent on entries written before 0.16.'},
            'target': _NULLABLE_STRING,
            'changes': {'nullable': True, 'description': 'For a change, each field as {from, to}; for a create or '
                                                         'delete, the whole record. Never a secret.'},
        },
    },
    'Me': {
        'type': 'object', 'required': ['name', 'role', 'via', 'capabilities', 'data_access'],
        'properties': {
            'name': {'type': 'string', 'nullable': True,
                     'description': "The administrator's name; 'admin' for the shared key, null on an open server."},
            'role': {'type': 'string', 'enum': ['owner', 'admin', 'developer', 'auditor']},
            'via': {'type': 'string', 'enum': ['token', 'break-glass', 'open']},
            'capabilities': {'type': 'array', 'items': {'type': 'string'},
                             'description': 'What the role may do in this API (adminroles.py).'},
            'data_access': {'type': 'boolean', 'description': 'Whether it may run SQL and saved queries.'},
        },
    },
    'Administrator': {
        'type': 'object',
        'required': ['name', 'role', 'email', 'active', 'created_at', 'created_by', 'last_seen_at', 'tokens'],
        'properties': {
            'name': {'type': 'string'},
            'role': {'type': 'string', 'enum': ['owner', 'admin', 'developer', 'auditor']},
            'email': _NULLABLE_STRING, 'active': {'type': 'boolean'}, 'created_at': {'type': 'string'},
            'created_by': _NULLABLE_STRING,
            'last_seen_at': {'type': 'string', 'nullable': True,
                             'description': 'Last signed in with one of their tokens (to the minute).'},
            'tokens': {'type': 'integer', 'description': 'Tokens they hold, expired ones included.'},
        },
    },
    'AdministratorList': {
        'type': 'object', 'required': ['items'],
        'properties': {'items': {'type': 'array', 'items': _ref('Administrator')}},
    },
    'AdministratorInput': {
        'type': 'object',
        'properties': {
            'name': {'type': 'string', 'description': "Create only. Not an API key's name; not 'admin' or 'cli'."},
            'role': {'type': 'string', 'enum': ['owner', 'admin', 'developer', 'auditor']},
            'email': _NULLABLE_STRING,
            'active': {'type': 'boolean', 'description': 'Update only; false stops all their tokens at once.'},
        },
    },
    'AdminToken': {
        'type': 'object',
        'required': ['id', 'admin', 'label', 'created_at', 'expires_at', 'expired', 'last_used_at'],
        'properties': {
            'id': {'type': 'string'}, 'admin': {'type': 'string'}, 'label': _NULLABLE_STRING,
            'created_at': {'type': 'string'},
            'expires_at': {'type': 'string', 'nullable': True, 'description': 'Valid through the end of this date.'},
            'expired': {'type': 'boolean'}, 'last_used_at': _NULLABLE_STRING,
        },
    },
    'AdminTokenList': {
        'type': 'object', 'required': ['items'],
        'properties': {'items': {'type': 'array', 'items': _ref('AdminToken')}},
    },
    'AdminTokenCreated': {
        'allOf': [_ref('AdminToken'), {
            'type': 'object', 'required': ['secret'],
            'properties': {'secret': {'type': 'string',
                                      'description': 'qagadm_... - shown this once; send it as X-API-Key.'}},
        }],
    },
    'AdminTokenInput': {
        'type': 'object',
        'properties': {'label': _NULLABLE_STRING,
                       'expires_at': {'type': 'string', 'nullable': True,
                                      'description': 'YYYY-MM-DD; null or absent for no expiry.'}},
    },
    'AuditLog': {
        'type': 'object', 'required': ['items', 'total', 'actions', 'retention'],
        'properties': {
            'items': {'type': 'array', 'items': _ref('AuditEntry')},
            'total': {'type': 'integer', 'description': 'Entries stored, before filtering.'},
            'actions': {'type': 'array', 'items': {'type': 'string'}, 'description': 'Every action in the log.'},
            'retention': {'type': 'integer', 'description': 'The log keeps this many entries '
                                                            '(QUERYAPIGATE_AUDIT_LOG_LIMIT).'},
        },
    },
    'SettingsSection': {
        'type': 'object', 'required': ['id', 'title', 'description', 'rows'],
        'properties': {
            'id': {'type': 'string'}, 'title': {'type': 'string'}, 'description': {'type': 'string'},
            'rows': {'type': 'array', 'items': {
                'type': 'object',
                'required': ['label', 'description', 'env', 'value', 'source', 'env_value', 'experimental'],
                'properties': {
                    'label': {'type': 'string'}, 'description': {'type': 'string'},
                    'env': {'type': 'string', 'description': 'The environment variable.'},
                    'value': {'type': 'string', 'description': 'The effective value; a secret only as '
                                                               '"configured" or "enabled".'},
                    'source': {'type': 'string', 'enum': ['env', 'default']},
                    'env_value': {'type': 'string', 'nullable': True,
                                  'description': 'The raw value, for a .env export; null at the default and '
                                                 'always for a secret.'},
                    'experimental': {'type': 'boolean',
                                     'description': 'A setting of an experimental feature (outside the '
                                                    'compatibility promise).'},
                }}},
        },
    },
    'SettingsList': {
        'type': 'object', 'required': ['items'],
        'properties': {'items': {'type': 'array', 'items': _ref('SettingsSection')}},
    },
    'McpStatus': {
        'type': 'object', 'required': ['reachable', 'port'],
        'properties': {'reachable': {'type': 'boolean'}, 'port': {'type': 'integer'}},
    },
    'McpToolList': {
        'type': 'object', 'required': ['items'],
        'properties': {'items': {'type': 'array', 'items': {
            'type': 'object', 'required': ['name', 'description', 'kind', 'params', 'read_only'],
            'properties': {
                'name': {'type': 'string'}, 'description': {'type': 'string'},
                'kind': {'type': 'string', 'enum': ['ad-hoc', 'saved query']},
                'params': {'type': 'array', 'items': {'type': 'string'}},
                'read_only': {'type': 'boolean'},
            }}}},
    },
    'QueryFlow': {
        'type': 'object', 'required': ['tables', 'joins', 'formatted', 'error'],
        'properties': {
            'tables': {'type': 'array', 'items': {'type': 'string'}},
            'joins': {'type': 'array', 'items': {
                'type': 'object', 'required': ['left', 'right', 'type', 'on'],
                'properties': {'left': {'type': 'string'}, 'right': {'type': 'string'},
                               'type': {'type': 'string', 'description': 'e.g. LEFT JOIN.'},
                               'on': {'type': 'string', 'description': 'The join condition; empty if none.'}}}},
            'formatted': _NULLABLE_STRING,
            'error': {'type': 'string', 'nullable': True, 'description': 'Why it could not be analyzed.'},
        },
    },
    'CacheEntryList': {
        'type': 'object', 'required': ['items'],
        'properties': {'items': {'type': 'array', 'items': {
            'type': 'object', 'required': ['key', 'content_type', 'size_bytes', 'ttl_remaining_s', 'meta'],
            'properties': {
                'key': {'type': 'string'}, 'content_type': {'type': 'string'},
                'size_bytes': {'type': 'integer'}, 'ttl_remaining_s': {'type': 'number'},
                'meta': {'type': 'object', 'additionalProperties': True,
                         'description': 'name, version, connection, format and page, when known.',
                         'properties': {'name': {'type': 'string'}, 'version': {'type': 'integer'},
                                        'connection': {'type': 'string'}, 'format': {'type': 'string'}}},
            }}}},
    },
    'CollectionList': {
        'type': 'object', 'required': ['items', 'uncollected'],
        'properties': {
            'items': {'type': 'array', 'items': {
                'type': 'object', 'required': ['name', 'queries', 'keys', 'roles'],
                'properties': {
                    'name': {'type': 'string'},
                    'queries': {'type': 'array', 'items': {'type': 'string'}},
                    'keys': {'type': 'array', 'items': {'type': 'string'},
                             'description': 'API keys whose collections grant names it.'},
                    'roles': {'type': 'array', 'items': {'type': 'string'}},
                }}},
            'uncollected': {'type': 'array', 'items': {'type': 'string'}, 'description': 'Queries in no collection.'},
        },
    },
    'CollectionRenamed': {
        'type': 'object', 'required': ['name', 'moved'],
        'properties': {
            'name': {'type': 'string'},
            'moved': {'type': 'object', 'required': ['queries', 'keys', 'roles'],
                      'properties': {'queries': {'type': 'array', 'items': {'type': 'string'}},
                                     'keys': {'type': 'array', 'items': {'type': 'string'}},
                                     'roles': {'type': 'array', 'items': {'type': 'string'}}}},
        },
    },
    'ExamplesStatus': {
        'type': 'object', 'required': ['loaded', 'partial', 'connection', 'queries', 'roles', 'keys', 'collections'],
        'properties': {
            'loaded': {'type': 'boolean', 'description': 'All of them are installed.'},
            'partial': {'type': 'boolean', 'description': 'Some are: an interrupted load (POST again completes it).'},
            'connection': _NULLABLE_STRING,
            'queries': {'type': 'array', 'items': {'type': 'string'}},
            'roles': {'type': 'array', 'items': {'type': 'string'}},
            'keys': {'type': 'array', 'items': {'type': 'string'}},
            'collections': {'type': 'array', 'items': {'type': 'string'}},
        },
    },
    'ExamplesLoaded': {
        'type': 'object', 'required': ['added', 'key_secrets', 'status'],
        'properties': {
            'added': {'type': 'object', 'required': ['connection', 'queries', 'roles', 'keys'],
                      'properties': {'connection': {'type': 'boolean'},
                                     'queries': {'type': 'array', 'items': {'type': 'string'}},
                                     'roles': {'type': 'array', 'items': {'type': 'string'}},
                                     'keys': {'type': 'array', 'items': {'type': 'string'}}}},
            'key_secrets': {'type': 'object', 'additionalProperties': {'type': 'string'},
                            'description': "The new example keys' secrets - shown this once."},
            'status': _ref('ExamplesStatus'),
        },
    },
    'ExamplesRemoved': {
        'type': 'object', 'required': ['removed', 'keys_still_granted', 'status'],
        'properties': {
            'removed': {'type': 'object', 'required': ['connection', 'queries', 'roles', 'keys'],
                        'properties': {'connection': {'type': 'boolean'},
                                       'queries': {'type': 'array', 'items': {'type': 'string'}},
                                       'roles': {'type': 'array', 'items': {'type': 'string'}},
                                       'keys': {'type': 'array', 'items': {'type': 'string'}}}},
            'keys_still_granted': {'type': 'array', 'items': {'type': 'string'},
                                   'description': 'Other keys granted an example collection; that grant is now inert.'},
            'status': _ref('ExamplesStatus'),
        },
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
    'Grants': {
        'type': 'object',
        'required': ['connections', 'queries', 'collections', 'allow_writes', 'allowed_write_ops', 'allowed_tables',
                     'rate_limit', 'allowed_ips'],
        'properties': {
            'connections': {'oneOf': [{'type': 'string', 'enum': ['*']},
                                      {'type': 'array', 'items': {'type': 'string'}}],
                            'description': '"*" for every connection, or a list of names (ad-hoc SQL and every '
                                           'saved query on them).'},
            'queries': {'oneOf': [{'type': 'string', 'enum': ['*']}, {'type': 'array', 'items': {'oneOf': [
                {'type': 'string'},
                {'type': 'object', 'required': ['name'],
                 'properties': {'name': {'type': 'string'}, 'allow_writes': {'type': 'boolean'}}}]}}],
                        'description': 'Saved queries runnable by name, independent of connections; an object entry '
                                       'can allow writes through that one query.'},
            'collections': {'type': 'array', 'items': {'type': 'string'},
                            'description': 'Every query in these collections, including ones filed there later.'},
            'allow_writes': {'type': 'boolean', 'description': 'Still capped by QUERYAPIGATE_ALLOW_WRITES.'},
            'allowed_write_ops': {'type': 'array', 'items': {'type': 'string'}, 'nullable': True},
            'allowed_tables': {'type': 'array', 'items': {'type': 'string'}, 'nullable': True},
            'rate_limit': {'type': 'string', 'nullable': True, 'description': 'e.g. 100/minute.'},
            'allowed_ips': {'type': 'array', 'items': {'type': 'string'}, 'nullable': True,
                            'description': 'Addresses or CIDR ranges.'},
        },
    },
    'ApiKey': {
        'allOf': [_ref('Grants'), {
            'type': 'object',
            'required': ['name', 'active', 'expires_at', 'expired', 'created_at', 'created_from_role',
                         'last_used_at', 'example', 'usage'],
            'properties': {
                'name': {'type': 'string'}, 'active': {'type': 'boolean'},
                'expires_at': {'type': 'string', 'nullable': True,
                               'description': 'Valid through the end of this date.'},
                'expired': {'type': 'boolean'},
                'created_at': _NULLABLE_STRING, 'created_from_role': _NULLABLE_STRING,
                'last_used_at': _NULLABLE_STRING, 'example': {'type': 'boolean'},
                'usage': {'type': 'object', 'required': ['queries', 'errors', 'rows'],
                          'properties': {'queries': {'type': 'integer'}, 'errors': {'type': 'integer'},
                                         'rows': {'type': 'integer'}}},
            }}],
    },
    'ApiKeyCreated': {
        'allOf': [_ref('ApiKey'), {
            'type': 'object', 'required': ['secret'],
            'properties': {'secret': {'type': 'string', 'description': 'Shown this once - it cannot be read again.'}},
        }],
    },
    'ApiKeyInput': {
        'type': 'object',
        'properties': {
            'name': {'type': 'string', 'description': 'Create only.'},
            'role': {'type': 'string', 'description': "Create only: copy this role's grants (no grant fields then)."},
            'active': {'type': 'boolean', 'description': 'Update only; false revokes the key at once.'},
            'expires_at': {'type': 'string', 'nullable': True},
            'connections': {}, 'queries': {}, 'collections': {'type': 'array', 'items': {'type': 'string'}},
            'allow_writes': {'type': 'boolean'},
            'allowed_write_ops': {'type': 'array', 'items': {'type': 'string'}, 'nullable': True},
            'allowed_tables': {'type': 'array', 'items': {'type': 'string'}, 'nullable': True},
            'rate_limit': {'type': 'string', 'nullable': True},
            'allowed_ips': {'type': 'array', 'items': {'type': 'string'}, 'nullable': True},
        },
    },
    'ApiKeyList': {
        'type': 'object', 'required': ['items'],
        'properties': {'items': {'type': 'array', 'items': _ref('ApiKey')}},
    },
    'Role': {
        'allOf': [_ref('Grants'), {
            'type': 'object', 'required': ['name', 'created_at', 'example', 'keys_created'],
            'properties': {'name': {'type': 'string'}, 'created_at': _NULLABLE_STRING, 'example': {'type': 'boolean'},
                           'keys_created': {'type': 'integer', 'description': 'Keys created from this role.'}},
        }],
    },
    'RoleInput': {
        'type': 'object',
        'properties': {
            'name': {'type': 'string', 'description': 'Create only.'},
            'connections': {}, 'queries': {}, 'collections': {'type': 'array', 'items': {'type': 'string'}},
            'allow_writes': {'type': 'boolean'},
            'allowed_write_ops': {'type': 'array', 'items': {'type': 'string'}, 'nullable': True},
            'allowed_tables': {'type': 'array', 'items': {'type': 'string'}, 'nullable': True},
            'rate_limit': {'type': 'string', 'nullable': True},
            'allowed_ips': {'type': 'array', 'items': {'type': 'string'}, 'nullable': True},
        },
    },
    'RoleList': {
        'type': 'object', 'required': ['items'],
        'properties': {'items': {'type': 'array', 'items': _ref('Role')}},
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
    '/api/v1/queries/{name}/versions/{version}/flow': {
        'get': _op('The tables and joins its SQL touches, and the SQL formatted (best effort)',
                   {'200': _ok(_ref('QueryFlow'))}, parameters=[_NAME, _VERSION]),
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
    '/api/v1/history': {
        'get': _op('Every run - of a saved query, or ad-hoc SQL - newest first', {'200': _ok(_ref('HistoryPage'))},
                   parameters=[
            {'name': 'query', 'in': 'query', 'schema': {'type': 'string'}},
            {'name': 'kind', 'in': 'query', 'schema': {'type': 'string', 'enum': ['saved', 'adhoc']}},
            {'name': 'version', 'in': 'query', 'schema': {'type': 'integer'}},
            {'name': 'status', 'in': 'query', 'schema': {'type': 'string', 'enum': ['success', 'error']}},
            {'name': 'key', 'in': 'query', 'schema': {'type': 'string'}},
            {'name': 'since', 'in': 'query', 'schema': {'type': 'string'}},
            {'name': 'until', 'in': 'query', 'schema': {'type': 'string'}},
            {'name': 'limit', 'in': 'query', 'schema': {'type': 'integer', 'default': 100}},
            {'name': 'cursor', 'in': 'query', 'schema': {'type': 'string'}}]),
    },
    '/api/v1/me': {
        'get': _op('Who you are: your name, role and what it may do', {'200': _ok(_ref('Me'))}),
    },
    '/api/v1/alerts': {
        'get': _op('What needs attention now - expiring keys, failing connections, slow or failing queries, '
                   'rate limits being hit - most severe first', {'200': _ok(_ref('AlertList'))}),
    },
    '/api/v1/instances': {
        'get': _op('The processes using this store now - serve, mcp and events, seen in the last 90 seconds - and '
                   'what looks wrong about them', {'200': _ok(_ref('InstanceList'))}),
    },
    '/api/v1/audit': {
        'get': _op('Administrative changes, newest first', {'200': _ok(_ref('AuditLog'))}, parameters=[
            {'name': 'action', 'in': 'query', 'schema': {'type': 'string'}},
            {'name': 'actor', 'in': 'query', 'schema': {'type': 'string'}},
            {'name': 'target', 'in': 'query', 'schema': {'type': 'string'}},
            {'name': 'q', 'in': 'query', 'schema': {'type': 'string'},
             'description': 'Matches the time, actor or target.'}]),
    },
    '/api/v1/cache/entries': {
        'get': _op('What is in the response cache now, soonest to expire first', {'200': _ok(_ref('CacheEntryList'))}),
        'delete': _op('Evict every entry', {'204': {'description': 'Cleared'}}),
    },
    '/api/v1/cache/entries/{key}': {
        'get': _op('The cached body, with its real content type', {'200': {
            'description': 'The body, exactly as a caller receives it on a hit',
            'content': {'*/*': {'schema': {'type': 'string', 'format': 'binary'}}}}},
            parameters=[{'name': 'key', 'in': 'path', 'required': True, 'schema': {'type': 'string'}}]),
        'delete': _op('Evict one entry', {'204': {'description': 'Evicted'}},
                      parameters=[{'name': 'key', 'in': 'path', 'required': True, 'schema': {'type': 'string'}}]),
    },
    '/api/v1/collections': {
        'get': _op('Every collection with its queries and who reaches it, and the queries in none',
                   {'200': _ok(_ref('CollectionList'))}),
    },
    '/api/v1/collections/{name}': {
        'patch': _op('Rename it (into an existing one only with merge: true); grants follow',
                     {'200': _ok(_ref('CollectionRenamed')), **_CONFLICT}, parameters=[_NAME],
                     body={'type': 'object', 'required': ['name'],
                           'properties': {'name': {'type': 'string'}, 'merge': {'type': 'boolean'}}}),
    },
    '/api/v1/collections/{name}/postman': {
        'get': _op('The collection as a Postman Collection v2.1 file',
                   {'200': _ok({'type': 'object', 'additionalProperties': True})}, parameters=[_NAME]),
    },
    '/api/v1/examples': {
        'get': _op('Whether the example APIs are installed', {'200': _ok(_ref('ExamplesStatus'))}),
        'post': _op("Install the example APIs; the response carries the example keys' secrets, shown this once",
                    {'200': _ok(_ref('ExamplesLoaded')), **_CONFLICT}),
        'delete': _op('Remove exactly what is marked as an example', {'200': _ok(_ref('ExamplesRemoved'))}),
    },
    '/api/v1/settings': {
        'get': _op("The server's effective configuration, by section (read-only)",
                   {'200': _ok(_ref('SettingsList'))}),
    },
    '/api/v1/mcp/status': {
        'get': _op("Whether the MCP server's port answers (a TCP connect, run only when asked)",
                   {'200': _ok(_ref('McpStatus'))}),
    },
    '/api/v1/mcp/tools': {
        'get': _op('What tools/list returns for an unrestricted caller', {'200': _ok(_ref('McpToolList'))}),
    },
    '/api/v1/api-keys': {
        'get': _op('List API keys (grants, status, expiry, last use, usage - never secrets)',
                   {'200': _ok(_ref('ApiKeyList'))}),
        'post': _op('Create an API key; the response carries its secret, shown this once',
                    {'201': _ok(_ref('ApiKeyCreated'), etag=True), **_CONFLICT}, body=_ref('ApiKeyInput')),
    },
    '/api/v1/api-keys/{name}': {
        'get': _op('Get an API key', {'200': _ok(_ref('ApiKey'), etag=True)}, parameters=[_NAME]),
        'patch': _op('Change some of its grants, its expiry, or active (false revokes it)',
                     {'200': _ok(_ref('ApiKey'), etag=True), **_PRECONDITION}, parameters=[_NAME, _IF_MATCH],
                     body=_ref('ApiKeyInput')),
        'delete': _op('Revoke and remove it', {'204': {'description': 'Revoked'}, **_PRECONDITION},
                      parameters=[_NAME, _IF_MATCH]),
    },
    '/api/v1/administrators': {
        'get': _op('List administrators (owners only)', {'200': _ok(_ref('AdministratorList'))}),
        'post': _op('Create an administrator (owners only); issue them a token next',
                    {'201': _ok(_ref('Administrator'), etag=True), **_CONFLICT}, body=_ref('AdministratorInput')),
    },
    '/api/v1/administrators/{name}': {
        'get': _op('Get an administrator', {'200': _ok(_ref('Administrator'), etag=True)}, parameters=[_NAME]),
        'patch': _op('Change their role, email or active (false stops every token of theirs)',
                     {'200': _ok(_ref('Administrator'), etag=True), **_PRECONDITION, **_CONFLICT},
                     parameters=[_NAME, _IF_MATCH], body=_ref('AdministratorInput')),
        'delete': _op('Remove them and their tokens', {'204': {'description': 'Removed'}, **_PRECONDITION,
                                                       **_CONFLICT}, parameters=[_NAME, _IF_MATCH]),
    },
    '/api/v1/administrators/{name}/tokens': {
        'get': _op("List an administrator's tokens - your own, or anyone's as an owner",
                   {'200': _ok(_ref('AdminTokenList'))}, parameters=[_NAME]),
        'post': _op('Issue a token - your own, or anyone\'s as an owner; the response carries its secret, shown once',
                    {'201': _ok(_ref('AdminTokenCreated'))}, parameters=[_NAME], body=_ref('AdminTokenInput')),
    },
    '/api/v1/administrators/{name}/tokens/{token_id}': {
        'delete': _op('Revoke a token - your own, or anyone\'s as an owner', {'204': {'description': 'Revoked'}},
                      parameters=[_NAME, {'name': 'token_id', 'in': 'path', 'required': True,
                                          'schema': {'type': 'string'}}]),
    },
    '/api/v1/roles': {
        'get': _op('List roles (grant templates)', {'200': _ok(_ref('RoleList'))}),
        'post': _op('Create a role', {'201': _ok(_ref('Role'), etag=True), **_CONFLICT}, body=_ref('RoleInput')),
    },
    '/api/v1/roles/{name}': {
        'get': _op('Get a role', {'200': _ok(_ref('Role'), etag=True)}, parameters=[_NAME]),
        'patch': _op('Change some of its grants (keys created from it are not touched)',
                     {'200': _ok(_ref('Role'), etag=True), **_PRECONDITION}, parameters=[_NAME, _IF_MATCH],
                     body=_ref('RoleInput')),
        'delete': _op('Delete the template (keys created from it keep their grants)',
                      {'204': {'description': 'Deleted'}, **_PRECONDITION}, parameters=[_NAME, _IF_MATCH]),
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

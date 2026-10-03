"""The Management API, version 1: /api/v1/... (ADR 0001, BACKLOG #72).

A versioned, resource-oriented interface to QueryAPIGate's own configuration - built for the Console, and usable
by anything else (scripts, Terraform, GitOps, other platforms). Built one resource at a time; the legacy routes
each one replaces keep working, marked deprecated. Conventions, the same on every resource:

- Admin only (it manages the server's configuration), like the legacy management routes.
- Every error carries a stable machine-readable `code` alongside the human `error` message (BACKLOG #69).
- A resource that can be edited returns an `ETag`; a change may send `If-Match` and gets 412 if the resource changed
  since it was read, so two admins can't silently overwrite each other's work.
- Every response shape is described in /openapi.json (v1_spec.py) and checked against it by the tests.

Routes here translate HTTP to services/ calls and back; the meaning of each operation lives in the service.
"""
from flask import Blueprint, jsonify, request

from . import collection_admin, history, schema, store
from .app import caller_key_name, get_int, get_json_body, require_admin
from .errors import ApiError
from .services import queries

bp = Blueprint('v1', __name__, url_prefix='/api/v1')


@bp.before_request
def admin_only():
    require_admin()


def _check_if_match(content):
    expected = request.headers.get('If-Match')
    if expected and expected != '*' and expected != queries.etag(content):
        raise ApiError('This query changed since you loaded it - reload it and try again', 412,
                       code='precondition_failed')


def _detail_response(name, status=200):
    name, content = queries.load(name)
    response = jsonify(queries.to_detail(name, content))
    response.headers['ETag'] = queries.etag(content)
    return response, status


# --------------------------------------------------------------------------------------
# Queries
# --------------------------------------------------------------------------------------

@bp.route('/queries', methods=['GET'])
def list_queries():
    args = request.args
    items = queries.list_queries(collection=args.get('collection'), connection=args.get('connection'),
                                 search=args.get('search'), status=args.get('status'))
    return jsonify({'items': items}), 200


@bp.route('/queries', methods=['POST'])
def create_query():
    data = get_json_body()
    queries.create(data, caller_key_name())
    return _detail_response(data['name'], 201)


@bp.route('/queries/validate', methods=['POST'])
def validate_query():
    return jsonify(queries.validate(get_json_body(), caller_key_name())), 200


@bp.route('/queries/<name>', methods=['GET'])
def get_query(name):
    return _detail_response(name)


@bp.route('/queries/<name>', methods=['PATCH'])
def update_query(name):
    """Query-level fields - today only `collection` (null removes it from any). Versions are immutable apart
    from their cache_ttl; anything else changes by adding a version."""
    name, content = queries.load(name)
    _check_if_match(content)
    data = get_json_body()
    unknown = sorted(set(data) - {'collection'})
    if unknown:
        raise ApiError(f"Only 'collection' can be changed on a query (unknown: {', '.join(unknown)}) - change "
                       'anything else by adding a version', code='unknown_field')
    if 'collection' in data:
        previous = store.set_collection(name, data['collection'])
        if previous != data['collection']:
            access = collection_admin.access_change(previous, data['collection'])
            store.record_audit(caller_key_name(), 'move_query', name,
                               {'collection': {'from': previous, 'to': data['collection']},
                                'keys_gaining_access': access['keys']['gain'],
                                'keys_losing_access': access['keys']['lose'], 'via': 'api/v1'})
    return _detail_response(name)


@bp.route('/queries/<name>', methods=['DELETE'])
def delete_query(name):
    name, content = queries.load(name)
    _check_if_match(content)
    store.delete_saved(name)
    store.record_audit(caller_key_name(), 'delete_query', name, {'via': 'api/v1'})
    return '', 204


@bp.route('/queries/<name>/versions', methods=['POST'])
def add_version(name):
    name, content = queries.load(name)
    _check_if_match(content)
    queries.add_version(name, get_json_body(), caller_key_name())
    return _detail_response(name, 201)


@bp.route('/queries/<name>/versions/<int:version>', methods=['GET'])
def get_version(name, version):
    name, content = queries.load(name)
    data = content.get(str(version))
    if not isinstance(data, dict):
        raise ApiError(f'Version {version} not found', 404, code='version_not_found')
    return jsonify(queries.to_version(content, version, data)), 200


@bp.route('/queries/<name>/versions/<int:version>', methods=['PATCH'])
def update_version(name, version):
    """A version's `cache_ttl` (null or 0 turns caching off) - the one thing that changes in place."""
    name, content = queries.load(name)
    _check_if_match(content)
    data = get_json_body()
    unknown = sorted(set(data) - {'cache_ttl'})
    if unknown:
        raise ApiError(f"Only 'cache_ttl' can be changed on a version (unknown: {', '.join(unknown)})",
                       code='unknown_field')
    if 'cache_ttl' in data:
        from . import definitions
        definitions.validate_cache_ttl(data['cache_ttl'])
        if not isinstance(content.get(str(version)), dict):
            raise ApiError(f'Version {version} not found', 404, code='version_not_found')
        store.set_cache_ttl(name, version, data['cache_ttl'])
        store.record_audit(caller_key_name(), 'set_cache_ttl', name,
                           {'version': version, 'cache_ttl': data['cache_ttl'] or None, 'via': 'api/v1'})
    return _detail_response(name)


@bp.route('/queries/<name>/versions/<int:version>', methods=['DELETE'])
def delete_version(name, version):
    name, content = queries.load(name)
    _check_if_match(content)
    if not isinstance(content.get(str(version)), dict):
        raise ApiError(f'Version {version} not found', 404, code='version_not_found')
    store.delete_saved(name, version)
    store.record_audit(caller_key_name(), 'delete_query', name, {'version': version, 'via': 'api/v1'})
    if not store.saved_query_exists(name):
        return '', 204
    return _detail_response(name)


@bp.route('/queries/<name>/publish', methods=['POST'])
def publish(name):
    """Make a version the one served - newer (publish a draft) or older (roll back)."""
    name, content = queries.load(name)
    _check_if_match(content)
    data = get_json_body()
    queries.publish(name, data.get('version'), caller_key_name())
    return _detail_response(name)


@bp.route('/queries/<name>/unpublish', methods=['POST'])
def unpublish(name):
    """Stop serving the query at all; every version is kept, and any can be published again."""
    name, content = queries.load(name)
    _check_if_match(content)
    queries.unpublish(name, caller_key_name())
    return _detail_response(name)


@bp.route('/queries/<name>/history', methods=['GET'])
def query_history(name):
    """This query's runs, newest first, paged with a cursor - GET /history scoped to one query."""
    name, _ = queries.load(name)
    args = request.args
    limit = get_int(args.get('limit'), 'limit')
    entries, next_cursor = history.search(
        query=name, version=get_int(args.get('version'), 'version'), status=args.get('status'),
        key=args.get('key'), since=args.get('since'), until=args.get('until'),
        limit=50 if limit is None else limit, cursor=args.get('cursor'))
    return jsonify({'items': entries, 'next_cursor': next_cursor}), 200


# --------------------------------------------------------------------------------------
# Connections (read side - what the query editor needs; the Connections slice grows this)
# --------------------------------------------------------------------------------------

@bp.route('/connections', methods=['GET'])
def list_connections():
    # Only what identifies a connection - never credentials or driver options.
    items = [{'name': name, 'db': details.get('db'), 'active': bool(details.get('active', True)),
              'host': details.get('host') or None, 'port': details.get('port') or None,
              'database': details.get('database') if isinstance(details.get('database'), str) else None}
             for name, details in sorted(store.read_connections().items())]
    return jsonify({'items': items}), 200


@bp.route('/connections/<name>/schema', methods=['GET'])
def connection_schema(name):
    if name not in store.read_connections():
        raise ApiError(f"Connection '{name}' not found", 404, code='connection_not_found')
    return jsonify(schema.fetch_schema(name)), 200

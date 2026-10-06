"""The Management API, version 1: /api/v1/... (ADR 0001, BACKLOG #72).

A versioned, resource-oriented interface to QueryAPIGate's own configuration - built for the Console, and usable
by anything else (scripts, Terraform, GitOps, other platforms). Built one resource at a time; the legacy routes
each one replaces keep working, marked deprecated. Conventions, the same on every resource:

- Administrators only (it manages the server's configuration): each operation is allowed to the roles whose
  capabilities include it (adminroles.py, ADR 0003) - `403 admin_only` for any other caller, `403 role_forbidden`
  for an administrator whose role can't.
- Every error carries a stable machine-readable `code` alongside the human `error` message (BACKLOG #69).
- A resource that can be edited returns an `ETag`; a change may send `If-Match` and gets 412 if the resource changed
  since it was read, so two admins can't silently overwrite each other's work.
- Every response shape is described in /openapi.json (v1_spec.py) and checked against it by the tests.

Routes here translate HTTP to services/ calls and back; the meaning of each operation lives in the service.
"""
from flask import Blueprint, Response, current_app, g, jsonify, request

from . import adminroles, alerts, collection_admin, config, history, instances, schema, sqlflow, store
from .app import caller_key_name, get_int, get_json_body
from .errors import ApiError
from .services import access, administrators, audit, collections, connections, mcp, queries
from .services import destinations as destination_service

bp = Blueprint('v1', __name__, url_prefix='/api/v1')


@bp.before_request
def admin_only():
    """Administrators only, each operation to the roles whose capabilities include it (adminroles.py)."""
    adminroles.authorize(g.permission, request.endpoint)


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


@bp.route('/me', methods=['GET'])
def me():
    """Who the caller is: name, role, what that role may do, and how they authenticated."""
    permission = g.permission
    return jsonify({'name': permission.name, 'role': permission.role, 'via': permission.via,
                    'capabilities': adminroles.capabilities(permission.role),
                    'data_access': permission.role in adminroles.DATA_ROLES}), 200


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
        access = collection_admin.access_change(store.read_collection(content), data['collection'])
        adminroles.require_to_widen_access(g.permission, {k: v['gain'] for k, v in access.items()},
                                           f"Moving '{name}' to collection '{data['collection']}'")
        previous = store.set_collection(name, data['collection'])
        if previous != data['collection']:
            store.record_audit(caller_key_name(), 'move_query', name,
                               {'collection': {'from': previous, 'to': data['collection']},
                                'keys_gaining_access': access['keys']['gain'],
                                'keys_losing_access': access['keys']['lose']})
    return _detail_response(name)


@bp.route('/queries/<name>', methods=['DELETE'])
def delete_query(name):
    name, content = queries.load(name)
    _check_if_match(content)
    store.delete_saved(name)
    store.record_audit(caller_key_name(), 'delete_query', name, None)
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


@bp.route('/queries/<name>/versions/<int:version>/flow', methods=['GET'])
def version_flow(name, version):
    """The tables and joins the version's SQL touches, and its SQL formatted - best effort (see sqlflow.py): a
    query that can't be analyzed (a Mongo query, an unsupported dialect, a parse failure) has an `error` and no
    tables, rather than failing."""
    name, content = queries.load(name)
    data = content.get(str(version))
    if not isinstance(data, dict):
        raise ApiError(f'Version {version} not found', 404, code='version_not_found')
    if data.get('query_type') == 'mongo' or not isinstance(data.get('sql_query'), str):
        return jsonify({'tables': [], 'joins': [], 'formatted': None,
                        'error': "SQL analysis isn't available for this query"}), 200
    connection = store.read_connections().get(data.get('connection_name') or '')
    return jsonify(sqlflow.extract_flow(data['sql_query'], connection['db'] if connection else None)), 200


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
                           {'version': version, 'cache_ttl': data['cache_ttl'] or None})
    return _detail_response(name)


@bp.route('/queries/<name>/versions/<int:version>', methods=['DELETE'])
def delete_version(name, version):
    name, content = queries.load(name)
    _check_if_match(content)
    if not isinstance(content.get(str(version)), dict):
        raise ApiError(f'Version {version} not found', 404, code='version_not_found')
    store.delete_saved(name, version)
    store.record_audit(caller_key_name(), 'delete_query', name, {'version': version})
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
    return jsonify({'items': connections.list_items()}), 200


@bp.route('/connections', methods=['POST'])
def create_connection():
    name = connections.create(get_json_body(), caller_key_name())
    return _connection_response(name, 201)


@bp.route('/connections/deleted', methods=['GET'])
def deleted_connections():
    """Deleted connections, newest first, with who deleted them and why - read from the audit log."""
    return jsonify({'items': connections.deleted()}), 200


@bp.route('/connections/test', methods=['POST'])
def test_connection():
    """Try to connect with the given fields (or a saved connection's, given only its `name`). Nothing is saved."""
    return jsonify(connections.test(get_json_body())), 200


@bp.route('/connections/databases', methods=['POST'])
def connection_databases():
    """Every database on the server the given fields (or a saved connection, by `name`) point at."""
    return jsonify({'databases': connections.databases(get_json_body())}), 200


def _connection_response(name, status=200):
    details = connections.load(name)
    response = jsonify(connections.to_detail(name, details))
    response.headers['ETag'] = connections.etag(details)
    return response, status


def _check_connection_if_match(details):
    expected = request.headers.get('If-Match')
    if expected and expected != '*' and expected != connections.etag(details):
        raise ApiError('This connection changed since you loaded it - reload it and try again', 412,
                       code='precondition_failed')


@bp.route('/connections/<name>', methods=['GET'])
def get_connection(name):
    return _connection_response(name)


@bp.route('/connections/<name>', methods=['PATCH'])
def update_connection(name):
    """Change some fields; everything not mentioned is kept. The password mask (or leaving `password` out) keeps the
    stored password; `null` removes an optional field."""
    _check_connection_if_match(connections.load(name))
    connections.update(name, get_json_body(), caller_key_name())
    return _connection_response(name)


@bp.route('/connections/<name>', methods=['DELETE'])
def delete_connection(name):
    """Every saved query using it stops working, so a `reason` is required; it is kept in the audit log."""
    _check_connection_if_match(connections.load(name))
    data = get_json_body(required=False) or {}
    connections.delete(name, data.get('reason'), caller_key_name())
    return '', 204


@bp.route('/connections/<name>/schema', methods=['GET'])
def connection_schema(name):
    """`?database=` browses another database on the same server (the API Designer's Database picker)."""
    connections.load(name)
    return jsonify(schema.fetch_schema(name, database=request.args.get('database') or None)), 200


# --------------------------------------------------------------------------------------
# API keys and roles
# --------------------------------------------------------------------------------------

def _check_entry_if_match(entry, what):
    expected = request.headers.get('If-Match')
    if expected and expected != '*' and expected != access.etag(entry):
        raise ApiError(f'This {what} changed since you loaded it - reload it and try again', 412,
                       code='precondition_failed')


def _key_response(name, status=200, secret=None):
    entry = access.load_key(name)
    body = access.to_key(name, entry)
    if secret is not None:
        body['secret'] = secret
    response = jsonify(body)
    response.headers['ETag'] = access.etag(entry)
    if secret is not None:
        response.headers['Cache-Control'] = 'no-store'  # the one response that carries a secret
    return response, status


@bp.route('/api-keys', methods=['GET'])
def list_api_keys():
    return jsonify({'items': access.list_keys()}), 200


@bp.route('/api-keys', methods=['POST'])
def create_api_key():
    """A new key, from explicit grants or copied from a `role`. The response's `secret` is the only time it is
    ever shown: store it now."""
    name, secret = access.create_key(get_json_body(), caller_key_name())
    return _key_response(name, 201, secret)


@bp.route('/api-keys/<name>', methods=['GET'])
def get_api_key(name):
    return _key_response(name)


@bp.route('/api-keys/<name>', methods=['PATCH'])
def update_api_key(name):
    """Change some grants, the expiry, or `active` (false revokes it at once); null clears an optional field."""
    _check_entry_if_match(access.load_key(name), 'API key')
    access.update_key(name, get_json_body(), caller_key_name())
    return _key_response(name)


@bp.route('/api-keys/<name>', methods=['DELETE'])
def delete_api_key(name):
    """Revoke and remove it: anything still using it stops working immediately."""
    _check_entry_if_match(access.load_key(name), 'API key')
    access.delete_key(name, caller_key_name())
    return '', 204


def _role_response(name, status=200):
    entry = access.load_role(name)
    response = jsonify(access.to_role(name, entry))
    response.headers['ETag'] = access.etag(entry)
    return response, status


@bp.route('/roles', methods=['GET'])
def list_roles():
    return jsonify({'items': access.list_roles()}), 200


@bp.route('/roles', methods=['POST'])
def create_role():
    return _role_response(access.create_role(get_json_body(), caller_key_name()), 201)


@bp.route('/roles/<name>', methods=['GET'])
def get_role(name):
    return _role_response(name)


@bp.route('/roles/<name>', methods=['PATCH'])
def update_role(name):
    """Change some of the template's grants. Keys already created from it are not touched."""
    _check_entry_if_match(access.load_role(name), 'role')
    access.update_role(name, get_json_body(), caller_key_name())
    return _role_response(name)


@bp.route('/roles/<name>', methods=['DELETE'])
def delete_role(name):
    """Remove the template; keys already created from it keep their grants."""
    _check_entry_if_match(access.load_role(name), 'role')
    access.delete_role(name, caller_key_name())
    return '', 204


# --------------------------------------------------------------------------------------
# Destinations: where exports may write (ADR 0004)
# --------------------------------------------------------------------------------------

def _destination_etag(destination):
    return destination_service.etag({k: v for k, v in destination.items() if k not in ('updated_at',)})


def _destination_response(name, status=200):
    destination = destination_service.load(name)
    response = jsonify(destination)
    response.headers['ETag'] = _destination_etag(destination)
    return response, status


def _check_destination_if_match(name):
    expected = request.headers.get('If-Match')
    if expected and expected != '*' and expected != _destination_etag(destination_service.load(name)):
        raise ApiError('This destination changed since you loaded it - reload it and try again', 412,
                       code='precondition_failed')


@bp.route('/destinations', methods=['GET'])
def list_destinations():
    return jsonify({'items': destination_service.list_all()}), 200


@bp.route('/destinations', methods=['POST'])
def create_destination():
    """A bucket prefix or folder exports may write under, and the credentials to write there."""
    return _destination_response(destination_service.create(get_json_body(), caller_key_name()), 201)


@bp.route('/destinations/test', methods=['POST'])
def test_destination_fields():
    """Write the probe object with fields not saved yet - the form's Test button."""
    return jsonify(destination_service.test(fields=get_json_body())), 200


@bp.route('/destinations/<name>', methods=['GET'])
def get_destination(name):
    return _destination_response(name)


@bp.route('/destinations/<name>', methods=['PATCH'])
def update_destination(name):
    """Change some fields; null removes an optional one; the masked secret sent back keeps it."""
    _check_destination_if_match(name)
    destination_service.update(name, get_json_body(), caller_key_name())
    return _destination_response(name)


@bp.route('/destinations/<name>', methods=['DELETE'])
def delete_destination(name):
    _check_destination_if_match(name)
    destination_service.delete(name, caller_key_name())
    return '', 204


@bp.route('/destinations/<name>/test', methods=['POST'])
def test_destination(name):
    """Write one small probe object under the prefix, as an export would: url, credentials and permission."""
    return jsonify(destination_service.test(name=name)), 200


# --------------------------------------------------------------------------------------
# Administrators and their admin tokens (ADR 0003)
# --------------------------------------------------------------------------------------

def _admin_etag(admin):
    # what an edit can change - not last_seen_at, which moves whenever they sign in
    return administrators.etag({k: admin[k] for k in ('name', 'role', 'email', 'active')})


def _admin_response(name, status=200):
    admin = administrators.load(name)
    response = jsonify(admin)
    response.headers['ETag'] = _admin_etag(admin)
    return response, status


def _check_admin_if_match(name):
    expected = request.headers.get('If-Match')
    if expected and expected != '*' and expected != _admin_etag(administrators.load(name)):
        raise ApiError('This administrator changed since you loaded it - reload it and try again', 412,
                       code='precondition_failed')


@bp.route('/administrators', methods=['GET'])
def list_administrators():
    return jsonify({'items': administrators.list_admins()}), 200


@bp.route('/administrators', methods=['POST'])
def create_administrator():
    """A new administrator. It signs in with a token issued next (POST .../tokens), shown once."""
    return _admin_response(administrators.create(get_json_body(), caller_key_name()), 201)


@bp.route('/administrators/<name>', methods=['GET'])
def get_administrator(name):
    return _admin_response(name)


@bp.route('/administrators/<name>', methods=['PATCH'])
def update_administrator(name):
    """Change the role, email or `active` (false stops every one of their tokens at once)."""
    _check_admin_if_match(name)
    administrators.update(name, get_json_body(), caller_key_name())
    return _admin_response(name)


@bp.route('/administrators/<name>', methods=['DELETE'])
def delete_administrator(name):
    """Remove them and every token they hold. Their name stays in the audit log."""
    _check_admin_if_match(name)
    administrators.delete(name, caller_key_name())
    return '', 204


@bp.route('/administrators/<name>/tokens', methods=['GET'])
def list_admin_tokens(name):
    administrators.require_self_or(g.permission, name, 'admins.read')
    return jsonify({'items': administrators.list_tokens(name)}), 200


@bp.route('/administrators/<name>/tokens', methods=['POST'])
def issue_admin_token(name):
    """A new token; the response's `secret` is the only time it is shown. Your own, or - as an owner - anyone's."""
    administrators.require_self_or(g.permission, name, 'admins.write')
    response = jsonify(administrators.issue_token(name, get_json_body(required=False), caller_key_name()))
    response.headers['Cache-Control'] = 'no-store'
    return response, 201


@bp.route('/administrators/<name>/tokens/<token_id>', methods=['DELETE'])
def revoke_admin_token(name, token_id):
    administrators.require_self_or(g.permission, name, 'admins.write')
    administrators.revoke_token(name, token_id, caller_key_name())
    return '', 204


# --------------------------------------------------------------------------------------
# History and audit
# --------------------------------------------------------------------------------------

@bp.route('/alerts', methods=['GET'])
def list_alerts():
    """What needs an admin's attention now (alerts.py), worked out afresh each time."""
    return jsonify({'items': alerts.collect(), 'checked_at': store.now()}), 200


@bp.route('/instances', methods=['GET'])
def list_instances():
    """The processes using this store right now (instances.py) - and what looks wrong about them."""
    return jsonify({'items': instances.alive(),
                    'problems': [{'kind': kind, 'message': message} for kind, message in instances.problems()]}), 200


@bp.route('/history', methods=['GET'])
def search_history():
    """Every run - of a saved query, or ad-hoc SQL - newest first, paged with a cursor. Filters: kind (saved or
    adhoc), query, version, status, key, since (inclusive) and until (exclusive) - a date or a time."""
    args = request.args
    limit = get_int(args.get('limit'), 'limit')
    entries, next_cursor = history.search(
        query=args.get('query'), version=get_int(args.get('version'), 'version'), status=args.get('status'),
        key=args.get('key'), since=args.get('since'), until=args.get('until'),
        limit=100 if limit is None else limit, cursor=args.get('cursor'), kind=args.get('kind'))
    return jsonify({'items': entries, 'next_cursor': next_cursor}), 200


@bp.route('/audit', methods=['GET'])
def list_audit():
    """Administrative changes, newest first. `q` matches the time, actor or target."""
    args = request.args
    return jsonify(audit.listing(action=args.get('action'), actor=args.get('actor'), target=args.get('target'),
                                 text=args.get('q'))), 200


# --------------------------------------------------------------------------------------
# Settings and MCP
# --------------------------------------------------------------------------------------

@bp.route('/settings', methods=['GET'])
def get_settings():
    """The server's effective configuration, by section. Read-only: settings are environment variables, changed
    by restarting the server. A secret is shown only as configured or not, and never in `env_value`."""
    return jsonify({'items': config.describe_settings()}), 200


@bp.route('/mcp/status', methods=['GET'])
def mcp_status():
    """Whether something answers on the MCP server's port - a plain TCP connect, run only when asked."""
    return jsonify(mcp.status()), 200


@bp.route('/mcp/tools', methods=['GET'])
def mcp_tools():
    """What `tools/list` returns for an unrestricted caller, whether or not the MCP server is running."""
    return jsonify({'items': mcp.tools()}), 200


# --------------------------------------------------------------------------------------
# The response cache
# --------------------------------------------------------------------------------------

def _cache():
    return current_app.extensions['queryapigate_cache']


@bp.route('/cache/entries', methods=['GET'])
def list_cache_entries():
    """What is cached right now (in-process or Redis), soonest to expire first - each entry's query, version,
    connection, format, content type, size and time left; never the body."""
    entries = _cache().list_entries()
    entries.sort(key=lambda e: e['ttl_remaining_s'])
    return jsonify({'items': entries}), 200


@bp.route('/cache/entries', methods=['DELETE'])
def clear_cache():
    """Evict everything: the next call to each query runs it for real. Not audited - housekeeping, not a change."""
    _cache().clear()
    return '', 204


@bp.route('/cache/entries/<key>', methods=['GET'])
def get_cache_entry(key):
    """The cached body itself, with its real content type - what a caller receives on a hit."""
    hit = _cache().get_body(key)
    if hit is None:
        raise ApiError('Cache entry not found (missing, expired, or already evicted)', 404,
                       code='cache_entry_not_found')
    body, content_type = hit
    return Response(body, content_type=content_type)


@bp.route('/cache/entries/<key>', methods=['DELETE'])
def delete_cache_entry(key):
    """Evict one entry early."""
    _cache().delete(key)
    return '', 204


# --------------------------------------------------------------------------------------
# Collections and the example APIs
# --------------------------------------------------------------------------------------

@bp.route('/collections', methods=['GET'])
def list_collections():
    """Every collection with its queries and the keys and roles that reach it, and the queries in none. Move a query
    with PATCH /api/v1/queries/{name}."""
    return jsonify(collections.listing()), 200


@bp.route('/collections/<name>', methods=['PATCH'])
def rename_collection(name):
    """Rename it: `{"name": "new"}`; into an existing collection only with `"merge": true`. Grants follow, and
    nobody's access narrows part-way."""
    return jsonify(collections.rename(name, get_json_body(), caller_key_name(), g.permission)), 200


@bp.route('/collections/<name>/postman', methods=['GET'])
def collection_postman(name):
    """The collection as a Postman Collection v2.1 file - its base URL is the one this request came in on; it holds
    no key."""
    response = jsonify(collections.postman_export(name, request.host_url))
    response.headers['Content-Disposition'] = f'attachment; filename="{name}.postman_collection.json"'
    return response, 200


@bp.route('/examples', methods=['GET'])
def examples_status():
    """Whether the example APIs are installed: `loaded` (all of them), `partial` (an interrupted load), and what."""
    return jsonify(collections.examples_status()), 200


@bp.route('/examples', methods=['POST'])
def load_examples():
    """Install the example APIs. Idempotent; 409 `examples_conflict`, changing nothing, if something that isn't an
    example holds one of their names. The example keys' secrets are in the response, shown this once."""
    response = jsonify(collections.load_examples(caller_key_name()))
    response.headers['Cache-Control'] = 'no-store'
    return response, 200


@bp.route('/examples', methods=['DELETE'])
def unload_examples():
    """Remove exactly what is marked as an example - nothing else."""
    return jsonify(collections.unload_examples(caller_key_name())), 200

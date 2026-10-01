"""The Flask application: HTTP routes on top of the store, engine and formatters."""
import json
import logging
import math
import queue
import re
import socket
import threading
import time
import uuid

from flask import Blueprint, Flask, Response, current_app, g, jsonify, redirect, request, url_for
from flask.json.provider import DefaultJSONProvider
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.wsgi import ClosingIterator

from . import (
    apikeys,
    broadcast,
    cache,
    collection_admin,
    config,
    cors,
    db,
    definitions,
    engine,
    examples,
    history,
    logging_setup,
    mcp_server,
    metrics,
    mongotools,
    openapi,
    pool,
    postman,
    schema,
    sqlflow,
    sqltools,
    store,
    ui,
)
from . import params as param_rules
from .errors import ApiError
from .formats import FORMATTERS, STREAM_FORMATTERS, json_default, stream_response
from .ratelimit import KeyRateLimiters, RateLimiter

log = logging.getLogger('queryapigate')
bp = Blueprint('api', __name__)

# Query-string arguments that control a request rather than supplying query parameters.
RESERVED_ARGS = {'format', 'page', 'page_size', 'connection_name', 'version', 'timeout', 'stream'}
PUBLIC_ENDPOINTS = {'api.index', 'api.favicon', 'api.health', 'api.docs', 'api.openapi_spec', 'api.admin_ui',
                    'api.metrics_endpoint'}
RATE_LIMIT_EXEMPT = {'api.health', 'api.metrics_endpoint'}  # monitoring keeps working while a client is throttled
ACCESS_LOG_QUIET = {'api.health', 'api.metrics_endpoint'}  # polled too often to log every hit
SAVED_QUERY_ENDPOINTS = {'api.run_named_query', 'api.execute_sql_from_file'}  # where run_saved() is reached


# A caller-supplied X-Request-Id is accepted only in this shape. Everything that reaches a log line or a history
# entry is therefore plain identifier characters - no whitespace, quotes or control characters to forge a log line
# with - and short enough not to bloat either. Matched with fullmatch(): `$` would also accept a trailing newline.
REQUEST_ID_RE = re.compile(r'[A-Za-z0-9._:-]{1,64}')


def new_request_id(supplied=None):
    """The ID to use for a request: the caller's own ``X-Request-Id`` when it has the accepted shape (so a run can
    be tied to a trace in the caller's system, a UUID or similar), otherwise a fresh 12-character one. An
    unacceptable value is ignored, not rejected - the response header shows which ID was actually used. It is a
    correlation aid only: never an identity, and nothing in the server trusts or authorises by it."""
    if supplied and REQUEST_ID_RE.fullmatch(supplied):
        return supplied
    return uuid.uuid4().hex[:12]


class JSONProvider(DefaultJSONProvider):
    default = staticmethod(json_default)
    sort_keys = False  # keep result columns in the order the query returned them


def load_examples_at_startup():
    """QUERYAPIGATE_LOAD_EXAMPLES=yes: make sure the example APIs are installed. A problem (something of yours
    holds an example's name, the home is read-only) is a warning, not a startup failure - the examples are a
    convenience and must never stop a server from serving what it was really configured for."""
    try:
        added = examples.load()
    except (ApiError, OSError) as error:
        log.warning('QUERYAPIGATE_LOAD_EXAMPLES is set but the examples could not be loaded: %s',
                    getattr(error, 'message', error))
        return
    except Exception as error:  # a convenience must never take the server down, whatever went wrong
        log.warning('QUERYAPIGATE_LOAD_EXAMPLES is set but the examples could not be loaded (unexpected error: %s)',
                    error, exc_info=True)
        return
    if added['connection'] or added['queries'] or added['roles'] or added['key_secrets']:
        store.record_audit('startup', 'load_examples', 'examples', examples.redact_for_audit(added))
        log.info('Loaded the example APIs: %d queries, %d roles, %d API keys', len(added['queries']),
                len(added['roles']), len(added['key_secrets']))
        if added['key_secrets']:
            # There is no interactive terminal at startup to hand these to (unlike `queryapigate examples
            # load`'s own stdout) and a secret is never recoverable once created - the log is the only
            # channel available, the same one every other startup notice here already uses, so this is
            # logged once, now, or it is lost forever. The server now requires authentication for every
            # request, not just the examples - capture these before this container/process's log output
            # rotates away.
            log.warning('Example API keys were just created - store these now, they cannot be shown again: %s',
                       ', '.join(f'{name}={secret}' for name, secret in sorted(added['key_secrets'].items())))


def create_app():
    from . import __version__
    config.check_settings()
    db.init_schema()
    store.import_legacy_data_if_empty()
    apikeys.import_legacy_keys_if_empty()
    apikeys.import_legacy_roles_if_empty()
    logging_setup.configure(log)
    app = Flask(__name__)
    hops = config.proxy_hops()
    if hops:  # behind reverse proxies: take the client address and scheme from their X-Forwarded-* headers
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=hops, x_proto=hops, x_host=hops)
    app.extensions['queryapigate_limiter'] = RateLimiter()
    app.extensions['queryapigate_key_limiter'] = KeyRateLimiters()
    app.extensions['queryapigate_broadcaster'] = broadcast.Broadcaster()
    redis_url = config.redis_url()
    if redis_url:
        from .rediscache import RedisResponseCache
        app.extensions['queryapigate_cache'] = RedisResponseCache(redis_url)
        log.info('Response cache: Redis (%s)', config.redact_redis_url(redis_url))
    else:
        app.extensions['queryapigate_cache'] = cache.ResponseCache()
    if db.is_postgres():
        log.info('Metadata store: %s', db.describe())
        if config.db_file().exists() and db.connection().execute(
                'SELECT 1 FROM connections LIMIT 1').fetchone() is None:
            log.warning('%s holds this home\'s connections, saved queries and keys, but the PostgreSQL metadata '
                        'database is empty - run `queryapigate migrate-to-postgres` to copy them across.',
                        config.db_file())
    if config.cors_origins() == '*' and not config.api_key():
        log.warning('QUERYAPIGATE_CORS_ORIGINS=* without QUERYAPIGATE_API_KEY: any website a user visits can call '
                    'this API from their browser and reach every active connection. Set an API key or list the '
                    'origins.')
    if apikeys.any_configured() and not config.api_key():
        log.warning('Scoped API keys exist but QUERYAPIGATE_API_KEY is not set: no key can manage connections, saved '
                    'queries or other API keys until it is - only a scoped key\'s own allowed connections work.')
    if config.secret_key():
        # Encrypt any literal password already on disk immediately, rather than waiting for its next
        # PATCH /connections - a connection saved before QUERYAPIGATE_SECRET_KEY existed benefits right away.
        store.encrypt_plaintext_passwords_in_place()
    else:
        encrypted = store.encrypted_password_connections()
        if encrypted:
            log.warning('Connection(s) %s have an encrypted password but QUERYAPIGATE_SECRET_KEY is not set - '
                        'they cannot be used until the key that encrypted them is restored.',
                        ', '.join(encrypted))
    plaintext = store.plaintext_password_connections()
    if plaintext:
        log.warning("Connection(s) %s store a literal password in %s. Consider a \"${VAR}\" reference to an "
                    "environment variable instead - it reads the same way but keeps the secret out of the file, "
                    "or set QUERYAPIGATE_SECRET_KEY to encrypt it at rest automatically.",
                    ', '.join(plaintext), config.db_file())
    if config.load_examples():
        load_examples_at_startup()
    app.json = JSONProvider(app)
    app.config['QUERYAPIGATE_VERSION'] = __version__

    @app.errorhandler(ApiError)
    def handle_api_error(error):
        extra = error.extra
        # A database's own error text can name tables, columns, constraints or even row values. A caller of a
        # saved query didn't write its SQL and can't fix it, so a scoped key gets only the generic message;
        # the full text is already in the server log (engine.execute_sql() logs it). The admin key - and
        # /execute_sql, where the caller wrote the SQL and needs the reason - keep `detail`.
        if 'detail' in extra and request.endpoint in SAVED_QUERY_ENDPOINTS:
            permission = g.get('permission')
            if permission is None or not permission.admin:
                extra = {k: v for k, v in extra.items() if k != 'detail'}
        response = jsonify({'error': error.message, **extra})
        if 'retry_after' in extra:
            response.headers['Retry-After'] = str(extra['retry_after'])
        return response, error.status

    @app.errorhandler(HTTPException)
    def handle_http_error(error):
        return jsonify({'error': error.description}), error.code

    @app.errorhandler(Exception)
    def handle_unexpected_error(error):
        log.exception('Unhandled error')
        return jsonify({'error': 'An error occurred'}), 500

    @app.before_request
    def gate():
        # Set first so every request - including one rejected below - gets an ID and a measured duration.
        g.request_id = new_request_id(request.headers.get('X-Request-Id'))
        g.request_started = time.monotonic()
        # Order matters: a browser's preflight cannot carry the API key, and rate limiting comes before the key
        # check so that guessing keys is throttled too.
        if cors.is_preflight(request):
            return cors.preflight_response(request.headers.get('Origin'))
        limited = check_rate_limit()
        if limited is not None:
            return limited
        g.permission = resolve_permission()
        if request.endpoint not in PUBLIC_ENDPOINTS and g.permission is None:
            return jsonify({'error': 'Unauthorized'}), 401
        limited = check_key_rate_limit()
        if limited is not None:
            return limited

    @app.after_request
    def decorate(response):
        cors.add_headers(response, request.headers.get('Origin'))
        if g.get('rate_limit'):
            limit, remaining = g.rate_limit
            response.headers['X-RateLimit-Limit'] = str(limit)
            response.headers['X-RateLimit-Remaining'] = str(remaining)
        if g.get('key_rate_limit'):
            limit, remaining = g.key_rate_limit
            response.headers['X-RateLimit-Key-Limit'] = str(limit)
            response.headers['X-RateLimit-Key-Remaining'] = str(remaining)
        response.headers['X-Request-Id'] = g.get('request_id', '-')
        elapsed = time.monotonic() - g.get('request_started', time.monotonic())
        endpoint = request.endpoint or 'unmatched'
        metrics.observe_request(request.method, endpoint, str(response.status_code), elapsed, caller_key_name())
        cache_status = response.headers.get('X-Cache')
        if cache_status == 'HIT':
            metrics.inc_cache_hit()
        elif cache_status == 'MISS':
            metrics.inc_cache_miss()
        if endpoint not in ACCESS_LOG_QUIET:
            extra = {'method': request.method, 'path': request.path, 'status': response.status_code,
                    'duration_ms': round(elapsed * 1000, 1)}
            if g.get('serialization_ms') is not None:  # only set for a paged response - see render()
                extra['serialization_ms'] = g.serialization_ms
            log.info('%s %s -> %s in %.1fms', request.method, request.path, response.status_code, elapsed * 1000,
                    extra=extra)
        return response

    app.register_blueprint(bp)
    return app


# --------------------------------------------------------------------------------------
# Request helpers
# --------------------------------------------------------------------------------------

def check_rate_limit():
    """Count this request against its client's quota; returns a 429 response when it is over the limit."""
    limit = config.rate_limit()
    if limit is None or request.method == 'OPTIONS' or request.endpoint in RATE_LIMIT_EXEMPT:
        return None
    count, period = limit
    client = request.remote_addr or 'unknown'
    allowed, remaining, retry_after = current_app.extensions['queryapigate_limiter'].hit(client, count, period)
    g.rate_limit = (count, remaining)
    if allowed:
        return None
    metrics.inc_rate_limit_rejection()
    response = jsonify({'error': 'Rate limit exceeded', 'retry_after': retry_after})
    response.status_code = 429
    response.headers['Retry-After'] = str(retry_after)
    return response


def check_key_rate_limit():
    """Like check_rate_limit() above, but for a key's own optional `rate_limit` grant - checked in
    *addition* to the server-wide, IP-based limit, never instead of it (see apikeys.py's module docstring).
    Runs after g.permission is resolved, unlike the IP-based check, since there is no per-key identity to
    limit by before then. Naturally a no-op for the admin key and the open/no-key case, both of which
    always resolve rate_limit=None - no special-casing needed for either."""
    permission = g.get('permission')
    if permission is None or permission.rate_limit is None or request.method == 'OPTIONS' \
            or request.endpoint in RATE_LIMIT_EXEMPT:
        return None
    count, period = permission.rate_limit
    limiter = current_app.extensions['queryapigate_key_limiter']
    allowed, remaining, retry_after = limiter.hit(permission.name, count, period)
    g.key_rate_limit = (count, remaining)
    if allowed:
        return None
    metrics.inc_rate_limit_rejection()
    response = jsonify({'error': "Rate limit exceeded for this API key", 'retry_after': retry_after})
    response.status_code = 429
    response.headers['Retry-After'] = str(retry_after)
    return response


def resolve_permission():
    """The caller's Permission: a matched key (admin or scoped), the unrestricted OPEN default when no key
    is configured anywhere, or None when a key is required but missing or wrong."""
    permission = apikeys.authenticate(request.headers.get('X-API-Key', ''), client_ip=request.remote_addr)
    if permission is not None:
        return permission
    return None if apikeys.auth_required() else apikeys.OPEN


def require_admin():
    """Only the admin key (QUERYAPIGATE_API_KEY, or no key at all when nothing is configured) manages the
    server's own configuration - connections, saved queries and other API keys."""
    if not g.permission.admin:
        raise ApiError('This API key is not authorized to manage the server configuration', 403)


def require_connection(connection_name):
    if not apikeys.can_use(g.permission, connection_name):
        raise ApiError(f"This API key is not permitted to use the connection '{connection_name}'", 403)


def caller_key_name():
    """The calling API key's name for logs/metrics ('admin', a scoped key's name, or '-'); safe to call even
    before permission is resolved, e.g. while rendering a CORS preflight or a 429 in decorate()."""
    permission = g.get('permission')
    return (permission.name if permission else None) or '-'


def _dict_diff(before, after):
    """{field: {'from':.., 'to':..}} for every field that differs between two dicts (union of both sets of
    keys) - used to build audit_log 'changes' for an update. Only the fields that actually changed, not the
    whole entry, so a reviewer doesn't have to spot the difference themselves."""
    keys = set(before or {}) | set(after or {})
    return {k: {'from': (before or {}).get(k), 'to': (after or {}).get(k)}
            for k in keys if (before or {}).get(k) != (after or {}).get(k)}


def _connection_audit_changes(before, after):
    """Like _dict_diff(), but for a connection's raw (unmasked) stored fields specifically: 'password' is
    reported only as the literal string 'changed' when it differs, in either direction - never the actual
    value, before or after masking, since this is what gets persisted to the audit log. Every other field
    (host, port, user, db, database, active, ...) is not a secret and is shown as given. `before=None`
    means the connection didn't exist yet - the caller records that as a 'create_connection' snapshot
    instead of calling this."""
    diff = _dict_diff(before, after)
    if 'password' in diff:
        diff['password'] = 'changed'
    return diff


def get_json_body(required=True):
    data = request.get_json(silent=True)
    if data is None and not required:
        # An optional body may be absent - but a body that was sent as JSON and fails to parse is a client
        # error, not "no parameters": silently treating it as empty turns a typo into a misleading
        # "<param> is required" (or, worse, a run with every parameter at its default).
        if request.is_json and request.get_data(cache=True).strip():
            raise ApiError('Request body is not valid JSON')
        return {}
    if not isinstance(data, dict):
        raise ApiError('Request body must be a JSON object')
    return data


get_object = definitions.as_object  # one definition, shared with saved-query validation


def get_int(value, label):
    if value is None or value == '':
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ApiError(f'{label} must be an integer') from None


def get_pagination():
    """Return (limit, offset, page) from the ?page and ?page_size query parameters."""
    page = get_int(request.args.get('page'), 'page')
    page_size = get_int(request.args.get('page_size'), 'page_size')
    page = 1 if page is None else page
    page_size = 10 if page_size is None else page_size
    if page < 1 or page_size < 1:
        raise ApiError('page and page_size must be positive')
    if page_size > config.max_page_size():
        raise ApiError(f'page_size must not exceed {config.max_page_size()}')
    return page_size, (page - 1) * page_size, page


def get_output_format(body=None):
    output_format = str(request.args.get('format') or (body or {}).get('format') or 'json').lower()
    if output_format not in FORMATTERS:
        raise ApiError(f"Unsupported format '{output_format}'. Supported formats: {', '.join(FORMATTERS)}")
    return output_format


def get_timeout(body=None):
    """Seconds allowed for the query: ?timeout= may lower the server limit but never raise it."""
    raw = request.args.get('timeout', (body or {}).get('timeout'))
    if raw in (None, ''):
        return config.effective_timeout(None)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ApiError('timeout must be a number of seconds') from None
    if not math.isfinite(value) or value <= 0:
        raise ApiError('timeout must be a positive number of seconds')
    return config.effective_timeout(value)


def render(result, output_format, page, page_size):
    """Build the paged response body and record how long that took (JSON/CSV/TSV/XML/YAML/XLSX encoding) as
    its own metric and access-log field, separate from query execution time - see
    metrics.observe_serialization(). Streaming responses never go through here; there's no equivalent
    single serialization span to measure for those (see that function's docstring)."""
    started = time.monotonic()
    response = jsonify({'message': 'No results returned'}) if not result else FORMATTERS[output_format](result)
    elapsed = time.monotonic() - started
    metrics.observe_serialization(output_format, elapsed)
    g.serialization_ms = round(elapsed * 1000, 1)
    response.headers['X-Page'] = str(page)
    response.headers['X-Page-Size'] = str(page_size)
    response.headers['X-Has-More'] = 'true' if result.has_more else 'false'
    return response


def get_stream_flag():
    """Whether ?stream=true was requested: the whole result, streamed as it comes off the cursor instead of
    one page built in memory first. Only csv/tsv/ndjson support it - see formats.STREAM_FORMATTERS."""
    return (request.args.get('stream') or '').strip().lower() in ('1', 'true', 'yes', 'on')


def stream_sql_response(sql, connection_name, params, timeout, output_format, filename, saved=None,
                        allowed_tables=None):
    """Shared by execute_sql_endpoint() and run_saved(): validates the stream=true-specific constraints
    (format, no pagination) and returns the chunked Response. ``saved``, when given, is (path, version) for
    a saved query whose run should still be recorded in its execution_history once the stream finishes."""
    if output_format not in STREAM_FORMATTERS:
        raise ApiError(f"stream=true only supports these formats: {', '.join(sorted(STREAM_FORMATTERS))}")
    if request.args.get('page') or request.args.get('page_size'):
        raise ApiError('stream=true exports the whole result and does not accept page/page_size')
    key_name = caller_key_name()
    columns, rows = engine.stream_sql(sql, connection_name, params, timeout, key_name=key_name,
                                      allowed_tables=allowed_tables)
    if saved is not None:
        path, number = saved
        # Captured here, not inside _record_stream_history(): that generator's body runs lazily, as the
        # response streams out - by then the request/app context this view function runs in is long gone
        # (no flask.stream_with_context() wrapping is used), so g, caller_key_name() and current_app are
        # only safe to read up front, while still inside the request that's actually issuing the query -
        # this is why the broadcaster instance itself is captured here too, not looked up inside the
        # generator the way _record_and_broadcast()'s other callers do.
        broadcaster = current_app.extensions['queryapigate_broadcaster']
        rows = _record_stream_history(rows, path, number, connection_name, g.get('request_id'), key_name,
                                      broadcaster)
    return stream_response(output_format, columns, rows, filename)


def _record_and_broadcast(path, number, connection_name, entry, broadcaster=None):
    """store.record_execution(), plus a live event for the admin UI's Home tab and any subscribed API key's
    own personal feed (BACKLOG #43) - every call site that used to call record_execution() directly calls
    this instead. ``broadcaster`` defaults to the current request's extension (the normal case); a caller
    running outside request/app context (see _record_stream_history() below) must pass it in explicitly
    instead. Published under ``entry['key_name']`` - the same value every subscriber's own filter (see
    stream_events()) is compared against, so a run only ever reaches the key that actually triggered it,
    plus the admin key's own unfiltered subscription."""
    store.record_execution(path, number, entry)
    if broadcaster is None:
        broadcaster = current_app.extensions['queryapigate_broadcaster']
    broadcaster.publish({'type': 'execution', 'filename': path, 'version': number,
                        'connection_name': connection_name, 'entry': entry}, entry['key_name'])


def _record_stream_history(rows, path, number, connection_name, request_id, key_name, broadcaster):
    """Records a saved query's streamed run in its execution_history once fully drained or failed partway
    through (not on a client disconnect, GeneratorExit) - counting rows as they pass through, since the
    total is not known up front, the same trade-off engine._drain() makes for the streaming metric."""
    entry = {'executed_at': store.now(), 'connection_name': connection_name,
             'request_id': request_id, 'key_name': key_name}
    count = 0
    try:
        for row in rows:
            count += 1
            yield row
    except GeneratorExit:
        raise
    except ApiError as error:
        _record_and_broadcast(path, number, connection_name,
                              {**entry, 'status': 'error', 'error': error.message, 'rows': count}, broadcaster)
        raise
    except Exception as error:
        _record_and_broadcast(path, number, connection_name,
                              {**entry, 'status': 'error', 'error': str(error), 'rows': count}, broadcaster)
        raise
    else:
        _record_and_broadcast(path, number, connection_name, {**entry, 'status': 'success', 'rows': count},
                              broadcaster)


# --------------------------------------------------------------------------------------
# Query execution
# --------------------------------------------------------------------------------------

@bp.route('/execute_sql', methods=['POST'])
def execute_sql_endpoint():
    data = get_json_body()
    if not data.get('sql'):
        raise ApiError('SQL query is missing')
    if not data.get('connection_name'):
        raise ApiError('Connection name is missing')
    require_connection(data['connection_name'])
    database = data.get('database')
    if database and not g.permission.admin:
        # Every other scoped-key boundary is per connection, never per database within one - a key granted a
        # connection may run any SQL that connection's own configured database allows, but not redirect that
        # same connection at a sibling database on the same server the admin never listed it for.
        raise ApiError('Only the admin key may run a query against a different database on this connection', 403)
    params = get_object(data.get('params'), 'params')
    output_format = get_output_format(data)
    timeout = get_timeout(data)
    if get_stream_flag():
        return stream_sql_response(data['sql'], data['connection_name'], params, timeout, output_format,
                                   filename=data['connection_name'], allowed_tables=g.permission.allowed_tables)
    limit, offset, page = get_pagination()
    result = engine.execute_sql(data['sql'], data['connection_name'], limit, offset, params, timeout,
                                allow_writes=g.permission.allow_writes, key_name=caller_key_name(),
                                allowed_write_ops=g.permission.allowed_write_ops, database=database,
                                allowed_tables=g.permission.allowed_tables)
    return render(result, output_format, page, limit)


@bp.route('/execute_mongo', methods=['POST'])
def execute_mongo_endpoint():
    """The Mongo sibling of /execute_sql - a find() query, ad-hoc, against a saved connection. Read-only,
    full stop (BACKLOG #36: MongoDB support is find-only in this version) - unlike /execute_sql there is no
    write permission to check."""
    data = get_json_body()
    if not data.get('collection'):
        raise ApiError('Collection is missing')
    if not data.get('connection_name'):
        raise ApiError('Connection name is missing')
    require_connection(data['connection_name'])
    filter_doc = mongotools.validate_filter(get_object(data.get('filter'), 'filter'))
    params = get_object(data.get('params'), 'params')
    output_format = get_output_format(data)
    timeout = get_timeout(data)
    limit, offset, page = get_pagination()
    result = engine.execute_mongo(data['collection'], filter_doc, data['connection_name'], limit, offset,
                                  params, timeout, projection=data.get('projection'), sort=data.get('sort'),
                                  key_name=caller_key_name())
    return render(result, output_format, page, limit)


def cache_lookup(cache_key, ttl):
    """A live entry rendered as a response (304 if the client already has it), or None on a cache miss."""
    hit = current_app.extensions['queryapigate_cache'].get(cache_key)
    if hit is None:
        return None
    body, content_type, headers, etag = hit
    quoted = f'"{etag}"'
    if request.headers.get('If-None-Match') == quoted:
        response = Response(status=304)
    else:
        response = Response(body, content_type=content_type)
        for name, value in headers:
            response.headers[name] = value
    response.headers['ETag'] = quoted
    response.headers['Cache-Control'] = f'max-age={ttl}'
    response.headers['X-Cache'] = 'HIT'
    return response


def cache_store(cache_key, response, ttl, meta=None):
    """Store `response` under `cache_key` for `ttl` seconds and tag it as a fresh cache MISS. `meta`
    (name/version/connection/format/page) is never consulted to serve a hit - it exists purely for the
    admin UI's cache browser, see cache.py's ResponseCache.set() docstring."""
    replay_headers = [(name, value) for name, value in response.headers.items()
                      if name.lower() not in ('content-type', 'content-length')]
    etag = current_app.extensions['queryapigate_cache'].set(cache_key, response.get_data(), response.content_type,
                                                       replay_headers, ttl, meta)
    response.headers['ETag'] = f'"{etag}"'
    response.headers['Cache-Control'] = f'max-age={ttl}'
    response.headers['X-Cache'] = 'MISS'
    return response


def run_saved(ref, body, url_params):
    """Execute a saved query (latest version unless one is requested) and record the run - or, for one with
    a cache_ttl whose SQL is read-only, serve a cached response instead."""
    path = store.resolve_saved_file(ref)
    content = store.load_versions(path, with_history=False)
    number, saved = store.select_version(content, get_int(request.args.get('version') or body.get('version'),
                                                            'version'))
    connection_name = request.args.get('connection_name') or body.get('connection_name') \
        or saved.get('connection_name')
    if not connection_name:
        raise ApiError('Connection name is missing')
    query_name = store.query_name(path)
    # A key's `queries` and `collections` grants are additive on top of `connections` (see apikeys.py's module
    # docstring): either can reach this exact saved query without any connection access - but only on the
    # connection the query itself names. Letting a caller choose another with ?connection_name= would turn
    # "may run this one curated query" into "may run this query's SQL against any connection on the server",
    # so a different (or, for a query with no default, any) connection still needs a real connection grant.
    # Whether the query is reachable at all is decided in one place, shared with /openapi.json and /catalog.
    granted_by_name = (apikeys.can_use_query(g.permission, query_name)
                       or apikeys.can_use_collection(g.permission, store.read_collection(content)))
    if not (granted_by_name and connection_name == saved.get('connection_name')):
        require_connection(connection_name)

    raw = {**url_params, **get_object(body.get('params'), 'params'),
           **get_object(body.get('placeholders'), 'placeholders')}
    output_format = get_output_format(body)
    timeout = get_timeout(body)

    if saved.get('query_type') == 'mongo':
        return run_saved_mongo(saved, path, number, ref, connection_name, raw, output_format, timeout)

    if not isinstance(saved.get('sql_query'), str):
        raise ApiError('Saved query has no SQL', 500)
    # Per-query write curation (apikeys.can_write_query()) only ever adds write reach for this one named
    # query on top of whatever the key's blanket allow_writes already grants - never the other way round.
    effective_allow_writes = g.permission.allow_writes or apikeys.can_write_query(g.permission, query_name)

    used = set(sqltools.placeholder_names(saved['sql_query']))
    values = param_rules.resolve(saved.get('query_parameters'), raw, used=used)
    sql = sqltools.fill_placeholders(saved['sql_query'], values)

    if get_stream_flag():
        # No caching for a streamed export - caching would require materialising the whole body anyway,
        # defeating the point - but the run is still recorded once the stream finishes, same as any other.
        return stream_sql_response(sql, connection_name, values, timeout, output_format, filename=ref,
                                   saved=(path, number), allowed_tables=g.permission.allowed_tables)

    limit, offset, page = get_pagination()

    # A saved query is only ever cached when it declares a cache_ttl *and* its SQL is read-only - never a
    # write, no matter the setting, since serving a cached response would silently skip that write.
    ttl = saved.get('cache_ttl') or 0
    cache_key = None
    if ttl > 0:
        dialect = store.get_connection(connection_name)['db']
        if sqltools.first_keyword(sql, dialect) in sqltools.READ_ONLY_STATEMENTS:
            cache_key = cache.ResponseCache.key(name=ref, version=number, connection=connection_name,
                                                values=values, format=output_format, page=page, page_size=limit)
            cache_meta = {'name': path, 'version': number, 'connection': connection_name,
                         'format': output_format, 'page': page}
            cached = cache_lookup(cache_key, ttl)
            if cached is not None:
                return cached

    entry = {'executed_at': store.now(), 'connection_name': connection_name,
             'request_id': g.get('request_id'), 'key_name': caller_key_name()}
    try:
        result, elapsed_ms = engine.timed(engine.execute_sql, sql, connection_name, limit, offset, values, timeout,
                                          allow_writes=effective_allow_writes, key_name=caller_key_name(),
                                          allowed_write_ops=g.permission.allowed_write_ops,
                                          allowed_tables=g.permission.allowed_tables)
    except ApiError as error:
        _record_and_broadcast(path, number, connection_name, {**entry, 'status': 'error', 'error': error.message})
        raise
    response = render(result, output_format, page, limit)  # sets g.serialization_ms - see render()
    _record_and_broadcast(path, number, connection_name, {**entry, 'status': 'success', 'rows': len(result.rows),
                                                          'duration_ms': elapsed_ms,
                                                          'serialization_ms': g.serialization_ms})
    if cache_key is not None:
        response = cache_store(cache_key, response, ttl, cache_meta)
    return response


def run_saved_mongo(saved, path, number, ref, connection_name, raw, output_format, timeout):
    """The mongo query_type branch of run_saved(): a find() query instead of SQL. No caching (cache_ttl is
    not yet supported for a mongo saved query) and no streaming (?stream=true) in this version - see
    BACKLOG #36. Always read-only, same as the ad-hoc /execute_mongo."""
    if get_stream_flag():
        raise ApiError('Streaming is not supported for Mongo queries yet', 400)
    used = set(mongotools.placeholder_names(saved.get('mongo_filter') or {}))
    values = param_rules.resolve(saved.get('query_parameters'), raw, used=used)
    filter_doc = mongotools.fill_placeholders(saved.get('mongo_filter') or {}, values)
    limit, offset, page = get_pagination()
    entry = {'executed_at': store.now(), 'connection_name': connection_name,
             'request_id': g.get('request_id'), 'key_name': caller_key_name()}
    try:
        result, elapsed_ms = engine.timed(engine.execute_mongo, saved['mongo_collection'], filter_doc,
                                          connection_name, limit, offset, timeout=timeout,
                                          projection=saved.get('mongo_projection'), sort=saved.get('mongo_sort'),
                                          key_name=caller_key_name())
    except ApiError as error:
        _record_and_broadcast(path, number, connection_name, {**entry, 'status': 'error', 'error': error.message})
        raise
    response = render(result, output_format, page, limit)  # sets g.serialization_ms - see render()
    _record_and_broadcast(path, number, connection_name, {**entry, 'status': 'success', 'rows': len(result.rows),
                                                          'duration_ms': elapsed_ms,
                                                          'serialization_ms': g.serialization_ms})
    return response


@bp.route('/execute_sql_from_file', methods=['POST'])
@bp.route('/execute_sql_with_parameters_from_file', methods=['POST'])
def execute_sql_from_file():
    body = get_json_body()
    return run_saved(body.get('filepath'), body, {})


@bp.route('/q/<name>', methods=['GET', 'POST'])
def run_named_query(name):
    body = get_json_body(required=False) if request.method == 'POST' else {}
    url_params = {k: v for k, v in request.args.items() if k not in RESERVED_ARGS}
    return run_saved(name, body, url_params)


# --------------------------------------------------------------------------------------
# Saved queries
# --------------------------------------------------------------------------------------

@bp.route('/view_file_content', methods=['GET'])
def view_file_content():
    """The admin UI's "Show raw file" toggle. No real file exists once saved queries live in SQLite, so
    this reconstructs the same JSON text a saved-query file always looked like (same shape
    store.load_versions() returns, same indent=4 the old write_json_atomic() used) - the UI already just
    JSON.parses this string and reads it the normal way, so it can't tell the difference."""
    require_admin()
    path = store.resolve_saved_file(request.args.get('filename'))
    content = store.load_versions(path)
    return jsonify({'content': json.dumps(content, indent=4)}), 200


@bp.route('/query_flow', methods=['GET'])
def query_flow():
    """Best-effort tables/joins a saved query's SQL touches - see sqlflow.py. Never raises: a query this
    can't analyze (unsupported dialect, a Mongo query, a parse failure) comes back with an `error` string
    and empty tables/joins for the UI to show as a plain hint, not a broken panel."""
    require_admin()
    path = store.resolve_saved_file(request.args.get('filename'))
    content = store.load_versions(path)
    _, saved = store.select_version(content, get_int(request.args.get('version'), 'version'))
    if saved.get('query_type') == 'mongo' or not isinstance(saved.get('sql_query'), str):
        return jsonify({'tables': [], 'joins': [], 'formatted': None,
                        'error': "SQL analysis isn't available for this query"}), 200
    connection_name = saved.get('connection_name')
    dialect = store.get_connection(connection_name)['db'] if connection_name else None
    return jsonify(sqlflow.extract_flow(saved['sql_query'], dialect)), 200


@bp.route('/save_sql_to_file', methods=['PATCH'])
def save_sql_to_file():
    require_admin()
    data = get_json_body()
    fields, collection = definitions.validate_definition(data)
    query_uuid, version = store.save_version(data['filename'], fields, collection)
    audit = {'version': version, 'connection_name': fields.get('connection_name'), 'author': data['author']}
    if collection is not definitions.NO_COLLECTION_GIVEN:
        audit['collection'] = collection
    store.record_audit(caller_key_name(), 'save_query', data['filename'], audit)
    return jsonify({'message': 'SQL query saved successfully', 'filename': data['filename'],
                    'uuid': query_uuid, 'version': version}), 200


@bp.route('/saved_sql/<name>', methods=['DELETE'])
def delete_saved_query(name):
    require_admin()
    version = get_int(request.args.get('version'), 'version')
    store.delete_saved(name, version)
    store.record_audit(caller_key_name(), 'delete_query', name,
                       {'version': version} if version is not None else None)
    what = f'Version {version} of {name}' if version is not None else name
    return jsonify({'message': f'{what} deleted'}), 200


@bp.route('/saved_sql/<name>/collection', methods=['PUT'])
def move_query(name):
    """File a saved query under a collection (``{"collection": "name"}``) or take it out of any
    (``{"collection": null}``). Not a new version. Because a key's collection grant is live, this is the one
    edit that can change what other keys can reach without touching them - so the audit entry records exactly
    which keys and roles gain or lose access, and so does the response."""
    require_admin()
    data = get_json_body()
    if 'collection' not in data:
        raise ApiError('collection is missing (use null to remove the query from its collection)')
    path = store.resolve_saved_file(name)
    query = store.query_name(path)
    previous = store.set_collection(name, data['collection'])
    current = data['collection']
    access = collection_admin.access_change(previous, current)
    if previous != current:
        # Flat lists (not the nested `access` object the response carries) so the audit view can show each on
        # its own line; empty ones are thinned out of a snapshot like any other unset field.
        store.record_audit(caller_key_name(), 'move_query', query,
                           {'collection': {'from': previous, 'to': current},
                            'keys_gaining_access': access['keys']['gain'],
                            'keys_losing_access': access['keys']['lose'],
                            'roles_now_including': access['roles']['gain'],
                            'roles_no_longer_including': access['roles']['lose']})
    return jsonify({'message': f"'{query}' moved", 'filename': query, 'from': previous, 'to': current,
                    'access': access}), 200


@bp.route('/saved_sql/<name>/cache_ttl', methods=['PUT'])
def set_query_cache_ttl(name):
    """Set (``{"cache_ttl": <seconds>}``) or clear (``0`` or ``null``) one version's cache_ttl in place -
    not a new version, same treatment as move_query() above for a query's collection. ?version= targets a
    specific version; omitted means the latest, same default select_version() itself uses."""
    require_admin()
    data = get_json_body()
    if 'cache_ttl' not in data:
        raise ApiError('cache_ttl is missing (use 0 or null to turn caching off)')
    ttl = data['cache_ttl']
    definitions.validate_cache_ttl(ttl)
    version = get_int(request.args.get('version'), 'version')
    path = store.resolve_saved_file(name)
    query = store.query_name(path)
    number = store.set_cache_ttl(name, version, ttl)
    store.record_audit(caller_key_name(), 'set_cache_ttl', query, {'version': number, 'cache_ttl': ttl or None})
    return jsonify({'message': f"'{query}' v{number} cache_ttl updated", 'filename': query, 'version': number,
                    'cache_ttl': ttl or None}), 200


@bp.route('/collections', methods=['GET'])
def get_collections():
    require_admin()
    return jsonify(collection_admin.describe()), 200


@bp.route('/examples', methods=['GET'])
def get_examples():
    require_admin()
    return jsonify(examples.status()), 200


@bp.route('/examples', methods=['POST'])
def load_examples():
    """Install the example APIs (see examples.py). Idempotent; 409 and nothing changed if something that is not an
    example already holds one of their names."""
    require_admin()
    added = examples.load()
    if added['connection'] or added['queries'] or added['roles'] or added['key_secrets']:
        store.record_audit(caller_key_name(), 'load_examples', 'examples', examples.redact_for_audit(added))
    return jsonify({'message': 'Examples loaded', **added, **examples.status()}), 200


@bp.route('/examples', methods=['DELETE'])
def unload_examples():
    """Remove exactly what is marked as an example - nothing else."""
    require_admin()
    removed = examples.unload()
    if removed['connection'] or removed['queries'] or removed['roles'] or removed['keys']:
        store.record_audit(caller_key_name(), 'unload_examples', 'examples', removed)
    return jsonify({'message': 'Examples removed', **removed}), 200


@bp.route('/collections/<name>/postman', methods=['GET'])
def collection_postman(name):
    """The collection as a Postman Collection v2.1 file (a download). Admin only; the base URL is the one this
    request arrived on, and the file holds no key - see postman.py."""
    require_admin()
    document = postman.build_collection(name, request.host_url)
    response = jsonify(document)
    response.headers['Content-Disposition'] = f'attachment; filename="{name}.postman_collection.json"'
    return response, 200


@bp.route('/collections/<name>', methods=['PATCH'])
def rename_collection(name):
    require_admin()
    data = get_json_body()
    new = data.get('name')
    if not isinstance(new, str):
        raise ApiError('name (the new collection name) is missing')
    merge = data.get('merge', False)
    if not isinstance(merge, bool):
        raise ApiError('merge must be true or false')
    result = collection_admin.rename_collection(name, new, merge=merge)
    store.record_audit(caller_key_name(), 'rename_collection', name, {'to': new, **result})
    return jsonify({'message': f"Collection '{name}' renamed to '{new}'", **result}), 200


@bp.route('/list_files', methods=['GET'])
def list_files():
    require_admin()
    sort_by = request.args.get('sort_by', 'name')
    if sort_by not in ('name', 'modified'):
        raise ApiError("sort_by must be 'name' or 'modified'")
    sort_order = request.args.get('sort_order', 'asc')
    if sort_order not in ('asc', 'desc'):
        raise ApiError("sort_order must be 'asc' or 'desc'")

    files = store.list_saved()
    if sort_by == 'name':
        def key(f):
            return f['filename'].lower()
    else:
        def key(f):
            return (f['versions'][-1].get('last_modified_at') or '') if f['versions'] else ''
    files.sort(key=key, reverse=sort_order == 'desc')
    return jsonify({'files': files}), 200


_SSE_HEARTBEAT_SECONDS = 15  # module constant so tests can shrink it
_open_streams = 0  # GET /events streams this process holds open right now - see stream_events()
_open_streams_lock = threading.Lock()


@bp.route('/events', methods=['GET'])
def stream_events():
    """Server-Sent Events: one `data: {...}` line per live event (today: a saved-query execution, as
    it's recorded - see _record_and_broadcast()). Originally admin-only, feeding just the admin UI's Home
    tab; open to any authenticated key now, each getting its own personal activity feed - a scoped key sees
    only executions triggered by that same key (broadcast.Broadcaster's key_name filter), the admin key
    keeps its original unfiltered view of everything, matching the admin-sees-all/scoped-sees-its-own
    pattern /catalog already uses elsewhere. Still requires a real key or open-access mode - never public -
    replaces polling /list_files every 5s for the admin UI's own panel.

    The client reads this with fetch()'s streamed response body rather than a plain `new EventSource(...)`:
    EventSource cannot set custom request headers, and this app has no cookie-based auth to fall back on -
    every other request already authenticates via X-API-Key (see ui.py's apiFetch()). Putting the key in the
    URL instead would put a secret in server access logs and browser history, which nothing else in this
    app does. Same wire format either way, just read manually so header-based auth keeps working.

    Each open stream occupies one of this server's request threads for as long as it stays open, so at most
    config.events_max_streams() are held at once (4 by default, of the Docker image's 8 threads); beyond that
    this answers 503 rather than let streams starve every other request. Many clients - apps, phones - belong on
    `queryapigate events` (events.py) instead: a separate asyncio process with no such limit, every instance's
    runs, and Last-Event-ID resume."""
    global _open_streams
    with _open_streams_lock:
        if _open_streams >= config.events_max_streams():
            raise ApiError('Too many open event streams on this server - try again shortly, or connect to '
                           '`queryapigate events` instead', 503, retry_after=5)
        _open_streams += 1
    broadcaster = current_app.extensions['queryapigate_broadcaster']
    subscriber = broadcaster.subscribe(key_name=None if g.permission.admin else g.permission.name)

    released = []

    def release():
        # Runs from the generator's own finally, or from the WSGI server closing a response whose generator never
        # started (the client left before the first byte) - whichever comes first, exactly once.
        global _open_streams
        with _open_streams_lock:
            if released:
                return
            released.append(True)
            _open_streams -= 1
        broadcaster.unsubscribe(subscriber)

    def events():
        try:
            while True:
                try:
                    event = subscriber.get(timeout=_SSE_HEARTBEAT_SECONDS)
                except queue.Empty:
                    yield ': keepalive\n\n'
                    continue
                yield f'data: {json.dumps(event, default=json_default)}\n\n'
        finally:
            release()

    response = Response(ClosingIterator(events(), release), mimetype='text/event-stream')
    response.headers['Cache-Control'] = 'no-cache'
    response.headers['X-Accel-Buffering'] = 'no'  # disable reverse-proxy response buffering (nginx, etc.)
    return response


# --------------------------------------------------------------------------------------
# Connections
# --------------------------------------------------------------------------------------

@bp.route('/connections', methods=['GET'])
def get_connections():
    require_admin()
    connections = store.mask_passwords(store.read_connections())
    for name, conn in connections.items():
        conn['usage'] = metrics.summary_for_connection(name)
    return jsonify({'connections': connections}), 200


@bp.route('/connections', methods=['PATCH'])
def update_connections():
    require_admin()
    connections = get_json_body().get('connections')
    if not connections or not isinstance(connections, dict):
        raise ApiError('Connections data is missing')
    before = store.read_connections()  # raw, unmasked - in memory only, never itself logged; see below
    store.update_connections(connections)
    after = store.read_connections()
    actor = caller_key_name()
    for name in connections:
        if name not in before:
            store.record_audit(actor, 'create_connection', name, store.mask_passwords({name: after[name]})[name])
        else:
            changes = _connection_audit_changes(before.get(name), after.get(name))
            if changes:
                store.record_audit(actor, 'update_connection', name, changes)
    pool.close_pooled_connections()  # new settings or credentials must not be served by old connections
    return jsonify({'message': 'Connections updated successfully'}), 200


@bp.route('/connections/databases', methods=['POST'])
def list_databases_route():
    """Every database on the server a connection points at - saved, or not yet saved. Give 'name' alone for
    an already-saved connection (Run SQL's database picker); give the connection's own fields (as
    /connections/test does) to probe one that isn't saved yet - the New/Edit connection form's 'Default
    database' dropdown - or to try changed fields before saving them. Admin only, like every other endpoint
    that reveals the server's own configuration or reaches into a connection's server ahead of a real query."""
    require_admin()
    body = get_json_body()
    if not isinstance(body, dict):
        raise ApiError('Connection fields are missing')
    if body.get('name') and not body.get('db'):
        databases = schema.list_databases(body['name'])
    else:
        if body.get('db') not in config.SUPPORTED_DB_TYPES:
            raise ApiError(f"'db' must be one of: {', '.join(config.SUPPORTED_DB_TYPES)}")
        if body.get('password') == config.PASSWORD_MASK:
            existing = store.read_connections().get(body.get('name') or '', {})
            body = {**body, 'password': existing.get('password', '')}
        databases = engine.list_databases(store.resolve_ad_hoc(body))
    return jsonify({'databases': databases}), 200


@bp.route('/connections/test', methods=['POST'])
def test_connection_route():
    """Try to connect with the given fields - not a saved connection's name, the fields themselves, exactly
    as the New/Edit connection form has them right now. Nothing is written anywhere; a masked password
    (unchanged from an existing connection's form) is resolved to the real stored value first, the same way
    update_connections() already treats one."""
    require_admin()
    details = get_json_body()
    if not isinstance(details, dict) or details.get('db') not in config.SUPPORTED_DB_TYPES:
        raise ApiError(f"'db' must be one of: {', '.join(config.SUPPORTED_DB_TYPES)}")
    if details.get('password') == config.PASSWORD_MASK:
        existing = store.read_connections().get(details.get('name') or '', {})
        details = {**details, 'password': existing.get('password', '')}
    result = engine.test_connection(store.resolve_ad_hoc(details))
    return jsonify({'message': 'Connected', **result}), 200


@bp.route('/connections/<name>', methods=['DELETE'])
def delete_connection(name):
    """Deleting a connection is destructive and hard to reason about after the fact - every saved query that
    used it stops working - so the caller must say why. The reason is stored nowhere but the audit log (there
    is no separate "deleted connections" table); the admin UI's own Deleted tab reads it straight from there,
    the same as everything else on this route already does."""
    require_admin()
    before = store.read_connections().get(name)
    if before is None:
        raise ApiError(f"Connection '{name}' not found", 404)
    data = get_json_body(required=False)
    reason = (data.get('reason') or '').strip()
    if not reason:
        raise ApiError('A reason is required to delete a connection')
    store.delete_connection(name)
    changes = {**store.mask_passwords({name: before})[name], 'deleted_reason': reason}
    store.record_audit(caller_key_name(), 'delete_connection', name, changes)
    pool.close_pooled_connections()  # a removed connection must not keep serving from idle sockets
    return jsonify({'message': f"Connection '{name}' deleted"}), 200


@bp.route('/connections/<name>/schema', methods=['GET'])
def connection_schema(name):
    require_connection(name)
    database = request.args.get('database') or None
    if database and not g.permission.admin:
        raise ApiError('Only the admin key may browse a different database on this connection', 403)
    return jsonify(schema.fetch_schema(name, database=database)), 200


@bp.route('/connections/<name>/table_ddl', methods=['GET'])
def connection_table_ddl(name):
    """A table's real CREATE TABLE text (BACKLOG #38) - not available for every dialect, see
    schema.fetch_table_ddl(). Same permission level as browsing the schema itself."""
    require_connection(name)
    table = request.args.get('table')
    if not table:
        raise ApiError('table is required')
    database = request.args.get('database') or None
    if database and not g.permission.admin:
        raise ApiError('Only the admin key may browse a different database on this connection', 403)
    return jsonify(schema.fetch_table_ddl(name, table, database=database)), 200


# --------------------------------------------------------------------------------------
# API keys
# --------------------------------------------------------------------------------------

@bp.route('/api_keys', methods=['GET'])
def get_api_keys():
    require_admin()
    keys = apikeys.list_keys()
    for name, key in keys.items():
        key['usage'] = metrics.summary_for_key(name)
    return jsonify({'keys': keys}), 200


@bp.route('/api_keys', methods=['POST'])
def create_api_key():
    require_admin()
    data = get_json_body()
    secret = apikeys.create_key(data.get('name'), connections=data.get('connections'),
                                allow_writes=data.get('allow_writes'), queries=data.get('queries'),
                                expires_at=data.get('expires_at'), rate_limit=data.get('rate_limit'),
                                allowed_ips=data.get('allowed_ips'),
                                allowed_write_ops=data.get('allowed_write_ops'), role=data.get('role'),
                                collections=data.get('collections'), allowed_tables=data.get('allowed_tables'))
    store.record_audit(caller_key_name(), 'create_key', data.get('name'), apikeys.list_keys().get(data.get('name')))
    return jsonify({'name': data.get('name'), 'key': secret,
                    'message': "Store this key now - it can't be shown again."}), 200


@bp.route('/api_keys/<name>', methods=['PATCH'])
def update_api_key(name):
    require_admin()
    data = get_json_body()
    before = apikeys.list_keys().get(name)
    # expires_at, rate_limit, allowed_ips and allowed_write_ops all need a real presence check, not .get():
    # an explicit null in the request body means "clear it", which must be distinguishable from the field
    # being absent ("leave it alone") - see apikeys.update_key's _UNSET sentinel.
    unset_kwargs = {k: data[k] for k in
                    ('expires_at', 'rate_limit', 'allowed_ips', 'allowed_write_ops', 'allowed_tables')
                    if k in data}
    apikeys.update_key(name, connections=data.get('connections'), allow_writes=data.get('allow_writes'),
                       active=data.get('active'), queries=data.get('queries'),
                       collections=data.get('collections'), **unset_kwargs)
    changes = _dict_diff(before, apikeys.list_keys().get(name))
    if changes:
        store.record_audit(caller_key_name(), 'update_key', name, changes)
    return jsonify({'message': f"API key '{name}' updated"}), 200


@bp.route('/api_keys/<name>', methods=['DELETE'])
def delete_api_key(name):
    require_admin()
    before = apikeys.list_keys().get(name)
    apikeys.delete_key(name)
    store.record_audit(caller_key_name(), 'delete_key', name, before)
    return jsonify({'message': f"API key '{name}' deleted"}), 200


@bp.route('/roles', methods=['GET'])
def get_roles():
    require_admin()
    return jsonify({'roles': apikeys.list_roles()}), 200


@bp.route('/roles', methods=['POST'])
def create_role_endpoint():
    require_admin()
    data = get_json_body()
    apikeys.create_role(data.get('name'), connections=data.get('connections'),
                        allow_writes=bool(data.get('allow_writes', False)), queries=data.get('queries'),
                        rate_limit=data.get('rate_limit'), allowed_ips=data.get('allowed_ips'),
                        allowed_write_ops=data.get('allowed_write_ops'), collections=data.get('collections'),
                        allowed_tables=data.get('allowed_tables'))
    store.record_audit(caller_key_name(), 'create_role', data.get('name'), apikeys.list_roles().get(data.get('name')))
    return jsonify({'message': f"Role '{data.get('name')}' created"}), 200


@bp.route('/roles/<name>', methods=['PATCH'])
def update_role_endpoint(name):
    require_admin()
    data = get_json_body()
    before = apikeys.list_roles().get(name)
    # Same _UNSET-sentinel presence check update_api_key() already uses for these three fields.
    unset_kwargs = {k: data[k] for k in ('rate_limit', 'allowed_ips', 'allowed_write_ops', 'allowed_tables')
                    if k in data}
    apikeys.update_role(name, connections=data.get('connections'), allow_writes=data.get('allow_writes'),
                        queries=data.get('queries'), collections=data.get('collections'), **unset_kwargs)
    changes = _dict_diff(before, apikeys.list_roles().get(name))
    if changes:
        store.record_audit(caller_key_name(), 'update_role', name, changes)
    return jsonify({'message': f"Role '{name}' updated"}), 200


@bp.route('/roles/<name>', methods=['DELETE'])
def delete_role_endpoint(name):
    require_admin()
    before = apikeys.list_roles().get(name)
    apikeys.delete_role(name)
    store.record_audit(caller_key_name(), 'delete_role', name, before)
    return jsonify({'message': f"Role '{name}' deleted"}), 200


@bp.route('/audit_log', methods=['GET'])
def audit_log_endpoint():
    """A durable record of administrative changes - who created, changed or removed an API key, connection
    or saved query, and when (see store.record_audit()). Admin only, like everything else that reveals the
    server's own configuration; newest entry first, capped at config.audit_log_limit()."""
    require_admin()
    return jsonify({'entries': list(reversed(store.read_audit_log()))}), 200


@bp.route('/history', methods=['GET'])
def history_endpoint():
    """Every stored run of every saved query, newest first and paged - for looking past the newest runs per
    version that /list_files and the admin UI show, e.g. "every failed call by the partner key last week" over
    a retention period's worth of history (QUERYAPIGATE_HISTORY_RETENTION_DAYS). Filters: query, version,
    status (success|error), key, since (inclusive) and until (exclusive) - a date or a time. Admin only: it
    reveals every key's activity. Pass the response's `next` back as ?cursor= for the following page."""
    require_admin()
    args = request.args
    limit = get_int(args.get('limit'), 'limit')
    entries, next_cursor = history.search(
        query=args.get('query'), version=get_int(args.get('version'), 'version'), status=args.get('status'),
        key=args.get('key'), since=args.get('since'), until=args.get('until'),
        limit=100 if limit is None else limit, cursor=args.get('cursor'))
    return jsonify({'entries': entries, 'next': next_cursor}), 200


@bp.route('/settings', methods=['GET'])
def settings_endpoint():
    """The server's own configuration as the admin UI's Settings screen shows it: every environment variable
    that matters, its effective value and whether it was set or is the default. Read-only, and admin only
    like the rest of what reveals the server's configuration. Secrets are reported as configured or not,
    never returned."""
    require_admin()
    return jsonify({'sections': config.describe_settings()}), 200


@bp.route('/settings/mcp_status', methods=['GET'])
def mcp_status_endpoint():
    """An on-demand reachability probe for the separate `queryapigate mcp` process (BACKLOG #54's own
    still-open "no reachability check" note) - a plain TCP connect attempt against its configured port, not
    a full MCP handshake, so it costs nothing beyond a bounded-timeout socket connect. Deliberately never
    run automatically alongside GET /settings, which is exactly why #54 skipped this originally (a probe's
    own latency and failure mode added to every settings load) - the admin UI only calls this on an explicit
    "Check now" click."""
    require_admin()
    port = config.mcp_port()
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=1.5):
            reachable = True
    except OSError:
        reachable = False
    return jsonify({'reachable': reachable, 'port': port}), 200


@bp.route('/settings/mcp_tools', methods=['GET'])
def mcp_tools_endpoint():
    """What `tools/list` currently returns for an unrestricted (admin) caller, computed in-process via
    mcp_server.list_tools_for() - the exact function the real MCP server itself calls - rather than a live
    probe of that separate process, so this works whether or not `queryapigate mcp` actually happens to be
    running right now. BACKLOG #54's own still-open "tool-listing panel" note."""
    require_admin()
    tools = mcp_server.list_tools_for(apikeys.OPEN)
    return jsonify({'tools': [{
        'name': t['name'],
        'description': t['description'],
        'kind': 'ad-hoc' if t['name'] in mcp_server.RESERVED_TOOL_NAMES else 'saved query',
        'params': sorted(t['inputSchema'].get('properties', {}).keys()),
        'read_only': t['readOnlyHint'],
    } for t in tools]}), 200


# --------------------------------------------------------------------------------------
# Cache entries - the admin UI's Caching screen "browse the cache" panel. Scoped to what
# QueryAPIGate itself put in the response cache (in-process or Redis, whichever is
# configured - see cache.py/rediscache.py, both implementing the same list_entries()/
# get_body()/delete()/clear() shape) rather than a general key-value browser: Redis here
# is an internal cache implementation detail, not a modeled connection.
# --------------------------------------------------------------------------------------

@bp.route('/cache/entries', methods=['GET'])
def cache_entries():
    """Every live cache entry's metadata (name/version/connection/format/page, content type, size, TTL
    remaining) - never the body itself, so listing stays cheap. Admin only, like everything else that
    reveals server-side state a caller didn't ask for."""
    require_admin()
    entries = current_app.extensions['queryapigate_cache'].list_entries()
    entries.sort(key=lambda e: e['ttl_remaining_s'])
    return jsonify({'entries': entries}), 200


@bp.route('/cache/entries', methods=['DELETE'])
def clear_cache_entries():
    """Evicts every entry - the "Clear cache" button. Not audited: this is cache housekeeping (a miss just
    re-populates from a real query), not a configuration change."""
    require_admin()
    current_app.extensions['queryapigate_cache'].clear()
    return jsonify({'message': 'Cache cleared'}), 200


@bp.route('/cache/entries/<key>', methods=['GET'])
def cache_entry_body(key):
    """The cached response body itself, served with its real content type - exactly as a caller would have
    received it on a hit. Lets the admin UI's cache browser reuse the same result renderer API Designer and
    a saved query's own Run tab already use, instead of a bespoke preview widget."""
    require_admin()
    hit = current_app.extensions['queryapigate_cache'].get_body(key)
    if hit is None:
        raise ApiError('Cache entry not found (missing, expired, or already evicted)', 404)
    body, content_type = hit
    return Response(body, content_type=content_type)


@bp.route('/cache/entries/<key>', methods=['DELETE'])
def delete_cache_entry(key):
    """Evicts one entry early. Not audited - see clear_cache_entries()."""
    require_admin()
    current_app.extensions['queryapigate_cache'].delete(key)
    return jsonify({'message': 'Entry deleted'}), 200


# --------------------------------------------------------------------------------------
# Service endpoints
# --------------------------------------------------------------------------------------

@bp.route('/', methods=['GET'])
def index():
    return redirect(url_for('api.docs'))


@bp.route('/favicon.ico', methods=['GET'])
def favicon():
    return '', 204


@bp.route('/health', methods=['GET'])
def health():
    from flask import current_app
    return jsonify({'status': 'ok', 'version': current_app.config['QUERYAPIGATE_VERSION']})


@bp.route('/metrics', methods=['GET'])
def metrics_endpoint():
    return Response(metrics.render(current_app.extensions.get('queryapigate_cache')),
                    mimetype='text/plain; version=0.0.4; charset=utf-8')


def _is_runnable(data):
    """Whether a saved-query version has something to actually execute - a SQL string, or (query_type
    'mongo') a Mongo collection - so a definition left mid-edit or otherwise malformed never gets listed
    somewhere a caller might then try to run it."""
    return isinstance(data.get('sql_query'), str) or (data.get('query_type') == 'mongo'
                                                       and isinstance(data.get('mongo_collection'), str))


def describe_saved_queries(permission):
    """What the OpenAPI document needs to know about each saved query (never its SQL text) that
    ``permission`` may actually call - the same test run_saved() applies, so a key scoped to specific
    queries (see apikeys.py) sees only its own approved list here, not the whole internal catalogue; an
    unrestricted (admin, or connection-wide) key sees everything, unchanged from before this filter."""
    described = []
    for name, number, data, collection in store.latest_versions():
        if not _is_runnable(data):
            continue
        connection_name = data.get('connection_name')
        if not apikeys.can_run_saved(permission, name, collection, connection_name):
            continue
        parameters = definitions.effective_parameters(data)  # what the SQL needs, in order
        described.append({'name': name, 'version': number, 'description': data.get('description'),
                          'tags': data.get('tags'), 'collection': collection,
                          'connection_name': data.get('connection_name'), 'parameters': parameters})
    return described


@bp.route('/openapi.json', methods=['GET'])
def openapi_spec():
    from flask import current_app
    # The generic API description is public. The list of saved queries (names, descriptions, parameters - never
    # the SQL itself) is shown to any authenticated caller, admin or scoped, same as any other endpoint they
    # could call through /q/<name> - a scoped key still needs to know a query's parameters to use it.
    saved = describe_saved_queries(g.permission) if g.permission is not None else None
    return jsonify(openapi.build_spec(current_app.config['QUERYAPIGATE_VERSION'], saved))


def describe_catalog(permission):
    """Like describe_saved_queries() (same reachability rule, same parameter shape - kept as its own loop
    rather than sharing one, since the two describe different things to different consumers: OpenAPI's
    request/response shape versus this endpoint's governance terms - and OpenAPI's shape is a stable
    contract other tooling parses, not something to risk changing by threading new fields through it), plus
    the governance facts OpenAPI has no field for: whether this query is cached, and whether *this specific
    caller* can write through it (apikeys.can_write_query() - independent of their blanket allow_writes)."""
    catalog = []
    for name, number, data, collection in store.latest_versions():
        if not _is_runnable(data):
            continue
        connection_name = data.get('connection_name')
        if not apikeys.can_run_saved(permission, name, collection, connection_name):
            continue
        parameters = definitions.effective_parameters(data)
        catalog.append({'name': name, 'version': number, 'description': data.get('description'),
                        'tags': data.get('tags'), 'collection': collection, 'connection_name': connection_name,
                        'parameters': parameters, 'cache_ttl': data.get('cache_ttl') or None,
                        'can_write': apikeys.can_write_query(permission, name)})
    return catalog


@bp.route('/catalog', methods=['GET'])
def catalog():
    """Everything this caller can reach through /q/<name>, and the terms it's offered under, in one place -
    closing the gap where that information exists (cache_ttl on a saved query, a key's own rate_limit and
    write curation) but was scattered across admin-only screens a scoped key can never reach. Requires the
    same authentication any other functional endpoint does (unlike /openapi.json, this is never public) -
    the whole point is answering 'what can *I* use,' which needs a resolved caller to mean anything."""
    permission = g.permission
    return jsonify({
        'queries': describe_catalog(permission),
        'caller': {
            'name': permission.name,
            'admin': permission.admin,
            'allow_writes': permission.allow_writes,
            'allowed_write_ops': permission.allowed_write_ops,
            'allowed_tables': sorted(permission.allowed_tables) if permission.allowed_tables is not None else None,
            'rate_limit': config.format_rate_limit(permission.rate_limit),
            'server_rate_limit': config.format_rate_limit(config.rate_limit()),
        },
    }), 200


@bp.route('/docs', methods=['GET'])
def docs():
    return Response(openapi.DOCS_HTML, mimetype='text/html')


@bp.route('/ui', methods=['GET'])
def admin_ui():
    return Response(ui.UI_HTML, mimetype='text/html')

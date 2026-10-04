"""The Flask application: HTTP routes on top of the store, engine and formatters."""
import json
import logging
import math
import queue
import re
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
    config,
    console,
    cors,
    db,
    definitions,
    engine,
    examples,
    governance,
    history,
    jwtauth,
    logging_setup,
    metrics,
    mongotools,
    openapi,
    schema,
    sqltools,
    store,
)
from . import params as param_rules
from .errors import ApiError, code_for
from .formats import FORMATTERS, STREAM_FORMATTERS, json_default, stream_response
from .ratelimit import KeyRateLimiters, RateLimiter

log = logging.getLogger('queryapigate')
bp = Blueprint('api', __name__)

# Query-string arguments that control a request rather than supplying query parameters.
RESERVED_ARGS = {'format', 'page', 'page_size', 'connection_name', 'version', 'timeout', 'stream'}
PUBLIC_ENDPOINTS = {'api.index', 'api.favicon', 'api.health', 'api.docs', 'api.openapi_spec', 'api.admin_ui',
                    'api.console_page', 'api.metrics_endpoint'}
# Monitoring keeps working while a client is throttled; the Console's static files are exempt so loading the page
# (several hashed assets) never spends a client's quota - they carry no data and no key.
RATE_LIMIT_EXEMPT = {'api.health', 'api.metrics_endpoint', 'api.console_page'}
ACCESS_LOG_QUIET = {'api.health', 'api.metrics_endpoint'}  # polled too often to log every hit
SAVED_QUERY_ENDPOINTS = {'api.run_named_query', 'api.execute_sql_from_file'}  # where run_saved() is reached
# Routes on their way out: each sends a Deprecation header (RFC 9745) and a Link to its successor, and /openapi.json
# marks it. None today - the legacy management routes were removed once /api/v1 replaced them (BACKLOG #72); this is
# how a 1.x deprecation is announced before 2.0 removes it.
DEPRECATED_ENDPOINTS: dict[str, str] = {}  # endpoint -> its successor
# A stable `code` for /api/v1 errors raised without their own (BACKLOG #69) - by HTTP status.
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
        # PATCH /api/v1/connections - a connection saved before QUERYAPIGATE_SECRET_KEY existed benefits right away.
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
        response = jsonify(error_body(error.message, error.status, extra, error.code))
        if 'retry_after' in extra:
            response.headers['Retry-After'] = str(extra['retry_after'])
        return response, error.status

    @app.errorhandler(HTTPException)
    def handle_http_error(error):
        return jsonify(error_body(error.description, error.code)), error.code

    @app.errorhandler(Exception)
    def handle_unexpected_error(error):
        log.exception('Unhandled error')
        return jsonify(error_body('An error occurred', 500)), 500

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
            return jsonify(error_body('Unauthorized', 401)), 401
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
        if request.endpoint in DEPRECATED_ENDPOINTS:
            response.headers['Deprecation'] = 'true'
            response.headers['Link'] = f'<{DEPRECATED_ENDPOINTS[request.endpoint]}>; rel="successor-version"'
        elapsed = time.monotonic() - g.get('request_started', time.monotonic())
        endpoint = request.endpoint or 'unmatched'
        governance.observe(request.method, endpoint, response.status_code, elapsed, caller_key_name())
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
    from . import v1  # imports this module's request helpers, so only once it is fully loaded
    app.register_blueprint(v1.bp)
    return app


def error_body(message, status, extra=None, code=None):
    """The JSON body of every error response (errors.py, BACKLOG #69): the message, a stable `code` (the raiser's
    own, or the one for the status), whatever extras the raiser attached, and the `request_id` that finds the
    matching log line."""
    return {'error': message, 'code': code_for(status, code), **(extra or {}), 'request_id': g.get('request_id', '-')}


# --------------------------------------------------------------------------------------
# Request helpers
# --------------------------------------------------------------------------------------

def _too_many(verdict, message):
    response = jsonify(error_body(message, 429, {'retry_after': verdict.retry_after}))
    response.status_code = 429
    response.headers['Retry-After'] = str(verdict.retry_after)
    return response


def check_rate_limit():
    """Count this request against its client's quota (governance.py); returns a 429 response when it is over."""
    if request.method == 'OPTIONS' or request.endpoint in RATE_LIMIT_EXEMPT:
        return None
    verdict = governance.check_client_limit(current_app, request.remote_addr)
    if verdict is None:
        return None
    g.rate_limit = (verdict.limit, verdict.remaining)
    return None if verdict.allowed else _too_many(verdict, 'Rate limit exceeded')


def check_key_rate_limit():
    """Like check_rate_limit() above, but for the caller's own optional `rate_limit` grant - checked in *addition* to
    the server-wide, IP-based limit, never instead of it. Runs after g.permission is resolved, since there is no
    per-key identity to limit by before then; a no-op for the admin key and the open/no-key case."""
    if request.method == 'OPTIONS' or request.endpoint in RATE_LIMIT_EXEMPT:
        return None
    verdict = governance.check_key_limit(current_app, g.get('permission'))
    if verdict is None:
        return None
    g.key_rate_limit = (verdict.limit, verdict.remaining)
    return None if verdict.allowed else _too_many(verdict, 'Rate limit exceeded for this API key')


def resolve_permission():
    """The caller's Permission: a matched key (admin or scoped), a signed-in user's verified bearer token
    (jwtauth.py), the unrestricted OPEN default when no key is configured anywhere, or None when a key is
    required but missing or wrong. A request carrying an X-API-Key is judged on that key alone - a wrong key
    never falls through to the token."""
    return authenticate_headers(request.headers.get('X-API-Key', ''), request.headers.get('Authorization', ''),
                                request.remote_addr)


def authenticate_headers(api_key, authorization, client_ip):
    """resolve_permission() without Flask - governance.authenticate(), kept under this name for events.py."""
    return governance.authenticate(api_key, authorization, client_ip)


def require_admin():
    """Only the admin key (QUERYAPIGATE_API_KEY, or no key at all when nothing is configured) manages the
    server's own configuration - connections, saved queries and other API keys."""
    if not g.permission.admin:
        raise ApiError('This API key is not authorized to manage the server configuration', 403, code='admin_only')


def require_connection(connection_name):
    if not apikeys.can_use(g.permission, connection_name):
        raise ApiError(f"This API key is not permitted to use the connection '{connection_name}'", 403,
                       code='connection_forbidden')


def caller_key_name():
    """The calling API key's name for logs/metrics ('admin', a scoped key's name, or '-'); safe to call even
    before permission is resolved, e.g. while rendering a CORS preflight or a 429 in decorate()."""
    permission = g.get('permission')
    return (permission.name if permission else None) or '-'


def get_json_body(required=True):
    data = request.get_json(silent=True)
    if data is None and not required:
        # An optional body may be absent - but a body that was sent as JSON and fails to parse is a client
        # error, not "no parameters": silently treating it as empty turns a typo into a misleading
        # "<param> is required" (or, worse, a run with every parameter at its default).
        if request.is_json and request.get_data(cache=True).strip():
            raise ApiError('Request body is not valid JSON', code='invalid_body')
        return {}
    if not isinstance(data, dict):
        raise ApiError('Request body must be a JSON object', code='invalid_body')
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
        raise ApiError('page and page_size must be positive', code='invalid_paging')
    if page_size > config.max_page_size():
        raise ApiError(f'page_size must not exceed {config.max_page_size()}', code='invalid_paging')
    return page_size, (page - 1) * page_size, page


def get_output_format(body=None):
    output_format = str(request.args.get('format') or (body or {}).get('format') or 'json').lower()
    if output_format not in FORMATTERS:
        raise ApiError(f"Unsupported format '{output_format}'. Supported formats: {', '.join(FORMATTERS)}",
                       code='invalid_format')
    return output_format


def get_timeout(body=None):
    """Seconds allowed for the query: ?timeout= may lower the server limit but never raise it."""
    raw = request.args.get('timeout', (body or {}).get('timeout'))
    if raw in (None, ''):
        return config.effective_timeout(None)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ApiError('timeout must be a number of seconds', code='invalid_timeout') from None
    if not math.isfinite(value) or value <= 0:
        raise ApiError('timeout must be a positive number of seconds', code='invalid_timeout')
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
                        allowed_tables=None, database=None):
    """Shared by execute_sql_endpoint() and run_saved(): validates the stream=true-specific constraints
    (format, no pagination) and returns the chunked Response, the run recorded in history once the stream
    finishes - in its saved query's, when ``saved`` is (path, version), or as an ad-hoc run."""
    if output_format not in STREAM_FORMATTERS:
        raise ApiError(f"stream=true only supports these formats: {', '.join(sorted(STREAM_FORMATTERS))}",
                       code='invalid_stream')
    if request.args.get('page') or request.args.get('page_size'):
        raise ApiError('stream=true exports the whole result and does not accept page/page_size', code='invalid_stream')
    key_name = caller_key_name()
    columns, rows = engine.stream_sql(sql, connection_name, params, timeout, key_name=key_name,
                                      allowed_tables=allowed_tables)
    # Captured here, not inside _record_stream_history(): that generator's body runs lazily, as the response
    # streams out - by then the request/app context this view function runs in is long gone (no
    # flask.stream_with_context() wrapping is used), so g, caller_key_name() and current_app are only safe to read
    # up front, while still inside the request that's actually issuing the query - this is why the broadcaster
    # instance itself is captured here too.
    broadcaster = current_app.extensions['queryapigate_broadcaster']
    entry = run_entry(connection_name)
    if saved is not None:
        path, number = saved

        def record(finished):
            _record_and_broadcast(path, number, connection_name, finished, broadcaster)
    else:
        if database:
            entry['database'] = database

        def record(finished):
            _record_adhoc_and_broadcast(sql, params, connection_name, finished, broadcaster)
    rows = _record_stream_history(rows, entry, record)
    return stream_response(output_format, columns, rows, filename)


def run_entry(connection_name):
    """The start of one run's history entry: when, where, by whom, through which front door."""
    return {'executed_at': store.now(), 'connection_name': connection_name, 'request_id': g.get('request_id'),
            'key_name': caller_key_name(), 'transport': g.get('transport', 'rest')}


def _record_adhoc_and_broadcast(sql, params, connection_name, entry, broadcaster=None):
    """An ad-hoc run's _record_and_broadcast(): into run history (history.record_adhoc()) and the live feed, as an
    `adhoc_execution` event, under the same per-key rule as a saved query's run."""
    recorded = history.record_adhoc(entry, sql, params)
    if broadcaster is None:
        broadcaster = current_app.extensions['queryapigate_broadcaster']
    broadcaster.publish({'type': 'adhoc_execution', 'connection_name': connection_name, 'entry': recorded},
                        entry['key_name'])


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


def _record_stream_history(rows, entry, record):
    """Records a streamed run, through ``record``, once fully drained or failed partway through (not on a client
    disconnect, GeneratorExit) - counting rows as they pass through, since the total is not known up front, the
    same trade-off engine._drain() makes for the streaming metric."""
    count = 0
    try:
        for row in rows:
            count += 1
            yield row
    except GeneratorExit:
        raise
    except ApiError as error:
        record({**entry, 'status': 'error', 'error': error.message, 'code': error.code, 'rows': count})
        raise
    except Exception as error:
        record({**entry, 'status': 'error', 'error': str(error), 'rows': count})
        raise
    else:
        record({**entry, 'status': 'success', 'rows': count})


# --------------------------------------------------------------------------------------
# Query execution
# --------------------------------------------------------------------------------------

@bp.route('/execute_sql', methods=['POST'])
def execute_sql_endpoint():
    data = get_json_body()
    if not data.get('sql'):
        raise ApiError('SQL query is missing', code='sql_required')
    if not data.get('connection_name'):
        raise ApiError('Connection name is missing', code='connection_required')
    require_connection(data['connection_name'])
    database = data.get('database')
    if database and not g.permission.admin:
        # Every other scoped-key boundary is per connection, never per database within one - a key granted a
        # connection may run any SQL that connection's own configured database allows, but not redirect that
        # same connection at a sibling database on the same server the admin never listed it for.
        raise ApiError('Only the admin key may run a query against a different database on this connection', 403,
                       code='admin_only')
    params = get_object(data.get('params'), 'params')
    output_format = get_output_format(data)
    timeout = get_timeout(data)
    if get_stream_flag():
        return stream_sql_response(data['sql'], data['connection_name'], params, timeout, output_format,
                                   filename=data['connection_name'], allowed_tables=g.permission.allowed_tables,
                                   database=database)
    limit, offset, page = get_pagination()
    entry = run_entry(data['connection_name'])
    if database:
        entry['database'] = database
    try:
        result, elapsed_ms = engine.timed(engine.execute_sql, data['sql'], data['connection_name'], limit, offset,
                                          params, timeout, allow_writes=g.permission.allow_writes,
                                          key_name=caller_key_name(), allowed_write_ops=g.permission.allowed_write_ops,
                                          database=database, allowed_tables=g.permission.allowed_tables)
    except ApiError as error:
        _record_adhoc_and_broadcast(data['sql'], params, data['connection_name'],
                                    {**entry, 'status': 'error', 'error': error.message, 'code': error.code})
        raise
    _record_adhoc_and_broadcast(data['sql'], params, data['connection_name'],
                                {**entry, 'status': 'success', 'rows': len(result.rows), 'duration_ms': elapsed_ms})
    return render(result, output_format, page, limit)


@bp.route('/execute_mongo', methods=['POST'])
def execute_mongo_endpoint():
    """The Mongo sibling of /execute_sql - a find() query, ad-hoc, against a saved connection. Read-only,
    full stop (BACKLOG #36: MongoDB support is find-only in this version) - unlike /execute_sql there is no
    write permission to check."""
    data = get_json_body()
    if not data.get('collection'):
        raise ApiError('Collection is missing', code='collection_required')
    if not data.get('connection_name'):
        raise ApiError('Connection name is missing', code='connection_required')
    require_connection(data['connection_name'])
    filter_doc = mongotools.validate_filter(get_object(data.get('filter'), 'filter'))
    params = get_object(data.get('params'), 'params')
    output_format = get_output_format(data)
    timeout = get_timeout(data)
    limit, offset, page = get_pagination()
    entry = {**run_entry(data['connection_name']), 'query_type': 'mongo', 'mongo_collection': data['collection']}
    statement = json.dumps({'collection': data['collection'], 'filter': filter_doc}, default=str)
    try:
        result, elapsed_ms = engine.timed(engine.execute_mongo, data['collection'], filter_doc,
                                          data['connection_name'], limit, offset, params, timeout,
                                          projection=data.get('projection'), sort=data.get('sort'),
                                          key_name=caller_key_name())
    except ApiError as error:
        _record_adhoc_and_broadcast(statement, params, data['connection_name'],
                                    {**entry, 'status': 'error', 'error': error.message, 'code': error.code})
        raise
    _record_adhoc_and_broadcast(statement, params, data['connection_name'],
                                {**entry, 'status': 'success', 'rows': len(result.rows), 'duration_ms': elapsed_ms})
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


def bind_claims(saved, raw):
    """Fill every `from_claim` parameter (params.py) of a saved query from the caller's verified token claims -
    the request has no say in them: `WHERE user_id = :user_id` with `"user_id": {"from_claim": "sub"}` can only
    ever return the signed-in user's own rows. Returns the parameters to resolve.

    - A signed-in user (jwtauth.py) gets each value from their token; sending one of these parameters anyway is a
      400, and a token without the claim is a 403.
    - The admin key (or open mode) has no token and supplies them like any other parameter - for testing and
      server-side use.
    - Any other API key can't run the query at all (403): it has no token to take the value from, and letting it
      choose one would defeat the point."""
    declared = param_rules.read_definitions(saved.get('query_parameters'))
    bound = {name: spec['from_claim'] for name, spec in declared.items() if spec['from_claim']}
    if not bound:
        return raw
    claims = g.permission.claims
    if claims is None:
        if g.permission.admin:
            return raw
        raise ApiError(f"This query takes {', '.join(sorted(bound))} from a signed-in user's token - call it with "
                       '`Authorization: Bearer <token>`, not an API key', 403, code='sign_in_required')
    sent = sorted(set(bound) & set(raw))
    if sent:
        raise ApiError(f"{', '.join(sent)} {'comes' if len(sent) == 1 else 'come'} from your sign-in token and "
                       "can't be set by the request", 400, code='param_from_claim')
    raw = dict(raw)
    for name, claim_name in bound.items():
        value = jwtauth.claim(claims, claim_name)
        if value is None or isinstance(value, (dict, list)):
            raise ApiError(f"Your sign-in token has no usable '{claim_name}' claim", 403, code='claim_missing')
        if declared[name]['type'] in (None, 'string') and isinstance(value, (int, float)) and \
                not isinstance(value, bool):
            value = str(value)  # a numeric user id, bound to a text column
        raw[name] = value
    return raw


def run_saved(ref, body, url_params):
    """Execute a saved query (its published version unless one is requested) and record the run - or, for one
    with a cache_ttl whose SQL is read-only, serve a cached response instead.

    A draft - a version newer than the published one, or any version of a query with none published - runs only
    for the admin key, and only when asked for by number (to test it before publishing). For anyone else it
    doesn't exist: the same 404 as a version number that was never used, so drafts can't be discovered."""
    path = store.resolve_saved_file(ref)
    content = store.load_versions(path, with_history=False)
    requested = get_int(request.args.get('version') or body.get('version'), 'version')
    if requested is not None and store.is_draft(content, requested) and not g.permission.admin:
        raise ApiError(f'Version {requested} not found', 404, code='version_not_found')
    number, saved = store.select_version(content, requested)
    connection_name = request.args.get('connection_name') or body.get('connection_name') \
        or saved.get('connection_name')
    if not connection_name:
        raise ApiError('Connection name is missing', code='connection_required')
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
    raw = bind_claims(saved, raw)
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

    entry = run_entry(connection_name)
    try:
        result, elapsed_ms = engine.timed(engine.execute_sql, sql, connection_name, limit, offset, values, timeout,
                                          allow_writes=effective_allow_writes, key_name=caller_key_name(),
                                          allowed_write_ops=g.permission.allowed_write_ops,
                                          allowed_tables=g.permission.allowed_tables)
    except ApiError as error:
        _record_and_broadcast(path, number, connection_name,
                              {**entry, 'status': 'error', 'error': error.message, 'code': error.code})
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
        raise ApiError('Streaming is not supported for Mongo queries yet', 400, code='stream_unsupported')
    used = set(mongotools.placeholder_names(saved.get('mongo_filter') or {}))
    values = param_rules.resolve(saved.get('query_parameters'), raw, used=used)
    filter_doc = mongotools.fill_placeholders(saved.get('mongo_filter') or {}, values)
    limit, offset, page = get_pagination()
    entry = run_entry(connection_name)
    try:
        result, elapsed_ms = engine.timed(engine.execute_mongo, saved['mongo_collection'], filter_doc,
                                          connection_name, limit, offset, timeout=timeout,
                                          projection=saved.get('mongo_projection'), sort=saved.get('mongo_sort'),
                                          key_name=caller_key_name())
    except ApiError as error:
        _record_and_broadcast(path, number, connection_name,
                              {**entry, 'status': 'error', 'error': error.message, 'code': error.code})
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
# Live events
# --------------------------------------------------------------------------------------

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
    pattern /catalog already uses elsewhere. Still requires a real key or open-access mode - never public. The
    Console's Home reads it to stay live.

    The client reads this with fetch()'s streamed response body rather than a plain `new EventSource(...)`:
    EventSource cannot set custom request headers, and this app has no cookie-based auth to fall back on -
    every other request already authenticates via X-API-Key (see the Console's apiFetch()). Putting the key in the
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
                           '`queryapigate events` instead', 503, retry_after=5, code='too_many_streams')
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

@bp.route('/connections/<name>/schema', methods=['GET'])
def connection_schema(name):
    require_connection(name)
    database = request.args.get('database') or None
    if database and not g.permission.admin:
        raise ApiError('Only the admin key may browse a different database on this connection', 403, code='admin_only')
    return jsonify(schema.fetch_schema(name, database=database)), 200


@bp.route('/connections/<name>/table_ddl', methods=['GET'])
def connection_table_ddl(name):
    """A table's real CREATE TABLE text (BACKLOG #38) - not available for every dialect, see
    schema.fetch_table_ddl(). Same permission level as browsing the schema itself."""
    require_connection(name)
    table = request.args.get('table')
    if not table:
        raise ApiError('table is required', code='table_required')
    database = request.args.get('database') or None
    if database and not g.permission.admin:
        raise ApiError('Only the admin key may browse a different database on this connection', 403, code='admin_only')
    return jsonify(schema.fetch_table_ddl(name, table, database=database)), 200


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
    for name, number, data, collection in store.live_versions():
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
    for name, number, data, collection in store.live_versions():
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
    """The admin UI moved to the Console (ADR 0001); old bookmarks land there."""
    return redirect('/console/', 301)


@bp.route('/console', methods=['GET'])
@bp.route('/console/', methods=['GET'])
@bp.route('/console/<path:path>', methods=['GET'])
def console_page(path=''):
    return console.serve(path)

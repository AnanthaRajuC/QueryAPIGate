"""The Flask application: HTTP routes on top of the store, engine and formatters."""
import json
import logging
import math
import os
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
    admins,
    apikeys,
    broadcast,
    cache,
    config,
    console,
    cors,
    dataplane,
    db,
    definitions,
    deprecations,
    descriptions,
    embeddings,
    engine,
    events,
    examples,
    experimental,
    governance,
    history,
    instances,
    jwtauth,
    limits,
    logging_setup,
    masking,
    metacache,
    metrics,
    mongodocs,
    mongotools,
    openapi,
    quotas,
    ratelimit,
    runstore,
    schema,
    scope,
    secheaders,
    sqltools,
    sso,
    store,
    tracing,
)
from . import params as param_rules
from .errors import ApiError, code_for
from .formats import FORMATTERS, STREAM_FORMATTERS, chunked_response, json_default, stream_response

log = logging.getLogger('queryapigate')
bp = Blueprint('api', __name__)

# Query-string arguments that control a request rather than supplying query parameters.
RESERVED_ARGS = {'format', 'page', 'page_size', 'connection_name', 'version', 'timeout', 'stream'}
PUBLIC_ENDPOINTS = {'api.index', 'api.favicon', 'api.health', 'api.docs', 'api.openapi_spec', 'api.admin_ui',
                    'api.console_page', 'api.metrics_endpoint',
                    # the data plane's reports and run store lease: gate() lets only its secret through (ADR 0028)
                    'api.dataplane_runs', 'api.dataplane_lease',
                    # Console sign-in (sso.py): each checks what it needs itself - state, the session and its CSRF token
                    'auth.login', 'auth.callback', 'auth.session', 'auth.logout'}
DATAPLANE_ENDPOINTS = {'api.dataplane_runs', 'api.dataplane_lease'}
# Monitoring keeps working while a client is throttled; the Console's static files are exempt so loading the page
# (several hashed assets) never spends a client's quota - they carry no data and no key.
RATE_LIMIT_EXEMPT = {'api.health', 'api.metrics_endpoint', 'api.console_page'}
ACCESS_LOG_QUIET = {'api.health', 'api.metrics_endpoint'}  # polled too often to log every hit
SAVED_QUERY_ENDPOINTS = {'api.run_named_query'}  # where run_saved() is reached
# Where a call returns data - what a caller's usage quota counts (quotas.py, ADR 0011)
QUOTA_ENDPOINTS = SAVED_QUERY_ENDPOINTS | {'api.execute_sql_endpoint', 'api.execute_mongo_endpoint'}
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


def apply_config_at_startup():
    """QUERYAPIGATE_CONFIG_FILE (ADR 0014): apply it before serving - once per file, so with several instances the first
    to start applies it and the others find it done. A file that can't be applied stops startup, changing nothing:
    in a rolling update the new pods don't come up, and the ones still serving keep the configuration they have."""
    from . import configcode
    path = config.config_file()
    if path is None:
        return
    try:
        doc = configcode.load(path.read_text())
        result = configcode.apply(doc, prune=config.config_prune(), actor='startup', once=True)
    except OSError as error:
        log.error('QUERYAPIGATE_CONFIG_FILE %s could not be read: %s', path, error)
        raise ValueError(f'QUERYAPIGATE_CONFIG_FILE {path} could not be read: {error}') from None
    except configcode.ConfigInvalid as error:
        message = f'QUERYAPIGATE_CONFIG_FILE {path} was not applied - nothing in it was. ' + '; '.join(error.problems)
        log.error('%s', message)  # one line in the log, whatever prints the exception after (gunicorn: a traceback)
        raise ValueError(message) from None
    if result.get('already_applied'):
        log.info('QUERYAPIGATE_CONFIG_FILE %s (sha256 %s) is already applied', path, result['file_sha256'][:12])
    else:
        counts = {a: sum(1 for c in result['changes'] if c['action'] == a) for a in ('create', 'update', 'delete')}
        log.info('Applied QUERYAPIGATE_CONFIG_FILE %s (sha256 %s): %d created, %d changed, %d deleted', path,
                 result['file_sha256'][:12], counts['create'], counts['update'], counts['delete'])


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


def warn_env_references():
    """Once at startup: ${VAR} references in use with no QUERYAPIGATE_SECRET_ENV_VARS - so an administrator may
    reference any other variable too, but the few secretrefs.py always refuses (ADR 0017)."""
    from . import destinations, secretrefs
    if config.secret_env_vars():
        return
    used = secretrefs.env_variables([*store.read_connections().values(), *destinations.list_all()])
    if used:
        log.warning('Connections and destinations reference environment variables (%s), and '
                    'QUERYAPIGATE_SECRET_ENV_VARS is unset: an administrator could reference any other variable too, '
                    "and have the server send it to a host of theirs as a password. Set it to the ones in use.",
                    ', '.join(used))


class _CapturePeer:
    """Record the real socket address (`REMOTE_ADDR`) before ProxyFix can replace it with an X-Forwarded-For value,
    so the open-access loopback check (gate()) judges the actual connection and never a header a client can forge.

    Behind a data plane (dataplane.py, ADR 0028) every request comes from it, over loopback: one that carries its
    secret names the client's own socket address in X-QAG-Peer, and that address is taken as the real one - so this
    server sees the request as if the client had connected directly, ProxyFix and QUERYAPIGATE_TRUST_PROXY included.
    A peer that isn't an IP address counts as remote, as a Unix socket does. A data plane from another release
    (X-QAG-Dataplane-Version) is still the data plane, but is given no plan (dataplane.paired()). Its headers are
    removed here, so nothing after this layer reads, logs or passes on the secret."""

    def __init__(self, app):
        self.app = app

    def __call__(self, environ, start_response):
        supplied = environ.pop(dataplane.SECRET_ENVIRON, None)
        peer = environ.pop(dataplane.PEER_ENVIRON, None)
        wanted = environ.pop(dataplane.WANTED_ENVIRON, None)
        version = environ.pop(dataplane.VERSION_ENVIRON, None)
        if supplied is not None and dataplane.matches(supplied):
            is_paired = dataplane.paired(version)
            environ['queryapigate.dataplane'] = 'plan' if wanted == '1' and is_paired else True
            environ['queryapigate.dataplane_paired'] = is_paired
            environ['REMOTE_ADDR'] = peer if dataplane.is_address(peer) else ''
        environ['queryapigate.peer_addr'] = environ.get('REMOTE_ADDR')
        return self.app(environ, start_response)


def _peer_is_local():
    """Whether the request's real socket peer is a loopback address (governance.is_loopback(), the MCP server's rule
    too). Fails closed: in open-access mode a caller it can't place must configure a key."""
    return governance.is_loopback(request.environ.get('queryapigate.peer_addr'))


def create_app():
    from . import __version__
    config.check_settings()
    logging_setup.configure(log)  # before anything below logs - the legacy import's deprecation warning, for one
    db.init_schema()
    runstore.check_marker()
    store.refuse_legacy_home()
    experimental.warn_in_use(log, os.environ, store.read_connections(), store.workspace_names())
    warn_env_references()
    instances.register('serve')
    instances.warn_if_unshared(log)
    app = Flask(__name__)
    app.wsgi_app = scope.WorkspacePrefix(app.wsgi_app)  # /w/<workspace>/... - every route, in that workspace (ADR 0023)
    hops = config.proxy_hops()
    if hops:  # behind reverse proxies: take the client address and scheme from their X-Forwarded-* headers
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=hops, x_proto=hops, x_host=hops)
    app.wsgi_app = _CapturePeer(app.wsgi_app)  # outermost: the real socket address, before ProxyFix can rewrite it
    if not apikeys.auth_required():
        log.warning('No API key, administrator, JWT or MCP OAuth is configured: open-access mode. The server answers '
                    'only local (loopback) clients; set QUERYAPIGATE_API_KEY to allow access from other hosts.')
    app.extensions['queryapigate_limiter'] = ratelimit.client_limiter(config.redis_url())
    app.extensions['queryapigate_key_limiter'] = ratelimit.key_limiters(config.redis_url())
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
    if not apikeys.auth_required():  # the open_server alert's condition (alerts.py)
        log.warning('No API key is configured: every request is allowed, including changing connections and keys. '
                    'Fine on your own machine - set QUERYAPIGATE_API_KEY before anyone else can reach this server.')
    if config.cors_origins() == '*' and not apikeys.auth_required():
        log.warning('QUERYAPIGATE_CORS_ORIGINS=* without QUERYAPIGATE_API_KEY: any website a user visits can call '
                    'this API from their browser and reach every active connection. Set an API key or list the '
                    'origins.')
    if apikeys.any_configured() and not config.api_key() and not admins.active_owner_exists():
        log.warning('Scoped API keys exist but QUERYAPIGATE_API_KEY is not set and there is no active owner: nobody '
                    'can manage connections, saved queries or keys until there is - set QUERYAPIGATE_API_KEY, or '
                    'create an owner with `queryapigate admins create NAME --role owner`.')
    if config.secret_key():
        # Encrypt any literal password already on disk immediately, rather than waiting for its next
        # PATCH /api/v1/connections - a connection saved before QUERYAPIGATE_SECRET_KEY existed benefits right away.
        store.encrypt_plaintext_passwords_in_place()
        from . import rekey
        found = rekey.report()
        if found['undecryptable']:
            log.warning('No key in QUERYAPIGATE_SECRET_KEY decrypts the password of %s - add the key it was '
                        'encrypted with (after the first), or enter it again.',
                        ', '.join(f"{v['type']} '{v['name']}'" for v in found['undecryptable']))
        if found['under_old_keys']:
            log.warning('%d password(s) are still encrypted with an old key in QUERYAPIGATE_SECRET_KEY - run '
                        '`queryapigate rekey` to move them to the first key, then remove the others.',
                        found['under_old_keys'])
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
    _note_audit_key_rotation()
    from . import auditstream
    auditstream.start()  # the audit log to a SIEM, when an output is set (ADR 0012)
    apply_config_at_startup()
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
        # A caller with column-masking rules never gets the database's own text, on any route: an error can quote a
        # value - "Could not convert string 'jane@example.com' to INT32" - that masking would have hidden.
        if 'detail' in extra and getattr(g.get('permission'), 'mask', None):
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

    @app.teardown_request
    def end_trace(_error):
        tracing.finish(g.pop('trace', None))
        token = g.pop('dataplane_token', None)
        if token is not None:
            metacache.reset(token)

    @app.before_request
    def gate():
        # Set first so every request - including one rejected below - gets an ID and a measured duration.
        g.request_id = new_request_id(request.headers.get('X-Request-Id'))
        g.request_started = time.monotonic()
        if request.endpoint in DATAPLANE_ENDPOINTS:  # the data plane's own calls: nothing else applies to them
            if request.environ.get('queryapigate.dataplane'):
                return None
            return jsonify(error_body('Not Found', 404)), 404
        if request.environ.get('queryapigate.dataplane') == 'plan':  # before the key is read: a plan names its row
            g.dataplane_record, g.dataplane_token = metacache.capture()
        # The request's span (tracing.py, ADR 0010): named by the route rule, never the path's values or query string
        rule = request.url_rule.rule if request.url_rule is not None else None
        g.trace = tracing.start(f'{request.method} {rule}' if rule else request.method, 'server', {
            'http.request.method': request.method, 'http.route': rule, 'url.scheme': request.scheme,
            'client.address': request.remote_addr, 'queryapigate.request_id': g.request_id},
            parent=tracing.extract(request.headers))
        instances.heartbeat()  # at most one small write per HEARTBEAT seconds
        # Order matters: a browser's preflight cannot carry the API key, and rate limiting comes before the key
        # check so that guessing keys is throttled too.
        if cors.is_preflight(request):
            if request.path.startswith('/auth/'):  # Console sign-in is same-origin only (sso.py)
                return '', 204
            return cors.preflight_response(request.headers.get('Origin'))
        limited = check_rate_limit()
        if limited is not None:
            return limited
        # The workspace the path names, if it names one, before the caller is known: a signed-in user's role is found
        # by name in it. Settled once the caller is (settle_workspace()).
        named = request.environ.get(scope.ENVIRON_KEY)
        g.workspace = named or db.DEFAULT_WORKSPACE
        g.permission = resolve_permission()
        if request.endpoint not in PUBLIC_ENDPOINTS and g.permission is None:
            return jsonify(error_body('Unauthorized', 401)), 401
        # Open-access mode (no credential configured anywhere) answers local clients only: without a key, anyone who
        # could reach the port would otherwise be an unrestricted admin. The real socket peer decides, never a
        # forgeable header. The public endpoints (health, docs, the Console shell) are reachable either way.
        if g.permission is apikeys.OPEN and request.endpoint not in PUBLIC_ENDPOINTS and not _peer_is_local():
            raise ApiError(governance.OPEN_ACCESS_REMOTE, 403, code='open_access_remote')
        if g.permission is not None:
            g.permission = settle_workspace(g.permission, named)
        limited = check_key_rate_limit()
        if limited is not None:
            return limited
        if request.endpoint in QUOTA_ENDPOINTS:  # before the cache too: a cached response is data delivered
            quotas.check(g.permission)

    @app.after_request
    def decorate(response):
        if request.endpoint in DATAPLANE_ENDPOINTS:  # counted and logged as the runs it reports, not itself
            return response
        if not request.path.startswith('/auth/'):  # Console sign-in is same-origin only (sso.py)
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
        secheaders.apply(response, request.is_secure)
        permission = g.get('permission')
        if getattr(permission, 'quota', None):
            if request.endpoint in QUOTA_ENDPOINTS and response.status_code == 200:
                # A stream's rows are counted as it finishes (_record_stream_history); a page's are in X-Row-Count,
                # which a cached response carries too.
                quotas.count(permission, 0 if response.is_streamed else response.headers.get('X-Row-Count'))
            response.headers.update(quotas.headers(permission))
        if g.get('trace') is not None:
            span = g.trace[0]
            tracing.set_attributes(span, http__response__status_code=response.status_code,
                                   queryapigate__caller=caller_key_name())
            if response.status_code >= 400:
                span.set_attribute('error.type', g.get('error_code') or str(response.status_code))
            if response.status_code >= 500:
                tracing.mark_error(span, g.get('error_code') or str(response.status_code))
        rule = request.url_rule.rule if request.url_rule is not None else None
        if rule in deprecations.ROUTES:  # on its way out (deprecations.py): say so, and name its successor
            response.headers['Deprecation'] = deprecations.header_value(rule)
            response.headers['Link'] = f'<{deprecations.ROUTES[rule]["successor"]}>; rel="successor-version"'
            deprecations.warn(rule)
        elapsed = time.monotonic() - g.get('request_started', time.monotonic())
        endpoint = request.endpoint or 'unmatched'
        governance.observe(request.method, endpoint, response.status_code, elapsed, metrics_key_name())
        cache_status = response.headers.get('X-Cache')
        if cache_status == 'HIT':
            metrics.inc_cache_hit()
        elif cache_status == 'MISS':
            metrics.inc_cache_miss()
        if endpoint not in ACCESS_LOG_QUIET and config.log_requests():
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
    app.register_blueprint(sso.bp)
    return app


def _note_audit_key_rotation():
    """Put a change of QUERYAPIGATE_AUDIT_KEY in the chain (ADR 0012): `audit_key_set` when one is set and the newest
    entry isn't keyed yet - so a key set means the newest entry is keyed, which verify() holds the chain to - and
    `audit_key_rotated` when the first key changed since the newest entry was signed."""
    from . import auditchain
    if not auditchain.keys():
        return
    try:
        conn = db.connection()
        if not auditchain.head_keyed(conn):  # a key set for the first time: from here on, every entry is keyed
            # How many came before, unkeyed: expected once, when the key is first set. Again later - after a key was
            # set long ago - it means the keyed history was replaced, which the SIEM's copy shows (ADR 0012).
            unkeyed = conn.execute('SELECT COUNT(*) FROM audit_log').fetchone()[0]
            store.record_audit('startup', 'audit_key_set', 'audit_log', {'entries_before_unkeyed': unkeyed})
        elif auditchain.head_signed_by(conn) not in (None, 0):
            store.record_audit('startup', 'audit_key_rotated', 'audit_log', {'keys': len(auditchain.keys())})
    except db.Error:
        log.warning('Could not check the audit key against the audit log', exc_info=True)


def error_body(message, status, extra=None, code=None):
    """The JSON body of every error response (errors.py, BACKLOG #69): the message, a stable `code` (the raiser's
    own, or the one for the status), whatever extras the raiser attached, and the `request_id` that finds the
    matching log line."""
    g.error_code = code_for(status, code)
    trace_id = tracing.ids()[0]
    return {'error': message, 'code': g.error_code, **(extra or {}), 'request_id': g.get('request_id', '-'),
            **({'trace_id': trace_id} if trace_id else {})}


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


def settle_workspace(permission, named):
    """The workspace this request acts in (ADR 0023 §4), and the caller's Permission in it:

    - an API key, or a signed-in user's role, is in one workspace - its own. Naming another is 404, as for one that
      doesn't exist, so a key can't learn which workspaces there are;
    - an administrator acts in the one the path names, or `default`: it must exist, and - for one whose roles are all
      memberships - be one of theirs, else 404 too. Their Permission was built for it (admins.permission_for()).
    """
    from . import workspaces

    def missing():
        return ApiError(f"No workspace named '{named}'", 404, code='workspace_not_found')

    if permission.via is None:  # a key or a signed-in user: theirs alone
        if named is not None and named != permission.workspace:
            raise missing()
        g.workspace = permission.workspace
        return permission
    if named is not None and (not workspaces.exists(named)
                              or (not permission.server_wide and named not in (permission.memberships or {}))):
        raise missing()
    if permission.workspace != g.workspace:  # the shared key and an open server: an owner, wherever it is named
        permission = permission._replace(workspace=g.workspace)
    return permission


def resolve_permission():
    """The caller's Permission: a matched key (admin or scoped), a signed-in user's verified bearer token
    (jwtauth.py), an administrator's Console session (sso.py), the unrestricted OPEN default when no key is
    configured anywhere, or None when a key is required but missing or wrong. A request carrying an X-API-Key is
    judged on that key alone - a wrong key never falls through to the token - and a session cookie counts only when
    neither header is sent, and only with its CSRF token (X-QAG-CSRF)."""
    api_key, authorization = request.headers.get('X-API-Key', ''), request.headers.get('Authorization', '')
    if not api_key and not authorization and request.cookies.get(sso.SESSION_COOKIE):
        permission = sso.authenticate(request.cookies.get(sso.SESSION_COOKIE), request.headers.get(sso.CSRF_HEADER))
        if permission is not None:
            return permission
    return authenticate_headers(api_key, authorization, request.remote_addr)


def authenticate_headers(api_key, authorization, client_ip):
    """resolve_permission() without Flask - governance.authenticate(), kept under this name for events.py."""
    return governance.authenticate(api_key, authorization, client_ip)


def require_connection(connection_name):
    if not apikeys.can_use(g.permission, connection_name):
        raise ApiError(f"This API key is not permitted to use the connection '{connection_name}'", 403,
                       code='connection_forbidden')


def caller_key_name():
    """The calling API key's name for logs/metrics ('admin', a scoped key's name, or '-'); safe to call even
    before permission is resolved, e.g. while rendering a CORS preflight or a 429 in decorate()."""
    permission = g.get('permission')
    return (permission.name if permission else None) or '-'


def metrics_key_name():
    """The caller as /metrics counts it (metrics.label_for()): an API key's name, `admin` for any administrator."""
    return metrics.label_for(g.get('permission'))


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
    page_size = limits.rows(page_size)  # the caller's own max_rows, if smaller: pages of that size (ADR 0011)
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
    with tracing.span(f'serialize {output_format}', attributes={'queryapigate.format': output_format}) as span:
        response = jsonify({'message': 'No results returned'}) if not result else FORMATTERS[output_format](result)
        tracing.set_attributes(span, queryapigate__bytes=response.content_length)
    elapsed = time.monotonic() - started
    metrics.observe_serialization(output_format, elapsed)
    g.serialization_ms = round(elapsed * 1000, 1)
    response.headers['X-Page'] = str(page)
    response.headers['X-Page-Size'] = str(page_size)
    response.headers['X-Row-Count'] = str(len(result.rows) if result else 0)
    response.headers['X-Has-More'] = 'true' if result.has_more else 'false'
    return response


def get_stream_flag():
    """Whether ?stream=true was requested: the whole result, streamed as it comes off the cursor instead of
    one page built in memory first. Only csv/tsv/ndjson support it - see formats.STREAM_FORMATTERS."""
    return (request.args.get('stream') or '').strip().lower() in ('1', 'true', 'yes', 'on')


def stream_sql_response(sql, connection_name, params, timeout, output_format, filename, saved=None,
                        allowed_tables=None, database=None, mask=None):
    """Shared by execute_sql_endpoint() and run_saved(): validates the stream=true-specific constraints
    (format, no pagination) and returns the chunked Response, the run recorded in history once the stream
    finishes - in its saved query's, when ``saved`` is (path, version), or as an ad-hoc run."""
    if output_format not in STREAM_FORMATTERS:
        raise ApiError(f"stream=true only supports these formats: {', '.join(sorted(STREAM_FORMATTERS))}",
                       code='invalid_stream')
    if request.args.get('page') or request.args.get('page_size'):
        raise ApiError('stream=true exports the whole result and does not accept page/page_size', code='invalid_stream')
    key_name = metrics_key_name()
    columns, rows = engine.stream_sql(sql, connection_name, params, timeout, key_name=key_name,
                                      allowed_tables=allowed_tables, mask=mask)
    # Captured here, not inside _record_stream_history(): that generator's body runs lazily, as the response
    # streams out - by then the request/app context this view function runs in is long gone (no
    # flask.stream_with_context() wrapping is used), so g, caller_key_name() and current_app are only safe to read
    # up front, while still inside the request that's actually issuing the query - this is why the broadcaster
    # instance itself is captured here too.
    broadcaster = current_app.extensions['queryapigate_broadcaster']
    entry = {**run_entry(connection_name), **masked_entry(rows.masked)}
    if saved is not None:
        path, number = saved

        def record(finished):
            _record_and_broadcast(path, number, finished, broadcaster)
    else:
        if database:
            entry['database'] = database

        def record(finished):
            _record_adhoc_and_broadcast(sql, params, finished, broadcaster)
    opened = rows
    response = stream_response(output_format, columns,
                               _record_stream_history(rows, entry, record, g.get('permission')), filename)
    # A response closed before its first chunk - the client gone at once - closes a generator that never started,
    # which runs none of its code: the rows' own close() is what ends the query, returns its connection and frees
    # its slot in the caller's max_concurrent. Idempotent, so a stream read to its end is unaffected.
    response.call_on_close(opened.close)
    return response


def stream_mongo_response(collection, filter_doc, connection_name, params, timeout, output_format, filename,
                          projection=None, sort=None, saved=None, statement=None, entry=None):
    """stream_sql_response() for a find() (ADR 0022): the same constraints and history, the documents shaped by
    their format (mongodocs.chunks()). A CSV or TSV run that left fields out says how many in its history entry -
    the response can't, its headers went out with the first row."""
    if output_format not in STREAM_FORMATTERS:
        raise ApiError(f"stream=true only supports these formats: {', '.join(sorted(STREAM_FORMATTERS))}",
                       code='invalid_stream')
    if request.args.get('page') or request.args.get('page_size'):
        raise ApiError('stream=true exports the whole result and does not accept page/page_size', code='invalid_stream')
    docs = engine.stream_mongo(collection, filter_doc, connection_name, params, timeout, projection=projection,
                               sort=sort, key_name=metrics_key_name(), allowed_tables=g.permission.allowed_tables,
                               mask=g.permission.mask)
    broadcaster = current_app.extensions['queryapigate_broadcaster']  # captured now: see stream_sql_response()
    entry = {**run_entry(connection_name), **(entry or {})}
    shaped = None

    def record(finished):
        finished = {**finished, **(shaped.report() if shaped is not None else {})}
        if saved is not None:
            _record_and_broadcast(*saved, finished, broadcaster)
        else:
            _record_adhoc_and_broadcast(statement, params, finished, broadcaster)

    body, shaped = mongodocs.chunks(output_format, _record_stream_history(docs, entry, record, g.get('permission')),
                                    projection)
    response = chunked_response(output_format, body, filename)
    response.call_on_close(docs.close)  # as in stream_sql_response(): a client gone before the first chunk
    return response


def masked_entry(fired):
    """A run's history field for what masking did (ADR 0005): which result columns, which action, from which source
    - so "was national_id ever returned to this key?" is answered by history. Nothing when nothing was masked."""
    return {'masked': fired} if fired else {}


def run_entry(connection_name):
    """The start of one run's history entry: when, where, by whom, through which front door."""
    trace_id = tracing.ids()[0]
    return {'executed_at': store.now(), 'connection_name': connection_name, 'request_id': g.get('request_id'),
            'key_name': caller_key_name(), 'transport': g.get('transport', 'rest'),
            **({'trace_id': trace_id} if trace_id else {})}


def _record_adhoc_and_broadcast(sql, params, entry, broadcaster=None):
    """An ad-hoc run's _record_and_broadcast(): into run history (history.record_adhoc()) and the live feed, as an
    `query.run` event of kind `adhoc`, under the same per-key rule as a saved query's run."""
    recorded = history.record_adhoc(entry, sql, params)
    if broadcaster is None:
        broadcaster = current_app.extensions['queryapigate_broadcaster']
    broadcaster.publish(events.run_payload(None, None, recorded), entry['key_name'], scope.current())


def _record_and_broadcast(path, number, entry, broadcaster=None):
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
    broadcaster.publish(events.run_payload(path, number, entry), entry['key_name'], scope.current())


def _record_stream_history(rows, entry, record, permission=None):
    """_stream_history(), in the request's workspace: its body runs as the response streams out, after the request -
    and with it scope.current()'s answer - has gone (scope.py)."""
    workspace = scope.current()

    def recorded():
        with scope.using(workspace):
            yield from _stream_history(rows, entry, record, permission)

    return recorded()


def _stream_history(rows, entry, record, permission=None):
    """Records a streamed run, through ``record``, once fully drained or failed partway through (not on a client
    disconnect, GeneratorExit) - counting rows as they pass through, since the total is not known up front, the
    same trade-off engine._drain() makes for the streaming metric. However it ends, the rows that left count against
    the caller's usage quota (``permission``, captured while still in the request)."""
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
    finally:
        quotas.count_rows(permission, count)


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
    # Optional ad-hoc masking (ADR 0005), e.g. the API Designer previewing a query's mask - unioned with the caller's
    # own key mask, so a preview can never reveal a column the key already hides.
    mask = masking.validate_rules(data.get('mask'))
    effective_mask = (mask or []) + (g.permission.mask or []) or None
    if get_stream_flag():
        return stream_sql_response(data['sql'], data['connection_name'], params, timeout, output_format,
                                   filename=data['connection_name'], allowed_tables=g.permission.allowed_tables,
                                   database=database, mask=effective_mask)
    limit, offset, page = get_pagination()
    entry = run_entry(data['connection_name'])
    if database:
        entry['database'] = database
    try:
        result, elapsed_ms = engine.timed(engine.execute_sql, data['sql'], data['connection_name'], limit, offset,
                                          params, timeout, allow_writes=g.permission.allow_writes,
                                          key_name=metrics_key_name(), allowed_write_ops=g.permission.allowed_write_ops,
                                          database=database, allowed_tables=g.permission.allowed_tables,
                                          mask=effective_mask)
    except ApiError as error:
        _record_adhoc_and_broadcast(data['sql'], params,
                                    {**entry, 'status': 'error', 'error': error.message, 'code': error.code})
        raise
    _record_adhoc_and_broadcast(data['sql'], params,
                                {**entry, 'status': 'success', 'rows': len(result.rows), 'duration_ms': elapsed_ms,
                                 **masked_entry(result.masked)})
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
    statement = json.dumps({'collection': data['collection'], 'filter': filter_doc}, default=str)
    if get_stream_flag():
        return stream_mongo_response(data['collection'], filter_doc, data['connection_name'], params, timeout,
                                     output_format, filename=data['collection'], projection=data.get('projection'),
                                     sort=data.get('sort'), statement=statement,
                                     entry={'query_type': 'mongo', 'mongo_collection': data['collection']})
    limit, offset, page = get_pagination()
    entry = {**run_entry(data['connection_name']), 'query_type': 'mongo', 'mongo_collection': data['collection']}
    try:
        result, elapsed_ms = engine.timed(engine.execute_mongo, data['collection'], filter_doc,
                                          data['connection_name'], limit, offset, params, timeout,
                                          projection=data.get('projection'), sort=data.get('sort'),
                                          key_name=metrics_key_name(), mask=g.permission.mask,
                                          allowed_tables=g.permission.allowed_tables)
    except ApiError as error:
        _record_adhoc_and_broadcast(statement, params,
                                    {**entry, 'status': 'error', 'error': error.message, 'code': error.code})
        raise
    _record_adhoc_and_broadcast(statement, params,
                                {**entry, 'status': 'success', 'rows': len(result.rows), 'duration_ms': elapsed_ms})
    return render(result, output_format, page, limit)


def _client_cache_control(ttl):
    """How long the *caller* may keep a cached response - `private`, so only the caller's own client may: a request
    authenticates with X-API-Key, not Authorization, so HTTP's rule keeping authenticated responses out of shared
    caches (RFC 9111 3.5) doesn't apply, and a CDN or caching proxy in front would otherwise keep one caller's rows -
    masked for its key - and serve them to the next caller of that URL, with any key or none."""
    return f'private, max-age={ttl}'


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
    response.headers['Cache-Control'] = _client_cache_control(ttl)
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
    response.headers['Cache-Control'] = _client_cache_control(ttl)
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
    if g.permission.claim_binding and not bound:
        raise ApiError("Your role runs only saved queries that take a parameter from your sign-in token, and this one "
                       "doesn't (require_claim_binding)", 403, code='claim_binding_required')
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


def bind_embeddings(saved, values, connection_name):
    """Replace each `embed` parameter's text with its embedding vector, as a pgvector literal (embeddings.py),
    for a semantic search. Postgres/pgvector only; refused when embeddings aren't configured - never run with
    the plain text bound where a vector belongs, which would error or silently miss. Experimental."""
    declared = param_rules.read_definitions(saved.get('query_parameters'))
    to_embed = [name for name, spec in declared.items() if spec.get('embed') and values.get(name)]
    if not to_embed:
        return values
    if store.get_connection(connection_name)['db'] != 'postgres':
        raise ApiError("An 'embed' parameter needs a Postgres (pgvector) connection", code='unsupported_operation')
    values = dict(values)
    for name in to_embed:
        values[name] = embeddings.vector_literal(embeddings.embed(values[name]))
    return values


def run_saved(ref, body, url_params):
    """_run_saved(), in its own span (tracing.py): named once the query is found, never by what the caller sent."""
    with tracing.span('query') as span:
        g.query_span = span
        return _run_saved(ref, body, url_params)


def _run_saved(ref, body, url_params):
    """Execute a saved query (its published version unless one is requested) and record the run - or, for one
    with a cache_ttl whose SQL is read-only, serve a cached response instead.

    A draft - a version newer than the published one, or any version of a query with none published - runs only
    for the admin key, and only when asked for by number (to test it before publishing). For anyone else it
    doesn't exist: the same 404 as a version number that was never used, so drafts can't be discovered."""
    path = store.resolve_saved_file(ref, check_exists=False)  # load_versions() 404s the same, one read fewer
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
    query_span = g.get('query_span')
    if query_span is not None:
        query_span.update_name(f'query {query_name}')
        tracing.set_attributes(query_span, queryapigate__query=query_name, queryapigate__version=number,
                               queryapigate__connection=connection_name)
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
    agent_write = False
    if g.get('transport') == 'mcp' and saved.get('mcp') is False:
        # Exposure turned off for this query (BACKLOG #54): not a tool over MCP, by name or not - though it still
        # serves over REST. Hidden from tools/list too (mcp_server.list_tools_for).
        raise ApiError(f"'{query_name}' is not available over MCP", 404, code='mcp_disabled')
    if g.get('transport') == 'mcp' and not sqltools.is_read_only(saved['sql_query'],
                                                                  store.get_connection(connection_name)['db']):
        # Over MCP a saved query that writes runs only as an agent write (ADR 0008): its author marked it, the
        # caller's grant allows it, on its own connection - and then through agentwrites.py, approved or insert-only.
        # Otherwise it isn't run, called by name or by version, whatever the caller's grant would allow over REST.
        from . import mcp_server
        if not (mcp_server.agent_may_write(g.permission, query_name, saved)
                and connection_name == saved.get('connection_name')):
            raise ApiError('Over MCP, saved queries only read - this one writes, so it is not run here', 403,
                           code='read_only')
        agent_write = True
    # Per-query write curation (apikeys.can_write_query()) only ever adds write reach for this one named
    # query on top of whatever the key's blanket allow_writes already grants - never the other way round.
    effective_allow_writes = g.permission.allow_writes or apikeys.can_write_query(g.permission, query_name)

    used = set(sqltools.placeholder_names(saved['sql_query']))
    values = param_rules.resolve(saved.get('query_parameters'), raw, used=used)
    values = bind_embeddings(saved, values, connection_name)
    sql = sqltools.fill_placeholders(saved['sql_query'], values)
    # A saved query's own mask rules (ADR 0005) apply to every caller, unioned with any the caller's key has - the
    # strictest rule wins per column (masking.py), so neither can weaken the other.
    effective_mask = (saved.get('mask') or []) + (g.permission.mask or []) or None
    if agent_write:
        return run_agent_write(saved, path, number, query_name, connection_name, values, sql, timeout)

    if get_stream_flag():
        # No caching for a streamed export - caching would require materialising the whole body anyway,
        # defeating the point - but the run is still recorded once the stream finishes, same as any other.
        return stream_sql_response(sql, connection_name, values, timeout, output_format, filename=ref,
                                   saved=(path, number), allowed_tables=g.permission.allowed_tables,
                                   mask=effective_mask)

    limit, offset, page = get_pagination()

    # A saved query is only ever cached when it declares a cache_ttl *and* its SQL is read-only - never a
    # write, no matter the setting, since serving a cached response would silently skip that write.
    ttl = saved.get('cache_ttl') or 0
    cache_key = None
    if ttl > 0:
        dialect = store.get_connection(connection_name)['db']
        if sqltools.is_read_only(sql, dialect):
            # Callers with different masking rules get different responses, so they never share one.
            cache_key = cache.ResponseCache.key(name=ref, version=number, connection=connection_name,
                                                values=values, format=output_format, page=page, page_size=limit,
                                                mask=masking.fingerprint(effective_mask),
                                                workspace=scope.current())
            cache_meta = {'name': path, 'version': number, 'connection': connection_name,
                         'format': output_format, 'page': page, 'workspace': scope.current()}
            cached = cache_lookup(cache_key, ttl)
            tracing.set_attributes(query_span, queryapigate__cache='hit' if cached is not None else 'miss')
            if cached is not None:
                return cached

    entry = run_entry(connection_name)
    try:
        result, elapsed_ms = engine.timed(engine.execute_sql, sql, connection_name, limit, offset, values, timeout,
                                          allow_writes=effective_allow_writes, key_name=metrics_key_name(),
                                          allowed_write_ops=g.permission.allowed_write_ops,
                                          allowed_tables=g.permission.allowed_tables, mask=effective_mask)
    except ApiError as error:
        _record_and_broadcast(path, number,
                              {**entry, 'status': 'error', 'error': error.message, 'code': error.code})
        raise
    tracing.set_attributes(query_span, queryapigate__rows=len(result.rows), queryapigate__masked=bool(result.masked))
    response = render(result, output_format, page, limit)  # sets g.serialization_ms - see render()
    _record_and_broadcast(path, number, {**entry, 'status': 'success', 'rows': len(result.rows),
                                         'duration_ms': elapsed_ms,
                                         'serialization_ms': g.serialization_ms,
                                         **masked_entry(result.masked)})
    if cache_key is not None:
        response = cache_store(cache_key, response, ttl, cache_meta)
    elif (g.get('dataplane_record') is not None and g.get('transport', 'rest') == 'rest' and not ttl
          and output_format == 'json'):  # the one format the data plane renders
        plan = dataplane.build(g.dataplane_record, g.permission, query_name, number, saved, connection_name, result,
                               config.allow_writes() and effective_allow_writes)
        if plan is not None:
            response.headers[dataplane.PLAN_HEADER] = dataplane.encode(plan)
    return response


def run_agent_write(saved, path, number, name, connection_name, values, sql, timeout):
    """An AI agent's write through a saved query (agentwrites.py): asked for, run once approved - or, insert-only, run
    at once. Answers with the tool's structured result in g.agent_write_result, which mcp_server.call_tool_for() passes
    on as it is."""
    from . import agentwrites
    args = g.get('agent_write_args') or {}
    caller = caller_key_name()
    tracing.set_attributes(g.get('query_span'), queryapigate__agent_write=saved['agent_writes'])
    if saved['agent_writes'] == 'insert':
        hashed = None
        if args.get('idempotency_key') is not None:
            hashed, earlier = agentwrites.idempotent(caller, name, args['idempotency_key'])
            if earlier is not None:
                if earlier['_arguments_hash'] != agentwrites.arguments_hash(name, number, values):
                    raise ApiError('This idempotency_key was used with other arguments - use a new one', 409,
                                   code='idempotency_conflict')
                return _agent_write_answer(earlier)
        request = agentwrites.create(caller, name, number, saved, connection_name, values, status='running',
                                     idempotency=hashed)
        return _execute_agent_write(request, saved, path, number, connection_name, values, sql, timeout)
    approval = args.get('approval')
    if not approval:
        request = agentwrites.create(caller, name, number, saved, connection_name, values)
        url = agentwrites.approval_url(request['id'])
        return _agent_write_answer({**request, 'status': 'approval_required'}, approve_at=url, message=(
            f"A person must approve this write before it runs. Ask them to open {url} - then call this tool again with "
            f"the same arguments and approval: \"{request['id']}\". It expires at {request['expires_at']}."))
    state, request = agentwrites.claim(approval, caller, name, number, values)
    if state == 'finished':
        return _agent_write_answer(request)
    return _execute_agent_write(request, saved, path, number, connection_name, values, sql, timeout)


def _execute_agent_write(request, saved, path, number, connection_name, values, sql, timeout):
    from . import agentwrites
    entry = {**run_entry(connection_name), 'request': request['id'],
             **({'approved_by': request['decided_by']} if request['decided_by'] else {})}
    effective_mask = (saved.get('mask') or []) + (g.permission.mask or []) or None
    try:
        result, affected = engine.execute_agent_write(sql, connection_name, values, saved.get('max_affected_rows') or 1,
                                                      timeout, key_name=metrics_key_name(),
                                                      allowed_write_ops=g.permission.allowed_write_ops,
                                                      allowed_tables=g.permission.allowed_tables,
                                                      mask=effective_mask)
    except ApiError as error:
        agentwrites.finish(request['id'], error={'error': error.message, 'code': error.code or 'internal_error'})
        _record_and_broadcast(path, number,
                              {**entry, 'status': 'error', 'error': error.message, 'code': error.code})
        raise
    limit = config.mcp_max_rows()
    rows = [dict(zip(result.columns, row, strict=False)) for row in result.rows[:limit]]
    done = agentwrites.finish(request['id'], result={'rows_affected': affected, 'rows': rows,
                                                     'truncated': len(result.rows) > limit})
    _record_and_broadcast(path, number, {**entry, 'status': 'success', 'rows': affected,
                                         **masked_entry(result.masked)})
    return _agent_write_answer(done)


def _agent_write_answer(request, **extra):
    """The write tool's structured result: approval_required (with where to approve), done (what it changed), or - for
    a request that ran and failed - the error it failed with, raised again."""
    if request['status'] == 'failed':
        failure = request['result'] or {}
        # what it failed with the first time - finish() always stores a code
        raise ApiError(failure.get('error') or 'This write failed', 409, code=failure.get('code') or 'internal_error')
    if request['status'] == 'done':
        result = request['result'] or {}
        payload = {'status': 'done', 'request': request['id'], 'rows_affected': result.get('rows_affected', 0),
                   'rows': result.get('rows') or [], 'truncated': bool(result.get('truncated'))}
    else:
        payload = {'status': request['status'], 'request': request['id'], 'expires_at': request['expires_at'],
                   'rows': [], 'truncated': False, **extra}
    g.agent_write_result = payload
    return jsonify(payload)


def run_saved_mongo(saved, path, number, ref, connection_name, raw, output_format, timeout):
    """The mongo query_type branch of run_saved(): a find() query instead of SQL. No caching (cache_ttl is
    not yet supported for a mongo saved query). ?stream=true streams the whole result (ADR 0022). Always
    read-only, same as the ad-hoc /execute_mongo."""
    used = set(mongotools.placeholder_names(saved.get('mongo_filter') or {}))
    values = param_rules.resolve(saved.get('query_parameters'), raw, used=used)
    filter_doc = mongotools.fill_placeholders(saved.get('mongo_filter') or {}, values)
    if get_stream_flag():
        return stream_mongo_response(saved['mongo_collection'], filter_doc, connection_name, None, timeout,
                                     output_format, filename=ref,
                                     projection=saved.get('mongo_projection'), sort=saved.get('mongo_sort'),
                                     saved=(path, number))
    limit, offset, page = get_pagination()
    entry = run_entry(connection_name)
    try:
        result, elapsed_ms = engine.timed(engine.execute_mongo, saved['mongo_collection'], filter_doc,
                                          connection_name, limit, offset, timeout=timeout,
                                          projection=saved.get('mongo_projection'), sort=saved.get('mongo_sort'),
                                          key_name=metrics_key_name(), mask=g.permission.mask,
                                          allowed_tables=g.permission.allowed_tables)
    except ApiError as error:
        _record_and_broadcast(path, number,
                              {**entry, 'status': 'error', 'error': error.message, 'code': error.code})
        raise
    response = render(result, output_format, page, limit)  # sets g.serialization_ms - see render()
    _record_and_broadcast(path, number, {**entry, 'status': 'success', 'rows': len(result.rows),
                                         'duration_ms': elapsed_ms,
                                         'serialization_ms': g.serialization_ms})
    return response


@bp.route('/q/<name>', methods=['GET', 'POST'])
def run_named_query(name):
    body = get_json_body(required=False) if request.method == 'POST' else {}
    url_params = {k: v for k, v in request.args.items() if k not in RESERVED_ARGS}
    return run_saved(name, body, url_params)


@bp.route('/internal/dataplane/runs', methods=['POST'])
def dataplane_runs():
    """The runs a data plane in front of this server served itself (dataplane.py, ADR 0028), batched: each recorded as
    run_saved() records one - run history, the event log, the live feed - and counted in /metrics as it would have been.
    Only the data plane reaches it: gate() answers 404 to anything without its secret. `dropped`: runs it served but
    couldn't queue for here, counted as history's own dropped runs are. `recorded`: with a run store, the data plane has
    written them there itself (ADR 0029), and only `kept` and `sampled_out` of them are counted here.

    Each run comes with its history entry already JSON (`entry`), and the labels and timings /metrics counts it by: at
    the data plane's rates Python's own work per run - encoding it, and its labels - is what it can't keep up with, so
    a batch is queued at once (history.record_reported()) and counted once per set of labels."""
    report = request.get_json(silent=True) or {}
    runs = report.get('runs') or []
    broadcaster = current_app.extensions['queryapigate_broadcaster']
    with scope.using(scope.current()):  # resolved once, not by each of a busy batch's few thousand label lookups
        if report.get('recorded'):  # in the run store already: written by the data plane itself (ADR 0029 §4)
            metrics.inc_history('recorded', int(report.get('kept') or 0))
            metrics.inc_history('sampled_out', int(report.get('sampled_out') or 0))
        else:
            history.record_reported(runs)
        if broadcaster.subscriber_count():  # the live feed's events, only when anyone is listening
            for run in runs:
                broadcaster.publish(events.run_payload(run['query'], run['version'], json.loads(run['entry'])),
                                    run['key'], scope.current())
        labelled = {}
        for run in runs:
            seen = labelled.setdefault((run['connection'], run['dialect'], run['status'], run['key'], run['method'],
                                        str(run['http_status'])), ([], [], [], [0]))
            seen[0].append(run['query_seconds'])
            seen[1].append(run['seconds'])
            if run['status'] == 'success':
                seen[2].append(run['serialization_seconds'])
                seen[3][0] += run['rows']
        for (connection, dialect, status, key, method, http_status), seen in labelled.items():
            metrics.observe_queries(connection, dialect, status, seen[0], key)
            if status == 'success':
                metrics.observe_rows(connection, dialect, key, seen[3][0])
                metrics.observe_serializations('json', seen[2])
            metrics.observe_requests(method, 'api.run_named_query', http_status, seen[1], key or '-')
    if report.get('dropped'):
        metrics.inc_history('dropped', int(report['dropped']))
    return '', 204


@bp.route('/internal/dataplane/lease', methods=['POST'])
def dataplane_lease():
    """A data plane's block of the event ids it mints when it writes its runs to the run store itself (runstore.py,
    ADR 0029 §2), leased here so its store access stays read-only: `{"holder": ...}` leases one, `{"holder": ...,
    "block": n}` renews it (409 once it has lapsed: lease again), and with `"release": true` gives it back, as it stops.
    With the layout of an id and the settle window, so the two processes can't disagree on them, and where the run
    store is (`location`, as the metadata store records it): a data plane configured with another one writes nothing
    there. 404 without a run store."""
    if not runstore.enabled():
        return jsonify(error_body('No run store is configured', 404)), 404
    if not request.environ.get('queryapigate.dataplane_paired'):
        return jsonify(error_body(dataplane.UNPAIRED, 409)), 409
    body = request.get_json(silent=True) or {}
    holder = body.get('holder')
    if not isinstance(holder, str) or not 0 < len(holder) <= 200:
        return jsonify(error_body('holder: a name for the data plane process', 400)), 400
    if body.get('release') is True and isinstance(body.get('block'), int):
        runstore.release(body['block'], holder)
        return '', 204
    if body.get('block') is not None:
        if not isinstance(body['block'], int) or not runstore.renew(body['block'], holder):
            return jsonify(error_body('That lease has lapsed - lease a block again', 409)), 409
        return jsonify({'block': body['block'], 'lease_seconds': runstore.LEASE_SECONDS}), 200
    try:
        block = runstore.lease(runstore.DATAPLANE_BLOCK, holder)
    except runstore.LeaseError:
        return jsonify(error_body(runstore.LEASES_TAKEN, 503)), 503  # the only reason, and no exception's text
    return jsonify({'block': block, 'size': runstore.DATAPLANE_BLOCK, 'lease_seconds': runstore.LEASE_SECONDS,
                    'epoch_ms': runstore.EPOCH_MS, 'per_ms': runstore.PER_MS,
                    'settle_seconds': runstore.SETTLE_SECONDS, 'database': runstore.database(),
                    'location': runstore.location(),
                    'sample_rate': config.history_sample_rate()}), 200


# --------------------------------------------------------------------------------------
# Live events
# --------------------------------------------------------------------------------------

_SSE_HEARTBEAT_SECONDS = 15  # module constant so tests can shrink it
_open_streams = 0  # GET /events streams this process holds open right now - see stream_events()
_open_streams_lock = threading.Lock()


@bp.route('/events', methods=['GET'])
def stream_events():
    """Server-Sent Events: one `data: {...}` line per live event (today: a run, `query.run` - events.run_payload() -
    as it's recorded, see _record_and_broadcast()), narrowed by the same subscription filters as `queryapigate
    events` (events.parse_filters()). Originally admin-only, feeding just the admin UI's Home
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
    try:
        filters = events.parse_filters(request.args.get)
    except ValueError as error:
        raise ApiError(str(error), code='invalid_filter') from None
    with _open_streams_lock:
        if _open_streams >= config.events_max_streams():
            raise ApiError('Too many open event streams on this server - try again shortly, or connect to '
                           '`queryapigate events` instead', 503, retry_after=5, code='too_many_streams')
        _open_streams += 1
    broadcaster = current_app.extensions['queryapigate_broadcaster']
    subscriber = broadcaster.subscribe(key_name=None if g.permission.admin else g.permission.name,
                                       workspace=g.permission.workspace)

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

    def stream():
        try:
            while True:
                try:
                    event = subscriber.get(timeout=_SSE_HEARTBEAT_SECONDS)
                except queue.Empty:
                    yield ': keepalive\n\n'
                    continue
                if not events.matches(filters, events.fields(event)):
                    continue
                yield f'data: {json.dumps(event, default=json_default)}\n\n'
        finally:
            release()

    response = Response(ClosingIterator(stream(), release), mimetype='text/event-stream')
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
    result = schema.fetch_schema(name, database=database, allowed_tables=g.permission.allowed_tables)
    return jsonify(result if database else descriptions.annotate(result, name, mask=g.permission.mask,
                                                                       allowed_tables=g.permission.allowed_tables)), 200


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
    return jsonify(schema.fetch_table_ddl(name, table, database=database,
                                          allowed_tables=g.permission.allowed_tables)), 200


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
    zone, offset = store.server_time_zone()
    return jsonify({'status': 'ok', 'version': current_app.config['QUERYAPIGATE_VERSION'], 'time_zone': zone,
                    'utc_offset': offset})


@bp.route('/metrics', methods=['GET'])
def metrics_endpoint():
    """Open, unless QUERYAPIGATE_METRICS_TOKEN is set: then for `Authorization: Bearer <that token>` - a scraper's -
    or an administrator whose role may observe, as the Console's Metrics screen reads it. What the Helm chart sets,
    since its ingress would otherwise publish the counts and the key, query and connection names in them (ADR 0017's
    review)."""
    from . import adminroles
    permission = g.get('permission')
    # every workspace's counts: a server-wide role's to read (ADR 0023)
    observer = adminroles.allows(permission, 'observe') and getattr(permission, 'server_wide', True)
    if not (observer or metrics.authorized(request.headers.get('Authorization'))):
        response = jsonify(error_body(metrics.UNAUTHORIZED, 401))
        response.status_code = 401
        response.headers['WWW-Authenticate'] = 'Bearer realm="metrics"'
        return response
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
        if not apikeys.can_run_saved(permission, name, collection, connection_name, data.get('query_parameters')):
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
        if not apikeys.can_run_saved(permission, name, collection, connection_name, data.get('query_parameters')):
            continue
        parameters = definitions.effective_parameters(data)
        entry = {'name': name, 'version': number, 'description': data.get('description'),
                 'tags': data.get('tags'), 'collection': collection, 'connection_name': connection_name,
                 'parameters': parameters, 'cache_ttl': data.get('cache_ttl') or None,
                 'can_write': apikeys.can_write_query(permission, name)}
        if permission.mask:  # what this caller's masking rules do to it - or that they'd refuse it
            try:
                dialect = store.get_connection(connection_name)['db'] if data.get('query_type') != 'mongo' else 'mongo'
                entry['masking'] = masking.describe(permission.mask, data.get('sql_query') or '', connection_name,
                                                    dialect)
            except ApiError as error:  # an inactive or missing connection: it can't be run either
                entry['masking'] = {'masked': [], 'refused': error.message}
        catalog.append(entry)
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
            'mask': permission.mask,
            'rate_limit': config.format_rate_limit(permission.rate_limit),
            'server_rate_limit': config.format_rate_limit(config.rate_limit()),
        },
    }), 200


@bp.route('/docs', methods=['GET'])
def docs():
    response = Response(openapi.DOCS_HTML, mimetype='text/html')
    response.headers['Content-Security-Policy'] = openapi.DOCS_CSP
    response.headers['Referrer-Policy'] = 'same-origin'
    return response


@bp.route('/ui', methods=['GET'])
def admin_ui():
    """The admin UI moved to the Console (ADR 0001); old bookmarks land there."""
    return redirect('/console/', 301)


@bp.route('/console', methods=['GET'])
@bp.route('/console/', methods=['GET'])
@bp.route('/console/<path:path>', methods=['GET'])
def console_page(path=''):
    return console.serve(path)

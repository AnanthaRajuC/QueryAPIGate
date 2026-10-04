"""Runtime configuration, read from environment variables at call time."""
import functools
import math
import os
import re
from pathlib import Path

from . import experimental

SUPPORTED_DB_TYPES = ('mysql', 'postgres', 'clickhouse', 'sqlite', 'h2', 'jdbc', 'duckdb', 'mongo')
PASSWORD_MASK = '********'
CONNECT_TIMEOUT = 10  # seconds
HISTORY_LIMIT = 50  # executions remembered per saved-query version (QUERYAPIGATE_HISTORY_LIMIT)
HISTORY_ADHOC_LIMIT = 1000  # ad-hoc SQL runs remembered in all (QUERYAPIGATE_HISTORY_ADHOC_LIMIT)
HISTORY_ADHOC_SQL_MODES = ('text', 'hash', 'none')  # what an ad-hoc run's history keeps of its SQL
HISTORY_ADHOC_SQL_MAX = 4000  # characters of ad-hoc SQL text kept, at most
DEFAULT_HISTORY_FLUSH_INTERVAL = 1.0  # seconds between batched history writes
DEFAULT_EVENTS_PORT = 5002  # `queryapigate events`
DEFAULT_EVENTS_MAX_CONNECTIONS = 10_000
DEFAULT_EVENTS_POLL_INTERVAL = 1.0  # seconds between `queryapigate events` checks for new runs
DEFAULT_EVENTS_MAX_STREAMS = 4  # concurrent GET /events streams the main server itself will hold open
AUDIT_LOG_LIMIT = 500  # administrative-action entries remembered across the whole server
DEFAULT_QUERY_TIMEOUT = 30.0  # seconds
DEFAULT_POOL_SIZE = 5  # idle connections kept per distinct connection
DEFAULT_POOL_IDLE_TIMEOUT = 300.0  # seconds
DEFAULT_SLOW_QUERY_THRESHOLD = 1.0  # seconds
DEFAULT_ALERT_ERROR_RATE = 20  # percent of a saved query's recent runs failing before it is an alert (alerts.py)
DEFAULT_ALERT_KEY_UNUSED_DAYS = 90  # days an API key may go unused before it is an alert
DEFAULT_MCP_PORT = 5001  # distinct from QUERYAPIGATE_PORT so `queryapigate mcp` and `serve` can run together
DEFAULT_MCP_MAX_ROWS = 200  # rows returned by an MCP tools/call - an LLM's context can't hold a huge result

# Ships with the package; override with QUERYAPIGATE_H2_JAR to use a different H2 version.
BUNDLED_H2_JAR = Path(__file__).parent / 'lib' / 'h2-2.2.224.jar'

# Shown by `queryapigate init`. Every entry starts inactive so nothing connects until you opt in.
EXAMPLE_CONNECTIONS = {
    'connections': {
        'example-sqlite': {'db': 'sqlite', 'database': 'example.db', 'active': False},
        'example-duckdb': {'db': 'duckdb', 'database': 'example.duckdb', 'active': False},
        'example-postgres': {'db': 'postgres', 'host': 'localhost', 'port': 5432, 'database': 'postgres',
                             'user': 'postgres', 'password': '${POSTGRES_PASSWORD}', 'active': False},
        'example-mysql': {'db': 'mysql', 'host': 'localhost', 'port': 3306, 'database': 'mydb',
                          'user': 'root', 'password': '${MYSQL_PASSWORD}', 'active': False},
        'example-clickhouse': {'db': 'clickhouse', 'host': 'localhost', 'port': 9000, 'database': 'default',
                               'user': 'default', 'password': '', 'active': False},
        'example-h2': {'db': 'h2', 'host': 'localhost', 'database': 'test', 'user': 'SA', 'password': '',
                       'active': False},
    }
}


def env_flag(name):
    return os.environ.get(name, '').strip().lower() in ('1', 'true', 'yes', 'on')


_FLAG_WORDS = ('', '0', '1', 'true', 'false', 'yes', 'no', 'on', 'off')


def load_examples():
    """QUERYAPIGATE_LOAD_EXAMPLES: load the example APIs at startup (idempotent) - the container-friendly way to get
    what ``queryapigate examples load`` does. Unset or a no-word means leave things as they are."""
    return env_flag('QUERYAPIGATE_LOAD_EXAMPLES')


def home():
    """Folder holding queryapigate.db (QUERYAPIGATE_HOME, default: current directory)."""
    return _resolved_home(os.environ.get('QUERYAPIGATE_HOME') or '', os.getcwd())


@functools.lru_cache(maxsize=32)
def _resolved_home(raw, cwd):
    """Path.resolve() walks every path component with lstat() - db.connection() calls home() several times
    per request, so resolving it once per distinct (QUERYAPIGATE_HOME, cwd) pair, rather than every call,
    takes that filesystem work off the hot path while still following a changed env var or cwd (every test
    gets a fresh temp home)."""
    return Path(raw or cwd).resolve()


def connections_file():
    return home() / 'db_connections.json'


def db_file():
    """queryapigate.db - the SQLite store for everything this app persists: connections, saved queries, API
    keys, roles and the audit log (db.py, store.py, apikeys.py). Created by `queryapigate init` on a fresh
    home; on an existing home with legacy db_connections.json/saved_sql/api_keys.json/roles.json/
    audit_log.json, those are imported automatically, once, the first time the server or CLI runs against
    it - see store.import_legacy_data_if_empty()/apikeys.import_legacy_keys_if_empty()/
    import_legacy_roles_if_empty()."""
    return home() / 'queryapigate.db'


def api_keys_file():
    return home() / 'api_keys.json'


def roles_file():
    return home() / 'roles.json'


def audit_log_file():
    return home() / 'audit_log.json'


def audit_log_limit():
    """Administrative-action entries remembered in queryapigate.db's audit_log table
    (QUERYAPIGATE_AUDIT_LOG_LIMIT), default AUDIT_LOG_LIMIT (500). Always a positive count, never "unbounded"
    like stream_max_rows() can be: each audit event trims the table back down to this cap in the same
    transaction as its own insert (see store.record_audit()), so letting it grow without bound would mean an
    ever-larger table and index, not a slower write. Raise this for longer retention, or set
    QUERYAPIGATE_AUDIT_LOG_EXPORT_FILE for retention this cap can never roll off. Validated at startup
    (check_settings()), the same "fail loudly on a typo" treatment stream_max_rows() gets."""
    raw = os.environ.get('QUERYAPIGATE_AUDIT_LOG_LIMIT', '').strip()
    return int(raw) if raw else AUDIT_LOG_LIMIT


def history_limit():
    """Runs kept per saved-query version (QUERYAPIGATE_HISTORY_LIMIT), default HISTORY_LIMIT (50), unless a
    retention period is set - and, whatever the retention, how many of a version's newest runs
    store.load_versions() carries (the full history is paged through GET /api/v1/history). Validated at startup by
    check_settings()."""
    raw = os.environ.get('QUERYAPIGATE_HISTORY_LIMIT', '').strip()
    return int(raw) if raw else HISTORY_LIMIT


def history_adhoc_limit():
    """QUERYAPIGATE_HISTORY_ADHOC_LIMIT: ad-hoc SQL runs (/execute_sql, MCP's execute_sql) kept in all, newest first,
    unless a retention period is set - they have no saved-query version to be capped per. Default 1000."""
    raw = os.environ.get('QUERYAPIGATE_HISTORY_ADHOC_LIMIT', '').strip()
    return int(raw) if raw else HISTORY_ADHOC_LIMIT


def history_adhoc_sql():
    """QUERYAPIGATE_HISTORY_ADHOC_SQL: what an ad-hoc run's history entry keeps of its SQL - `text` (default; the
    first HISTORY_ADHOC_SQL_MAX characters), `hash` (its SHA-256 only: which statements repeat, never their text,
    e.g. when SQL carries literal values that mustn't be stored) or `none`. Validated at startup."""
    raw = os.environ.get('QUERYAPIGATE_HISTORY_ADHOC_SQL', '').strip().lower()
    return raw or 'text'


def history_retention_days():
    """QUERYAPIGATE_HISTORY_RETENTION_DAYS: keep every run for this many days instead of only each version's newest
    history_limit() runs - for watching API behaviour over time, best on a PostgreSQL store. None (the
    default) keeps the per-version cap. Validated at startup by check_settings()."""
    raw = os.environ.get('QUERYAPIGATE_HISTORY_RETENTION_DAYS', '').strip()
    return int(raw) if raw else None


def history_sample_rate():
    """QUERYAPIGATE_HISTORY_SAMPLE_RATE: the fraction (0 < rate <= 1) of *successful* runs written to history;
    failed runs are always written, since those are the ones worth looking into. Default 1 (every run)."""
    raw = os.environ.get('QUERYAPIGATE_HISTORY_SAMPLE_RATE', '').strip()
    return float(raw) if raw else 1.0


def history_flush_interval():
    """QUERYAPIGATE_HISTORY_FLUSH_INTERVAL: seconds between batched history writes (history.py). 0 writes each
    run's entry inside its own request instead, as before batching existed. Default 1."""
    raw = os.environ.get('QUERYAPIGATE_HISTORY_FLUSH_INTERVAL', '').strip()
    return float(raw) if raw else DEFAULT_HISTORY_FLUSH_INTERVAL


def audit_log_export_file():
    """Optional path (QUERYAPIGATE_AUDIT_LOG_EXPORT_FILE) to also append every audit entry to, one JSON object per
    line, appended only - never capped or rewritten like the audit_log table itself, so retention here doesn't
    depend on audit_log_limit() being sized generously enough. None when unset (today's behaviour,
    unchanged): nothing exported beyond the audit_log table's own rolling window."""
    raw = os.environ.get('QUERYAPIGATE_AUDIT_LOG_EXPORT_FILE', '').strip()
    return Path(raw) if raw else None


def saved_sql_dir():
    return home() / 'saved_sql'


def h2_jar():
    return os.environ.get('QUERYAPIGATE_H2_JAR') or str(BUNDLED_H2_JAR)


def allow_writes():
    return env_flag('QUERYAPIGATE_ALLOW_WRITES')


def api_key():
    return os.environ.get('QUERYAPIGATE_API_KEY') or None


def query_timeout():
    """Server-wide statement time limit in seconds (QUERYAPIGATE_QUERY_TIMEOUT); None when disabled (set to 0)."""
    try:
        value = float(os.environ.get('QUERYAPIGATE_QUERY_TIMEOUT', DEFAULT_QUERY_TIMEOUT))
    except ValueError:
        return DEFAULT_QUERY_TIMEOUT
    if value == 0:
        return None
    return value if value > 0 else DEFAULT_QUERY_TIMEOUT


def effective_timeout(requested=None):
    """The limit to enforce: a request may ask for less time than the server allows, never more."""
    limit = query_timeout()
    if requested is None:
        return limit
    return requested if limit is None else min(requested, limit)


def pool_size():
    """Idle connections kept per distinct connection (QUERYAPIGATE_POOL_SIZE); 0 disables pooling."""
    try:
        return max(0, int(os.environ.get('QUERYAPIGATE_POOL_SIZE', DEFAULT_POOL_SIZE)))
    except ValueError:
        return DEFAULT_POOL_SIZE


def pool_idle_timeout():
    """Seconds an idle pooled connection is kept before it is closed (QUERYAPIGATE_POOL_IDLE_TIMEOUT)."""
    try:
        value = float(os.environ.get('QUERYAPIGATE_POOL_IDLE_TIMEOUT', DEFAULT_POOL_IDLE_TIMEOUT))
    except ValueError:
        return DEFAULT_POOL_IDLE_TIMEOUT
    return value if value > 0 else DEFAULT_POOL_IDLE_TIMEOUT


def cors_origins():
    """Origins allowed to call the API from a browser (QUERYAPIGATE_CORS_ORIGINS): None (off), '*' or a frozenset.

    Entries are compared case-insensitively and without a trailing slash, e.g. https://app.example.com.
    """
    raw = os.environ.get('QUERYAPIGATE_CORS_ORIGINS', '').strip()
    if not raw:
        return None
    origins = [item.strip().rstrip('/').lower() for item in raw.split(',') if item.strip()]
    if '*' in origins:
        return '*'
    return frozenset(origins) or None


_RATE_PERIODS = {'second': 1, 'minute': 60, 'hour': 3600, 'day': 86400}
_RATE_RE = re.compile(r'^\s*(\d+)\s*/\s*(second|minute|hour|day)s?\s*$', re.I)


def parse_rate_limit(text, label='QUERYAPIGATE_RATE_LIMIT'):
    """Parse '60/minute' (also second, hour, day) into (requests, seconds); raises ValueError when malformed.
    ``label`` names the setting in the error message - the default fits this function's own env var, but a
    caller validating the same grammar for something else (e.g. apikeys.py's per-key rate_limit) should
    pass its own name so the message doesn't misleadingly point at QUERYAPIGATE_RATE_LIMIT."""
    match = _RATE_RE.match(text or '')
    if not match or int(match.group(1)) < 1:
        raise ValueError(f"{label} must look like '60/minute' (a positive count, then second, minute, "
                         f"hour or day), not {text!r}")
    return int(match.group(1)), _RATE_PERIODS[match.group(2).lower()]


_RATE_PERIOD_NAMES = {seconds: name for name, seconds in _RATE_PERIODS.items()}


def format_rate_limit(rate_limit):
    """The inverse of parse_rate_limit(): (60, 60) -> '60/minute'. None in, None out - for rendering an
    already-resolved (requests, seconds) pair (e.g. a Permission's own rate_limit) back into the same
    human-readable form it was originally configured in."""
    if rate_limit is None:
        return None
    count, seconds = rate_limit
    return f'{count}/{_RATE_PERIOD_NAMES.get(seconds, str(seconds) + "s")}'


def rate_limit():
    """(requests, seconds) allowed per client (QUERYAPIGATE_RATE_LIMIT), or None when limiting is off."""
    raw = os.environ.get('QUERYAPIGATE_RATE_LIMIT', '').strip()
    if not raw:
        return None
    try:
        return parse_rate_limit(raw)
    except ValueError:
        return None  # create_app() rejects a malformed value at startup; never limit by accident afterwards


def proxy_hops():
    """Reverse proxies in front of the app whose X-Forwarded-* headers can be trusted (QUERYAPIGATE_TRUST_PROXY)."""
    try:
        return max(0, int(os.environ.get('QUERYAPIGATE_TRUST_PROXY', 0)))
    except ValueError:
        return 0


def check_settings():
    """Raise ValueError for a malformed setting, so a typo fails at startup instead of silently switching off a
    protection."""
    # This project was named SQL2API before it was QueryAPIGate, and its settings were SQL2API_*. They are
    # deliberately not read any more - but silently ignoring a leftover SQL2API_API_KEY would start the server
    # with no admin key configured, which means open access. So a leftover old name is a startup error.
    legacy = sorted(name for name in os.environ if name.startswith('SQL2API_'))
    if legacy:
        renamed = ', '.join(f"{name} -> QUERYAPIGATE_{name[len('SQL2API_'):]}" for name in legacy)
        raise ValueError(f'these settings use the old SQL2API_ prefix, which is no longer read: {renamed}. '
                         'Rename each one; ignoring them silently could leave the server without an API key')
    raw = os.environ.get('QUERYAPIGATE_LOAD_EXAMPLES', '').strip().lower()
    if raw not in _FLAG_WORDS:
        raise ValueError(f"QUERYAPIGATE_LOAD_EXAMPLES must be yes or no (or 1/0, true/false, on/off), not '{raw}'")
    raw = os.environ.get('QUERYAPIGATE_RATE_LIMIT', '').strip()
    if raw:
        parse_rate_limit(raw)
    raw = os.environ.get('QUERYAPIGATE_STREAM_MAX_ROWS', '').strip()
    if raw and (not raw.isdigit() or int(raw) < 1):
        raise ValueError('QUERYAPIGATE_STREAM_MAX_ROWS must be a positive integer')
    raw = os.environ.get('QUERYAPIGATE_AUDIT_LOG_LIMIT', '').strip()
    if raw and (not raw.isdigit() or int(raw) < 1):
        raise ValueError('QUERYAPIGATE_AUDIT_LOG_LIMIT must be a positive integer')
    raw = os.environ.get('QUERYAPIGATE_HISTORY_ADHOC_SQL', '').strip().lower()
    if raw and raw not in HISTORY_ADHOC_SQL_MODES:
        raise ValueError(f"QUERYAPIGATE_HISTORY_ADHOC_SQL must be one of: {', '.join(HISTORY_ADHOC_SQL_MODES)}")
    for name in ('QUERYAPIGATE_HISTORY_LIMIT', 'QUERYAPIGATE_HISTORY_ADHOC_LIMIT',
                 'QUERYAPIGATE_HISTORY_RETENTION_DAYS'):
        raw = os.environ.get(name, '').strip()
        if raw and (not raw.isdigit() or int(raw) < 1):
            raise ValueError(f'{name} must be a positive integer')
    raw = os.environ.get('QUERYAPIGATE_ALERT_ERROR_RATE', '').strip()
    if raw and (not raw.isdigit() or int(raw) > 100):
        raise ValueError('QUERYAPIGATE_ALERT_ERROR_RATE must be a percentage from 0 to 100 (0 turns the check off)')
    raw = os.environ.get('QUERYAPIGATE_ALERT_KEY_UNUSED_DAYS', '').strip()
    if raw and not raw.isdigit():
        raise ValueError('QUERYAPIGATE_ALERT_KEY_UNUSED_DAYS must be a whole number of days (0 turns the check off)')
    for name in ('QUERYAPIGATE_EVENTS_PORT', 'QUERYAPIGATE_EVENTS_MAX_CONNECTIONS'):
        raw = os.environ.get(name, '').strip()
        if raw and (not raw.isdigit() or int(raw) < 1):
            raise ValueError(f'{name} must be a positive integer')
    raw = os.environ.get('QUERYAPIGATE_EVENTS_MAX_STREAMS', '').strip()
    if raw and not raw.isdigit():
        raise ValueError('QUERYAPIGATE_EVENTS_MAX_STREAMS must be a whole number (0 turns GET /events off on this '
                         'server - use `queryapigate events` instead)')
    raw = os.environ.get('QUERYAPIGATE_EVENTS_POLL_INTERVAL', '').strip()
    if raw and not _number_in(raw, lambda v: 0.05 <= v <= 60):
        raise ValueError('QUERYAPIGATE_EVENTS_POLL_INTERVAL must be a number of seconds from 0.05 to 60')
    raw = os.environ.get('QUERYAPIGATE_HISTORY_SAMPLE_RATE', '').strip()
    if raw and not _number_in(raw, lambda v: 0 < v <= 1):
        raise ValueError('QUERYAPIGATE_HISTORY_SAMPLE_RATE must be a number above 0 and at most 1, e.g. 0.1 '
                         'to record one successful run in ten (failed runs are always recorded)')
    raw = os.environ.get('QUERYAPIGATE_HISTORY_FLUSH_INTERVAL', '').strip()
    if raw and not _number_in(raw, lambda v: 0 <= v <= 60):
        raise ValueError('QUERYAPIGATE_HISTORY_FLUSH_INTERVAL must be a number of seconds from 0 to 60 '
                         '(0 writes each run inside its own request)')
    raw = os.environ.get('QUERYAPIGATE_MCP_PORT', '').strip()
    if raw and (not raw.isdigit() or int(raw) < 1):
        raise ValueError('QUERYAPIGATE_MCP_PORT must be a positive integer')
    raw = os.environ.get('QUERYAPIGATE_MCP_MAX_ROWS', '').strip()
    if raw and (not raw.isdigit() or int(raw) < 1):
        raise ValueError('QUERYAPIGATE_MCP_MAX_ROWS must be a positive integer')
    raw = os.environ.get('QUERYAPIGATE_SECRET_KEY', '').strip()
    if raw:
        try:
            from cryptography.fernet import Fernet
        except ImportError:
            raise ValueError('QUERYAPIGATE_SECRET_KEY is set but the "cryptography" package is not installed - '
                             'run `pip install "queryapigate[encryption]"`') from None
        try:
            Fernet(raw.encode('utf-8'))
        except (ValueError, TypeError):
            raise ValueError('QUERYAPIGATE_SECRET_KEY must be a valid Fernet key - 32 url-safe base64-encoded '
                             'bytes, e.g. from `python -c "from cryptography.fernet import Fernet; '
                             'print(Fernet.generate_key().decode())"`') from None
    check_database_url()
    check_jwt_settings()
    raw = os.environ.get('QUERYAPIGATE_REDIS_URL', '').strip()
    if raw:
        if not _REDIS_SCHEME_RE.match(raw):
            raise ValueError("QUERYAPIGATE_REDIS_URL must start with redis://, rediss:// or unix://, not "
                             f"'{raw.split('://')[0]}://'")
        try:
            import redis  # noqa: F401
        except ImportError:
            raise ValueError('QUERYAPIGATE_REDIS_URL is set but the "redis" package is not installed - '
                             'run `pip install "queryapigate[redis]"`') from None


def secret_key():
    """The server-side key used to encrypt connection passwords at rest (QUERYAPIGATE_SECRET_KEY), or None when
    unset - encryption at rest is opt-in; without it, a connection's password is stored exactly as given,
    today's unchanged behaviour. Validated as a real Fernet key at startup by check_settings(), not here."""
    return os.environ.get('QUERYAPIGATE_SECRET_KEY') or None


_REDIS_SCHEME_RE = re.compile(r'^(rediss?|unix)://', re.I)
_POSTGRES_SCHEME_RE = re.compile(r'^postgres(ql)?://', re.I)


def _number_in(raw, accept):
    try:
        value = float(raw)
    except ValueError:
        return False
    return math.isfinite(value) and accept(value)


def check_database_url():
    """Part of check_settings(), callable on its own: the CLI opens the store (db.init_schema()) before any
    command runs, so a malformed QUERYAPIGATE_DATABASE_URL must be reported there, not as a driver traceback."""
    raw = os.environ.get('QUERYAPIGATE_DATABASE_URL', '').strip()
    if not raw:
        return
    if not _POSTGRES_SCHEME_RE.match(raw):
        raise ValueError("QUERYAPIGATE_DATABASE_URL must start with postgres:// or postgresql://, not "
                         f"'{raw.split('://')[0]}://' - leave it unset to keep queryapigate.db (SQLite)")
    try:
        import psycopg2  # noqa: F401
    except ImportError:
        raise ValueError('QUERYAPIGATE_DATABASE_URL is set but the "psycopg2" package is not installed - '
                         'run `pip install "queryapigate[postgres]"`') from None


def database_url():
    """QUERYAPIGATE_DATABASE_URL: keep connections, saved queries, API keys, roles, run history and the audit
    log in this PostgreSQL database instead of queryapigate.db in the home folder - so several instances
    behind a load balancer can share them (see db.py). None (the default) keeps SQLite. Validated as a
    postgres:// URL, and that psycopg2 is installed, at startup by check_settings()."""
    return os.environ.get('QUERYAPIGATE_DATABASE_URL', '').strip() or None


def redis_url():
    """QUERYAPIGATE_REDIS_URL: swaps the response cache (cache.py's ResponseCache) for a Redis-backed one
    (rediscache.RedisResponseCache) that survives a process restart and can be shared across horizontally-
    scaled instances - see cache.py's own docstring for why the in-process default can't do that. None
    (the default) keeps today's in-process cache, unchanged, with no new import. Validated as a real
    redis:// URL, and that the redis package is actually installed, at startup by check_settings()."""
    return os.environ.get('QUERYAPIGATE_REDIS_URL', '').strip() or None


def redact_url(url):
    """`url` with any password hidden, for safe logging and the Settings panel - same spirit as
    store.mask_passwords() for a connection's own password."""
    from urllib.parse import urlsplit, urlunsplit
    parts = urlsplit(url)
    if not parts.password:
        return url
    netloc = parts.netloc.replace(parts.password, PASSWORD_MASK, 1)
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


redact_redis_url = redact_url


def max_page_size():
    try:
        return max(1, int(os.environ.get('QUERYAPIGATE_MAX_PAGE_SIZE', 1000)))
    except ValueError:
        return 1000


def stream_max_rows():
    """Row cap for a streaming (?stream=true) export (QUERYAPIGATE_STREAM_MAX_ROWS), or None when unbounded -
    today's original behaviour, unchanged unless explicitly opted into. Validated at startup
    (check_settings()) rather than silently falling back like max_page_size() does: this is a safety cap an
    admin is deliberately turning on, so a typo should fail loudly, not silently leave it unbounded."""
    raw = os.environ.get('QUERYAPIGATE_STREAM_MAX_ROWS', '').strip()
    return int(raw) if raw else None


def json_logs():
    """Emit structured (one JSON object per line) logs instead of plain text (QUERYAPIGATE_JSON_LOGS)."""
    return env_flag('QUERYAPIGATE_JSON_LOGS')


JWT_ASYMMETRIC_ALGORITHMS = ('RS256', 'RS384', 'RS512', 'PS256', 'PS384', 'PS512', 'ES256', 'ES384', 'ES512',
                             'EdDSA')
JWT_SYMMETRIC_ALGORITHMS = ('HS256', 'HS384', 'HS512')
_JWT_VARS = ('QUERYAPIGATE_JWT_SECRET', 'QUERYAPIGATE_JWT_JWKS_URL', 'QUERYAPIGATE_JWT_ISSUER',
             'QUERYAPIGATE_JWT_AUDIENCE', 'QUERYAPIGATE_JWT_ALGORITHMS', 'QUERYAPIGATE_JWT_ROLE',
             'QUERYAPIGATE_JWT_ROLE_CLAIM', 'QUERYAPIGATE_JWT_USER_CLAIM')


def _env(name):
    return os.environ.get(name, '').strip() or None


def jwt_secret():
    """QUERYAPIGATE_JWT_SECRET: verify `Authorization: Bearer` tokens signed with this shared secret (HS256 by
    default) - for tokens your own backend issues. See jwtauth.py."""
    return _env('QUERYAPIGATE_JWT_SECRET')


def jwt_jwks_url():
    """QUERYAPIGATE_JWT_JWKS_URL: verify tokens against your identity provider's published signing keys (RS256 by
    default) - Auth0, Cognito, Firebase, Keycloak, Azure AD and the like."""
    return _env('QUERYAPIGATE_JWT_JWKS_URL')


def jwt_enabled():
    return jwt_secret() is not None or jwt_jwks_url() is not None


def jwt_issuer():
    return _env('QUERYAPIGATE_JWT_ISSUER')


def jwt_audience():
    """Accepted `aud` values (comma-separated in QUERYAPIGATE_JWT_AUDIENCE), or None."""
    raw = _env('QUERYAPIGATE_JWT_AUDIENCE')
    return [part.strip() for part in raw.split(',') if part.strip()] if raw else None


def jwt_algorithms():
    raw = _env('QUERYAPIGATE_JWT_ALGORITHMS')
    if raw:
        return [part.strip() for part in raw.split(',') if part.strip()]
    return ['RS256'] if jwt_jwks_url() else ['HS256']


def jwt_role():
    """QUERYAPIGATE_JWT_ROLE: the role whose grants every token caller gets (unless QUERYAPIGATE_JWT_ROLE_CLAIM picks
    another for them)."""
    return _env('QUERYAPIGATE_JWT_ROLE')


def jwt_role_claim():
    """QUERYAPIGATE_JWT_ROLE_CLAIM: a claim naming the role to use for that caller (a string, or a list - the first
    that names an existing role wins). Falls back to jwt_role() when absent."""
    return _env('QUERYAPIGATE_JWT_ROLE_CLAIM')


def jwt_user_claim():
    """QUERYAPIGATE_JWT_USER_CLAIM: the claim identifying the user - default `sub`."""
    return _env('QUERYAPIGATE_JWT_USER_CLAIM') or 'sub'


def check_jwt_settings():
    """Part of check_settings(). Fails startup on anything that would make token checking unsafe or impossible -
    a fail-open misconfiguration is far worse here than a server that refuses to start."""
    if not any(_env(name) for name in _JWT_VARS):
        return
    secret, jwks = jwt_secret(), jwt_jwks_url()
    if secret is None and jwks is None:
        raise ValueError('QUERYAPIGATE_JWT_* settings are set but neither QUERYAPIGATE_JWT_SECRET nor '
                         'QUERYAPIGATE_JWT_JWKS_URL is - tokens could not be verified')
    if secret is not None and jwks is not None:
        raise ValueError('Set QUERYAPIGATE_JWT_SECRET or QUERYAPIGATE_JWT_JWKS_URL, not both')
    try:
        import jwt  # noqa: F401
    except ImportError:
        raise ValueError('JWT authentication is configured but the "PyJWT" package is not installed - '
                         'run `pip install "queryapigate[jwt]"`') from None
    algorithms = jwt_algorithms()
    allowed = JWT_SYMMETRIC_ALGORITHMS if secret is not None else JWT_ASYMMETRIC_ALGORITHMS
    wrong = [a for a in algorithms if a not in allowed]
    if wrong or not algorithms:
        # Mixing the two families is the classic "algorithm confusion" hole: a public key accepted as an HMAC secret.
        raise ValueError(f"QUERYAPIGATE_JWT_ALGORITHMS may only list {', '.join(allowed)} with "
                         f"{'QUERYAPIGATE_JWT_SECRET' if secret else 'QUERYAPIGATE_JWT_JWKS_URL'}, not "
                         f"{', '.join(wrong) or 'nothing'}")
    if secret is not None and len(secret.encode('utf-8')) < 32:
        raise ValueError('QUERYAPIGATE_JWT_SECRET must be at least 32 bytes - a short HMAC secret can be brute-forced '
                         'from any one token')
    if jwks is not None:
        if not jwks.lower().startswith('https://') and not jwks.lower().startswith('http://localhost') \
                and not jwks.lower().startswith('http://127.0.0.1'):
            raise ValueError('QUERYAPIGATE_JWT_JWKS_URL must be an https:// URL - signing keys fetched over plain HTTP '
                             'could be swapped in transit')
        if jwt_issuer() is None or jwt_audience() is None:
            # An identity provider signs tokens for every application it serves: without these, a token issued for
            # some other app, or by another tenant of a shared provider, would be accepted here too.
            raise ValueError('QUERYAPIGATE_JWT_JWKS_URL needs QUERYAPIGATE_JWT_ISSUER and QUERYAPIGATE_JWT_AUDIENCE '
                             "set too, so only tokens issued for this API are accepted")
    if jwt_role() is None and jwt_role_claim() is None:
        raise ValueError('JWT authentication needs QUERYAPIGATE_JWT_ROLE (the role every token caller gets) and/or '
                         'QUERYAPIGATE_JWT_ROLE_CLAIM (a claim naming one)')


def events_port():
    """Bind port for `queryapigate events` (QUERYAPIGATE_EVENTS_PORT), default 5002 - see events.py."""
    raw = os.environ.get('QUERYAPIGATE_EVENTS_PORT', '').strip()
    return int(raw) if raw else DEFAULT_EVENTS_PORT


def events_max_connections():
    """Open streams one `queryapigate events` process accepts before answering 503
    (QUERYAPIGATE_EVENTS_MAX_CONNECTIONS) - each is a socket, so this also bounds file descriptors."""
    raw = os.environ.get('QUERYAPIGATE_EVENTS_MAX_CONNECTIONS', '').strip()
    return int(raw) if raw else DEFAULT_EVENTS_MAX_CONNECTIONS


def events_poll_interval():
    """Seconds between `queryapigate events` checks for newly recorded runs (QUERYAPIGATE_EVENTS_POLL_INTERVAL).
    On PostgreSQL a NOTIFY wakes it as soon as a batch commits, so this is only the fallback there."""
    raw = os.environ.get('QUERYAPIGATE_EVENTS_POLL_INTERVAL', '').strip()
    return float(raw) if raw else DEFAULT_EVENTS_POLL_INTERVAL


def events_max_streams():
    """Concurrent GET /events streams the main (WSGI) server holds open (QUERYAPIGATE_EVENTS_MAX_STREAMS). Each
    one occupies a request thread for as long as it is open, so the default leaves most of the Docker image's 8
    threads for requests; beyond it /events answers 503. `queryapigate events` has no such limit to speak of."""
    raw = os.environ.get('QUERYAPIGATE_EVENTS_MAX_STREAMS', '').strip()
    return int(raw) if raw else DEFAULT_EVENTS_MAX_STREAMS


def mcp_port():
    """Bind port for `queryapigate mcp` (QUERYAPIGATE_MCP_PORT), default DEFAULT_MCP_PORT (5001) - distinct
    from QUERYAPIGATE_PORT so the MCP server and the REST server can run side by side against the same
    QUERYAPIGATE_HOME without a port clash."""
    raw = os.environ.get('QUERYAPIGATE_MCP_PORT', '').strip()
    return int(raw) if raw else DEFAULT_MCP_PORT


def mcp_max_rows():
    """Row cap for an MCP tools/call result (QUERYAPIGATE_MCP_MAX_ROWS), default DEFAULT_MCP_MAX_ROWS (200) -
    independent of QUERYAPIGATE_MAX_PAGE_SIZE, the REST API's own page-size ceiling: an LLM's context window
    can't hold a large result the way a human paging through the admin UI can, so this needs its own,
    smaller-by-default cap regardless of what a REST caller is allowed to request. Validated at startup
    (check_settings()), the same "fail loudly on a typo" treatment stream_max_rows() gets."""
    raw = os.environ.get('QUERYAPIGATE_MCP_MAX_ROWS', '').strip()
    return int(raw) if raw else DEFAULT_MCP_MAX_ROWS


def alert_error_rate():
    """QUERYAPIGATE_ALERT_ERROR_RATE: the percentage of a saved query's recent runs that may fail before
    GET /api/v1/alerts calls it out (alerts.py). Default 20; 0 turns the check off. Validated at startup."""
    raw = os.environ.get('QUERYAPIGATE_ALERT_ERROR_RATE', '').strip()
    return int(raw) if raw else DEFAULT_ALERT_ERROR_RATE


def alert_key_unused_days():
    """QUERYAPIGATE_ALERT_KEY_UNUSED_DAYS: days an active API key may go unused before it is an alert - an unused
    credential is risk without benefit. Default 90; 0 turns the check off. Validated at startup."""
    raw = os.environ.get('QUERYAPIGATE_ALERT_KEY_UNUSED_DAYS', '').strip()
    return int(raw) if raw else DEFAULT_ALERT_KEY_UNUSED_DAYS


def slow_query_threshold():
    """Seconds a query may take before it is logged as a warning (QUERYAPIGATE_SLOW_QUERY_THRESHOLD, default 1);
    None when 0 disables it."""
    try:
        value = float(os.environ.get('QUERYAPIGATE_SLOW_QUERY_THRESHOLD', DEFAULT_SLOW_QUERY_THRESHOLD))
    except ValueError:
        return DEFAULT_SLOW_QUERY_THRESHOLD
    if value == 0:
        return None
    return value if value > 0 else DEFAULT_SLOW_QUERY_THRESHOLD


def _seconds(value):
    return None if value is None else f'{value:g} s'


def describe_settings():
    """Every server setting the admin UI's Settings screen shows, grouped: [{id, title, description, rows}].

    Each row is {label, description, env, value, source, env_value}. ``value`` is what to display (secrets are
    reduced to "configured" / "enabled" - never the secret itself), ``source`` is "env" when the variable is set
    and "default" when the built-in default applies, and ``env_value`` is the raw value for a "copy as .env"
    export, or None for a secret so that export can never leak one. Read-only: settings are environment
    variables, changed by restarting the server, not through the API."""
    def row(label, description, env, value, secret=False, experimental_row=False):
        raw = os.environ.get(env, '').strip()
        return {'label': label, 'description': description, 'env': env, 'value': value,
                'source': 'env' if raw else 'default', 'env_value': None if secret or not raw else raw,
                'experimental': experimental_row or env in experimental.SETTINGS}

    def on_off(flag):
        return 'on' if flag else 'off'

    stream = stream_max_rows()
    timeout, slow = query_timeout(), slow_query_threshold()
    limit, export = rate_limit(), audit_log_export_file()
    cors = cors_origins()
    redis_val = redis_url()
    return [
        {'id': 'general', 'title': 'General',
         'description': 'Where this server keeps its files and what it loads at startup.', 'rows': [
            row('Home directory', 'Folder holding queryapigate.db (connections, saved queries, API keys, '
                'roles and the audit log), unless a metadata database is set below.', 'QUERYAPIGATE_HOME',
                str(home())),
            row('Metadata database', 'Where connections, saved queries, API keys, roles, run history and the '
                'audit log are kept: queryapigate.db in the home directory (default), or a shared PostgreSQL '
                'database so several instances can run side by side.', 'QUERYAPIGATE_DATABASE_URL',
                'SQLite (queryapigate.db)' if database_url() is None
                else f'PostgreSQL ({redact_url(database_url())})', secret=True),
            row('Load examples', 'Load the example APIs at startup. Idempotent.',
                'QUERYAPIGATE_LOAD_EXAMPLES', on_off(load_examples())),
            row('H2 driver', 'JAR used for h2 connections.', 'QUERYAPIGATE_H2_JAR',
                Path(h2_jar()).name + ('' if os.environ.get('QUERYAPIGATE_H2_JAR') else ' (bundled)'))]},
        {'id': 'security', 'title': 'Security',
         'description': 'Admin access, write protection and secrets at rest.', 'rows': [
            row('Admin API key', 'Key with full access to this UI and the admin API. Unset means open access.',
                'QUERYAPIGATE_API_KEY', 'configured' if api_key() else 'not set - open access', secret=True),
            row('Allow writes', 'When off, statements that modify data are rejected.',
                'QUERYAPIGATE_ALLOW_WRITES', on_off(allow_writes())),
            row('Encryption at rest', 'Fernet key used to encrypt connection passwords on disk.',
                'QUERYAPIGATE_SECRET_KEY', 'enabled' if secret_key() else 'off', secret=True),
            row('Trusted proxy hops', 'Reverse proxies whose X-Forwarded-* headers are trusted.',
                'QUERYAPIGATE_TRUST_PROXY', str(proxy_hops()))]},
        {'id': 'jwt', 'title': 'Signed-in users (JWT)',
         'description': 'Accept `Authorization: Bearer` tokens from your own login or identity provider, so each '
             'app user calls the API as themselves - no API key per user. Off unless a secret or JWKS URL is set.',
         'rows': [
            row('Verification', 'A shared secret (tokens your backend signs) or your identity provider\'s JWKS URL.',
                'QUERYAPIGATE_JWT_JWKS_URL' if jwt_jwks_url() else 'QUERYAPIGATE_JWT_SECRET',
                'off' if not jwt_enabled() else ('shared secret' if jwt_secret() else f'JWKS ({jwt_jwks_url()})'),
                secret=jwt_secret() is not None or not jwt_enabled()),
            row('Issuer', 'Required `iss` claim.', 'QUERYAPIGATE_JWT_ISSUER', jwt_issuer() or 'not checked'),
            row('Audience', 'Accepted `aud` values.', 'QUERYAPIGATE_JWT_AUDIENCE',
                ', '.join(jwt_audience()) if jwt_audience() else 'not checked'),
            row('Algorithms', 'Signature algorithms accepted.', 'QUERYAPIGATE_JWT_ALGORITHMS',
                ', '.join(jwt_algorithms()) if jwt_enabled() else '-'),
            row('Role', 'Role whose grants every token caller gets.', 'QUERYAPIGATE_JWT_ROLE', jwt_role() or 'not set'),
            row('Role claim', 'Claim naming a caller\'s role instead.', 'QUERYAPIGATE_JWT_ROLE_CLAIM',
                jwt_role_claim() or 'not set'),
            row('User claim', 'Claim identifying the user.', 'QUERYAPIGATE_JWT_USER_CLAIM', jwt_user_claim())]},
        {'id': 'execution', 'title': 'Query execution',
         'description': 'Limits applied to every statement this server runs.', 'rows': [
            row('Query timeout', 'Server-wide statement limit. A request may ask for less, never more. '
                '0 disables it.', 'QUERYAPIGATE_QUERY_TIMEOUT', _seconds(timeout) or 'off'),
            row('Max page size', 'Largest page_size a caller may request.', 'QUERYAPIGATE_MAX_PAGE_SIZE',
                f'{max_page_size()} rows'),
            row('Stream row cap', 'Row cap for ?stream=true exports.', 'QUERYAPIGATE_STREAM_MAX_ROWS',
                'unbounded' if stream is None else f'{stream} rows'),
            row('Slow query threshold', 'Queries slower than this are logged as warnings. 0 disables it.',
                'QUERYAPIGATE_SLOW_QUERY_THRESHOLD', _seconds(slow) or 'off')]},
        {'id': 'pool', 'title': 'Connection pool',
         'description': 'Idle connections kept open per distinct connection.', 'rows': [
            row('Pool size', 'Idle connections kept per connection. 0 disables pooling.',
                'QUERYAPIGATE_POOL_SIZE', str(pool_size())),
            row('Idle timeout', 'How long an idle pooled connection is kept before it is closed.',
                'QUERYAPIGATE_POOL_IDLE_TIMEOUT', _seconds(pool_idle_timeout()))]},
        {'id': 'traffic', 'title': 'Rate limits & CORS',
         'description': 'Per-client throttling and browser origins allowed to call the API.', 'rows': [
            row('Rate limit', 'Requests allowed per client. API keys and roles can set their own.',
                'QUERYAPIGATE_RATE_LIMIT', format_rate_limit(limit) or 'off'),
            row('Counted', 'Where rate limits are counted: in Redis, shared by every instance '
                '(QUERYAPIGATE_REDIS_URL), or in this process alone.', '',
                'in Redis - shared' if redis_url() else 'in this process'),  # no variable of its own
            row('CORS origins', 'Comma-separated origins, or * for any. Unset turns CORS off.',
                'QUERYAPIGATE_CORS_ORIGINS',
                'off' if cors is None else '*' if cors == '*' else ', '.join(sorted(cors)))]},
        {'id': 'audit', 'title': 'Audit & logging',
         'description': 'Retention of administrative actions and log format.', 'rows': [
            row('Audit log limit', 'Entries kept in queryapigate.db.', 'QUERYAPIGATE_AUDIT_LOG_LIMIT',
                str(audit_log_limit())),
            row('Audit export file', 'Append-only JSON-lines copy of every audit entry, never rolled off.',
                'QUERYAPIGATE_AUDIT_LOG_EXPORT_FILE', 'not set' if export is None else str(export)),
            row('JSON logs', 'One JSON object per line instead of plain text.', 'QUERYAPIGATE_JSON_LOGS',
                on_off(json_logs()))]},
        {'id': 'history', 'title': 'Run history',
         'description': 'What each run - of a saved query, or ad-hoc SQL - leaves behind in its history, and for how '
             'long. GET /api/v1/history (and a query\'s own history) pages through everything kept.', 'rows': [
            row('History limit', 'Runs kept per saved-query version, unless a retention period is set.',
                'QUERYAPIGATE_HISTORY_LIMIT', f'{history_limit()} runs'),
            row('Ad-hoc history limit', 'Ad-hoc SQL runs kept in all, unless a retention period is set.',
                'QUERYAPIGATE_HISTORY_ADHOC_LIMIT', f'{history_adhoc_limit()} runs'),
            row('Ad-hoc SQL kept', 'What an ad-hoc run\'s history keeps of its SQL: the text (first '
                f'{HISTORY_ADHOC_SQL_MAX} characters), a hash, or none. Parameter values are never kept.',
                'QUERYAPIGATE_HISTORY_ADHOC_SQL', history_adhoc_sql()),
            row('History retention', 'Keep every run for this many days instead of a per-version count. Best '
                'with a PostgreSQL metadata store.', 'QUERYAPIGATE_HISTORY_RETENTION_DAYS',
                'per-version limit' if history_retention_days() is None else f'{history_retention_days()} days'),
            row('Sample rate', 'Fraction of successful runs recorded; failed runs are always recorded.',
                'QUERYAPIGATE_HISTORY_SAMPLE_RATE', f'{history_sample_rate():g}'),
            row('Flush interval', 'Seconds between batched history writes; 0 writes inside each request.',
                'QUERYAPIGATE_HISTORY_FLUSH_INTERVAL', _seconds(history_flush_interval()))]},
        {'id': 'cache', 'title': 'Response cache & live updates',
         'description': 'Where cache_ttl-carrying saved queries store their cached responses, and how the '
             'admin UI\'s Home tab is notified of new query runs.', 'rows': [
            row('Cache backend', 'In-process (default) or a shared Redis, surviving restarts and shared '
                'across instances.', 'QUERYAPIGATE_REDIS_URL',
                'in-process' if redis_val is None else f'Redis ({redact_redis_url(redis_val)})',
                secret=True),
            row('Live updates', 'GET /events on this server: live runs for the admin UI and light use, from '
                'this process only.', '', 'in-process', experimental_row=True),  # live events, no variable
            row('Streams on this server', 'Concurrent GET /events streams this server holds open - each takes '
                'a request thread. Beyond it, /events answers 503. 0 turns it off.',
                'QUERYAPIGATE_EVENTS_MAX_STREAMS', str(events_max_streams())),
            row('Events server port', 'Port of `queryapigate events`: a separate process for many clients '
                '(apps, phones), every instance\'s runs, and resuming with Last-Event-ID.',
                'QUERYAPIGATE_EVENTS_PORT', str(events_port())),
            row('Events server connections', 'Open streams one `queryapigate events` process accepts.',
                'QUERYAPIGATE_EVENTS_MAX_CONNECTIONS', str(events_max_connections())),
            row('Events poll interval', 'Seconds between checks for new runs (on PostgreSQL a notification '
                'usually arrives first).', 'QUERYAPIGATE_EVENTS_POLL_INTERVAL', _seconds(events_poll_interval()))]},
        {'id': 'alerts', 'title': 'Alerts',
         'description': 'When GET /api/v1/alerts (the Console\'s Alerts screen) calls something out. A query is '
             'slow when its typical run is over the slow-query threshold (Query execution).', 'rows': [
            row('Query error rate', 'Share of a saved query\'s recent runs that may fail before it is an alert. '
                '0 turns the check off.', 'QUERYAPIGATE_ALERT_ERROR_RATE',
                f'{alert_error_rate()}%' if alert_error_rate() else 'off'),
            row('Unused key', 'Days an active API key may go unused before it is an alert. 0 turns the check off.',
                'QUERYAPIGATE_ALERT_KEY_UNUSED_DAYS',
                f'{alert_key_unused_days()} days' if alert_key_unused_days() else 'off')]},
        {'id': 'mcp', 'title': 'MCP server',
         'description': 'Settings for `queryapigate mcp` (BACKLOG #42) - a separate process this one doesn\'t '
             'start. "Check now" tries its port on this host.', 'rows': [
            row('MCP port', 'Bind port for `queryapigate mcp`.', 'QUERYAPIGATE_MCP_PORT', str(mcp_port())),
            row('MCP max rows', 'Row cap for a tools/call result - an LLM\'s context can\'t hold a huge one.',
                'QUERYAPIGATE_MCP_MAX_ROWS', f'{mcp_max_rows()} rows')]},
    ]

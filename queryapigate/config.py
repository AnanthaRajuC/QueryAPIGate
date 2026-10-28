"""Runtime configuration, read from environment variables at call time."""
import functools
import logging
import math
import os
import re
import urllib.parse
from pathlib import Path

from . import experimental

SUPPORTED_DB_TYPES = ('mysql', 'postgres', 'clickhouse', 'sqlite', 'h2', 'jdbc', 'duckdb', 'mongo', 'trino')
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
DEFAULT_EVENTS_MAX_STREAMS = 4  # concurrent GET /events streams the main server itself will hold open, at most
DEFAULT_EVENTS_RETENTION_HOURS = 24  # how long the event log keeps an event (ADR 0020)
AUDIT_LOG_LIMIT = 500  # administrative-action entries remembered across the whole server
DEFAULT_QUERY_TIMEOUT = 30.0  # seconds
DEFAULT_THREADS = 8  # request threads in the Docker image's one gunicorn worker (QUERYAPIGATE_THREADS)
MAX_THREADS = 256
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
        'example-trino': {'db': 'trino', 'host': 'localhost', 'port': 8080, 'database': 'tpch',
                          'user': 'queryapigate', 'options': {'schema': 'tiny', 'http_scheme': 'http'},
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


def access_log_query():
    """QUERYAPIGATE_ACCESS_LOG_QUERY: the access log keeps each request's query string - its parameter values. Off by
    default (ADR 0016): the path alone, as QueryAPIGate's own log, run history and traces keep it."""
    return env_flag('QUERYAPIGATE_ACCESS_LOG_QUERY')


def config_file():
    """QUERYAPIGATE_CONFIG_FILE: a configuration file (ADR 0014) applied at startup, before the server serves - once per
    file: an instance that finds it already applied leaves the store alone. None when unset."""
    raw = os.environ.get('QUERYAPIGATE_CONFIG_FILE', '').strip()
    return Path(raw) if raw else None


def config_prune():
    """QUERYAPIGATE_CONFIG_PRUNE: applying QUERYAPIGATE_CONFIG_FILE at startup also deletes what a file manages and this
    one no longer has (`config apply --prune`)."""
    return env_flag('QUERYAPIGATE_CONFIG_PRUNE')


def config_locked():
    """QUERYAPIGATE_CONFIG_LOCKED: what a configuration file manages can be changed only through a file - the API and
    the Console answer 409 config_managed (ADR 0014)."""
    return env_flag('QUERYAPIGATE_CONFIG_LOCKED')


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
    keys, roles and the audit log (db.py, store.py, apikeys.py). Created on first start. A home from 0.9 or older,
    with only the pre-SQLite JSON files, is refused - see store.refuse_legacy_home()."""
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
    default) keeps the per-version cap - except with a run store (runstore.py), where retention is always by time:
    RUN_STORE_HISTORY_DAYS unless this is set. Validated at startup by check_settings()."""
    raw = os.environ.get('QUERYAPIGATE_HISTORY_RETENTION_DAYS', '').strip()
    if raw:
        return int(raw)
    return RUN_STORE_HISTORY_DAYS if run_store_url() else None


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


def threads():
    """Request threads per server process (QUERYAPIGATE_THREADS, default 8): what the Docker image's gunicorn runs
    (gunicorn_conf.py), and what the pool size and the cap on /events streams default from. A value check_settings()
    refuses falls back to the default here, so gunicorn can read it before the app says what's wrong."""
    raw = os.environ.get('QUERYAPIGATE_THREADS', '').strip()
    return int(raw) if raw.isdigit() and 1 <= int(raw) <= MAX_THREADS else DEFAULT_THREADS


def access_log():
    """QUERYAPIGATE_ACCESS_LOG: the image's gunicorn writes an access line per request to stdout (on by default). Off
    for a server whose proxy or load balancer already logs requests - what a line costs, with no one reading it."""
    return os.environ.get('QUERYAPIGATE_ACCESS_LOG', '').strip().lower() not in ('0', 'false', 'no', 'off')


def pool_size():
    """Idle connections kept per distinct connection (QUERYAPIGATE_POOL_SIZE); 0 disables pooling. Unset, one per
    request thread (threads()): fewer would make threads beyond it open a connection per call, more would sit idle."""
    try:
        return max(0, int(os.environ.get('QUERYAPIGATE_POOL_SIZE', threads())))
    except ValueError:
        return threads()


def metadata_cache_ttl():
    """Seconds an API key, saved query or connection read from the store is reused (QUERYAPIGATE_METADATA_CACHE_TTL,
    metacache.py). 0, the default, reads them on every request. A change made through this process is seen at once;
    one made through another process (another instance, the CLI) up to this long later."""
    try:
        return max(0.0, float(os.environ.get('QUERYAPIGATE_METADATA_CACHE_TTL', 0)))
    except ValueError:
        return 0.0


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


MAX_HSTS_SECONDS = 2 * 365 * 24 * 3600


def hsts_max_age():
    """QUERYAPIGATE_HSTS_MAX_AGE: seconds browsers should insist on HTTPS for this host (Strict-Transport-Security,
    ADR 0010), or None - the default, so a server without HTTPS never locks browsers out. Sent only on requests that
    arrived over HTTPS (secheaders.py). Validated at startup by check_settings()."""
    raw = os.environ.get('QUERYAPIGATE_HSTS_MAX_AGE', '').strip()
    return int(raw) if raw.isdigit() and int(raw) > 0 else None


_SERVICE_LINK_RE = re.compile(r'^tcp://[^:/]+:\d+$')
PORT_SETTINGS = ('QUERYAPIGATE_PORT', 'QUERYAPIGATE_EVENTS_PORT', 'QUERYAPIGATE_MCP_PORT')


_ignored_links: set = set()


def port_setting(name, default):
    """A port from the environment. A value like tcp://10.0.0.7:80 is not one anyone sets: Kubernetes injects it into
    every pod when a Service is named like this setting (a Service `queryapigate-events` sets
    QUERYAPIGATE_EVENTS_PORT) - so it is ignored, with a warning saying why, and the default applies."""
    raw = os.environ.get(name, '').strip()
    if not raw:
        return default
    if _SERVICE_LINK_RE.match(raw):
        if name not in _ignored_links:
            _ignored_links.add(name)
            service = name[:-len('_PORT')].lower().replace('_', '-')
            logging.getLogger('queryapigate').warning(
                "%s is '%s', which Kubernetes sets for a Service named '%s' (service links) - ignored. Set "
                'enableServiceLinks: false in the pod spec to stop it (deploy/kubernetes/queryapigate.yaml does).',
                name, raw, service)
        return default
    if not raw.isdigit() or int(raw) < 1:
        raise ValueError(f'{name} must be a positive integer')
    return int(raw)


def check_settings():
    """Raise ValueError for a malformed setting, so a typo fails at startup instead of silently switching off a
    protection."""
    load_secret_files()
    raw = os.environ.get('QUERYAPIGATE_SECRET_CACHE_SECONDS', '').strip()
    if raw and (not raw.isdigit() or not 1 <= int(raw) <= 86400):
        raise ValueError('QUERYAPIGATE_SECRET_CACHE_SECONDS must be a whole number of seconds from 1 to 86400')
    _check_vault()
    for folder in os.environ.get('QUERYAPIGATE_SECRET_FILE_DIRS', '').split(','):
        if folder.strip() and not os.path.isabs(folder.strip()):
            raise ValueError(f"QUERYAPIGATE_SECRET_FILE_DIRS: '{folder.strip()}' must be an absolute path")
    if dataplane_secret() is not None and len(dataplane_secret()) < DATAPLANE_SECRET_MIN:
        raise ValueError(f'QUERYAPIGATE_DATAPLANE_SECRET must be at least {DATAPLANE_SECRET_MIN} characters - the data '
                         'plane refuses a shorter one too')
    if metrics_token() is not None and len(metrics_token()) < METRICS_TOKEN_MIN:
        raise ValueError(f'QUERYAPIGATE_METRICS_TOKEN must be at least {METRICS_TOKEN_MIN} characters - '
                         '`openssl rand -hex 32` makes one')
    for name in secret_env_vars():
        if not SECRET_ENV_VAR_RE.fullmatch(name):
            raise ValueError(f"QUERYAPIGATE_SECRET_ENV_VARS: '{name}' must be a variable's name, or a prefix "
                             'ending in *')
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
    for name in ('QUERYAPIGATE_CONFIG_PRUNE', 'QUERYAPIGATE_CONFIG_LOCKED', 'QUERYAPIGATE_ACCESS_LOG_QUERY'):
        raw = os.environ.get(name, '').strip().lower()
        if raw not in _FLAG_WORDS:
            raise ValueError(f"{name} must be yes or no (or 1/0, true/false, on/off), not '{raw}'")
    if config_prune() and config_file() is None:
        raise ValueError('QUERYAPIGATE_CONFIG_PRUNE is set but QUERYAPIGATE_CONFIG_FILE is not - prune what, against '
                         'which file?')
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
    raw = os.environ.get('QUERYAPIGATE_WRITE_APPROVAL_MINUTES', '').strip()
    if raw and (not raw.isdigit() or not 1 <= int(raw) <= 1440):
        raise ValueError('QUERYAPIGATE_WRITE_APPROVAL_MINUTES must be a whole number of minutes from 1 to 1440')
    raw = _env('QUERYAPIGATE_CONSOLE_URL')
    if raw and (not _secure_url(raw) or urllib.parse.urlsplit(raw).path not in ('', '/')):
        raise ValueError('QUERYAPIGATE_CONSOLE_URL is the https:// address the Console is reached at, with no path - '
                         'https://data.example.com')
    raw = _env('QUERYAPIGATE_EMBEDDING_URL')
    if raw and not _secure_url(raw):
        raise ValueError('QUERYAPIGATE_EMBEDDING_URL must be an https:// (or localhost) embeddings endpoint, e.g. '
                         'https://api.openai.com/v1/embeddings or http://localhost:11434/v1/embeddings')
    if raw and not embedding_model():
        raise ValueError('QUERYAPIGATE_EMBEDDING_URL is set but QUERYAPIGATE_EMBEDDING_MODEL is not - which model '
                         'should embed the text?')
    raw = os.environ.get('QUERYAPIGATE_ALERT_AGENT_ROWS', '').strip()
    if raw and not raw.isdigit():
        raise ValueError('QUERYAPIGATE_ALERT_AGENT_ROWS must be a whole number of rows (0 turns the check off)')
    for entry in os.environ.get('QUERYAPIGATE_EXPORT_DIRS', '').split(','):
        if entry.strip() and not os.path.isabs(entry.strip()):
            raise ValueError(f"QUERYAPIGATE_EXPORT_DIRS must be absolute folder paths; '{entry.strip()}' is relative")
    for name in PORT_SETTINGS:
        port_setting(name, None)
    raw = os.environ.get('QUERYAPIGATE_EVENTS_MAX_CONNECTIONS', '').strip()
    if raw and (not raw.isdigit() or int(raw) < 1):
        raise ValueError('QUERYAPIGATE_EVENTS_MAX_CONNECTIONS must be a positive integer')
    raw = os.environ.get('QUERYAPIGATE_THREADS', '').strip()
    if raw and (not raw.isdigit() or not 1 <= int(raw) <= MAX_THREADS):
        raise ValueError(f'QUERYAPIGATE_THREADS must be a whole number from 1 to {MAX_THREADS}')
    raw = os.environ.get('QUERYAPIGATE_ACCESS_LOG', '').strip().lower()
    if raw not in _FLAG_WORDS:
        raise ValueError(f"QUERYAPIGATE_ACCESS_LOG must be on or off (or 1/0, true/false, yes/no), not '{raw}'")
    raw = os.environ.get('QUERYAPIGATE_EVENTS_MAX_STREAMS', '').strip()
    if raw and not raw.isdigit():
        raise ValueError('QUERYAPIGATE_EVENTS_MAX_STREAMS must be a whole number (0 turns GET /events off on this '
                         'server - use `queryapigate events` instead)')
    raw = os.environ.get('QUERYAPIGATE_EVENTS_POLL_INTERVAL', '').strip()
    if raw and not _number_in(raw, lambda v: 0.05 <= v <= 60):
        raise ValueError('QUERYAPIGATE_EVENTS_POLL_INTERVAL must be a number of seconds from 0.05 to 60')
    raw = os.environ.get('QUERYAPIGATE_EVENTS_RETENTION_HOURS', '').strip()
    if raw and (not raw.isdigit() or int(raw) < 1):
        raise ValueError('QUERYAPIGATE_EVENTS_RETENTION_HOURS must be a whole number of hours, at least 1')
    raw = os.environ.get('QUERYAPIGATE_HISTORY_SAMPLE_RATE', '').strip()
    if raw and not _number_in(raw, lambda v: 0 < v <= 1):
        raise ValueError('QUERYAPIGATE_HISTORY_SAMPLE_RATE must be a number above 0 and at most 1, e.g. 0.1 '
                         'to record one successful run in ten (failed runs are always recorded)')
    raw = os.environ.get('QUERYAPIGATE_HISTORY_FLUSH_INTERVAL', '').strip()
    if raw and not _number_in(raw, lambda v: 0 <= v <= 60):
        raise ValueError('QUERYAPIGATE_HISTORY_FLUSH_INTERVAL must be a number of seconds from 0 to 60 '
                         '(0 writes each run inside its own request)')
    raw = os.environ.get('QUERYAPIGATE_MCP_MAX_ROWS', '').strip()
    if raw and (not raw.isdigit() or int(raw) < 1):
        raise ValueError('QUERYAPIGATE_MCP_MAX_ROWS must be a positive integer')
    check_secret_keys()
    from . import auditchain, auditstream, tracing
    tracing.check()
    auditchain.check_keys()
    auditstream.check()
    raw = os.environ.get('QUERYAPIGATE_HSTS_MAX_AGE', '').strip()
    if raw and not (raw.isdigit() and 0 < int(raw) <= MAX_HSTS_SECONDS):
        raise ValueError('QUERYAPIGATE_HSTS_MAX_AGE must be a whole number of seconds from 1 to '
                         f'{MAX_HSTS_SECONDS} (two years) - 31536000 is a year')
    check_database_url()
    check_run_store_url()
    check_jwt_settings()
    check_mcp_oauth_settings()
    check_sso_settings()
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


def export_dirs():
    """QUERYAPIGATE_EXPORT_DIRS: the local folders a destination may write under - comma-separated absolute paths,
    set by whoever runs the server. Unset, no local folder may be a destination (object storage only): otherwise an
    administrator could write files anywhere this process can."""
    raw = os.environ.get('QUERYAPIGATE_EXPORT_DIRS', '')
    return [os.path.realpath(p.strip()) for p in raw.split(',') if p.strip()]


def secret_file_dirs():
    """QUERYAPIGATE_SECRET_FILE_DIRS: the folders `${file:...}` references may read (ADR 0015) - comma-separated
    absolute paths, set by whoever runs the server. Unset, no file may be read as a secret: otherwise an
    administrator could have the server send them any file it can read, as a connection's password."""
    raw = os.environ.get('QUERYAPIGATE_SECRET_FILE_DIRS', '')
    return [os.path.realpath(p.strip()) for p in raw.split(',') if p.strip()]


SECRET_ENV_VAR_RE = re.compile(r'[A-Za-z_][A-Za-z0-9_]*\*?')


def secret_env_vars():
    """QUERYAPIGATE_SECRET_ENV_VARS: the environment variables `${VAR}` references may read (ADR 0017) - names, or
    prefixes ending in `*` (`WAREHOUSE_*`), comma-separated. Unset, any may be read but those secretrefs.py always
    refuses: the server's own settings, and the cloud, Vault and Kubernetes credentials it may run with. Validated by
    check_settings()."""
    raw = os.environ.get('QUERYAPIGATE_SECRET_ENV_VARS', '')
    return [p.strip() for p in raw.split(',') if p.strip()]


def secret_cache_seconds():
    """QUERYAPIGATE_SECRET_CACHE_SECONDS: how long a secret resolved from a provider is kept in memory at most (default
    300) - fetched again a little before. Validated by check_settings()."""
    raw = os.environ.get('QUERYAPIGATE_SECRET_CACHE_SECONDS', '').strip()
    return int(raw) if raw.isdigit() and 1 <= int(raw) <= 86400 else 300


# ---- Vault (ADR 0015): what secretrefs.py and vault.py read ----

VAULT_AUTH_METHODS = ('token', 'kubernetes', 'approle')


def _setting(name, default=''):
    return os.environ.get(name, '').strip() or default


def vault_addr():
    """QUERYAPIGATE_VAULT_ADDR: Vault's address, e.g. https://vault.example.com:8200 - unset, ${vault:...} and
    vault-database are refused."""
    return _setting('QUERYAPIGATE_VAULT_ADDR') or None


def vault_auth():
    """QUERYAPIGATE_VAULT_AUTH: how the server signs in to Vault - token (default), kubernetes or approle."""
    return _setting('QUERYAPIGATE_VAULT_AUTH', 'token').lower()


def vault_auth_mount():
    """QUERYAPIGATE_VAULT_AUTH_MOUNT: where the auth method is mounted - by default its own name."""
    return _setting('QUERYAPIGATE_VAULT_AUTH_MOUNT', vault_auth()).strip('/')


def vault_token():
    return _setting('QUERYAPIGATE_VAULT_TOKEN') or None


def vault_token_file():
    """QUERYAPIGATE_VAULT_TOKEN_FILE: a token read again at each sign-in - the file Vault Agent keeps current."""
    return _setting('QUERYAPIGATE_VAULT_TOKEN_FILE') or None


def vault_k8s_role():
    return _setting('QUERYAPIGATE_VAULT_K8S_ROLE') or None


def vault_k8s_token_file():
    return _setting('QUERYAPIGATE_VAULT_K8S_TOKEN_FILE', '/var/run/secrets/kubernetes.io/serviceaccount/token')


def vault_role_id():
    return _setting('QUERYAPIGATE_VAULT_ROLE_ID') or None


def vault_secret_id():
    return _setting('QUERYAPIGATE_VAULT_SECRET_ID') or None


def vault_namespace():
    """QUERYAPIGATE_VAULT_NAMESPACE: a Vault Enterprise namespace, or None."""
    return _setting('QUERYAPIGATE_VAULT_NAMESPACE') or None


def vault_cacert():
    """QUERYAPIGATE_VAULT_CACERT: the CA Vault's certificate is checked against, when it isn't a public one."""
    return _setting('QUERYAPIGATE_VAULT_CACERT') or None


def vault_paths():
    """QUERYAPIGATE_VAULT_PATHS: the path prefixes ${vault:...} may read, comma-separated (secret/data/reporting/) -
    unset, none: the server's token may reach far more than any connection should."""
    return [p.strip().strip('/') + '/' for p in os.environ.get('QUERYAPIGATE_VAULT_PATHS', '').split(',') if p.strip()]


def vault_database_roles():
    """QUERYAPIGATE_VAULT_DATABASE_ROLES: the database secrets engine roles a connection may ask for, comma-separated -
    each optionally bound to the host its credentials may go to: reporting-ro@warehouse.internal[:5432]. Unset,
    none."""
    return [r.strip() for r in os.environ.get('QUERYAPIGATE_VAULT_DATABASE_ROLES', '').split(',') if r.strip()]


def vault_database_mount():
    """QUERYAPIGATE_VAULT_DATABASE_MOUNT: where the database secrets engine is mounted (default: database)."""
    return _setting('QUERYAPIGATE_VAULT_DATABASE_MOUNT', 'database').strip('/')


def aws_region():
    """QUERYAPIGATE_AWS_REGION: the region for Secrets Manager and RDS IAM tokens - else boto3's own (AWS_REGION...)."""
    return _setting('QUERYAPIGATE_AWS_REGION') or None


def aws_secret_prefixes():
    """QUERYAPIGATE_AWS_SECRET_PREFIXES: the secret names ${aws-sm:...} may read begin with one of these
    (prod/reporting/) - unset, none: the server's IAM role may reach far more than any connection should."""
    return [p.strip() for p in os.environ.get('QUERYAPIGATE_AWS_SECRET_PREFIXES', '').split(',') if p.strip()]


def aws_rds_hosts():
    """QUERYAPIGATE_AWS_RDS_HOSTS: the database hosts aws-rds-iam credentials may be made for - a name, or a suffix
    starting with a dot (.abc123.eu-west-1.rds.amazonaws.com). Unset, none."""
    return [h.strip().lower() for h in os.environ.get('QUERYAPIGATE_AWS_RDS_HOSTS', '').split(',') if h.strip()]


def _check_vault():
    if not vault_addr():
        return
    if not re.match(r'^https?://[^/]+', vault_addr()):
        raise ValueError('QUERYAPIGATE_VAULT_ADDR must be an http:// or https:// address')
    method = vault_auth()
    if method not in VAULT_AUTH_METHODS:
        raise ValueError(f"QUERYAPIGATE_VAULT_AUTH must be one of: {', '.join(VAULT_AUTH_METHODS)} - not '{method}'")
    if method == 'kubernetes' and not vault_k8s_role():
        raise ValueError('QUERYAPIGATE_VAULT_AUTH is kubernetes: set QUERYAPIGATE_VAULT_K8S_ROLE to the Vault role')
    if method == 'approle' and not (vault_role_id() and vault_secret_id()):
        raise ValueError('QUERYAPIGATE_VAULT_AUTH is approle: set QUERYAPIGATE_VAULT_ROLE_ID and '
                         'QUERYAPIGATE_VAULT_SECRET_ID')
    if method == 'token' and not (vault_token() or vault_token_file()):
        raise ValueError('QUERYAPIGATE_VAULT_AUTH is token: set QUERYAPIGATE_VAULT_TOKEN or '
                         'QUERYAPIGATE_VAULT_TOKEN_FILE')
    if vault_cacert() and not os.path.isfile(vault_cacert()):
        raise ValueError(f'QUERYAPIGATE_VAULT_CACERT: {vault_cacert()} is not a file')


# The settings that are secrets: each can also be read from a file, NAME_FILE (ADR 0015) - Docker's and most images'
# convention - read at startup, as the environment variable would be.
SECRET_SETTINGS = ('QUERYAPIGATE_API_KEY', 'QUERYAPIGATE_SECRET_KEY', 'QUERYAPIGATE_DATABASE_URL',
                   'QUERYAPIGATE_REDIS_URL', 'QUERYAPIGATE_AUDIT_KEY', 'QUERYAPIGATE_MASK_KEY',
                   'QUERYAPIGATE_JWT_SECRET', 'QUERYAPIGATE_SSO_CLIENT_SECRET', 'QUERYAPIGATE_VAULT_SECRET_ID',
                   'QUERYAPIGATE_METRICS_TOKEN', 'QUERYAPIGATE_RUN_STORE_URL')
# QUERYAPIGATE_VAULT_TOKEN_FILE isn't one of them: vault.py reads it again at each sign-in, as Vault Agent rotates it.
_from_files: dict[str, str] = {}  # setting -> the file it was read from


def load_secret_files():
    """Read each NAME_FILE that's set into NAME, once: every reader of the setting then works as if it were set
    directly. Both set is refused - which one would win isn't obvious, so neither does."""
    for name in SECRET_SETTINGS:
        path = os.environ.get(f'{name}_FILE', '').strip()
        if not path or _from_files.get(name) == path:
            continue
        if os.environ.get(name, '').strip():
            raise ValueError(f'{name} and {name}_FILE are both set - set one')
        try:
            with open(path, encoding='utf-8') as f:
                value = f.read()
        except (OSError, UnicodeDecodeError) as error:
            raise ValueError(f'{name}_FILE: {path} could not be read: {error}') from None
        value = value[:-2] if value.endswith('\r\n') else value[:-1] if value.endswith('\n') else value
        if not value.strip():
            raise ValueError(f'{name}_FILE: {path} is empty')
        os.environ[name] = value
        _from_files[name] = path


def secret_source(name):
    """Where a secret setting came from, for Settings: its file, or None for the environment."""
    return _from_files.get(name)


METRICS_TOKEN_MIN = 16
DATAPLANE_SECRET_MIN = 16  # dataplane/src/config.rs's MIN_SECRET: it is all that lets a request speak for a client


def metrics_token():
    """QUERYAPIGATE_METRICS_TOKEN: the bearer token /metrics needs, or None - then /metrics is open, as it was before
    0.30 (metrics.authorized()). Validated by check_settings()."""
    return os.environ.get('QUERYAPIGATE_METRICS_TOKEN', '').strip() or None


def mask_key():
    """QUERYAPIGATE_MASK_KEY: the key column masking's `hash` action is keyed with (masking.py), or None - then the
    store makes one once and every instance shares it."""
    return os.environ.get('QUERYAPIGATE_MASK_KEY', '').strip() or None


def dataplane_secret():
    """QUERYAPIGATE_DATAPLANE_SECRET: what a Rust data plane in front of this server sends to have its forwarded `/q/`
    calls answered with a plan (dataplane.py, ADR 0028), or None - then no request gets one. A prototype setting:
    shared by the two processes only, never by a client."""
    return os.environ.get('QUERYAPIGATE_DATAPLANE_SECRET', '').strip() or None


def secret_keys():
    """The keys connection and destination passwords are encrypted with at rest (QUERYAPIGATE_SECRET_KEY), first
    to last - empty when unset: encryption at rest is opt-in, and without it a password is stored exactly as given.
    Several, comma-separated, while rotating (ADR 0009): the first encrypts, every one decrypts, and
    `queryapigate rekey` moves what's stored under the others to the first. Validated by check_secret_keys()."""
    return [k.strip() for k in os.environ.get('QUERYAPIGATE_SECRET_KEY', '').split(',') if k.strip()]


def secret_key():
    """The key new values are encrypted with - the first of secret_keys() - or None when encryption is off."""
    keys = secret_keys()
    return keys[0] if keys else None


def _audit_key_state():
    from . import auditchain
    count = len(auditchain.keys())
    if not count:
        return 'not set - plain SHA-256'
    return 'keyed (HMAC-SHA256)' + (f', {count} keys' if count > 1 else '')


def _encryption_state():
    keys = secret_keys()
    if len(keys) < 2:
        return 'enabled' if keys else 'off'
    from . import rekey
    old = rekey.report()['under_old_keys']
    return (f'{len(keys)} keys - {old} password(s) still under an old key: run `queryapigate rekey`' if old
            else f'{len(keys)} keys - every password under the first: remove the others')


def check_secret_keys():
    """Each key in QUERYAPIGATE_SECRET_KEY a real Fernet key, none twice - saying which one is wrong."""
    keys = secret_keys()
    if not keys:
        return
    try:
        from cryptography.fernet import Fernet
    except ImportError:
        raise ValueError('QUERYAPIGATE_SECRET_KEY is set but the "cryptography" package is not installed - '
                         'run `pip install "queryapigate[encryption]"`') from None
    for i, key in enumerate(keys):
        which = ('QUERYAPIGATE_SECRET_KEY' if len(keys) == 1
                 else f'Key {i + 1} of {len(keys)} in QUERYAPIGATE_SECRET_KEY')
        try:
            Fernet(key.encode('utf-8'))
        except (ValueError, TypeError):
            raise ValueError(f'{which} must be a valid Fernet key - 32 url-safe base64-encoded bytes, e.g. from '
                             '`python -c "from cryptography.fernet import Fernet; '
                             'print(Fernet.generate_key().decode())"`') from None
        if key in keys[:i]:
            raise ValueError(f'{which} is the same as key {keys.index(key) + 1} - list each key once, the one to '
                             'encrypt with first')


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
    load_secret_files()  # QUERYAPIGATE_DATABASE_URL_FILE: the store's URL is needed before anything else
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


RUN_STORE_HISTORY_DAYS = 30  # history's retention in the run store unless QUERYAPIGATE_HISTORY_RETENTION_DAYS says
_RUN_STORE_DATABASE_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]{0,62}$')


def run_store_url():
    """QUERYAPIGATE_RUN_STORE_URL: keep run history, the event log and usage rollups in ClickHouse, at this
    http(s)://user:password@host:port/database URL, instead of in the metadata store (runstore.py, ADR 0029). None (the
    default) keeps them there. Validated at startup by check_run_store_url()."""
    return os.environ.get('QUERYAPIGATE_RUN_STORE_URL', '').strip() or None


def check_run_store_url():
    """Part of check_settings(): the run store's URL is http(s)://, names a host and a database, and is https:// unless
    the host is this machine - it carries a password, and every run's record."""
    from urllib.parse import urlsplit
    raw = run_store_url()
    if raw is None:
        return
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError as error:
        raise ValueError(f'QUERYAPIGATE_RUN_STORE_URL is not a URL: {error}') from None
    if parts.scheme not in ('http', 'https') or not parts.hostname:
        raise ValueError("QUERYAPIGATE_RUN_STORE_URL must be http(s)://user:password@host:port/database - ClickHouse's "
                         'HTTP interface')
    if parts.scheme == 'http' and parts.hostname not in ('localhost', '127.0.0.1', '::1'):
        raise ValueError('QUERYAPIGATE_RUN_STORE_URL must be https:// unless ClickHouse is on this machine: it carries '
                         'a password and every run')
    database = parts.path.strip('/') or 'queryapigate'
    if not _RUN_STORE_DATABASE_RE.match(database):
        raise ValueError(f"QUERYAPIGATE_RUN_STORE_URL: '{database}' is not a database name ClickHouse takes unquoted - "
                         'letters, digits and _')
    if port is not None and not 0 < port < 65536:
        raise ValueError('QUERYAPIGATE_RUN_STORE_URL: the port must be from 1 to 65535')


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


def log_requests():
    """QUERYAPIGATE_LOG_REQUESTS: log a line for every request, and one for each query it runs (on by default). Off
    leaves those INFO lines out - errors, warnings, slow queries and agent writes are still logged - for a server whose
    proxy already logs requests: the lines cost CPU, and eight threads queue on the one handler that writes them."""
    return os.environ.get('QUERYAPIGATE_LOG_REQUESTS', '').strip().lower() not in ('0', 'false', 'no', 'off')


def json_logs():
    """Emit structured (one JSON object per line) logs instead of plain text (QUERYAPIGATE_JSON_LOGS)."""
    return env_flag('QUERYAPIGATE_JSON_LOGS')


JWT_ASYMMETRIC_ALGORITHMS = ('RS256', 'RS384', 'RS512', 'PS256', 'PS384', 'PS512', 'ES256', 'ES384', 'ES512',
                             'EdDSA')
JWT_SYMMETRIC_ALGORITHMS = ('HS256', 'HS384', 'HS512')
_JWT_VARS = ('QUERYAPIGATE_JWT_SECRET', 'QUERYAPIGATE_JWT_JWKS_URL', 'QUERYAPIGATE_JWT_ISSUER',
             'QUERYAPIGATE_JWT_AUDIENCE', 'QUERYAPIGATE_JWT_ALGORITHMS', 'QUERYAPIGATE_JWT_ROLE',
             'QUERYAPIGATE_JWT_ROLE_CLAIM', 'QUERYAPIGATE_JWT_USER_CLAIM', 'QUERYAPIGATE_JWT_WORKSPACE_CLAIM')


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


def jwt_workspace_claim():
    """QUERYAPIGATE_JWT_WORKSPACE_CLAIM: a claim naming the workspace a signed-in user belongs to (ADR 0023). The
    token, not the request path, decides a federated user's workspace, so a user signed in for one team can't reach
    another's by changing the `/w/<workspace>/` prefix. Unset, every signed-in user acts in the default workspace."""
    return _env('QUERYAPIGATE_JWT_WORKSPACE_CLAIM')


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


_MCP_OAUTH_VARS = ('QUERYAPIGATE_MCP_RESOURCE_URL', 'QUERYAPIGATE_MCP_AUTH_SERVER', 'QUERYAPIGATE_MCP_SCOPE',
                   'QUERYAPIGATE_MCP_ROLE', 'QUERYAPIGATE_MCP_ROLE_CLAIM')
DEFAULT_MCP_SCOPE = 'queryapigate.mcp'


def mcp_resource_url():
    """QUERYAPIGATE_MCP_RESOURCE_URL: the MCP server's canonical URI (`https://data.example.com/mcp`) - the audience
    every OAuth access token on /mcp must name (ADR 0006, mcp_server.py)."""
    return _env('QUERYAPIGATE_MCP_RESOURCE_URL')


def mcp_auth_server():
    """QUERYAPIGATE_MCP_AUTH_SERVER: the issuer URL of the identity provider that issues tokens for the MCP server;
    its signing keys are found through its published metadata."""
    return _env('QUERYAPIGATE_MCP_AUTH_SERVER')


def mcp_oauth_enabled():
    return mcp_resource_url() is not None and mcp_auth_server() is not None


def mcp_scope():
    """QUERYAPIGATE_MCP_SCOPE: the scope a token must carry to use the MCP server - default `queryapigate.mcp`."""
    return _env('QUERYAPIGATE_MCP_SCOPE') or DEFAULT_MCP_SCOPE


def mcp_role():
    """QUERYAPIGATE_MCP_ROLE: the role whose grants every OAuth caller on /mcp gets, unless the role claim picks
    another."""
    return _env('QUERYAPIGATE_MCP_ROLE')


def mcp_role_claim():
    """QUERYAPIGATE_MCP_ROLE_CLAIM: a claim naming an OAuth caller's role on /mcp (a string, or a list - the first
    that names an existing role wins)."""
    return _env('QUERYAPIGATE_MCP_ROLE_CLAIM')


def _secure_url(url):
    lowered = url.lower()
    return lowered.startswith('https://') or lowered.startswith(('http://localhost', 'http://127.0.0.1'))


def check_mcp_oauth_settings():
    """Part of check_settings(): the MCP server's OAuth settings are all there and safe, or not set at all."""
    if not any(_env(name) for name in _MCP_OAUTH_VARS):
        return
    resource, issuer = mcp_resource_url(), mcp_auth_server()
    if resource is None or issuer is None:
        raise ValueError('OAuth for the MCP server needs both QUERYAPIGATE_MCP_RESOURCE_URL (its own URL) and '
                         'QUERYAPIGATE_MCP_AUTH_SERVER (the identity provider that issues its tokens)')
    for name, url in (('QUERYAPIGATE_MCP_RESOURCE_URL', resource), ('QUERYAPIGATE_MCP_AUTH_SERVER', issuer)):
        parts = urllib.parse.urlsplit(url)
        if not _secure_url(url) or not parts.netloc:
            raise ValueError(f'{name} must be an https:// URL - tokens and signing keys over plain HTTP could be '
                             'read or swapped in transit')
        if parts.query or parts.fragment:
            raise ValueError(f'{name} must not have a query string or a fragment')
    try:
        import jwt  # noqa: F401
    except ImportError:
        raise ValueError('OAuth for the MCP server needs the "PyJWT" package - run `pip install '
                         '"queryapigate[jwt]"`') from None
    if mcp_role() is None and mcp_role_claim() is None:
        raise ValueError('OAuth for the MCP server needs QUERYAPIGATE_MCP_ROLE (the role every agent gets) and/or '
                         'QUERYAPIGATE_MCP_ROLE_CLAIM (a claim naming one)')
    if resource in (jwt_audience() or []):
        # The point of a separate audience: a token for the MCP server must never be accepted by REST, or the reverse.
        raise ValueError('QUERYAPIGATE_JWT_AUDIENCE must not include QUERYAPIGATE_MCP_RESOURCE_URL - tokens for the '
                         'MCP server and for the REST API are kept apart')


_SSO_VARS = ('QUERYAPIGATE_SSO_ISSUER', 'QUERYAPIGATE_SSO_CLIENT_ID', 'QUERYAPIGATE_SSO_CLIENT_SECRET',
             'QUERYAPIGATE_SSO_REDIRECT_URL', 'QUERYAPIGATE_SSO_ROLES', 'QUERYAPIGATE_SSO_GROUPS_CLAIM',
             'QUERYAPIGATE_SSO_SESSION_HOURS', 'QUERYAPIGATE_SSO_ONLY', 'QUERYAPIGATE_SSO_DEFAULT_ROLE',
             'QUERYAPIGATE_SSO_ALLOWED_DOMAINS')
# Most capable first: a person in several mapped groups gets the first of these they qualify for. The same names as
# adminroles.ROLES (tests/test_sso_settings.py says so) - kept here so config needn't import the role table.
SSO_ROLE_ORDER = ('owner', 'admin', 'developer', 'auditor')
SSO_CALLBACK_PATH = '/auth/sso/callback'
DEFAULT_SSO_SESSION_HOURS = 8
MAX_SSO_SESSION_HOURS = 24 * 7


def sso_issuer():
    """QUERYAPIGATE_SSO_ISSUER: the identity provider administrators sign in to the Console through (ADR 0007)."""
    return _env('QUERYAPIGATE_SSO_ISSUER')


def sso_client_id():
    return _env('QUERYAPIGATE_SSO_CLIENT_ID')


def sso_client_secret():
    return _env('QUERYAPIGATE_SSO_CLIENT_SECRET')


def sso_redirect_url():
    """QUERYAPIGATE_SSO_REDIRECT_URL: the sign-in callback as the provider calls it -
    https://data.example.com/auth/sso/callback."""
    return _env('QUERYAPIGATE_SSO_REDIRECT_URL')


def sso_enabled():
    return all((sso_issuer(), sso_client_id(), sso_client_secret(), sso_redirect_url()))


def sso_roles():
    """QUERYAPIGATE_SSO_ROLES as [(role, group)]: `owner=qag-owners,admin=qag-admins` - a role may be named more than
    once, for several groups."""
    raw = _env('QUERYAPIGATE_SSO_ROLES')
    out = []
    for part in (raw or '').split(','):
        if not part.strip():
            continue
        role, sep, group = part.partition('=')
        if not sep or role.strip() not in SSO_ROLE_ORDER or not group.strip():
            raise ValueError(f"QUERYAPIGATE_SSO_ROLES entries are role=group, the role one of "
                             f"{', '.join(SSO_ROLE_ORDER)} - not '{part.strip()}'")
        out.append((role.strip(), group.strip()))
    return out


def sso_groups_claim():
    return _env('QUERYAPIGATE_SSO_GROUPS_CLAIM') or 'groups'


def sso_session_hours():
    raw = _env('QUERYAPIGATE_SSO_SESSION_HOURS')
    return int(raw) if raw else DEFAULT_SSO_SESSION_HOURS


def sso_only():
    """QUERYAPIGATE_SSO_ONLY: administrators who sign in through SSO have no personal tokens (admins.py)."""
    return env_flag('QUERYAPIGATE_SSO_ONLY')


def sso_default_role():
    """QUERYAPIGATE_SSO_DEFAULT_ROLE: the role for someone in no mapped group - for providers whose ID tokens carry
    no groups. Only with QUERYAPIGATE_SSO_ALLOWED_DOMAINS."""
    return _env('QUERYAPIGATE_SSO_DEFAULT_ROLE')


def sso_allowed_domains():
    raw = _env('QUERYAPIGATE_SSO_ALLOWED_DOMAINS')
    return [d.strip().lower().lstrip('@') for d in (raw or '').split(',') if d.strip()]


def check_sso_settings():
    """Part of check_settings(): Console sign-in through SSO is set up completely and safely, or not at all."""
    if not any(_env(name) for name in _SSO_VARS):
        return
    missing = [name for name in _SSO_VARS[:4] if not _env(name)]
    if missing:
        raise ValueError(f"Console sign-in through SSO needs {', '.join(_SSO_VARS[:4])} - missing: "
                         f"{', '.join(missing)}")
    for name, url in (('QUERYAPIGATE_SSO_ISSUER', sso_issuer()), ('QUERYAPIGATE_SSO_REDIRECT_URL', sso_redirect_url())):
        parts = urllib.parse.urlsplit(url)
        if not _secure_url(url) or not parts.netloc or parts.query or parts.fragment:
            raise ValueError(f'{name} must be an https:// URL with no query string or fragment')
    if not urllib.parse.urlsplit(sso_redirect_url()).path.endswith(SSO_CALLBACK_PATH):
        raise ValueError(f'QUERYAPIGATE_SSO_REDIRECT_URL must end with {SSO_CALLBACK_PATH} - the address the provider '
                         'sends people back to')
    try:
        import jwt  # noqa: F401
    except ImportError:
        raise ValueError('Console sign-in through SSO needs the "PyJWT" package - run `pip install '
                         '"queryapigate[jwt]"`') from None
    roles, default = sso_roles(), sso_default_role()
    if not roles and not default:
        raise ValueError('Console sign-in through SSO needs QUERYAPIGATE_SSO_ROLES (groups to roles) - or '
                         'QUERYAPIGATE_SSO_DEFAULT_ROLE with QUERYAPIGATE_SSO_ALLOWED_DOMAINS')
    if default is not None:
        if default not in SSO_ROLE_ORDER or default == 'owner':
            raise ValueError('QUERYAPIGATE_SSO_DEFAULT_ROLE must be admin, developer or auditor - owners come from a '
                             'group, never by default')
        if not sso_allowed_domains():
            # A provider such as Google signs in anyone with an account: a role for everyone means everyone on earth.
            raise ValueError('QUERYAPIGATE_SSO_DEFAULT_ROLE needs QUERYAPIGATE_SSO_ALLOWED_DOMAINS, so only people '
                             'with a verified email at your domains get it')
    raw = _env('QUERYAPIGATE_SSO_SESSION_HOURS')
    if raw and (not raw.isdigit() or not 1 <= int(raw) <= MAX_SSO_SESSION_HOURS):
        raise ValueError(f'QUERYAPIGATE_SSO_SESSION_HOURS must be a whole number of hours from 1 to '
                         f'{MAX_SSO_SESSION_HOURS}')
    raw = os.environ.get('QUERYAPIGATE_SSO_ONLY', '').strip().lower()
    if raw not in _FLAG_WORDS:
        raise ValueError(f"QUERYAPIGATE_SSO_ONLY must be yes or no, not '{raw}'")


def events_port():
    """Bind port for `queryapigate events` (QUERYAPIGATE_EVENTS_PORT), default 5002 - see events.py."""
    return port_setting('QUERYAPIGATE_EVENTS_PORT', DEFAULT_EVENTS_PORT)


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


def events_retention_hours():
    """Hours the event log keeps an event (QUERYAPIGATE_EVENTS_RETENTION_HOURS), default 24 - how far back a client of
    `queryapigate events` can resume with Last-Event-ID (ADR 0020)."""
    raw = os.environ.get('QUERYAPIGATE_EVENTS_RETENTION_HOURS', '').strip()
    return int(raw) if raw else DEFAULT_EVENTS_RETENTION_HOURS


def events_max_streams():
    """Concurrent GET /events streams the main (WSGI) server holds open (QUERYAPIGATE_EVENTS_MAX_STREAMS). Each
    one occupies a request thread for as long as it is open, so the default - 4, and never more than half the
    threads (threads()) - leaves most of them for requests; beyond it /events answers 503. `queryapigate events` has no
    such limit to speak of."""
    raw = os.environ.get('QUERYAPIGATE_EVENTS_MAX_STREAMS', '').strip()
    return int(raw) if raw else min(DEFAULT_EVENTS_MAX_STREAMS, threads() // 2)


def mcp_port():
    """Bind port for `queryapigate mcp` (QUERYAPIGATE_MCP_PORT), default DEFAULT_MCP_PORT (5001) - distinct
    from QUERYAPIGATE_PORT so the MCP server and the REST server can run side by side against the same
    QUERYAPIGATE_HOME without a port clash."""
    return port_setting('QUERYAPIGATE_MCP_PORT', DEFAULT_MCP_PORT)


def mcp_max_rows():
    """Row cap for an MCP tools/call result (QUERYAPIGATE_MCP_MAX_ROWS), default DEFAULT_MCP_MAX_ROWS (200) -
    independent of QUERYAPIGATE_MAX_PAGE_SIZE, the REST API's own page-size ceiling: an LLM's context window
    can't hold a large result the way a human paging through the admin UI can, so this needs its own,
    smaller-by-default cap regardless of what a REST caller is allowed to request. Validated at startup
    (check_settings()), the same "fail loudly on a typo" treatment stream_max_rows() gets."""
    raw = os.environ.get('QUERYAPIGATE_MCP_MAX_ROWS', '').strip()
    return int(raw) if raw else DEFAULT_MCP_MAX_ROWS


_EMBEDDING_VARS = ('QUERYAPIGATE_EMBEDDING_URL', 'QUERYAPIGATE_EMBEDDING_MODEL', 'QUERYAPIGATE_EMBEDDING_API_KEY')


def embedding_url():
    """QUERYAPIGATE_EMBEDDING_URL: an OpenAI-compatible embeddings endpoint (OpenAI's own, or a local Ollama's
    http://localhost:11434/v1/embeddings). With a model set, an `embed` query parameter turns a caller's text
    into a vector for a pgvector search. Experimental - see embeddings.py."""
    return _env('QUERYAPIGATE_EMBEDDING_URL')


def embedding_model():
    """QUERYAPIGATE_EMBEDDING_MODEL: the model name sent to the embeddings endpoint (e.g. text-embedding-3-small,
    or nomic-embed-text on Ollama)."""
    return _env('QUERYAPIGATE_EMBEDDING_MODEL')


def embedding_api_key():
    """QUERYAPIGATE_EMBEDDING_API_KEY: bearer token for the embeddings endpoint, if it needs one (omit for a
    local Ollama)."""
    return _env('QUERYAPIGATE_EMBEDDING_API_KEY')


def embeddings_configured():
    """Whether semantic search is available: both an endpoint and a model are set."""
    return bool(embedding_url() and embedding_model())


def alert_error_rate():
    """QUERYAPIGATE_ALERT_ERROR_RATE: the percentage of a saved query's recent runs that may fail before
    GET /api/v1/alerts calls it out (alerts.py). Default 20; 0 turns the check off. Validated at startup."""
    raw = os.environ.get('QUERYAPIGATE_ALERT_ERROR_RATE', '').strip()
    return int(raw) if raw else DEFAULT_ALERT_ERROR_RATE


DEFAULT_WRITE_APPROVAL_MINUTES = 15


def write_approval_minutes():
    """QUERYAPIGATE_WRITE_APPROVAL_MINUTES: how long an agent's write waits for a person to approve it - and, once
    approved, for the agent to run it (ADR 0008). Default 15."""
    raw = os.environ.get('QUERYAPIGATE_WRITE_APPROVAL_MINUTES', '').strip()
    return int(raw) if raw else DEFAULT_WRITE_APPROVAL_MINUTES


def console_url():
    """QUERYAPIGATE_CONSOLE_URL: where people reach the Console - https://data.example.com - for the links this server
    hands out (an agent's write to approve). Unset, the origin of QUERYAPIGATE_SSO_REDIRECT_URL, or none."""
    raw = _env('QUERYAPIGATE_CONSOLE_URL')
    if raw:
        return raw.rstrip('/')
    redirect = sso_redirect_url()
    if redirect:
        parts = urllib.parse.urlsplit(redirect)
        return f'{parts.scheme}://{parts.netloc}'
    return None


DEFAULT_ALERT_AGENT_ROWS = 10_000


def alert_agent_rows():
    """QUERYAPIGATE_ALERT_AGENT_ROWS: rows an AI agent may read over MCP in an hour before a sudden rise is an alert
    (agent_unusual_volume) - below it, nothing fires however unusual. Default 10000; 0 turns the check off."""
    raw = os.environ.get('QUERYAPIGATE_ALERT_AGENT_ROWS', '').strip()
    return int(raw) if raw else DEFAULT_ALERT_AGENT_ROWS


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


def _provider_status():
    from datetime import datetime

    from . import secretrefs
    status = dict(secretrefs.cache.status)
    if not status:
        return 'none used yet'
    parts = []
    for name, st in sorted(status.items()):
        if st.get('failing_since'):
            parts.append(f"{name}: failing since {datetime.fromtimestamp(st['failing_since']):%H:%M:%S} - "
                         f"{st.get('last_error')}")
        else:
            parts.append(f"{name}: ok at {datetime.fromtimestamp(st['last_ok']):%H:%M:%S}")
    return '; '.join(parts)


def describe_settings():
    """Every server setting the admin UI's Settings screen shows, grouped: [{id, title, description, rows}].

    Each row is {label, description, env, value, source, env_value}. ``value`` is what to display (secrets are
    reduced to "configured" / "enabled" - never the secret itself), ``source`` is "env" when the variable is set
    and "default" when the built-in default applies, and ``env_value`` is the raw value for a "copy as .env"
    export, or None for a secret so that export can never leak one. Read-only: settings are environment
    variables, changed by restarting the server, not through the API."""
    def row(label, description, env, value, secret=False):
        raw = os.environ.get(env, '').strip()
        if secret and secret_source(env):
            value = f'{value} (from {env}_FILE)'  # never the file's content: where it came from
        return {'label': label, 'description': description, 'env': env, 'value': value,
                'source': 'env' if raw else 'default', 'env_value': None if secret or not raw else raw,
                'experimental': env in experimental.SETTINGS}

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
            row('Run store', 'Where run history, the event log and usage rollups are kept: the metadata store '
                '(default), or ClickHouse, for a data plane\'s rates (ADR 0029).', 'QUERYAPIGATE_RUN_STORE_URL',
                'the metadata store' if run_store_url() is None
                else f'ClickHouse ({redact_url(run_store_url())})', secret=True),
            row('Load examples', 'Load the example APIs at startup. Idempotent.',
                'QUERYAPIGATE_LOAD_EXAMPLES', on_off(load_examples())),
            row('Configuration file', 'A configuration file applied at startup, once per file - connections, '
                'queries, roles, keys and the rest as code (ADR 0014).', 'QUERYAPIGATE_CONFIG_FILE',
                str(config_file()) if config_file() else 'none'),
            row('Prune at startup', 'Applying it also deletes what a file manages and this one no longer has.',
                'QUERYAPIGATE_CONFIG_PRUNE', on_off(config_prune())),
            row('Locked to the file', 'What a file manages can be changed only through a file, not here or the API.',
                'QUERYAPIGATE_CONFIG_LOCKED', on_off(config_locked())),
            row('H2 driver', 'JAR used for h2 connections.', 'QUERYAPIGATE_H2_JAR',
                Path(h2_jar()).name + ('' if os.environ.get('QUERYAPIGATE_H2_JAR') else ' (bundled)'))]},
        {'id': 'security', 'title': 'Security',
         'description': 'Admin access, write protection and secrets at rest.', 'rows': [
            row('Secret files', 'Folders ${file:...} references may read - a connection\'s or destination\'s '
                'password from a file the CSI driver, Docker or Vault Agent keeps current (ADR 0015).',
                'QUERYAPIGATE_SECRET_FILE_DIRS', ', '.join(secret_file_dirs()) or 'none - ${file:...} refused'),
            row('Environment secrets', 'Variables ${VAR} references may read. Never the server\'s own settings, nor '
                'the cloud, Vault and Kubernetes credentials it may run with (AWS_*, AZURE_*, ARM_*, GOOGLE_*, GCP_*, '
                'VAULT_*, KUBERNETES_*).', 'QUERYAPIGATE_SECRET_ENV_VARS',
                ', '.join(secret_env_vars()) or 'any - set it to the ones your connections use'),
            row('Secret cache', 'How long a secret from a provider is kept in memory at most.',
                'QUERYAPIGATE_SECRET_CACHE_SECONDS', f'{secret_cache_seconds()} s'),
            row('Secret providers', 'How the ones this process has used answered, last.', '',
                _provider_status()),
            row('AWS', 'Region for ${aws-sm:...} and aws-rds-iam - else boto3\'s own. Secrets: names beginning with '
                'QUERYAPIGATE_AWS_SECRET_PREFIXES; IAM tokens: hosts in QUERYAPIGATE_AWS_RDS_HOSTS.',
                'QUERYAPIGATE_AWS_REGION', aws_region() or 'boto3\'s default'),
            row('AWS secret prefixes', 'Secret names ${aws-sm:...} may read begin with one of these.',
                'QUERYAPIGATE_AWS_SECRET_PREFIXES', ', '.join(aws_secret_prefixes()) or 'none - ${aws-sm:...} refused'),
            row('RDS IAM hosts', 'Hosts aws-rds-iam credentials may be made for.', 'QUERYAPIGATE_AWS_RDS_HOSTS',
                ', '.join(aws_rds_hosts()) or 'none - aws-rds-iam refused'),
            row('Vault', 'Where ${vault:...} references and vault-database credentials come from, and how this server '
                'signs in to it.', 'QUERYAPIGATE_VAULT_ADDR',
                f'{vault_addr()} ({vault_auth()})' if vault_addr() else 'off'),
            row('Vault paths', 'Path prefixes ${vault:...} may read.', 'QUERYAPIGATE_VAULT_PATHS',
                ', '.join(vault_paths()) or 'none - ${vault:...} refused'),
            row('Vault database roles', 'Roles of the database secrets engine a connection\'s credentials may ask for.',
                'QUERYAPIGATE_VAULT_DATABASE_ROLES', ', '.join(vault_database_roles()) or 'none'),
            row('Admin API key', 'Key with full access to this UI and the admin API. Unset means open access.',
                'QUERYAPIGATE_API_KEY', 'configured' if api_key() else 'not set - open access', secret=True),
            row('Allow writes', 'When off, statements that modify data are rejected.',
                'QUERYAPIGATE_ALLOW_WRITES', on_off(allow_writes())),
            row('Encryption at rest', 'Fernet key used to encrypt connection and destination passwords on disk; '
                'several, comma-separated, while rotating it - the first encrypts.',
                'QUERYAPIGATE_SECRET_KEY', _encryption_state(), secret=True),
            row('Metrics token', 'The bearer token /metrics needs. Unset: /metrics is open to whoever can reach it.',
                'QUERYAPIGATE_METRICS_TOKEN', 'configured' if metrics_token() else 'not set - /metrics is open',
                secret=True),
            row('Masking hash key', "Key for column masking's hash action. Unset: one made once in the store.",
                'QUERYAPIGATE_MASK_KEY', 'configured' if mask_key() else 'from the store', secret=True),
            row('Data plane secret', 'Shared with a Rust data plane in front of this server (ADR 0028), which then '
                'serves /q/ reads itself. Unset: no data plane can.',
                'QUERYAPIGATE_DATAPLANE_SECRET', 'configured' if dataplane_secret() else 'not set', secret=True),
            row('Export folders', 'Local folders a destination may write under. Unset: object storage only.',
                'QUERYAPIGATE_EXPORT_DIRS', ', '.join(export_dirs()) or 'object storage only'),
            row('Trusted proxy hops', 'Reverse proxies whose X-Forwarded-* headers are trusted.',
                'QUERYAPIGATE_TRUST_PROXY', str(proxy_hops())),
            row('HTTPS only (HSTS)', 'Seconds browsers should use only HTTPS for this host; sent only on requests '
                'that arrived over HTTPS. Unset: not sent.', 'QUERYAPIGATE_HSTS_MAX_AGE',
                f'{hsts_max_age()} seconds' if hsts_max_age() else 'off')]},
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
            row('User claim', 'Claim identifying the user.', 'QUERYAPIGATE_JWT_USER_CLAIM', jwt_user_claim()),
            row('Workspace claim', 'Claim naming the workspace a user belongs to (ADR 0023); unset, the default '
                'workspace.', 'QUERYAPIGATE_JWT_WORKSPACE_CLAIM', jwt_workspace_claim() or 'not set')]},
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
        {'id': 'pool', 'title': 'Threads & connection pool',
         'description': 'Requests served at once, and idle connections kept open per distinct connection.', 'rows': [
            row('Threads', "Requests one server process serves at once - the Docker image's gunicorn. Read at "
                'startup.', 'QUERYAPIGATE_THREADS', str(threads())),
            row('Pool size', 'Idle connections kept per connection - by default, one per thread. 0 disables pooling.',
                'QUERYAPIGATE_POOL_SIZE', str(pool_size())),
            row('Idle timeout', 'How long an idle pooled connection is kept before it is closed.',
                'QUERYAPIGATE_POOL_IDLE_TIMEOUT', _seconds(pool_idle_timeout())),
            row('Metadata cache', 'How long a key, saved query or connection read from the store is reused. '
                '0 reads them on every request.', 'QUERYAPIGATE_METADATA_CACHE_TTL',
                _seconds(metadata_cache_ttl() or None) or 'off')]},
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
        {'id': 'tracing', 'title': 'Tracing (OpenTelemetry)',
         'description': 'Spans to your tracing backend through an OpenTelemetry collector - experimental. On when an '
             'OTLP endpoint is set, with `pip install "queryapigate[otel]"`; every other OTEL_* variable works as '
             'for any service. Spans never hold parameter values, rows, statements or credentials.', 'rows': [
            row('OTLP endpoint', 'Where spans are sent (OTLP over HTTP). Unset: no tracing.',
                'OTEL_EXPORTER_OTLP_ENDPOINT', redact_url(os.environ['OTEL_EXPORTER_OTLP_ENDPOINT'])
                if os.environ.get('OTEL_EXPORTER_OTLP_ENDPOINT', '').strip() else 'off'),
            row('OTLP traces endpoint', 'The same, for traces only - wins over the one above.',
                'OTEL_EXPORTER_OTLP_TRACES_ENDPOINT', redact_url(os.environ['OTEL_EXPORTER_OTLP_TRACES_ENDPOINT'])
                if os.environ.get('OTEL_EXPORTER_OTLP_TRACES_ENDPOINT', '').strip() else 'not set'),
            row('Service name', "The name spans are filed under.", 'OTEL_SERVICE_NAME',
                os.environ.get('OTEL_SERVICE_NAME', '').strip() or 'queryapigate'),
            row('Sampler', "Which traces are kept: the SDK's default follows the caller's decision.",
                'OTEL_TRACES_SAMPLER', os.environ.get('OTEL_TRACES_SAMPLER', '').strip() or 'parentbased_always_on')]},
        {'id': 'audit', 'title': 'Audit & logging',
         'description': 'Retention of administrative actions and log format.', 'rows': [
            row('Audit log limit', 'Entries kept in queryapigate.db.', 'QUERYAPIGATE_AUDIT_LOG_LIMIT',
                str(audit_log_limit())),
            row('Audit export file', 'Append-only JSON-lines copy of every audit entry, never rolled off.',
                'QUERYAPIGATE_AUDIT_LOG_EXPORT_FILE', 'not set' if export is None else str(export)),
            row('Audit to syslog', 'Stream every audit entry to a SIEM over syslog (RFC 5424): tls://host:6514, or '
                'tcp://host:514. Unset: no stream.', 'QUERYAPIGATE_AUDIT_SYSLOG_URL',
                redact_url(os.environ['QUERYAPIGATE_AUDIT_SYSLOG_URL'])
                if os.environ.get('QUERYAPIGATE_AUDIT_SYSLOG_URL', '').strip() else 'off'),
            row('Audit syslog CA', "CA certificate to trust the syslog server's with. Unset: the system's.",
                'QUERYAPIGATE_AUDIT_SYSLOG_CA', _env('QUERYAPIGATE_AUDIT_SYSLOG_CA') or 'system'),
            row('Audit syslog client certificate', 'For mutual TLS, with its key below.',
                'QUERYAPIGATE_AUDIT_SYSLOG_CERT', _env('QUERYAPIGATE_AUDIT_SYSLOG_CERT') or 'none'),
            row('Audit syslog client key', 'The client certificate\'s key.', 'QUERYAPIGATE_AUDIT_SYSLOG_KEY',
                'configured' if _env('QUERYAPIGATE_AUDIT_SYSLOG_KEY') else 'none', secret=True),
            row('Audit to OTLP logs', 'Stream every audit entry as OpenTelemetry log records, to the OTLP logs '
                'endpoint.', 'QUERYAPIGATE_AUDIT_OTLP', on_off(env_flag('QUERYAPIGATE_AUDIT_OTLP'))),
            row('Audit chain key', 'HMAC key the audit log is chained with, so an edit made without it shows. Unset: '
                'plain SHA-256 - consistent, but anyone with the store can recompute it.', 'QUERYAPIGATE_AUDIT_KEY',
                _audit_key_state(), secret=True),
            row('JSON logs', 'One JSON object per line instead of plain text.', 'QUERYAPIGATE_JSON_LOGS',
                on_off(json_logs())),
            row('Request log', 'A line for every request and each query it runs. Errors, warnings and slow queries '
                'are logged either way.', 'QUERYAPIGATE_LOG_REQUESTS', on_off(log_requests())),
            row('Access log', "The Docker image's gunicorn access line per request, on stdout. Read at startup.",
                'QUERYAPIGATE_ACCESS_LOG', on_off(access_log()))]},
        {'id': 'history', 'title': 'Run history',
         'description': 'What each run - of a saved query, or ad-hoc SQL - leaves behind in its history, and for how '
             'long. GET /api/v1/history (and a query\'s own history) pages through everything kept.', 'rows': [
            row('History limit', 'Runs kept per saved-query version, unless a retention period is set.',
                'QUERYAPIGATE_HISTORY_LIMIT', f'{history_limit()} runs'),
            row('Ad-hoc history limit', 'Ad-hoc SQL runs kept in all, unless a retention period is set.',
                'QUERYAPIGATE_HISTORY_ADHOC_LIMIT', f'{history_adhoc_limit()} runs'),
            row('Ad-hoc SQL kept', 'What an ad-hoc run\'s history keeps of its SQL: the text (first '
                f'{HISTORY_ADHOC_SQL_MAX} characters), a hash, or none - and with hash or none, the log shows every '
                'statement by its hash too. Parameter values are never kept.',
                'QUERYAPIGATE_HISTORY_ADHOC_SQL', history_adhoc_sql()),
            row('Access log query strings', 'The access log keeps each request\'s query string - parameter values '
                'sent in the URL. Off: the path alone (ADR 0016).', 'QUERYAPIGATE_ACCESS_LOG_QUERY',
                on_off(access_log_query())),
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
                'this process only.', '', 'in-process'),
            row('Streams on this server', 'Concurrent GET /events streams this server holds open - each takes '
                'a request thread. Beyond it, /events answers 503. 0 turns it off.',
                'QUERYAPIGATE_EVENTS_MAX_STREAMS', str(events_max_streams())),
            row('Events server port', 'Port of `queryapigate events`: a separate process for many clients '
                '(apps, phones), every instance\'s runs, and resuming with Last-Event-ID.',
                'QUERYAPIGATE_EVENTS_PORT', str(events_port())),
            row('Events server connections', 'Open streams one `queryapigate events` process accepts.',
                'QUERYAPIGATE_EVENTS_MAX_CONNECTIONS', str(events_max_connections())),
            row('Events poll interval', 'Seconds between checks for new runs (on PostgreSQL a notification '
                'usually arrives first).', 'QUERYAPIGATE_EVENTS_POLL_INTERVAL', _seconds(events_poll_interval())),
            row('Event retention', 'Hours the event log keeps an event: how far back a client can resume with '
                'Last-Event-ID.', 'QUERYAPIGATE_EVENTS_RETENTION_HOURS', f'{events_retention_hours()} h')]},
        {'id': 'alerts', 'title': 'Alerts',
         'description': 'When GET /api/v1/alerts (the Console\'s Alerts screen) calls something out. A query is '
             'slow when its typical run is over the slow-query threshold (Query execution).', 'rows': [
            row('Query error rate', 'Share of a saved query\'s recent runs that may fail before it is an alert. '
                '0 turns the check off.', 'QUERYAPIGATE_ALERT_ERROR_RATE',
                f'{alert_error_rate()}%' if alert_error_rate() else 'off'),
            row('Unused key', 'Days an active API key may go unused before it is an alert. 0 turns the check off.',
                'QUERYAPIGATE_ALERT_KEY_UNUSED_DAYS',
                f'{alert_key_unused_days()} days' if alert_key_unused_days() else 'off'),
            row('Agent volume', 'Rows an AI agent may read over MCP in an hour before a sudden rise is an alert. 0 '
                'turns the check off.', 'QUERYAPIGATE_ALERT_AGENT_ROWS',
                f'{alert_agent_rows():,} rows' if alert_agent_rows() else 'off')]},
        {'id': 'sso', 'title': 'Console sign-in (SSO)',
         'description': 'Administrators sign in to the Console through your identity provider (OpenID Connect), '
             'with roles from their groups. Off unless the first four are set.', 'rows': [
            row('Issuer', 'The identity provider administrators sign in through.', 'QUERYAPIGATE_SSO_ISSUER',
                sso_issuer() or 'off'),
            row('Client ID', 'This server, as registered at the provider.', 'QUERYAPIGATE_SSO_CLIENT_ID',
                sso_client_id() or 'not set'),
            row('Client secret', 'The client\'s secret at the provider.', 'QUERYAPIGATE_SSO_CLIENT_SECRET',
                'configured' if sso_client_secret() else 'not set', secret=True),
            row('Redirect URL', 'Where the provider sends people back to: ends with /auth/sso/callback.',
                'QUERYAPIGATE_SSO_REDIRECT_URL', sso_redirect_url() or 'not set'),
            row('Roles', 'Groups to roles, e.g. owner=qag-owners,admin=qag-admins.', 'QUERYAPIGATE_SSO_ROLES',
                _env('QUERYAPIGATE_SSO_ROLES') or 'not set'),
            row('Groups claim', 'The ID token claim holding a person\'s groups.', 'QUERYAPIGATE_SSO_GROUPS_CLAIM',
                sso_groups_claim()),
            row('Session length', 'How long a Console session lasts.', 'QUERYAPIGATE_SSO_SESSION_HOURS',
                f'{sso_session_hours()} hours' if _env('QUERYAPIGATE_SSO_SESSION_HOURS') is None
                or _env('QUERYAPIGATE_SSO_SESSION_HOURS').isdigit() else 'invalid'),
            row('SSO only', 'Administrators who sign in through SSO have no personal tokens; automation keeps its.',
                'QUERYAPIGATE_SSO_ONLY', on_off(sso_only())),
            row('Default role', 'The role for someone in no mapped group - only with allowed domains.',
                'QUERYAPIGATE_SSO_DEFAULT_ROLE', sso_default_role() or 'none'),
            row('Allowed domains', 'Verified email domains a default role is given to.',
                'QUERYAPIGATE_SSO_ALLOWED_DOMAINS', ', '.join(sso_allowed_domains()) or 'any')]},
        {'id': 'mcp', 'title': 'MCP server',
         'description': 'Settings for `queryapigate mcp` (BACKLOG #42) - a separate process this one doesn\'t '
             'start. "Check now" tries its port on this host.', 'rows': [
            row('MCP port', 'Bind port for `queryapigate mcp`.', 'QUERYAPIGATE_MCP_PORT', str(mcp_port())),
            row('MCP max rows', 'Row cap for a tools/call result - an LLM\'s context can\'t hold a huge one.',
                'QUERYAPIGATE_MCP_MAX_ROWS', f'{mcp_max_rows()} rows'),
            row('Embeddings endpoint', 'An OpenAI-compatible embeddings URL - turns an `embed` parameter\'s text '
                'into a vector for a pgvector search.', 'QUERYAPIGATE_EMBEDDING_URL', embedding_url() or 'off'),
            row('Embeddings model', 'Model name sent to the embeddings endpoint.', 'QUERYAPIGATE_EMBEDDING_MODEL',
                embedding_model() or 'not set'),
            row('Embeddings API key', 'Bearer token for the embeddings endpoint, if it needs one.',
                'QUERYAPIGATE_EMBEDDING_API_KEY', 'configured' if embedding_api_key() else 'not set', secret=True),
            row('Write approval window', 'Minutes an agent\'s write waits for a person to approve it, and then to run.',
                'QUERYAPIGATE_WRITE_APPROVAL_MINUTES', f'{write_approval_minutes()} minutes'),
            row('Console URL', 'Where people reach the Console - for the approval links agents are given.',
                'QUERYAPIGATE_CONSOLE_URL', console_url() or 'not set (links are a path)'),
            row('Resource URL', 'The MCP server\'s own URL - the audience an agent\'s OAuth token must name. With '
                'the next one, turns on sign-in through your identity provider for AI clients.',
                'QUERYAPIGATE_MCP_RESOURCE_URL', mcp_resource_url() or 'off'),
            row('Authorization server', 'Issuer URL of the identity provider that signs agents in and issues their '
                'tokens.', 'QUERYAPIGATE_MCP_AUTH_SERVER', mcp_auth_server() or 'off'),
            row('Scope', 'Scope a token must carry to use the MCP server.', 'QUERYAPIGATE_MCP_SCOPE', mcp_scope()),
            row('Role', 'Role whose grants every agent signed in this way gets.', 'QUERYAPIGATE_MCP_ROLE',
                mcp_role() or 'not set'),
            row('Role claim', 'Claim naming an agent\'s role instead.', 'QUERYAPIGATE_MCP_ROLE_CLAIM',
                mcp_role_claim() or 'not set')]},
    ]

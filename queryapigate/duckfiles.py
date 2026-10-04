"""What a DuckDB connection may read (BACKLOG #75): its `allowed_paths`, enforced by DuckDB itself.

A DuckDB connection can read files straight from SQL - `read_parquet('s3://sales/2026/*.parquet')`,
`FROM 'https://data.example.com/prices.csv'` - so the question is never "can it", but "which". Rather than parse the
SQL for every way DuckDB has of reading a file, each connection is locked as it opens:

- `allowed_paths` become DuckDB's `allowed_directories` (entries ending in `/`) and `allowed_paths` (exact files);
- `enable_external_access` is turned off, so nothing else - no other file, URL, `ATTACH` or `COPY` target - is
  reachable, and extensions can't be installed or loaded;
- `lock_configuration` stops a query from changing any of it back.

With no `allowed_paths`, a connection reads no files at all: only its own database.

HTTP(S) entries must name files, never prefixes: a web server resolves `..` in a URL path, and DuckDB compares the
URL as written, so `https://host/public/../private/x` would pass a `https://host/public/` prefix. Object storage
(`s3://`, `gs://`, `r2://`) and local folders take prefixes. A bucket prefix is still only a second lock: an
S3-compatible server could resolve `..` in a key too, so the credentials' own permissions are the real boundary.

Credentials for object storage reuse the connection's `user` (access key id) and `password` (secret), so the secret
is masked, encrypted at rest and `${VAR}`-capable like any password. `views` ({name: SELECT ...}) are created as
temporary views on each new connection, giving the schema browser and `allowed_tables` names to work with.
"""
import hashlib
import json
import os
import re
import threading

from .errors import ApiError

REMOTE_SCHEMES = ('s3://', 'gs://', 'gcs://', 'r2://', 'http://', 'https://')
_HTTP = ('http://', 'https://')
STORAGE_TYPES = ('s3', 'gcs', 'r2')
_VIEW_NAME_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]{0,62}$')
_SECRET_OPTIONS = {'region': 'REGION', 'endpoint': 'ENDPOINT', 'url_style': 'URL_STYLE', 'account_id': 'ACCOUNT_ID'}


_DENIED_RE = re.compile(r'Cannot access (?:file|directory) "([^"]+)"')


def not_allowed(error):
    """DuckDB's refusal of a path outside allowed_paths, as a clear 403 rather than a failed query."""
    match = _DENIED_RE.search(str(error))
    what = f"'{match.group(1)}'" if match else 'that file'
    return ApiError(f"This connection may not read {what} - it isn't under the connection's allowed_paths", 403,
                    code='path_not_allowed')


def _invalid(message):
    return ApiError(message, code='invalid_body')


def validate(details):
    """Check a DuckDB connection's file-access fields; raises ApiError(invalid_body) naming the problem."""
    paths = details.get('allowed_paths')
    if paths is not None:
        if not isinstance(paths, list) or not all(isinstance(p, str) and p.strip() for p in paths):
            raise _invalid('allowed_paths must be a list of paths or URLs')
        for path in paths:
            segments = path.replace('\\', '/').split('/')
            if '..' in segments or '*' in path or ('?' in path and not path.startswith(_HTTP)):
                raise _invalid(f"allowed_paths entry '{path}' can't contain '..' segments or wildcards")
            if not path.startswith(REMOTE_SCHEMES) and not os.path.isabs(path):
                raise _invalid(f"allowed_paths entry '{path}' must be an absolute path (or an s3://, gs://, r2:// or "
                               'http(s):// address) - DuckDB checks a path as the SQL writes it, so a relative one '
                               'would never match')
            if path.startswith(_HTTP) and path.endswith('/'):
                raise _invalid(f"allowed_paths entry '{path}' must name a file: a web server resolves '..' in a URL, "
                               'so an HTTP prefix would not stop a request outside it')
    views = details.get('views')
    if views is not None:
        if not isinstance(views, dict) or not all(isinstance(sql, str) and sql.strip() for sql in views.values()):
            raise _invalid('views must be an object of {name: SELECT statement}')
        for name in views:
            if not _VIEW_NAME_RE.match(name):
                raise _invalid(f"view name '{name}' must be letters, digits and '_', starting with a letter or '_'")
    storage = details.get('storage')
    if storage is not None and storage not in STORAGE_TYPES:
        raise _invalid(f"storage must be one of: {', '.join(STORAGE_TYPES)}")
    if 'use_ssl' in details and not isinstance(details['use_ssl'], bool):
        raise _invalid('use_ssl must be true or false')


def _resolve_local(path):
    if path.startswith(REMOTE_SCHEMES):
        return path
    resolved = os.path.normpath(path)
    return resolved + os.sep if path.endswith(('/', os.sep)) else resolved


def is_remote(details):
    return any(p.startswith(REMOTE_SCHEMES) for p in details.get('allowed_paths') or [])


def _quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def _load_httpfs(conn):
    try:
        conn.execute('LOAD httpfs')
        return
    except Exception:
        pass
    try:
        conn.execute('INSTALL httpfs')
        conn.execute('LOAD httpfs')
    except Exception as error:
        raise ApiError("Reading s3://, gs://, r2:// or http(s):// files needs DuckDB's httpfs extension, which isn't "
                       f'installed and could not be downloaded: {str(error).splitlines()[0]}', 500,
                       code='connection_misconfigured') from None


def _create_secret(conn, details):
    storage = details.get('storage') or 's3'
    parts = [f'TYPE {storage}', f"KEY_ID {_quote(details['user'])}", f"SECRET {_quote(details.get('password') or '')}"]
    for key, name in _SECRET_OPTIONS.items():
        if details.get(key) not in (None, ''):
            parts.append(f'{name} {_quote(details[key])}')
    if 'use_ssl' in details:
        parts.append(f"USE_SSL {'true' if details['use_ssl'] else 'false'}")
    conn.execute(f"CREATE OR REPLACE SECRET queryapigate_storage ({', '.join(parts)})")


# DuckDB keeps settings and secrets per database, not per connection: every connection this process opens to the same
# file shares them. So a file is locked once, by the first connection to it, and a later connection to the same file
# must want exactly the same - or it would silently get the first one's paths and credentials.
_fingerprints: dict[str, str] = {}
_fingerprints_lock = threading.Lock()


def _fingerprint(details):
    access = {k: details.get(k) for k in ('allowed_paths', 'user', 'password', 'storage', *_SECRET_OPTIONS, 'use_ssl')}
    return hashlib.sha256(json.dumps(access, sort_keys=True, default=str).encode()).hexdigest()


def lock_down(conn, details):
    """Set up a freshly opened DuckDB connection - credentials, views - then restrict it to its allowed_paths."""
    database = details.get('database')
    shared = database != ':memory:'
    with _fingerprints_lock:
        locked = conn.execute("SELECT current_setting('lock_configuration')").fetchone()[0]
        if locked and shared:
            if _fingerprints.get(database) != _fingerprint(details):
                raise ApiError(f"Another connection to '{database}' on this server has different allowed_paths or "
                               "storage credentials - DuckDB shares them per database file, so connections to the "
                               'same file must agree', 500, code='connection_misconfigured')
        else:
            _configure(conn, details)
            if shared:
                _fingerprints[database] = _fingerprint(details)
    for name, sql in (details.get('views') or {}).items():  # temporary views belong to each connection
        try:
            conn.execute(f'CREATE OR REPLACE TEMP VIEW "{name}" AS {sql}')
        except ApiError:
            raise
        except Exception as error:
            if 'Permission Error' in str(error):
                raise not_allowed(error) from None
            raise ApiError(f"View '{name}' could not be created: {str(error).splitlines()[0]}", 500,
                           code='connection_misconfigured') from None


def _configure(conn, details):
    paths = [_resolve_local(p) for p in details.get('allowed_paths') or []]
    conn.execute('SET allow_persistent_secrets = false')  # never the host's own ~/.duckdb secrets
    if is_remote(details):
        _load_httpfs(conn)
        if details.get('user'):
            _create_secret(conn, details)
    directories = [p for p in paths if p.endswith(('/', os.sep))]
    files = [p for p in paths if not p.endswith(('/', os.sep))]
    conn.execute(f"SET allowed_directories = [{', '.join(_quote(p) for p in directories)}]")
    conn.execute(f"SET allowed_paths = [{', '.join(_quote(p) for p in files)}]")
    conn.execute('SET autoinstall_known_extensions = false')
    conn.execute('SET autoload_known_extensions = false')
    conn.execute('SET enable_external_access = false')
    conn.execute('SET lock_configuration = true')

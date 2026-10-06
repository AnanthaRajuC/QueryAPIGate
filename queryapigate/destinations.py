"""Destinations: where exports may write (ADR 0004, BACKLOG #89).

A destination is a prefix - an `s3://`, `gs://` or `r2://` bucket prefix, or a local folder - and the credentials
to write under it. Nothing else: what is written, and when, belongs to an export (exports.py).

Writes can't leave the prefix. `open_writer()` returns a fresh in-memory DuckDB connection locked to exactly that
prefix by duckfiles.lock_down() - `allowed_paths = [prefix]`, external access otherwise off, the configuration
locked - so no path template, parameter value or file name can reach anything else; DuckDB itself refuses.

The secret (`password`) is stored like a connection password: masked in every response, a `${VAR}` reference kept
as written, a literal one encrypted with QUERYAPIGATE_SECRET_KEY when set (store.py).
"""
import json
import os
import re
import time

from . import config, db, duckfiles, store
from .errors import ApiError

FIELDS = ('url', 'storage', 'region', 'endpoint', 'url_style', 'use_ssl', 'account_id', 'user', 'password')
_NAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$')
_SCHEME_STORAGE = {'s3://': 's3', 'gs://': 'gcs', 'gcs://': 'gcs', 'r2://': 'r2'}
PROBE_FILE = '_queryapigate_probe.csv'  # what POST .../test writes - one small object, overwritten each time


def _invalid(message):
    return ApiError(message, code='invalid_body')


def _scheme(url):
    return next((scheme for scheme in _SCHEME_STORAGE if url.startswith(scheme)), None)


def local_path(url):
    """A local destination's folder, from `file:///exports/` or `/exports/`; None for object storage."""
    if _scheme(url):
        return None
    return url[len('file://'):] if url.startswith('file://') else url


def validate(fields, partial=False):
    """The destination's stored fields, normalised; raises invalid_body naming the problem."""
    if not isinstance(fields, dict):
        raise _invalid('The request body must be a JSON object')
    unknown = sorted(set(fields) - set(FIELDS))
    if unknown:
        raise ApiError(f"Unknown field(s): {', '.join(unknown)}", code='unknown_field')
    out = {k: v for k, v in fields.items() if v is not None}
    url = out.get('url')
    if url is None:
        if not partial:
            raise _invalid('url is required: an s3://, gs:// or r2:// prefix, or a local folder, ending in /')
    else:
        if not isinstance(url, str) or not url.endswith('/'):
            raise _invalid('url must be a prefix ending in / - s3://bucket/exports/, or a folder')
        segments = url.split('/')
        if '..' in segments or any(c in url for c in '*?[]{}\'"\\'):
            raise _invalid("url can't contain '..', wildcards, quotes or backslashes")
        scheme = _scheme(url)
        if scheme is None:
            folder = local_path(url)
            if url.startswith(('http://', 'https://')) or not os.path.isabs(folder):
                raise _invalid('url must be an s3://, gs:// or r2:// prefix, or an absolute folder (file:///path/ or '
                               '/path/) - exports are never written over HTTP')
        elif len(url) <= len(scheme) + 1:
            raise _invalid('url must name a bucket')
        expected = _SCHEME_STORAGE.get(scheme) if scheme else None
        if out.get('storage') is not None and expected and out['storage'] != expected:
            raise _invalid(f"storage '{out['storage']}' doesn't match {scheme} - leave it out, or use '{expected}'")
        if expected:
            out['storage'] = expected
    for key in ('storage', 'region', 'endpoint', 'url_style', 'account_id', 'user', 'password'):
        if key in out and not isinstance(out[key], str):
            raise _invalid(f'{key} must be text')
    if out.get('url_style') not in (None, 'vhost', 'path'):
        raise _invalid("url_style must be 'vhost' or 'path'")
    if 'use_ssl' in out and not isinstance(out['use_ssl'], bool):
        raise _invalid('use_ssl must be true or false')
    return out


def _check_name(name):
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise ApiError("A destination's name is 1-100 letters, digits, '.', '_' or '-', starting with a letter or "
                       'digit', code='invalid_name')


# --------------------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------------------

def _row(row):
    return {'name': row['name'], 'created_at': row['created_at'], 'updated_at': row['updated_at'],
            **json.loads(row['details_json'])}


def _load(conn, name):
    row = conn.execute('SELECT name, created_at, updated_at, details_json FROM destinations WHERE name = ?',
                       (name,)).fetchone()
    return _row(row) if row is not None else None


def masked(destination):
    """As every response shows it: the secret masked, a ${VAR} reference as written."""
    return store.mask_passwords({'_': destination})['_'] if destination.get('password') else destination


def list_all():
    rows = db.connection().execute(
        'SELECT name, created_at, updated_at, details_json FROM destinations ORDER BY name').fetchall()
    return [_row(row) for row in rows]


def get(name):
    destination = _load(db.connection(), name)
    if destination is None:
        raise ApiError(f"Destination '{name}' not found", 404, code='destination_not_found')
    return destination


def _write(conn, name, fields, created_at, timestamp):
    details = {k: v for k, v in fields.items() if k in FIELDS}
    conn.execute("""
        INSERT INTO destinations (name, created_at, updated_at, details_json) VALUES (?, ?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET updated_at = excluded.updated_at, details_json = excluded.details_json
    """, (name, created_at, timestamp, json.dumps(details)))


def create(name, fields):
    _check_name(name)
    fields = validate(fields)
    if 'password' in fields:
        fields['password'] = store._encrypt_password(fields['password'])
    with db.transaction() as conn:
        if _load(conn, name) is not None:
            raise ApiError(f"A destination named '{name}' already exists", 409, code='destination_exists')
        timestamp = store.now()
        _write(conn, name, fields, timestamp, timestamp)
    return get(name)


def update(name, changes):
    """Change some fields; null removes an optional one. The masked secret sent back unchanged keeps it."""
    validate(changes, partial=True)
    with db.transaction() as conn:
        current = _load(conn, name)
        if current is None:
            raise ApiError(f"Destination '{name}' not found", 404, code='destination_not_found')
        fields = {k: current[k] for k in FIELDS if k in current}
        for key, value in changes.items():
            if value is None:
                fields.pop(key, None)
            elif key == 'password' and value == config.PASSWORD_MASK:
                continue  # echoed back from a GET: keep the stored (possibly encrypted) secret
            else:
                fields[key] = store._encrypt_password(value) if key == 'password' else value
        if 'url' in changes and changes['url'] is not None and 'storage' not in changes:
            fields.pop('storage', None)  # re-derived from the new url's scheme
        fields = {**validate({k: v for k, v in fields.items() if k != 'password'}),
                  **({'password': fields['password']} if 'password' in fields else {})}
        _write(conn, name, fields, current['created_at'], store.now())
    return get(name)


def delete(name, used_by=()):
    """Remove it. Refused while an export still writes there (`used_by` names them)."""
    if used_by:
        raise ApiError(f"Destination '{name}' is used by export(s) {', '.join(sorted(used_by))} - change or delete "
                       'them first', 409, code='destination_in_use')
    with db.transaction() as conn:
        if _load(conn, name) is None:
            raise ApiError(f"Destination '{name}' not found", 404, code='destination_not_found')
        conn.execute('DELETE FROM destinations WHERE name = ?', (name,))


# --------------------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------------------

def _usable(destination):
    """The fields with ${VAR} expanded and the secret decrypted - only ever at the moment of writing."""
    return store.resolve_ad_hoc({k: destination[k] for k in FIELDS if k in destination})


def open_writer(destination):
    """A DuckDB connection that may write under the destination's prefix and nowhere else. Returns (conn, prefix):
    `prefix` is how a target inside it is spelled for DuckDB."""
    import duckdb
    fields = _usable(destination)
    url = fields['url']
    folder = local_path(url)
    prefix = os.path.normpath(folder) + os.sep if folder is not None else url
    if folder is not None:
        os.makedirs(prefix, exist_ok=True)
    details = {'database': ':memory:', 'allowed_paths': [prefix],
               **{k: fields[k] for k in ('storage', 'region', 'endpoint', 'url_style', 'use_ssl', 'account_id',
                                         'user', 'password') if fields.get(k) not in (None, '')}}
    conn = duckdb.connect()
    try:
        duckfiles.lock_down(conn, details)
    except Exception:
        conn.close()
        raise
    return conn, prefix


def target(prefix, relative):
    """Where `relative` lands under `prefix` - refusing anything that would climb out, before DuckDB does too. For a
    local folder, its parent folders are created (DuckDB doesn't)."""
    if not relative or relative.startswith('/') or '\\' in relative or '..' in relative.split('/'):
        raise _invalid(f"The export path '{relative}' must be relative to the destination, without '..'")
    full = prefix + relative
    if not prefix.startswith(tuple(_SCHEME_STORAGE)):
        os.makedirs(os.path.dirname(full), exist_ok=True)
    return full


def test(destination):
    """Write one small object (PROBE_FILE) under the prefix, as an export would: proves the url, credentials and
    write permission together. Returns {'object', 'elapsed_ms'}."""
    started = time.monotonic()
    conn, prefix = open_writer(destination)
    try:
        path = target(prefix, PROBE_FILE)
        conn.execute(f"COPY (SELECT 'queryapigate' AS probe, now() AS written_at) TO {duckfiles._quote(path)} "
                     '(FORMAT csv, HEADER true)')
    except ApiError:
        raise
    except Exception as error:
        if 'Permission Error' in str(error):
            raise duckfiles.not_allowed(error) from None
        raise ApiError(f"Couldn't write to the destination: {str(error).splitlines()[0]}", 502,
                       code='destination_unreachable') from None
    finally:
        conn.close()
    return {'object': path, 'elapsed_ms': round((time.monotonic() - started) * 1000, 1)}

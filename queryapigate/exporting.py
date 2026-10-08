"""Delivering a result to a destination (ADR 0004, BACKLOG #89): render the object's path, stage the rows, and have
DuckDB write them there as Parquet, CSV or NDJSON.

Rows pass through once, into a temporary staging file (parquet.stage()) - never all held in memory - and DuckDB
copies from it through a connection locked to the destination's prefix plus that one staging file
(destinations.open_writer()). An object-storage upload is all-or-nothing: a failed run leaves no partial object.
"""
import os
import re
import secrets
from datetime import datetime

from . import destinations, duckfiles, parquet
from .errors import ApiError

FORMATS = ('parquet', 'csv', 'ndjson')
_PLACEHOLDER_RE = re.compile(r'\{([^{}]*)\}')
_SAFE_VALUE_RE = re.compile(r'^[A-Za-z0-9._-]{1,100}$')
BUILT_IN = ('name', 'date', 'time', 'run')


def new_run_id():
    return secrets.token_hex(6)


def render_path(template, name, params=None, run_id=None, now=None):
    """The object path for one run, from a template such as `orders/{date}/orders_{run}.parquet`: `{name}` (the
    saved query), `{date}` (YYYY-MM-DD), `{time}` (HHMMSS), `{run}` (the run's id) and `{param}` for a parameter's
    value - which must be plain (letters, digits, '.', '_', '-'), so no value can add a folder or climb out."""
    if not isinstance(template, str) or not template.strip():
        raise ApiError('The export path is required, e.g. orders/{date}/orders_{run}.parquet', code='invalid_body')
    now = now or datetime.now()
    params = params or {}
    builtin = {'name': name, 'date': now.strftime('%Y-%m-%d'), 'time': now.strftime('%H%M%S'),
               'run': run_id or new_run_id()}

    def fill(match):
        key = match.group(1)
        if key in builtin:
            return builtin[key]
        if key not in params:
            raise ApiError(f"The export path has an unknown placeholder {{{key}}} - use {{name}}, {{date}}, {{time}}, "
                           '{run} or a parameter of the query', code='invalid_body')
        value = str(params[key])
        if not _SAFE_VALUE_RE.match(value):
            raise ApiError(f"Parameter '{key}' can't be used in the export path: its value must be letters, digits, "
                           "'.', '_' or '-'", code='invalid_body')
        return value

    if template.count('{') != template.count('}'):
        raise ApiError('The export path has an unmatched { or }', code='invalid_body')
    rendered = _PLACEHOLDER_RE.sub(fill, template)
    if rendered.startswith('/') or '\\' in rendered or '..' in rendered.split('/'):
        raise ApiError(f"The export path '{template}' must be relative to the destination, without '..'",
                       code='invalid_body')
    return rendered


def deliver(destination, relative, columns, rows, fmt='parquet', track=None, skip_empty=False):
    """Write `rows` under `columns` to `relative` in `destination`. Returns {'object', 'rows', 'bytes', 'largest'}:
    `object` None when `skip_empty` and there were no rows; `bytes` for a local folder only; `largest` the largest
    value of the `track` column (an incremental watermark), or None."""
    if fmt not in FORMATS:
        raise ApiError(f"format must be one of: {', '.join(FORMATS)}", code='invalid_body')
    parquet._require()
    staged = parquet.stage(columns, rows, track)
    try:
        if skip_empty and not staged.count:
            return {'object': None, 'rows': 0, 'bytes': None, 'largest': None}
        conn, prefix = destinations.open_writer(destination, read_files=[staged.path])
        try:
            target = destinations.target(prefix, relative)
            try:
                parquet.copy(conn, staged, target, fmt)
            except Exception as error:
                if 'Permission Error' in str(error):
                    raise duckfiles.not_allowed(error) from None
                raise ApiError(f"Couldn't write {target}: {str(error).splitlines()[0]}", 502,
                               code='destination_unreachable') from None
        finally:
            conn.close()
    finally:
        os.unlink(staged.path)
    size = os.path.getsize(target) if destinations.local_path(destination['url']) is not None else None
    return {'object': target, 'rows': staged.count, 'bytes': size, 'largest': staged.largest}

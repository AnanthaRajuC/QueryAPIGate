"""Saved exports (ADR 0004, BACKLOG #89): a saved query delivered to a destination - its parameters, format, object
path and, optionally, an incremental watermark so each run delivers only what changed.

A run (`run()`):
1. takes the export's **lease** in the store - one run at a time per export, on any instance; a second answers
   `409 export_running`. A lease outlives a crashed run only until it expires;
2. binds the **watermark** (or `incremental.start`, the first time) to the query's `incremental.parameter`;
3. streams the query's **published** version through exporting.deliver() - staged once, written by DuckDB under
   the destination's prefix;
4. **only once the object is written** stores the largest `incremental.column` value delivered as the new
   watermark: a failed run leaves it where it was, so the next one sends those rows again - at least once, never
   lost;
5. records the run in history (`transport: "export"`) and releases the lease, whatever happened.

The clock stays outside: cron, a Kubernetes CronJob or Airflow calls `POST /api/v1/exports/{name}/runs` or
`queryapigate exports run NAME`.
"""
import contextlib
import json
import re
import time
from datetime import date, datetime
from decimal import Decimal

from . import config, db, destinations, exporting, store
from .errors import ApiError

FIELDS = ('description', 'query', 'params', 'format', 'destination', 'path', 'incremental', 'skip_empty', 'timeout')
_NAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$')
LEASE_MARGIN = 300  # seconds a lease outlasts the run's own time limit, before another run may take it over
NO_LIMIT_LEASE = 3600  # a run's lease when there is no query time limit at all


def _invalid(message):
    return ApiError(message, code='invalid_body')


# --------------------------------------------------------------------------------------
# A saved query, ready to run - shared with `queryapigate export`
# --------------------------------------------------------------------------------------

def prepare(query, raw_params, connection=None):
    """(path, version, connection_name, sql, values) for running a saved query's published version with
    `raw_params` - every value checked against its declared parameter, as /q/<name> does."""
    from .params import resolve as resolve_params
    from .sqltools import fill_placeholders, placeholder_names
    path = store.resolve_saved_file(query)
    version, saved = store.select_version(store.load_versions(path), None)
    if saved.get('query_type') == 'mongo':
        raise ApiError("A MongoDB query can't be exported yet - only SQL queries", code='invalid_body')
    connection_name = connection or saved.get('connection_name')
    if not connection_name:
        raise ApiError('Connection name is missing - set one on the saved query')
    sql = saved.get('sql_query')
    if not isinstance(sql, str):
        raise ApiError('Saved query has no SQL', 500)
    used = set(placeholder_names(sql))
    values = resolve_params(saved.get('query_parameters'), raw_params, used=used)
    return path, version, connection_name, fill_placeholders(sql, values), values


# --------------------------------------------------------------------------------------
# Watermarks
# --------------------------------------------------------------------------------------

def _encode_watermark(value):
    """A delivered value as stored: JSON, with its kind, so it binds back as what it was."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal, datetime, date, str)):
        raise ApiError(f'An incremental column must hold numbers, text, dates or times, not {type(value).__name__}',
                       code='invalid_watermark')
    if isinstance(value, datetime):
        return json.dumps({'kind': 'datetime', 'value': value.isoformat(sep=' ')})
    if isinstance(value, date):
        return json.dumps({'kind': 'date', 'value': value.isoformat()})
    if isinstance(value, Decimal):
        return json.dumps({'kind': 'decimal', 'value': str(value)})
    return json.dumps({'kind': type(value).__name__, 'value': value})


def _decode_watermark(stored):
    """The value to bind for the next run: numbers as numbers, everything else as text, which each parameter type
    and database reads back (`2026-10-08 09:30:00` for a timestamp)."""
    if stored is None:
        return None
    return json.loads(stored)['value']


# --------------------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------------------

_COLUMNS = 'name, created_at, updated_at, watermark, lease_until, details_json'


def _row(row):
    return {'name': row['name'], 'created_at': row['created_at'], 'updated_at': row['updated_at'],
            **json.loads(row['details_json']), 'watermark': _decode_watermark(row['watermark']),
            'running': bool(row['lease_until'] and row['lease_until'] > time.time())}


def _load(conn, name):
    row = conn.execute(f'SELECT {_COLUMNS} FROM exports WHERE name = ?', (name,)).fetchone()
    return _row(row) if row is not None else None


def list_all():
    return [_row(row) for row in db.connection().execute(f'SELECT {_COLUMNS} FROM exports ORDER BY name').fetchall()]


def get(name):
    export = _load(db.connection(), name)
    if export is None:
        raise ApiError(f"Export '{name}' not found", 404, code='export_not_found')
    return export


def using_destination(destination):
    """Names of the exports that write to `destination` - which keep it from being deleted."""
    return [e['name'] for e in list_all() if e.get('destination') == destination]


def validate(fields):
    """An export's stored fields, checked against the destination and the saved query as they are now."""
    unknown = sorted(set(fields) - set(FIELDS))
    if unknown:
        raise ApiError(f"Unknown field(s): {', '.join(unknown)}", code='unknown_field')
    out = {k: v for k, v in fields.items() if v is not None}
    for key in ('query', 'destination', 'path'):
        if not isinstance(out.get(key), str) or not out[key].strip():
            raise _invalid(f'{key} is required')
    out.setdefault('format', 'parquet')
    if out['format'] not in exporting.FORMATS:
        raise _invalid(f"format must be one of: {', '.join(exporting.FORMATS)}")
    out.setdefault('params', {})
    if not isinstance(out['params'], dict):
        raise _invalid('params must be an object of {parameter: value}')
    out.setdefault('skip_empty', True)
    if not isinstance(out['skip_empty'], bool):
        raise _invalid('skip_empty must be true or false')
    if 'timeout' in out and (isinstance(out['timeout'], bool) or not isinstance(out['timeout'], (int, float))
                             or out['timeout'] <= 0):
        raise _invalid('timeout must be a positive number of seconds')
    if 'description' in out and not isinstance(out['description'], str):
        raise _invalid('description must be text')
    destinations.get(out['destination'])
    incremental = out.get('incremental')
    raw = dict(out['params'])
    if incremental is not None:
        if not isinstance(incremental, dict) or set(incremental) - {'column', 'parameter', 'start'} \
                or not all(isinstance(incremental.get(k), str) and incremental[k] for k in ('column', 'parameter')):
            raise _invalid('incremental must be {"column": ..., "parameter": ..., "start": ...} - column and parameter '
                           'naming a result column and one of the query\'s parameters')
        if incremental['parameter'] in out['params']:
            raise _invalid(f"'{incremental['parameter']}' is the incremental parameter - it can't also be in params")
        raw[incremental['parameter']] = incremental.get('start')
    # The query as it is published now: it must exist, use the incremental parameter, and take these values.
    if incremental is not None:
        from .sqltools import placeholder_names
        _, published = store.select_version(store.load_versions(store.resolve_saved_file(out['query'])), None)
        if incremental['parameter'] not in placeholder_names(published.get('sql_query') or ''):
            raise _invalid(f"The query doesn't use :{incremental['parameter']} - an incremental export binds the "
                           f"last value it delivered to it, e.g. WHERE {incremental['column']} > "
                           f":{incremental['parameter']}")
    _, _, _, _, values = prepare(out['query'], {k: v for k, v in raw.items() if v is not None})
    exporting.render_path(out['path'], out['query'], values)  # every placeholder must fill in
    return out


def _check_name(name):
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise ApiError("An export's name is 1-100 letters, digits, '.', '_' or '-', starting with a letter or digit",
                       code='invalid_name')


def _write(conn, name, fields, created_at):
    conn.execute(f"""
        INSERT INTO exports ({_COLUMNS}) VALUES (?, ?, ?, NULL, NULL, ?)
        ON CONFLICT(name) DO UPDATE SET updated_at = excluded.updated_at, details_json = excluded.details_json
    """, (name, created_at, store.now(), json.dumps({k: fields[k] for k in FIELDS if k in fields}, default=str)))


def create(name, fields):
    _check_name(name)
    fields = validate(fields)
    with db.transaction() as conn:
        if _load(conn, name) is not None:
            raise ApiError(f"An export named '{name}' already exists", 409, code='export_exists')
        _write(conn, name, fields, store.now())
    return get(name)


def update(name, changes):
    """Change some fields (null removes an optional one); `watermark` sets or (null) clears the incremental position."""
    if not isinstance(changes, dict):
        raise _invalid('The request body must be a JSON object')
    current = get(name)
    fields = {k: current[k] for k in FIELDS if k in current}
    for key, value in changes.items():
        if key == 'watermark':
            continue
        if value is None:
            fields.pop(key, None)
        else:
            fields[key] = value
    fields = validate(fields)
    with db.transaction() as conn:
        _write(conn, name, fields, current['created_at'])
        if 'watermark' in changes:
            conn.execute('UPDATE exports SET watermark = ? WHERE name = ?',
                         (_encode_watermark(changes['watermark']), name))
    return get(name)


def delete(name):
    with db.transaction() as conn:
        if _load(conn, name) is None:
            raise ApiError(f"Export '{name}' not found", 404, code='export_not_found')
        conn.execute('DELETE FROM exports WHERE name = ?', (name,))


# --------------------------------------------------------------------------------------
# Running
# --------------------------------------------------------------------------------------

def _take_lease(name, seconds):
    with db.transaction() as conn:
        row = conn.execute('SELECT lease_until FROM exports WHERE name = ?', (name,)).fetchone()
        if row is None:
            raise ApiError(f"Export '{name}' not found", 404, code='export_not_found')
        now = time.time()
        if row['lease_until'] and row['lease_until'] > now:
            raise ApiError(f"Export '{name}' is already running - try again once that run has finished", 409,
                           code='export_running', retry_after=max(1, int(row['lease_until'] - now)))
        conn.execute('UPDATE exports SET lease_until = ? WHERE name = ?', (now + seconds, name))


def _release(name, watermark=None, advance=False):
    with db.transaction() as conn:
        if advance:
            conn.execute('UPDATE exports SET lease_until = NULL, watermark = ? WHERE name = ?', (watermark, name))
        else:
            conn.execute('UPDATE exports SET lease_until = NULL WHERE name = ?', (name,))


def _check_watermark(export, value):
    """Refuse - before anything is written - a new watermark the next run couldn't bind: a timestamp for a parameter
    that only takes dates, say. Otherwise this run would deliver, and every run after it fail."""
    from .params import resolve as resolve_params
    incremental = export['incremental']
    _, saved = store.select_version(store.load_versions(store.resolve_saved_file(export['query'])), None)
    bound = _decode_watermark(_encode_watermark(value))
    try:
        resolve_params(saved.get('query_parameters'), {**export.get('params', {}), incremental['parameter']: bound},
                       used={incremental['parameter']})
    except ApiError as error:
        raise ApiError(f"The largest {incremental['column']} in this run, {bound!r}, isn't a value "
                       f":{incremental['parameter']} accepts ({error.message}), so the next run couldn't use it as "
                       'its watermark. Nothing was written. Make the column and the parameter the same kind - select '
                       'a date column for a date parameter, or widen the parameter.', code='invalid_watermark') \
            from None


def run(name, actor):
    """Run export `name` once. Returns the run: id, object, rows, bytes, watermark before and after."""
    from .engine import stream_sql
    export = get(name)
    timeout = config.effective_timeout(export.get('timeout'))
    _take_lease(name, (timeout or NO_LIMIT_LEASE) + LEASE_MARGIN)
    started = time.monotonic()
    run_id = exporting.new_run_id()
    incremental = export.get('incremental')
    before = export.get('watermark')
    entry = {'executed_at': store.now(), 'request_id': None, 'key_name': actor, 'transport': 'export',
             'export': name, 'destination': export['destination'], 'run_id': run_id}
    path = version = None
    try:
        raw = dict(export.get('params') or {})
        if incremental is not None:
            position = before if before is not None else incremental.get('start')
            if position is not None:
                raw[incremental['parameter']] = position
        path, version, connection_name, sql, values = prepare(export['query'], raw)
        entry['connection_name'] = connection_name
        relative = exporting.render_path(export['path'], export['query'], values, run_id)
        destination = destinations.get(export['destination'])
        columns, rows = stream_sql(sql, connection_name, values, timeout)
        with contextlib.closing(rows):  # a failed write must still end the query and give its connection back
            result = exporting.deliver(destination, relative, columns, rows, export.get('format', 'parquet'),
                                       track=incremental['column'] if incremental else None,
                                       skip_empty=export.get('skip_empty', True),
                                       check_largest=(lambda value: _check_watermark(export, value))
                                       if incremental else None)
    except ApiError as error:
        _release(name)
        # Recorded even when the query couldn't be prepared (deleted, a parameter no longer valid): history keeps no
        # run for a query that isn't there, so that one is kept without a query, as an ad-hoc run is - still tagged
        # with the export, which is what the export_failing alert and the runs list look for.
        store.record_execution(path, version, {**entry, 'status': 'error', 'error': error.message,
                                               'code': error.code})
        raise
    except BaseException:
        _release(name)
        raise
    largest = result['largest']
    after = _decode_watermark(_encode_watermark(largest)) if largest is not None else before
    _release(name, _encode_watermark(largest) if largest is not None else None, advance=largest is not None)
    outcome = {'id': run_id, 'export': name, 'object': result['object'], 'rows': result['rows'],
               'bytes': result['bytes'], 'watermark': {'from': before, 'to': after} if incremental else None,
               'duration_ms': round((time.monotonic() - started) * 1000, 1)}
    store.record_execution(path, version, {**entry, 'status': 'success', 'rows': result['rows'],
                                           'object': result['object'], 'bytes': result['bytes'],
                                           'watermark': outcome['watermark'], 'duration_ms': outcome['duration_ms']})
    return outcome

"""Parquet output for any query, on any database (BACKLOG #81) - written by DuckDB, so no new dependency.

Rows go once into a temporary newline-delimited JSON file while each column's type is worked out from its values,
then DuckDB reads that file with those types and writes Parquet. Nothing holds the whole result in Python, so a
streamed export (`?stream=true`, `queryapigate export --format parquet`) stays within DuckDB's own memory, which
spills to disk past its limit. The DuckDB connection here is a private, in-memory one, reading only the file this
module wrote - never anything a caller's SQL names.

Column types, from the values (nulls ignored): integers -> BIGINT (DECIMAL(38,0) past 64 bits), floats -> DOUBLE,
Decimal -> DECIMAL(38, its largest scale), booleans -> BOOLEAN, dates -> DATE, datetimes -> TIMESTAMP (TIMESTAMPTZ
with a time zone), times -> TIME; integers mixed with floats -> DOUBLE; anything else, or a mix, -> VARCHAR. A
column with no values at all is VARCHAR.
"""
import json
import os
import tempfile
from datetime import date, datetime, time
from decimal import Decimal

from .errors import ApiError

MIMETYPE = 'application/vnd.apache.parquet'
_INT64 = (-2 ** 63, 2 ** 63 - 1)


def available():
    try:
        import duckdb  # noqa: F401
    except ImportError:
        return False
    return True


def _require():
    if not available():
        raise ApiError('Parquet output needs DuckDB - run `pip install "queryapigate[duckdb]"` (the Docker image '
                       'includes it)', 500, code='format_unavailable')


def _kind(value):
    if isinstance(value, bool):
        return 'bool'
    if isinstance(value, int):
        return 'int' if _INT64[0] <= value <= _INT64[1] else 'bigint'
    if isinstance(value, float):
        return 'float'
    if isinstance(value, Decimal):
        return 'decimal' if value.is_finite() else 'other'
    if isinstance(value, datetime):
        return 'timestamptz' if value.tzinfo is not None else 'timestamp'
    if isinstance(value, date):
        return 'date'
    if isinstance(value, time):
        return 'time'
    return 'other'


def _encode(value):
    """A JSON-safe value DuckDB can cast to the column's type - exact for Decimal and dates, as text."""
    if isinstance(value, bool) or value is None or isinstance(value, (int, float, str)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    if isinstance(value, (dict, list)):
        return json.dumps(value, default=str)
    return str(value)


def _column_type(kinds, scale):
    if not kinds:
        return 'VARCHAR'
    if kinds == {'bool'}:
        return 'BOOLEAN'
    if kinds == {'int'}:
        return 'BIGINT'
    if kinds <= {'int', 'bigint'}:
        return 'DECIMAL(38,0)'
    if kinds <= {'int', 'float'}:
        return 'DOUBLE'
    if kinds <= {'int', 'bigint', 'decimal'}:
        return f'DECIMAL(38,{min(scale, 18)})'
    if kinds <= {'int', 'float', 'decimal'}:
        return 'DOUBLE'
    if kinds == {'date'}:
        return 'DATE'
    if kinds <= {'date', 'timestamp'}:
        return 'TIMESTAMP'
    if kinds == {'timestamptz'}:
        return 'TIMESTAMPTZ'
    if kinds == {'time'}:
        return 'TIME'
    return 'VARCHAR'


def _quote(text):
    return "'" + str(text).replace("'", "''") + "'"


def stage(columns, rows, track=None):
    """Write `rows` once to a temporary newline-delimited JSON file, working out each column's type on the way.
    Returns a Staged; the caller removes `staged.path` (os.unlink) when done. `track`, a column name, also finds the
    largest non-null value in that column (an export's incremental watermark)."""
    kinds = [set() for _ in columns]
    scales = [0] * len(columns)
    count = 0
    if track is not None and track not in columns:
        raise ApiError(f"The incremental column '{track}' isn't in the query's result (columns: "
                       f"{', '.join(columns)})", code='invalid_watermark')
    tracked = columns.index(track) if track is not None else None
    largest = None
    fd, staging = tempfile.mkstemp(suffix='.ndjson', prefix='qag-stage-')
    try:
        with os.fdopen(fd, 'w') as out:
            for row in rows:
                record = []
                for i, value in enumerate(row):
                    if value is not None:
                        kind = _kind(value)
                        kinds[i].add(kind)
                        if kind == 'decimal':
                            scales[i] = max(scales[i], max(0, -value.as_tuple().exponent))
                    record.append(_encode(value))
                if tracked is not None and row[tracked] is not None:
                    value = row[tracked]
                    try:
                        if largest is None or value > largest:
                            largest = value
                    except TypeError:
                        raise ApiError(f"Column '{track}' mixes values that can't be compared ({largest!r}, "
                                       f'{value!r}) - it can\'t be an incremental column', code='invalid_watermark') \
                            from None
                out.write(json.dumps({'r': record}) + '\n')
                count += 1
    except BaseException:
        os.unlink(staging)
        raise
    types = [_column_type(k, s) for k, s in zip(kinds, scales, strict=False)]
    return Staged(staging, columns, types, count, largest)


class Staged:
    """Rows staged for DuckDB: the file, the columns and their types, how many rows, and the tracked maximum."""

    def __init__(self, path, columns, types, count, largest):
        self.path, self.columns, self.types, self.count, self.largest = path, columns, types, count, largest


# One thread: DuckDB's COPY from the staged file then holds a steady ~150 MB, whatever the size - with its default
# (one thread per core) it buffered every thread's share, and memory grew with the result (about 600 MB for 2 million
# rows, 1.4 GB for 6 million). One thread also keeps the rows in the query's order.
WRITER_SETTINGS = ('SET threads = 1',)

COPY_FORMATS = {'parquet': '(FORMAT parquet)', 'csv': '(FORMAT csv, HEADER true)', 'ndjson': '(FORMAT json)'}


def copy(conn, staged, target, fmt='parquet'):
    """Have DuckDB write the staged rows to `target` (a local path, or an object-storage URL `conn` may write to) as
    Parquet, CSV or NDJSON."""
    options = COPY_FORMATS[fmt]
    if staged.count:
        # Each line holds the row as a JSON array; columns are picked out by position, so names (duplicates, odd
        # characters) never have to survive as JSON keys.
        source = f"read_json({_quote(staged.path)}, format='newline_delimited', columns={{'r': 'JSON'}})"
        conn.execute(f'COPY (SELECT {_json_select(staged.columns, staged.types)} FROM {source}) TO {_quote(target)} '
                     f'{options}')
    else:
        empty = ', '.join(f'NULL::{t} AS "{n.replace(chr(34), chr(34) * 2)}"'
                          for n, t in zip(staged.columns, staged.types, strict=False))
        conn.execute(f'COPY (SELECT {empty} WHERE false) TO {_quote(target)} {options}')


def write(columns, rows, path):
    """Write `rows` (any iterable of sequences) under `columns` to a Parquet file at `path`. Returns the row count."""
    _require()
    import duckdb
    staged = stage(columns, rows)
    try:
        conn = duckdb.connect()
        try:
            for setting in WRITER_SETTINGS:
                conn.execute(setting)
            copy(conn, staged, path)
        finally:
            conn.close()
    finally:
        os.unlink(staged.path)
    return staged.count


def _json_select(columns, types):
    """Each output column from the line's JSON array, by position, cast to its type."""
    out = []
    for i, (name, kind) in enumerate(zip(columns, types, strict=False)):
        label = name.replace('"', '""')
        if kind == 'VARCHAR':  # a string as itself, anything else (a number, an object) as its JSON text
            value = f"CASE json_type(r, '$[{i}]') WHEN 'NULL' THEN NULL " \
                    f"WHEN 'VARCHAR' THEN json_extract_string(r, '$[{i}]') " \
                    f"ELSE CAST(json_extract(r, '$[{i}]') AS VARCHAR) END"
        else:
            value = f"CAST(json_extract_string(r, '$[{i}]') AS {kind})"
        out.append(f'{value} AS "{label}"')
    return ', '.join(out)


def to_bytes(columns, rows):
    """The Parquet file for a (page of a) result, as bytes."""
    fd, path = tempfile.mkstemp(suffix='.parquet', prefix='qag-parquet-')
    os.close(fd)
    try:
        write(columns, rows, path)
        with open(path, 'rb') as f:
            return f.read()
    finally:
        os.unlink(path)


def stream(columns, rows, chunk_size=1 << 16):
    """Parquet as chunks for a streamed HTTP response: the file is written first (a Parquet footer comes last), then
    sent in pieces and removed."""
    fd, path = tempfile.mkstemp(suffix='.parquet', prefix='qag-parquet-')
    os.close(fd)
    try:
        write(columns, rows, path)
        with open(path, 'rb') as f:
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    return
                yield chunk
    finally:
        os.unlink(path)

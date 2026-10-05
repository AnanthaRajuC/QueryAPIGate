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


def write(columns, rows, path):
    """Write `rows` (any iterable of sequences) under `columns` to a Parquet file at `path`. Returns the row count."""
    _require()
    import duckdb
    kinds = [set() for _ in columns]
    scales = [0] * len(columns)
    count = 0
    fd, staging = tempfile.mkstemp(suffix='.ndjson', prefix='qag-parquet-')
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
                out.write(json.dumps({'r': record}) + '\n')
                count += 1
        types = [_column_type(k, s) for k, s in zip(kinds, scales, strict=False)]
        conn = duckdb.connect()
        try:
            if count:
                # Each line holds the row as a JSON array; columns are picked out by position, so names (duplicates,
                # odd characters) never have to survive as JSON keys.
                source = f"read_json({_quote(staging)}, format='newline_delimited', columns={{'r': 'JSON'}})"
                conn.execute(f'COPY (SELECT {_json_select(columns, types)} FROM {source}) TO {_quote(path)} '
                             '(FORMAT parquet)')
            else:
                empty = ', '.join(f'NULL::{t} AS "{n.replace(chr(34), chr(34) * 2)}"'
                                  for n, t in zip(columns, types, strict=False))
                conn.execute(f'COPY (SELECT {empty} WHERE false) TO {_quote(path)} (FORMAT parquet)')
        finally:
            conn.close()
    finally:
        os.unlink(staging)
    return count


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

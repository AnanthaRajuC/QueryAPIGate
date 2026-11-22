"""Result sets and their output formats (JSON, NDJSON, CSV, TSV, XML, YAML, XLSX, Parquet).

How a value is written (rc1, B6 - each lost information before):
- a timestamp with a zone (TIMESTAMPTZ) keeps its offset and fraction - in JSON, ISO 8601:
  `2026-11-22T12:03:10.123456+05:30`; one without stays `2026-11-22 12:03:10`, with `.ffffff` when it has a fraction;
- a DECIMAL is its exact digits - in JSON, a number written as the database gave it (`1234567890.123456789`), not a
  float's nearest; NaN and infinities, which JSON has no number for, as strings;
- binary (BLOB, BYTEA, VARBINARY) is base64 - before, its bytes read as UTF-8, with every invalid byte lost.
"""
import base64
import csv
import json
import re
import secrets
import xml.etree.ElementTree as ET
from datetime import date, datetime, time
from decimal import Decimal
from io import BytesIO, StringIO

import yaml
from flask import Response
from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE

from . import parquet
from .errors import ApiError


def timestamp_text(value):
    """A datetime as JSON shows it: ISO 8601 with its offset and fraction when it has a zone, else
    'YYYY-MM-DD HH:MM:SS[.ffffff]'."""
    return value.isoformat() if value.tzinfo is not None else value.isoformat(sep=' ')


def json_default(obj):
    """``default=`` hook for json.dumps covering the types database drivers return. A Decimal here is a float - the
    exact number is for result rows (dumps_rows()); this is for everything else (an export's watermark, a cached
    value)."""
    if isinstance(obj, datetime):
        return timestamp_text(obj)
    if isinstance(obj, (date, time)):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, (bytes, bytearray, memoryview)):
        return base64.b64encode(bytes(obj)).decode('ascii')
    return str(obj)


# A Decimal in a result row, written as the exact number it is: json.dumps can only write a float, so each is dumped as
# a string marked with a token drawn for this response - which no value in the data can know - and the quotes taken off
# after. str() of a finite Decimal is always a JSON number ('-12.50', '1E+2', '1.5E-7'); only those are marked.


def dumps_rows(value):
    """json.dumps() of result rows as the API writes them (compact, ASCII): Decimals as exact numbers."""
    token = []

    def default(obj):
        if isinstance(obj, Decimal):
            text = str(obj)
            if not obj.is_finite():
                return text  # NaN, Infinity: JSON has no number for them
            if not token:
                token.append(secrets.token_hex(8))
            return f'{token[0]}{text}{token[0]}'
        return json_default(obj)

    text = json.dumps(value, default=default, separators=(',', ':'))
    if not token:
        return text
    return re.sub(f'"{token[0]}(-?[0-9][0-9.eE+-]*){token[0]}"', r'\1', text)


def _unique(names):
    """Disambiguate repeated column names (e.g. ``SELECT a.id, b.id``) so no data is lost."""
    seen, result = {}, []
    for name in names:
        name = str(name)
        seen[name] = seen.get(name, 0) + 1
        result.append(name if seen[name] == 1 else f'{name}_{seen[name]}')
    return result


def _cell(value):
    """Coerce a value into plain built-in types that CSV, YAML and Excel writers all accept.

    Some drivers (e.g. H2 through JPype) return subclasses of int/float/str, which PyYAML's safe
    dumper rejects because it only recognises the exact built-in types.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return bool(value)
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return float(value)
    if isinstance(value, str):
        return str(value)
    if isinstance(value, Decimal):
        return value  # exact: CSV and XML write str() of it, JSON its digits (dumps_rows()), YAML a number
    if isinstance(value, (bytes, bytearray, memoryview)):
        return base64.b64encode(bytes(value)).decode('ascii')
    if isinstance(value, (datetime, date, time)):
        return value  # a zone kept: CSV and XML write str() of it, JSON timestamp_text()
    if isinstance(value, (dict, list)):
        # A Mongo document's nested subdocuments/arrays - JSON-encoded rather than left as a Python repr
        # (which str() would produce and is not valid JSON), even though the exporter that ultimately uses
        # this value may itself be JSON (nested JSON-in-a-string) or a flat format (CSV/XLSX) that has no
        # better way to show a nested structure in one cell.
        return json.dumps(value, default=json_default)
    return str(value)


def _xml_name(name):
    name = re.sub(r'[^\w.\-]', '_', name)
    return name if re.match(r'[A-Za-z_]', name) else f'_{name}'


class ResultSetDTO:
    """A page of query results (column names + rows) with output-format converters."""

    def __init__(self, rows, columns, has_more=False):
        self.columns = _unique(columns)
        self.raw_rows = rows  # as the driver returned them - Parquet keeps their types (Decimal, time zones)
        self.rows = [[_cell(v) for v in row] for row in rows]
        self.has_more = has_more

    def __bool__(self):
        return bool(self.rows)

    def as_dicts(self):
        return [dict(zip(self.columns, row, strict=False)) for row in self.rows]

    def _to_delimited(self, delimiter, mimetype):
        data_io = StringIO()
        writer = csv.writer(data_io, delimiter=delimiter)
        if self.columns:  # none - a write, a Mongo find that matched nothing - is an empty body, not a blank line
            writer.writerow(self.columns)
        writer.writerows(self.rows)
        return Response(data_io.getvalue(), mimetype=mimetype)

    def to_csv(self):
        return self._to_delimited(',', 'text/csv')

    def to_tsv(self):
        return self._to_delimited('\t', 'text/tab-separated-values')

    def to_json(self):
        return Response(dumps_rows(self.as_dicts()) + '\n', mimetype='application/json')

    def to_ndjson(self):
        lines = [dumps_rows(row) + '\n' for row in self.as_dicts()]
        return Response(''.join(lines), mimetype='application/x-ndjson')  # no rows: an empty body

    def to_xml(self):
        root = ET.Element('data')
        tags = [_xml_name(c) for c in self.columns]
        for row in self.rows:
            item = ET.SubElement(root, 'item')
            for tag, value in zip(tags, row, strict=False):
                ET.SubElement(item, tag).text = '' if value is None else str(value)
        return Response(ET.tostring(root, encoding='unicode', method='xml'), mimetype='application/xml')

    def to_yaml(self):
        # PyYAML's safe dumper has dates and datetimes but no time of day: a TIME column was a 500. As text, the
        # same ISO form JSON gives it.
        rows = [{k: v.isoformat() if isinstance(v, time) else v for k, v in row.items()} for row in self.as_dicts()]
        return Response(yaml.dump(rows, Dumper=_YamlDumper, default_flow_style=False, sort_keys=False,
                                  allow_unicode=True), mimetype='application/x-yaml')

    def to_xlsx(self):
        wb = Workbook()
        ws = wb.active
        ws.append(self.columns)
        for row in self.rows:
            ws.append([_xlsx_cell(v) for v in row])
        excel_data = BytesIO()
        wb.save(excel_data)
        return Response(
            excel_data.getvalue(),
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            headers={'Content-Disposition': 'attachment;filename=result.xlsx'},
        )

    def to_parquet(self):
        if not self.columns:
            # A Parquet file has a schema, and a result with no columns - a write, a Mongo find that matched nothing -
            # has none to give it.
            raise ApiError('This result has no columns, so there is no Parquet file to write - ask for json, ndjson '
                           'or csv', code='invalid_format')
        return Response(parquet.to_bytes(self.columns, self.raw_rows), mimetype=parquet.MIMETYPE,
                        headers={'Content-Disposition': 'attachment;filename=result.parquet'})


class _YamlDumper(yaml.SafeDumper):
    """safe_dump's, with a Decimal as the exact number it is."""


def _yaml_decimal(dumper, value):
    text = str(value)
    if value.is_finite():
        return dumper.represent_scalar('tag:yaml.org,2002:float' if '.' in text or 'E' in text
                                       else 'tag:yaml.org,2002:int', text)
    return dumper.represent_str(text)


_YamlDumper.add_representer(Decimal, _yaml_decimal)


def _xlsx_cell(value):
    """A sheet has no time zones - a timestamp's wall time, as before - and only plain strings."""
    if isinstance(value, str):
        return ILLEGAL_CHARACTERS_RE.sub('', value)
    if isinstance(value, (datetime, time)) and value.tzinfo is not None:
        return value.replace(tzinfo=None)
    return value


FORMATTERS = {
    'json': ResultSetDTO.to_json,
    'ndjson': ResultSetDTO.to_ndjson,
    'csv': ResultSetDTO.to_csv,
    'tsv': ResultSetDTO.to_tsv,
    'xml': ResultSetDTO.to_xml,
    'yaml': ResultSetDTO.to_yaml,
    'xlsx': ResultSetDTO.to_xlsx,
    'parquet': ResultSetDTO.to_parquet,
}

# --------------------------------------------------------------------------------------
# Streaming: unlike ResultSetDTO above, these never hold more than one row (or one small
# write buffer) in memory at a time - `rows` is a generator pulling batches from a live
# database cursor, not a pre-materialised list. Only CSV and NDJSON are genuinely streamable
# text formats; XML/YAML/XLSX all need the whole document structure in memory to write
# correctly (a closing root tag, a single top-level list, a zip's central directory), so
# they stay page-at-a-time via ResultSetDTO/FORMATTERS above.

STREAM_FORMATTERS = frozenset({'csv', 'tsv', 'ndjson', 'parquet'})
STREAM_MIMETYPES = {'csv': 'text/csv', 'tsv': 'text/tab-separated-values', 'ndjson': 'application/x-ndjson',
                    'parquet': parquet.MIMETYPE}
STREAM_DELIMITERS = {'csv': ',', 'tsv': '\t'}


def _stream_delimited(columns, rows, delimiter):
    buf = StringIO()
    writer = csv.writer(buf, delimiter=delimiter)
    writer.writerow(columns)
    yield buf.getvalue()
    for row in rows:
        buf.seek(0)
        buf.truncate(0)
        writer.writerow([_cell(v) for v in row])
        yield buf.getvalue()


def _stream_ndjson(columns, rows):
    for row in rows:
        yield dumps_rows(dict(zip(columns, (_cell(v) for v in row), strict=False))) + '\n'


def iter_stream_chunks(output_format, columns, rows):
    """The text chunks for ``output_format`` in ``STREAM_FORMATTERS``, generated from ``rows`` as they
    arrive rather than built up front - shared by stream_response() (an HTTP response body) and the
    ``queryapigate export`` CLI command (written straight to a file), so there is exactly one place that knows
    how to turn a row stream into CSV/TSV/NDJSON text."""
    columns = _unique(columns)
    if output_format == 'parquet':  # bytes, not text: the file is written by DuckDB first (its footer comes last)
        return parquet.stream(columns, rows)
    if output_format == 'ndjson':
        return _stream_ndjson(columns, rows)
    return _stream_delimited(columns, rows, STREAM_DELIMITERS[output_format])


def stream_response(output_format, columns, rows, filename):
    """A chunked ``Response`` for ``output_format`` in ``STREAM_FORMATTERS``, generated from ``rows`` as
    they arrive rather than built up front - the point being that a result far larger than fits in memory
    can still be exported, at the cost of the usual conveniences a fully-buffered response gets for free
    (a reliable Content-Length, the ability to retry a failed write, an ETag for caching)."""
    return chunked_response(output_format, iter_stream_chunks(output_format, columns, rows), filename)


def chunked_response(output_format, body, filename):
    """The Response for chunks of `output_format` already made - stream_response()'s, and a MongoDB stream's
    (mongodocs.chunks())."""
    response = Response(body, mimetype=STREAM_MIMETYPES[output_format])
    response.headers['Content-Disposition'] = f'attachment; filename="{filename}.{output_format}"'
    return response

"""Result sets and their output formats (JSON, NDJSON, CSV, TSV, XML, YAML, XLSX)."""
import csv
import json
import re
import xml.etree.ElementTree as ET
from datetime import date, datetime, time
from decimal import Decimal
from io import BytesIO, StringIO

import yaml
from flask import Response, jsonify
from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE


def json_default(obj):
    """``default=`` hook for json.dumps covering the types database drivers return."""
    if isinstance(obj, datetime):
        return obj.strftime('%Y-%m-%d %H:%M:%S')
    if isinstance(obj, (date, time)):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, (bytes, bytearray, memoryview)):
        return bytes(obj).decode('utf-8', 'replace')
    return str(obj)


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
        return float(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).decode('utf-8', 'replace')
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    if isinstance(value, (date, time)):
        return value
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
        self.rows = [[_cell(v) for v in row] for row in rows]
        self.has_more = has_more

    def __bool__(self):
        return bool(self.rows)

    def as_dicts(self):
        return [dict(zip(self.columns, row, strict=False)) for row in self.rows]

    def _to_delimited(self, delimiter, mimetype):
        data_io = StringIO()
        writer = csv.writer(data_io, delimiter=delimiter)
        writer.writerow(self.columns)
        writer.writerows(self.rows)
        return Response(data_io.getvalue(), mimetype=mimetype)

    def to_csv(self):
        return self._to_delimited(',', 'text/csv')

    def to_tsv(self):
        return self._to_delimited('\t', 'text/tab-separated-values')

    def to_json(self):
        return jsonify(self.as_dicts())

    def to_ndjson(self):
        lines = (json.dumps(row, default=json_default) for row in self.as_dicts())
        return Response('\n'.join(lines) + '\n', mimetype='application/x-ndjson')

    def to_xml(self):
        root = ET.Element('data')
        tags = [_xml_name(c) for c in self.columns]
        for row in self.rows:
            item = ET.SubElement(root, 'item')
            for tag, value in zip(tags, row, strict=False):
                ET.SubElement(item, tag).text = '' if value is None else str(value)
        return Response(ET.tostring(root, encoding='unicode', method='xml'), mimetype='application/xml')

    def to_yaml(self):
        return Response(yaml.safe_dump(self.as_dicts(), default_flow_style=False, sort_keys=False,
                                       allow_unicode=True),
                        mimetype='application/x-yaml')

    def to_xlsx(self):
        wb = Workbook()
        ws = wb.active
        ws.append(self.columns)
        for row in self.rows:
            ws.append([ILLEGAL_CHARACTERS_RE.sub('', v) if isinstance(v, str) else v for v in row])
        excel_data = BytesIO()
        wb.save(excel_data)
        return Response(
            excel_data.getvalue(),
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            headers={'Content-Disposition': 'attachment;filename=result.xlsx'},
        )


FORMATTERS = {
    'json': ResultSetDTO.to_json,
    'ndjson': ResultSetDTO.to_ndjson,
    'csv': ResultSetDTO.to_csv,
    'tsv': ResultSetDTO.to_tsv,
    'xml': ResultSetDTO.to_xml,
    'yaml': ResultSetDTO.to_yaml,
    'xlsx': ResultSetDTO.to_xlsx,
}

# --------------------------------------------------------------------------------------
# Streaming: unlike ResultSetDTO above, these never hold more than one row (or one small
# write buffer) in memory at a time - `rows` is a generator pulling batches from a live
# database cursor, not a pre-materialised list. Only CSV and NDJSON are genuinely streamable
# text formats; XML/YAML/XLSX all need the whole document structure in memory to write
# correctly (a closing root tag, a single top-level list, a zip's central directory), so
# they stay page-at-a-time via ResultSetDTO/FORMATTERS above.

STREAM_FORMATTERS = frozenset({'csv', 'tsv', 'ndjson'})
STREAM_MIMETYPES = {'csv': 'text/csv', 'tsv': 'text/tab-separated-values', 'ndjson': 'application/x-ndjson'}
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
        yield json.dumps(dict(zip(columns, (_cell(v) for v in row), strict=False)), default=json_default) + '\n'


def iter_stream_chunks(output_format, columns, rows):
    """The text chunks for ``output_format`` in ``STREAM_FORMATTERS``, generated from ``rows`` as they
    arrive rather than built up front - shared by stream_response() (an HTTP response body) and the
    ``queryapigate export`` CLI command (written straight to a file), so there is exactly one place that knows
    how to turn a row stream into CSV/TSV/NDJSON text."""
    columns = _unique(columns)
    if output_format == 'ndjson':
        return _stream_ndjson(columns, rows)
    return _stream_delimited(columns, rows, STREAM_DELIMITERS[output_format])


def stream_response(output_format, columns, rows, filename):
    """A chunked ``Response`` for ``output_format`` in ``STREAM_FORMATTERS``, generated from ``rows`` as
    they arrive rather than built up front - the point being that a result far larger than fits in memory
    can still be exported, at the cost of the usual conveniences a fully-buffered response gets for free
    (a reliable Content-Length, the ability to retry a failed write, an ETag for caching)."""
    body = iter_stream_chunks(output_format, columns, rows)
    response = Response(body, mimetype=STREAM_MIMETYPES[output_format])
    response.headers['Content-Disposition'] = f'attachment; filename="{filename}.{output_format}"'
    return response

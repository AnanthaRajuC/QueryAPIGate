"""MongoDB documents as a streamed export (ADR 0022).

A SQL stream knows its columns before its first row; documents don't share a schema, so each streamed format decides
what that means for it:

- NDJSON writes each document whole - nested documents and arrays stay nested, no field is ever dropped.
- Parquet (and anything staged before it's written - an export to a destination) takes every field of every document:
  the columns grow as fields are first seen, and parquet.stage() reads the earlier rows' missing ones back as null.
- CSV and TSV send their header first, so the columns are decided before the first row: an inclusion projection's
  fields, in its order (`_id` first unless it sets `"_id": 0`), or else the union of the first LOOKAHEAD documents'
  fields. A field first seen after that has no column; it's left out and counted (`dropped`).

Nested values in a column are JSON text, as in the paged formats (formats._cell).
"""
import csv
import json
import logging
from collections import Counter, namedtuple
from io import StringIO
from itertools import islice

from . import parquet
from .formats import STREAM_DELIMITERS, _cell, json_default

log = logging.getLogger('queryapigate')

LOOKAHEAD = 1000  # documents read for CSV's columns when no projection gives them - part of ADR 0022's contract

# A saved `mongo` query, its parameters filled in, ready to stream - what exports.prepare() returns in a SQL query's
# place.
Find = namedtuple('Find', 'collection filter projection sort')


def _includes(value):
    """A projection value that includes its field: 1 or true. 0 or false excludes; anything else is an expression."""
    return isinstance(value, (bool, int, float)) and bool(value)


def projected_columns(projection):
    """The columns an inclusion projection fixes, in its order - `_id` first unless it's excluded - or None when the
    projection doesn't fix them (none, exclusion only, or one with expressions)."""
    if not isinstance(projection, dict):
        return None
    fields = [name for name in projection if name != '_id']
    if not fields or not all(_includes(projection[name]) for name in fields):
        return None
    keep_id = '_id' not in projection or _includes(projection['_id'])
    return (['_id'] if keep_id else []) + fields


def _value(doc, column):
    """A column's value in `doc`: a field, or - for a projected `a.b` - the nested one. None if it isn't there."""
    if column in doc:
        return doc[column]
    value = doc
    for part in column.split('.'):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


class Documents:
    """Documents as rows under `columns`. With `grow`, every field is a column, added as it's first seen (the same
    list object grows, so parquet.stage() can be handed it before the first row). Otherwise the columns are fixed
    by settle() - from `projection`, or from the first `lookahead` documents - and fields outside them are counted
    in `dropped`."""

    def __init__(self, docs, projection=None, grow=False, lookahead=None):
        self._docs = iter(docs)
        self._source = docs
        self._projection = projection
        self._grow = grow
        self._lookahead = LOOKAHEAD if lookahead is None else lookahead
        self._buffered = None
        self._covered = set()
        self.columns = []
        self.dropped = Counter()  # field -> documents it was left out of

    def settle(self):
        """Decide the columns, reading the first `lookahead` documents if that's what it takes. Returns them."""
        if self._buffered is not None:
            return self.columns
        fixed = None if self._grow else projected_columns(self._projection)
        if fixed is not None or self._grow:
            self._buffered = []
            self.columns.extend(fixed or [])
        else:
            self._buffered = list(islice(self._docs, self._lookahead))
            self.columns.extend(dict.fromkeys(name for doc in self._buffered for name in doc))
        # a top-level field is covered by its own column, or by a projected `field.nested` one
        self._covered = set(self.columns) | {column.split('.', 1)[0] for column in self.columns}
        return self.columns

    def __iter__(self):
        self.settle()
        buffered, self._buffered = self._buffered, []
        for doc in buffered:
            yield self._row(doc)
        del buffered
        for doc in self._docs:
            yield self._row(doc)
        if not self.columns:  # nothing at all came back: still a valid file, with the one field every document has
            self.columns.extend(projected_columns(self._projection) or ['_id'])
        if self.dropped:
            names = sorted(self.dropped)
            log.warning('A streamed find left out %s field%s first seen after its columns were decided: %s',
                        len(names), '' if len(names) == 1 else 's',
                        ', '.join(names[:10]) + (', ...' if len(names) > 10 else ''),
                        extra={'fields_dropped': len(names)})

    def _row(self, doc):
        if self._grow:
            for name in doc:
                if name not in self._covered:
                    self._covered.add(name)
                    self.columns.append(name)
            return [doc.get(column) for column in self.columns]
        for name in doc:
            if name not in self._covered:
                self.dropped[name] += 1
        return [_value(doc, column) for column in self.columns]

    def report(self):
        """What a run's history entry says about the shaping: how many fields were left out, if any."""
        return {'fields_dropped': len(self.dropped)} if self.dropped else {}

    def close(self):
        close = getattr(self._source, 'close', None)
        if close is not None:
            close()


def _ndjson(docs):
    for doc in docs:
        yield json.dumps(doc, default=json_default) + '\n'


def _delimited(shaped, delimiter):
    buf = StringIO()
    writer = csv.writer(buf, delimiter=delimiter)
    writer.writerow(shaped.settle())
    yield buf.getvalue()
    for row in shaped:
        buf.seek(0)
        buf.truncate(0)
        writer.writerow([_cell(value) for value in row])
        yield buf.getvalue()


def chunks(output_format, docs, projection=None):
    """(body, shaped): the chunks of `output_format` (formats.STREAM_FORMATTERS) for `docs` as they arrive - text,
    or bytes for Parquet - and the Documents that shaped them (None for NDJSON, which needs no columns), whose
    report() is read once the body is exhausted."""
    if output_format == 'ndjson':
        return _ndjson(docs), None
    if output_format == 'parquet':
        shaped = Documents(docs, grow=True)
        return parquet.stream(shaped.columns, shaped), shaped
    shaped = Documents(docs, projection)
    return _delimited(shaped, STREAM_DELIMITERS[output_format]), shaped

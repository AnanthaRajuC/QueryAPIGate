"""Best-effort table/join extraction from a saved query's SQL, via `sqlglot`.

This is purely for the Access tab's "Query flow" visualization - it is never consulted by
sqltools.validate_sql or anything on the execution path, and it must never raise: a parse
failure just means the visualization shows nothing for that query, not a broken screen.
"""
from . import sqltools

# sqlglot's dialect names for the dialects this project can meaningfully parse. H2 and JDBC
# connections can point at any vendor's SQL over a generic bridge - there is no "generic JDBC"
# sqlglot dialect to guess at, so those (and mongo, which has no SQL at all) are left out on
# purpose, not as a gap to fill later.
_DIALECT_MAP = {'mysql': 'mysql', 'postgres': 'postgres', 'clickhouse': 'clickhouse',
                'sqlite': 'sqlite', 'duckdb': 'duckdb'}


def _substitute_placeholders(sql, dialect):
    """Replace this project's own `:name`/`{name}` parameter markers with a harmless literal
    before handing the text to a strict parser - only the query's structure matters here, not
    the values, and sqlglot does not understand either marker convention."""
    sql = sqltools._param_re(dialect).sub(lambda m: '1' if m.group('name') else m.group(0), sql)
    return sqltools._BRACE_RE.sub('1', sql)


def extract_flow(sql, dialect):
    """Return {'tables': [name, ...], 'joins': [{'left', 'right', 'type', 'on'}, ...], 'error'}.

    `error` is set (and tables/joins are empty) whenever real analysis isn't possible - an
    unsupported dialect, sqlglot missing, or a query sqlglot can't parse - and is meant to be
    shown to the user as a plain explanatory line, never as a failure.
    """
    if dialect not in _DIALECT_MAP:
        return {'tables': [], 'joins': [], 'error': f"SQL analysis isn't available for '{dialect}' connections"}
    try:
        import sqlglot
        from sqlglot import exp
    except ImportError:
        return {'tables': [], 'joins': [], 'error': 'sqlglot is not installed'}

    sqlglot_dialect = _DIALECT_MAP[dialect]
    try:
        parsed = sqlglot.parse_one(_substitute_placeholders(sql, dialect), read=sqlglot_dialect)
    except Exception:
        return {'tables': [], 'joins': [], 'error': 'Could not analyze this query'}

    tables = []
    seen = set()
    for table in parsed.find_all(exp.Table):
        name = table.name
        if name and name not in seen:
            seen.add(name)
            tables.append(name)

    def table_name(node):
        return node.name if isinstance(node, exp.Table) else node.alias_or_name

    joins = []
    for select in parsed.find_all(exp.Select):
        from_ = select.args.get('from')
        prev = table_name(from_.this) if from_ else None
        for join in select.args.get('joins') or []:
            right = table_name(join.this)
            on = join.args.get('on')
            kind = (join.kind or join.side or '').upper()
            joins.append({'left': prev, 'right': right, 'type': (kind + ' JOIN').strip(),
                          'on': on.sql(dialect=sqlglot_dialect)[:200] if on else ''})
            prev = right

    return {'tables': tables, 'joins': joins, 'error': None}

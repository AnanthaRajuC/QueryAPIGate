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


def pretty_print(sql, dialect):
    """A real, sqlglot-formatted rendering of `sql`, or None when that isn't possible - an
    unsupported dialect, a legacy `{name}` placeholder (sqlglot has no notion of this project's
    own text-substitution convention and misparses it as a struct literal - a bound `:name`
    parameter needs no such care, since sqlglot parses and round-trips it natively), or a query
    sqlglot can't parse or print. Never raises."""
    if dialect not in _DIALECT_MAP or sqltools._BRACE_RE.search(sql):
        return None
    try:
        import sqlglot
    except ImportError:
        return None
    sqlglot_dialect = _DIALECT_MAP[dialect]
    try:
        return sqlglot.parse_one(sql, read=sqlglot_dialect).sql(dialect=sqlglot_dialect, pretty=True)
    except Exception:
        return None


def extract_flow(sql, dialect):
    """Return {'tables': [name, ...], 'joins': [{'left', 'right', 'type', 'on'}, ...],
    'formatted', 'error'}.

    `error` is set (and tables/joins/formatted are empty/None) whenever real analysis isn't
    possible - an unsupported dialect, sqlglot missing, or a query sqlglot can't parse - and is
    meant to be shown to the user as a plain explanatory line, never as a failure. `formatted` can
    independently be None even when tables/joins succeeded - see pretty_print().
    """
    if dialect not in _DIALECT_MAP:
        return {'tables': [], 'joins': [], 'formatted': None,
                'error': f"SQL analysis isn't available for '{dialect}' connections"}
    try:
        import sqlglot
        from sqlglot import exp
    except ImportError:
        return {'tables': [], 'joins': [], 'formatted': None, 'error': 'sqlglot is not installed'}

    sqlglot_dialect = _DIALECT_MAP[dialect]
    try:
        parsed = sqlglot.parse_one(sqltools.substitute_placeholders(sql, dialect), read=sqlglot_dialect)
    except Exception:
        return {'tables': [], 'joins': [], 'formatted': None, 'error': 'Could not analyze this query'}

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

    return {'tables': tables, 'joins': joins, 'formatted': pretty_print(sql, dialect), 'error': None}

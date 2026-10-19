"""Table-level SQL allow-listing (``allowed_tables`` - see apikeys.py's module docstring), for the five
dialects sqlglot can parse (see ``_DIALECT_MAP``). Unlike sqlflow.py's best-effort, never-raises table
extraction for the Access tab's visualization, this one is a security check: a query whose tables can't be
verified - an unsupported dialect, a parse failure - must be rejected, not silently let through.

Extraction walks the parsed statement keeping track of which CTEs are visible where (``_real_tables``), not
``sqlglot.optimizer.scope.build_scope()`` - that returns None entirely for DELETE/UPDATE/INSERT (verified against a
real sqlglot install), which would silently miss a write statement's own target table. A bare name refers to a CTE
only where SQL says it does: in the statement after its WITH, in the CTEs defined after it - and in its own body
only under WITH RECURSIVE. Anywhere else, and always when qualified (``public.secret``), it's the table: dropping
every table that merely shared a CTE's name let `WITH secret AS (SELECT * FROM secret) SELECT * FROM secret` past a
key allowed only `orders` (fixed 2026-10-15).

Known, accepted limitations: for most dialects a table name is extracted bare (``information_schema.tables`` ->
``'tables'``), not schema-qualified - matches allowed_write_ops's own bare-keyword-list style, but can't
disambiguate two identically-named tables in different schemas. **Trino is the exception** (ADR 0026): because one
Trino connection federates many catalogs, its names are extracted **fully qualified** (``catalog.schema.table``),
filling a reference's missing parts from the connection's defaults, and grants are qualified the same way
(``qualify_grant``) - so a grant of ``orders`` does not match ``orders`` in every catalog. A table-valued function
names no table, so one that reads data -
DuckDB's ``read_parquet(...)``, ``read_csv(...)``, ``glob(...)``, a ClickHouse ``url(...)`` - is refused outright
for a table-restricted key; only pure generators (``range``, ``numbers``, ``generate_series``, ``unnest``) pass.
"""
from . import sqltools
from .errors import ApiError

_DIALECT_MAP = {'mysql': 'mysql', 'postgres': 'postgres', 'clickhouse': 'clickhouse',
                'sqlite': 'sqlite', 'duckdb': 'duckdb', 'trino': 'trino'}  # kept in sync with sqlflow.py's own map


def extract_tables(sql, dialect, default_catalog=None, default_schema=None):
    """The real base tables ``sql`` touches, lowercased - or raises ApiError if that can't be determined for
    this dialect or this specific query. A failure here must reject the query, never degrade quietly the
    way sqlflow.py's own extraction does for its visualization - see the module docstring.

    For ``trino``, whose names are three-part, each table comes back **fully qualified** as
    ``catalog.schema.table``: the catalog and schema a reference leaves out are filled from the connection's
    defaults (``default_catalog``/``default_schema``), so `orders`, `sf1.orders` and `hive.sf1.orders` are
    distinguished rather than all collapsed to `orders`. For every other dialect, names are bare as before
    (``default_catalog``/``default_schema`` are ignored). Match the result against grants run through
    ``qualify_grant()`` with the same defaults."""
    if dialect not in _DIALECT_MAP:
        raise ApiError(f"Table access restrictions aren't supported for '{dialect}' connections yet", 403,
                       code='table_check_unsupported')
    try:
        import sqlglot
        from sqlglot import exp
    except ImportError:
        raise ApiError('Table access restrictions need the "sqlglot" package - '
                       'run `pip install "queryapigate[flow]"`', 500, code='table_check_unavailable') from None
    sqlglot_dialect = _DIALECT_MAP[dialect]
    try:
        parsed = sqlglot.parse_one(sqltools.substitute_placeholders(sql, dialect), read=sqlglot_dialect)
    except Exception:
        raise ApiError('This query could not be analyzed to enforce its table access restrictions', 403,
                       code='table_check_failed') from None
    for source in [*parsed.find_all(exp.Table), *parsed.find_all(exp.Lateral)]:
        function = source.this
        if isinstance(function, exp.Func) and not _is_generator(function):
            name = function.sql_name().lower() if not isinstance(function, exp.Anonymous) else function.name.lower()
            raise ApiError(f'This API key may only query its allowed tables - a table function such as {name}() '
                           "reads data the allow-list can't see, so it isn't allowed", 403, code='table_not_allowed')
    if dialect == 'trino':
        def qualify(node):  # fully-qualified catalog.schema.table, defaults filling what the reference omits
            catalog = (node.catalog or default_catalog or '').lower()
            schema = (node.db or default_schema or '').lower()
            return f'{catalog}.{schema}.{node.name.lower()}'
    else:
        def qualify(node):
            return node.name.lower()
    try:
        return _real_tables(parsed, frozenset(), qualify)
    except RecursionError:  # nested deeper than can be walked: refused, never let through
        raise ApiError('This query could not be analyzed to enforce its table access restrictions', 403,
                       code='table_check_failed') from None


def qualify_grant(name, default_catalog, default_schema):
    """A Trino ``allowed_tables`` grant - ``table``, ``schema.table`` or ``catalog.schema.table`` - filled to
    ``catalog.schema.table`` with the connection's defaults, so it matches extract_tables()'s qualified names."""
    parts = [p.lower() for p in name.split('.')]
    if len(parts) == 1:
        parts = [(default_catalog or '').lower(), (default_schema or '').lower(), parts[0]]
    elif len(parts) == 2:
        parts = [(default_catalog or '').lower(), *parts]
    return '.'.join(parts[-3:])


def _real_tables(node, visible, qualify):
    """The tables under ``node`` that aren't references to a CTE in ``visible`` (lower-case bare names) - see the
    module docstring for the scoping rule. ``qualify`` turns a real table node into the name it is matched by
    (bare, or fully-qualified for trino)."""
    from sqlglot import exp
    if isinstance(node, exp.Table):
        name = node.name.lower()  # the CTE-shadowing rule is on the bare name; a CTE is never catalog/schema-qualified
        if not name or (name in visible and not node.args.get('db') and not node.args.get('catalog')):
            return set()
        return {qualify(node)} | _children(node, visible, qualify)
    found = set()
    with_ = node.args.get('with') if isinstance(node, exp.Expression) else None
    if isinstance(with_, exp.With):
        ctes = [(cte.alias_or_name.lower(), cte.this) for cte in with_.expressions]
        if with_.args.get('recursive'):
            visible = visible | {name for name, _ in ctes}
            for _, body in ctes:
                found |= _real_tables(body, visible, qualify)
        else:
            for name, body in ctes:  # each body sees the CTEs before it, not itself or those after
                found |= _real_tables(body, visible, qualify)
                visible = visible | {name}
    return found | _children(node, visible, qualify, skip=with_)


def _children(node, visible, qualify, skip=None):
    found = set()
    for child in node.iter_expressions():
        if child is not skip:
            found |= _real_tables(child, visible, qualify)
    return found


# Table functions that only generate values - no data to restrict - and so stay usable with allowed_tables.
_GENERATORS = {'numbers', 'range', 'generate_series', 'system_range', 'unnest', 'generate_subscripts'}


def _is_generator(function):
    from sqlglot import exp
    # ExplodingGenerateSeries is newer than the oldest sqlglot supported (25.2) - looked up, not assumed
    generators = tuple(getattr(exp, name) for name in ('GenerateSeries', 'ExplodingGenerateSeries', 'Unnest')
                       if hasattr(exp, name))
    if isinstance(function, generators):
        return True
    return isinstance(function, exp.Anonymous) and function.name.lower() in _GENERATORS

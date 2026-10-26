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

Names are matched **qualified** (ADR 0026 for Trino; 0.43 for the rest): a reference's missing parts are filled from
the connection's defaults, and grants are qualified the same way (``qualify_grant``), so a grant of ``customer`` doesn't
match ``billing.customer``. Trino's names are ``catalog.schema.table``, since one connection federates many catalogs.
Every other dialect's are ``schema.table`` - the schema being MySQL's or ClickHouse's database, PostgreSQL's or
DuckDB's schema, SQLite's attached database - with a reference's catalog, where the dialect has one, kept as a third
part (``memory.main.orders``) so it only matches a grant naming it. What an unqualified name resolves to is the
connection's default (``engine._table_defaults``); on PostgreSQL, where that depends on ``search_path``, a
table-restricted call pins ``search_path`` to it (runners.py). Known, accepted limitation: names are compared
lower-cased, so on a server that treats case as significant (MySQL's ``lower_case_table_names=0``, a quoted
PostgreSQL ``"Customer"``) two tables differing only in case are one grant.

A table-valued function names no table, so one that reads data - DuckDB's ``read_parquet(...)``, ``read_csv(...)``,
``glob(...)``, a ClickHouse ``url(...)`` - is refused outright for a table-restricted key; only pure generators
(``range``, ``numbers``, ``generate_series``, ``unnest``) pass.
"""
import functools

from . import sqltools
from .errors import ApiError

_DIALECT_MAP = {'mysql': 'mysql', 'postgres': 'postgres', 'clickhouse': 'clickhouse',
                'sqlite': 'sqlite', 'duckdb': 'duckdb', 'trino': 'trino'}  # kept in sync with sqlflow.py's own map


def extract_tables(sql, dialect, default_catalog=None, default_schema=None):
    """The real base tables ``sql`` touches, lowercased - see _extract_tables(). Cached: the answer is a pure function
    of these four arguments, and a saved query's SQL never changes between calls, so parsing it again on every call
    of a table-restricted key was wasted work. A frozenset, so no caller can change the cached answer; a query that
    is refused isn't cached and raises again each time."""
    return _extract_tables(sql, dialect, default_catalog, default_schema)


@functools.lru_cache(maxsize=1024)  # bounded: varied ad-hoc SQL from a table-restricted key can't grow it
def _extract_tables(sql, dialect, default_catalog, default_schema):
    """The real base tables ``sql`` touches, lowercased - or raises ApiError if that can't be determined for
    this dialect or this specific query. A failure here must reject the query, never degrade quietly the
    way sqlflow.py's own extraction does for its visualization - see the module docstring.

    Each table comes back **qualified**, the parts a reference leaves out filled from the connection's defaults
    (``default_catalog``/``default_schema``): ``catalog.schema.table`` for ``trino``, so `orders`, `sf1.orders` and
    `hive.sf1.orders` are distinguished; ``schema.table`` for every other dialect, so `customer` (the connection's
    database or schema) and `billing.customer` are - with a reference's own catalog kept as a third part. Match the
    result against grants run through ``qualify_grant()`` with the same defaults and dialect."""
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

    def parse(text):
        try:
            return sqlglot.parse_one(text, read=sqlglot_dialect)
        except Exception:
            raise ApiError('This query could not be analyzed to enforce its table access restrictions', 403,
                           code='table_check_failed') from None
    text = sqltools.substitute_placeholders(sql, dialect)
    parsed = parse(text)
    if isinstance(parsed, exp.Command) and sqltools.first_keyword(text, dialect) == 'explain':
        parsed = parse(sqltools.explained_statement(text, dialect))  # what it plans (sqlglot can't parse EXPLAIN here)
    if not isinstance(parsed, _statements(exp)) or parsed.find(*_opaque(exp)):
        raise ApiError("This API key may only query its allowed tables - this kind of statement doesn't name the "
                       "tables it reads in a way that can be checked, so it isn't allowed", 403,
                       code='table_check_failed')
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
        def qualify(node):  # schema.table, the default schema filling a bare name; a stated catalog stays a part
            schema = f'{(node.db or default_schema or "").lower()}.{node.name.lower()}'
            return f'{node.catalog.lower()}.{schema}' if node.catalog else schema
    try:
        return frozenset(_real_tables(parsed, frozenset(), qualify))
    except RecursionError:  # nested deeper than can be walked: refused, never let through
        raise ApiError('This query could not be analyzed to enforce its table access restrictions', 403,
                       code='table_check_failed') from None


def qualify_grant(name, default_catalog, default_schema, dialect='trino'):
    """An ``allowed_tables`` grant qualified as extract_tables() qualifies a reference, so the two match: on Trino,
    ``table``, ``schema.table`` or ``catalog.schema.table`` filled to ``catalog.schema.table``; on every other dialect,
    a bare ``table`` filled to ``schema.table`` with the connection's default schema, and a qualified one kept."""
    parts = [p.lower() for p in name.split('.')]
    if dialect != 'trino':
        return '.'.join(parts) if len(parts) > 1 else f'{(default_schema or "").lower()}.{parts[0]}'
    if len(parts) == 1:
        parts = [(default_catalog or '').lower(), (default_schema or '').lower(), parts[0]]
    elif len(parts) == 2:
        parts = [(default_catalog or '').lower(), *parts]
    return '.'.join(parts[-3:])


def _real_tables(node, visible, qualify):
    """The tables under ``node`` that aren't references to a CTE in ``visible`` (lower-case bare names) - see the
    module docstring for the scoping rule. ``qualify`` turns a real table node into the name it is matched by
    (see _extract_tables())."""
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


def _statements(exp):
    """The statements whose tables can be read off their parse: queries and data changes, DDL naming its table, and
    MySQL's EXPLAIN/DESCRIBE of one. Any other root - SHOW CREATE TABLE, SET, USE, ATTACH, PRAGMA, CALL, PREPARE and
    EXECUTE, DECLARE and FETCH, or a statement sqlglot only keeps as text - can read or reroute tables the check can't
    see, so a table-restricted key may not run it. Names looked up: they differ across the sqlglot versions supported
    (AlterTable became Alter)."""
    names = ('Query', 'Values', 'Insert', 'Update', 'Delete', 'Merge', 'Copy', 'Describe', 'Create', 'Drop', 'Alter',
             'AlterTable', 'TruncateTable')
    return tuple(getattr(exp, name) for name in names if hasattr(exp, name))


def _opaque(exp):
    """Nodes that, anywhere in a statement, stand for something the check can't see into."""
    names = ('Command', 'Show', 'Set', 'Use', 'Pragma', 'Attach', 'Detach')
    return tuple(getattr(exp, name) for name in names if hasattr(exp, name))


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

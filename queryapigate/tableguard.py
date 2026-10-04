"""Table-level SQL allow-listing (``allowed_tables`` - see apikeys.py's module docstring), for the five
dialects sqlglot can parse (see ``_DIALECT_MAP``). Unlike sqlflow.py's best-effort, never-raises table
extraction for the Access tab's visualization, this one is a security check: a query whose tables can't be
verified - an unsupported dialect, a parse failure - must be rejected, not silently let through.

Extraction is ``parsed.find_all(exp.Table)`` minus any CTE names defined in the same query, not
``sqlglot.optimizer.scope.build_scope()`` - the latter correctly excludes CTE names for SELECT/WITH but
returns None entirely for DELETE/UPDATE/INSERT (verified directly against a real sqlglot install), which
would silently miss a write statement's own target table. The simpler find_all()-minus-CTE-names approach
was verified to give correct results uniformly across CTEs, subqueries, UNION, self-joins, and bare or
CTE'd DELETE/UPDATE/INSERT - one code path for every statement type this guard needs to check, not two.

Known, accepted limitations: a table name is extracted bare (``information_schema.tables`` -> ``'tables'``),
not schema-qualified - matches allowed_write_ops's own bare-keyword-list style, but can't disambiguate two
identically-named tables in different schemas. A table-valued function (e.g. ClickHouse's ``numbers(10)``)
extracts no table at all, so allowed_tables can't meaningfully restrict one.
"""
from . import sqltools
from .errors import ApiError

_DIALECT_MAP = {'mysql': 'mysql', 'postgres': 'postgres', 'clickhouse': 'clickhouse',
                'sqlite': 'sqlite', 'duckdb': 'duckdb'}  # kept in sync with sqlflow.py's own map


def extract_tables(sql, dialect):
    """The real base tables ``sql`` touches, lowercased - or raises ApiError if that can't be determined for
    this dialect or this specific query. A failure here must reject the query, never degrade quietly the
    way sqlflow.py's own extraction does for its visualization - see the module docstring."""
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
    cte_names = {cte.alias_or_name for cte in parsed.find_all(exp.CTE)}
    return {t.name.lower() for t in parsed.find_all(exp.Table) if t.name and t.name not in cte_names}

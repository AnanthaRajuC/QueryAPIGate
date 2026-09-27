"""Schema introspection: list a connection's tables/views and their columns.

One query per dialect against its catalogue (``information_schema`` for MySQL/PostgreSQL/H2/DuckDB,
``system.tables``/``system.columns`` for ClickHouse, ``sqlite_master``/``pragma_table_info`` for SQLite),
run through the normal execution pipeline (the SQL guard, pagination, pooling) like any other query - there
is no separate code path or driver logic to introspect a database. Nothing here is user-controlled, so the
per-dialect SQL is a plain literal string, not a bound parameter.

Drivers disagree on the case of unaliased and even aliased result column names (MySQL and H2 report
catalogue columns in upper case; PostgreSQL, ClickHouse and SQLite report them as written), so rows are
matched up case-insensitively rather than by relying on any one driver's convention.
"""
from . import engine, store
from .errors import ApiError

# Large enough that no real schema is ever truncated, small enough to bound one request's memory use.
ROW_CAP = 5000

_QUERIES = {
    'mysql': """
        SELECT t.table_schema AS table_schema, t.table_name AS table_name, t.table_type AS table_type,
               c.column_name AS column_name, c.data_type AS data_type, c.is_nullable AS is_nullable,
               c.ordinal_position AS ordinal_position
        FROM information_schema.tables t
        JOIN information_schema.columns c
          ON c.table_schema = t.table_schema AND c.table_name = t.table_name
        WHERE t.table_schema = DATABASE()
        ORDER BY t.table_name, c.ordinal_position
    """,
    'postgres': """
        SELECT t.table_schema AS table_schema, t.table_name AS table_name, t.table_type AS table_type,
               c.column_name AS column_name, c.data_type AS data_type, c.is_nullable AS is_nullable,
               c.ordinal_position AS ordinal_position
        FROM information_schema.tables t
        JOIN information_schema.columns c
          ON c.table_schema = t.table_schema AND c.table_name = t.table_name
        WHERE t.table_schema NOT IN ('pg_catalog', 'information_schema')
        ORDER BY t.table_name, c.ordinal_position
    """,
    'clickhouse': """
        SELECT t.database AS table_schema, t.name AS table_name, t.engine AS table_type, c.name AS column_name,
               c.type AS data_type, if(startsWith(c.type, 'Nullable('), 'YES', 'NO') AS is_nullable,
               c.position AS ordinal_position
        FROM system.tables t
        JOIN system.columns c ON c.database = t.database AND c.table = t.name
        WHERE t.database = currentDatabase()
        ORDER BY t.name, c.position
    """,
    'sqlite': """
        SELECT 'main' AS table_schema, m.name AS table_name, m.type AS table_type, ti.name AS column_name,
               ti.type AS data_type, CASE ti."notnull" WHEN 0 THEN 'YES' ELSE 'NO' END AS is_nullable,
               ti.cid + 1 AS ordinal_position
        FROM sqlite_master m
        JOIN pragma_table_info(m.name) ti
        WHERE m.type IN ('table', 'view') AND m.name NOT LIKE 'sqlite_%'
        ORDER BY m.name, ti.cid
    """,
    'h2': """
        SELECT t.table_schema AS table_schema, t.table_name AS table_name, t.table_type AS table_type,
               c.column_name AS column_name, c.data_type AS data_type, c.is_nullable AS is_nullable,
               c.ordinal_position AS ordinal_position
        FROM information_schema.tables t
        JOIN information_schema.columns c
          ON c.table_schema = t.table_schema AND c.table_name = t.table_name
        WHERE t.table_schema NOT IN ('INFORMATION_SCHEMA')
        ORDER BY t.table_name, c.ordinal_position
    """,
    'duckdb': """
        SELECT t.table_schema AS table_schema, t.table_name AS table_name, t.table_type AS table_type,
               c.column_name AS column_name, c.data_type AS data_type, c.is_nullable AS is_nullable,
               c.ordinal_position AS ordinal_position
        FROM information_schema.tables t
        JOIN information_schema.columns c
          ON c.table_schema = t.table_schema AND c.table_name = t.table_name
        WHERE t.table_schema = 'main'
        ORDER BY t.table_name, c.ordinal_position
    """,
}


def _normalize_table_type(value):
    """'BASE TABLE'/'table' -> table; 'VIEW'/'View'/'MaterializedView' (ClickHouse engine) -> view."""
    return 'view' if 'view' in (value or '').lower() else 'table'


def list_databases(connection_name):
    """Every database on the server a saved connection points at - Run SQL's "browse a different database on
    this same server" picker. Goes through the normal execute_sql path, like fetch_schema() below (unlike
    engine.list_databases(), which the New/Edit connection form's ad-hoc probe needs instead, since that
    connection may not be saved yet). Shares engine.LIST_DATABASES_QUERIES with that function so the two can
    never disagree about which dialects support this."""
    details = store.get_connection(connection_name)
    dialect = details['db']
    if dialect == 'mongo':
        return engine.list_databases(details)
    if dialect not in engine.LIST_DATABASES_QUERIES:
        raise ApiError(f"Listing databases isn't supported for '{dialect}' connections")
    result = engine.execute_sql(engine.LIST_DATABASES_QUERIES[dialect], connection_name, 1000, 0)
    return [row[0] for row in result.rows]


def fetch_schema(connection_name, database=None):
    """Return {'tables': [{'name', 'schema', 'type', 'columns': [{'name', 'type', 'nullable', 'position'}]}],
    'truncated'}. ``schema`` is the catalogue schema/namespace a table lives in (e.g. Postgres's 'public',
    MySQL's own database name, SQLite's fixed 'main') - not present for a mongo collection, which has no
    such concept. ``database`` browses a different database on the same server than the connection's own
    configured one - Run SQL's database picker; see engine.execute_sql()'s own ``database`` parameter."""
    details = store.get_connection(connection_name)
    dialect = details['db']
    if dialect == 'mongo':
        if database:
            details = {**details, 'database': database}
        return {'tables': [{'name': name, 'type': 'collection', 'columns': []}
                           for name in engine.list_collections(details)], 'truncated': False}
    if dialect not in _QUERIES:
        raise ApiError(f"Schema introspection isn't supported for '{dialect}' connections yet")
    if database and dialect not in engine.LIST_DATABASES_QUERIES:
        raise ApiError(f"Switching databases isn't supported for '{dialect}' connections")
    result = engine.execute_sql(_QUERIES[dialect], connection_name, ROW_CAP, 0, database=database)
    lower_columns = [str(c).lower() for c in result.columns]
    tables = {}
    order = []
    for row in result.rows:
        record = dict(zip(lower_columns, row))
        name = record['table_name']
        if name not in tables:
            tables[name] = {'name': name, 'schema': record['table_schema'],
                            'type': _normalize_table_type(record['table_type']), 'columns': []}
            order.append(name)
        tables[name]['columns'].append({
            'name': record['column_name'], 'type': record['data_type'],
            'nullable': str(record['is_nullable']).upper() == 'YES', 'position': record['ordinal_position'],
        })
    return {'tables': [tables[name] for name in order], 'truncated': result.has_more}

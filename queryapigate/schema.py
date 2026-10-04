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
import re

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
               ti.cid + 1 AS ordinal_position, ti.pk AS pk_position, fk."table" AS fk_table, fk."to" AS fk_column
        FROM sqlite_master m
        JOIN pragma_table_info(m.name) ti
        LEFT JOIN pragma_foreign_key_list(m.name) fk ON fk."from" = ti.name
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

# Primary/foreign key markers for the schema browser's Columns tab - a second, additive query per dialect
# (SQLite's is cheap enough to fold into _QUERIES['sqlite'] above instead). Deliberately absent: 'h2' (its
# information_schema constraint shape isn't verifiable without a real JVM/JDBC server, and a guessed query
# risks a silently-wrong badge, a worse failure than no badge) and 'clickhouse' (no real foreign-key concept,
# and its primary key is an informational ORDER BY-style clause on the table, not a per-column constraint
# the other dialects report the same way). A composite (multi-column) key only reports its first column -
# noted, not silently wrong.
_KEY_QUERIES = {
    'mysql': """
        SELECT tc.table_name AS table_name, kcu.column_name AS column_name, tc.constraint_type AS constraint_type,
               kcu.referenced_table_name AS referenced_table_name, kcu.referenced_column_name AS referenced_column_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON kcu.constraint_name = tc.constraint_name AND kcu.table_schema = tc.table_schema
        WHERE tc.table_schema = DATABASE() AND tc.constraint_type IN ('PRIMARY KEY', 'FOREIGN KEY')
    """,
    'postgres': """
        SELECT tc.table_name AS table_name, kcu.column_name AS column_name, tc.constraint_type AS constraint_type,
               ccu.table_name AS referenced_table_name, ccu.column_name AS referenced_column_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON kcu.constraint_name = tc.constraint_name AND kcu.table_schema = tc.table_schema
        LEFT JOIN information_schema.constraint_column_usage ccu
          ON ccu.constraint_name = tc.constraint_name AND tc.constraint_type = 'FOREIGN KEY'
        WHERE tc.table_schema NOT IN ('pg_catalog', 'information_schema')
          AND tc.constraint_type IN ('PRIMARY KEY', 'FOREIGN KEY')
    """,
    'duckdb': """
        SELECT table_name AS table_name, constraint_type AS constraint_type,
               constraint_column_names[1] AS column_name,
               referenced_table AS referenced_table_name, referenced_column_names[1] AS referenced_column_name
        FROM duckdb_constraints()
        WHERE schema_name = 'main' AND constraint_type IN ('PRIMARY KEY', 'FOREIGN KEY')
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
        raise ApiError(f"Listing databases isn't supported for '{dialect}' connections", code='unsupported_operation')
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
        raise ApiError(f"Schema introspection isn't supported for '{dialect}' connections yet",
                       code='unsupported_operation')
    if database and dialect not in engine.LIST_DATABASES_QUERIES:
        raise ApiError(f"Switching databases isn't supported for '{dialect}' connections", code='unsupported_operation')
    result = engine.execute_sql(_QUERIES[dialect], connection_name, ROW_CAP, 0, database=database)
    lower_columns = [str(c).lower() for c in result.columns]
    tables = {}
    order = []
    for row in result.rows:
        record = dict(zip(lower_columns, row, strict=False))
        name = record['table_name']
        if name not in tables:
            tables[name] = {'name': name, 'schema': record['table_schema'],
                            'type': _normalize_table_type(record['table_type']), 'columns': []}
            order.append(name)
        column = {'name': record['column_name'], 'type': record['data_type'],
                  'nullable': str(record['is_nullable']).upper() == 'YES', 'position': record['ordinal_position'],
                  'primary_key': False, 'foreign_key': None}
        if dialect == 'sqlite':
            if record.get('pk_position'):
                column['primary_key'] = True
            if record.get('fk_table'):
                column['foreign_key'] = {'table': record['fk_table'], 'column': record['fk_column']}
        tables[name]['columns'].append(column)
    if dialect in _KEY_QUERIES:
        _merge_keys(tables, connection_name, dialect, database)
    return {'tables': [tables[name] for name in order], 'truncated': result.has_more}


def _merge_keys(tables, connection_name, dialect, database):
    """Merge primary_key/foreign_key onto columns schema.fetch_schema() already built, from _KEY_QUERIES[dialect]
    - best-effort, never raises. An unexpected constraint-catalogue shape or a permissions error just means no
    PK/FK badges for this connection, not a broken schema fetch (the same discipline sqlflow.py uses)."""
    try:
        result = engine.execute_sql(_KEY_QUERIES[dialect], connection_name, ROW_CAP, 0, database=database)
        lower_columns = [str(c).lower() for c in result.columns]
        for row in result.rows:
            record = dict(zip(lower_columns, row, strict=False))
            table = tables.get(record['table_name'])
            if not table:
                continue
            column = next((c for c in table['columns'] if c['name'] == record['column_name']), None)
            if not column:
                continue
            if record['constraint_type'] == 'PRIMARY KEY':
                column['primary_key'] = True
            elif record['constraint_type'] == 'FOREIGN KEY' and record.get('referenced_table_name'):
                column['foreign_key'] = {'table': record['referenced_table_name'],
                                         'column': record['referenced_column_name']}
    except Exception:
        pass


# A table's real DDL (BACKLOG #38). SQLite is free - sqlite_master.sql already *is* the original CREATE
# TABLE text. MySQL and ClickHouse each have a single SHOW CREATE TABLE statement. Postgres has no
# single-statement equivalent (real reconstruction from pg_catalog is a separate, bigger piece of work);
# H2/DuckDB are left out the same way they were for the PK/FK work above - unverified completeness in this
# environment; Mongo has no DDL at all (schemaless).
_DDL_DIALECTS = frozenset({'mysql', 'sqlite', 'clickhouse'})
# A plain SQL identifier - letters, digits, underscore, not starting with a digit. SHOW CREATE TABLE has no
# bound-parameter form for a table name in any driver (binding only ever covers values, never identifiers),
# so this is defense-in-depth *after* the real safety mechanism below: only a name the connection's own
# catalogue already reported ever reaches this far.
_IDENTIFIER_RE = re.compile(r'^[A-Za-z_]\w*$')


def fetch_table_ddl(connection_name, table_name, database=None):
    """Return {'ddl': the real CREATE TABLE text} for `table_name` on `connection_name` - not available for
    every dialect (see _DDL_DIALECTS)."""
    details = store.get_connection(connection_name)
    dialect = details['db']
    if dialect not in _DDL_DIALECTS:
        raise ApiError(f"Showing a table's DDL isn't supported for '{dialect}' connections",
                       code='unsupported_operation')
    # The one real safety mechanism: only a table this connection's own schema actually has can ever reach
    # a DDL query - table_name is never trusted as a safe SQL identifier just because it arrived on a
    # request.
    tables = fetch_schema(connection_name, database=database)['tables']
    if not any(t['name'] == table_name for t in tables):
        raise ApiError(f"'{table_name}' is not a table on '{connection_name}'", 404, code='table_not_found')
    if dialect == 'sqlite':
        result = engine.execute_sql("SELECT sql FROM sqlite_master WHERE type = :type AND name = :name",
                                    connection_name, 1, 0, params={'type': 'table', 'name': table_name},
                                    database=database)
    else:
        if not _IDENTIFIER_RE.match(table_name):
            raise ApiError(f"'{table_name}' is not a table on '{connection_name}'", 404, code='table_not_found')
        quoted = f'`{table_name}`' if dialect == 'mysql' else table_name
        result = engine.execute_sql(f'SHOW CREATE TABLE {quoted}', connection_name, 1, 0, database=database)
    if not result.rows:
        raise ApiError(f"Could not fetch DDL for '{table_name}'", 500)
    return {'ddl': str(result.rows[0][-1])}

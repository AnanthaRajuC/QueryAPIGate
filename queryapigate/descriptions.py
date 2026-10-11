"""What tables and columns mean, for the people and the agents that query them (BACKLOG #86, ADR 0006).

An agent sees `orders.amt` and `cust_tier`; it can only answer well if it's told "net revenue in EUR, refunds
excluded". Descriptions are kept per connection - one for each table, one for each column, and one for the
connection itself (notes: the joins to use, how a metric is defined) - and served wherever a schema is: the schema
endpoints, MCP's `list_tables`, and the data dictionary MCP resource. A saved query describes its own result columns
in its definition (`column_descriptions`).

They come from people (the Console's schema browser, PATCH /api/v1/connections/<name>/descriptions) or from the
database's own comments (`COMMENT ON` in PostgreSQL and DuckDB, column comments in MySQL and ClickHouse), imported on
request - `source` says which, and an import never replaces a written one unless asked to.

Names are kept lower case and matched case-insensitively, as allowed_tables and masking match them. A description
reaches no further than the schema it's attached to: callers only see those of the tables they can reach
(schema._only_allowed() runs first), and annotate() marks a column the caller gets masked, so an agent doesn't plan
around a value it will never see.
"""
from . import db, engine, masking, store
from .errors import ApiError

MAX_DESCRIPTION = 2000       # a table's or a column's
MAX_NOTES = 20000            # the connection's own
MAX_NAME = 128
MAX_ITEMS = 2000             # in one PATCH
SOURCES = ('written', 'database')

# The database's own comments, one row per table (column_name empty) or column. Each is read-only catalogue SQL with
# nothing user-controlled in it, run through the normal pipeline like schema.py's.
_COMMENT_QUERIES = {
    'postgres': """
        SELECT c.relname AS table_name, COALESCE(a.attname, '') AS column_name, d.description AS description
        FROM pg_description d
        JOIN pg_class c ON c.oid = d.objoid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        LEFT JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = d.objsubid AND d.objsubid > 0
        WHERE d.classoid = 'pg_class'::regclass AND c.relkind IN ('r', 'v', 'm', 'p', 'f')
          AND n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname NOT LIKE 'pg_toast%'
          AND (d.objsubid = 0 OR a.attname IS NOT NULL)
    """,
    'mysql': """
        SELECT table_name AS table_name, '' AS column_name, table_comment AS description
        FROM information_schema.tables
        WHERE table_schema = DATABASE() AND table_type = 'BASE TABLE' AND table_comment <> ''
        UNION ALL
        SELECT table_name, column_name, column_comment
        FROM information_schema.columns
        WHERE table_schema = DATABASE() AND column_comment <> ''
    """,
    'clickhouse': """
        SELECT name AS table_name, '' AS column_name, comment AS description
        FROM system.tables WHERE database = currentDatabase() AND comment != ''
        UNION ALL
        SELECT table AS table_name, name AS column_name, comment AS description
        FROM system.columns WHERE database = currentDatabase() AND comment != ''
    """,
    'duckdb': """
        SELECT table_name AS table_name, '' AS column_name, comment AS description
        FROM duckdb_tables() WHERE schema_name = 'main' AND comment IS NOT NULL AND comment <> ''
        UNION ALL
        SELECT view_name, '', comment
        FROM duckdb_views() WHERE schema_name = 'main' AND NOT internal AND comment IS NOT NULL AND comment <> ''
        UNION ALL
        SELECT table_name, column_name, comment
        FROM duckdb_columns() WHERE schema_name = 'main' AND comment IS NOT NULL AND comment <> ''
    """,
}
IMPORTABLE = frozenset(_COMMENT_QUERIES)


def _invalid(message):
    return ApiError(message, code='invalid_body')


def _name(value, what):
    if value is None:
        return ''
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_NAME:
        raise _invalid(f'{what} must be a name of 1 to {MAX_NAME} characters, or null')
    return value.strip().lower()


def validate_items(items):
    """A PATCH body's items as [(table, column, description or None)]: `table` null is the connection's own notes,
    `column` null the table's own description; a null or empty `description` removes one."""
    if not isinstance(items, list) or not items:
        raise _invalid('items must be a non-empty list of {"table", "column", "description"}')
    if len(items) > MAX_ITEMS:
        raise _invalid(f'At most {MAX_ITEMS} descriptions in one request')
    out, seen = [], set()
    for item in items:
        if not isinstance(item, dict) or not set(item) <= {'table', 'column', 'description'} \
                or 'description' not in item:
            raise _invalid('Each item is {"table": ..., "column": ..., "description": ...} - and nothing else')
        table, column = _name(item.get('table'), 'table'), _name(item.get('column'), 'column')
        if column and not table:
            raise _invalid('A column description needs its table')
        text = item['description']
        if text is not None and not isinstance(text, str):
            raise _invalid('description must be text, or null to remove it')
        text = (text or '').strip() or None
        limit = MAX_NOTES if not table else MAX_DESCRIPTION
        if text is not None and len(text) > limit:
            raise _invalid(f'A description may be at most {limit} characters')
        if (table, column) in seen:
            raise _invalid(f"{table or 'the connection'}{'.' + column if column else ''} is listed twice")
        seen.add((table, column))
        out.append((table, column, text))
    return out


def _row(record):
    table, column, text, source, updated_at, updated_by = record
    return {'table': table or None, 'column': column or None, 'description': text, 'source': source,
            'updated_at': updated_at, 'updated_by': updated_by}


def list_for(connection_name):
    """Every description on a connection: the connection's notes first, then by table and column."""
    rows = db.connection().execute(
        'SELECT table_name, column_name, description, source, updated_at, updated_by FROM descriptions '
        'WHERE connection_name = ? ORDER BY table_name, column_name', (connection_name,)).fetchall()
    return [_row(tuple(r)) for r in rows]


def _lookup(connection_name):
    """{(table, column): description} for one connection."""
    rows = db.connection().execute('SELECT table_name, column_name, description FROM descriptions '
                                   'WHERE connection_name = ?', (connection_name,)).fetchall()
    return {(r[0], r[1]): r[2] for r in rows}


def notes(connection_name):
    return _lookup(connection_name).get(('', ''))


def write(connection_name, items, actor, source='written', overwrite=True):
    """Apply validated items: set, or remove where the description is None. With `overwrite` false (an import) a
    written description is kept. Returns {(table, column): (before, after)} for what changed."""
    changed = {}
    now = store.now()
    with db.transaction() as conn:
        existing = {(r[0], r[1]): (r[2], r[3]) for r in conn.execute(
            'SELECT table_name, column_name, description, source FROM descriptions WHERE connection_name = ?',
            (connection_name,)).fetchall()}
        for table, column, text in items:
            before = existing.get((table, column))
            if before is not None and not overwrite and before[1] == 'written':
                continue
            if text is None:
                if before is not None:
                    conn.execute('DELETE FROM descriptions WHERE connection_name = ? AND table_name = ? '
                                 'AND column_name = ?', (connection_name, table, column))
                    changed[(table, column)] = (before[0], None)
                continue
            if before is not None and before[0] == text and before[1] == source:
                continue
            if before is None:
                conn.execute('INSERT INTO descriptions (connection_name, table_name, column_name, description, source, '
                             'updated_at, updated_by) VALUES (?, ?, ?, ?, ?, ?, ?)',
                             (connection_name, table, column, text, source, now, actor))
            else:
                conn.execute('UPDATE descriptions SET description = ?, source = ?, updated_at = ?, updated_by = ? '
                             'WHERE connection_name = ? AND table_name = ? AND column_name = ?',
                             (text, source, now, actor, connection_name, table, column))
            changed[(table, column)] = (before[0] if before else None, text)
    return changed


def database_comments(connection_name):
    """The database's own table and column comments as [(table, column, description)]."""
    dialect = store.get_connection(connection_name)['db']
    if dialect not in _COMMENT_QUERIES:
        raise ApiError(f"Importing comments isn't supported for '{dialect}' connections - "
                       f"{'it has no comments' if dialect == 'sqlite' else 'write the descriptions instead'}",
                       code='unsupported_operation')
    result = engine.execute_sql(_COMMENT_QUERIES[dialect], connection_name, 50000, 0)
    found = {}
    for table, column, text in result.rows:
        if not isinstance(table, str) or not isinstance(text, str) or not text.strip():
            continue
        found[(table.lower(), (column or '').lower())] = text.strip()[:MAX_DESCRIPTION]
    return [(table, column, text) for (table, column), text in sorted(found.items())]


def annotate(result, connection_name, mask=None, allowed_tables=None):
    """A fetched schema ({'tables': [...]}) with each table's and column's description, and - for a caller with
    masking rules - `masked: <action>` on each column they'd get masked. Run after the caller's allowed_tables have
    been applied, so nothing is described that the caller can't reach. The connection's notes are free text that may
    name any table, so a caller restricted to some tables (`allowed_tables`) never gets them."""
    known = _lookup(connection_name)
    tables = []
    for table in result.get('tables', []):
        name = str(table.get('name', '')).lower()
        described = {**table}
        text = known.get((name, ''))
        if text:
            described['description'] = text
        columns = []
        for column in table.get('columns', []):
            column = {**column}
            text = known.get((name, str(column.get('name', '')).lower()))
            if text:
                column['description'] = text
            action = masking.action_for(mask, table.get('schema'), name, column.get('name', ''),
                                        view=table.get('type') == 'view') if mask else None
            if action:
                column['masked'] = action
            columns.append(column)
        described['columns'] = columns
        tables.append(described)
    notes_text = known.get(('', '')) if allowed_tables is None else None
    return {**result, 'tables': tables, **({'notes': notes_text} if notes_text else {})}


def dictionary(connection_name, schema_result):
    """The data dictionary MCP resource: a connection's notes, then every table and column, as Markdown an agent can
    read in one go. `schema_result` is annotate()'s output for the caller."""
    lines = [f'# Data dictionary: {connection_name}', '']
    if schema_result.get('notes'):
        lines += [schema_result['notes'], '']
    for table in schema_result.get('tables', []):
        kind = ' (view)' if table.get('type') == 'view' else ''
        lines.append(f"## {table['name']}{kind}")
        if table.get('description'):
            lines.append(table['description'])
        lines.append('')
        for column in table.get('columns', []):
            parts = [f"- `{column['name']}`"]
            if column.get('type'):
                parts.append(f"({column['type']})")
            if column.get('description'):
                parts.append('- ' + column['description'])
            if column.get('masked'):
                parts.append(f"- masked for you ({column['masked']})")
            lines.append(' '.join(parts))
        lines.append('')
    if schema_result.get('truncated'):
        lines.append('_The schema is longer than this; ask list_tables for the rest._')
    return '\n'.join(lines).rstrip() + '\n'

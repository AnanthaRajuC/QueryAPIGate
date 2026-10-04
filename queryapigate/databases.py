"""Which database supports what (BACKLOG #66): the support matrix in DATABASE_CONNECTION_CONFIGURATION.md, worked out
from the code that implements each feature wherever the code has such a list - so the published table cannot claim
more than the code does. tests/test_databases.py fails if the document's table differs from markdown_table().

Tier 1 types are covered by the compatibility promise and run against a real server in CI. The others are
experimental (experimental.py): they work, with the gaps the matrix shows, and graduate on the terms GRADUATION
gives.
"""
from . import engine, experimental, schema, sqlflow, tableguard
from .runners import RUNNERS, STREAM_RUNNERS

TYPES = ('postgres', 'mysql', 'sqlite', 'duckdb', 'clickhouse', 'h2', 'jdbc', 'mongo')
NAMES = {'postgres': 'PostgreSQL', 'mysql': 'MySQL', 'sqlite': 'SQLite', 'duckdb': 'DuckDB', 'clickhouse': 'ClickHouse',
         'h2': 'H2', 'jdbc': 'JDBC', 'mongo': 'MongoDB'}
TIER_1 = tuple(t for t in TYPES if t not in experimental.DB_TYPES)

# Run against a real server (or, for SQLite, a real file) on every CI push: tests/test_integration.py's classes, and
# the main suite for SQLite. MongoDB has no such test yet - the main reason it is experimental.
INTEGRATION_TESTED = frozenset({'postgres', 'mysql', 'clickhouse', 'duckdb', 'h2', 'jdbc', 'sqlite'})

# Where the database itself refuses writes in a read-only session, beside QueryAPIGate's own SQL check (sqltools):
# MySQL and PostgreSQL read-only transactions, ClickHouse readonly=1, SQLite mode=ro. DuckDB, H2 and JDBC rest on
# the SQL check alone (runners.py says why for each).
DRIVER_READ_ONLY = frozenset({'mysql', 'postgres', 'clickhouse', 'sqlite'})

# QUERYAPIGATE_QUERY_TIMEOUT cancels a running query (runners.py): all but generic JDBC, which has no portable way to.
TIMEOUT_ENFORCED = frozenset(t for t in TYPES if t != 'jdbc')

YES, NO, NA = 'yes', 'no', 'n/a'


def _sql(db):
    return db in RUNNERS


def matrix():
    """[(feature, {db type: 'yes' | 'no' | 'n/a' | a short note})], in the order the document shows them."""
    rows = [
        ('Saved queries and ad-hoc runs',
         {t: YES if _sql(t) else 'find() only' for t in TYPES}),
        ('Read-only by default (SQL check)', {t: YES if _sql(t) else NA for t in TYPES}),
        ('Read-only enforced by the database too', {t: YES if t in DRIVER_READ_ONLY else (NA if not _sql(t) else NO)
                                                    for t in TYPES}),
        ('Writes, when allowed', {t: YES if _sql(t) else NO for t in TYPES}),
        ('Query time limit', {t: ('SELECT only' if t == 'mysql' else YES) if t in TIMEOUT_ENFORCED else NO
                              for t in TYPES}),  # MySQL's max_execution_time covers SELECT only (MariaDB: all)
        ('`allowed_tables` (refused where not supported)', {t: YES if t in tableguard._DIALECT_MAP else NO
                                                            for t in TYPES}),
        ('Schema browser, MCP `list_tables`', {t: YES if t in schema._QUERIES or t == 'mongo' else NO for t in TYPES}),
        ('Primary and foreign keys in the schema', {t: YES if t in schema._KEY_QUERIES else (NO if _sql(t) else NA)
                                                    for t in TYPES}),
        ('Listing and switching databases', {t: YES if t in engine.LIST_DATABASES_QUERIES or t == 'mongo' else
                                             (NA if t in ('sqlite', 'duckdb') else NO)  # one file: nothing to switch to
                                             for t in TYPES}),
        ('Table DDL', {t: YES if t in schema._DDL_DIALECTS else (NO if _sql(t) else NA) for t in TYPES}),
        ('Streaming exports', {t: YES if t in STREAM_RUNNERS else NO for t in TYPES}),
        ('Response caching (`cache_ttl`)', {t: YES for t in TYPES}),
        ('MCP `execute_sql`', {t: YES if _sql(t) else NO for t in TYPES}),
        ('Tables-and-joins diagram', {t: YES if t in sqlflow._DIALECT_MAP else (NO if _sql(t) else NA)
                                      for t in TYPES}),
        ('Tested against a real server in CI', {t: YES if t in INTEGRATION_TESTED else NO for t in TYPES}),
    ]
    return rows


GRADUATION = (
    'it runs against a real server in the CI integration job (tests/test_integration.py), reads and writes;',
    'the read-only default, the query time limit and `allowed_tables` all work on it - each a test, not a claim;',
    'its gaps in this table are closed, or stated as limits of the database itself;',
    'its entry leaves `queryapigate/experimental.py`, and this table and the changelog say so.',
)


def markdown_table():
    """The matrix as the Markdown table the document carries between its support-matrix markers."""
    header = '| | ' + ' | '.join(
        f'{NAMES[t]}{"" if t in TIER_1 else " *"}' for t in TYPES) + ' |'
    lines = [header, '|---|' + '---|' * len(TYPES)]
    for feature, cells in matrix():
        lines.append(f'| {feature} | ' + ' | '.join(cells[t] for t in TYPES) + ' |')
    return '\n'.join(lines)

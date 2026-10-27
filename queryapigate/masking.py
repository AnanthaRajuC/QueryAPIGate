"""Column masking (ADR 0005, BACKLOG #63): rules that hide, hash or drop the values of named source columns in what a
caller receives - traced through the SQL to where each result column comes from, and refusing when that can't be
told.

A rule names a **source** column, `table.column` (`schema.table.column` optional), with `*` wildcards, and an action:

- `hash` - HMAC-SHA256 with a per-store key, 16 hex characters: equal inputs give equal outputs, so grouping, counting
  and joining still work, and without the key a likely value can't be tried against it;
- `mask` - text keeps its first character, an email its domain (`j***@example.com`); any other value becomes null;
- `null` - the column stays, its values null;
- `drop` - the column is removed;
- `keep` - left as it is, on purpose: settles a view column the other rules can't be told apart from (below).

**Tracing.** A rule on the *result* column name would be beaten by `SELECT email AS e` or `lower(email)`. So each
result column is traced, with sqlglot's lineage, to the table columns it is computed from - through aliases,
expressions, CTEs, subqueries, unions and joins - with the connection's own schema to expand `SELECT *`. A result
column computed from a masked source is masked as a whole, by the strictest rule that applies (drop > null > mask >
hash). Result columns are traced by position, so two with the same name never borrow each other's source.

**Failing closed.** When a result column can't be traced - a table function, a `*` the schema can't expand, a column
the parser can't place, a dialect sqlglot can't read - the query is refused (`403 mask_unresolvable`), never served
unmasked. Constants (`1`, `'x'`, `VALUES`, `count(*)`) carry no column's values and need no rule.

**Views.** Lineage stops at a view: its columns are matched as `view.column`. A rule whose column part matches a view's
column but whose table part doesn't name the view - `customers.email`, read through `customer_view.email` - can't be
told apart from the view hiding a masked column, so that query is refused too, unless a rule names the view column
itself: `*.email` (or `customer_view.email`) with an action, or `keep` to say it's fine as it is.

What masking doesn't stop - filtering or grouping on a masked column (`WHERE email = '...'`) - is in ADR 0005.
"""
import collections
import fnmatch
import hashlib
import hmac
import json
import logging
import os
import queue
import re
import secrets
import threading
import time

from . import config, db, metacache, sqltools, store
from .errors import ApiError

log = logging.getLogger('queryapigate')

ACTIONS = ('drop', 'null', 'mask', 'hash', 'keep')  # strictest first
_STRICTNESS = {action: rank for rank, action in enumerate(ACTIONS)}
_PATTERN_RE = re.compile(r'^[A-Za-z0-9_*$]+(\.[A-Za-z0-9_*$]+){1,2}$')
_DIALECT_MAP = {'mysql': 'mysql', 'postgres': 'postgres', 'clickhouse': 'clickhouse', 'sqlite': 'sqlite',
                'duckdb': 'duckdb', 'trino': 'trino'}  # as tableguard.py's
_KEY_NAME = 'mask_hash_key'
_ALIAS = '__qag_mask_{}'


def _refuse(message):
    return ApiError(message, 403, code='mask_unresolvable')


# --------------------------------------------------------------------------------------
# Rules
# --------------------------------------------------------------------------------------

def validate_rules(rules):
    """Rules as stored: a list of {"column": "table.column", "action": ...}; raises invalid_body naming the problem."""
    if rules is None:
        return None
    if not isinstance(rules, list):
        raise ApiError('mask must be a list of {"column": "table.column", "action": "hash|mask|null|drop"}',
                       code='invalid_body')
    out = []
    for rule in rules:
        if not isinstance(rule, dict) or set(rule) != {'column', 'action'}:
            raise ApiError('Each mask rule is {"column": "table.column", "action": ...} - and nothing else',
                           code='invalid_body')
        column, action = rule['column'], rule['action']
        if not isinstance(column, str) or not _PATTERN_RE.match(column):
            raise ApiError(f"mask column '{column}' must be table.column (or schema.table.column), with * as a "
                           'wildcard: customers.email, *.phone', code='invalid_body')
        if action not in ACTIONS:
            raise ApiError(f"mask action '{action}' must be one of: {', '.join(ACTIONS)}", code='invalid_body')
        out.append({'column': column.lower(), 'action': action})
    return out


def fingerprint(rules):
    """Stable for a set of rules - part of a cached response's key, so callers with different rules never share one."""
    if not rules:
        return None
    canonical = json.dumps(sorted((r['column'], r['action']) for r in rules))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def _matches(pattern, schema, table, column):
    parts = pattern.split('.')
    if len(parts) == 3:
        return fnmatch.fnmatchcase(schema or '', parts[0]) and fnmatch.fnmatchcase(table, parts[1]) \
            and fnmatch.fnmatchcase(column, parts[2])
    return fnmatch.fnmatchcase(table, parts[0]) and fnmatch.fnmatchcase(column, parts[1])


def _column_part_matches(pattern, column):
    return fnmatch.fnmatchcase(column, pattern.split('.')[-1])


def action_for(rules, schema, table, column, view=False):
    """What reading one table column gets a caller with these rules: the strictest action that applies (never
    `keep`), None, or `refused` for a view column plan() couldn't tell apart from a masked one. For describing a
    schema to its caller (descriptions.annotate()); plan() is what masks a result."""
    schema, table, column = (schema or '').lower(), table.lower(), column.lower()
    matching = [rule['action'] for rule in rules or () if _matches(rule['column'], schema, table, column)]
    if matching:
        chosen = min(matching, key=_STRICTNESS.get)
        return None if chosen == 'keep' else chosen
    if view and any(_column_part_matches(rule['column'], column) for rule in rules or ()):
        return 'refused'
    return None


# --------------------------------------------------------------------------------------
# Tracing
# --------------------------------------------------------------------------------------

class Source:
    """A table column a result column is computed from."""

    def __init__(self, schema, table, column, view=False):
        self.schema, self.table, self.column, self.view = (schema or '').lower(), table.lower(), column.lower(), view

    def __repr__(self):
        return f"{self.table}.{self.column}{' (view)' if self.view else ''}"

    def __eq__(self, other):
        return isinstance(other, Source) and (self.schema, self.table, self.column) == \
            (other.schema, other.table, other.column)

    def __hash__(self):
        return hash((self.schema, self.table, self.column))


def _outermost_select(expression):
    from sqlglot import exp
    while isinstance(expression, exp.SetOperation):
        expression = expression.this
    while isinstance(expression, exp.Subquery):
        expression = expression.this
    return expression if isinstance(expression, exp.Select) else None


_RANKING = {'rank', 'dense_rank', 'ntile', 'percent_rank', 'cume_dist'}


def _is_constant(expression):
    """Whether a lineage leaf that isn't a table column holds no column's values: literals, NULL, plain operators and
    casts over them, count(*). Anything else - a function sqlglot can't see into, such as DuckDB's COLUMNS('e.*'),
    which picks columns by pattern - is not trusted to be a constant."""
    from sqlglot import exp
    allowed = (exp.Alias, exp.Literal, exp.Null, exp.Boolean, exp.Paren, exp.Neg, exp.Not, exp.Binary, exp.Cast,
               exp.TryCast, exp.DataType, exp.DataTypeParam, exp.Identifier, exp.Interval, exp.Var, exp.CurrentDate,
               exp.CurrentTimestamp, exp.CurrentTime, exp.Tuple,
               # a ranking over no column - RANK() OVER (ORDER BY COUNT(*) DESC); a column inside is still refused
               exp.Window, exp.Order, exp.Ordered, exp.RowNumber)
    # sqlglot 30 parses the ranking functions as their own types (exp.Rank); older ones, as anonymous functions
    ranking = tuple(getattr(exp, name) for name in ('Rank', 'DenseRank', 'Ntile', 'PercentRank', 'CumeDist')
                    if hasattr(exp, name))
    for node in expression.walk():
        if isinstance(node, ranking) or (isinstance(node, exp.Anonymous) and node.name.lower() in _RANKING):
            continue  # the function itself; a column among its arguments is still walked, and refused
        if isinstance(node, exp.Count) and isinstance(node.this, (exp.Star, exp.Literal)):
            continue
        if isinstance(node, exp.Star) and isinstance(node.parent, exp.Count):
            continue
        if not isinstance(node, allowed):
            return False
    return True


def _from_sources(select):
    """What a SELECT reads from, in the order its result columns come - the FROM item, then each join's, a nested
    join's own in place."""
    from sqlglot import exp
    sources = []

    def add(source):
        sources.append(source)
        for join in (source.args.get('joins') or ()) if isinstance(source, exp.Table) else ():
            add(join.this)
    from_ = sqltools.sqlglot_arg(select, 'from')
    if from_ is not None:
        add(from_.this)
    for join in select.args.get('joins') or ():
        add(join.this)
    return sources + list(select.args.get('laterals') or ())


def _is_derived(source):
    """A FROM source that is not a table: a subquery, VALUES, LATERAL, UNNEST or a table function."""
    from sqlglot import exp
    return isinstance(source, (exp.Subquery, exp.Values, exp.Lateral, exp.Unnest)) or (
        isinstance(source, exp.Table) and isinstance(source.this, exp.Func))


def _refuse_shapes(parsed):
    """Query shapes whose columns tracing can't follow faithfully, refused before tracing: a real table given new
    column names in its alias (`customers AS c(a, e)` - e is then email, while the parser sees a column named e),
    SELECT * over a NATURAL or USING join (the database merges the join columns, so the result has fewer columns
    than the parser counts), and SELECT * over several sources one of which isn't a table.

    That last: masks are applied by position, and sqlglot before 30.x expands `*` with the tables first and a
    subquery, VALUES or LATERAL after them, whatever order the query names them in - so in `SELECT * FROM (SELECT ...)
    s JOIN customers c`, the database returns s's columns first while the trace put customers' there, and a mask
    meant for customers.email landed on another column; the email came back unmasked. Refused on every sqlglot
    version rather than trusted to one: `s.*, c.*`, or named columns, trace in the order written."""
    from sqlglot import exp
    for table in parsed.find_all(exp.Table):
        alias = table.args.get('alias')
        if alias is not None and alias.columns:
            raise _refuse(f"A table alias that renames columns ({table.sql()}) can't be traced for masking - "
                          'alias the columns in the select list instead')
    for select in parsed.find_all(exp.Select):
        if not any(isinstance(p, exp.Star) or (isinstance(p, exp.Column) and isinstance(p.this, exp.Star))
                   for p in select.expressions):
            continue
        for join in select.args.get('joins') or ():
            if join.args.get('using') or str(join.args.get('method') or '').upper() == 'NATURAL':
                raise _refuse('SELECT * over a NATURAL or USING join can\'t be traced for masking - name the columns')
        if not any(isinstance(p, exp.Star) for p in select.expressions):
            continue  # each t.* expands one source, in the order written
        sources = _from_sources(select)
        if len(sources) > 1 and any(_is_derived(source) for source in sources):
            raise _refuse("SELECT * over a subquery, VALUES or LATERAL joined with other tables can't be traced in "
                          'order for masking - select s.*, c.* or name the columns')


def check_columns(traced, result_columns):
    """Refuse when the database returned a different number of columns than tracing found - the masks would land
    on the wrong columns."""
    if traced is not None and len(traced) != len(result_columns):
        raise _refuse(f'The database returned {len(result_columns)} columns where masking traced {len(traced)} - '
                      'name the columns in the select list')


MIN_SQLGLOT = (25, 2)  # exp.SetOperation and the lineage the tracing below relies on; verified from here up to <31


def require_supported_sqlglot(sqlglot):
    """Refuse - with what to do - a sqlglot older than the tracing was verified on, rather than failing inside it.
    pyproject.toml asks for it too; this is for an install whose sqlglot was pinned by something else."""
    found = tuple(int(part) for part in re.findall(r'\d+', sqlglot.__version__)[:2])
    if found < MIN_SQLGLOT:
        raise ApiError(f"Column masking needs sqlglot {'.'.join(map(str, MIN_SQLGLOT))} or later, and "
                       f"{sqlglot.__version__} is installed - run `pip install -U 'sqlglot>=25.2,<31'`", 500,
                       code='masking_unavailable')


def trace(sql, dialect, tables):
    """[(result column, {Source, ...}), ...] in the result's order, or None for a statement that returns no table data
    (SHOW, DESCRIBE, EXPLAIN). `tables` is schema.fetch_schema()'s list. Raises mask_unresolvable when any result
    column can't be traced."""
    if sqltools.first_keyword(sql, dialect) in ('show', 'describe', 'desc', 'explain'):
        return None
    if dialect not in _DIALECT_MAP:
        raise _refuse(f"Column masking can't trace queries on '{dialect}' connections, so a caller with mask rules "
                      'is refused on them')
    try:
        import sqlglot
        from sqlglot import exp
        from sqlglot.lineage import lineage
        from sqlglot.optimizer.qualify import qualify
    except ImportError:
        raise ApiError('Column masking needs the "sqlglot" package - run `pip install "queryapigate[flow]"`', 500,
                       code='masking_unavailable') from None
    require_supported_sqlglot(sqlglot)
    read = _DIALECT_MAP[dialect]
    schema = {}
    views = set()
    for table in tables or ():
        columns = {c['name']: 'TEXT' for c in table.get('columns') or ()}
        if columns:
            schema.setdefault(table['name'], {}).update(columns)
        if table.get('type') == 'view':
            views.add(table['name'].lower())
    try:
        parsed = sqlglot.parse_one(sqltools.substitute_placeholders(sql, dialect), read=read)
    except Exception as error:
        raise _refuse(f"This query's result columns can't be traced to their sources for masking: "
                      f'{str(error).splitlines()[0]}') from None
    _refuse_shapes(parsed)
    known = {name.lower(): {c.lower() for c in columns} for name, columns in schema.items()}
    try:
        qualified = qualify(parsed, schema=schema or None, dialect=read)
    except Exception as error:
        raise _refuse(f"This query's result columns can't be traced to their sources for masking: "
                      f'{str(error).splitlines()[0]}') from None
    outer = _outermost_select(qualified)
    if outer is None:
        raise _refuse("This query's result columns can't be traced to their sources for masking")
    names = []
    for position, projection in enumerate(list(outer.expressions)):
        if isinstance(projection, exp.Star) or (isinstance(projection, exp.Column)
                                                and isinstance(projection.this, exp.Star)):
            raise _refuse("SELECT * couldn't be expanded for masking - a table this connection's schema doesn't list, "
                          'or a table function; name the columns')
        names.append(projection.alias_or_name)
        projection.replace(exp.alias_(projection.unalias(), _ALIAS.format(position), quoted=True))
    traced = []
    for position, name in enumerate(names):
        try:
            node = lineage(_ALIAS.format(position), qualified, schema=schema or None, dialect=read)
        except Exception as error:
            raise _refuse(f"Result column '{name}' can't be traced to its sources for masking: "
                          f'{str(error).splitlines()[0]}') from None
        sources = set()
        for leaf in node.walk():
            if leaf.downstream:
                continue
            source = leaf.source
            column = leaf.name.split('.')[-1].strip('"')
            if isinstance(source, exp.Table):
                if not source.name or column == '*' or isinstance(source.this, exp.Func):
                    raise _refuse(f"Result column '{name}' comes from a table function or a table this connection's "
                                  'schema doesn\'t list, so masking can\'t tell what it holds')
                if source.name.lower() in known and column.lower() not in known[source.name.lower()]:
                    raise _refuse(f"Result column '{name}' traces to {source.name}.{column}, which that table doesn't "
                                  'have - masking can\'t tell what it holds')
                sources.add(Source(source.db, source.name, column, view=source.name.lower() in views))
            elif isinstance(source, exp.Values) and _is_constant(source.args.get('expressions') and
                                                                 exp.Tuple(expressions=source.expressions)):
                continue  # literal rows written into the query
            elif isinstance(source, exp.Select) and _is_constant(leaf.expression):
                continue  # a constant, or count(*): no column's values in it
            else:
                raise _refuse(f"Result column '{name}' can't be traced to its sources for masking")
        traced.append((name, sources))
    return traced


def plan(rules, traced):
    """Per result column, the action to apply (or None), and what fired: ([action|None, ...], [{column, action,
    source}, ...]). Raises mask_unresolvable for a view column a rule can't be told apart from."""
    actions, fired = [], []
    for name, sources in traced:
        chosen = None
        for source in sources:
            matching = [rule for rule in rules if _matches(rule['column'], source.schema, source.table, source.column)]
            for rule in matching:
                if chosen is None or _STRICTNESS[rule['action']] < _STRICTNESS[chosen[0]]:
                    chosen = (rule['action'], source)
            if source.view and not matching:
                for rule in rules:
                    if not _column_part_matches(rule['column'], source.column):
                        continue
                    raise _refuse(f"Result column '{name}' comes from view {source.table}, and mask rule "
                                  f"'{rule['column']}' may mean the column it shows - name the view in the rule, or "
                                  f"use *.{rule['column'].split('.')[-1]}, or say {source.table}.{source.column} is "
                                  "fine with the action 'keep'")
        if chosen is not None and chosen[0] == 'keep':
            chosen = None
        actions.append(chosen[0] if chosen else None)
        if chosen:
            fired.append({'column': name, 'action': chosen[0], 'source': repr(chosen[1])})
    return actions, fired


# --------------------------------------------------------------------------------------
# Applying
# --------------------------------------------------------------------------------------

# store target -> the hash key it holds: made once and never changed, so read once per process
_store_keys: dict[tuple, bytes] = {}
metacache.on_clear(_store_keys.clear)  # and again after a change to the store - a restore, a test resetting it


def hash_key():
    """The key `hash` uses: QUERYAPIGATE_MASK_KEY, or one the store makes once and every instance shares. A store's key
    is never changed once made, so it's read from the store once per process (per store) and kept until this process
    next changes the store, not read on every masked call."""
    configured = config.mask_key()
    if configured:
        return configured.encode()
    target = db.current_target()
    cached = _store_keys.get(target)
    if cached is not None:
        return cached
    _store_keys[target] = key = _store_hash_key()
    return key


def _store_hash_key():
    conn = db.connection()
    row = conn.execute('SELECT value FROM server_secrets WHERE name = ?', (_KEY_NAME,)).fetchone()
    if row is None:
        with db.transaction() as write:
            row = write.execute('SELECT value FROM server_secrets WHERE name = ?', (_KEY_NAME,)).fetchone()
            if row is None:
                write.execute('INSERT INTO server_secrets (name, value, created_at) VALUES (?, ?, ?)',
                              (_KEY_NAME, secrets.token_hex(32), store.now()))
                row = write.execute('SELECT value FROM server_secrets WHERE name = ?', (_KEY_NAME,)).fetchone()
    return row['value'].encode()


def _hash(value, key):
    if value is None:
        return None
    text = value.decode('utf-8', 'replace') if isinstance(value, (bytes, bytearray)) else str(value)
    return hmac.new(key, text.encode('utf-8'), hashlib.sha256).hexdigest()[:16]


def _mask(value):
    if not isinstance(value, str) or not value:
        return None
    if '@' in value:
        local, _, domain = value.rpartition('@')
        return (local[:1] + '***@' + domain) if local else '***@' + domain
    return value[:1] + '*' * max(3, len(value) - 1)


def apply(columns, rows, actions):
    """(columns, rows) with `actions` applied - dropped columns removed, the rest replaced row by row, lazily. The
    rows come back as a list for a list, otherwise as an iterator whose close() closes the one it wraps (a stream's -
    see engine._Rows)."""
    if not any(actions):
        return columns, rows
    key = hash_key() if 'hash' in actions else None
    kept = [i for i, action in enumerate(actions) if action != 'drop']
    out_columns = [columns[i] for i in kept]

    def convert(row):
        values = []
        for i in kept:
            value, action = row[i], actions[i]
            if action == 'hash':
                value = _hash(value, key)
            elif action == 'mask':
                value = _mask(value)
            elif action == 'null':
                value = None
            values.append(value)
        return tuple(values)

    if isinstance(rows, list):
        return out_columns, [convert(row) for row in rows]
    return out_columns, _Masked(rows, convert)


class _Masked:
    """A stream's rows, masked as they pass - closing it closes the stream underneath, read or not."""

    def __init__(self, rows, convert):
        self._rows, self._iter, self._convert = rows, iter(rows), convert

    def __iter__(self):
        return self

    def __next__(self):
        return self._convert(next(self._iter))

    def close(self):
        close = getattr(self._rows, 'close', None)
        if close is not None:
            close()


# --------------------------------------------------------------------------------------
# For a query: trace before it runs, apply to what comes back
# --------------------------------------------------------------------------------------

SCHEMA_TTL = 60  # seconds a connection's schema is reused for tracing, at most
SCHEMA_REFRESH_AFTER = 45  # past this, a call starts reading it again in the background and goes on with this one
TRACE_CACHE_SIZE = 256  # statements kept traced per connection schema - bounds what varied ad-hoc SQL can hold
_schemas: dict = {}  # (store target, workspace, connection, database) -> (read at, tables, traces)
_schemas_lock = threading.Lock()
_generation = 0  # bumped by forget_schema(): a read that started before it never stores what it read
_refreshing: set = set()  # keys a background read is already under way for
_refresher = None  # (pid, queue) of this process's background reader - started on first use, after any fork


def _schema(connection_name, database=None):
    """(tables, traces) for a connection: its schema, reused for SCHEMA_TTL, and the statements already traced against
    exactly that schema. The traces live in the schema's own cache entry, so they are dropped with it - when it
    expires, or forget_schema() runs after the connection changes - and a trace never outlives the schema it was
    computed from.

    Read ahead: the first call past SCHEMA_REFRESH_AFTER has the schema read again in the background (_refresh()) and
    is served from the cache meanwhile, so a busy connection's calls never wait for it - re-reading it, and tracing
    each statement again, put a 75 ms spike in one call a minute (PERFORMANCE_V3.md). A schema is still never used
    past SCHEMA_TTL: a connection quiet for that long is read in the call, as before. Keyed by store and workspace as
    well as by name: two workspaces' connections can share a name, and not their tables."""
    from . import schema, scope
    key = (db.current_target(), scope.current(), connection_name, database)
    now = time.monotonic()
    with _schemas_lock:
        cached = _schemas.get(key)
        if cached is not None and now - cached[0] < SCHEMA_TTL:
            if now - cached[0] >= SCHEMA_REFRESH_AFTER and key not in _refreshing:
                _refreshing.add(key)
                _refresh_soon(key)
            return cached[1], cached[2]
        generation = _generation
    tables = schema.fetch_schema(connection_name, database)['tables']
    traces = collections.OrderedDict()
    with _schemas_lock:
        if generation == _generation:
            _schemas[key] = (now, tables, traces)
    return tables, traces


def schema_expires_in(connection_name, database=None):
    """Seconds until the schema copy masking traces this connection's statements against expires (SCHEMA_TTL), or None
    when it holds none - how long a data plane may apply a trace made from it (dataplane.py, ADR 0028)."""
    from . import scope
    key = (db.current_target(), scope.current(), connection_name, database)
    with _schemas_lock:
        cached = _schemas.get(key)
    if cached is None:
        return None
    return max(0.0, SCHEMA_TTL - (time.monotonic() - cached[0]))


def _refresh_soon(key):
    """Queue `key` for this process's background reader, starting it if this process has none yet. Called holding
    _schemas_lock."""
    global _refresher
    if _refresher is None or _refresher[0] != os.getpid():
        jobs: queue.SimpleQueue = queue.SimpleQueue()
        threading.Thread(target=_read_ahead, args=(jobs,), name='queryapigate-schema-refresh', daemon=True).start()
        _refresher = (os.getpid(), jobs)
    _refresher[1].put(key)


def _read_ahead(jobs):
    """The background reader: one thread, so its store connection is opened once, not per read."""
    while True:
        key = jobs.get()
        try:
            _refresh(key)
        except Exception:
            # the cached schema serves until SCHEMA_TTL; then a call reads it itself, and sees whatever is wrong
            log.warning('Reading the schema of %s again for masking failed', key[2], exc_info=True)
        finally:
            with _schemas_lock:
                _refreshing.discard(key)


def _refresh(key):
    """Read `key`'s schema again, in its own store and workspace. With no request behind it, the read runs under no
    caller's limits and takes none of their concurrent slots. The traces carry over when the schema is unchanged -
    they are a function of it - and start empty when it isn't."""
    from . import schema, scope
    target, workspace, connection_name, database = key
    with _schemas_lock:
        generation = _generation
    started = time.monotonic()
    with scope.using(workspace):
        if db.current_target() != target:
            return  # the store this was cached from is no longer this server's
        tables = schema.fetch_schema(connection_name, database)['tables']
    with _schemas_lock:
        cached = _schemas.get(key)
        if cached is None or generation != _generation:
            return  # forgotten meanwhile: the next call reads it afresh
        _schemas[key] = (started, tables, cached[2] if cached[1] == tables else collections.OrderedDict())


def _cached_trace(traces, sql, dialect, tables):
    """trace() of `sql` - after returning_as_select() - computed once per schema and reused. Tracing is a pure function
    of the statement, the dialect and the schema, and costs milliseconds of sqlglot lineage work, while a saved
    query's SQL never changes between calls: repeating it on every call was most of a masked call's time. Cached
    frozen (a tuple of (column, frozenset of Sources)) so nothing can change it for the next caller. A statement that
    can't be traced isn't cached: it raises again, every time."""
    key = (dialect, sql)
    with _schemas_lock:
        if key in traces:
            traces.move_to_end(key)
            return traces[key]
    traced = trace(returning_as_select(sql, dialect), dialect, tables)
    frozen = None if traced is None else tuple((name, frozenset(sources)) for name, sources in traced)
    with _schemas_lock:
        traces[key] = frozen
        while len(traces) > TRACE_CACHE_SIZE:
            traces.popitem(last=False)
    return frozen


def forget_schema(connection_name=None):
    """Drop cached schemas - one connection's (in every workspace), after it changes, or all - and whatever a
    background read of one is about to store."""
    global _generation
    with _schemas_lock:
        _generation += 1
        for key in [k for k in _schemas if connection_name is None or k[2] == connection_name]:
            del _schemas[key]


class Plan:
    """What masking does to one query's result: traced before the query runs, applied to what comes back."""

    def __init__(self, traced, actions, fired):
        self.traced, self.actions, self.fired = traced, actions, fired

    def apply(self, columns, rows):
        check_columns(self.traced, columns)
        return apply(list(columns), rows, self.actions)


def describe(rules, sql, connection_name, dialect):
    """For GET /catalog: what a caller with `rules` gets from a saved query - {'masked': [{column, action}]}, or
    {'refused': why} when masking can't follow it. The result column and the action only: where it was traced from
    names tables the caller may not otherwise see."""
    try:
        plan_ = prepare(rules, sql, connection_name, dialect)
    except ApiError as error:
        if error.code != 'mask_unresolvable':
            raise
        return {'masked': [], 'refused': error.message}
    if plan_ is None:
        return {'masked': [], 'refused': None}
    return {'masked': [{'column': f['column'], 'action': f['action']} for f in plan_.fired], 'refused': None}


def returning_as_select(sql, dialect):
    """A data change's RETURNING clause, as the SELECT that returns the same columns - `SELECT <its expressions> FROM
    <the table written>` - so tracing reads it like any read (an agent's write, ADR 0008). Only for a change to one
    table: an UPDATE ... FROM or DELETE ... USING could return another table's columns, and is refused. Any other
    statement comes back as it is."""
    if sqltools.first_keyword(sql, dialect) not in ('insert', 'update', 'delete'):
        return sql
    try:
        import sqlglot
        from sqlglot import exp
    except ImportError:
        return sql  # trace() says what's missing
    try:
        parsed = sqlglot.parse_one(sqltools.substitute_placeholders(sql, dialect), read=_DIALECT_MAP.get(dialect))
    except Exception:
        raise _refuse("This statement can't be read to mask what it returns") from None
    returning = parsed.args.get('returning')
    if returning is None:
        return sql
    target = parsed.this if not isinstance(parsed.this, exp.Schema) else parsed.this.this  # INSERT INTO t (cols)
    if not isinstance(target, exp.Table) or sqltools.sqlglot_arg(parsed, 'from') or parsed.args.get('using') \
            or parsed.args.get('joins'):
        raise _refuse('Masking can follow what a change to one table returns - not one that joins others in')
    select = exp.select(*returning.expressions).from_(target.copy())
    return select.sql(dialect=_DIALECT_MAP.get(dialect))


def prepare(rules, sql, connection_name, dialect, database=None):
    """The Plan for running `sql` for a caller with `rules`, or None when there is nothing to mask (no rules, or a
    statement with no table data). Raises mask_unresolvable - before the query runs - when it can't be traced."""
    if not rules:
        return None
    if dialect == 'mongo':
        raise _refuse("Column masking isn't supported for MongoDB connections yet, so a caller with mask rules is "
                      'refused on them')
    if dialect in _DIALECT_MAP:
        tables, traces = _schema(connection_name, database)
        traced = _cached_trace(traces, sql, dialect, tables)
    else:
        traced = trace(returning_as_select(sql, dialect), dialect, [])  # refused for this dialect
    if traced is None:
        return None
    actions, fired = plan(rules, traced)
    return Plan(traced, actions, fired)

"""Pure SQL text handling: validation, pagination, and parameter substitution/binding."""
import math
import re

from . import config
from .errors import ApiError

# Quoted strings, quoted identifiers and comments - text inside these is never inspected.
#
# Two variants exist because MySQL and ClickHouse honour a C-style backslash escape inside '...' and "..."
# string literals by default (\' does not close the string), while PostgreSQL, SQLite and H2 do not (under
# their standard/default settings, a quote only escapes via doubling, '' or ""). Using the doubling-only
# rule for a backslash-honouring database under-counts how long a string stays open: a value ending in an
# odd number of backslashes before a quote can make that database treat what we consider "outside the
# string" as still inside it, or vice versa, letting a semicolon or a second statement hide from this
# guard's semicolon check while the database executes it as separate, live SQL. This was verified against
# real MySQL and ClickHouse servers, see tests/test_sql_guard_fuzz.py. `--` comments also require the
# character after `--` to be whitespace or end of input, matching real SQL comment syntax; a bare `--x`
# is not a comment to any of our dialects and must stay visible to this guard.
_COMMENT = r'--(?=[ \t\n]|\Z)[^\n]*|/\*.*?\*/'
_ANSI_LITERAL = rf"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|`(?:[^`]|``)*`|{_COMMENT}"
_BACKSLASH_LITERAL = rf"'(?:[^'\\]|\\.|'')*'|\"(?:[^\"\\]|\\.|\"\")*\"|`(?:[^`]|``)*`|{_COMMENT}"
_ANSI_LITERALS_RE = re.compile(_ANSI_LITERAL, re.S)
_BACKSLASH_LITERALS_RE = re.compile(_BACKSLASH_LITERAL, re.S)
# Dialects whose string literals honour backslash escapes (\') by default - see the comment above.
BACKSLASH_ESCAPE_DIALECTS = frozenset({'mysql', 'clickhouse'})
# A ":name" marker, skipping literals and Postgres "::" casts. Built per dialect for the same reason.
_PARAM_DIALECT_LITERALS = ((None, _ANSI_LITERAL), *((d, _BACKSLASH_LITERAL) for d in BACKSLASH_ESCAPE_DIALECTS))
_PARAM_RE = {dialect: re.compile(rf'(?P<skip>{literal}|::)|:(?P<name>[A-Za-z_]\w*)', re.S)
            for dialect, literal in _PARAM_DIALECT_LITERALS}
_BRACE_RE = re.compile(r'\{(\w+)\}')
# A statement's own trailing row window, in every form the paginated dialects accept: LIMIT n [OFFSET m],
# LIMIT ALL, MySQL/SQLite/ClickHouse's LIMIT m, n (offset first), OFFSET m [ROWS] alone, and the SQL-standard
# [OFFSET m ROWS] FETCH {FIRST|NEXT} [n] {ROW|ROWS} ONLY. Each number may instead be a bound :parameter.
# Matched against the statement with its literals and comments masked (see _window_mask()), never the raw text.
_N = r'(\d+|:[A-Za-z_]\w*)'
_TRAILING_WINDOW_RES = (
    ('offset_count', re.compile(rf'\s+LIMIT\s+{_N}\s*,\s*{_N}\s*$', re.I)),
    ('limit_offset', re.compile(rf'\s+LIMIT\s+(?:{_N}|ALL)(?:\s+OFFSET\s+{_N}(?:\s+ROWS?)?)?\s*$', re.I)),
    ('offset_fetch', re.compile(
        rf'\s+OFFSET\s+{_N}(?:\s+ROWS?)?(?:\s+FETCH\s+(?:FIRST|NEXT)\s+{_N}?\s*ROWS?\s+ONLY)?\s*$', re.I)),
    ('fetch', re.compile(rf'\s+FETCH\s+(?:FIRST|NEXT)\s+{_N}?\s*ROWS?\s+ONLY\s*$', re.I)),
)
_SAFE_TEXT_RE = re.compile(r'^[\w\s.,:@%+/\-]*$')

READ_ONLY_STATEMENTS = ('select', 'with', 'show', 'describe', 'desc', 'explain', 'values', 'table')
PAGINATED_STATEMENTS = ('select', 'with')


def _literals_re(dialect):
    return _BACKSLASH_LITERALS_RE if dialect in BACKSLASH_ESCAPE_DIALECTS else _ANSI_LITERALS_RE


def _param_re(dialect):
    return _PARAM_RE[dialect] if dialect in _PARAM_RE else _PARAM_RE[None]


def substitute_placeholders(sql, dialect):
    """Replace this project's own `:name`/`{name}` parameter markers with a harmless literal - for handing
    SQL text to a strict external parser (sqlglot, via sqlflow.py/tableguard.py) that has no notion of
    either marker convention and would otherwise misparse or reject them. Only the query's structure matters
    to those callers, never the values."""
    sql = _param_re(dialect).sub(lambda m: '1' if m.group('name') else m.group(0), sql)
    return _BRACE_RE.sub('1', sql)


def first_keyword(sql, dialect=None):
    match = re.match(r'[\s(]*([A-Za-z]+)', _literals_re(dialect).sub(' ', sql))
    return match.group(1).lower() if match else ''


def is_paginated(sql, dialect=None):
    """Whether the runner should append its own LIMIT/OFFSET. Always False for a generic 'jdbc' connection:
    that syntax is not portable across arbitrary vendors (Oracle and SQL Server use OFFSET/FETCH, not
    LIMIT), so paging for those connections instead falls back to fetching the window and slicing it in
    Python - correct for any vendor, just not server-side-optimal. See runners._JDBC."""
    return dialect != 'jdbc' and first_keyword(sql, dialect) in PAGINATED_STATEMENTS


def validate_sql(sql, dialect=None, allow_writes=None, allowed_write_ops=None, allowed_tables=None):
    """Normalise a client-supplied statement and enforce the single-statement/read-only rules.

    ``dialect`` should be the target connection's ``db`` value, so literals are read with the rules that
    database actually applies (see the module docstring above _ANSI_LITERAL). ``allow_writes`` defaults to
    the server-wide setting; a caller may pass an already-narrower effective value (e.g. a scoped API key
    without write access) but never a more permissive one - see ``engine.execute_sql``.

    ``allowed_write_ops`` (a key's own grant - see apikeys.py's module docstring) narrows *which* write
    statements are allowed once writes are otherwise permitted - e.g. INSERT but not DELETE/DROP on the
    same key. It never restricts a read-only statement, only a write one; ``None`` (the default) means no
    such narrowing, exactly today's behaviour.

    ``allowed_tables`` (a key's own grant, BACKLOG #21) narrows *which tables* a statement may touch -
    checked via tableguard.extract_tables(), which raises ApiError itself when a query's tables can't be
    verified for this dialect (only mysql/postgres/clickhouse/sqlite/duckdb are supported - see
    tableguard.py) rather than letting an unverifiable query through unchecked. ``None`` (the default) means
    no such narrowing.
    """
    if not isinstance(sql, str) or not sql.strip():
        raise ApiError('SQL query is missing', code='sql_required')
    sql = re.sub(r'[\s;]+$', '', sql.strip())
    if ';' in _literals_re(dialect).sub(' ', sql):
        raise ApiError('Only a single SQL statement can be executed at a time', code='multiple_statements')
    if not (config.allow_writes() if allow_writes is None else allow_writes):
        if first_keyword(sql, dialect) not in READ_ONLY_STATEMENTS or '/*!' in sql:
            raise ApiError('Only read-only statements (SELECT, WITH, SHOW, DESCRIBE, EXPLAIN) are allowed. '
                           'Set QUERYAPIGATE_ALLOW_WRITES=1 to lift this restriction.', 403, code='read_only')
    elif allowed_write_ops is not None:
        keyword = first_keyword(sql, dialect)
        if keyword not in READ_ONLY_STATEMENTS and keyword not in allowed_write_ops:
            raise ApiError(f"This API key may only perform these write operations: "
                           f"{', '.join(sorted(allowed_write_ops))}.", 403, code='write_op_not_allowed')
    if allowed_tables is not None:
        from . import tableguard  # deferred: keeps this module's own import surface as light as today
        found = tableguard.extract_tables(sql, dialect)                          # unless this is actually used
        forbidden = found - allowed_tables
        if forbidden:
            raise ApiError(f"This API key may only query these tables: {', '.join(sorted(allowed_tables))}. "
                           f"Forbidden: {', '.join(sorted(forbidden))}.", 403, code='table_not_allowed')
    return sql


def _window_mask(sql, dialect):
    """`sql` with the same length, comments turned into spaces and string literals/quoted identifiers into a
    filler no window pattern matches - so a trailing comment never hides a LIMIT, and a LIMIT-looking piece of
    a string is never taken for one."""
    def mask(match):
        text = match.group(0)
        return ' ' * len(text) if text.startswith(('--', '/*')) else '\0' * len(text)
    return _literals_re(dialect).sub(mask, sql)


def _window_value(token, params, clause):
    """A LIMIT/OFFSET operand as an integer: a literal, or a bound :parameter's value."""
    if not token.startswith(':'):
        return int(token)
    name = token[1:]
    value = (params or {}).get(name)
    if isinstance(value, bool) or not (isinstance(value, int) or (isinstance(value, str) and value.strip().isdigit())) \
            or int(value) < 0:
        raise ApiError(f'{clause} :{name} must be a non-negative whole number', code='param_invalid')
    return int(value)


def paginate(sql, limit, offset, params=None, dialect=None):
    """Apply the requested page window (`limit` rows from `offset`) *within* the statement's own row window.

    A statement that ends in its own LIMIT/OFFSET (any form in _TRAILING_WINDOW_RES) keeps meaning what its author
    wrote: "LIMIT 3" returns at most 3 rows however large the page, and paging moves through those rows only. The
    statement's window is replaced by the intersection of the two - offset `own_offset + offset`, at most
    `own_limit - offset` rows - so a caller's "one more row than the page" probe (has_more) is naturally false
    once the author's limit is reached. Without a window of its own, the page window is simply appended."""
    limit, offset = int(limit), int(offset)
    own_limit, own_offset = None, 0
    masked = _window_mask(sql, dialect)
    for kind, pattern in _TRAILING_WINDOW_RES:
        match = pattern.search(masked)
        if not match:
            continue
        groups = [sql[match.start(i):match.end(i)] if match.start(i) != -1 else None
                  for i in range(1, pattern.groups + 1)]
        if kind == 'offset_count':
            own_offset = _window_value(groups[0], params, 'OFFSET')
            own_limit = _window_value(groups[1], params, 'LIMIT')
        elif kind == 'limit_offset':
            own_limit = _window_value(groups[0], params, 'LIMIT') if groups[0] else None  # LIMIT ALL: no limit
            own_offset = _window_value(groups[1], params, 'OFFSET') if groups[1] else 0
        elif kind == 'offset_fetch':
            own_offset = _window_value(groups[0], params, 'OFFSET')
            if 'FETCH' in masked[match.start():].upper():
                own_limit = _window_value(groups[1], params, 'FETCH') if groups[1] else 1
        else:  # fetch
            own_limit = _window_value(groups[0], params, 'FETCH') if groups[0] else 1
        sql = sql[:match.start()]
        break
    window = limit if own_limit is None else max(0, min(limit, own_limit - offset))
    return f'{sql}\nLIMIT {window} OFFSET {own_offset + offset}'


# --------------------------------------------------------------------------------------
# Parameters
# --------------------------------------------------------------------------------------

def _check_value(name, value):
    if value is None or isinstance(value, (bool, str)):
        return
    if isinstance(value, (int, float)) and math.isfinite(value):
        return
    raise ApiError(f"Parameter '{name}' must be a string, finite number, boolean or null", code='param_invalid')


def fill_placeholders(sql, values):
    """Substitute ``{name}`` text placeholders (legacy style; prefer bound ``:name`` parameters).

    Because the value becomes part of the SQL text it is restricted to numbers, booleans and
    plain text so it cannot break out of the query. Only placeholders present in the SQL are
    validated, so the same dict can also carry values for bound parameters.
    """
    for key, value in values.items():
        token = f'{{{key}}}'
        if token not in sql:
            continue
        if isinstance(value, bool):
            text = 'TRUE' if value else 'FALSE'
        elif isinstance(value, (int, float)):
            if not math.isfinite(value):
                raise ApiError(f"Placeholder '{key}' must be a finite number", code='param_invalid')
            text = str(value)
        elif isinstance(value, str) and _SAFE_TEXT_RE.match(value) and '--' not in value:
            text = value
        else:
            raise ApiError(f"Placeholder '{key}' is substituted into the SQL text, so it must be a number, "
                           "boolean or a string containing only letters, digits, whitespace and . , : @ % + / - "
                           f"(use a bound :{key} parameter for arbitrary text)", code='param_invalid')
        sql = sql.replace(token, text)
    missing = sorted(set(_BRACE_RE.findall(sql)))
    if missing:
        raise ApiError(f"No value provided for placeholder(s): {', '.join(missing)}", code='param_required')
    return sql


def named_parameters(sql, dialect=None):
    """Names of the ``:name`` bound parameters used by ``sql``, in order of appearance."""
    return [m.group('name') for m in _param_re(dialect).finditer(sql) if m.group('name')]


def placeholder_names(sql, dialect=None):
    """Every parameter name ``sql`` uses - bound ``:name`` first, then ``{name}`` text placeholders - once each.

    app.py calls this without a dialect (bookkeeping: which declared parameters need a value, and the
    OpenAPI catalogue) even where a connection - and so a dialect - is known, because getting the dialect
    wrong here can only make that bookkeeping slightly inaccurate in a rare, contrived edge case; it never
    affects what SQL is actually sent to a database. The SQL that is actually executed always goes through
    engine.execute_sql -> validate_sql and runners.py's bind_parameters, both of which do receive the
    connection's real dialect. See the module docstring above _ANSI_LITERAL for why the dialect matters.
    """
    return list(dict.fromkeys(named_parameters(sql, dialect) + _BRACE_RE.findall(sql)))


_MARKERS = {'qmark': lambda name: '?', 'format': lambda name: '%s', 'pyformat': lambda name: f'%({name})s'}


def bind_parameters(sql, params, style, dialect=None):
    """Rewrite ``:name`` markers into a driver's paramstyle and return ``(sql, args)``.

    ``style`` is 'qmark' (``?``, positional), 'format' (``%s``, positional) or 'pyformat'
    (``%(name)s``, mapping). Values never become part of the SQL text. Returns ``(sql, None)``
    when the statement has no parameters, so drivers do not apply ``%`` formatting to it.
    """
    matches = [m for m in _param_re(dialect).finditer(sql) if m.group('name')]
    if not matches:
        return sql, None
    names = [m.group('name') for m in matches]
    missing = sorted(set(names) - set(params))
    if missing:
        raise ApiError(f"No value provided for parameter(s): {', '.join(missing)}", code='param_required')
    for name in set(names):
        _check_value(name, params[name])

    escape = (lambda text: text.replace('%', '%%')) if style in ('format', 'pyformat') else (lambda text: text)
    pieces, position = [], 0
    for match in matches:
        pieces.append(escape(sql[position:match.start()]))
        pieces.append(_MARKERS[style](match.group('name')))
        position = match.end()
    pieces.append(escape(sql[position:]))
    args = {name: params[name] for name in names} if style == 'pyformat' else [params[name] for name in names]
    return ''.join(pieces), args

"""Ties the pieces together: look up the connection, validate the SQL, run it, wrap the page."""
import hashlib
import logging
import re
import time

from . import config, limits, masking, metrics, mongotools, store, tracing
from .errors import ApiError
from .formats import ResultSetDTO
from .pool import get_pool
from .runners import (
    RUNNERS,
    STREAM_RUNNERS,
    WRITE_RUNNERS,
    TooManyRowsAffected,
    mongo_find,
    mongo_list_collections,
    mongo_list_databases,
    mongo_ping,
    mongo_stream,
)
from .sqltools import first_keyword, validate_sql

log = logging.getLogger('queryapigate')


def _sql_hash(sql):
    """A correlation key for "did this same query run elsewhere/before" without re-reading the SQL text
    itself - same idea (and same full-length sha256 hex) pool.py and cache.py already use for their own
    keys. Logged *alongside* the full SQL text (see the log.info() calls below), not instead of it - the
    hash is for a log aggregator to filter/group on; the full text is still there for a human reading logs
    locally."""
    return hashlib.sha256(sql.encode('utf-8')).hexdigest()


def _sql_connection(connection_name):
    """A connection to run SQL on - refusing, clearly, one that has no SQL (a MongoDB connection runs find()
    queries through /execute_mongo instead), rather than failing inside the driver lookup."""
    details = store.get_connection(connection_name)
    if details['db'] not in RUNNERS:
        raise ApiError(f"'{connection_name}' is a {details['db']} connection, which doesn't run SQL - use "
                       '/execute_mongo for its find() queries', code='wrong_connection_type')
    return details


def _table_defaults(details):
    """The (catalog, schema) that fill a reference's or grant's missing parts for qualified `allowed_tables` matching
    (tableguard.py): what an unqualified table name resolves to on this connection. Trino's catalog is the
    connection's `database` (ADR 0026); elsewhere the catalog is left to the reference, and the schema is MySQL's or
    ClickHouse's database, PostgreSQL's `schema` option (`public` unless set - see _search_path()), SQLite's and
    DuckDB's `main`."""
    dialect = details.get('db')
    if dialect == 'trino':
        return details.get('database'), (details.get('schema') or 'default')
    if dialect == 'mysql':
        return None, details.get('database')
    if dialect == 'clickhouse':
        return None, details.get('database') or 'default'
    if dialect == 'postgres':
        return None, details.get('schema') or 'public'
    return None, 'main'


def _search_path(details, allowed_tables):
    """The runner options pinning what a table-restricted PostgreSQL call's unqualified names resolve to: its
    `search_path` set, inside the call, to the schema tableguard assumed - so neither a role's own search_path nor a
    `"$user"` schema can send `customer` somewhere other than the `public.customer` the grant was checked against."""
    if details['db'] != 'postgres' or allowed_tables is None:
        return {}
    return {'search_path': _table_defaults(details)[1]}


def _db_span(details, sql, connection_name):
    """The client span around a database call (tracing.py): which statement kind, on which database - never the
    statement itself."""
    kind = first_keyword(sql, details['db'])
    return tracing.span(f'{kind.upper() or "SQL"} {connection_name}', 'client',
                        tracing.db_attributes(details, kind, connection_name))


def logged_sql(sql):
    """A statement as the log shows it: its text - or, when QUERYAPIGATE_HISTORY_ADHOC_SQL keeps SQL text out of run
    history (hash, none), out of the log too, by its SHA-256 (ADR 0016): an ad-hoc statement, or a legacy {name}
    placeholder, can carry a value in its text."""
    return sql if config.history_adhoc_sql() == 'text' else f'(text not logged; sha256 {_sql_hash(sql)})'


def execute_sql(sql, connection_name, limit, offset, params=None, timeout=None, allow_writes=True, key_name='-',
                allowed_write_ops=None, database=None, allowed_tables=None, mask=None):
    """Run ``sql`` on a named connection and return the requested page as a ResultSetDTO.

    ``allow_writes`` is the caller's own permission (e.g. a scoped API key); the connection is only ever
    writable when both that *and* the server-wide QUERYAPIGATE_ALLOW_WRITES allow it - a caller can narrow the
    server's setting, never widen it. ``key_name`` is only for the query counter in ``/metrics`` (audit: which
    key touched which connection); it plays no part in what the query is allowed to do. ``allowed_write_ops``
    is a key's own narrower allow-list of write keywords, if it has one - see ``sqltools.validate_sql()``.
    ``allowed_tables`` is a key's own narrower allow-list of tables it may query, if it has one - see
    ``sqltools.validate_sql()``/``tableguard.py``. ``database`` overrides the connection's own configured
    database for just this call - Run SQL's "browse a different database on this same server" picker (see
    schema.fetch_schema()); a stored, scoped API key can never send this itself (there is no request field
    for it), only the admin UI's own ad-hoc calls do.
    """
    details = _sql_connection(connection_name)
    if database:
        details = {**details, 'database': database}
    effective_allow_writes = config.allow_writes() and allow_writes
    default_catalog, default_schema = _table_defaults(details)
    sql = validate_sql(sql, dialect=details['db'], allow_writes=effective_allow_writes,
                       allowed_write_ops=allowed_write_ops, allowed_tables=allowed_tables,
                       default_catalog=default_catalog, default_schema=default_schema)
    # The caller's column-masking rules (masking.py): traced before the query runs, so a query masking can't follow
    # is refused without touching the database.
    masked = masking.prepare(mask, sql, connection_name, details['db'], database)
    # The caller's own limits (limits.py, ADR 0011), within the server's
    timeout, limit = limits.timeout(timeout), limits.rows(limit)
    if config.log_requests():
        log.info('Executing on %s (%s), limit=%s offset=%s timeout=%s: %s',
                 connection_name, details['db'], limit, offset, timeout, logged_sql(sql),
                 extra={'connection': connection_name, 'dialect': details['db'], 'limit': limit, 'offset': offset,
                       'timeout': timeout, 'sql_hash': _sql_hash(sql)})
    started = time.monotonic()
    status = 'error'
    metrics.inc_active_query()
    try:
        with limits.acquire(timeout), _db_span(details, sql, connection_name) as span:
            columns, rows = RUNNERS[details['db']](details, sql, params, limit, offset, not effective_allow_writes,
                                                 timeout, get_pool(), **_search_path(details, allowed_tables))
            tracing.set_attributes(span, db__response__returned_rows=min(len(rows), limit))
        status = 'success'
    except ApiError:
        raise
    except ImportError as error:
        log.exception('Missing database driver')
        raise ApiError(f"The driver for '{details['db']}' is not installed", 500, detail=str(error),
                       code='driver_missing') from error
    except Exception as error:
        log.exception('Query on %s failed', connection_name)
        raise _failure(error, 'An error occurred while executing the SQL query', details) from error
    finally:
        metrics.dec_active_query()
        elapsed = time.monotonic() - started
        metrics.observe_query(connection_name, details['db'], status, elapsed, key_name)
        threshold = config.slow_query_threshold()
        if threshold and elapsed >= threshold:
            log.warning('Slow query on %s (%s): %.1fms - %s', connection_name, details['db'], elapsed * 1000,
                        logged_sql(sql),
                       extra={'connection': connection_name, 'dialect': details['db'],
                              'duration_ms': round(elapsed * 1000, 1)})
    has_more = len(rows) > limit
    result_rows = rows[:limit]
    metrics.observe_rows(connection_name, details['db'], key_name, len(result_rows))
    if masked is not None:
        columns, result_rows = masked.apply(columns, list(result_rows))
    result = ResultSetDTO(result_rows, columns, has_more=has_more)
    result.masked = masked.fired if masked is not None else []
    return result


def execute_mongo(collection, filter_doc, connection_name, limit, offset, params=None, timeout=None,
                  projection=None, sort=None, key_name='-', mask=None, allowed_tables=None):
    """Run a Mongo find() on a named connection and return the requested page as a ResultSetDTO - the
    non-SQL sibling of execute_sql(). Always read-only: MongoDB support is find-only in this version (see
    mongotools.py and BACKLOG #36), so unlike execute_sql there is no allow_writes/allowed_write_ops
    parameter at all - there is no write path to gate. ``params`` resolves any ``:name`` placeholders in
    ``filter_doc`` via mongotools.fill_placeholders(), the JSON-document equivalent of execute_sql's bound
    :name parameters.

    Mongo documents are schemaless, so unlike execute_sql (whose driver already returns a fixed column
    list) the (columns, rows) pair ResultSetDTO expects is built here: the union of keys across the fetched
    page, in first-seen order, becomes ``columns``, and each document becomes a positionally-aligned row -
    lossy for wildly different document shapes, but keeps every existing exporter (json/ndjson/csv/xlsx/
    xml/yaml) working unmodified.

    ``allowed_tables`` (a key's grant, lower-case names) is, on MongoDB, the collections it may read - checked
    before anything is asked of the server, as validate_sql() checks a statement's tables.
    """
    if allowed_tables is not None and str(collection).lower() not in allowed_tables:
        raise ApiError(f"This API key may only query these tables: {', '.join(sorted(allowed_tables))}. "
                       f'Forbidden: {collection}.', 403, code='table_not_allowed')
    masking.prepare(mask, '', connection_name, 'mongo')  # refused for a caller with rules, for now
    details = store.get_connection(connection_name)
    if details['db'] != 'mongo':
        raise ApiError(f"'{connection_name}' is not a mongo connection", code='wrong_connection_type')
    filter_doc = mongotools.validate_filter(filter_doc or {})
    mongotools.validate_options(projection, sort)
    timeout, limit = limits.timeout(timeout), limits.rows(limit)
    if params:
        filter_doc = mongotools.fill_placeholders(filter_doc, params)
    if config.log_requests():
        log.info('Finding on %s (mongo), collection=%s limit=%s offset=%s timeout=%s',
                 connection_name, collection, limit, offset, timeout,
                 extra={'connection': connection_name, 'dialect': 'mongo', 'limit': limit, 'offset': offset,
                       'timeout': timeout})
    started = time.monotonic()
    status = 'error'
    metrics.inc_active_query()
    try:
        with limits.acquire(timeout), tracing.span(f'find {connection_name}', 'client',
                                                   tracing.db_attributes(details, 'find', connection_name)) as span:
            docs = mongo_find(details, collection, filter_doc, projection, sort, limit, offset, timeout, get_pool())
            tracing.set_attributes(span, db__collection__name=collection,
                                   db__response__returned_rows=min(len(docs), limit))
        status = 'success'
    except ApiError:
        raise
    except ImportError as error:
        log.exception('Missing database driver')
        raise ApiError("The driver for 'mongo' is not installed", 500, detail=str(error),
                       code='driver_missing') from error
    except Exception as error:
        log.exception('Find on %s failed', connection_name)
        raise _failure(error, 'An error occurred while executing the find query', details) from error
    finally:
        metrics.dec_active_query()
        elapsed = time.monotonic() - started
        metrics.observe_query(connection_name, 'mongo', status, elapsed, key_name)
        threshold = config.slow_query_threshold()
        if threshold and elapsed >= threshold:
            log.warning('Slow find on %s (mongo): %.1fms', connection_name, elapsed * 1000,
                       extra={'connection': connection_name, 'dialect': 'mongo',
                              'duration_ms': round(elapsed * 1000, 1)})
    has_more = len(docs) > limit
    result_docs = docs[:limit]
    columns = list(dict.fromkeys(key for doc in result_docs for key in doc.keys()))
    rows = [[doc.get(col) for col in columns] for doc in result_docs]
    metrics.observe_rows(connection_name, 'mongo', key_name, len(result_docs))
    return ResultSetDTO(rows, columns, has_more=has_more)


# How drivers say they could not reach the database at all, as opposed to the database refusing the query. Each
# driver has its own exception classes, so this goes by what they say; an OSError anywhere in the chain (refused,
# unreachable, unknown host, timed out) is a connection problem whatever the driver.
_CONNECTION_MESSAGES = (
    "can't connect", 'could not connect', 'unable to connect', 'failed to connect', 'connection refused',
    'connection timed out', 'timeout expired', 'name or service not known', 'nodename nor servname',
    'could not translate host name', 'unknown mysql server host', 'lost connection to mysql server during query',
    'server closed the connection unexpectedly', 'no servers found', 'serverselectiontimeouterror',
    'unable to open database file', 'network is unreachable', 'no route to host',
)


def is_connection_failure(error):
    """Whether a driver error means the database could not be reached, rather than that it rejected the query."""
    seen = error
    for _ in range(8):  # an exception's causes, not just the exception
        if seen is None:
            break
        if isinstance(seen, OSError):
            return True
        text = f'{type(seen).__name__} {seen}'.lower()
        if any(message in text for message in _CONNECTION_MESSAGES):
            return True
        seen = seen.__cause__ or seen.__context__
    return False


def _failure(error, message, details):
    """The ApiError for a run that failed in the driver: connection_failed (502) when the database couldn't be
    reached - so callers, history and alerts (a failing connection) can tell that from bad SQL - else query_failed."""
    if is_connection_failure(error):
        return ApiError('Could not connect to the database', 502,
                        detail=_redact_password(str(error), details.get('password')), code='connection_failed')
    return ApiError(message, 500, detail=str(error), code='query_failed')


def test_connection(details):
    """Try to actually connect to and query ``details`` - a connection's fields as an admin is about to save
    them, not yet written anywhere. Used by ``POST /api/v1/connections/test`` so a typo'd host or a firewalled port
    is found out before saving, not on the query that comes after. Always read-only, regardless of
    QUERYAPIGATE_ALLOW_WRITES: a connectivity probe has no business writing anything. Bypasses execute_sql
    entirely - no metrics, no audit entry, no named connection to look up - this is a one-off, not a served
    request, but it does share the normal connection pool, so a passing test can leave behind a warm
    connection the first real query then reuses."""
    if details.get('db') not in RUNNERS and details.get('db') != 'mongo':
        raise ApiError(f"'db' must be one of: {', '.join((*RUNNERS, 'mongo'))}", code='unsupported_database')
    started = time.monotonic()
    try:
        if details['db'] == 'mongo':
            mongo_ping(details, get_pool())
        else:
            RUNNERS[details['db']](details, 'SELECT 1', None, 1, 0, True, config.CONNECT_TIMEOUT, get_pool())
    except ImportError as error:
        raise ApiError(f"The driver for '{details['db']}' is not installed", 500, detail=str(error),
                       code='driver_missing') from error
    except Exception as error:
        detail = _redact_password(str(error), details.get('password'))
        raise ApiError('Could not connect', 502, detail=detail, code='connection_failed') from error
    return {'elapsed_ms': round((time.monotonic() - started) * 1000, 1)}


def _redact_password(message, password):
    """A driver's own error text sometimes echoes back the connection string it tried - never let that hand
    a real password back to whoever is testing the connection."""
    return message.replace(str(password), '********') if password else message


# Every database on the server, for a dialect that actually has more than one - sqlite and duckdb are a
# single file (there is no "other database" on the same server to list), and h2 and jdbc have no one
# catalogue query that works for every database reachable that way, so listing there is a clear, named-dialect
# ApiError rather than a best-effort guess. schema.list_databases() re-exposes this same dict for an
# already-saved connection, so the two paths can never drift apart on which dialects are supported.
LIST_DATABASES_QUERIES = {
    'mysql': 'SELECT schema_name AS name FROM information_schema.schemata ORDER BY schema_name',
    'postgres': 'SELECT datname AS name FROM pg_database WHERE NOT datistemplate ORDER BY datname',
    'clickhouse': 'SELECT name FROM system.databases ORDER BY name',
    'trino': 'SHOW CATALOGS',  # Trino's "databases" are catalogs; switching database switches catalog (ADR 0026)
}
# PostgreSQL has no server-wide connection, only a connection to one specific database - so listing every
# *other* database first needs some database that (almost) always exists to connect to. MySQL and ClickHouse
# have no such requirement; they are omitted here on purpose.
_LIST_DATABASES_BOOTSTRAP = {'postgres': 'postgres', 'trino': 'system'}  # Trino's `system` catalog always exists


def list_databases(details):
    """Every database on the server ``details`` points at - the New/Edit connection form's "Default database"
    dropdown (before the connection is even saved, so there is no name to look up yet) and, for an
    already-saved connection, Run SQL's own "browse a different database on this server" picker (via
    schema.list_databases()). Bypasses execute_sql like test_connection() does, for the same reason: this is
    metadata about the server, not a governed query against one specific, already-chosen database."""
    dialect = details.get('db')
    if dialect == 'mongo':
        try:
            return mongo_list_databases(details, get_pool())
        except ImportError as error:
            raise ApiError("The driver for 'mongo' is not installed", 500, detail=str(error),
                           code='driver_missing') from error
        except Exception as error:
            detail = _redact_password(str(error), details.get('password'))
            raise ApiError('Could not list databases', 502, detail=detail, code='connection_failed') from error
    query = LIST_DATABASES_QUERIES.get(dialect)
    if not query:
        raise ApiError(f"Listing databases isn't supported for '{dialect}' connections", code='unsupported_operation')
    probe = details if details.get('database') else {**details, 'database': _LIST_DATABASES_BOOTSTRAP.get(dialect, '')}
    try:
        _columns, rows = RUNNERS[dialect](probe, query, None, 1000, 0, True, config.CONNECT_TIMEOUT, get_pool())
    except ImportError as error:
        raise ApiError(f"The driver for '{dialect}' is not installed", 500, detail=str(error),
                       code='driver_missing') from error
    except Exception as error:
        detail = _redact_password(str(error), details.get('password'))
        raise ApiError('Could not list databases', 502, detail=detail, code='connection_failed') from error
    return [row[0] for row in rows]


def list_collections(details):
    """Collection names in the database ``details`` points at - schema.fetch_schema()'s mongo case, the
    non-relational sibling of _QUERIES' information_schema-shaped catalogue queries in schema.py."""
    try:
        return mongo_list_collections(details, get_pool())
    except ImportError as error:
        raise ApiError("The driver for 'mongo' is not installed", 500, detail=str(error),
                       code='driver_missing') from error
    except Exception as error:
        detail = _redact_password(str(error), details.get('password'))
        raise ApiError('Could not list collections', 502, detail=detail, code='connection_failed') from error


def execute_agent_write(sql, connection_name, params, max_affected_rows, timeout=None, key_name='-',
                        allowed_write_ops=None, allowed_tables=None, mask=None):
    """An AI agent's write through a saved query (ADR 0008): checked as every statement is - the server's write
    ceiling, the caller's allowed_write_ops and allowed_tables, masking on what a RETURNING clause gives back - then run
    in a transaction that is committed only if it changed at most `max_affected_rows` rows. Returns (ResultSetDTO,
    rows changed); more rows, and it is rolled back and refused with too_many_rows_affected."""
    details = _sql_connection(connection_name)
    if details['db'] not in WRITE_RUNNERS:
        raise ApiError(f"Agents can't write on '{details['db']}' connections", 403, code='read_only')
    default_catalog, default_schema = _table_defaults(details)
    sql = validate_sql(sql, dialect=details['db'], allow_writes=config.allow_writes(),
                       allowed_write_ops=allowed_write_ops, allowed_tables=allowed_tables,
                       default_catalog=default_catalog, default_schema=default_schema)
    # Only rows a RETURNING clause gives back can carry a masked value; a write without one returns none.
    returns_rows = re.search(r'\bRETURNING\b', sql, re.IGNORECASE) is not None
    masked = masking.prepare(mask, sql, connection_name, details['db'], None) if mask and returns_rows else None
    timeout = limits.timeout(timeout)
    log.info('Agent write on %s (%s), at most %s rows: %s', connection_name, details['db'], max_affected_rows,
             logged_sql(sql),
             extra={'connection': connection_name, 'dialect': details['db'], 'sql_hash': _sql_hash(sql)})
    started = time.monotonic()
    status = 'error'
    metrics.inc_active_query()
    try:
        with limits.acquire(timeout), _db_span(details, sql, connection_name) as span:
            columns, rows, affected = WRITE_RUNNERS[details['db']](details, sql, params, max_affected_rows, timeout,
                                                                   get_pool(), **_search_path(details, allowed_tables))
            tracing.set_attributes(span, db__response__affected_rows=affected)
        status = 'success'
    except TooManyRowsAffected as error:
        raise ApiError(f'This write would have changed {error.affected} rows, and this query may change at most '
                       f'{error.ceiling} - nothing was changed', 409, code='too_many_rows_affected') from None
    except ApiError:
        raise
    except ImportError as error:
        raise ApiError(f"The driver for '{details['db']}' is not installed", 500, detail=str(error),
                       code='driver_missing') from error
    except Exception as error:
        log.exception('Agent write on %s failed', connection_name)
        raise _failure(error, 'An error occurred while executing the SQL query', details) from error
    finally:
        metrics.dec_active_query()
        metrics.observe_query(connection_name, details['db'], status, time.monotonic() - started, key_name)
    if masked is not None:
        columns, rows = masked.apply(columns, list(rows))
    result = ResultSetDTO(list(rows), columns, has_more=False)
    result.masked = masked.fired if masked is not None else []
    return result, affected


def stream_sql(sql, connection_name, params=None, timeout=None, key_name='-', allowed_tables=None, mask=None):
    """Like execute_sql, but for the whole result rather than one page - and, unlike execute_sql, always
    read-only regardless of QUERYAPIGATE_ALLOW_WRITES or the caller's own permission. A large export has no
    business mutating data, and forcing this sidesteps a lot of incidental complexity around commit timing
    on a connection that may stay checked out for a long time - see runners.py's "Streaming" section for
    what that already involves per dialect without adding writes into the mix too.

    ``allowed_tables`` is a key's own narrower allow-list of tables it may query, if it has one - see
    ``sqltools.validate_sql()``/``tableguard.py``.

    Returns (columns, rows): ``columns`` is available immediately (the underlying generator is primed once
    to get it, surfacing a connection or SQL error here just like execute_sql does), ``rows`` is a lazy
    generator - the connection checked out (or freshly opened) for it is released only once that generator
    is exhausted, errors, or a client disconnect closes it early (see runners._make_stream_runner).
    """
    details = _sql_connection(connection_name)
    default_catalog, default_schema = _table_defaults(details)
    sql = validate_sql(sql, dialect=details['db'], allow_writes=False, allowed_tables=allowed_tables,
                       default_catalog=default_catalog, default_schema=default_schema)
    masked = masking.prepare(mask, sql, connection_name, details['db'])  # before the query runs, as execute_sql's
    timeout = limits.timeout(timeout)
    row_limit = limits.rows(config.stream_max_rows())  # read now, in the request: the rows are read after it returns
    slot = limits.acquire(timeout)  # held until the last row is read - _drain() or _Rows.close() releases it
    if config.log_requests():
        log.info('Streaming from %s (%s): %s', connection_name, details['db'], logged_sql(sql),
                 extra={'connection': connection_name, 'dialect': details['db'], 'sql_hash': _sql_hash(sql)})
    status = 'error'
    metrics.inc_active_query()
    try:
        # the span covers opening the stream - up to its first row's columns - not the hours a large export may take
        with _db_span(details, sql, connection_name):
            stream = STREAM_RUNNERS[details['db']](details, sql, params, timeout, get_pool(),
                                                   **_search_path(details, allowed_tables))
            columns = next(stream)
        status = 'success'
    except ApiError:
        raise
    except ImportError as error:
        log.exception('Missing database driver')
        raise ApiError(f"The driver for '{details['db']}' is not installed", 500, detail=str(error),
                       code='driver_missing') from error
    except Exception as error:
        log.exception('Streaming query on %s failed', connection_name)
        raise _failure(error, 'An error occurred while executing the SQL query', details) from error
    finally:
        # On success, the query stays active until _drain() below finishes consuming it - the decrement
        # (and the row/stream-status metrics) move there with it, not here.
        if status == 'error':
            metrics.observe_stream(connection_name, details['db'], status, key_name)
            metrics.dec_active_query()
            slot.release()
    rows = _Rows(stream, connection_name, details['db'], key_name, slot, row_limit)
    if masked is None:
        rows.masked = []
        return columns, rows
    try:
        columns, masked_rows = masked.apply(columns, rows)
    except ApiError:
        rows.close()  # the result didn't match what was traced: end the query, return its connection
        raise
    masked_rows.masked = masked.fired
    return columns, masked_rows


def stream_mongo(collection, filter_doc, connection_name, params=None, timeout=None, projection=None, sort=None,
                 key_name='-', allowed_tables=None, mask=None):
    """stream_sql() for a find() (ADR 0022): the same checks as execute_mongo(), the same bookkeeping as a SQL
    stream - the caller's slot held, the stream row limit, the active-query count, the rows counted as they leave.
    Returns the documents, lazily, as a _Rows: each a dict, as the server sent it after the projection. Turning them
    into columns is the format's business (mongodocs.py), since documents don't share a schema."""
    if allowed_tables is not None and str(collection).lower() not in allowed_tables:
        raise ApiError(f"This API key may only query these tables: {', '.join(sorted(allowed_tables))}. "
                       f'Forbidden: {collection}.', 403, code='table_not_allowed')
    masking.prepare(mask, '', connection_name, 'mongo')  # refused for a caller with rules, as execute_mongo()
    details = store.get_connection(connection_name)
    if details['db'] != 'mongo':
        raise ApiError(f"'{connection_name}' is not a mongo connection", code='wrong_connection_type')
    filter_doc = mongotools.validate_filter(filter_doc or {})
    mongotools.validate_options(projection, sort)
    if params:
        filter_doc = mongotools.fill_placeholders(filter_doc, params)
    timeout = limits.timeout(timeout)
    row_limit = limits.rows(config.stream_max_rows())  # read now, in the request: the rows are read after it returns
    slot = limits.acquire(timeout)  # held until the last document is read - _drain() or _Rows.close() releases it
    if config.log_requests():
        log.info('Streaming a find from %s (mongo), collection=%s', connection_name, collection,
                 extra={'connection': connection_name, 'dialect': 'mongo', 'timeout': timeout})
    status = 'error'
    metrics.inc_active_query()
    try:
        with tracing.span(f'find {connection_name}', 'client',
                          tracing.db_attributes(details, 'find', connection_name)) as span:
            tracing.set_attributes(span, db__collection__name=collection)
            stream = mongo_stream(details, collection, filter_doc, projection, sort, timeout, get_pool())
            next(stream)  # the first batch: a failure here is an error response, not a stream cut short
        status = 'success'
    except ApiError:
        raise
    except ImportError as error:
        log.exception('Missing database driver')
        raise ApiError("The driver for 'mongo' is not installed", 500, detail=str(error),
                       code='driver_missing') from error
    except Exception as error:
        log.exception('Streaming find on %s failed', connection_name)
        raise _failure(error, 'An error occurred while executing the find query', details) from error
    finally:
        if status == 'error':  # on success, as in stream_sql(), _drain() does this once the stream ends
            metrics.observe_stream(connection_name, 'mongo', status, key_name)
            metrics.dec_active_query()
            slot.release()
    rows = _Rows(stream, connection_name, 'mongo', key_name, slot, row_limit)
    rows.masked = []
    return rows


class _Rows:
    """The rows stream_sql() returns: an iterator over _drain(), whose close() ends the query even before the first
    row is read. Closing a generator that hasn't started runs none of its code, so a caller that gave up first - an
    export whose destination failed, a client gone before the first chunk - used to leave the query counted as
    active and its connection checked out until garbage collection."""

    def __init__(self, stream, connection_name, dialect, key_name, slot=limits.NO_SLOT, row_limit=None):
        self._stream = stream  # already primed by stream_sql(): its own cleanup runs when it is closed
        self._where = (connection_name, dialect, key_name)
        self._slot = slot
        self._drain = _drain(stream, connection_name, dialect, key_name, slot, row_limit)
        self._started = False
        self._closed = False

    def __iter__(self):
        return self

    def __next__(self):
        self._started = True
        return next(self._drain)

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self._started:
            self._drain.close()  # _drain's own GeneratorExit handling does the bookkeeping
            # _drain reads the stream with a for loop, and closing it doesn't close what it was reading: the
            # connection (a MongoDB cursor, on the server) would otherwise wait for garbage collection
            self._stream.close()
            return
        self._stream.close()  # releases the connection (runners._make_stream_runner)
        self._slot.release()
        connection_name, dialect, key_name = self._where
        metrics.observe_rows(connection_name, dialect, key_name, 0)
        metrics.dec_active_query()


def _drain(rows, connection_name, dialect, key_name, slot=limits.NO_SLOT, row_limit=None):
    """Wraps the row generator stream_sql() returns: records the export's final status once it is fully
    consumed (success) or fails partway through (error - client disconnects are not failures, so
    GeneratorExit is excluded), and makes sure a mid-stream failure lands in the log. Once the response has
    started, a failure here can no longer change its status or body shape - the client just sees the
    connection end early - so logging clearly is the most this can do about it. Also where the query
    started by stream_sql() actually stops being "active" (see inc_active_query() there), and where rows
    that made it out are counted - even a partial count on failure or disconnect is data that left the
    server, not nothing.

    Also enforces QUERYAPIGATE_STREAM_MAX_ROWS (config.stream_max_rows()), if set: unlike a paged response,
    ?stream=true has no ceiling of its own otherwise - the whole point is not buffering the result, so
    nothing else naturally bounds how much a single export can return. Breaking out of the ``for`` loop
    below (rather than letting it run to exhaustion) still reaches the ``else`` clause normally - a ``break``
    only skips a ``for``'s own ``else``, not the surrounding ``try``'s - so the usual success bookkeeping
    still runs; ``rows.close()`` is what actually releases the still-open connection early, the same
    GeneratorExit-based cleanup a client disconnecting mid-stream already triggers (see
    runners._make_stream_runner), just initiated from here instead of by the client closing the response.
    """
    limit = row_limit  # QUERYAPIGATE_STREAM_MAX_ROWS, or the caller's smaller max_rows - read by stream_sql()
    truncated = False
    count = 0
    try:
        for row in rows:
            count += 1
            slot.renew()
            yield row
            if limit is not None and count >= limit:
                truncated = True
                break
    except GeneratorExit:
        metrics.observe_rows(connection_name, dialect, key_name, count)
        metrics.dec_active_query()
        raise
    except Exception:
        log.exception('Streaming from %s failed partway through', connection_name)
        metrics.observe_stream(connection_name, dialect, 'error', key_name)
        metrics.observe_rows(connection_name, dialect, key_name, count)
        metrics.dec_active_query()
        raise
    else:
        if truncated:
            rows.close()
            log.warning('Streaming from %s truncated at %s rows (QUERYAPIGATE_STREAM_MAX_ROWS or max_rows)',
                       connection_name, limit,
                       extra={'connection': connection_name, 'dialect': dialect, 'row_limit': limit})
        metrics.observe_stream(connection_name, dialect, 'truncated' if truncated else 'success', key_name)
        metrics.observe_rows(connection_name, dialect, key_name, count)
        metrics.dec_active_query()
    finally:
        slot.release()  # however it ended: the last row read, a failure, the client gone, the row limit


def timed(func, *args, **kwargs):
    """Call ``func`` and return (result, elapsed milliseconds)."""
    started = time.monotonic()
    result = func(*args, **kwargs)
    return result, round((time.monotonic() - started) * 1000)

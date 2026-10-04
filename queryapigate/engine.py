"""Ties the pieces together: look up the connection, validate the SQL, run it, wrap the page."""
import hashlib
import logging
import time

from . import config, metrics, mongotools, store
from .errors import ApiError
from .formats import ResultSetDTO
from .pool import get_pool
from .runners import RUNNERS, STREAM_RUNNERS, mongo_find, mongo_list_collections, mongo_list_databases, mongo_ping
from .sqltools import validate_sql

log = logging.getLogger('queryapigate')


def _sql_hash(sql):
    """A correlation key for "did this same query run elsewhere/before" without re-reading the SQL text
    itself - same idea (and same full-length sha256 hex) pool.py and cache.py already use for their own
    keys. Logged *alongside* the full SQL text (see the log.info() calls below), not instead of it - the
    hash is for a log aggregator to filter/group on; the full text is still there for a human reading logs
    locally."""
    return hashlib.sha256(sql.encode('utf-8')).hexdigest()


def execute_sql(sql, connection_name, limit, offset, params=None, timeout=None, allow_writes=True, key_name='-',
                allowed_write_ops=None, database=None, allowed_tables=None):
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
    details = store.get_connection(connection_name)
    if database:
        details = {**details, 'database': database}
    effective_allow_writes = config.allow_writes() and allow_writes
    sql = validate_sql(sql, dialect=details['db'], allow_writes=effective_allow_writes,
                       allowed_write_ops=allowed_write_ops, allowed_tables=allowed_tables)
    log.info('Executing on %s (%s), limit=%s offset=%s timeout=%s: %s',
             connection_name, details['db'], limit, offset, timeout, sql,
             extra={'connection': connection_name, 'dialect': details['db'], 'limit': limit, 'offset': offset,
                   'timeout': timeout, 'sql_hash': _sql_hash(sql)})
    started = time.monotonic()
    status = 'error'
    metrics.inc_active_query()
    try:
        columns, rows = RUNNERS[details['db']](details, sql, params, limit, offset, not effective_allow_writes,
                                             timeout, get_pool())
        status = 'success'
    except ApiError:
        raise
    except ImportError as error:
        log.exception('Missing database driver')
        raise ApiError(f"The driver for '{details['db']}' is not installed", 500, detail=str(error),
                       code='driver_missing') from error
    except Exception as error:
        log.exception('Query on %s failed', connection_name)
        raise ApiError('An error occurred while executing the SQL query', 500, detail=str(error),
                       code='query_failed') from error
    finally:
        metrics.dec_active_query()
        elapsed = time.monotonic() - started
        metrics.observe_query(connection_name, details['db'], status, elapsed, key_name)
        threshold = config.slow_query_threshold()
        if threshold and elapsed >= threshold:
            log.warning('Slow query on %s (%s): %.1fms - %s', connection_name, details['db'], elapsed * 1000, sql,
                       extra={'connection': connection_name, 'dialect': details['db'],
                              'duration_ms': round(elapsed * 1000, 1)})
    has_more = len(rows) > limit
    result_rows = rows[:limit]
    metrics.observe_rows(connection_name, details['db'], key_name, len(result_rows))
    return ResultSetDTO(result_rows, columns, has_more=has_more)


def execute_mongo(collection, filter_doc, connection_name, limit, offset, params=None, timeout=None,
                  projection=None, sort=None, key_name='-'):
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
    """
    details = store.get_connection(connection_name)
    if details['db'] != 'mongo':
        raise ApiError(f"'{connection_name}' is not a mongo connection", code='wrong_connection_type')
    filter_doc = mongotools.validate_filter(filter_doc or {})
    if params:
        filter_doc = mongotools.fill_placeholders(filter_doc, params)
    log.info('Finding on %s (mongo), collection=%s limit=%s offset=%s timeout=%s',
             connection_name, collection, limit, offset, timeout,
             extra={'connection': connection_name, 'dialect': 'mongo', 'limit': limit, 'offset': offset,
                   'timeout': timeout})
    started = time.monotonic()
    status = 'error'
    metrics.inc_active_query()
    try:
        docs = mongo_find(details, collection, filter_doc, projection, sort, limit, offset, timeout, get_pool())
        status = 'success'
    except ApiError:
        raise
    except ImportError as error:
        log.exception('Missing database driver')
        raise ApiError("The driver for 'mongo' is not installed", 500, detail=str(error),
                       code='driver_missing') from error
    except Exception as error:
        log.exception('Find on %s failed', connection_name)
        raise ApiError('An error occurred while executing the find query', 500, detail=str(error),
                       code='query_failed') from error
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
}
# PostgreSQL has no server-wide connection, only a connection to one specific database - so listing every
# *other* database first needs some database that (almost) always exists to connect to. MySQL and ClickHouse
# have no such requirement; they are omitted here on purpose.
_LIST_DATABASES_BOOTSTRAP = {'postgres': 'postgres'}


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


def stream_sql(sql, connection_name, params=None, timeout=None, key_name='-', allowed_tables=None):
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
    details = store.get_connection(connection_name)
    sql = validate_sql(sql, dialect=details['db'], allow_writes=False, allowed_tables=allowed_tables)
    log.info('Streaming from %s (%s): %s', connection_name, details['db'], sql,
             extra={'connection': connection_name, 'dialect': details['db'], 'sql_hash': _sql_hash(sql)})
    status = 'error'
    metrics.inc_active_query()
    try:
        stream = STREAM_RUNNERS[details['db']](details, sql, params, timeout, get_pool())
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
        raise ApiError('An error occurred while executing the SQL query', 500, detail=str(error),
                       code='query_failed') from error
    finally:
        # On success, the query stays active until _drain() below finishes consuming it - the decrement
        # (and the row/stream-status metrics) move there with it, not here.
        if status == 'error':
            metrics.observe_stream(connection_name, details['db'], status, key_name)
            metrics.dec_active_query()
    return columns, _drain(stream, connection_name, details['db'], key_name)


def _drain(rows, connection_name, dialect, key_name):
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
    limit = config.stream_max_rows()
    truncated = False
    count = 0
    try:
        for row in rows:
            count += 1
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
            log.warning('Streaming from %s truncated at %s rows (QUERYAPIGATE_STREAM_MAX_ROWS)',
                       connection_name, limit,
                       extra={'connection': connection_name, 'dialect': dialect, 'row_limit': limit})
        metrics.observe_stream(connection_name, dialect, 'truncated' if truncated else 'success', key_name)
        metrics.observe_rows(connection_name, dialect, key_name, count)
        metrics.dec_active_query()


def timed(func, *args, **kwargs):
    """Call ``func`` and return (result, elapsed milliseconds)."""
    started = time.monotonic()
    result = func(*args, **kwargs)
    return result, round((time.monotonic() - started) * 1000)

"""One runner per database type: connect (or borrow a pooled connection), run one statement, return (columns, rows).

Runners fetch ``limit + 1`` rows: the extra row is how the caller learns whether another page exists.
Database drivers are imported lazily so only the ones you actually use need to be installed.

Each network database is a small ``_Driver`` subclass. ``_make_runner`` turns it into the callable stored in
``RUNNERS``, which either opens a connection for the single call or borrows one from the pool.
"""
import atexit
import logging
import math
import os
import re
import sqlite3
import threading
import time
from pathlib import Path

from . import config, duckfiles, store
from .errors import ApiError
from .pool import Session, close_pooled_connections
from .sqltools import bind_parameters, is_paginated, paginate

log = logging.getLogger('queryapigate')


def _timed_out(timeout):
    return ApiError(f'The query exceeded the time limit of {timeout:g} seconds and was cancelled', 504,
                    timeout=timeout, code='query_timeout')


def _millis(timeout):
    return max(1, int(timeout * 1000))


# A connection's TLS settings each driver is given (as `options`, or alongside the standard fields) - only these: other
# driver parameters can make it read files of the server's (libpq's passfile and service, for one).
POSTGRES_TLS = ('sslmode', 'sslrootcert', 'sslcert', 'sslkey', 'sslcrl')
MYSQL_TLS = ('ssl_ca', 'ssl_cert', 'ssl_key', 'ssl_verify_cert', 'ssl_verify_identity', 'ssl_disabled', 'auth_plugin')


def _connect_args(details, tls=(), **renames):
    """Pick the standard connection fields out of a stored connection, renaming keys per driver - and its TLS
    settings, `tls`."""
    args = {}
    for key in ('host', 'port', 'user', 'password', 'database', *tls):
        if details.get(key) not in (None, ''):
            args[renames.get(key, key)] = details[key]
    return args


def _resolve_db_file(details, label):
    """The local file path for a `database` connection field: resolved against QUERYAPIGATE_HOME, and required
    to already exist - so a typo in the path 404s instead of silently opening (SQLite) or creating
    (DuckDB) a fresh, empty database file at the wrong location."""
    path = details.get('database')
    if not path:
        raise ApiError('Database file path not provided', code='connection_misconfigured')
    if not os.path.isabs(path):
        path = os.path.join(config.home(), path)
    if not os.path.isfile(path):
        raise ApiError(f'{label} database file not found', 404, code='database_file_not_found')
    return path


def _prepare(sql, params, style, limit, offset, dialect):
    """Return (sql, args, sliced) - the statement to send, its bound arguments and whether the
    database already applied the requested window (LIMIT/OFFSET) for us."""
    window = limit + 1
    if is_paginated(sql, dialect):
        sql, sliced = paginate(sql, window, offset, params, dialect), True
    else:
        sliced = False
    sql, args = bind_parameters(sql, params or {}, style, dialect)
    return sql, args, sliced


def _fetch_page(cursor, sql, params, style, limit, offset, dialect, wrap=None):
    """One page of rows. ``wrap`` rewrites the final statement - after paging, before it's sent - for a driver that
    sends its own statements in the same message, or puts a hint on the query (ADR 0027)."""
    sql, args, sliced = _prepare(sql, params, style, limit, offset, dialect)
    if wrap is not None:
        sql = wrap(sql)
    if args is None:
        cursor.execute(sql)
    else:
        cursor.execute(sql, args)
    if not cursor.description:
        return [], []
    rows = cursor.fetchall() if sliced else cursor.fetchmany(offset + limit + 1)[offset:]
    return [d[0] for d in cursor.description], rows


# --------------------------------------------------------------------------------------
# Streaming: no LIMIT/OFFSET, and never more than one _STREAM_BATCH of rows in memory at
# once, however large the full result is. `_stream_cursor` yields the column names first
# (a one-item "sentinel" yield, so the caller can prime the generator once to learn them
# before deciding anything about the response, then hand the rest of the generator - the
# actual rows - straight to the client without this module needing to know about HTTP at
# all) and then every row, one at a time, pulled from the database in _STREAM_BATCH-sized
# fetchmany() calls rather than a single fetchall(). A driver whose cursor already streams
# from the server without buffering the whole result client-side (an unbuffered MySQL
# cursor, a named/server-side PostgreSQL cursor, ClickHouse's execute_iter) additionally
# keeps *this process's* memory use flat regardless of result size; the others (SQLite,
# DuckDB, H2/JDBC) still bound it to one batch at a time even where the underlying engine
# or driver computes the full result before the first fetch - see runners.py driver
# docstrings and DATABASE_CONNECTION_CONFIGURATION.md for which is which.
# --------------------------------------------------------------------------------------

_STREAM_BATCH = 1000


def _stream_cursor(cursor, sql, params, style, dialect):
    sql, args = bind_parameters(sql, params or {}, style, dialect)
    if args is None:
        cursor.execute(sql)
    else:
        cursor.execute(sql, args)
    if not cursor.description:
        yield ()
        return
    yield tuple(d[0] for d in cursor.description)
    while True:
        batch = cursor.fetchmany(_STREAM_BATCH)
        if not batch:
            return
        yield from batch


# --------------------------------------------------------------------------------------
# Drivers
# --------------------------------------------------------------------------------------

class _Driver:
    """How to connect to, check, reset, query and close one kind of database."""

    DIALECT: str | None = None  # the connection's `db` value; used to pick the right literal-quoting rules
    PARAM_STYLE: str | None = None  # this driver's bind_parameters() style; used by the default stream()

    def connect(self, details, read_only):
        raise NotImplementedError

    def is_alive(self, session):
        """Cheap check that a connection that sat idle is still usable."""
        return True

    def reset(self, session):
        """Put a used connection back into a clean state: end its transaction so no stale snapshot lingers."""

    def close(self, session):
        session.conn.close()

    def query(self, session, sql, params, limit, offset, read_only, timeout):
        raise NotImplementedError

    def stream(self, session, sql, params, timeout):
        """Like query(), but for the whole result rather than one page: no LIMIT/OFFSET, and returns the
        _stream_cursor() generator directly (columns first, then every row) instead of a (columns, rows)
        pair, so a caller consuming it lazily never forces more than one batch into memory. Always called
        with a connection opened read-only (streaming never writes - see engine.stream_sql), so unlike
        query() there is no read_only branch or commit() to consider here.

        The default implementation (a plain cursor(), fetchmany()-batched) is correct for any driver whose
        cursor supports fetchmany, which is every driver here; MySQL, PostgreSQL and ClickHouse override it
        to additionally avoid buffering the whole result on the client side - see their own stream().
        """
        return _stream_cursor(session.conn.cursor(), sql, params, self.PARAM_STYLE, self.DIALECT)


# MySQL reports 3024 (ER_QUERY_TIMEOUT), MariaDB 1969 (ER_STATEMENT_TIMEOUT)
_MYSQL_TIMEOUT_ERRNOS = (3024, 1969)
# A statement that starts with SELECT, after any leading comments (but not a /*+ hint of its own)
_LEADING_SELECT_RE = re.compile(r'^((?:\s|--[^\n]*(?:\n|$)|#[^\n]*(?:\n|$)|/\*(?!\+).*?\*/)*)(select)\b',
                                re.IGNORECASE | re.DOTALL)


def _mysql_timeout_wrap(cursor, session, timeout):
    """The read-only path's time limit, on the statement itself rather than the session (ADR 0027): a stored
    function a query calls could set the session's max_execution_time to 0 for every later caller, but not lift a
    limit carried by the statement. MySQL takes a MAX_EXECUTION_TIME hint on a top-level SELECT; MariaDB, which
    ignores MySQL's hints, takes SET STATEMENT ... FOR. A statement the hint can't go on (one starting WITH, say) gets
    the session value set for this call - every call, not only when it changes. Returns the rewrite, or None."""
    if not timeout:
        return None
    if session.state.get('mariadb') is None:
        session.state['mariadb'] = 'mariadb' in (session.conn.get_server_info() or '').lower()
    if session.state['mariadb']:
        return lambda sql: f'SET STATEMENT max_statement_time = {timeout:g} FOR {sql}'
    millis = _millis(timeout)

    def hinted(sql):
        match = _LEADING_SELECT_RE.match(sql)
        if match:
            return f'{match.group(1)}{match.group(2)} /*+ MAX_EXECUTION_TIME({millis}) */{sql[match.end():]}'
        cursor.execute(f'SET SESSION max_execution_time = {millis}')
        session.state['timeout'] = None  # no longer the value _set_mysql_timeout() last set
        return sql
    return hinted


def _set_mysql_timeout(cursor, session, timeout):
    """Shared by _MySQL.query() and .stream(): MySQL limits SELECT statements in milliseconds; MariaDB
    uses a differently named variable in seconds. 0 removes a limit an earlier request left behind on
    this pooled connection."""
    import mysql.connector
    if timeout == session.state.get('timeout'):
        return
    try:
        cursor.execute(f'SET SESSION max_execution_time = {_millis(timeout) if timeout else 0}')
    except mysql.connector.Error:
        try:
            cursor.execute(f'SET SESSION max_statement_time = {timeout:g}' if timeout
                           else 'SET SESSION max_statement_time = 0')
        except mysql.connector.Error:
            log.warning('This MySQL server supports neither max_execution_time nor '
                        'max_statement_time; the query time limit is not enforced')
    session.state['timeout'] = timeout


class _MySQL(_Driver):
    DIALECT = 'mysql'
    PARAM_STYLE = 'format'

    def connect(self, details, read_only):
        """A read-only connection is read-only per transaction, not only per session (ADR 0027): it opens one with
        START TRANSACTION READ ONLY, and reset() ends each with ROLLBACK AND CHAIN, which starts the next with the
        same access mode. A query can change the session's default - `SELECT f()`, for a stored function that runs
        SET SESSION TRANSACTION READ WRITE - but not the transaction it is running in, so every later call on this
        pooled connection still runs read-only. The session default is set too, as a second layer."""
        import mysql.connector
        conn = mysql.connector.connect(connection_timeout=config.CONNECT_TIMEOUT,
                                       **_connect_args(details, tls=MYSQL_TLS))
        if read_only:
            try:
                cursor = conn.cursor()
                cursor.execute('SET SESSION TRANSACTION READ ONLY')
                cursor.execute('START TRANSACTION READ ONLY')
                cursor.close()
            except Exception:
                conn.close()
                raise
        return conn

    def is_alive(self, session):
        return session.conn.is_connected()

    @staticmethod
    def _cursor(session):
        """This connection's one cursor, made once. mysql-connector pings the server on every cursor() - a round
        trip, during which its C extension holds the GIL, freezing every thread in the worker (ADR 0027) - so a
        query never asks for a new one."""
        cursor = session.state.get('cursor')
        if cursor is None:
            cursor = session.state['cursor'] = session.conn.cursor()
        return cursor

    def reset(self, session):
        conn = session.conn
        if conn.unread_result:  # a page that didn't read its whole result, or a stream closed early
            conn.consume_results()
        if session.state.get('read_only'):
            self._cursor(session).execute('ROLLBACK AND CHAIN')  # ends it, and starts the next one read-only
        elif conn.in_transaction:
            conn.rollback()

    def query(self, session, sql, params, limit, offset, read_only, timeout):
        import mysql.connector
        conn = session.conn
        cursor = self._cursor(session)
        session.state['read_only'] = read_only
        if read_only:
            wrap = _mysql_timeout_wrap(cursor, session, timeout)  # on the statement: a query can't lift it
        else:
            wrap = None
            _set_mysql_timeout(cursor, session, timeout)
        try:
            result = _fetch_page(cursor, sql, params, 'format', limit, offset, self.DIALECT, wrap=wrap)
        except mysql.connector.Error as error:
            if getattr(error, 'errno', None) in _MYSQL_TIMEOUT_ERRNOS:
                raise _timed_out(timeout) from None
            raise
        if conn.unread_result:
            conn.consume_results()
        if not read_only:
            conn.commit()
        return result

    def stream(self, session, sql, params, timeout):
        """Unlike query(), uses an *unbuffered* cursor (mysql-connector-python's default cursor fetches and
        buffers the entire result set into the client on execute() - fine for one page, defeats the point
        of streaming for a large export) - see DATABASE_CONNECTION_CONFIGURATION.md#streaming-exports.
        Verified end-to-end against a real server: 1M rows streamed over real HTTP with this process's own
        RSS sampled throughout - flat at ~49MB the entire way, against ~344MB for the same query fetched
        the ordinary (buffered) way."""
        import mysql.connector
        conn = session.conn
        session.state['read_only'] = True
        # Set for this export, every time: a session value left from before can't be trusted (ADR 0027)
        session.state['timeout'] = object()
        _set_mysql_timeout(self._cursor(session), session, timeout)
        try:
            yield from _stream_cursor(conn.cursor(buffered=False), sql, params, 'format', self.DIALECT)
        except mysql.connector.Error as error:
            if getattr(error, 'errno', None) in _MYSQL_TIMEOUT_ERRNOS:
                raise _timed_out(timeout) from None
            raise


def _postgres_prefix(read_only, timeout):
    """What a PostgreSQL message sends ahead of the query: the transaction's access mode and time limit."""
    return (('SET TRANSACTION READ ONLY; ' if read_only else '')
            + (f'SET LOCAL statement_timeout = {_millis(timeout)}; ' if timeout else ''))


class _Postgres(_Driver):
    DIALECT = 'postgres'
    PARAM_STYLE = 'format'

    def connect(self, details, read_only):
        """Autocommit, so each query is one message PostgreSQL runs as one implicit transaction, ending with it - one
        round trip, and nothing left to roll back (ADR 0027). Read-only is set in that message, per transaction; the
        session default set here is a second layer only, since `SELECT set_config(...)` can change it."""
        import psycopg2
        conn = psycopg2.connect(connect_timeout=config.CONNECT_TIMEOUT,
                                **_connect_args(details, tls=POSTGRES_TLS, database='dbname'))
        try:
            conn.set_session(readonly=True, autocommit=True) if read_only else conn.set_session(autocommit=True)
        except Exception:
            conn.close()
            raise
        return conn

    def is_alive(self, session):
        if session.conn.closed:
            return False
        with session.conn.cursor() as cursor:
            cursor.execute('SELECT 1')
        return True

    def reset(self, session):
        conn = session.conn
        if not conn.autocommit:  # a stream or an agent write ran in an explicit transaction: end it
            conn.rollback()
            conn.autocommit = True

    def query(self, session, sql, params, limit, offset, read_only, timeout):
        import psycopg2.errors
        # One message (ADR 0027): read-only and the time limit are set inside the transaction the query runs in -
        # LOCAL, so nothing outlives it - and the message commits as it ends, a write included.
        prefix = _postgres_prefix(read_only, timeout)
        with session.conn.cursor() as cursor:
            try:
                return _fetch_page(cursor, sql, params, 'format', limit, offset, self.DIALECT,
                                   wrap=(lambda statement: prefix + statement) if prefix else None)
            except psycopg2.errors.QueryCanceled:
                raise _timed_out(timeout) from None

    def stream(self, session, sql, params, timeout):
        """Unlike query(), uses a named (server-side) cursor - PostgreSQL's default cursor, like MySQL's,
        fetches the whole result set into the client on execute() - see
        DATABASE_CONNECTION_CONFIGURATION.md#streaming-exports. A named cursor needs an open transaction,
        which every connection here already sits in outside autocommit mode; it is torn down along with
        that transaction by reset() when the connection is released, same as the LOCAL statement_timeout.

        Does not reuse _stream_cursor(): a named cursor's execute() is really a `DECLARE CURSOR ... FOR
        <query>` under the hood, so it returns immediately without running the query at all - .description
        stays None, and statement_timeout has nothing to cancel yet, until the *first fetch*, which is what
        actually runs it server-side. So the first fetchmany() has to happen before .description is read,
        the reverse of every other driver here - verified against a real, deliberately slow PostgreSQL
        query on a named cursor (confirms both that .description is only populated after that first fetch,
        and that statement_timeout does still correctly cancel it there). Also verified end-to-end like
        MySQL above: 1M rows over real HTTP, RSS flat at ~55MB throughout, against ~564MB buffered."""
        import psycopg2.errors
        conn = session.conn
        conn.autocommit = False  # a named cursor needs an explicit transaction; reset() ends it, restoring autocommit
        with conn.cursor() as setter:
            setter.execute(_postgres_prefix(True, timeout).rstrip('; '))
        cursor = conn.cursor(name='queryapigate_stream')
        cursor.itersize = _STREAM_BATCH  # rows fetched from the server per underlying fetchmany() call
        sql, args = bind_parameters(sql, params or {}, 'format', self.DIALECT)
        try:
            cursor.execute(sql) if args is None else cursor.execute(sql, args)
            first_batch = cursor.fetchmany(_STREAM_BATCH)
        except psycopg2.errors.QueryCanceled:
            raise _timed_out(timeout) from None
        if not cursor.description:
            yield ()
            return
        yield tuple(d[0] for d in cursor.description)
        yield from first_batch
        try:
            while True:
                batch = cursor.fetchmany(_STREAM_BATCH)
                if not batch:
                    return
                yield from batch
        except psycopg2.errors.QueryCanceled:
            raise _timed_out(timeout) from None


_CLICKHOUSE_TIMEOUT_EXCEEDED = 159


class _ClickHouse(_Driver):
    DIALECT = 'clickhouse'

    # The native-protocol Client pings before each query and reconnects by itself, so it needs no
    # is_alive check, and every setting is sent per query, so there is no session state to reset.
    def connect(self, details, read_only):
        from clickhouse_driver import Client
        client_args = {}
        limit = config.query_timeout()
        if limit:
            # Backstop in case the server never answers; the server-side limit below is what normally fires.
            client_args['send_receive_timeout'] = math.ceil(limit) + 5
        return Client(connect_timeout=config.CONNECT_TIMEOUT, **client_args, **_connect_args(details))

    def close(self, session):
        session.conn.disconnect()

    def query(self, session, sql, params, limit, offset, read_only, timeout):
        from clickhouse_driver.errors import ServerException
        sql, args, sliced = _prepare(sql, params, 'pyformat', limit, offset, self.DIALECT)
        settings = {}
        if timeout:
            settings['max_execution_time'] = math.ceil(timeout)  # whole seconds
        if read_only:
            settings['readonly'] = 1  # last: it forbids changing settings after it
        try:
            result = session.conn.execute(sql, args, with_column_types=True, settings=settings or None)
        except ServerException as error:
            if error.code == _CLICKHOUSE_TIMEOUT_EXCEEDED:
                raise _timed_out(timeout) from None
            raise
        # Statements without a result set (DDL, INSERT) may not return a (rows, types) pair.
        rows, column_types = result if isinstance(result, tuple) else ([], [])
        if not sliced:
            rows = rows[offset:offset + limit + 1]
        return [c[0] for c in column_types or []], rows

    def stream(self, session, sql, params, timeout):
        """Unlike query(), uses execute_iter() - the native ClickHouse protocol is columnar and block-
        based to begin with, so unlike MySQL/PostgreSQL this needs no special cursor mode, just the
        streaming entry point instead of the buffered one. With with_column_types=True its first yielded
        item is the [(name, type), ...] column list rather than a data row. Verified end-to-end (a real
        server, 1M rows, watching the actual process RSS during the HTTP download): memory grows somewhat
        early on and then plateaus, rather than the roughly linear growth with result size a fully-buffered
        fetch shows - block-level internal buffering, not row-by-row, but still not proportional to how
        large the full result is. Also verified that max_execution_time still cancels it mid-stream, same
        as query()."""
        from clickhouse_driver.errors import ServerException
        sql, args = bind_parameters(sql, params or {}, 'pyformat', self.DIALECT)
        settings = {'readonly': 1}  # streaming is always read-only - see engine.stream_sql
        if timeout:
            settings['max_execution_time'] = math.ceil(timeout)
        try:
            iterator = session.conn.execute_iter(sql, args, with_column_types=True, settings=settings)
            yield tuple(name for name, _type in next(iterator))
            yield from iterator
        except ServerException as error:
            if error.code == _CLICKHOUSE_TIMEOUT_EXCEEDED:
                raise _timed_out(timeout) from None
            raise


class _DuckDB(_Driver):
    """DuckDB: an embedded analytical database (own storage, own `.db` file, no server process) that can
    also query flat files directly - a saved query's own SQL can call `read_csv('data.csv')` or
    `read_json('data.json')` without any new connection fields, so this one connection type covers both
    "a genuinely capable embedded database" and "drop a file and query it".

    DuckDB refuses to open a connection to a file with a different `read_only` setting than a connection
    already open on it in this process ("Can't open a connection to same database file with a different
    configuration than existing connections"), which the pool's read-only/read-write connections-are-
    pooled-separately design would trip over constantly. So, like H2 and the generic jdbc driver, every
    connection here is opened read-write regardless of the caller's read_only flag, and the read-only
    guarantee rests on validate_sql() alone - including the writes a read-only first keyword can hide (a writable
    CTE, EXPLAIN ANALYZE), which DuckDB would run; see sqltools.write_operations() and tests/test_sql_guard_writes.py.

    Unlike H2/jdbc, DuckDB autocommits each statement by default (nothing is normally left open for
    reset() to end) and its Python connection does support cancelling an in-progress statement
    (`Connection.interrupt()`), so query timeouts are enforced here via a background timer, not merely
    documented as unsupported.
    """
    DIALECT = 'duckdb'
    PARAM_STYLE = 'qmark'

    def connect(self, details, read_only):
        """`database: ":memory:"` is a database of nothing but views and files (BACKLOG #75). Either way the connection
        is locked to its allowed_paths before anyone's SQL runs on it - see duckfiles.py."""
        import duckdb
        database = ':memory:' if details.get('database') == ':memory:' else _resolve_db_file(details, 'DuckDB')
        conn = duckdb.connect(database)
        try:
            duckfiles.lock_down(conn, details)
        except BaseException:
            conn.close()
            raise
        return conn

    def is_alive(self, session):
        try:
            session.conn.execute('SELECT 1')
            return True
        except Exception:
            return False

    def reset(self, session):
        import duckdb
        try:
            session.conn.rollback()
        except duckdb.TransactionException:
            pass  # autocommit already applied the last statement; there was nothing open to roll back

    def query(self, session, sql, params, limit, offset, read_only, timeout):
        import duckdb
        conn = session.conn
        timer = threading.Timer(timeout, conn.interrupt) if timeout else None
        if timer:
            timer.daemon = True
            timer.start()
        try:
            result = _fetch_page(conn, sql, params, 'qmark', limit, offset, self.DIALECT)
        except duckdb.InterruptException:
            raise _timed_out(timeout) from None
        except duckdb.PermissionException as error:
            raise duckfiles.not_allowed(error) from None
        finally:
            if timer:
                timer.cancel()
        if not read_only:
            conn.commit()
        return result

    def stream(self, session, sql, params, timeout):
        """The timer only wraps execute(), not the fetchmany() loop after it: DuckDB's engine computes the
        whole result relation during execute() (see the class docstring - its own kind of buffering, not
        specific to streaming), so that is the only phase that can actually run long; fetchmany() calls
        afterwards just read pages of an already-computed relation and are not a timeout candidate."""
        import duckdb
        conn = session.conn
        timer = threading.Timer(timeout, conn.interrupt) if timeout else None
        if timer:
            timer.daemon = True
            timer.start()
        try:
            sql, args = bind_parameters(sql, params or {}, 'qmark', self.DIALECT)
            conn.execute(sql) if args is None else conn.execute(sql, args)
        except duckdb.InterruptException:
            raise _timed_out(timeout) from None
        except duckdb.PermissionException as error:
            raise duckfiles.not_allowed(error) from None
        finally:
            if timer:
                timer.cancel()
        if not conn.description:
            yield ()
            return
        yield tuple(d[0] for d in conn.description)
        while True:
            batch = conn.fetchmany(_STREAM_BATCH)
            if not batch:
                return
            yield from batch


def _attach_thread_as_daemon():
    """Make the calling thread a *daemon* thread as far as the JVM is concerned.

    jaydebeapi attaches every thread that uses H2 to the JVM as a non-daemon thread, and JPype's shutdown then
    waits for those threads forever - so a server that has handled concurrent H2 requests would hang on exit.
    Attaching them as daemons first (jaydebeapi only attaches threads that are not attached yet) avoids that.
    """
    import jpype
    if jpype.isJVMStarted() and not jpype.java.lang.Thread.isAttached():
        jpype.java.lang.Thread.attachAsDaemon()


_jvm_exit_hook_registered = False


def _register_jvm_exit_hook():
    global _jvm_exit_hook_registered
    if not _jvm_exit_hook_registered:
        # JPype shuts the JVM down from its own atexit hook, which it registers when the JVM starts (just
        # now, by whichever of H2/_JDBC connected first). Hooks run last-in first-out, so registering ours
        # after that makes pooled connections close while the JVM is still alive - closing them afterwards
        # would hang the interpreter at exit.
        atexit.register(close_pooled_connections)
        _jvm_exit_hook_registered = True


def _jvm_classpath():
    """Every jar an H2 or jdbc connection might need on the JVM's classpath.

    JPype starts exactly one JVM per process, and its classpath is fixed at that moment - jaydebeapi only
    passes the jars of *this* connect() call, so if H2 (say) happens to start the JVM first, a jdbc
    connection's own jar would never be on the classpath and it would fail with a class-not-found error the
    first time it is used, even though nothing about its own configuration is wrong. Passing every
    currently-configured jar on every connect() call means whichever connection is used first brings all of
    them along. A jdbc connection added *after* the JVM has already started still needs the server
    restarted before its jar takes effect - that part is unavoidable with one JVM per process.
    """
    jars = {config.h2_jar()}
    for details in store.read_connections().values():
        if details.get('db') == 'jdbc' and details.get('jar'):
            jars.add(details['jar'])
    return sorted(jars)


class _H2(_Driver):
    DIALECT = 'h2'
    PARAM_STYLE = 'qmark'

    def connect(self, details, read_only):
        import jaydebeapi
        if not os.path.isfile(config.h2_jar()):  # a source checkout: the driver is fetched, not committed
            raise ApiError(f"H2's driver isn't installed ({config.h2_jar()}): in a source checkout run "
                           'python .github/scripts/fetch_h2_jar.py, or set QUERYAPIGATE_H2_JAR', 500,
                           code='driver_missing')
        _attach_thread_as_daemon()
        host = details.get('host') or 'localhost'
        if details.get('port') and ':' not in host:
            host = f"{host}:{details['port']}"
        url = f"jdbc:h2:tcp://{host}/~/{details.get('database')}"
        # Unlike the other drivers, H2's JDBC setReadOnly() is only a hint and does not block writes,
        # so here the read-only guarantee rests on validate_sql() alone.
        conn = jaydebeapi.connect('org.h2.Driver', url, [details.get('user'), details.get('password')],
                                  _jvm_classpath())
        _register_jvm_exit_hook()
        return conn

    def is_alive(self, session):
        _attach_thread_as_daemon()
        return bool(session.conn.jconn.isValid(2))

    def reset(self, session):
        _attach_thread_as_daemon()
        session.conn.rollback()

    def close(self, session):
        _attach_thread_as_daemon()
        session.conn.close()

    def query(self, session, sql, params, limit, offset, read_only, timeout):
        import jaydebeapi
        _attach_thread_as_daemon()
        conn = session.conn
        cursor = conn.cursor()
        if timeout != session.state.get('timeout'):
            cursor.execute(f'SET QUERY_TIMEOUT {_millis(timeout) if timeout else 0}')
            session.state['timeout'] = timeout
        try:
            result = _fetch_page(cursor, sql, params, 'qmark', limit, offset, self.DIALECT)
        except jaydebeapi.Error as error:
            # H2: "Statement was canceled or the session timed out" (error 57014 / 90051)
            if timeout and any(word in str(error).lower() for word in ('canceled', 'timed out')):
                raise _timed_out(timeout) from None
            raise
        if not read_only:
            conn.commit()
        return result

    def stream(self, session, sql, params, timeout):
        """Same JVM-thread-attachment requirement as every other H2 method (see _attach_thread_as_daemon).
        Uses the same fetchmany()-batched cursor as the default _Driver.stream(); jaydebeapi exposes no way
        to set the underlying JDBC ResultSet's fetch size, so unlike MySQL/PostgreSQL this does not avoid a
        real, one-off memory cost proportional to the result size on the JVM side of the bridge (verified:
        a jump right after execute(), before any row is fetched, that then plateaus rather than growing
        further per row) - but it does still avoid this Python process's own memory growing without bound
        as more rows are pulled, which the old fetchall()-everything-at-once approach could not."""
        import jaydebeapi
        _attach_thread_as_daemon()
        conn = session.conn
        if timeout != session.state.get('timeout'):
            with conn.cursor() as setter:
                setter.execute(f'SET QUERY_TIMEOUT {_millis(timeout) if timeout else 0}')
            session.state['timeout'] = timeout
        try:
            yield from _stream_cursor(conn.cursor(), sql, params, 'qmark', self.DIALECT)
        except jaydebeapi.Error as error:
            if timeout and any(word in str(error).lower() for word in ('canceled', 'timed out')):
                raise _timed_out(timeout) from None
            raise


class _JDBC(_Driver):
    """A generic JDBC connection for any database not covered by a dedicated driver above (Oracle, SQL
    Server, DB2, Snowflake, ...) - reuses the JVM this project already embeds for H2. A connection needs
    three fields the other drivers do not: ``jar`` (path to the vendor's JDBC driver jar), ``driver_class``
    (its fully-qualified Java class name) and ``jdbc_url`` (the full JDBC URL - vendor URL formats vary too
    much to build one generically the way the other drivers build theirs from host/port).

    Two limits are inherent to embedding one JVM per process, not specific to this driver:

    - No native query-timeout cancellation. The other drivers each have a way to cancel a running statement
      (a SQL command, a session variable, a driver-level cursor call); there is no such thing that works
      across arbitrary JDBC drivers without reaching into jaydebeapi's private internals (its ``Cursor``
      only exposes the prepared statement *after* ``execute()`` both prepares and runs it, leaving no seam
      to call ``setQueryTimeout()`` first). ``QUERYAPIGATE_QUERY_TIMEOUT`` is not enforced for this connection
      type; a slow query here runs to completion regardless of the configured limit.
    - Like H2, ``Connection.setReadOnly()`` is advisory in the JDBC specification, not something every
      driver is required to enforce - the read-only guarantee rests on ``validate_sql()`` alone, same as H2.

    Pagination is applied client-side (see ``sqltools.is_paginated``), since ``LIMIT``/``OFFSET`` is not
    portable SQL either. Schema introspection (``GET /connections/<name>/schema``) is not supported for this
    connection type - see ``schema.py``.
    """
    DIALECT = 'jdbc'
    PARAM_STYLE = 'qmark'

    def connect(self, details, read_only):
        import jaydebeapi
        _attach_thread_as_daemon()
        jar, driver_class, url = details.get('jar'), details.get('driver_class'), details.get('jdbc_url')
        if not (jar and driver_class and url):
            raise ApiError("A 'jdbc' connection needs 'jar', 'driver_class' and 'jdbc_url'", 500,
                           code='connection_misconfigured')
        conn = jaydebeapi.connect(driver_class, url, [details.get('user'), details.get('password')],
                                  _jvm_classpath())
        if read_only:
            try:
                conn.jconn.setReadOnly(True)
            except Exception:
                log.debug('setReadOnly() was not accepted by this JDBC driver; the guard still applies',
                         exc_info=True)
        _register_jvm_exit_hook()
        return conn

    def is_alive(self, session):
        _attach_thread_as_daemon()
        return bool(session.conn.jconn.isValid(2))

    def reset(self, session):
        _attach_thread_as_daemon()
        session.conn.rollback()

    def close(self, session):
        _attach_thread_as_daemon()
        session.conn.close()

    def query(self, session, sql, params, limit, offset, read_only, timeout):
        _attach_thread_as_daemon()
        conn = session.conn
        cursor = conn.cursor()
        result = _fetch_page(cursor, sql, params, 'qmark', limit, offset, self.DIALECT)
        if not read_only:
            conn.commit()
        return result

    def stream(self, session, sql, params, timeout):
        """Same JVM-thread-attachment requirement as every other method here. No timeout to apply -
        QUERYAPIGATE_QUERY_TIMEOUT is not enforced for jdbc at all (see the class docstring above)."""
        _attach_thread_as_daemon()
        yield from _stream_cursor(session.conn.cursor(), sql, params, 'qmark', self.DIALECT)


class _Trino(_Driver):
    """Trino (and its ancestor Presto): a distributed SQL engine that federates many catalogs behind one
    endpoint - Hive, Iceberg, Delta, Kafka, object stores, other databases (ADR 0026). Experimental (#64).

    The connection's generic `database` field is a Trino **catalog**; an optional `schema` sets the default
    schema. Like DuckDB/H2/jdbc, Trino has no read-only session mode, so the read-only guarantee rests on
    validate_sql() alone (`trino` is not in databases.DRIVER_READ_ONLY). Trino's own access control - a
    read-only, catalog/schema-scoped Trino user per connection - is the outer boundary; see the ADR.

    Sessions are cheap, stateless HTTP, so reset() is a no-op and is_alive() is a cheap SELECT 1. The query
    time limit is enforced by cancelling the running query on a background timer (as DuckDB does), with the
    server's static QUERYAPIGATE_QUERY_TIMEOUT also set as a per-session `query_max_run_time` backstop.
    """
    DIALECT = 'trino'
    PARAM_STYLE = 'qmark'

    def connect(self, details, read_only):
        import trino
        host, user = details.get('host'), details.get('user')
        catalog = details.get('database')  # the generic `database` field is a Trino catalog
        if not host or not user:
            raise ApiError('A Trino connection needs a host and a user', code='connection_misconfigured')
        if not catalog:
            raise ApiError("A Trino connection needs a catalog (the connection's database field)",
                           code='connection_misconfigured')
        kwargs = {'host': host, 'port': int(details.get('port') or 8080), 'user': user, 'catalog': catalog,
                  'schema': details.get('schema') or 'default',
                  'http_scheme': details.get('http_scheme') or 'http'}
        if details.get('password'):
            from trino.auth import BasicAuthentication
            kwargs['auth'] = BasicAuthentication(user, details['password'])
            kwargs['http_scheme'] = 'https'  # Trino requires HTTPS for password (basic) auth
        limit = config.query_timeout()
        if limit:  # a static backstop; the per-query timer below enforces the actual (possibly lower) timeout
            kwargs['session_properties'] = {'query_max_run_time': f'{math.ceil(limit)}s'}
        return trino.dbapi.connect(**kwargs)

    def is_alive(self, session):
        try:
            cursor = session.conn.cursor()
            cursor.execute('SELECT 1')
            cursor.fetchone()
            return True
        except Exception:
            return False

    def query(self, session, sql, params, limit, offset, read_only, timeout):
        cursor = session.conn.cursor()
        cancelled = {'fired': False}

        def _cancel():
            cancelled['fired'] = True
            try:
                cursor.cancel()
            except Exception:  # noqa: BLE001 - best effort; the query may have just finished
                pass

        timer = threading.Timer(timeout, _cancel) if timeout else None
        if timer:
            timer.daemon = True
            timer.start()
        try:
            return _fetch_page(cursor, sql, params, 'qmark', limit, offset, self.DIALECT)
        except Exception:
            if cancelled['fired']:
                raise _timed_out(timeout) from None
            raise
        finally:
            if timer:
                timer.cancel()


def _make_runner(driver):
    def run(details, sql, params, limit, offset, read_only, timeout=None, pool=None):
        if pool is None:
            session = Session(driver.connect(details, read_only), driver)
            try:
                return driver.query(session, sql, params, limit, offset, read_only, timeout)
            finally:
                session.close()
        with pool.checkout(driver, details, read_only) as session:
            return driver.query(session, sql, params, limit, offset, read_only, timeout)

    return run


def _make_stream_runner(driver):
    """Like _make_runner, but for stream(): always opens the connection read-only (streaming never writes
    - see engine.stream_sql) and, since the whole point is that a caller never has to hold the whole result
    in memory, returns the driver's row generator directly instead of a materialised (columns, rows) pair.
    Whichever of the two branches below checks the connection out (from the pool, or a fresh one) does not
    release or close it until that generator is exhausted, raises, or is closed early - see the
    "Streaming" section atop this module."""
    def run(details, sql, params, timeout=None, pool=None):
        if pool is None:
            session = Session(driver.connect(details, True), driver)
            try:
                yield from driver.stream(session, sql, params, timeout)
            finally:
                session.close()
        else:
            with pool.checkout(driver, details, True) as session:
                yield from driver.stream(session, sql, params, timeout)

    return run


_run_mysql = _make_runner(_MySQL())
_run_postgres = _make_runner(_Postgres())
_run_clickhouse = _make_runner(_ClickHouse())
_run_h2 = _make_runner(_H2())
_run_jdbc = _make_runner(_JDBC())
_run_duckdb = _make_runner(_DuckDB())
_run_trino = _make_runner(_Trino())
_stream_mysql = _make_stream_runner(_MySQL())
_stream_postgres = _make_stream_runner(_Postgres())
_stream_clickhouse = _make_stream_runner(_ClickHouse())
_stream_h2 = _make_stream_runner(_H2())
_stream_jdbc = _make_stream_runner(_JDBC())
_stream_duckdb = _make_stream_runner(_DuckDB())
_stream_trino = _make_stream_runner(_Trino())


def _run_sqlite(details, sql, params, limit, offset, read_only, timeout=None, pool=None):
    """SQLite is a local file, so opening it per call is cheap and it is never pooled."""
    path = _resolve_db_file(details, 'SQLite')
    if read_only:
        conn = sqlite3.connect(f'{Path(path).as_uri()}?mode=ro', uri=True, timeout=config.CONNECT_TIMEOUT)
    else:
        conn = sqlite3.connect(path, timeout=config.CONNECT_TIMEOUT)
    try:
        expired = []
        if timeout:
            deadline = time.monotonic() + timeout

            def check_deadline():
                if time.monotonic() > deadline:
                    expired.append(True)
                    return 1  # non-zero aborts the running statement
                return 0

            conn.set_progress_handler(check_deadline, 10000)  # called every 10 000 VM instructions
        cursor = conn.cursor()
        try:
            result = _fetch_page(cursor, sql, params, 'qmark', limit, offset, 'sqlite')
        except sqlite3.OperationalError:
            if expired:
                raise _timed_out(timeout) from None
            raise
        if not read_only:
            conn.commit()
        return result
    finally:
        conn.close()


def _stream_sqlite(details, sql, params, timeout=None, pool=None):
    """Like _run_sqlite, never pooled - opened directly and closed once this generator is exhausted,
    errors, or is closed early. Always opened read-only (streaming never writes)."""
    path = _resolve_db_file(details, 'SQLite')
    conn = sqlite3.connect(f'{Path(path).as_uri()}?mode=ro', uri=True, timeout=config.CONNECT_TIMEOUT)
    try:
        expired = []
        if timeout:
            deadline = time.monotonic() + timeout

            def check_deadline():
                if time.monotonic() > deadline:
                    expired.append(True)
                    return 1
                return 0

            conn.set_progress_handler(check_deadline, 10000)
        cursor = conn.cursor()
        try:
            yield from _stream_cursor(cursor, sql, params, 'qmark', 'sqlite')
        except sqlite3.OperationalError:
            if expired:
                raise _timed_out(timeout) from None
            raise
    finally:
        conn.close()


RUNNERS = {
    'mysql': _run_mysql,
    'postgres': _run_postgres,
    'clickhouse': _run_clickhouse,
    'sqlite': _run_sqlite,
    'h2': _run_h2,
    'jdbc': _run_jdbc,
    'duckdb': _run_duckdb,
    'trino': _run_trino,
}

# Every dialect above supports streaming too - each stream_*() generator here yields the column names
# once, then every row, one at a time (see the "Streaming" section earlier in this module).
STREAM_RUNNERS = {
    'mysql': _stream_mysql,
    'postgres': _stream_postgres,
    'clickhouse': _stream_clickhouse,
    'sqlite': _stream_sqlite,
    'h2': _stream_h2,
    'jdbc': _stream_jdbc,
    'duckdb': _stream_duckdb,
    'trino': _stream_trino,
}

# --------------------------------------------------------------------------------------
# MongoDB: not in RUNNERS/STREAM_RUNNERS above - a find() filter is a JSON document, not a SQL string, so
# it doesn't fit _Driver.query()'s (sql, params, limit, offset, ...) signature. It reuses connection
# pooling (pool.checkout()/Session only need .connect()/.close()/.is_alive(), never the query() shape) and
# the same "fetch limit + 1 rows" pagination trick, called directly by engine.execute_mongo() instead of
# through _make_runner(). Read-only, full stop - there is no read_only parameter to flip, unlike every SQL
# driver above (see BACKLOG #36: MongoDB support is find-only in this version).
# --------------------------------------------------------------------------------------

class _Mongo(_Driver):
    DIALECT = 'mongo'

    def connect(self, details, read_only):
        import pymongo
        args = _connect_args(details, user='username')
        args.pop('database', None)  # MongoClient itself takes no default database; find() selects one per call
        return pymongo.MongoClient(connectTimeoutMS=_millis(config.CONNECT_TIMEOUT),
                                   serverSelectionTimeoutMS=_millis(config.CONNECT_TIMEOUT), **args)

    def is_alive(self, session):
        return True  # MongoClient manages its own pool/reconnection internally - nothing to check here


_mongo_driver = _Mongo()


def _mongo_database(details, client):
    name = details.get('database')
    if not name:
        raise ApiError('Connection has no database configured', code='connection_misconfigured')
    return client[name]


def _with_mongo_session(details, pool, read_only, body):
    if pool is None:
        session = Session(_mongo_driver.connect(details, read_only), _mongo_driver)
        try:
            return body(session)
        finally:
            session.close()
    with pool.checkout(_mongo_driver, details, read_only) as session:
        return body(session)


def mongo_find(details, collection_name, filter_doc, projection, sort, limit, offset, timeout=None, pool=None):
    """Run one find() and return (docs, has_more) - docs is a plain list of dicts (up to ``limit`` of them),
    ``has_more`` says whether the fetched extra row means another page exists, same "fetch limit + 1" trick
    every SQL runner above uses."""
    from pymongo.errors import ExecutionTimeout

    def body(session):
        coll = _mongo_database(details, session.conn)[collection_name]
        cursor = coll.find(filter_doc, projection)
        if sort:
            cursor = cursor.sort(list(sort.items()))
        cursor = cursor.skip(offset).limit(limit + 1)
        if timeout:
            cursor = cursor.max_time_ms(_millis(timeout))
        try:
            return list(cursor)
        except ExecutionTimeout:
            raise _timed_out(timeout) from None

    return _with_mongo_session(details, pool, True, body)


# Documents a streamed find() asks the server for at a time - its cursor's batchSize. A batch is held in memory while
# it's written out, as a SQL stream's fetchmany() batch is.
MONGO_STREAM_BATCH = 1000


def mongo_stream(details, collection_name, filter_doc, projection, sort, timeout=None, pool=None):
    """The whole result of one find(), a document at a time - the MongoDB _make_stream_runner() (ADR 0022). Yields
    True once the first batch is in, so a bad filter or an unreachable server is an error before anything is sent
    (engine.stream_mongo() reads it, as stream_sql() reads a SQL stream's columns), then each document. `timeout` is
    the cursor's maxTimeMS: the whole stream's time on the server, not one batch's. The connection is held until
    the generator is exhausted, fails or is closed."""
    from pymongo.errors import ExecutionTimeout

    def documents(session):
        coll = _mongo_database(details, session.conn)[collection_name]
        cursor = coll.find(filter_doc, projection, batch_size=MONGO_STREAM_BATCH)
        if sort:
            cursor = cursor.sort(list(sort.items()))
        if timeout:
            cursor = cursor.max_time_ms(_millis(timeout))
        try:
            docs = iter(cursor)
            try:
                first = next(docs, None)
                yield True
                if first is None:
                    return
                yield first
                yield from docs
            except ExecutionTimeout:
                raise _timed_out(timeout) from None
        finally:
            cursor.close()

    if pool is None:
        session = Session(_mongo_driver.connect(details, True), _mongo_driver)
        try:
            yield from documents(session)
        finally:
            session.close()
    else:
        with pool.checkout(_mongo_driver, details, True) as session:
            yield from documents(session)


def mongo_ping(details, pool=None):
    """A connectivity probe - POST /api/v1/connections/test's mongo case (see engine.test_connection)."""
    _with_mongo_session(details, pool, True, lambda session: session.conn.admin.command('ping'))


def mongo_list_databases(details, pool=None):
    """Every database on the server - lights up the same "Default database"/"browse a different database on
    this server" UI the SQL dialects in engine.LIST_DATABASES_QUERIES get, but natively rather than via a
    SQL query (see engine.list_databases)."""
    return _with_mongo_session(details, pool, True, lambda session: session.conn.list_database_names())


def mongo_list_collections(details, pool=None):
    """Collection names in this connection's configured database - schema.fetch_schema()'s mongo case."""
    def body(session):
        return sorted(_mongo_database(details, session.conn).list_collection_names())

    return _with_mongo_session(details, pool, True, body)


# --------------------------------------------------------------------------------------
# Agent writes (ADR 0008): one statement in a transaction, committed only when it changed no more rows than the
# saved query's max_affected_rows - otherwise rolled back, so the ceiling is the rows actually changed when it ran,
# not an estimate made beforehand. Only on the databases that can do both (definitions.AGENT_WRITE_DIALECTS).
# Each returns (columns, rows, affected); rows are a RETURNING clause's, if the statement has one.
# --------------------------------------------------------------------------------------

class TooManyRowsAffected(Exception):
    def __init__(self, affected, ceiling):
        super().__init__(f'{affected} rows')
        self.affected, self.ceiling = affected, ceiling


def _write(cursor, sql, params, style, dialect, count_row=False):
    """Execute and report: (columns, rows, affected). `count_row`: the database answers a data change with one
    "Count" row instead of a rowcount (DuckDB) - unless the statement returns rows of its own."""
    import re
    statement, args = bind_parameters(sql, params or {}, style, dialect)
    if args is None:
        cursor.execute(statement)
    else:
        cursor.execute(statement, args)
    columns = [d[0] for d in cursor.description] if cursor.description else []
    rows = cursor.fetchall() if cursor.description else []
    returning = re.search(r'\bRETURNING\b', sql, re.IGNORECASE) is not None
    if count_row and not returning:
        affected = int(rows[0][0]) if rows else 0
        return [], [], affected
    if returning:
        affected = len(rows)
    else:
        affected = cursor.rowcount if cursor.rowcount is not None and cursor.rowcount >= 0 else len(rows)
    return columns, rows, affected


def _write_with_ceiling(conn, write, ceiling):
    """Run `write()` -> (columns, rows, affected); commit, or roll back and raise when it changed too many rows."""
    try:
        columns, rows, affected = write()
    except BaseException:
        conn.rollback()
        raise
    if affected > ceiling:
        conn.rollback()
        raise TooManyRowsAffected(affected, ceiling)
    conn.commit()
    return columns, rows, affected


def _write_postgres(details, sql, params, ceiling, timeout=None, pool=None):
    import psycopg2.errors
    driver = _Postgres()

    def run(session):
        session.conn.autocommit = False  # rolled back past its ceiling: needs a real transaction; reset() restores

        def write():
            with session.conn.cursor() as cursor:
                if timeout:
                    cursor.execute(f'SET LOCAL statement_timeout = {_millis(timeout)}')
                try:
                    return _write(cursor, sql, params, 'format', 'postgres')
                except psycopg2.errors.QueryCanceled:
                    raise _timed_out(timeout) from None
        return _write_with_ceiling(session.conn, write, ceiling)
    return _with_session(driver, details, pool, run)


def _write_mysql(details, sql, params, ceiling, timeout=None, pool=None):
    import mysql.connector
    driver = _MySQL()

    def run(session):
        def write():
            cursor = session.conn.cursor()
            try:
                _set_mysql_timeout(cursor, session, timeout)
                try:
                    return _write(cursor, sql, params, 'format', 'mysql')
                except mysql.connector.Error as error:
                    if getattr(error, 'errno', None) in _MYSQL_TIMEOUT_ERRNOS:
                        raise _timed_out(timeout) from None
                    raise
            finally:
                cursor.close()
        return _write_with_ceiling(session.conn, write, ceiling)
    return _with_session(driver, details, pool, run)


def _write_duckdb(details, sql, params, ceiling, timeout=None, pool=None):
    import duckdb
    driver = _DuckDB()

    def run(session):
        conn = session.conn
        conn.begin()  # DuckDB commits each statement by itself otherwise - nothing would be left to roll back

        def write():
            timer = threading.Timer(timeout, conn.interrupt) if timeout else None
            if timer:
                timer.daemon = True
                timer.start()
            try:
                return _write(conn, sql, params, 'qmark', 'duckdb', count_row=True)
            except duckdb.InterruptException:
                raise _timed_out(timeout) from None
            except duckdb.PermissionException as error:
                raise duckfiles.not_allowed(error) from None
            finally:
                if timer:
                    timer.cancel()
        return _write_with_ceiling(conn, write, ceiling)
    return _with_session(driver, details, pool, run)


def _write_sqlite(details, sql, params, ceiling, timeout=None, pool=None):
    conn = sqlite3.connect(_resolve_db_file(details, 'SQLite'), timeout=config.CONNECT_TIMEOUT)
    try:
        expired = []
        if timeout:
            deadline = time.monotonic() + timeout

            def check_deadline():
                if time.monotonic() > deadline:
                    expired.append(True)
                    return 1
                return 0
            conn.set_progress_handler(check_deadline, 10000)

        def write():
            try:
                return _write(conn.cursor(), sql, params, 'qmark', 'sqlite')
            except sqlite3.OperationalError:
                if expired:
                    raise _timed_out(timeout) from None
                raise
        return _write_with_ceiling(conn, write, ceiling)
    finally:
        conn.close()


def _with_session(driver, details, pool, run):
    if pool is None:
        session = Session(driver.connect(details, False), driver)
        try:
            return run(session)
        finally:
            session.close()
    with pool.checkout(driver, details, False) as session:
        return run(session)


WRITE_RUNNERS = {'postgres': _write_postgres, 'mysql': _write_mysql, 'sqlite': _write_sqlite, 'duckdb': _write_duckdb}

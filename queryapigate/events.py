"""`queryapigate events`: a standalone Server-Sent Events server for many clients - apps, phones, dashboards.

The main server's own GET /events (app.stream_events()) holds one request thread per open stream, so it can only
ever serve a handful (config.events_max_streams()). This process serves the same `/events` on its own port
(config.events_port()) with asyncio instead: one thread, thousands of open streams, no new dependency.

**Where events come from.** Not from any one process's memory: from the event log in the store, the `events` table
(ADR 0020). Every run is written there in the same batch as its history row (history.write()) - whatever history's
sample rate - and kept for config.events_retention_hours(). An event's id is its row id there. This process tails
that table, so it sees the runs of every worker and every instance sharing the store - woken by a Postgres NOTIFY the
moment a batch commits, or checking every config.events_poll_interval() seconds on SQLite. A run dropped under
back-pressure, before it was written at all, produces no event.

**Resuming.** A client that reconnects with `Last-Event-ID` (EventSource does this by itself) first gets every
event after that id that it is allowed to see and asked for, a page at a time, then the live stream, with nothing
missed or repeated in between. An event older than the retention window can't be replayed: the client gets the
stream from the oldest one still kept.

**Who sees what.** Exactly as GET /events on the main server: an X-API-Key is required (unless the server runs
with no keys at all); the admin key sees every event, any other key only its own. Subscription filters
(?type=, ?kind=, ?query=, ?connection=, ?status= - parse_filters()) narrow that, never widen it. A stream's key is
checked again every _RECHECK_SECONDS, so a revoked or expired key's stream is closed. A client that stops reading
is disconnected once _QUEUE_SIZE events are waiting for it, rather than holding memory or silently losing events -
it reconnects with Last-Event-ID and catches up.

**Ordering on Postgres.** Ids are handed out when a row is inserted, but transactions in different processes can
commit out of id order, so a smaller id can become visible after a larger one was already delivered. Each check
therefore looks back _LOOKBACK ids behind the newest one delivered and sends any it hasn't seen yet.
"""
import asyncio
import json
import logging
import time
from collections import deque
from datetime import datetime
from urllib.parse import parse_qs, urlsplit

from . import config, cors, db, instances, ratelimit, secheaders, tracing
from .errors import code_for

try:
    import resource  # POSIX only: raising the open-file limit is skipped where it doesn't exist (Windows)
except ImportError:  # pragma: no cover
    resource = None  # type: ignore[assignment]

log = logging.getLogger('queryapigate')

_HEARTBEAT_SECONDS = 15.0   # a comment line this often keeps proxies and NAT from closing an idle stream
_RECHECK_SECONDS = 60.0     # how often an open stream's key is checked again
_QUEUE_SIZE = 1000          # events waiting for one client before it is disconnected as too slow
_REPLAY_PAGE = 1000         # events read per page of a Last-Event-ID resume
_LOOKBACK = 1000            # ids behind the newest delivered that each check looks at again (see above)
_BATCH = 1000               # rows fetched per check
_REQUEST_TIMEOUT = 10.0     # seconds a client gets to send its request line and headers
_MAX_HEADER_LINES = 100
_WRITE_TIMEOUT = 30.0       # seconds a write may wait on a client's full socket buffer
_RETRY_MS = 3000            # reconnection delay suggested to EventSource clients
_SWEEP_LOCK_ID = 0x51A8     # next to history's: the advisory lock one instance's retention sweep holds
_SWEEP_CHUNK = 5_000        # events deleted per sweep transaction
_LAG_BUCKETS = (0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0, 300.0)  # seconds; occurred_at has one-second precision

RUN = 'query.run'  # the event type of a run, saved or ad-hoc - the only type written so far
TIME_FORMAT = '%Y-%m-%d %H:%M:%S'
INSERT = ('INSERT INTO events (type, source, occurred_at, key_name, connection_name, query_name, status, '
          'payload_json, workspace) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)')
INSERT_WITH_ID = ('INSERT INTO events (id, type, source, occurred_at, key_name, connection_name, query_name, status, '
                  'payload_json, workspace) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)')


def run_payload(query_name, version, entry):
    """The JSON a run's event carries (ADR 0020 §5), on both /events endpoints: `query.run`, for a saved query's run
    (`kind` saved) or ad-hoc SQL (`kind` adhoc, `query` and `version` null - its entry carries the SQL as
    QUERYAPIGATE_HISTORY_ADHOC_SQL keeps it). `kind`, `query` and `version` are GET /api/v1/history's names."""
    return {'type': RUN, 'source': entry.get('transport'),
            'occurred_at': entry.get('executed_at') or datetime.now().strftime(TIME_FORMAT),
            'kind': 'saved' if query_name is not None else 'adhoc', 'query': query_name, 'version': version,
            'connection_name': entry.get('connection_name'), 'entry': entry}


def run_columns(query_name, version, entry, workspace=None):
    """A run's row in the events table, for INSERT - every column after the id; `workspace` the run's (ADR 0023),
    the workspace this code acts in unless given."""
    from . import scope
    payload = run_payload(query_name, version, entry)
    return (RUN, payload['source'], payload['occurred_at'], entry.get('key_name'), entry.get('connection_name'),
            query_name, entry.get('status'), json.dumps(payload, default=str), workspace or scope.current())


# ---- subscription filters (ADR 0020 §6) ----

FILTERS = ('type', 'kind', 'query', 'connection', 'status')
_FILTER_COLUMNS = {'type': 'type', 'query': 'query_name', 'connection': 'connection_name', 'status': 'status'}


def parse_filters(get):
    """{filter: value} from a request's query string - ``get(name)`` returns a parameter's value, or None. Raises
    ValueError, saying what's wrong, for a `status` or `kind` that can never match; any other value simply matches
    nothing until something has that name."""
    filters = {name: get(name) for name in FILTERS if get(name) not in (None, '')}
    if filters.get('status') not in (None, 'success', 'error'):
        raise ValueError("status must be 'success' or 'error'")
    if filters.get('kind') not in (None, 'saved', 'adhoc'):
        raise ValueError("kind must be 'saved' or 'adhoc'")
    return filters


def fields(payload):
    """What filters compare, from an event's payload: (type, kind, query, connection, status)."""
    return (payload.get('type'), payload.get('kind'), payload.get('query'), payload.get('connection_name'),
            (payload.get('entry') or {}).get('status'))


def matches(filters, event_fields):
    """Whether an event with these fields() passes every filter."""
    return all(value == event_fields[FILTERS.index(name)] for name, value in filters.items())


def _filter_sql(filters):
    clauses, params = [], []
    for name, value in filters.items():
        if name == 'kind':
            clauses.append('query_name IS NOT NULL' if value == 'saved' else 'query_name IS NULL')
        else:
            clauses.append(f'{_FILTER_COLUMNS[name]} = ?')
            params.append(value)
    return ''.join(f' AND {c}' for c in clauses), params


def _chunk(data):
    """One piece of the stream in HTTP/1.1 chunked framing. Without it the body is delimited only by the connection
    closing, and some clients (Python's requests, for one) then wait for the close before handing over anything."""
    return b'%x\r\n%s\r\n' % (len(data), data)


def _format(event_id, payload_json):
    return f'id: {event_id}\ndata: {payload_json}\n\n'.encode()


# ---- store access: plain blocking calls, run in a worker thread (asyncio.to_thread) ----

_COLUMNS = 'id, key_name, type, query_name, connection_name, status, occurred_at, payload_json'


def _newest_id():
    row = db.connection().execute('SELECT MAX(id) FROM events').fetchone()
    return row[0] or 0


def _ids_after(floor):
    return [row[0] for row in db.connection().execute(
        'SELECT id FROM events WHERE id > ? ORDER BY id LIMIT ?', (floor, _BATCH + _LOOKBACK))]


def _rows(ids):
    rows = []
    for start in range(0, len(ids), 500):  # well under SQLite's bound-parameter limit on older builds (999)
        chunk = ids[start:start + 500]
        rows += db.connection().execute(
            f'SELECT {_COLUMNS} FROM events WHERE id IN ({", ".join("?" * len(chunk))}) ORDER BY id',
            tuple(chunk)).fetchall()
    return rows


def _key_since(key_name):
    """When the API key named ``key_name`` was created, or None (a signed-in user, an administrator). A key sees no
    event from before then: a deleted key's name can be given to a new one, and its runs aren't the new key's
    (ADR 0021)."""
    row = db.connection().execute('SELECT created_at FROM api_keys WHERE name = ?', (key_name,)).fetchone()
    return row[0] if row else None


def _replay_page(after, key_name, filters, since=None):
    """One page of the events after ``after`` that ``key_name`` may see (None: every event) and ``filters`` ask for,
    from ``since`` on, oldest first."""
    where, params = _filter_sql(filters)
    if since is not None:
        where, params = ' AND occurred_at >= ?' + where, [since, *params]
    if key_name is not None:
        where, params = ' AND key_name = ?' + where, [key_name, *params]
    return db.connection().execute(
        f'SELECT id, payload_json FROM events WHERE id > ?{where} ORDER BY id LIMIT ?',
        (after, *params, _REPLAY_PAGE)).fetchall()


def sweep(now=None):
    """Delete events older than config.events_retention_hours(), in chunks. Returns how many were deleted. Run by each
    process's history writer (history.py) every few minutes; on PostgreSQL only by the instance holding the sweep
    lock, so several don't delete the same rows in parallel."""
    from datetime import timedelta
    cutoff = ((now or datetime.now()) - timedelta(hours=config.events_retention_hours())).strftime(TIME_FORMAT)
    conn = db.connection()
    try:
        if db.is_postgres() and not conn.execute('SELECT pg_try_advisory_lock(?)', (_SWEEP_LOCK_ID,)).fetchone()[0]:
            return 0  # another instance is sweeping right now
    except (OSError, *db.Error):
        log.warning('Event log retention sweep failed; the next one retries', exc_info=True)
        return 0
    deleted = 0
    try:
        while True:
            with db.transaction(append_only=True) as tx:
                count = tx.execute('DELETE FROM events WHERE id IN (SELECT id FROM events WHERE occurred_at < ? '
                                   'LIMIT ?)', (cutoff, _SWEEP_CHUNK)).rowcount
            deleted += count
            if count < _SWEEP_CHUNK:
                return deleted
    except (OSError, *db.Error):
        log.warning('Event log retention sweep failed; the next one retries', exc_info=True)
        return deleted
    finally:
        if db.is_postgres():
            try:
                conn.execute('SELECT pg_advisory_unlock(?)', (_SWEEP_LOCK_ID,))
            except (OSError, *db.Error):
                pass  # a lost connection takes its session lock with it


def _authenticate(credentials, client_ip):
    """The caller's Permission from (X-API-Key, Authorization) - every front door's rules (governance.py): an API
    key, or a signed-in user's bearer token (jwtauth.py)."""
    from .governance import authenticate
    return authenticate(credentials[0], credentials[1], client_ip)


def _lag(occurred_at):
    """Seconds from when an event happened (server local time, to the second) to now."""
    try:
        return max(0.0, time.time() - time.mktime(time.strptime(occurred_at, TIME_FORMAT)))
    except (TypeError, ValueError):
        return None


class _Client:
    __slots__ = ('queue', 'key_name', 'filters', 'since', 'writer', 'too_slow')

    def __init__(self, key_name, writer, filters=None, since=None):
        self.queue = asyncio.Queue(maxsize=_QUEUE_SIZE)
        self.key_name = key_name  # None: sees every event
        self.filters = filters or {}
        self.since = since  # an API key's created_at: nothing older is its (_key_since())
        self.writer = writer
        self.too_slow = False


class EventServer:
    def __init__(self, host='127.0.0.1', port=None, max_connections=None, poll_interval=None):
        self.host = host
        self.port = config.events_port() if port is None else port
        self.max_connections = max_connections or config.events_max_connections()
        self.poll_interval = poll_interval or config.events_poll_interval()
        self.clients = set()
        self.by_key = {}            # key name (None for "everything") -> set of clients
        self.cursor = 0             # newest event id delivered
        self.recent = set()         # ids delivered in the lookback window
        self.recent_order = deque()
        self.wake = None            # asyncio.Event, created in start() - on its own event loop
        self.limiter = ratelimit.client_limiter(config.redis_url())  # shared with every instance's REST limit
        self.server = None
        self.tasks = []
        self.delivered = 0          # events taken off the log and queued to their clients
        self.replayed = 0           # events sent on a Last-Event-ID resume
        self.slow_disconnects = 0   # clients cut off for falling _QUEUE_SIZE events behind
        self.lag = [0] * (len(_LAG_BUCKETS) + 1)  # occurred_at to delivery, per bucket (the last: +Inf)
        self.lag_sum = 0.0

    # ---- lifecycle ----

    async def start(self):
        _raise_open_file_limit()
        self.wake = asyncio.Event()
        self.cursor = await asyncio.to_thread(_newest_id)
        for rowid in await asyncio.to_thread(_ids_after, max(0, self.cursor - _LOOKBACK)):
            if rowid <= self.cursor:  # already there before this server started: not news to anyone
                self._remember(rowid)
        self.server = await asyncio.start_server(self._handle, self.host, self.port, limit=16384)
        self.port = self.server.sockets[0].getsockname()[1]
        self.tasks = [asyncio.create_task(self._tail())]
        if db.is_postgres():
            self.tasks.append(asyncio.create_task(self._listen()))
        log.info('Serving events at http://%s:%s/events (metadata: %s)', self.host, self.port, db.describe())

    async def stop(self):
        for task in self.tasks:
            task.cancel()
        if self.server is not None:
            self.server.close()
        for client in list(self.clients):  # end every stream now, not at its next heartbeat
            client.too_slow = True
            while not client.queue.empty():
                client.queue.get_nowait()
            client.queue.put_nowait(None)
        await asyncio.sleep(0)  # let each stream's handler see that and close its socket
        if self.server is not None:
            await self.server.wait_closed()

    # ---- where events come from ----

    async def _tail(self):
        while True:
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=self.poll_interval)
            except asyncio.TimeoutError:
                pass
            self.wake.clear()
            try:
                await asyncio.to_thread(instances.heartbeat)  # a no-op until HEARTBEAT has passed
                while await self._check():
                    pass
            except Exception:  # a store hiccup: log it, and the next round simply tries again
                log.warning('Events: checking for new runs failed', exc_info=True)

    async def _check(self):
        """Deliver every event written since the last check. True when there may be more right away."""
        ids = await asyncio.to_thread(_ids_after, max(0, self.cursor - _LOOKBACK))
        new = [i for i in ids if i not in self.recent]
        if not new:
            return False
        for event_id, key_name, type_, query_name, connection, status, occurred_at, payload_json in \
                await asyncio.to_thread(_rows, new[:_BATCH]):
            kind = 'saved' if query_name is not None else 'adhoc'
            self._deliver(event_id, key_name, (type_, kind, query_name, connection, status),
                          _format(event_id, payload_json), occurred_at)
        return len(new) > _BATCH or len(ids) >= _BATCH + _LOOKBACK

    def _remember(self, rowid):
        self.recent.add(rowid)
        self.recent_order.append(rowid)
        self.cursor = max(self.cursor, rowid)
        while self.recent_order and self.recent_order[0] < self.cursor - _LOOKBACK:
            self.recent.discard(self.recent_order.popleft())

    def _deliver(self, rowid, key_name, event_fields, data, occurred_at=None):
        self._remember(rowid)
        for client in (*self.by_key.get(None, ()), *self.by_key.get(key_name, ())):
            if client.too_slow or not matches(client.filters, event_fields) \
                    or (client.since is not None and (occurred_at or '') < client.since):
                continue
            try:
                client.queue.put_nowait((rowid, data))
            except asyncio.QueueFull:
                # Too far behind: cut it off rather than hold memory or silently skip events - it reconnects with
                # Last-Event-ID and catches up from the log. The queue is emptied so the sentinel fits.
                client.too_slow = True
                self.slow_disconnects += 1
                while not client.queue.empty():
                    client.queue.get_nowait()
                client.queue.put_nowait(None)
        self.delivered += 1
        lag = _lag(occurred_at)
        if lag is not None:
            self.lag[next((i for i, edge in enumerate(_LAG_BUCKETS) if lag <= edge), len(_LAG_BUCKETS))] += 1
            self.lag_sum += lag

    async def _listen(self):
        """Postgres only: LISTEN on history's channel and wake _tail() the moment a batch commits."""
        import psycopg2

        from . import history
        loop = asyncio.get_running_loop()
        while True:
            conn = None
            try:
                conn = await asyncio.to_thread(psycopg2.connect, config.database_url(),
                                               connect_timeout=config.CONNECT_TIMEOUT)
                conn.autocommit = True
                with conn.cursor() as cursor:
                    cursor.execute(f'LISTEN {history.NOTIFY_CHANNEL}')
                lost = loop.create_future()

                def readable(conn=conn, lost=lost):
                    try:
                        conn.poll()
                    except Exception as error:  # the connection dropped: reconnect below
                        if not lost.done():
                            lost.set_exception(error)
                        return
                    if conn.notifies:
                        conn.notifies.clear()
                        self.wake.set()
                loop.add_reader(conn.fileno(), readable)
                self.wake.set()  # anything committed while (re)connecting
                try:
                    await lost
                finally:
                    loop.remove_reader(conn.fileno())
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning('Events: lost the PostgreSQL notification connection; reconnecting', exc_info=True)
            finally:
                if conn is not None:
                    conn.close()
            await asyncio.sleep(2)

    # ---- one client ----

    async def _handle(self, reader, writer):
        try:
            request = await asyncio.wait_for(_read_request(reader), timeout=_REQUEST_TIMEOUT)
        except Exception:  # timed out, malformed, oversized (LimitOverrunError) or gone: nothing to answer
            writer.close()
            return
        method, target, headers = request
        path, query = urlsplit(target).path, parse_qs(urlsplit(target).query)
        origin = headers.get('origin')
        allow_origin = cors.allow_origin_value(origin)
        try:
            if path == '/health' and method == 'GET':
                await _respond(writer, 200, {'status': 'ok', 'connections': len(self.clients),
                                             'delivered': self.delivered}, allow_origin)
            elif path == '/metrics' and method == 'GET':
                from . import metrics
                if metrics.authorized(headers.get('authorization')):
                    await _respond_text(writer, 200, self.render_metrics())
                else:
                    await _respond(writer, 401, {'error': metrics.UNAUTHORIZED}, allow_origin)
            elif path != '/events':
                await _respond(writer, 404, {'error': 'Not found'}, allow_origin)
            elif method == 'OPTIONS' and allow_origin and 'access-control-request-method' in headers:
                await _respond(writer, 204, None, allow_origin, preflight=True)
            elif method != 'GET':
                await _respond(writer, 405, {'error': 'Method not allowed'}, allow_origin)
            else:
                await self._stream(reader, writer, headers, query, allow_origin)
        except (ConnectionError, asyncio.TimeoutError):
            pass
        except Exception:  # e.g. the store unreachable while checking a key: drop this client, keep serving
            log.warning('Events: a %s %s request failed', method, path, exc_info=True)
        finally:
            writer.close()

    async def _stream(self, reader, writer, headers, query, allow_origin):
        client_ip = _client_ip(writer, headers)
        limit = config.rate_limit()
        if limit is not None and not self.limiter.hit(client_ip, *limit)[0]:
            await _respond(writer, 429, {'error': 'Rate limit exceeded'}, allow_origin)
            return
        if len(self.clients) >= self.max_connections:
            await _respond(writer, 503, {'error': 'Too many open event streams - try again shortly',
                                         'code': 'too_many_streams'}, allow_origin,
                           extra={'Retry-After': '5'})
            return
        secret = (headers.get('x-api-key', ''), headers.get('authorization', ''))
        permission = await asyncio.to_thread(_authenticate, secret, client_ip)
        if permission is None:
            await _respond(writer, 401, {'error': 'Unauthorized'}, allow_origin)
            return
        # One span for opening the stream (tracing.py) - not one per event: a stream can stay open for hours
        with tracing.span('GET /events', 'server', {
                'http.request.method': 'GET', 'http.route': '/events', 'client.address': client_ip,
                'queryapigate.caller': permission.name or '-', 'http.response.status_code': 200},
                parent=tracing.extract(headers)):
            pass
        try:
            filters = parse_filters(lambda name: (query.get(name) or [None])[0])
        except ValueError as error:
            await _respond(writer, 400, {'error': str(error), 'code': 'invalid_filter'}, allow_origin)
            return
        last_id = _last_event_id(headers, query)
        key_name = None if permission.admin else permission.name
        since = None if key_name is None else await asyncio.to_thread(_key_since, key_name)
        client = _Client(key_name, writer, filters, since)
        self.clients.add(client)
        self.by_key.setdefault(key_name, set()).add(client)  # live events queue up from here on...
        try:
            head = ['HTTP/1.1 200 OK', 'Content-Type: text/event-stream', 'Cache-Control: no-cache',
                    'X-Accel-Buffering: no', 'Transfer-Encoding: chunked', 'Connection: close', *_SECURITY_HEADERS]
            if allow_origin:
                head += [f'Access-Control-Allow-Origin: {allow_origin}', 'Vary: Origin']
            writer.write(('\r\n'.join(head) + '\r\n\r\n').encode() + _chunk(f'retry: {_RETRY_MS}\n\n'.encode()))
            sent = 0
            if last_id is not None:  # ...while what was missed is sent first, so nothing falls in between
                after = last_id  # only what is replayed moves `sent`: an id from another store mustn't mute live events
                while not client.too_slow:  # every page, up to the live stream (ADR 0020 §7)
                    page = await asyncio.to_thread(_replay_page, after, key_name, filters, since)
                    for event_id, payload_json in page:
                        writer.write(_chunk(_format(event_id, payload_json)))
                        sent = after = event_id
                    self.replayed += len(page)
                    await asyncio.wait_for(writer.drain(), timeout=_WRITE_TIMEOUT)
                    if len(page) < _REPLAY_PAGE:
                        break
            watcher = asyncio.create_task(self._watch(reader, client))
            try:
                await self._pump(client, secret, client_ip, sent)
                if not writer.is_closing():
                    writer.write(b'0\r\n\r\n')  # the last chunk: the stream ended cleanly
            finally:
                watcher.cancel()
        finally:
            self.clients.discard(client)
            self.by_key.get(key_name, set()).discard(client)

    async def _watch(self, reader, client):
        """End the stream as soon as the client hangs up, rather than at the next heartbeat write - otherwise a
        client that leaves keeps its slot (and counts against max_connections) for up to _HEARTBEAT_SECONDS. A
        client sends nothing after its request, so any read returning is either EOF or a misbehaving client."""
        try:
            await reader.read(1)
        except Exception:  # reset by peer etc. - gone either way
            pass
        client.too_slow = True
        while not client.queue.empty():
            client.queue.get_nowait()
        client.queue.put_nowait(None)

    async def _pump(self, client, secret, client_ip, sent):
        writer = client.writer
        next_check = time.monotonic() + _RECHECK_SECONDS
        while not writer.is_closing():
            try:
                item = await asyncio.wait_for(client.queue.get(), timeout=_HEARTBEAT_SECONDS)
            except asyncio.TimeoutError:
                writer.write(_chunk(b': keepalive\n\n'))
            else:
                if item is None:
                    return  # too slow: see _deliver()
                rowid, data = item
                if rowid <= sent:
                    continue  # already sent as part of the replay
                writer.write(_chunk(data))
            await asyncio.wait_for(writer.drain(), timeout=_WRITE_TIMEOUT)
            if time.monotonic() >= next_check:
                next_check = time.monotonic() + _RECHECK_SECONDS
                if await asyncio.to_thread(_authenticate, secret, client_ip) is None:
                    return  # key revoked/expired/deactivated, or the bearer token expired, since the stream opened


    def render_metrics(self):
        """This process's own counts, as Prometheus text (ADR 0020 §8)."""
        lines = ['# HELP queryapigate_events_streams Event streams open right now.',
                 '# TYPE queryapigate_events_streams gauge', f'queryapigate_events_streams {len(self.clients)}']
        for name, help_text, value in (
                ('delivered', 'Events taken off the event log and queued to their clients.', self.delivered),
                ('replayed', 'Events sent to clients resuming with Last-Event-ID.', self.replayed),
                ('slow_disconnects', 'Clients disconnected for falling too far behind.', self.slow_disconnects)):
            lines += [f'# HELP queryapigate_events_{name}_total {help_text}',
                      f'# TYPE queryapigate_events_{name}_total counter', f'queryapigate_events_{name}_total {value}']
        lines += ['# HELP queryapigate_events_lag_seconds From when an event happened to its delivery (to the second).',
                  '# TYPE queryapigate_events_lag_seconds histogram']
        cumulative = 0
        for edge, count in zip(_LAG_BUCKETS, self.lag, strict=False):
            cumulative += count
            lines.append(f'queryapigate_events_lag_seconds_bucket{{le="{edge}"}} {cumulative}')
        cumulative += self.lag[-1]
        lines += [f'queryapigate_events_lag_seconds_bucket{{le="+Inf"}} {cumulative}',
                  f'queryapigate_events_lag_seconds_sum {self.lag_sum}',
                  f'queryapigate_events_lag_seconds_count {cumulative}']
        return '\n'.join(lines) + '\n'


# ---- small HTTP helpers ----

async def _read_request(reader):
    line = (await reader.readline()).decode('latin-1').rstrip('\r\n')
    parts = line.split(' ')
    if len(parts) != 3 or not parts[2].startswith('HTTP/1.'):
        raise ValueError('not an HTTP/1.x request line')
    headers = {}
    for _ in range(_MAX_HEADER_LINES):
        raw = (await reader.readline()).decode('latin-1').rstrip('\r\n')
        if not raw:
            return parts[0].upper(), parts[1], headers
        name, sep, value = raw.partition(':')
        if not sep:
            raise ValueError('malformed header')
        headers[name.strip().lower()] = value.strip()
    raise ValueError('too many headers')


# secheaders.py's, but Cache-Control: the stream says no-cache, everything else no-store
_SECURITY_HEADERS = [f'{name}: {value}' for name, value in secheaders.api_headers() if name != 'Cache-Control']


async def _respond(writer, status, body, allow_origin, preflight=False, extra=None):
    reasons = {200: 'OK', 204: 'No Content', 400: 'Bad Request', 401: 'Unauthorized', 404: 'Not Found',
               405: 'Method Not Allowed', 429: 'Too Many Requests', 503: 'Service Unavailable'}
    if body is not None and 'error' in body:  # the same {error, code} every front door answers with (errors.py)
        body = {'error': body['error'], 'code': code_for(status, body.get('code'))}
    payload = b'' if body is None else json.dumps(body).encode()
    head = [f'HTTP/1.1 {status} {reasons.get(status, "")}', f'Content-Length: {len(payload)}', 'Connection: close',
            *_SECURITY_HEADERS, 'Cache-Control: no-store']
    if body is not None:
        head.append('Content-Type: application/json')
    if allow_origin:
        head += [f'Access-Control-Allow-Origin: {allow_origin}', 'Vary: Origin']
        if preflight:
            head += ['Access-Control-Allow-Methods: GET, OPTIONS',
                     'Access-Control-Allow-Headers: X-API-Key, Authorization, Last-Event-ID',
                     f'Access-Control-Max-Age: {cors.PREFLIGHT_MAX_AGE}']
    head += [f'{name}: {value}' for name, value in (extra or {}).items()]
    writer.write(('\r\n'.join(head) + '\r\n\r\n').encode() + payload)
    await asyncio.wait_for(writer.drain(), timeout=_WRITE_TIMEOUT)


async def _respond_text(writer, status, text):
    payload = text.encode()
    head = [f'HTTP/1.1 {status} OK', f'Content-Length: {len(payload)}', 'Connection: close', *_SECURITY_HEADERS,
            'Cache-Control: no-store', 'Content-Type: text/plain; version=0.0.4; charset=utf-8']
    writer.write(('\r\n'.join(head) + '\r\n\r\n').encode() + payload)
    await asyncio.wait_for(writer.drain(), timeout=_WRITE_TIMEOUT)


def _last_event_id(headers, query):
    """The id to resume after: the Last-Event-ID header (sent by EventSource on reconnect), or ?last_event_id= for
    a client that can't set headers. Anything that isn't a whole number starts a fresh, live-only stream."""
    raw = headers.get('last-event-id') or (query.get('last_event_id') or [''])[0]
    return int(raw) if raw.isdigit() else None


def _client_ip(writer, headers):
    peer = writer.get_extra_info('peername')
    address = peer[0] if peer else None
    hops = config.proxy_hops()
    if hops and headers.get('x-forwarded-for'):
        forwarded = [part.strip() for part in headers['x-forwarded-for'].split(',') if part.strip()]
        if len(forwarded) >= hops:
            return forwarded[-hops]
    return address


def _raise_open_file_limit():
    """Each open stream is a socket: lift the soft file-descriptor limit as far as the hard limit allows."""
    if resource is None:
        return
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        wanted = hard if hard != resource.RLIM_INFINITY else max(soft, 65536)
        if soft != resource.RLIM_INFINITY and soft < wanted:
            resource.setrlimit(resource.RLIMIT_NOFILE, (wanted, hard))
    except (ValueError, OSError):
        pass


def run(host='127.0.0.1', port=None):
    """`queryapigate events`: serve until interrupted."""
    async def main():
        server = EventServer(host, port)
        await server.start()
        try:
            await asyncio.Event().wait()
        finally:
            await server.stop()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

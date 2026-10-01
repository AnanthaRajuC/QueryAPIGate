"""`queryapigate events`: a standalone Server-Sent Events server for many clients - apps, phones, dashboards.

The main server's own GET /events (app.stream_events()) holds one request thread per open stream, so it can only
ever serve a handful (config.events_max_streams()). This process serves the same `/events` on its own port
(config.events_port()) with asyncio instead: one thread, thousands of open streams, no new dependency.

**Where events come from.** Not from any one process's memory: from the store itself. Every run recorded in
execution_history (history.py) is an event, and its row id is the event's SSE `id:`. This process tails that
table, so it sees the runs of every worker and every instance sharing the store - woken by a Postgres NOTIFY the
moment a batch commits (history.write()), or checking every config.events_poll_interval() seconds on SQLite.
Runs that history doesn't keep (sampled out, or dropped under back-pressure) produce no event.

**Resuming.** A client that reconnects with `Last-Event-ID` (EventSource does this by itself) first gets every
run after that id that it is allowed to see - up to _REPLAY_LIMIT - then the live stream, with nothing missed or
repeated in between. A run trimmed from history before the client came back (a version keeps only its newest
QUERYAPIGATE_HISTORY_LIMIT runs unless QUERYAPIGATE_HISTORY_RETENTION_DAYS is set) can't be replayed.

**Who sees what.** Exactly as GET /events on the main server: an X-API-Key is required (unless the server runs
with no keys at all); the admin key sees every run, any other key only its own. A stream's key is checked again
every _RECHECK_SECONDS, so a revoked or expired key's stream is closed. A client that stops reading is
disconnected once _QUEUE_SIZE events are waiting for it, rather than holding memory or silently losing events -
it reconnects with Last-Event-ID and catches up.

**Ordering on Postgres.** Row ids are handed out when a row is inserted, but transactions in different processes
can commit out of id order, so a smaller id can become visible after a larger one was already delivered. Each
check therefore looks back _LOOKBACK ids behind the newest one delivered and sends any it hasn't seen yet.
"""
import asyncio
import json
import logging
import time
from collections import deque
from urllib.parse import parse_qs, urlsplit

from . import config, cors, db, history
from .ratelimit import RateLimiter

try:
    import resource  # POSIX only: raising the open-file limit is skipped where it doesn't exist (Windows)
except ImportError:  # pragma: no cover
    resource = None  # type: ignore[assignment]

log = logging.getLogger('queryapigate')

_HEARTBEAT_SECONDS = 15.0   # a comment line this often keeps proxies and NAT from closing an idle stream
_RECHECK_SECONDS = 60.0     # how often an open stream's key is checked again
_QUEUE_SIZE = 1000          # events waiting for one client before it is disconnected as too slow
_REPLAY_LIMIT = 1000        # runs sent on a Last-Event-ID resume
_LOOKBACK = 1000            # ids behind the newest delivered that each check looks at again (see above)
_BATCH = 1000               # rows fetched per check
_REQUEST_TIMEOUT = 10.0     # seconds a client gets to send its request line and headers
_MAX_HEADER_LINES = 100
_WRITE_TIMEOUT = 30.0       # seconds a write may wait on a client's full socket buffer
_RETRY_MS = 3000            # reconnection delay suggested to EventSource clients


def event_payload(query_name, version, entry):
    """The JSON an event carries - the same shape the main server's GET /events uses."""
    return {'type': 'execution', 'filename': query_name, 'version': version,
            'connection_name': entry.get('connection_name'), 'entry': entry}


def _format(rowid, query_name, version, entry_json):
    entry = json.loads(entry_json)
    return f'id: {rowid}\ndata: {json.dumps(event_payload(query_name, version, entry))}\n\n'.encode()


# ---- store access: plain blocking calls, run in a worker thread (asyncio.to_thread) ----

def _newest_id():
    row = db.connection().execute('SELECT MAX(rowid) FROM execution_history').fetchone()
    return row[0] or 0


def _ids_after(floor):
    return [row[0] for row in db.connection().execute(
        'SELECT rowid FROM execution_history WHERE rowid > ? ORDER BY rowid LIMIT ?', (floor, _BATCH + _LOOKBACK))]


def _rows(ids):
    rows = []
    for start in range(0, len(ids), 500):  # well under SQLite's bound-parameter limit on older builds (999)
        chunk = ids[start:start + 500]
        rows += db.connection().execute(
            f'SELECT rowid, query_name, version, key_name, entry_json FROM execution_history '
            f'WHERE rowid IN ({", ".join("?" * len(chunk))}) ORDER BY rowid', tuple(chunk)).fetchall()
    return rows


def _replay(after, key_name):
    """Runs after ``after`` that ``key_name`` may see (None: every run), oldest first."""
    if key_name is None:
        return db.connection().execute(
            'SELECT rowid, query_name, version, entry_json FROM execution_history WHERE rowid > ? '
            'ORDER BY rowid LIMIT ?', (after, _REPLAY_LIMIT)).fetchall()
    return db.connection().execute(
        'SELECT rowid, query_name, version, entry_json FROM execution_history WHERE rowid > ? AND key_name = ? '
        'ORDER BY rowid LIMIT ?', (after, key_name, _REPLAY_LIMIT)).fetchall()


def _authenticate(credentials, client_ip):
    """The caller's Permission from (X-API-Key, Authorization) - exactly the main server's rules
    (app.authenticate_headers()): an API key, or a signed-in user's bearer token (jwtauth.py)."""
    from .app import authenticate_headers  # imported here: app pulls in Flask, which `events` needs no more of
    return authenticate_headers(credentials[0], credentials[1], client_ip)


class _Client:
    __slots__ = ('queue', 'key_name', 'writer', 'too_slow')

    def __init__(self, key_name, writer):
        self.queue = asyncio.Queue(maxsize=_QUEUE_SIZE)
        self.key_name = key_name  # None: sees every run
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
        self.limiter = RateLimiter()
        self.server = None
        self.tasks = []
        self.delivered = 0

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
                while await self._check():
                    pass
            except Exception:  # a store hiccup: log it, and the next round simply tries again
                log.warning('Events: checking for new runs failed', exc_info=True)

    async def _check(self):
        """Deliver every run recorded since the last check. True when there may be more right away."""
        ids = await asyncio.to_thread(_ids_after, max(0, self.cursor - _LOOKBACK))
        new = [i for i in ids if i not in self.recent]
        if not new:
            return False
        for rowid, query_name, version, key_name, entry_json in await asyncio.to_thread(_rows, new[:_BATCH]):
            self._deliver(rowid, key_name, _format(rowid, query_name, version, entry_json))
        return len(new) > _BATCH or len(ids) >= _BATCH + _LOOKBACK

    def _remember(self, rowid):
        self.recent.add(rowid)
        self.recent_order.append(rowid)
        self.cursor = max(self.cursor, rowid)
        while self.recent_order and self.recent_order[0] < self.cursor - _LOOKBACK:
            self.recent.discard(self.recent_order.popleft())

    def _deliver(self, rowid, key_name, data):
        self._remember(rowid)
        for client in (*self.by_key.get(None, ()), *self.by_key.get(key_name, ())):
            if client.too_slow:
                continue
            try:
                client.queue.put_nowait((rowid, data))
            except asyncio.QueueFull:
                # Too far behind: cut it off rather than hold memory or silently skip events - it reconnects with
                # Last-Event-ID and catches up from history. The queue is emptied so the sentinel fits.
                client.too_slow = True
                while not client.queue.empty():
                    client.queue.get_nowait()
                client.queue.put_nowait(None)
        self.delivered += 1

    async def _listen(self):
        """Postgres only: LISTEN on history's channel and wake _tail() the moment a batch commits."""
        import psycopg2
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
            await _respond(writer, 503, {'error': 'Too many open event streams - try again shortly'}, allow_origin,
                           extra={'Retry-After': '5'})
            return
        secret = (headers.get('x-api-key', ''), headers.get('authorization', ''))
        permission = await asyncio.to_thread(_authenticate, secret, client_ip)
        if permission is None:
            await _respond(writer, 401, {'error': 'Unauthorized'}, allow_origin)
            return
        last_id = _last_event_id(headers, query)
        key_name = None if permission.admin else permission.name
        client = _Client(key_name, writer)
        self.clients.add(client)
        self.by_key.setdefault(key_name, set()).add(client)  # live events queue up from here on...
        try:
            head = ['HTTP/1.1 200 OK', 'Content-Type: text/event-stream', 'Cache-Control: no-cache',
                    'X-Accel-Buffering: no', 'Connection: close']
            if allow_origin:
                head += [f'Access-Control-Allow-Origin: {allow_origin}', 'Vary: Origin']
            writer.write(('\r\n'.join(head) + '\r\n\r\n' + f'retry: {_RETRY_MS}\n\n').encode())
            sent = 0
            if last_id is not None:  # ...while what was missed is sent first, so nothing falls in between
                for rowid, query_name, version, entry_json in await asyncio.to_thread(_replay, last_id, key_name):
                    writer.write(_format(rowid, query_name, version, entry_json))
                    sent = rowid
                await asyncio.wait_for(writer.drain(), timeout=_WRITE_TIMEOUT)
            watcher = asyncio.create_task(self._watch(reader, client))
            try:
                await self._pump(client, secret, client_ip, sent)
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
                writer.write(b': keepalive\n\n')
            else:
                if item is None:
                    return  # too slow: see _deliver()
                rowid, data = item
                if rowid <= sent:
                    continue  # already sent as part of the replay
                writer.write(data)
            await asyncio.wait_for(writer.drain(), timeout=_WRITE_TIMEOUT)
            if time.monotonic() >= next_check:
                next_check = time.monotonic() + _RECHECK_SECONDS
                if await asyncio.to_thread(_authenticate, secret, client_ip) is None:
                    return  # key revoked/expired/deactivated, or the bearer token expired, since the stream opened


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


async def _respond(writer, status, body, allow_origin, preflight=False, extra=None):
    reasons = {200: 'OK', 204: 'No Content', 401: 'Unauthorized', 404: 'Not Found', 405: 'Method Not Allowed',
               429: 'Too Many Requests', 503: 'Service Unavailable'}
    payload = b'' if body is None else json.dumps(body).encode()
    head = [f'HTTP/1.1 {status} {reasons.get(status, "")}', f'Content-Length: {len(payload)}', 'Connection: close']
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

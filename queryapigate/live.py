"""Live queries (ADR 0033, a prototype behind QUERYAPIGATE_LIVE_QUERIES): a client subscribes to a saved query with its
parameters, `GET /live/<query>?<params>` on `queryapigate events`, and gets its result - then the result again each
time it changes, because a table the query reads changed.

**Nothing new decides access.** Every result is an ordinary run of the saved query, as the subscriber: its key's or
signed-in user's grants, `from_claim`, masking, quotas, run history (transport `live`). A key that may run the query
may subscribe to it; one that may not gets the same 403 or 404 that `GET /q/<name>` would answer.

**Change signals carry no row data.** A signal is a `table.changed` event in the event log (ADR 0020): a connection
and the tables that changed there - `null` when they couldn't be told, which means "anything on that connection". It
is written by:

- QueryAPIGate's own writes: a successful write through `engine.execute_sql()` (saved write queries, ad-hoc SQL, an
  agent's approved write) signals the tables the statement touches (note_write());
- Debezium Server's HTTP sink, posting to `POST /ingest/cdc/<source>` on the main server (`/w/<workspace>/...`
  outside `default`), each request signed with Standard Webhooks (verify_signature()) using the source's secret
  (cdcsources.py, ADR 0033 §11 - managed through /api/v1/cdc-sources, or QUERYAPIGATE_CDC_SOURCES); its changes are
  reduced to the tables they touched (debezium_tables()) and stored as one signal before the request is answered, so
  Debezium only moves on once it's durable.

Because the signal is an event, every `queryapigate events` process sharing the store hears it, from any instance,
woken by NOTIFY on PostgreSQL. The admin's /events stream shows it too; no other key's does (it names no key).

**Re-runs.** A signal marks every subscription on that connection whose tables it names (or all of them, when either
side's tables are unknown). Subscriptions by the same caller to the same query with the same parameters are one group
and share one run. A group re-runs at most once per QUERYAPIGATE_LIVE_MIN_INTERVAL seconds, however many signals
arrive, and its result is pushed only when it differs from the last one sent. A subscriber that falls behind gets the
newest result, not every one: a live query is a state, not a log, so there is no Last-Event-ID - a reconnect simply
gets the current result first.

Opening a stream and each group's re-run are calls against the caller's own rate limit, counted where GET /q/ counts
its calls; a re-run it refuses - or `max_concurrent` does - is postponed, not dropped (ADR 0033 §12). A signal also
makes the response cache's entries for the tables it names a miss (invalidate(), ADR 0033 §10), and a live re-run
skips the cache's lookup.
"""
import asyncio
import base64
import functools
import hashlib
import hmac
import json
import logging
import threading
import time
import urllib.parse
from datetime import datetime

from . import cdcsources, config, db, engine, events, history, runstore, scope, sqltools, store, tableguard
from .errors import ApiError

log = logging.getLogger('queryapigate')

SIGNAL = 'table.changed'
TIME_FORMAT = '%Y-%m-%d %H:%M:%S'
SIGNATURE_TOLERANCE = 300       # seconds a Standard Webhooks timestamp may be off, either way: bounds replays
MAX_BODY = 8 * 1024 * 1024      # bytes of one ingest request
RESERVED = ('format', 'page', 'page_size', 'stream', 'version', 'connection_name')  # not a live query's parameters
_GROUP_QUEUE = 4                # results waiting for one slow subscriber; the oldest is dropped for the newest
_TICK = 0.1                     # seconds between checks for groups due a re-run
_WORKERS = 8                    # re-runs at once, per process


def enabled():
    return config.live_queries()


# ---- signals ----

def signal_columns(connection_name, tables, source, workspace=None):
    """A `table.changed` event's row for events.INSERT: no key (only the admin's stream shows it), no status."""
    occurred_at = datetime.now().strftime(TIME_FORMAT)
    payload = {'type': SIGNAL, 'source': source, 'occurred_at': occurred_at, 'connection_name': connection_name,
               'tables': sorted(tables) if tables is not None else None}
    return (SIGNAL, source, occurred_at, None, connection_name, None, None, json.dumps(payload),
            workspace or scope.current())


# ---- the response cache (cache.py, rediscache.py): a signal makes what it concerns a miss ----

def cache_fields(workspace, connection_name, tables):
    """The generation counters a cached response depends on: its tables' and its connection's "every table" one -
    or, when its tables can't be told (None), its connection's "any change" one. \x1f can't be in a name."""
    base = f'{workspace}\x1f{connection_name}\x1f'
    if tables is None:
        return (base + '*any',)
    return (base + '*all', *(base + table for table in sorted(tables)))


def response_fields(workspace, connection_name, sql, details):
    """cache_fields() for a saved query's SQL on its connection - what every cache lookup of it asks, so it's
    memoized: one dict lookup a hit, instead of finding the tables and building the names again."""
    return _response_fields(workspace, connection_name, sql, details.get('db'), *engine._table_defaults(details))


@functools.lru_cache(maxsize=4096)  # bounded: one entry per (workspace, connection, SQL) - saved queries' SQL
def _response_fields(workspace, connection_name, sql, dialect, default_catalog, default_schema):
    tables = tables_read(sql, {'db': dialect}, (default_catalog, default_schema))
    return cache_fields(workspace, connection_name, tables)


def signal_fields(workspace, connection_name, tables):
    """The counters a signal moves: each table's and "any change" - or, naming no tables, "every table" too."""
    base = f'{workspace}\x1f{connection_name}\x1f'
    if tables is None:
        return (base + '*any', base + '*all')
    return (base + '*any', *(base + table for table in sorted(tables)))


def invalidate(workspace, connection_name, tables):
    """A signal's effect on this process's response cache (or the shared one, with Redis): every cached response it
    concerns becomes a miss. Outside a Flask app (no cache to reach) it does nothing. Never raises."""
    from flask import current_app, has_app_context
    if not has_app_context():
        return
    try:
        current_app.extensions['queryapigate_cache'].bump(signal_fields(workspace, connection_name, tables))
    except Exception:  # the signal is stored already; a cache entry then lives to its TTL
        log.warning('Live queries: could not clear cached responses on %s', connection_name, exc_info=True)


def note_write(sql, details, connection_name):
    """After a successful statement on a writable connection: if it wrote, queue a signal naming the tables it
    touched (all of them - INSERT ... SELECT's sources too, which only costs a needless re-run). Never raises."""
    if not enabled():
        return
    try:
        dialect = details['db']
        if sqltools.is_read_only(sql, dialect):
            return
        tables = tables_read(sql, details)
        history.record_event(signal_columns(connection_name, tables, 'write'), scope.current())
        invalidate(scope.current(), connection_name, tables)
    except Exception:  # a signal must never fail the write it reports
        log.warning('Live queries: could not signal a write on %s', connection_name, exc_info=True)


def tables_read(sql, details, defaults=None):
    """The qualified tables ``sql`` touches, as tableguard names them - None when that can't be told (an unsupported
    dialect, a parse failure, a `{name}` text placeholder that might be a table). ``defaults``: the connection's
    (catalog, schema), when already known."""
    if '{' in sql:
        return None
    try:
        return set(tableguard.extract_tables(sql, details['db'], *(defaults or engine._table_defaults(details))))
    except ApiError:
        return None


def write_signal(columns):
    """Store one signal now, in its own transaction - for ingest, which may only answer once it's durable. Raises
    ApiError (503) when it can't be stored, so Debezium retries."""
    workspace = columns[-1]
    if runstore.enabled():
        row = (None, None, columns[2], '{}', None, None, False, columns, workspace)
        if runstore.write([row], db.current_target()) is None:
            raise ApiError('The change could not be stored - try again', 503, code='store_unavailable')
        return
    try:
        with db.transaction(append_only=True) as conn:
            conn.execute(events.INSERT, columns)
            if db.is_postgres():
                conn.execute(f'NOTIFY {history.NOTIFY_CHANNEL}')
    except (OSError, *db.Error) as error:
        raise ApiError('The change could not be stored - try again', 503, code='store_unavailable') from error


# ---- Debezium ingest ----

def verify_signature(secret, headers, body, now=None):
    """Whether ``body`` is signed with ``secret`` by Standard Webhooks' rules - what Debezium Server's HTTP sink sends
    with `debezium.sink.http.authentication.type=standard-webhooks`: `webhook-signature` holds one or more
    `v1,<base64 HMAC-SHA256>` of `<webhook-id>.<webhook-timestamp>.<body>`, keyed by the secret's base64 bytes (a
    `whsec_` prefix dropped). A timestamp further than SIGNATURE_TOLERANCE from now is refused."""
    msg_id, stamp, signatures = (headers.get('webhook-id'), headers.get('webhook-timestamp'),
                                 headers.get('webhook-signature'))
    if not (msg_id and stamp and signatures and secret):
        return False
    try:
        if abs((now or time.time()) - int(stamp)) > SIGNATURE_TOLERANCE:
            return False
        key = base64.b64decode(secret.removeprefix('whsec_'))
    except ValueError:
        return False
    expected = base64.b64encode(hmac.new(key, f'{msg_id}.{stamp}.'.encode() + body, hashlib.sha256).digest())
    for signature in signatures.split():
        version, _, value = signature.partition(',')
        if version == 'v1' and hmac.compare_digest(value.encode(), expected):
            return True
    return False


def debezium_tables(body, server_name=None):
    """The qualified tables (``schema.table`` - PostgreSQL's schema, else MySQL's database; MongoDB's
    ``database.collection``) that the changes in ``body`` touched: one Debezium change event, or a batch of them
    (`batch.enabled=true`), each with or without
    the `{"schema", "payload"}` wrapper. Tombstones, heartbeats and schema changes touch none; a snapshot's rows
    (`op: r`) are rows that didn't change, and touch none either. Raises ValueError for a body that isn't one, or
    for a change from a server other than ``server_name`` when that's set."""
    try:
        parsed = json.loads(body) if body.strip() else None
    except ValueError as error:
        raise ValueError('the body is not JSON') from error
    except RecursionError as error:  # nested deeper than the parser goes: a 500 would have Debezium retry it forever
        raise ValueError('the body is nested too deeply') from error
    tables = set()
    for item in parsed if isinstance(parsed, list) else [parsed]:
        if isinstance(item, dict) and 'payload' in item and 'schema' in item:
            item = item['payload']
        if not isinstance(item, dict) or not isinstance(item.get('source'), dict) or item.get('op') in (None, 'r'):
            continue
        source = item['source']
        if server_name and source.get('name') != server_name:
            raise ValueError(f"a change from server '{source.get('name')}', not '{server_name}'")
        # a relational source names a table; MongoDB's names a collection in a database (no schema)
        namespace, table = source.get('schema') or source.get('db'), source.get('table') or source.get('collection')
        if not (isinstance(namespace, str) and isinstance(table, str) and table):
            continue
        tables.add(f'{namespace}.{table}'.lower())
    return tables


def ingest(source_name, headers, body, workspace=None):
    """POST /ingest/cdc/<source>: verify, reduce to tables, store one signal. Returns how many tables it named.
    `workspace` is the one the path named (/w/<workspace>/ingest/...), None when it named none (cdcsources.find())."""
    if not enabled():
        raise ApiError('Not Found', 404)
    if len(body) > MAX_BODY:  # before the signature is computed over it
        raise ApiError('The request is too large', 413, code='payload_too_large')
    source = cdcsources.find(source_name, workspace)
    # An unknown workspace, an unknown source and a bad signature all answer 404, so names can't be probed. During a
    # rotation's grace period (ADR 0009) the secret before it verifies too.
    if source is None or not any(verify_signature(secret, headers, body) for secret in source['secrets']):
        raise ApiError(f"No CDC source named '{source_name}'", 404, code='cdc_source_not_found')
    cdcsources.mark_received(source, source_name)
    try:
        tables = debezium_tables(body.decode('utf-8'), source['server_name'])
    except (ValueError, UnicodeDecodeError) as error:
        raise ApiError(f'Not a Debezium change event: {error}', 422, code='invalid_change') from error
    if tables:
        write_signal(signal_columns(source['connection'], tables, f'cdc:{source_name}', source['workspace']))
        invalidate(source['workspace'], source['connection'], tables)
    return len(tables)


# ---- running a live query (blocking: called in a worker thread) ----

def _context(flask_app, permission, name, params):
    from flask import g

    from . import app as app_module
    query_string = urllib.parse.urlencode({**params, 'format': 'json'})
    ctx = flask_app.test_request_context(f'/q/{name}', method='GET', query_string=query_string)
    ctx.push()
    g.request_id = app_module.new_request_id(None)
    g.permission = permission
    g.workspace = permission.workspace
    g.transport = 'live'  # recorded on the run's history entry; run_saved() skips the cache and refuses writes
    return ctx


def plan(flask_app, permission, name):
    """(connection, tables) a subscription to ``name`` listens on: its published version's connection, and the tables
    its SQL reads - for a MongoDB query, its collection as ``database.collection``, as Debezium names it (None: any
    change on the connection - SQL whose tables can't be told)."""
    ctx = _context(flask_app, permission, name, {})
    try:
        path = store.resolve_saved_file(name, check_exists=False)
        _, saved = store.select_version(store.load_versions(path, with_history=False), None)
        connection_name = saved.get('connection_name')
        if saved.get('query_type') == 'mongo':
            database = store.get_connection(connection_name).get('database')
            collection = saved.get('mongo_collection')
            if not (isinstance(database, str) and database and isinstance(collection, str) and collection):
                return connection_name, None
            return connection_name, {f'{database}.{collection}'.lower()}
        if not isinstance(saved.get('sql_query'), str):
            return connection_name, None
        return connection_name, tables_read(saved['sql_query'], store.get_connection(connection_name))
    finally:
        ctx.pop()


def run_once(flask_app, permission, name, params):
    """One run of saved query ``name`` as ``permission``, exactly as GET /q/<name> runs it, with its quota checked
    and counted: {"rows": [...], "truncated": bool}. Raises ApiError as that route would answer."""
    from . import app as app_module
    from . import quotas
    ctx = _context(flask_app, permission, name, params)
    try:
        quotas.check(permission)
        response = app_module.run_saved(name, {}, dict(params))
        rows = response.get_json() if response.headers.get('X-Row-Count') != '0' else []
        quotas.count(permission, len(rows))
        return {'rows': rows, 'truncated': response.headers.get('X-Has-More') == 'true'}
    finally:
        ctx.pop()


def frame(event, data):
    return f'event: {event}\ndata: {json.dumps(data, default=str)}\n\n'.encode()


# ---- subscriptions, in `queryapigate events` ----

class _Group:
    """Every subscription by one caller to one query with the same parameters: one run serves them all."""
    __slots__ = ('key', 'permission', 'name', 'params', 'workspace', 'connection', 'tables', 'subscribers',
                 'digest', 'body', 'last_run', 'dirty', 'running', 'not_before')

    def __init__(self, key, permission, name, params, connection, tables):
        self.key, self.permission, self.name, self.params = key, permission, name, params
        self.workspace, self.connection, self.tables = permission.workspace, connection, tables
        self.subscribers = set()   # asyncio.Queue of frames; None ends a stream
        self.digest = None         # of the last result sent
        self.body = None           # the last result frame, for a subscriber joining the group
        self.last_run = 0.0
        self.dirty = False
        self.running = False
        self.not_before = 0.0     # a postponed re-run waits until then (monotonic): the caller's limits allow it


# Per-token claims that say when and which token, not who: two tokens differing only in these may share a group
_TOKEN_CLAIMS = frozenset({'exp', 'iat', 'nbf', 'jti', 'auth_time'})


def group_key(permission, name, params):
    """Which subscriptions share one re-run - and so one result: the same caller, query and parameters. A signed-in
    user's claims are part of it, since `from_claim` parameters (app.bind_claims) come from them: two tokens for one
    user with another tenant, say, must never see each other's rows."""
    claims = permission.claims
    if claims:
        claims = hashlib.sha256(json.dumps({k: v for k, v in claims.items() if k not in _TOKEN_CLAIMS},
                                           sort_keys=True, default=str).encode()).hexdigest()
    return (permission.workspace, permission.name, permission.via, claims or None, name,
            tuple(sorted(params.items())))


class LiveQueries:
    def __init__(self, flask_app=None):
        self._flask_app = flask_app
        self._flask_app_lock = threading.Lock()
        self.groups = {}           # group key -> _Group
        self.by_connection = {}    # (workspace, connection) -> set of group keys
        self.wake = None
        self.semaphore = None
        self.task = None
        self.reruns = self.pushed = self.unchanged = self.signals = self.postponed = 0

    def flask_app(self):
        if self._flask_app is None:  # built on the first subscription: an events server without one never needs it
            # under a lock: two first subscriptions at once (worker threads) would each build an app otherwise
            with self._flask_app_lock:
                if self._flask_app is None:
                    from .app import create_app
                    self._flask_app = create_app()
        return self._flask_app

    def start(self):
        self.wake = asyncio.Event()
        self.semaphore = asyncio.Semaphore(_WORKERS)
        self.task = asyncio.create_task(self._loop())

    def stop(self):
        if self.task is not None:
            self.task.cancel()
        for group in self.groups.values():
            for queue in group.subscribers:
                _offer(queue, None)

    def _take(self, permission):
        """One call from the caller's own `rate_limit` (a key's, or a signed-in user's role's - ADR 0033 §12), counted
        where GET /q/ counts its calls: None when it may run, else how many seconds until it may."""
        from . import governance
        verdict = governance.check_key_limit(self.flask_app(), permission)
        return None if verdict is None or verdict.allowed else max(1, verdict.retry_after)

    def _postpone(self, group, seconds):
        """Hold a re-run back - not drop it: the group stays marked, and runs once ``seconds`` have passed."""
        group.dirty = True
        group.not_before = time.monotonic() + seconds
        self.postponed += 1

    async def subscribe(self, permission, name, params):
        """A new subscriber's queue, with the current result already in it. Raises ApiError - as GET /q/<name> would
        answer - before anything is streamed, so a refusal is an ordinary HTTP error. Opening a stream is a call
        against the caller's rate limit, whether or not it runs anything new."""
        flask_app = await asyncio.to_thread(self.flask_app)
        wait = await asyncio.to_thread(self._take, permission)
        if wait is not None:
            raise ApiError('Rate limit exceeded for this API key', 429, code='rate_limited', retry_after=wait)
        key = group_key(permission, name, params)
        group = self.groups.get(key)
        if group is None:
            connection, tables = await asyncio.to_thread(plan, flask_app, permission, name)
            group = _Group(key, permission, name, params, connection, tables)
            await self._run(group, 'subscribe', raise_errors=True)
            if key in self.groups:  # another subscriber opened it meanwhile: join that one
                group = self.groups[key]
            else:
                self.groups[key] = group
                self.by_connection.setdefault((group.workspace, connection), set()).add(key)
        queue = asyncio.Queue(maxsize=_GROUP_QUEUE)
        queue.put_nowait(group.body)
        group.subscribers.add(queue)
        return group, queue

    def unsubscribe(self, group, queue):
        group.subscribers.discard(queue)
        if not group.subscribers and self.groups.get(group.key) is group:
            del self.groups[group.key]
            keys = self.by_connection.get((group.workspace, group.connection), set())
            keys.discard(group.key)
            if not keys:
                self.by_connection.pop((group.workspace, group.connection), None)

    def on_signal(self, workspace, payload_json):
        """A `table.changed` event from the log: mark every group it may concern."""
        try:
            payload = json.loads(payload_json)
        except (TypeError, ValueError):
            return
        self.signals += 1
        tables = payload.get('tables')
        changed = set(tables) if isinstance(tables, list) else None
        for key in self.by_connection.get((workspace, payload.get('connection_name')), ()):
            group = self.groups[key]
            if changed is None or group.tables is None or group.tables & changed:
                group.dirty = True
        if self.wake is not None:
            self.wake.set()

    async def _loop(self):
        while True:
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=_TICK)
            except asyncio.TimeoutError:
                pass
            self.wake.clear()
            now, interval = time.monotonic(), config.live_min_interval()
            for group in list(self.groups.values()):
                if group.dirty and not group.running and now - group.last_run >= interval and now >= group.not_before:
                    group.dirty = False
                    group.running = True
                    asyncio.create_task(self._refresh(group))

    async def _refresh(self, group):
        try:
            wait = await asyncio.to_thread(self._take, group.permission)  # one call a group, however many subscribe
            if wait is not None:
                self._postpone(group, wait)
                return
            async with self.semaphore:
                await self._run(group, 'change')
        finally:
            group.running = False

    async def _run(self, group, reason, raise_errors=False):
        group.last_run = time.monotonic()
        self.reruns += reason == 'change'
        try:
            result = await asyncio.to_thread(run_once, self.flask_app(), group.permission, group.name, group.params)
        except ApiError as error:
            if raise_errors:
                raise
            if error.code == 'too_many_concurrent':  # the caller's other queries hold every slot: try again shortly
                self._postpone(group, max(1.0, config.live_min_interval()))
                return
            from .errors import code_for
            body = frame('error', {'error': error.message, 'code': code_for(error.status, error.code)})
            ending = error.status in (401, 403, 404)  # no longer allowed, or gone: end the streams, they won't recover
            for queue in list(group.subscribers):
                _offer(queue, body)
                if ending:
                    _offer(queue, None)
            return
        except Exception:
            log.warning('Live queries: re-running %s failed', group.name, exc_info=True)
            return
        digest = hashlib.sha256(json.dumps(result, sort_keys=True, default=str).encode()).hexdigest()
        if digest == group.digest:
            self.unchanged += 1
            return
        group.digest = digest
        group.body = frame('result', {'query': group.name, 'params': group.params, **result, 'reason': reason,
                                      'refreshed_at': datetime.now().strftime(TIME_FORMAT)})
        if reason == 'change':
            self.pushed += 1
            for queue in list(group.subscribers):
                _offer(queue, group.body)

    def metrics_lines(self):
        lines = ['# HELP queryapigate_live_subscriptions Live query subscriptions open right now.',
                 '# TYPE queryapigate_live_subscriptions gauge',
                 f'queryapigate_live_subscriptions {sum(len(g.subscribers) for g in self.groups.values())}',
                 '# HELP queryapigate_live_groups Distinct live queries (caller, query, parameters) being kept fresh.',
                 '# TYPE queryapigate_live_groups gauge', f'queryapigate_live_groups {len(self.groups)}']
        for name, help_text, value in (
                ('signals', 'Table-change signals heard.', self.signals),
                ('reruns', 'Live queries re-run after a change.', self.reruns),
                ('pushed', 'Re-runs whose result changed, and was pushed.', self.pushed),
                ('unchanged', 'Re-runs whose result was the same, so nothing was sent.', self.unchanged),
                ('postponed', "Re-runs held back by the caller's rate limit or max_concurrent, to run once it allows.",
                 self.postponed)):
            lines += [f'# HELP queryapigate_live_{name}_total {help_text}',
                      f'# TYPE queryapigate_live_{name}_total counter', f'queryapigate_live_{name}_total {value}']
        return lines


def _offer(queue, item):
    """Put ``item`` on a subscriber's queue; when it's full, the oldest result makes room - a live query's subscriber
    wants the newest state, not every state in between. A None (end the stream) always gets in."""
    while True:
        try:
            queue.put_nowait(item)
            return
        except asyncio.QueueFull:
            queue.get_nowait()

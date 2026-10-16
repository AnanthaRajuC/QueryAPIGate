"""AI agents' writes over MCP, each approved by a person before it runs (ADR 0008, BACKLOG #88).

A saved query its author marked `agent_writes` is a write tool for callers whose grant allows the write
(mcp_server.agent_may_write()). What happens when an agent calls one:

- **"approval"** (the default): the first call writes nothing. It records a *write request* - the caller, the query
  and version, the exact arguments - and answers `approval_required` with where a person approves it. An administrator
  whose role has `writes.approve` approves or rejects it (/api/v1/write-requests, the Console's Approvals). The agent
  then calls again with the same arguments and `approval` set to the request: it runs if the request was approved, for
  this caller, with these arguments (compared by hash - approving one write never lets another run), before it expires.
  It runs once: another call with the same approval gets the first one's result.
- **"insert"**: a single INSERT runs at once - it can add rows, never change or remove one - with an optional
  `idempotency_key`, so a retried call within a day returns the first one's result instead of inserting again.

Either way it runs through engine.execute_agent_write(): in a transaction, rolled back when it changed more than the
query's `max_affected_rows`. Requests are kept 30 days.
"""
import hashlib
import json
import secrets
import time
from datetime import datetime

from . import config, db, scope, store
from .errors import ApiError

KEEP_DAYS = 30
IDEMPOTENCY_SECONDS = 24 * 3600
MAX_PENDING_PER_CALLER = 100  # an agent asking for writes faster than people decide them is refused, not stored
STATUSES = ('pending', 'approved', 'running', 'done', 'failed', 'rejected', 'expired')
_COLUMNS = 'id, query_name, version, caller, status, created_at, expires_at, idempotency_hash, details_json'


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def arguments_hash(name, version, values):
    """What an approval is for: the query, its version and the exact arguments it will run with."""
    return _hash(json.dumps({'query': name, 'version': version, 'values': values}, sort_keys=True, default=str))


def _row(row):
    details = json.loads(row['details_json'])
    status = row['status']
    if status in ('pending', 'approved') and row['expires_at'] < time.time():
        status = 'expired'
    return {'id': row['id'], 'query': row['query_name'], 'version': row['version'], 'caller': row['caller'],
            'status': status, 'created_at': row['created_at'],
            'expires_at': datetime.fromtimestamp(row['expires_at']).strftime('%Y-%m-%d %H:%M:%S'),
            'mode': details.get('mode'), 'connection': details.get('connection_name'), 'sql': details.get('sql'),
            'arguments': details.get('arguments') or {}, 'max_affected_rows': details.get('max_affected_rows'),
            'decided_by': details.get('decided_by'), 'decided_via': details.get('decided_via'),
            'decided_at': details.get('decided_at'), 'reason': details.get('reason'),
            'result': details.get('result'), '_arguments_hash': details.get('arguments_hash')}


def public(request):
    return {k: v for k, v in request.items() if not k.startswith('_')}


def get(request_id):
    # in its own workspace only: another workspace's approvers don't see it (ADR 0023)
    row = db.connection().execute(f'SELECT {_COLUMNS} FROM write_requests WHERE workspace = ? AND id = ?',
                                  (scope.current(), request_id)).fetchone() if isinstance(request_id, str) else None
    if row is None:
        raise ApiError(f"No write request '{request_id}'", 404, code='write_request_not_found')
    return _row(row)


def list_requests(status=None, limit=200):
    if status not in (None, '') and status not in STATUSES:
        raise ApiError(f"status must be one of: {', '.join(STATUSES)}", code='invalid_filter')
    rows = db.connection().execute(f'SELECT {_COLUMNS} FROM write_requests WHERE workspace = ? '
                                   'ORDER BY created_at DESC, id DESC LIMIT ?',
                                   (scope.current(), max(1, min(int(limit), 1000)))).fetchall()
    found = [_row(r) for r in rows]
    return [r for r in found if not status or r['status'] == status]


def approval_url(request_id):
    base = config.console_url()
    return f'{base}/console/approvals/{request_id}' if base else f'/console/approvals/{request_id}'


def _sweep(conn):
    conn.execute("DELETE FROM write_requests WHERE created_at < ?",
                 (datetime.fromtimestamp(time.time() - KEEP_DAYS * 86400).strftime('%Y-%m-%d %H:%M:%S'),))


def _write_details(conn, request_id, **changes):
    row = conn.execute('SELECT details_json FROM write_requests WHERE id = ?', (request_id,)).fetchone()
    details = {**json.loads(row['details_json']), **changes}
    conn.execute('UPDATE write_requests SET details_json = ? WHERE id = ?', (json.dumps(details, default=str),
                                                                            request_id))


def create(caller, name, version, saved, connection_name, values, status='pending', idempotency=None):
    """A new write request; pending ones expire after QUERYAPIGATE_WRITE_APPROVAL_MINUTES."""
    if status == 'pending':
        waiting = db.connection().execute("SELECT COUNT(*) FROM write_requests WHERE workspace = ? AND caller = ? "
                                          "AND status = 'pending' AND expires_at >= ?",
                                          (scope.current(), caller, time.time())).fetchone()[0]
        if waiting >= MAX_PENDING_PER_CALLER:
            raise ApiError(f'{waiting} writes asked for by this caller are already waiting for approval - wait for '
                           'them to be decided or to expire', 429, code='too_many_pending_writes')
    request_id = 'wr_' + secrets.token_hex(6)
    details = {'mode': saved.get('agent_writes'), 'connection_name': connection_name, 'sql': saved['sql_query'],
               'arguments': values, 'arguments_hash': arguments_hash(name, version, values),
               'max_affected_rows': saved.get('max_affected_rows') or 1}
    with db.transaction() as conn:
        _sweep(conn)
        conn.execute(f'INSERT INTO write_requests (workspace, {_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                     (scope.current(), request_id, name, version, caller, status, store.now(),
                      time.time() + config.write_approval_minutes() * 60, idempotency,
                      json.dumps(details, default=str)))
    return get(request_id)


def decide(request_id, approve, actor, via, reason=None):
    """Approve or reject a pending request. An approval gives the agent the approval window again to run it."""
    if reason is not None and (not isinstance(reason, str) or len(reason) > 500):
        raise ApiError('reason is text of at most 500 characters', code='invalid_body')
    with db.transaction() as conn:
        request = get(request_id)
        if actor == request['caller'] and via != 'open':
            # Four eyes: the credential an agent asked with can't approve its own write - an MCP client configured with
            # an approver's token, or the shared key, would otherwise approve whatever its model asks for. (A server
            # with no keys at all has no identities to tell apart - everyone is "-", and may do anything anyway.)
            raise ApiError('This write was asked for under your own name - another approver must decide it', 403,
                           code='own_write')
        if request['status'] != 'pending':
            raise ApiError(f"This write request is {request['status']} - only a pending one can be decided", 409,
                           code='write_request_closed')
        status = 'approved' if approve else 'rejected'
        changed = conn.execute(
            'UPDATE write_requests SET status = ?, expires_at = ? WHERE id = ? AND status = ?',
            (status, time.time() + config.write_approval_minutes() * 60, request_id, 'pending')).rowcount
        if changed != 1:  # decided by someone else a moment ago
            raise ApiError('This write request was decided a moment ago', 409, code='write_request_closed')
        _write_details(conn, request_id, decided_by=actor, decided_via=via, decided_at=store.now(),
                       reason=(reason or '').strip() or None)
    # The arguments' names and hash, never their values: auditors read the audit log, and they have no data access
    # (the same reason run history keeps a run's parameter names only).
    store.record_audit(actor, 'approve_write' if approve else 'reject_write', request_id,
                       {'query': request['query'], 'caller': request['caller'],
                        'arguments': sorted(request['arguments']), 'arguments_hash': request['_arguments_hash'],
                        **({'reason': reason} if reason else {})}, via=via)
    return get(request_id)


def claim(request_id, caller, name, version, values):
    """The approved request a second call names, claimed to run now - or the reason it can't run, as an ApiError.
    A request already run returns its stored result instead (`('done', request)`), so a retry never writes twice."""
    request = get(request_id) if isinstance(request_id, str) and request_id else None
    if request is None or request['caller'] != caller or request['query'] != name:
        raise ApiError("No write request by that id for this caller and tool - call without `approval` to ask for "
                       'one', 404, code='write_request_not_found')
    if request['_arguments_hash'] != arguments_hash(name, version, values) or request['version'] != version:
        raise ApiError('These arguments are not the ones approved - an approval is for exactly one write. Call '
                       'without `approval` to ask for this one', 403, code='approval_mismatch')
    if request['status'] in ('done', 'failed'):
        return 'finished', request
    if request['status'] == 'pending':
        raise ApiError('Not approved yet - a person must approve it first: ' + approval_url(request_id), 409,
                       code='approval_pending')
    if request['status'] == 'rejected':
        raise ApiError(f"Rejected by {request['decided_by']}" + (f": {request['reason']}" if request['reason'] else ''),
                       403, code='approval_rejected')
    if request['status'] in ('expired',):
        raise ApiError('This approval has expired - call without `approval` to ask again', 409,
                       code='approval_expired')
    if request['status'] == 'running':
        raise ApiError('This write is running now', 409, code='write_running')
    with db.transaction() as conn:
        if conn.execute("UPDATE write_requests SET status = 'running' WHERE id = ? AND status = 'approved'",
                        (request_id,)).rowcount != 1:
            raise ApiError('This write is running now', 409, code='write_running')
    return 'claimed', request


def idempotent(caller, name, key):
    """The hash an insert's idempotency key is kept under, and the request a previous call with it made (if any, in
    the last day)."""
    if not isinstance(key, str) or not 1 <= len(key) <= 200:
        raise ApiError('idempotency_key is text of 1 to 200 characters', code='param_invalid')
    hashed = _hash(json.dumps([caller, name, key]))
    since = datetime.fromtimestamp(time.time() - IDEMPOTENCY_SECONDS).strftime('%Y-%m-%d %H:%M:%S')
    row = db.connection().execute(f'SELECT {_COLUMNS} FROM write_requests WHERE workspace = ? AND idempotency_hash = ? '
                                  'AND created_at >= ? ORDER BY created_at DESC LIMIT 1',
                                  (scope.current(), hashed, since)).fetchone()
    return hashed, (_row(row) if row is not None else None)


def finish(request_id, result=None, error=None):
    with db.transaction() as conn:
        conn.execute('UPDATE write_requests SET status = ? WHERE id = ?', ('done' if error is None else 'failed',
                                                                          request_id))
        _write_details(conn, request_id, finished_at=store.now(), result=result if error is None else error)
    return get(request_id)


def pending_count():
    rows = db.connection().execute("SELECT created_at, expires_at FROM write_requests WHERE status = 'pending'"
                                   ).fetchall()
    live = [r for r in rows if r['expires_at'] >= time.time()]
    return len(live), min((r['created_at'] for r in live), default=None)

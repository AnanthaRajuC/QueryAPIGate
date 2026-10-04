"""Which processes are using this store right now (BACKLOG #58): `queryapigate serve` workers, `mcp`, `events`.

Each process records itself when it starts and refreshes its row as it serves requests - at most every
HEARTBEAT seconds, so it costs one small write per half minute, and the health checks a deployment already runs keep an
otherwise idle instance visible. There is no background thread: one would write to whichever store the process is
pointed at when it wakes, and a process only ever needs to be seen while it is answering.

What this is for: telling an operator about a deployment that looks wrong - several instances sharing a store without
sharing rate limits and the response cache (no Redis), or instances on different versions (a rolling upgrade, or one
that stopped halfway). See alerts.py and GET /api/v1/instances.
"""
import os
import socket
import threading
import time
import uuid

from . import config, db

HEARTBEAT = 30.0  # seconds between refreshes of this process's row
ALIVE = 90.0      # a row refreshed this recently is a running process
FORGET = 86400.0  # rows not refreshed for a day are removed

_id = None
_role = None
_last_beat = 0.0
_lock = threading.Lock()


def instance_id():
    global _id
    with _lock:
        if _id is None or os.getpid() != int(_id.split('-')[1]):  # a forked worker is an instance of its own
            _id = f'{uuid.uuid4().hex[:12]}-{os.getpid()}'
        return _id


def register(role):
    """Record this process (role: serve, mcp or events) in the store. Called at startup."""
    global _role, _last_beat
    _role = role
    _write(time.time(), first=True)
    _last_beat = time.monotonic()


def heartbeat():
    """Refresh this process's row if HEARTBEAT has passed - cheap enough to call on every request."""
    global _last_beat
    if _role is None or time.monotonic() - _last_beat < HEARTBEAT:
        return
    _last_beat = time.monotonic()
    try:
        _write(time.time(), first=False)
    except Exception:  # visibility, never a reason for a request to fail
        pass


def _write(now, first):
    from . import __version__
    row = (instance_id(), socket.gethostname(), os.getpid(), _role or 'serve', __version__,
           1 if config.redis_url() else 0, now, now)
    with db.transaction() as conn:
        if first:
            conn.execute('DELETE FROM instances WHERE last_seen < ?', (now - FORGET,))
            conn.execute('DELETE FROM instances WHERE id = ?', (row[0],))
            conn.execute('INSERT INTO instances (id, host, pid, role, version, shared_limits, started_at, last_seen) '
                         'VALUES (?, ?, ?, ?, ?, ?, ?, ?)', row)
        elif conn.execute('UPDATE instances SET last_seen = ? WHERE id = ?', (now, row[0])).rowcount == 0:
            conn.execute('INSERT INTO instances (id, host, pid, role, version, shared_limits, started_at, last_seen) '
                         'VALUES (?, ?, ?, ?, ?, ?, ?, ?)', row)


def alive(now=None):
    """Every process seen in the last ALIVE seconds, newest start first."""
    now = time.time() if now is None else now
    rows = db.connection().execute(
        'SELECT id, host, pid, role, version, shared_limits, started_at, last_seen FROM instances '
        'WHERE last_seen >= ? ORDER BY started_at DESC', (now - ALIVE,)).fetchall()
    return [{'id': r[0], 'host': r[1], 'pid': r[2], 'role': r[3], 'version': r[4], 'shared_limits': bool(r[5]),
             'started_at': _stamp(r[6]), 'last_seen': _stamp(r[7]), 'this': r[0] == _id} for r in rows]


def _stamp(seconds):
    return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(seconds))


def problems(now=None):
    """What looks wrong about the processes sharing this store: [(kind, message)] - for alerts and the startup log."""
    running = alive(now)
    found = []
    unshared = [i for i in running if not i['shared_limits']]
    if len(running) > 1 and unshared:
        found.append(('instances_not_shared', f'{len(running)} processes share this store, and {len(unshared)} of them '
                      'without Redis, so their rate limits and response cache are counted and kept per process. '
                      'Set the same QUERYAPIGATE_REDIS_URL on every instance.'))
    versions = sorted({i['version'] for i in running})
    if len(versions) > 1:
        counts = ', '.join(f"{v} on {sum(1 for i in running if i['version'] == v)}" for v in versions)
        found.append(('instances_versions_differ', f'Instances sharing this store run different versions ({counts}). '
                      'Expected only during a rolling upgrade - finish it.'))
    return found


def warn_if_unshared(log):
    try:
        for _kind, message in problems():
            log.warning(message)
    except Exception:  # visibility, never a reason not to start
        pass

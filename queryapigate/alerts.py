"""What needs an admin's attention, right now: GET /api/v1/alerts, the Console's Alerts screen and its header bell.

Every alert is a live condition, worked out afresh on each request from the store and this process's counters -
nothing is stored, so an alert goes away by itself once its cause does (a key extended, a connection fixed, a query
sped up). Each has a stable `id` (kind and subject), so a client can remember which ones it was told about or
dismissed.

    {"id": "key_expiring:partner", "severity": "warning", "kind": "key_expiring",
     "title": "API key 'partner' expires in 3 days", "detail": "...", "since": "2026-10-07",
     "target": {"type": "key", "name": "partner"}}

Severities: `critical` (broken or unsafe now), `warning` (will break, or degrading), `info` (worth a look).

The checks read the newest RECENT_RUNS runs of history, so their cost doesn't grow with history kept. Rate-limit
counts are this process's own, since it started (as /metrics' are) - with several instances, each sees its share.
"""
import json
import threading
import time
from collections import deque
from datetime import datetime, timedelta
from statistics import median

from . import admins, apikeys, config, db, history, instances, metrics, ratelimit

RECENT_RUNS = 5000  # newest runs the history checks look at
QUERY_WINDOW = timedelta(days=7)  # a query's error rate and speed are judged on its runs this recent ...
QUERY_RUNS = 50  # ... at most this many of them, newest first
MIN_RUNS_FOR_RATE = 10  # fewer runs than this say too little about an error rate
MIN_RUNS_FOR_SPEED = 5
CONNECTION_STREAK = 3  # a connection's newest runs all failing for a connection reason, this many in a row
CONNECTION_WINDOW = timedelta(hours=24)
EXPIRY_WARNING = timedelta(days=7)
BREAK_GLASS_WINDOW = timedelta(hours=24)  # a use of the shared key this recent, once named owners exist
RATE_LIMITED_PER_HOUR = 10  # refusals in the last hour before a key or client is called out

# Error codes (errors.py) that mean the connection itself is the problem, not the SQL run on it
CONNECTION_CODES = {'connection_failed', 'driver_missing', 'connection_misconfigured', 'database_file_not_found',
                    'secret_key_required', 'password_undecryptable', 'connection_inactive'}

SEVERITY_ORDER = {'critical': 0, 'warning': 1, 'info': 2}

_lock = threading.Lock()
_refusals: dict[tuple, deque] = {}  # ('key' | 'client', who) -> monotonic times of rate-limit refusals


def note_rate_limited(kind, who):
    """One rate-limit refusal, for the 'keeps hitting its limit' alert: kind 'key' (a key's own rate_limit) or
    'client' (QUERYAPIGATE_RATE_LIMIT, by address - checked before the caller is known)."""
    now = time.monotonic()
    with _lock:
        times = _refusals.setdefault((kind, who or 'unknown'), deque(maxlen=1000))
        times.append(now)


# Every kind of alert, the one list: the OpenAPI enum is built from it and tests/test_alerts.py checks API.md's table.
KINDS = ('open_server', 'break_glass_used', 'connection_failing', 'key_expired', 'key_expiring', 'query_errors',
         'query_timeouts', 'query_slow', 'key_rate_limited', 'client_rate_limited', 'history_failed', 'history_dropped',
         'instances_not_shared', 'instances_versions_differ', 'rate_limits_not_shared', 'export_failing', 'key_unused')


def _alert(severity, kind, subject, title, detail, target=None, since=None):
    if kind not in KINDS:
        raise ValueError(f'{kind} is not in alerts.KINDS')
    return {'id': f'{kind}:{subject}', 'severity': severity, 'kind': kind, 'title': title, 'detail': detail,
            'since': since, 'target': target}


def _plural(n, word):
    return f'{n} {word}' + ('' if n == 1 else 's')


def collect(now=None):
    """Every alert that holds now, most severe first."""
    now = now or datetime.now()
    found = [*_server(now), *_keys(now), *_runs(now), *_rate_limited(), *_history_health()]
    return sorted(found, key=lambda a: (SEVERITY_ORDER[a['severity']], a['kind'], a['id']))


def _server(now):
    if apikeys.auth_required():
        return _break_glass(now)
    return [_alert('critical', 'open_server', 'server', 'Anyone can use this server',
                   'No API key is configured, so every request - including changing connections and keys - is '
                   'allowed. Set QUERYAPIGATE_API_KEY.', {'type': 'settings', 'name': 'security'})]


def _break_glass(now):
    if not admins.active_owner_exists():
        return []
    used = admins.last_break_glass_use((now - BREAK_GLASS_WINDOW).strftime(history.TIME_FORMAT))
    if used is None:
        return []
    return [_alert('warning', 'break_glass_used', 'server', 'The shared admin key was used',
                   f"QUERYAPIGATE_API_KEY - the break-glass key - was used at {used['timestamp']} "
                   f"({used.get('target')}), although named owners exist. If nobody meant to, change it; once "
                   'everyone signs in with their own token, remove it from the environment.',
                   {'type': 'settings', 'name': 'security'}, used['timestamp'])]


def _keys(now):
    found = []
    unused_days = config.alert_key_unused_days()
    for name, key in sorted(apikeys.list_keys().items()):
        if key.get('active') is False:
            continue  # revoked on purpose
        target = {'type': 'key', 'name': name}
        expires = key.get('expires_at')
        if expires:
            try:
                deadline = datetime.strptime(expires, '%Y-%m-%d').replace(hour=23, minute=59, second=59)
            except ValueError:
                deadline = None
            if deadline is not None and now > deadline:
                found.append(_alert('warning', 'key_expired', name, f"API key '{name}' has expired",
                                    f'It expired on {expires}, so every call with it is refused. Extend it if '
                                    'it is still in use, or revoke it.', target, expires))
                continue
            if deadline is not None and deadline - now <= EXPIRY_WARNING:
                days = max(0, (deadline.date() - now.date()).days)
                when = 'today' if days == 0 else 'tomorrow' if days == 1 else f'in {days} days'
                found.append(_alert('warning', 'key_expiring', name, f"API key '{name}' expires {when}",
                                    f'On {expires}. Calls with it will be refused from then on - extend it, or '
                                    'give its callers a new key first.', target, expires))
        if unused_days:
            last = key.get('last_used_at') or key.get('created_at')
            try:
                idle = now - datetime.strptime(last, history.TIME_FORMAT) if last else None
            except ValueError:
                idle = None
            if idle is not None and idle >= timedelta(days=unused_days):
                used = key.get('last_used_at')
                found.append(_alert(
                    'info', 'key_unused', name, f"API key '{name}' hasn't been used in {idle.days} days",
                    (f'Last used {used}.' if used else f'Never used since it was created on {last}.') +
                    ' An unused credential is risk without benefit - revoke it if nothing needs it.', target, last))
    return found


def _recent_runs(now):
    """The newest runs, as (query_name, executed_at, entry) - written ones, including this process's queued."""
    history.flush()
    rows = db.connection().execute(
        'SELECT query_name, executed_at, entry_json FROM execution_history ORDER BY rowid DESC LIMIT ?',
        (RECENT_RUNS,)).fetchall()
    oldest = (now - QUERY_WINDOW).strftime(history.TIME_FORMAT)
    runs = []
    for row in rows:
        if row['executed_at'] < oldest:
            continue
        try:
            entry = json.loads(row['entry_json'])
        except ValueError:
            continue
        runs.append((row['query_name'], row['executed_at'], entry))
    runs.sort(key=lambda r: r[1], reverse=True)
    return runs


def _runs(now):
    runs = _recent_runs(now)
    # An export's run can fail at its destination, which says nothing about the query or its connection.
    served = [run for run in runs if run[2].get('transport') != 'export']
    return [*_failing_connections(served, now), *_queries(served, now), *_exports(runs)]


def _exports(runs):
    """An export whose newest run failed: nothing reaches its destination until it is fixed."""
    found, seen = [], set()
    for _, executed_at, entry in runs:  # newest first
        name = entry.get('export')
        if entry.get('transport') != 'export' or not name or name in seen:
            continue
        seen.add(name)
        if entry.get('status') == 'error':
            found.append(_alert('warning', 'export_failing', name, f"Export '{name}' is failing",
                                f"Its last run, at {executed_at}, failed: {entry.get('error')}. Nothing new reaches "
                                f"{entry.get('destination')} until it succeeds; an incremental export catches up "
                                'then, since its watermark only moves after a file is written.',
                                {'type': 'export', 'name': name}, executed_at))
    return found


def _failing_connections(runs, now):
    found = []
    since = (now - CONNECTION_WINDOW).strftime(history.TIME_FORMAT)
    by_connection = {}
    for _, executed_at, entry in runs:
        if executed_at >= since and entry.get('connection_name'):
            by_connection.setdefault(entry['connection_name'], []).append((executed_at, entry))
    for name, newest_first in sorted(by_connection.items()):
        streak = []
        for executed_at, entry in newest_first:
            if entry.get('status') != 'error' or entry.get('code') not in CONNECTION_CODES:
                break
            streak.append((executed_at, entry))
        if len(streak) >= CONNECTION_STREAK:
            latest = streak[0][1]
            found.append(_alert(
                'critical', 'connection_failing', name, f"Connection '{name}' is failing",
                f"Its last {len(streak)} runs failed ({latest.get('code')}): {latest.get('error') or 'no detail'}. "
                'Check the database is up and the connection settings are right.',
                {'type': 'connection', 'name': name}, streak[-1][0]))
    return found


def _queries(runs, now):
    found = []
    error_rate = config.alert_error_rate()
    slow = config.slow_query_threshold()
    day_ago = (now - timedelta(hours=24)).strftime(history.TIME_FORMAT)
    by_query = {}
    for query, executed_at, entry in runs:
        if query is not None:
            by_query.setdefault(query, []).append((executed_at, entry))
    for name, newest_first in sorted(by_query.items()):
        target = {'type': 'query', 'name': name}
        recent = newest_first[:QUERY_RUNS]
        failed = [e for _, e in recent if e.get('status') == 'error']
        if error_rate and len(recent) >= MIN_RUNS_FOR_RATE and len(failed) * 100 >= error_rate * len(recent):
            share = round(len(failed) * 100 / len(recent))
            found.append(_alert(
                'warning', 'query_errors', name, f"Query '{name}' fails {share}% of its runs",
                f"{len(failed)} of its last {_plural(len(recent), 'run')} failed. The newest: "
                f"{failed[0].get('error') or 'no detail'}", target, recent[-1][0]))
        timeouts = [t for t, e in newest_first if t >= day_ago and e.get('code') == 'query_timeout']
        if timeouts:
            found.append(_alert(
                'warning', 'query_timeouts', name,
                f"Query '{name}' timed out {_plural(len(timeouts), 'time')} in the last 24 hours",
                'Runs past their time limit are cancelled and their callers get a 504. Add an index, narrow the '
                'query, or raise its timeout.', target, timeouts[-1]))
        durations = [e['duration_ms'] for _, e in recent
                     if e.get('status') == 'success' and isinstance(e.get('duration_ms'), (int, float))]
        if slow and len(durations) >= MIN_RUNS_FOR_SPEED and median(durations) >= slow * 1000:
            found.append(_alert(
                'warning', 'query_slow', name, f"Query '{name}' is slow",
                f"Its typical run takes {median(durations) / 1000:.1f} s (median of the last "
                f"{_plural(len(durations), 'successful run')}), over QUERYAPIGATE_SLOW_QUERY_THRESHOLD "
                f'({slow:g} s). Consider an index, a narrower query, or a cache_ttl.', target, recent[-1][0]))
    return found


def _rate_limited():
    found = []
    cutoff = time.monotonic() - 3600
    with _lock:
        counts = {}
        for who, times in _refusals.items():
            while times and times[0] < cutoff:
                times.popleft()
            if times:
                counts[who] = len(times)
    for (kind, who), count in sorted(counts.items()):
        if count < RATE_LIMITED_PER_HOUR:
            continue
        if kind == 'key':
            found.append(_alert(
                'warning', 'key_rate_limited', who, f"API key '{who}' keeps hitting its rate limit",
                f"{_plural(count, 'call')} refused in the last hour by its own rate_limit. Raise the limit if the "
                'traffic is expected, or ask its owner to slow down or cache.', {'type': 'key', 'name': who}))
        else:
            found.append(_alert(
                'warning', 'client_rate_limited', who, f'Client {who} keeps hitting the rate limit',
                f"{_plural(count, 'request')} refused in the last hour by QUERYAPIGATE_RATE_LIMIT, counted by "
                'address before any key is checked.', {'type': 'settings', 'name': 'traffic'}))
    return found


def _history_health():
    counts = metrics.history_counts()
    found = []
    try:
        for kind, message in instances.problems():
            found.append(_alert('warning', kind, 'server', {
                'instances_not_shared': 'Instances share this store without sharing rate limits',
                'instances_versions_differ': 'Instances are running different versions'}[kind], message,
                {'type': 'settings', 'name': 'traffic'}))
    except Exception:  # an older store without the instances table, say - no alert rather than no alerts
        pass
    since = ratelimit.degraded()
    if since is not None:
        found.append(_alert(
            'warning', 'rate_limits_not_shared', 'server', 'Rate limits are counted per instance',
            'Redis failed a rate-limit check in the last ten minutes, so each instance is counting on its own - '
            'every limit is effectively multiplied by the number of instances. Check QUERYAPIGATE_REDIS_URL and Redis '
            "itself; see the server log.", {'type': 'settings', 'name': 'traffic'},
            time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(since))))
    if counts.get('failed'):
        found.append(_alert(
            'warning', 'history_failed', 'server', 'Run history could not be written',
            f"{_plural(counts['failed'], 'run')} could not be recorded since this server started - see the server "
            'log. Runs that are not recorded are missing from history, metrics screens and live events.',
            {'type': 'settings', 'name': 'history'}))
    if counts.get('dropped'):
        found.append(_alert(
            'warning', 'history_dropped', 'server', 'Run history is dropping runs',
            f"{_plural(counts['dropped'], 'run')} dropped since this server started because the store could not "
            'keep up. Lower QUERYAPIGATE_HISTORY_SAMPLE_RATE, or move to a PostgreSQL store.',
            {'type': 'settings', 'name': 'history'}))
    return found

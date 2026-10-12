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

from . import admins, apikeys, config, db, history, instances, metrics, oidc, ratelimit, usage

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
         'instances_not_shared', 'instances_versions_differ', 'rate_limits_not_shared', 'export_failing', 'key_unused',
         'agent_unusual_volume', 'sso_unreachable', 'writes_awaiting_approval', 'encryption_key_rotation_incomplete',
         'quota_nearly_used', 'audit_chain_broken', 'audit_delivery_behind', 'secret_provider_failing')


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
    found = [*_server(now), *_sso(), *_encryption(), *_secret_providers(), *_audit_chain(), *_audit_delivery(now),
             *_writes(),
             *_quotas(), *_keys(now), *_runs(now), *_rate_limited(), *_history_health()]
    return sorted(found, key=lambda a: (SEVERITY_ORDER[a['severity']], a['kind'], a['id']))


def _server(now):
    if apikeys.auth_required():
        return _break_glass(now)
    return [_alert('critical', 'open_server', 'server', 'Anyone can use this server',
                   'No API key is configured, so every request - including changing connections and keys - is '
                   'allowed. Set QUERYAPIGATE_API_KEY.', {'type': 'settings', 'name': 'security'})]


def _sso():
    """Console sign-in through SSO is on, but the identity provider's metadata can't be had: nobody can sign in that
    way until it can (sessions already open keep working)."""
    if not config.sso_enabled() or oidc.metadata(config.sso_issuer()) is not None:
        return []
    return [_alert('critical', 'sso_unreachable', 'sso', "Console sign-in through SSO is down",
                   f"The identity provider's metadata can't be fetched from {config.sso_issuer()}, so nobody can sign "
                   'in through it - sessions already open keep working. Check the provider is up and '
                   'QUERYAPIGATE_SSO_ISSUER is right; until then, sign in with an admin token or the break-glass key.',
                   {'type': 'settings', 'name': 'sso'})]


def _encryption():
    """QUERYAPIGATE_SECRET_KEY lists more than one key: a rotation was started (ADR 0009) and isn't finished -
    passwords are still under an old key (run rekey), or none are (remove the old keys)."""
    if len(config.secret_keys()) < 2:
        return []
    from . import rekey
    found = rekey.report()
    target = {'type': 'settings', 'name': 'security'}
    old = found['under_old_keys']
    if old:
        return [_alert('warning', 'encryption_key_rotation_incomplete', 'secret_key',
                       f"{_plural(old, 'password')} {'is' if old == 1 else 'are'} still encrypted with an old key",
                       'QUERYAPIGATE_SECRET_KEY lists several keys, so a rotation has started. Run `queryapigate '
                       'rekey` to re-encrypt them with the first key, then remove the others.', target)]
    return [_alert('info', 'encryption_key_rotation_incomplete', 'secret_key',
                   'Every password is under the new encryption key - remove the old ones',
                   f"QUERYAPIGATE_SECRET_KEY still lists {found['keys']} keys, but nothing is encrypted with any but "
                   'the first. Remove the others from every instance to finish the rotation.', target)]


def _secret_providers():
    """A secret provider - a file, Vault, AWS - failing for five minutes in this process (ADR 0015): connections whose
    secrets come from it work only while the values it gave before are within their time, then fail."""
    import time

    from . import secretrefs
    found = []
    for name, status in sorted(secretrefs.cache.status.items()):
        since = status.get('failing_since')
        if since is None or time.time() - since < 300:
            continue
        stamp = f'{datetime.fromtimestamp(since):%Y-%m-%d %H:%M:%S}'
        found.append(_alert('critical', 'secret_provider_failing', name, f'The secret provider {name} is failing',
                            f"It has failed since {stamp}: {status.get('last_error')}. Connections that take a secret "
                            'from it work only while what it gave before is within its time; then they fail with '
                            'secret_unavailable.', {'type': 'settings', 'name': 'security'}, stamp))
    return found


def _audit_chain():
    """The audit log's hash chain doesn't verify (ADR 0012): an entry changed, removed, inserted or reordered."""
    from . import auditchain
    try:
        report = auditchain.cached_verify(db.connection())
    except db.Error:
        return []
    if report['intact']:
        return []
    found = report['break']
    where = f"entry {found.get('seq')} ({found.get('action')}, {found.get('timestamp')})"
    problem = _CHAIN_PROBLEMS.get(found.get('problem'), found.get('problem'))
    return [_alert('critical', 'audit_chain_broken', 'audit_log', 'The audit log has been tampered with',
                   f"Its hash chain breaks at {where}: {problem}. Compare it with your SIEM's copy, or the export "
                   'file (queryapigate audit verify --file).', {'type': 'audit', 'name': 'verify'},
                   found.get('timestamp'))]


def _audit_delivery(now):
    """A SIEM output more than five minutes behind, or failing (ADR 0012)."""
    from . import auditstream
    if not auditstream.outputs():
        return []
    found = []
    for out in auditstream.status():
        oldest = out['oldest_undelivered_at'] if out['behind'] else None
        late = oldest is not None and oldest < (now - timedelta(minutes=5)).strftime('%Y-%m-%d %H:%M:%S')
        if not late and not (out['behind'] and out['last_error']):
            continue
        severity = 'critical' if late and out['last_error'] else 'warning'
        detail = (f"{out['behind']} audit entries are waiting, the oldest since {oldest}. "
                  + (f"Last error: {out['last_error']} ({out['last_error_at']}). " if out['last_error'] else '')
                  + 'They are kept, past the rolling cap, until delivered.')
        found.append(_alert(severity, 'audit_delivery_behind', out['output'],
                            f"The audit log isn't reaching {out['target'] or out['output']}", detail,
                            {'type': 'audit', 'name': 'delivery'}, oldest))
    return found


_CHAIN_PROBLEMS = {'changed': 'it was changed after it was written',
                   'changed_before': 'the entry before it was changed or removed',
                   'missing': 'entries before it are missing', 'out_of_order': 'it is out of order',
                   'unkeyed_after_keyed': 'it was re-written without the audit key'}


def _quotas():
    """Keys that have used 80% of a usage quota this period (warning), or all of it (critical) - ADR 0011."""
    from . import quotas
    found = []
    for name, key in apikeys.list_keys().items():
        usage = quotas.usage(name, key.get('quota')) if key.get('active', True) else None
        if usage is None:
            continue
        for what in ('calls', 'rows'):
            limit = usage[f'{what}_limit']
            if limit is None or usage[what] < 0.8 * limit:
                continue
            used_up = usage[what] >= limit
            found.append(_alert('critical' if used_up else 'warning', 'quota_nearly_used', f'{name}:{what}',
                                f"API key '{name}' has used {'all' if used_up else f'{usage[what] * 100 // limit}%'} "
                                f"of its {quotas.ADJECTIVE[usage['per']]} quota of {what}",
                                f"{usage[what]:,} of {limit:,} {what} this {usage['per']}. "
                                + ('Its calls are refused (quota_exceeded) until ' if used_up else 'It resets at ')
                                + f"{usage['resets_at']}.", {'type': 'key', 'name': name}, usage['period_start']))
    return found


def _writes():
    """AI agents' writes waiting for a person to approve them (ADR 0008) - each expires unapproved otherwise."""
    from . import agentwrites
    count, oldest = agentwrites.pending_count()
    if not count:
        return []
    return [_alert('warning', 'writes_awaiting_approval', 'writes',
                   f"{_plural(count, 'write')} by AI agents {'is' if count == 1 else 'are'} waiting for approval",
                   f'The oldest was asked for at {oldest}. Each expires unapproved after '
                   f'{config.write_approval_minutes()} minutes - approve or reject them on the Approvals screen.',
                   {'type': 'approvals', 'name': 'pending'}, oldest)]


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
    return [*_failing_connections(served, now), *_queries(served, now), *_exports(runs), *_agents(runs, now)]


def _agents(runs, now):
    """An AI agent suddenly reading far more than it usually does - a loop gone wrong, or someone pulling data out."""
    found = []
    for name, rows, average in usage.unusual_volume(runs, now, config.alert_agent_rows()):
        usual = f'about {round(average):,} an hour' if average >= 1 else 'next to nothing before'
        found.append(_alert('warning', 'agent_unusual_volume', name, f"Agent '{name}' is reading far more than usual",
                            f'{rows:,} rows over MCP in the last hour, against {usual}. Check what it is asking on '
                            'the Agents screen; narrow its grant, or lower its rate_limit, if it shouldn\'t.',
                            {'type': 'agent', 'name': name}, (now - timedelta(hours=1)).strftime(history.TIME_FORMAT)))
    return found


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

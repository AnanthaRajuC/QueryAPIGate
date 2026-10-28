"""What each AI agent did (BACKLOG #87, ADR 0006): GET /api/v1/usage/agents and the Console's Agents screen.

An agent is a caller on the MCP server - an API key, or a signed-in user (`jwt:<sub>`) - and everything here is
worked out from run history, where every MCP call already is: nothing new is recorded. Per agent: calls, rows read,
failures and refusals by code (a `table_not_allowed` or `mask_unresolvable` is an agent trying something its grant
doesn't allow), how often masking applied, the saved queries it ran and its most frequent ad-hoc statements - by text,
or by SHA-256 when QUERYAPIGATE_HISTORY_ADHOC_SQL is `hash`.

What history doesn't hold isn't here either: calls refused before they ran (no credential, a rate limit) are only in
/metrics; with QUERYAPIGATE_HISTORY_SAMPLE_RATE below 1, successful runs are a sample (`sample_rate` says so, and
failures are always kept); and runs past QUERYAPIGATE_HISTORY_RETENTION_DAYS are gone.
"""
import json
from datetime import datetime, timedelta

from . import config, db, history, runstore, scope
from .errors import ApiError

MAX_SCAN = 100_000          # runs read for one summary, newest first; past it the summary says `truncated`
TOP_STATEMENTS = 5
STATEMENT_PREVIEW = 500     # characters of an ad-hoc statement shown
DEFAULT_WINDOW = timedelta(hours=24)


def _statement(entry):
    """(key, shown) for an ad-hoc run's statement - whitespace-normalised text, or its hash - or None."""
    if isinstance(entry.get('sql'), str):
        text = ' '.join(entry['sql'].split())
        return 'sql:' + text, {'sql': text[:STATEMENT_PREVIEW], 'sql_sha256': None}
    if isinstance(entry.get('sql_sha256'), str):
        return 'sha:' + entry['sql_sha256'], {'sql': None, 'sql_sha256': entry['sql_sha256']}
    return None


def _rows(entry):
    rows = entry.get('rows')
    return rows if isinstance(rows, int) and not isinstance(rows, bool) else 0


def summarise(runs):
    """[(query_name, executed_at, entry)] -> one item per agent, busiest first."""
    agents = {}
    for query, executed_at, entry in runs:
        if entry.get('transport') != 'mcp':
            continue
        name = entry.get('key_name') or '-'
        agent = agents.setdefault(name, {
            'agent': name, 'calls': 0, 'rows': 0, 'errors': 0, 'masked_calls': 0, 'first_seen': executed_at,
            'last_seen': executed_at, 'connections': set(), 'queries': {}, 'adhoc_calls': 0, 'refusals': {},
            '_statements': {}})
        agent['calls'] += 1
        agent['rows'] += _rows(entry)
        agent['first_seen'] = min(agent['first_seen'], executed_at)
        agent['last_seen'] = max(agent['last_seen'], executed_at)
        if entry.get('connection_name'):
            agent['connections'].add(entry['connection_name'])
        if entry.get('masked'):
            agent['masked_calls'] += 1
        if entry.get('status') == 'error':
            agent['errors'] += 1
            code = entry.get('code') or 'internal_error'
            agent['refusals'][code] = agent['refusals'].get(code, 0) + 1
        if query is not None:
            agent['queries'][query] = agent['queries'].get(query, 0) + 1
            continue
        agent['adhoc_calls'] += 1
        statement = _statement(entry)
        if statement is not None:
            key, shown = statement
            counted = agent['_statements'].setdefault(key, {**shown, 'count': 0, 'rows': 0, 'errors': 0})
            counted['count'] += 1
            counted['rows'] += _rows(entry)
            counted['errors'] += entry.get('status') == 'error'
    items = []
    for agent in agents.values():
        statements = sorted(agent.pop('_statements').values(), key=lambda s: (-s['count'], s['sql'] or s['sql_sha256']))
        items.append({**agent, 'connections': sorted(agent['connections']),
                      'refusals': dict(sorted(agent['refusals'].items(), key=lambda kv: (-kv[1], kv[0]))),
                      'queries': dict(sorted(agent['queries'].items(), key=lambda kv: (-kv[1], kv[0]))),
                      'top_statements': statements[:TOP_STATEMENTS]})
    return sorted(items, key=lambda a: (-a['calls'], a['agent']))


def _bound(value, label, default):
    """A date or a time, as GET /api/v1/history takes them; `default` when not given."""
    return history._time_bound(value, label) or default.strftime(history.TIME_FORMAT)


MAX_HOURS = 24 * 366


def _hours(value):
    try:
        hours = int(value)
    except (TypeError, ValueError):
        hours = 0
    if not 1 <= hours <= MAX_HOURS:
        raise ApiError(f'hours must be a whole number from 1 to {MAX_HOURS}', code='invalid_filter')
    return timedelta(hours=hours)


def agents(since=None, until=None, hours=None, now=None):
    """GET /api/v1/usage/agents: the summary for runs in [since, until) - or the last `hours` (in the server's own
    time, whatever the caller's clock says) - by default the last 24 hours."""
    now = now or datetime.now()
    if hours not in (None, '') and since not in (None, ''):
        raise ApiError('Give since or hours, not both', code='invalid_filter')
    window = _hours(hours) if hours not in (None, '') else DEFAULT_WINDOW
    since = _bound(since, 'since', now - window)
    until = _bound(until, 'until', now + timedelta(seconds=1))
    if since >= until:
        raise ApiError('since must be before until', code='invalid_filter')
    history.flush()
    if runstore.enabled():
        runstore.ensure_schema()
        rows = [{'query_name': row['query_name'], 'executed_at': row['executed_at'], 'entry_json': row['entry']}
                for row in runstore.newest_first('workspace = {ws:String}', {'ws': scope.current()}, MAX_SCAN + 1,
                                                 top=until, floor=since, columns='query_name, executed_at, entry')]
    else:
        rows = db.connection().execute(
            'SELECT query_name, executed_at, entry_json FROM execution_history WHERE workspace = ? '
            'AND executed_at >= ? AND executed_at < ? ORDER BY executed_at DESC LIMIT ?',
            (scope.current(), since, until, MAX_SCAN + 1)).fetchall()
    runs = []
    for row in rows[:MAX_SCAN]:
        try:
            runs.append((row['query_name'], row['executed_at'], json.loads(row['entry_json'])))
        except ValueError:
            continue
    items = summarise(runs)
    _add_writes(items, since, until)
    return {'since': since, 'until': until, 'sample_rate': config.history_sample_rate(),
            'truncated': len(rows) > MAX_SCAN, 'items': items}


WRITE_OUTCOMES = ('pending', 'approved', 'done', 'failed', 'rejected', 'expired')


def _add_writes(items, since, until):
    """Each agent's write requests (ADR 0008) in the window, by how they ended - an agent with requests and no runs
    is listed too, so a write asked for and rejected isn't invisible."""
    from . import agentwrites
    by_agent = {}
    for request in agentwrites.list_requests(limit=1000):
        if since <= request['created_at'] < until:
            outcome = 'pending' if request['status'] == 'running' else request['status']
            counts = by_agent.setdefault(request['caller'], dict.fromkeys(WRITE_OUTCOMES, 0))
            counts[outcome] = counts.get(outcome, 0) + 1
    known = {item['agent'] for item in items}
    for agent in sorted(set(by_agent) - known):
        items.append({'agent': agent, 'calls': 0, 'rows': 0, 'errors': 0, 'masked_calls': 0, 'first_seen': None,
                      'last_seen': None, 'connections': [], 'queries': {}, 'adhoc_calls': 0, 'refusals': {},
                      'top_statements': []})
    for item in items:
        item['writes'] = by_agent.get(item['agent'], dict.fromkeys(WRITE_OUTCOMES, 0))


# --- The agent_unusual_volume alert (alerts.py) ------------------------------------------------------------------

VOLUME_FACTOR = 10          # the last hour's rows against the agent's own average hour


def unusual_volume(runs, now, floor):
    """[(agent, rows in the last hour, average rows an hour before it)] for agents reading at least `floor` rows in
    the last hour and VOLUME_FACTOR times their own average hour over the runs given (the alerts' newest runs, up to
    seven days) - a plain comparison with the agent's own history, nothing learned."""
    if not floor:
        return []
    hour_ago = (now - timedelta(hours=1)).strftime(history.TIME_FORMAT)
    oldest = min((executed_at for _, executed_at, _ in runs), default=hour_ago)
    span = max((datetime.strptime(hour_ago, history.TIME_FORMAT) -
                datetime.strptime(min(oldest, hour_ago), history.TIME_FORMAT)).total_seconds() / 3600, 1)
    recent, before = {}, {}
    for _, executed_at, entry in runs:
        if entry.get('transport') != 'mcp':
            continue
        totals = recent if executed_at >= hour_ago else before
        name = entry.get('key_name') or '-'
        totals[name] = totals.get(name, 0) + _rows(entry)
    found = []
    for name, rows in sorted(recent.items()):
        average = before.get(name, 0) / span
        if rows >= floor and rows >= VOLUME_FACTOR * average:
            found.append((name, rows, average))
    return found

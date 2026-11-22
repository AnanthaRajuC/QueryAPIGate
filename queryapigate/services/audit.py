"""The audit log as the Management API presents it (/api/v1/audit, BACKLOG #72).

Every administrative change - who created, changed or removed a connection, saved query, API key or role, and
when - newest first. The log keeps the last config.audit_log_limit() entries (QUERYAPIGATE_AUDIT_LOG_LIMIT); the
response says how many, so a reader knows how far back it goes.
"""
import base64
import json

from .. import config, store
from ..errors import ApiError

MAX_PAGE = 1000  # also the default: more than the log keeps unless QUERYAPIGATE_AUDIT_LOG_LIMIT is raised


def encode_cursor(value):
    return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip('=')


def decode_cursor(cursor, check):
    """The value a next_cursor holds, if ``check`` accepts it - else invalid_filter, as GET /history answers."""
    try:
        value = json.loads(base64.urlsafe_b64decode(cursor + '=' * (-len(cursor) % 4)))
        if check(value):
            return value
    except (ValueError, TypeError):
        pass
    raise ApiError('cursor is not one this server returned', code='invalid_filter')


def page_limit(raw, default):
    """`limit`: 1 to MAX_PAGE, `default` when not given."""
    if raw in (None, ''):
        return default
    try:
        limit = int(raw)
    except (TypeError, ValueError):
        limit = 0
    if not 1 <= limit <= MAX_PAGE:
        raise ApiError(f'limit must be a whole number from 1 to {MAX_PAGE}', code='invalid_filter')
    return limit


def _matches(entry, action, actor, target, text):
    if action and entry.get('action') != action:
        return False
    if actor and entry.get('actor') != actor:
        return False
    if target and entry.get('target') != target:
        return False
    if text:
        haystack = ' '.join(str(entry.get(k) or '') for k in ('timestamp', 'actor', 'target')).lower()
        return text.lower() in haystack
    return True


ALL = object()


def listing(action=None, actor=None, target=None, text=None, workspace=ALL, limit=None, cursor=None):
    """A page of the matching entries, newest first - `limit` of them (default MAX_PAGE), and a `next_cursor` for the
    next page or null on the last (rc1, B13: a log kept longer than its default came back whole) - with what a filter
    needs: every action in the log (not only the matching ones), the stored total and the retention cap. `workspace`
    (ADR 0023): only that workspace's entries - None for the server's own, and every entry from before workspaces
    (which can't say whose it was) - or ALL."""
    limit = page_limit(limit, MAX_PAGE)
    before = decode_cursor(cursor, lambda v: isinstance(v, int)) if cursor else None
    entries = list(reversed(store.read_audit_entries()))
    if workspace is not ALL:
        entries = [(i, e) for i, e in entries if e.get('workspace') == workspace]
    matching = [(i, e) for i, e in entries if (before is None or i < before)
                and _matches(e, action, actor, target, (text or '').strip())]
    page = matching[:limit]
    return {
        'items': [e for _, e in page],
        'next_cursor': encode_cursor(page[-1][0]) if len(matching) > limit else None,
        'total': len(entries),
        'actions': sorted({e['action'] for _, e in entries if e.get('action')}),
        'retention': config.audit_log_limit(),
    }

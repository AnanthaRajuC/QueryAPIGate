"""The audit log as the Management API presents it (/api/v1/audit, BACKLOG #72).

Every administrative change - who created, changed or removed a connection, saved query, API key or role, and
when - newest first. The log keeps the last config.audit_log_limit() entries (QUERYAPIGATE_AUDIT_LOG_LIMIT); the
response says how many, so a reader knows how far back it goes.
"""
from .. import config, store


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


def listing(action=None, actor=None, target=None, text=None):
    """The matching entries, newest first, with what a filter needs: every action in the log (not only the
    matching ones), the stored total and the retention cap."""
    entries = list(reversed(store.read_audit_log()))
    return {
        'items': [e for e in entries if _matches(e, action, actor, target, (text or '').strip())],
        'total': len(entries),
        'actions': sorted({e['action'] for e in entries if e.get('action')}),
        'retention': config.audit_log_limit(),
    }

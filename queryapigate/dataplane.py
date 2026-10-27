"""queryapigate.dataplane - the plans a Rust data plane in front of this server applies (ADR 0028, a prototype).

The data plane serves `/q/<name>` calls itself when it holds a plan for them, and forwards every other request here
unchanged. A forwarded call carrying the data plane's secret (QUERYAPIGATE_DATAPLANE_SECRET, X-QAG-Dataplane) is served
exactly as any other, with two differences:

- every store read the metadata cache would serve is made fresh, and its digest recorded (metacache.capture()); and
- if the call is one the data plane could serve (eligible()), its successful response carries the plan (X-QAG-Plan):
  everything this server decided about it that doesn't depend on the parameters' values.

A plan holds the verdicts only Python computes - the SQL guard, the table allow-list, the masking trace - as their
result: the statement to send, in segments around its bound parameters; its own row window, resolved per call; the
search_path a table-restricted PostgreSQL call pins; and per result column, its mask action. It names the digests of
the rows it was computed from (the key, the saved query, the connection), so the data plane uses it only for a call
whose rows digest the same, and a masked plan expires with the schema its trace was made from. The data plane never
decides anything of its own: it applies a plan, or it forwards.

Saved queries with `{name}` text placeholders never get a plan: their values are part of the SQL text, so the verdict
can change from one call to the next.
"""
import base64
import hmac
import ipaddress
import json

from . import config, runners, sqltools

SECRET_HEADER = 'X-QAG-Dataplane'
PEER_HEADER = 'X-QAG-Peer'
PLAN_HEADER = 'X-QAG-Plan'
VERSION = 1
DIALECTS = ('postgres', 'mysql')  # the two the prototype serves - every other connection is forwarded
_ROWS = ('api_key', 'saved_query', 'connection')  # what a plan names the digests of, one row set each


SECRET_ENVIRON = 'HTTP_X_QAG_DATAPLANE'  # the two headers as WSGI names them - read, and removed, by app._CapturePeer
PEER_ENVIRON = 'HTTP_X_QAG_PEER'


def matches(supplied):
    """Whether a request's X-QAG-Dataplane is QUERYAPIGATE_DATAPLANE_SECRET, compared in constant time. The data plane
    drops the header from whatever a client sends, so only it can send the right one."""
    secret = config.dataplane_secret()
    return bool(secret and supplied) and hmac.compare_digest(supplied.encode('utf-8', 'replace'), secret.encode())


def is_address(peer):
    """Whether X-QAG-Peer names an IP address - the client's socket address, as the data plane saw it."""
    try:
        ipaddress.ip_address(peer or '')
    except ValueError:
        return False
    return True


def eligible_caller(permission):
    """A caller the data plane may serve: a scoped API key - not an administrator, a signed-in user or an open server -
    in the default workspace, with none of the limits that keep their own state (rate limit, quota, ADR 0011's)."""
    return (permission is not None and not permission.admin and permission.claims is None and permission.role is None
            and not permission.claim_binding and permission.workspace == 'default'
            and permission.rate_limit is None and permission.quota is None and permission.max_concurrent is None
            and permission.query_timeout is None and permission.max_rows is None)


def digests(record):
    """{kind: digest} of the one row set of each kind this request read, or None when it read any kind twice with
    different results, or under more than one key - then no single set of rows explains the verdict."""
    found = {}
    for (kind, _key), seen in record.items():
        if kind in found or len(seen) != 1:
            return None
        found[kind] = next(iter(seen))
    return found if all(kind in found for kind in _ROWS) else None


def _operand(operand):
    """A window operand as a plan carries it: {'value': n} for a literal, {'param': name, 'clause': ...} for a bound
    one - resolved and checked per call, as sqltools._window_value() does - or None for none."""
    if operand is None:
        return None
    token, clause = operand
    if token.startswith(':'):
        return {'param': token[1:], 'clause': clause}
    return {'value': int(token)}


def statement(sql, dialect):
    """The plan's statement: `sql` (as validated) in segments - text, and {'param': name} where a bound parameter goes -
    with its own row window apart when the runner pages it (runners._prepare()), and where MySQL's time-limit hint goes
    (runners._mysql_timeout_wrap()). None when the data plane couldn't send it as the runner does."""
    paginated = sqltools.is_paginated(sql, dialect)
    limit = offset = None
    if paginated:
        sql, limit, offset = sqltools.own_window(sql, dialect)
    hint_at = runners._leading_select(sql) if dialect == 'mysql' else None
    if dialect == 'mysql' and hint_at is None:
        return None  # the runner sets the session's time limit for this one, a round trip the data plane doesn't make
    segments, position = [], 0
    for match in sqltools._param_re(dialect).finditer(sql):
        if match.group('name'):
            segments.append(sql[position:match.start()])
            segments.append({'param': match.group('name')})
            position = match.end()
    segments.append(sql[position:])
    if hint_at is not None and hint_at > len(segments[0]):
        return None
    return {'segments': segments, 'paginated': paginated, 'limit': _operand(limit), 'offset': _operand(offset),
            'hint_at': hint_at}


def build(record, permission, query_name, number, saved, connection_name, result, effective_allow_writes):
    """The plan for a `/q/` call that just succeeded, or None when the data plane must keep forwarding it."""
    prepared = getattr(result, 'prepared', None)
    rows = digests(record)
    if prepared is None or rows is None or not eligible_caller(permission) or effective_allow_writes:
        return None
    details = prepared['details']
    dialect = details['db']
    if dialect not in DIALECTS or connection_name != saved.get('connection_name') or saved.get('cache_ttl'):
        return None
    if sqltools._BRACE_RE.search(saved['sql_query']) or not sqltools.is_read_only(prepared['sql'], dialect):
        return None
    if any(isinstance(rule, dict) and rule.get('embed') for rule in (saved.get('query_parameters') or {}).values()):
        return None
    sent = statement(prepared['sql'], dialect)
    if sent is None:
        return None
    mask, expires_in = None, None
    masked = prepared['mask_plan']
    if masked is not None:
        from . import masking
        expires_in = masking.schema_expires_in(connection_name)
        if expires_in is None:
            return None
        mask = {'columns': len(masked.traced), 'actions': masked.actions, 'fired': masked.fired}
    return {'v': VERSION, 'rows': rows, 'key': {'workspace': permission.workspace, 'name': permission.name},
            'query': {'name': query_name, 'version': number}, 'connection': {'name': connection_name,
            'dialect': dialect}, 'statement': sent, 'search_path': prepared['search_path'], 'mask': mask,
            'expires_in': expires_in,
            # the declared parameters params.resolve() enforces: those the SQL uses, found as app.run_saved() finds them
            'used': sorted(sqltools.placeholder_names(saved['sql_query'])),
            'sql': prepared['sql']}  # as validated: what engine.execute_sql() logs and hashes


def encode(plan):
    """The plan as the X-QAG-Plan header's value: compact JSON, base64url - header-safe whatever the SQL holds."""
    text = json.dumps(plan, separators=(',', ':'), sort_keys=True)
    return base64.urlsafe_b64encode(text.encode('utf-8')).decode('ascii')


def decode(value):
    return json.loads(base64.urlsafe_b64decode(value.encode('ascii')))

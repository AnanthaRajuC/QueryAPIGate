"""What every front door applies to a caller before and after the work itself (BACKLOG #61): authentication, the
server-wide rate limit by client address, a key's own `rate_limit` grant, and request metrics.

These used to live only in REST's Flask request hooks, so a front door that doesn't pass through them - the MCP
server runs a saved query inside a synthetic request context, which runs no hooks - skipped them: no rate limit for
an agent's key, no JWT, and its calls missing from /metrics. REST's hooks (app.py) and the MCP server
(mcp_server.handle_call) now both call these, so a new front door gets them by calling the same functions.

The limiters are held on the Flask app. With QUERYAPIGATE_REDIS_URL set they count in Redis, so every instance and
process - `queryapigate serve`, `queryapigate mcp`, `queryapigate events` - shares one budget per client and per key
(BACKLOG #55); without it each process counts on its own.
"""
import ipaddress
from dataclasses import dataclass

from . import admins, alerts, apikeys, config, jwtauth, metrics, ratelimit

OPEN_ACCESS_REMOTE = ('This server has no API key set, so it answers only local clients. Set QUERYAPIGATE_API_KEY '
                      'to allow access from other hosts.')


@dataclass(frozen=True)
class Verdict:
    """One rate-limit check: whether the call may proceed, and what to tell the caller either way."""
    allowed: bool
    limit: int
    remaining: int
    retry_after: int


def is_loopback(peer):
    """Whether a connection's real peer is a loopback address - the one caller open-access mode answers. Fails closed:
    an address that can't be parsed, or none at all (a Unix socket, a test client's name), counts as remote."""
    try:
        return ipaddress.ip_address(peer or '').is_loopback
    except ValueError:
        return False


def authenticate(api_key, authorization, client_ip):
    """The caller's Permission: a matched key (admin or scoped), a signed-in user's verified bearer token
    (jwtauth.py), the unrestricted OPEN default when no key is configured anywhere, or None when a key is required
    but missing or wrong. A call carrying an API key is judged on that key alone - a wrong key never falls through to
    the token."""
    if api_key:
        permission = apikeys.authenticate(api_key, client_ip=client_ip)
    elif authorization and config.jwt_enabled():
        permission = jwtauth.authenticate(authorization, client_ip=client_ip)
    else:
        permission = None
    if permission is not None:
        if permission.via == 'break-glass':
            admins.note_break_glass()
        return permission
    return None if apikeys.auth_required() else apikeys.OPEN


def authenticate_mcp(api_key, authorization, client_ip, peer=None):
    """(Permission, None) for a caller the MCP server lets in, else (None, refusal) - a jwtauth.McpRefusal saying
    401 or 403 and why. An API key is judged as everywhere. A bearer token, with OAuth on for the MCP server
    (ADR 0006), must be one issued for it - a REST token isn't, however valid; with OAuth off, REST's JWT settings
    apply, as before. Open access is for a local caller only: `client_ip` (the client a trusted proxy names, if one
    does) and the connection's own `peer`, when it's known, must both be loopback - so neither a remote client sent
    on by a proxy on this host, nor a forged X-Forwarded-For that a too-wide FORWARDED_ALLOW_IPS would honour, gets
    in."""
    if api_key or not authorization or not config.mcp_oauth_enabled():
        permission = authenticate(api_key, authorization, client_ip)
        if permission is apikeys.OPEN and not (is_loopback(client_ip) and (peer is None or is_loopback(peer))):
            # No key, administrator, JWT or OAuth configured anywhere: the unrestricted OPEN caller, which only a local
            # client may be - as REST's gate refuses it (app.py). Before 0.47 the MCP server let any client in as it.
            return None, jwtauth.McpRefusal(403, 'open_access_remote', OPEN_ACCESS_REMOTE)
        if permission is not None:
            return permission, None
        return None, jwtauth.McpRefusal(401, 'unauthorized', 'Unauthorized: missing or invalid X-API-Key or bearer '
                                                             'token')
    return jwtauth.authenticate_mcp(authorization, client_ip=client_ip)


def check_client_limit(flask_app, client_ip):
    """Count one call against its client address's share of QUERYAPIGATE_RATE_LIMIT; None when there is no limit.
    Checked before authentication, so guessing keys is throttled too."""
    limit = config.rate_limit()
    if limit is None:
        return None
    count, period = limit
    allowed, remaining, retry_after = flask_app.extensions['queryapigate_limiter'].hit(
        client_ip or 'unknown', count, period)
    if not allowed:
        metrics.inc_rate_limit_rejection()
        alerts.note_rate_limited('client', client_ip)
    return Verdict(allowed, count, remaining, retry_after)


def check_key_limit(flask_app, permission):
    """Count one call against the caller's own `rate_limit` grant (a key's, or a signed-in user's role's), in
    addition to the client limit, never instead of it; None when the caller has none (the admin key never does)."""
    if permission is None or permission.rate_limit is None:
        return None
    count, period = permission.rate_limit
    allowed, remaining, retry_after = flask_app.extensions['queryapigate_key_limiter'].hit(
        ratelimit.budget_name(permission), count, period)
    if not allowed:
        metrics.inc_rate_limit_rejection()
        alerts.note_rate_limited('key', (getattr(permission, 'workspace', 'default'), permission.name))
    return Verdict(allowed, count, remaining, retry_after)


def observe(method, endpoint, status, elapsed, key):
    """One finished call in /metrics' request counters and latency histograms. REST passes the HTTP method and its
    route's endpoint; MCP passes method 'MCP' and an 'mcp.*' endpoint, so the two can be told apart."""
    metrics.observe_request(method, endpoint, str(status), elapsed, key or '-')

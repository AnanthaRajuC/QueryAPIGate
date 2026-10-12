"""The access log (ADR 0016): one line per request - who asked, for what path, with what result - and never the query
string's values unless the deployer asks for them.

A saved query's parameters can arrive in the URL (`GET /q/orders?customer_email=alice@example.com`), and a web
server's usual access log writes the whole request line - so every value would be in the log platform for as long as
it keeps logs. QueryAPIGate's own log, run history and traces all leave values out on purpose; this keeps the access
log in step. QUERYAPIGATE_ACCESS_LOG_QUERY=1 puts the query string back. The referrer, which can carry another URL's
query string, is never logged.

Used by the image's gunicorn (`gunicorn -c python:queryapigate.gunicorn_conf`) and by `queryapigate serve`.
"""
from . import config

# gunicorn's atoms: the client, the time, method, path (U: without its query) and protocol, status, size, seconds, the
# request id QueryAPIGate answered with, and the user agent.
_WITHOUT_QUERY = '%(h)s - %(t)s "%(m)s %(U)s %(H)s" %(s)s %(b)s %(L)ss rid=%({x-request-id}o)s "%(a)s"'
_WITH_QUERY = '%(h)s - %(t)s "%(r)s" %(s)s %(b)s %(L)ss rid=%({x-request-id}o)s "%(a)s"'


def gunicorn_format():
    """gunicorn's access_log_format, as QUERYAPIGATE_ACCESS_LOG_QUERY says."""
    return _WITH_QUERY if config.access_log_query() else _WITHOUT_QUERY


def request_line(method, path, protocol):
    """A request line as the access log shows it: `path` with its query string removed, unless asked to keep it."""
    if not config.access_log_query():
        path = path.split('?', 1)[0]
    return f'{method} {path} {protocol}'

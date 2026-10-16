"""The workspace a piece of work acts in (ADR 0023 §4) - what every lookup of something a workspace owns is scoped to.

- **A request** acts in one workspace, settled once in app.gate(): an API key's or a signed-in user's role's own, or -
  for an administrator - the one the path names (`/w/<workspace>/...`, stripped by WorkspacePrefix below), or
  `default`. It is kept on flask.g, so a request thread reused for the next request starts afresh.
- **Anything else** - the CLI, a background writer, a stream's rows read after its request ended - acts in `default`
  unless it says otherwise with `using()`. A stream captures its request's workspace up front, as it captures the
  broadcaster (app.stream_sql_response()); queued history, events and usage counts carry theirs in the row.
"""
import contextlib
import contextvars
import re

from .db import DEFAULT_WORKSPACE

_override = contextvars.ContextVar('queryapigate_workspace', default=None)
PREFIX_RE = re.compile(r'^/w/([A-Za-z0-9][A-Za-z0-9_.-]{0,99})(/.*)?$')
ENVIRON_KEY = 'queryapigate.workspace'


def current():
    """The workspace this code acts in."""
    override = _override.get()
    if override is not None:
        return override
    from flask import g, has_app_context
    if has_app_context():
        workspace = g.get('workspace')
        if workspace is not None:
            return workspace
    return DEFAULT_WORKSPACE


@contextlib.contextmanager
def using(workspace):
    """Act in `workspace` for the block - over a request's own, too."""
    token = _override.set(workspace or DEFAULT_WORKSPACE)
    try:
        yield workspace
    finally:
        _override.reset(token)


class WorkspacePrefix:
    """WSGI middleware: `/w/<workspace>/rest` is `/rest` in that workspace - every route, runtime and Management API
    alike, takes the prefix with no route of its own. The name goes into the environ for app.gate() to settle."""

    def __init__(self, app):
        self.app = app

    def __call__(self, environ, start_response):
        match = PREFIX_RE.match(environ.get('PATH_INFO', ''))
        if match:
            environ[ENVIRON_KEY] = match.group(1)
            environ['SCRIPT_NAME'] = environ.get('SCRIPT_NAME', '') + f'/w/{match.group(1)}'
            environ['PATH_INFO'] = match.group(2) or '/'
        return self.app(environ, start_response)

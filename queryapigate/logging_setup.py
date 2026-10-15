"""One log line per event from the ``queryapigate`` logger, tagged with the request ID and the calling API key's
name that caused it (both ``-`` outside a request, e.g. at startup; key is also ``-`` when no key is
configured at all, and ``admin`` for ``QUERYAPIGATE_API_KEY`` - see ``apikeys.Permission``). Plain text by
default; ``QUERYAPIGATE_JSON_LOGS=1`` switches to one JSON object per line for log aggregators.

In JSON mode, any caller-supplied ``extra={...}`` on a ``log.info()``/``log.warning()`` call becomes its
own top-level JSON key, not just text folded into ``message`` - see ``_JsonFormatter``. ``engine.py`` and
``app.py`` use this for fields a log aggregator would otherwise have to parse out of the message string
(``connection``, ``dialect``, ``status``, ``duration_ms``, ...), so a query or request can be filtered on
those directly rather than only by ``request_id``/``key``, which were already real fields before this.

``configure()`` is called from ``create_app()`` - the one place every entry point (``queryapigate serve``,
gunicorn/WSGI, the test suite) goes through - so logging is set up the same way regardless of how the app
is run, instead of relying on the CLI's ``logging.basicConfig()``, which never runs under a WSGI server.
"""
import json
import logging

from flask import g

from . import config, tracing

_PLAIN_FORMAT = '%(asctime)s %(levelname)s %(name)s [%(request_id)s key=%(key)s]: %(message)s'
# Every attribute a plain LogRecord carries with no `extra` at all - used to spot which attributes on a
# given record were added via `extra={...}` (see _JsonFormatter below), without hardcoding their names.
_BASE_RECORD_ATTRS = frozenset(vars(logging.makeLogRecord({}))) | {'message', 'asctime'}


class _RequestContextFilter(logging.Filter):
    def filter(self, record):
        try:
            record.request_id = g.request_id
        except RuntimeError:  # outside a Flask request context (startup, CLI, background threads)
            record.request_id = '-'
            record.key = '-'
            return True
        permission = g.get('permission')  # unset before the key is resolved, e.g. a CORS preflight
        record.key = (permission.name if permission else None) or '-'
        return True


class _PlainFormatter(logging.Formatter):
    """One event, one line: a line break in the message - a caller's SQL, a request path with %0A in it - is written
    as \\n, so it can't start what reads as a log line of its own (CodeQL py/log-injection). A traceback keeps its
    lines: it comes after the message, from the server, not a caller."""

    def formatMessage(self, record):
        message = record.message
        record.message = message.replace('\\', '\\\\').replace('\r', '\\r').replace('\n', '\\n')
        try:
            return super().formatMessage(record)
        finally:
            record.message = message


class _JsonFormatter(logging.Formatter):
    def format(self, record):
        payload = {
            'time': self.formatTime(record, '%Y-%m-%dT%H:%M:%S'),
            'level': record.levelname,
            'logger': record.name,
            'request_id': getattr(record, 'request_id', '-'),
            'key': getattr(record, 'key', '-'),
            'message': record.getMessage(),
        }
        # request_id/key are already set above (from _RequestContextFilter, not a caller's `extra`); every
        # other attribute a record doesn't get by default came from an explicit `extra={...}` at the call
        # site, and becomes its own field here rather than staying folded into `message` above.
        payload.update({k: v for k, v in vars(record).items()
                        if k not in _BASE_RECORD_ATTRS and k not in ('request_id', 'key')})
        trace_id, span_id = tracing.ids()  # with tracing on (ADR 0010): the line leads to its trace, and back
        if trace_id:
            payload['trace_id'], payload['span_id'] = trace_id, span_id
        if record.exc_info:
            payload['exception'] = self.formatException(record.exc_info)
        return json.dumps(payload)


def configure(logger):
    """Attach one handler to ``logger``, replacing any handler set by an earlier call of this function (safe to
    call repeatedly - once per ``create_app()``, including in tests) but leaving any other handler alone, e.g.
    one ``unittest.TestCase.assertLogs`` installs temporarily while a test is running."""
    for existing in list(logger.handlers):
        if getattr(existing, '_queryapigate_managed', False):
            logger.removeHandler(existing)
    handler = logging.StreamHandler()
    handler._queryapigate_managed = True
    handler.addFilter(_RequestContextFilter())
    handler.setFormatter(_JsonFormatter() if config.json_logs() else _PlainFormatter(_PLAIN_FORMAT))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False

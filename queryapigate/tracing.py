"""Distributed tracing with OpenTelemetry (ADR 0010, BACKLOG #92) - optional and experimental.

On when an OTLP endpoint is set the standard way (OTEL_EXPORTER_OTLP_ENDPOINT or OTEL_EXPORTER_OTLP_TRACES_ENDPOINT),
with `pip install "queryapigate[otel]"`; every other OpenTelemetry variable works as for any service
(OTEL_SERVICE_NAME, OTEL_RESOURCE_ATTRIBUTES, OTEL_TRACES_SAMPLER, the exporter's headers and timeout). Off, nothing
is imported, and every helper here costs a dictionary lookup.

The spans are written by hand, not by auto-instrumentation, so each records only what's chosen for it: the route
rule, never the path's values or the query string; the saved query's name, never its statement; the database and
how many rows, never a row; who called, never their credential. Spans leave the server for a collector whose readers
have no data access - so parameter values, rows, statement text, query strings, headers and secrets never go into
one (tests/test_tracing.py fails if they do). Exceptions aren't recorded either: a database's message can carry
values. A span gets `error.type`, the error's code or class.

The provider starts lazily, on the first span in each process - after gunicorn forks, so its export thread lives
in the worker - and is flushed when the process exits.
"""
import atexit
import contextlib
import os
import threading

from . import __version__

_lock = threading.Lock()
_tracer = None  # set by _start(), or use() in tests
_provider = None
_ENDPOINTS = ('OTEL_EXPORTER_OTLP_ENDPOINT', 'OTEL_EXPORTER_OTLP_TRACES_ENDPOINT')

# connection type -> OpenTelemetry's db.system.name
DB_SYSTEMS = {'postgres': 'postgresql', 'mysql': 'mysql', 'sqlite': 'sqlite', 'duckdb': 'duckdb',
              'clickhouse': 'clickhouse', 'h2': 'h2database', 'jdbc': 'other_sql', 'mongo': 'mongodb'}


def enabled():
    """Whether this process should trace: an OTLP endpoint set, and the SDK not turned off (OTEL_SDK_DISABLED)."""
    if os.environ.get('OTEL_SDK_DISABLED', '').strip().lower() == 'true':
        return False
    return any(os.environ.get(name, '').strip() for name in _ENDPOINTS)


def check():
    """At startup: an endpoint set without the packages stops the server, saying how to install them."""
    if not enabled():
        return
    try:
        import opentelemetry.exporter.otlp.proto.http.trace_exporter  # noqa: F401
        import opentelemetry.sdk.trace  # noqa: F401
    except ImportError:
        raise ValueError('An OTLP endpoint is set (OTEL_EXPORTER_OTLP_ENDPOINT) but the OpenTelemetry packages are not '
                         'installed - run `pip install "queryapigate[otel]"`') from None


def _start():
    global _tracer, _provider
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    from . import instances
    attributes = {'service.version': __version__, 'service.instance.id': instances.instance_id()}
    if not os.environ.get('OTEL_SERVICE_NAME', '').strip():
        attributes['service.name'] = 'queryapigate'
    provider = TracerProvider(resource=Resource.create(attributes))  # the sampler: OTEL_TRACES_SAMPLER, as the SDK
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))  # in the background: never slows a request
    atexit.register(provider.shutdown)
    _provider, _tracer = provider, provider.get_tracer('queryapigate', __version__)


def flush(timeout_millis=5000):
    """Send what's waiting now, rather than in the next batch - for tests, and short-lived commands."""
    if _provider is not None:
        _provider.force_flush(timeout_millis)


def tracer():
    """The tracer, started on first use - or None when tracing is off."""
    if _tracer is not None:
        return _tracer
    if not enabled():
        return None
    with _lock:
        if _tracer is None:
            _start()
    return _tracer


def use(provider):
    """Tests: trace into `provider` (with an in-memory exporter), whatever the environment says; None turns it off -
    or, with an OTLP endpoint set, starts afresh from it on the next span."""
    global _tracer, _provider
    _provider = provider
    _tracer = provider.get_tracer('queryapigate', __version__) if provider is not None else None


def _kind(name):
    from opentelemetry.trace import SpanKind
    return {'server': SpanKind.SERVER, 'client': SpanKind.CLIENT}.get(name, SpanKind.INTERNAL)


def _clean(attributes):
    return {k: v for k, v in (attributes or {}).items() if v is not None}


def extract(carrier):
    """The context a caller's W3C `traceparent` (and `tracestate`) carries, from a dict of headers - or None."""
    if tracer() is None or not carrier:
        return None
    from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
    lowered = {k.lower(): v for k, v in carrier.items() if k.lower() in ('traceparent', 'tracestate') and v}
    return TraceContextTextMapPropagator().extract(lowered) if lowered else None


def from_environment():
    """The context a scheduler running this process passed in TRACEPARENT (and TRACESTATE) - or None."""
    return extract({'traceparent': os.environ.get('TRACEPARENT', '').strip(),
                    'tracestate': os.environ.get('TRACESTATE', '').strip()})


def start(name, kind='internal', attributes=None, parent=None):
    """A span made current until finish() - for request hooks, where a `with` can't reach from before to after.
    Returns an opaque handle, or None when tracing is off."""
    current = tracer()
    if current is None:
        return None
    from opentelemetry import context, trace
    span = current.start_span(name, context=parent, kind=_kind(kind), attributes=_clean(attributes))
    return span, context.attach(trace.set_span_in_context(span))


def finish(handle, error=None):
    if handle is None:
        return
    from opentelemetry import context
    span, token = handle
    if error is not None:
        failed(span, error)
    span.end()
    try:
        context.detach(token)
    except ValueError:  # detached in another context (a streamed response finishing elsewhere): nothing to undo
        pass


@contextlib.contextmanager
def span(name, kind='internal', attributes=None, parent=None):
    """A child span of whatever is current, for a `with` block; yields the span, or None when tracing is off. An
    exception marks it failed - by its type, never its message - and propagates."""
    current = tracer()
    if current is None:
        yield None
        return
    with current.start_as_current_span(name, context=parent, kind=_kind(kind), attributes=_clean(attributes),
                                       record_exception=False, set_status_on_exception=False) as opened:
        try:
            yield opened
        except BaseException as error:
            failed(opened, error)
            raise


def failed(span_, error):
    """Mark a span failed by an exception: `error.type` the ApiError's code or the exception's class."""
    code = getattr(error, 'code', None)
    mark_error(span_, code if isinstance(code, str) else type(error).__name__)


def mark_error(span_, error_type):
    """A span's status ERROR, with `error.type` - never a message, which can carry values."""
    from opentelemetry.trace import Status, StatusCode
    span_.set_attribute('error.type', error_type)
    span_.set_status(Status(StatusCode.ERROR))


def set_attributes(span_, **attributes):
    """Add attributes to `span_` (None: tracing is off); None values are left out. Names use '.' for '__'."""
    if span_ is None:
        return
    for key, value in attributes.items():
        if value is not None:
            span_.set_attribute(key.replace('__', '.'), value)


def current():
    """The current span, when tracing is on and one is recording - or None."""
    if tracer() is None:
        return None
    from opentelemetry import trace
    found = trace.get_current_span()
    return found if found.is_recording() else None


def ids():
    """(trace_id, span_id) of the current span as hex - for run history, log lines and error bodies - or
    (None, None)."""
    found = current()
    if found is None:
        return None, None
    context = found.get_span_context()
    return format(context.trace_id, '032x'), format(context.span_id, '016x')


def db_attributes(details, statement_kind, connection_name):
    """A database call's attributes - the system, the database and the server - never its statement."""
    database = details.get('database')
    return _clean({'db.system.name': DB_SYSTEMS.get(details.get('db'), 'other_sql'),
                   'db.operation.name': statement_kind.upper() or None,
                   # a network database's name - not SQLite's or DuckDB's, which is a file path on this server
                   'db.namespace': database if details.get('host') and isinstance(database, str) else None,
                   'server.address': details.get('host') or None,
                   'server.port': int(details['port']) if str(details.get('port') or '').isdigit() else None,
                   'queryapigate.connection': connection_name})

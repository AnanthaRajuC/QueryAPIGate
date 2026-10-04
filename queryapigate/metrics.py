"""In-process metrics, rendered as Prometheus text exposition format at ``/metrics``.

Counters and histograms live in this process's memory - correct for the image this project ships (gunicorn
with a single worker; see the Dockerfile, which explains why in a comment next to ``--workers 1``) but not
for a multi-process deployment, which would need a shared backing store instead (e.g. ``prometheus_client``
in its multiprocess mode) - nothing here does that.
"""
import bisect
import threading

from . import pool

_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0)

_lock = threading.Lock()
_request_counts: dict[tuple, int] = {}  # (method, endpoint, status, key) -> int
_request_hist: dict[tuple, list] = {}   # (method, endpoint) -> [count per bucket in _BUCKETS..., +Inf count]
_request_sum: dict[tuple, float] = {}   # (method, endpoint) -> total seconds
_query_counts: dict[tuple, int] = {}    # (connection, dialect, status, key) -> int
_query_hist: dict[tuple, list] = {}     # (connection, dialect) -> [count per bucket...]
_query_sum: dict[tuple, float] = {}     # (connection, dialect) -> total seconds
_stream_counts: dict[tuple, int] = {}   # (connection, dialect, status, key) -> int
_row_counts: dict[tuple, int] = {}      # (connection, dialect, key) -> total rows returned
_serialization_hist: dict[tuple, list] = {}  # (format,) -> [count per bucket...]
_serialization_sum: dict[tuple, float] = {}  # (format,) -> total seconds
_active_queries = 0                     # queries currently executing (paged or mid-stream)
_rate_limit_rejections = 0
_cache_hits = 0                         # responses served from cache.ResponseCache/rediscache.RedisResponseCache
_cache_misses = 0
_history_runs: dict[tuple, int] = {}  # (outcome,) -> runs: recorded / sampled_out / dropped / failed


def _bucket_index(elapsed):
    return bisect.bisect_left(_BUCKETS, elapsed)


def _key_label(key):
    """Every signed-in user (jwtauth.py: callers named `jwt:<user>`) is counted under one `jwt` label: one label
    value per user would grow these counters - and every Prometheus series built on them - with the user base."""
    return 'jwt' if isinstance(key, str) and key.startswith('jwt:') else key


def observe_request(method, endpoint, status, elapsed, key='-'):
    """`key` is the calling API key's name ('admin' for QUERYAPIGATE_API_KEY, '-' when none is configured), kept
    on the request/query counters (audit: who did what) but not the latency histograms, so the number of
    distinct keys never multiplies the size of the bucketed output."""
    with _lock:
        count_key = (method, endpoint, status, _key_label(key))
        _request_counts[count_key] = _request_counts.get(count_key, 0) + 1
        hkey = (method, endpoint)
        counts = _request_hist.setdefault(hkey, [0] * (len(_BUCKETS) + 1))
        counts[_bucket_index(elapsed)] += 1
        _request_sum[hkey] = _request_sum.get(hkey, 0.0) + elapsed


def observe_query(connection_name, dialect, status, elapsed, key='-'):
    with _lock:
        count_key = (connection_name, dialect, status, _key_label(key))
        _query_counts[count_key] = _query_counts.get(count_key, 0) + 1
        hkey = (connection_name, dialect)
        counts = _query_hist.setdefault(hkey, [0] * (len(_BUCKETS) + 1))
        counts[_bucket_index(elapsed)] += 1
        _query_sum[hkey] = _query_sum.get(hkey, 0.0) + elapsed


def observe_stream(connection_name, dialect, status, key='-'):
    """A separate counter from observe_query(): a streaming export can run for minutes, so mixing its
    duration into the regular per-query latency histogram would make that histogram's percentiles
    meaningless for the fast, page-at-a-time queries it actually describes. There is deliberately no
    equivalent streaming latency histogram here for the same reason - just how many started and how they
    ended."""
    with _lock:
        count_key = (connection_name, dialect, status, _key_label(key))
        _stream_counts[count_key] = _stream_counts.get(count_key, 0) + 1


def observe_rows(connection_name, dialect, key, count):
    """Rows actually returned to the caller - the trimmed page for a paged query, or however many made it
    out before a streaming export finished or failed partway through (see engine._drain()); not split by
    status, since a partial streamed count is still data that left the server, not nothing."""
    with _lock:
        row_key = (connection_name, dialect, _key_label(key))
        _row_counts[row_key] = _row_counts.get(row_key, 0) + count


def observe_serialization(output_format, elapsed):
    """How long formats.render() took to build the response body - JSON/CSV/TSV/XML/YAML/XLSX encoding,
    after the query itself has already finished. Only ever called for a paged (non-streaming) response:
    streaming formats it row by row, interleaved with network I/O, so there's no single "serialization
    happened here" span to measure the way there is for a page built in memory up front."""
    with _lock:
        hkey = (output_format,)
        counts = _serialization_hist.setdefault(hkey, [0] * (len(_BUCKETS) + 1))
        counts[_bucket_index(elapsed)] += 1
        _serialization_sum[hkey] = _serialization_sum.get(hkey, 0.0) + elapsed


def inc_active_query():
    global _active_queries
    with _lock:
        _active_queries += 1


def dec_active_query():
    """Pairs with inc_active_query(). For a paged query the whole span is one function call
    (engine.execute_sql); for a streaming export the connection stays checked out for as long as the client
    keeps reading, so the increment happens once in engine.stream_sql() but the decrement is deferred to
    engine._drain() - whichever of the two actually turns out to be where the query stops being active."""
    global _active_queries
    with _lock:
        _active_queries -= 1


def inc_rate_limit_rejection():
    global _rate_limit_rejections
    with _lock:
        _rate_limit_rejections += 1


def inc_cache_hit():
    global _cache_hits
    with _lock:
        _cache_hits += 1


def inc_history(outcome, count=1):
    with _lock:
        _history_runs[(outcome,)] = _history_runs.get((outcome,), 0) + count


def history_counts():
    """Runs by history outcome since this process started: recorded, sampled_out, dropped, failed."""
    with _lock:
        return {outcome: count for (outcome,), count in _history_runs.items()}


def inc_cache_miss():
    global _cache_misses
    with _lock:
        _cache_misses += 1


def summary_for_key(name):
    """Live usage for one API key's name, aggregated from the same counters /metrics renders - queries run,
    of those how many failed, and rows returned. No latency figure: the request/query duration histograms
    are deliberately not split by key (see observe_request()'s docstring), so there is nothing to average
    per key without changing that trade-off."""
    with _lock:
        queries = sum(count for (_, _, _, key), count in _query_counts.items() if key == name)
        errors = sum(count for (_, _, status, key), count in _query_counts.items()
                     if key == name and status == 'error')
        rows = sum(count for (_, _, key), count in _row_counts.items() if key == name)
    return {'queries': queries, 'errors': errors, 'rows': rows}


def summary_for_connection(name):
    """Live usage for one connection name, aggregated the same way as summary_for_key() - plus an average
    query latency, which *is* available here: the duration histograms are keyed by (connection, dialect),
    and a connection has exactly one dialect, so this is an unambiguous per-connection average."""
    with _lock:
        queries = sum(count for (conn, _, _, _), count in _query_counts.items() if conn == name)
        errors = sum(count for (conn, _, status, _), count in _query_counts.items()
                     if conn == name and status == 'error')
        rows = sum(count for (conn, _, _), count in _row_counts.items() if conn == name)
        elapsed = sum(total for (conn, _), total in _query_sum.items() if conn == name)
        sampled = sum(sum(counts) for (conn, _), counts in _query_hist.items() if conn == name)
    avg_duration_ms = round(elapsed / sampled * 1000, 1) if sampled else None
    return {'queries': queries, 'errors': errors, 'rows': rows, 'avg_duration_ms': avg_duration_ms}


def _escape(value):
    return str(value).replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n')


def _labels(pairs):
    return '{' + ','.join(f'{name}="{_escape(value)}"' for name, value in pairs) + '}'


def _render_counter(lines, name, help_text, label_names, counts):
    lines.append(f'# HELP {name} {help_text}')
    lines.append(f'# TYPE {name} counter')
    for key in sorted(counts):
        lines.append(f'{name}{_labels(zip(label_names, key, strict=False))} {counts[key]}')


def _render_histogram(lines, name, help_text, label_names, hist, totals):
    lines.append(f'# HELP {name} {help_text}')
    lines.append(f'# TYPE {name} histogram')
    for key in sorted(hist):
        labels = list(zip(label_names, key, strict=False))
        cumulative = 0
        # hist has one more count than there are edges - the +Inf bucket, written after this loop
        for edge, count in zip(_BUCKETS, hist[key], strict=False):
            cumulative += count
            lines.append(f'{name}_bucket{_labels([*labels, ("le", edge)])} {cumulative}')
        cumulative += hist[key][-1]  # the +Inf bucket
        lines.append(f'{name}_bucket{_labels([*labels, ("le", "+Inf")])} {cumulative}')
        lines.append(f'{name}_sum{_labels(labels)} {totals.get(key, 0.0)}')
        lines.append(f'{name}_count{_labels(labels)} {cumulative}')


def render(cache=None):
    """The current metrics as Prometheus text exposition format. `cache` is the live response-cache
    instance (cache.ResponseCache or rediscache.RedisResponseCache, app.extensions['queryapigate_cache']) -
    passed in by the /metrics route rather than imported here, since the cache lives per-Flask-app, not as
    a module-level singleton the way pool.py's shared pool does."""
    with _lock:
        request_counts = dict(_request_counts)
        request_hist = {k: list(v) for k, v in _request_hist.items()}
        request_sum = dict(_request_sum)
        query_counts = dict(_query_counts)
        query_hist = {k: list(v) for k, v in _query_hist.items()}
        query_sum = dict(_query_sum)
        stream_counts = dict(_stream_counts)
        row_counts = dict(_row_counts)
        serialization_hist = {k: list(v) for k, v in _serialization_hist.items()}
        serialization_sum = dict(_serialization_sum)
        active_queries = _active_queries
        rejections = _rate_limit_rejections
        cache_hits = _cache_hits
        cache_misses = _cache_misses
        history_runs = dict(_history_runs)

    lines = []
    _render_counter(lines, 'queryapigate_requests_total', 'Total HTTP requests.',
                    ('method', 'endpoint', 'status', 'key'), request_counts)
    _render_histogram(lines, 'queryapigate_request_duration_seconds', 'HTTP request latency in seconds.',
                      ('method', 'endpoint'), request_hist, request_sum)
    _render_counter(lines, 'queryapigate_queries_total', 'Total SQL queries executed.',
                    ('connection', 'dialect', 'status', 'key'), query_counts)
    _render_histogram(lines, 'queryapigate_query_duration_seconds', 'SQL query latency in seconds.',
                      ('connection', 'dialect'), query_hist, query_sum)
    _render_counter(lines, 'queryapigate_stream_exports_total', 'Total streaming (stream=true) exports started.',
                    ('connection', 'dialect', 'status', 'key'), stream_counts)
    _render_counter(lines, 'queryapigate_rows_returned_total', 'Total rows returned by SQL queries.',
                    ('connection', 'dialect', 'key'), row_counts)
    _render_histogram(lines, 'queryapigate_serialization_duration_seconds',
                      'Response body serialization latency in seconds, by output format (paged responses only).',
                      ('format',), serialization_hist, serialization_sum)

    _render_counter(lines, 'queryapigate_history_runs_total',
                    'Saved-query runs by what happened to their history entry: recorded, sampled_out (not kept '
                    'by QUERYAPIGATE_HISTORY_SAMPLE_RATE), dropped (the write queue was full) or failed (the '
                    'write itself failed).', ('outcome',), history_runs)
    from . import history  # imported here: history imports this module
    lines.append('# HELP queryapigate_history_pending Saved-query runs queued for the next batched history write.')
    lines.append('# TYPE queryapigate_history_pending gauge')
    lines.append(f'queryapigate_history_pending {history.pending_count()}')

    lines.append('# HELP queryapigate_active_queries SQL queries currently executing (paged or mid-stream).')
    lines.append('# TYPE queryapigate_active_queries gauge')
    lines.append(f'queryapigate_active_queries {active_queries}')

    lines.append('# HELP queryapigate_pool_idle_connections Idle pooled database connections currently held.')
    lines.append('# TYPE queryapigate_pool_idle_connections gauge')
    shared = pool.get_pool()
    lines.append(f'queryapigate_pool_idle_connections {shared.idle_count() if shared else 0}')

    lines.append('# HELP queryapigate_pool_active_connections Pooled database connections checked out right '
                 'now (not capped - see pool.py - purely observability).')
    lines.append('# TYPE queryapigate_pool_active_connections gauge')
    lines.append(f'queryapigate_pool_active_connections {shared.active_count() if shared else 0}')

    lines.append('# HELP queryapigate_rate_limit_rejections_total Requests rejected by the rate limiter.')
    lines.append('# TYPE queryapigate_rate_limit_rejections_total counter')
    lines.append(f'queryapigate_rate_limit_rejections_total {rejections}')

    lines.append('# HELP queryapigate_cache_hits_total Responses served from the cache_ttl response cache.')
    lines.append('# TYPE queryapigate_cache_hits_total counter')
    lines.append(f'queryapigate_cache_hits_total {cache_hits}')

    lines.append('# HELP queryapigate_cache_misses_total Cacheable requests not found in the response cache.')
    lines.append('# TYPE queryapigate_cache_misses_total counter')
    lines.append(f'queryapigate_cache_misses_total {cache_misses}')

    lines.append('# HELP queryapigate_cache_entries Responses currently held in the response cache.')
    lines.append('# TYPE queryapigate_cache_entries gauge')
    lines.append(f'queryapigate_cache_entries {cache.size() if cache is not None else 0}')

    return '\n'.join(lines) + '\n'

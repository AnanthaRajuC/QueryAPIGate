#!/usr/bin/env python3
"""General API latency benchmark (BACKLOG #25) - answers a different question from run.py's large-export
buffered-vs-streamed benchmark: for the common case (a small GET /q/<name> or POST /execute_sql call), how
much overhead does queryapigate itself add on top of the database's own query time? See
benchmarks/README.md for methodology.

Uses a tiny, fixed-size table (100 rows, narrow width - see dataset.py) so the database's own execution time
is close to zero and what's left in the measured latency is close to queryapigate's own overhead: parameter
binding, the SQL guard, rate-limit/permission checks and response serialization. `health` never touches a
database at all - a floor for pure HTTP/WSGI overhead to compare the other two scenarios against.

Run manually, never from CI. Needs a reachable database - see benchmarks/README.md.
"""
import argparse
import json
import os
import platform
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

import dataset  # noqa: E402,I001
from common import Server, build_connections_file, get_connection_details, native_close, native_connect, save_query  # noqa: E402,I001
from queryapigate import __version__ as queryapigate_version  # noqa: E402,I001

RESULTS_DIR = Path(__file__).parent / 'results'
ROWS = 100  # small and fixed - the point is overhead, not query execution time
PERCENTILES = (50, 95, 99)


def seed(dialect, details, home):
    width_chars = dataset.WIDTHS['narrow']
    if dialect == 'sqlite':
        import sqlite3
        path = home / 'bench.db'
        conn = sqlite3.connect(str(path))
        dataset.seed_sqlite(conn, ROWS, width_chars)
        conn.close()
        return str(path)
    if dialect == 'duckdb':
        import duckdb
        path = home / 'bench.duckdb'
        conn = duckdb.connect(str(path))
        dataset.seed_duckdb(conn, ROWS, width_chars)
        conn.close()
        return str(path)
    conn = native_connect(dialect, details)
    try:
        dataset.SEEDERS[dialect](conn, ROWS, width_chars)
    finally:
        native_close(dialect, conn)
    return None


def timed_get(server, path):
    req = urllib.request.Request(f'http://127.0.0.1:{server.port}{path}')
    started = time.monotonic()
    with urllib.request.urlopen(req, timeout=30) as res:
        res.read()
    return time.monotonic() - started


def timed_execute_sql(server):
    body = json.dumps({'sql': 'SELECT * FROM bench_data WHERE id = 1', 'connection_name': 'bench'}).encode()
    req = urllib.request.Request(f'http://127.0.0.1:{server.port}/execute_sql', data=body,
                                 headers={'Content-Type': 'application/json'}, method='POST')
    started = time.monotonic()
    with urllib.request.urlopen(req, timeout=30) as res:
        res.read()
    return time.monotonic() - started


def percentiles(values, pcts):
    ordered = sorted(values)
    out = {}
    for p in pcts:
        idx = min(len(ordered) - 1, int(round(p / 100 * (len(ordered) - 1))))
        out[p] = ordered[idx]
    return out


def run_dialect(dialect, requests, port):
    details = get_connection_details(dialect)
    print(f'== {dialect} ==  requests={requests}')

    with tempfile.TemporaryDirectory() as home_str:
        home = Path(home_str)
        file_path = seed(dialect, details, home)
        build_connections_file(home, dialect, details, file_path)

        server = Server(home, port)
        try:
            save_query(server, 'bench_lookup', 'SELECT * FROM bench_data WHERE id = :id',
                      query_parameters={'id': {'type': 'integer', 'required': True}})

            scenarios = {
                'health (no database)': lambda: timed_get(server, '/health'),
                'GET /q/<name>': lambda: timed_get(server, '/q/bench_lookup?id=1'),
                'POST /execute_sql': lambda: timed_execute_sql(server),
            }
            report = {'dialect': dialect, 'requests': requests, 'queryapigate_version': queryapigate_version,
                      'host': platform.platform(), 'cpu_count': os.cpu_count(),
                      'timestamp': datetime.now(timezone.utc).isoformat(), 'scenarios': {}}

            for name, fn in scenarios.items():
                print(f'  warming up {name!r}...', flush=True)
                for _ in range(10):
                    fn()
                print(f'  {name}: ', end='', flush=True)
                samples = [fn() for _ in range(requests)]
                pct = percentiles(samples, PERCENTILES)
                print(f'p50={pct[50] * 1000:.1f}ms p95={pct[95] * 1000:.1f}ms p99={pct[99] * 1000:.1f}ms')
                report['scenarios'][name] = {
                    'p50_ms': pct[50] * 1000, 'p95_ms': pct[95] * 1000, 'p99_ms': pct[99] * 1000,
                    'min_ms': min(samples) * 1000, 'max_ms': max(samples) * 1000, 'requests': requests,
                }
        finally:
            server.stop()
    return report


def render_markdown(report):
    lines = [
        f"# Latency benchmark: {report['dialect']}", '',
        f"- queryapigate {report['queryapigate_version']}, {report['timestamp']}",
        f"- {report['host']}, {report['cpu_count']} CPUs",
        f"- {ROWS} rows (fixed, narrow width), {report['requests']} requests per scenario, sequential "
        "(not concurrent - see pooling.py for that dimension)", '',
        '| Scenario | p50 (ms) | p95 (ms) | p99 (ms) | Min (ms) | Max (ms) |',
        '|---|---|---|---|---|---|',
    ]
    for name, s in report['scenarios'].items():
        lines.append(f"| {name} | {s['p50_ms']:.2f} | {s['p95_ms']:.2f} | {s['p99_ms']:.2f} "
                     f"| {s['min_ms']:.2f} | {s['max_ms']:.2f} |")
    return '\n'.join(lines) + '\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('dialect', choices=sorted(dataset.SEEDERS))
    parser.add_argument('--requests', type=int, default=200)
    parser.add_argument('--port', type=int, default=5310)
    args = parser.parse_args()

    report = run_dialect(args.dialect, args.requests, args.port)

    RESULTS_DIR.mkdir(exist_ok=True)
    date = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    out_path = RESULTS_DIR / f'latency-{args.dialect}-{date}.md'
    out_path.write_text(render_markdown(report))
    print(f'\nWrote {out_path}')
    print(render_markdown(report))


if __name__ == '__main__':
    main()

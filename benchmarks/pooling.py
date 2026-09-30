#!/usr/bin/env python3
"""Connection pooling under concurrency (BACKLOG #25). See pool.py's own module docstring first: this
pool only bounds *idle* connections kept around - it does not cap concurrent connections, so there is no
"wait time when the pool is exhausted" to measure the way a traditional bounded pool would have. What *is*
real to measure: as concurrent callers increase relative to QUERYAPIGATE_POOL_SIZE, how do throughput and
per-request latency change, and how does the idle-pool hit rate (queryapigate_pool_idle_connections, sampled
during the run) behave once concurrency exceeds the configured pool size?

Uses the same tiny fixed-size table as latency.py - the point is concurrency behavior, not query execution
time. A thread-per-caller client fires the same GET /q/<name> call in a tight loop for a fixed wall-clock
window at each concurrency level; QUERYAPIGATE_POOL_SIZE is fixed for the whole run so it's a controlled
comparison point, not an independent variable itself.

Run manually, never from CI. Needs a reachable database - see benchmarks/README.md.
"""
import argparse
import os
import platform
import statistics
import sys
import tempfile
import threading
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
ROWS = 100
WINDOW_S = 3.0  # wall-clock time each concurrency level runs for
DEFAULT_POOL_SIZE = 5  # matches config.DEFAULT_POOL_SIZE - fixed for the whole run, see module docstring


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


class Worker(threading.Thread):
    """One simulated concurrent caller: fires GET /q/bench_lookup in a tight loop until told to stop,
    recording each request's latency. A real thread (not asyncio) so the client side can genuinely overlap
    requests the way separate real clients would - matching this project's own "real subprocess, real HTTP"
    benchmark philosophy rather than simulating concurrency in a single event loop."""

    def __init__(self, port):
        super().__init__(daemon=True)
        self.port = port
        self.latencies = []
        self.errors = 0
        self._stop_event = threading.Event()

    def stop(self):
        self._stop_event.set()

    def run(self):
        url = f'http://127.0.0.1:{self.port}/q/bench_lookup?id=1'
        while not self._stop_event.is_set():
            started = time.monotonic()
            try:
                with urllib.request.urlopen(url, timeout=30) as res:
                    res.read()
                self.latencies.append(time.monotonic() - started)
            except Exception:
                self.errors += 1


def idle_connections(server):
    text = server.metrics_text()
    for line in text.splitlines():
        if line.startswith('queryapigate_pool_idle_connections '):
            return int(line.split()[-1])
    return None


def run_level(server, concurrency, window_s):
    workers = [Worker(server.port) for _ in range(concurrency)]
    idle_samples = []
    stop_sampling = threading.Event()

    def sample_idle():
        while not stop_sampling.is_set():
            v = idle_connections(server)
            if v is not None:
                idle_samples.append(v)
            stop_sampling.wait(0.2)

    sampler = threading.Thread(target=sample_idle, daemon=True)
    started = time.monotonic()
    for w in workers:
        w.start()
    sampler.start()
    time.sleep(window_s)
    for w in workers:
        w.stop()
    stop_sampling.set()
    for w in workers:
        w.join(timeout=5)
    sampler.join(timeout=2)
    elapsed = time.monotonic() - started

    all_latencies = [lat for w in workers for lat in w.latencies]
    total_errors = sum(w.errors for w in workers)
    total_requests = len(all_latencies) + total_errors
    return {
        'concurrency': concurrency,
        'requests': total_requests,
        'errors': total_errors,
        'throughput_rps': len(all_latencies) / elapsed if elapsed else 0,
        'p50_ms': statistics.median(all_latencies) * 1000 if all_latencies else None,
        'p95_ms': (sorted(all_latencies)[int(len(all_latencies) * 0.95)] * 1000
                  if len(all_latencies) >= 20 else None),
        'idle_min': min(idle_samples) if idle_samples else None,
        'idle_max': max(idle_samples) if idle_samples else None,
    }


def run_dialect(dialect, levels, window_s, pool_size, port):
    details = get_connection_details(dialect)
    print(f'== {dialect} ==  pool_size={pool_size}  levels={levels}  window={window_s}s')

    with tempfile.TemporaryDirectory() as home_str:
        home = Path(home_str)
        file_path = seed(dialect, details, home)
        build_connections_file(home, dialect, details, file_path)

        server = Server(home, port, pool_size=pool_size)
        try:
            save_query(server, 'bench_lookup', 'SELECT * FROM bench_data WHERE id = :id',
                      query_parameters={'id': {'type': 'integer', 'required': True}})
            # Warm the pool once before measuring, so the first real level isn't paying cold-connect cost.
            for _ in range(5):
                urllib.request.urlopen(f'http://127.0.0.1:{server.port}/q/bench_lookup?id=1', timeout=30).read()

            report = {'dialect': dialect, 'pool_size': pool_size, 'queryapigate_version': queryapigate_version,
                      'host': platform.platform(), 'cpu_count': os.cpu_count(),
                      'timestamp': datetime.now(timezone.utc).isoformat(), 'levels': []}
            for concurrency in levels:
                print(f'  concurrency={concurrency}: ', end='', flush=True)
                level = run_level(server, concurrency, window_s)
                print(f"{level['throughput_rps']:.0f} req/s, p50={level['p50_ms']:.1f}ms, "
                     f"idle {level['idle_min']}-{level['idle_max']}, errors={level['errors']}")
                report['levels'].append(level)
        finally:
            server.stop()
    return report


def render_markdown(report):
    lines = [
        f"# Pooling benchmark: {report['dialect']}", '',
        f"- queryapigate {report['queryapigate_version']}, {report['timestamp']}",
        f"- {report['host']}, {report['cpu_count']} CPUs",
        f"- QUERYAPIGATE_POOL_SIZE={report['pool_size']} (fixed for this run), {ROWS} rows (fixed, narrow width)",
        '',
        "queryapigate's pool only bounds *idle* connections kept around, not concurrent ones (see pool.py) - "
        "there is no bounded-pool wait time to show. `Idle range` is the min-max of "
        "`queryapigate_pool_idle_connections` sampled every 200ms during the window.", '',
        '| Concurrency | Throughput (req/s) | p50 (ms) | p95 (ms) | Idle range | Errors |',
        '|---|---|---|---|---|---|',
    ]
    for lv in report['levels']:
        p50 = f"{lv['p50_ms']:.2f}" if lv['p50_ms'] is not None else '—'
        p95 = f"{lv['p95_ms']:.2f}" if lv['p95_ms'] is not None else '—'
        idle = f"{lv['idle_min']}-{lv['idle_max']}" if lv['idle_min'] is not None else '—'
        lines.append(f"| {lv['concurrency']} | {lv['throughput_rps']:.0f} | {p50} | {p95} | {idle} | {lv['errors']} |")
    return '\n'.join(lines) + '\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('dialect', choices=sorted(dataset.SEEDERS))
    parser.add_argument('--levels', default='1,5,10,20,50', help='comma-separated concurrency levels')
    parser.add_argument('--window', type=float, default=WINDOW_S, help='seconds per concurrency level')
    parser.add_argument('--pool-size', type=int, default=DEFAULT_POOL_SIZE)
    parser.add_argument('--port', type=int, default=5320)
    args = parser.parse_args()
    levels = [int(x) for x in args.levels.split(',')]

    report = run_dialect(args.dialect, levels, args.window, args.pool_size, args.port)

    RESULTS_DIR.mkdir(exist_ok=True)
    date = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    out_path = RESULTS_DIR / f'pooling-{args.dialect}-{date}.md'
    out_path.write_text(render_markdown(report))
    print(f'\nWrote {out_path}')
    print(render_markdown(report))


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""Cache performance benchmark (BACKLOG #25) - the performance claim behind opt-in saved-query response
caching (`cache_ttl`, see documentation/API.md#response-caching) has never been benchmarked, only tested for
correctness (tests/test_queryapigate.py). This measures the actual hit-vs-miss latency delta and effectiveness
under a realistic repeated-call pattern.

A saved query with `cache_ttl` set is called on a fixed interval for longer than one TTL window: the first
call each window is a miss (X-Cache: MISS), the rest are hits (X-Cache: HIT) until the TTL rolls over. Real
HTTP throughout, classified by the response's own X-Cache header rather than assumed from timing.

Run manually, never from CI. Needs a reachable database - see benchmarks/README.md.
"""
import argparse
import os
import platform
import statistics
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
ROWS = 100
CACHE_TTL_S = 2
CALL_INTERVAL_S = 0.2
WINDOWS = 4  # how many TTL windows to run through


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


def timed_call(server, path):
    req = urllib.request.Request(f'http://127.0.0.1:{server.port}{path}')
    started = time.monotonic()
    with urllib.request.urlopen(req, timeout=30) as res:
        res.read()
        cache_status = res.headers.get('X-Cache', 'MISSING')
    return time.monotonic() - started, cache_status


def run_dialect(dialect, port):
    details = get_connection_details(dialect)
    total_calls = int(WINDOWS * CACHE_TTL_S / CALL_INTERVAL_S)
    print(f'== {dialect} ==  cache_ttl={CACHE_TTL_S}s  interval={CALL_INTERVAL_S}s  calls={total_calls}')

    with tempfile.TemporaryDirectory() as home_str:
        home = Path(home_str)
        file_path = seed(dialect, details, home)
        build_connections_file(home, dialect, details, file_path)

        server = Server(home, port)
        try:
            save_query(server, 'bench_cached', 'SELECT * FROM bench_data WHERE id = :id', cache_ttl=CACHE_TTL_S,
                      query_parameters={'id': {'type': 'integer', 'required': True}})
            save_query(server, 'bench_uncached', 'SELECT * FROM bench_data WHERE id = :id',
                      query_parameters={'id': {'type': 'integer', 'required': True}})

            samples = []
            for _i in range(total_calls):
                elapsed, status = timed_call(server, '/q/bench_cached?id=1')
                samples.append((elapsed, status))
                print('H' if status == 'HIT' else ('M' if status == 'MISS' else '?'), end='', flush=True)
                time.sleep(CALL_INTERVAL_S)
            print()

            hits = [s for s, status in samples if status == 'HIT']
            misses = [s for s, status in samples if status == 'MISS']

            # Baseline: the same query pattern with no cache_ttl set, so every call is a real query - the
            # "what would this cost without caching at all" comparison the miss samples above can't give on
            # their own (a miss still has to run a real query AND set the cache; this baseline never does the
            # second part, isolating query cost from cache-write cost).
            print('  baseline (no cache_ttl): ', end='', flush=True)
            baseline = [timed_call(server, '/q/bench_uncached?id=1')[0] for _ in range(20)]
            print(f'{statistics.median(baseline) * 1000:.2f}ms median')

            report = {
                'dialect': dialect, 'cache_ttl_s': CACHE_TTL_S, 'call_interval_s': CALL_INTERVAL_S,
                'queryapigate_version': queryapigate_version, 'host': platform.platform(),
                'cpu_count': os.cpu_count(), 'timestamp': datetime.now(timezone.utc).isoformat(),
                'hits': len(hits), 'misses': len(misses), 'total_calls': total_calls,
                'hit_median_ms': statistics.median(hits) * 1000 if hits else None,
                'miss_median_ms': statistics.median(misses) * 1000 if misses else None,
                'baseline_median_ms': statistics.median(baseline) * 1000,
            }
        finally:
            server.stop()
    return report


def render_markdown(report):
    hit_ms = f"{report['hit_median_ms']:.2f}" if report['hit_median_ms'] is not None else '—'
    miss_ms = f"{report['miss_median_ms']:.2f}" if report['miss_median_ms'] is not None else '—'
    speedup = (f"{report['miss_median_ms'] / report['hit_median_ms']:.1f}x"
              if report['hit_median_ms'] and report['miss_median_ms'] else '—')
    lines = [
        f"# Caching benchmark: {report['dialect']}", '',
        f"- queryapigate {report['queryapigate_version']}, {report['timestamp']}",
        f"- {report['host']}, {report['cpu_count']} CPUs",
        f"- cache_ttl={report['cache_ttl_s']}s, called every {report['call_interval_s']}s, "
        f"{report['total_calls']} calls total ({report['hits']} hits, {report['misses']} misses)", '',
        '| | Median latency (ms) |',
        '|---|---|',
        f"| Cache hit | {hit_ms} |",
        f"| Cache miss (query + cache write) | {miss_ms} |",
        f"| No cache_ttl at all (baseline query cost) | {report['baseline_median_ms']:.2f} |",
        '',
        f"Hit vs. miss speedup: **{speedup}**.",
    ]
    return '\n'.join(lines) + '\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('dialect', choices=sorted(dataset.SEEDERS))
    parser.add_argument('--port', type=int, default=5330)
    args = parser.parse_args()

    report = run_dialect(args.dialect, args.port)

    RESULTS_DIR.mkdir(exist_ok=True)
    date = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    out_path = RESULTS_DIR / f'caching-{args.dialect}-{date}.md'
    out_path.write_text(render_markdown(report))
    print(f'\nWrote {out_path}')
    print(render_markdown(report))


if __name__ == '__main__':
    main()

"""Light-load trace (as tpcc_mysql.py's n5) with the worker's and this client's GC collections logged, to line slow
calls up with them (PERFORMANCE_MYSQL_V2.md, The light-load tail). Needs session C's setup and builds in
.bench/mysql-v2; run as `python probe.py LABEL`, with sitecustomize.py beside it."""
import argparse
import http.client
import json
import os
import queue
import sys
import threading
import time
from pathlib import Path

REPO = next(p for p in Path(__file__).resolve().parents if (p / 'benchmarks' / 'tpcc_mysql.py').exists())
sys.path.insert(0, str(REPO / 'benchmarks'))
import tpcc_mysql as t  # noqa: E402

OUT = REPO / '.bench' / 'mysql-v2'
t.args = argparse.Namespace(out=str(OUT), refs='v0.42.0,origin/main', warehouses=20, zipf_s=1.0, seed='qag',
                            go_tpc=None, gateway_cpus='0-7', db_cpus='8-11', load_cpus='12-19', py_spy=None,
                            smoke=False, m2_queries='', head_label='main', base_label='v0.42.0')
t.OUT = OUT
t.BUILDS = {'v0.42.0': ('v0.42.0', {}), 'main': ('origin/main', {}),
            'main+cache': ('origin/main', {'QUERYAPIGATE_METADATA_CACHE_TTL': '5'})}
t.oltp.args = t.args


CLIENT_GCS = []
_CLIENT_GC_AT = [0.0, 0.0]  # wall clock and perf counter at the start of the collection in progress


def _client_gc(phase, info):
    if phase == 'start':
        _CLIENT_GC_AT[0], _CLIENT_GC_AT[1] = time.time(), time.perf_counter()
    elif info['generation'] >= 1 or time.perf_counter() - _CLIENT_GC_AT[1] > 0.001:
        CLIENT_GCS.append((_CLIENT_GC_AT[0], info['generation'], (time.perf_counter() - _CLIENT_GC_AT[1]) * 1000))


def trace(urls, key, rate=150, secs=60):
    import gc
    gc.callbacks.append(_client_gc)
    host = urls[0].split('/')[2]
    n = int(rate * secs)
    t0 = time.perf_counter() + 0.5
    wall0 = time.time() + (t0 - time.perf_counter())
    due = queue.Queue()
    for i in range(n):
        due.put((i, t0 + i / rate))
    out = [None] * n

    def worker():
        conn = http.client.HTTPConnection(host, timeout=30)
        while True:
            try:
                i, at = due.get_nowait()
            except queue.Empty:
                return
            wait = at - time.perf_counter()
            if wait > 0:
                time.sleep(wait)
            conn.request('GET', '/' + urls[i % len(urls)].split('/', 3)[3], headers={'X-API-Key': key})
            r = conn.getresponse()
            r.read()
            out[i] = (wall0 + (at - t0), (time.perf_counter() - at) * 1000, r.status)
    threads = [threading.Thread(target=worker) for _ in range(32)]
    [th.start() for th in threads]
    [th.join() for th in threads]
    return out


def main(label, settings):
    log = OUT / f'gc-{label}.log'
    for old in OUT.glob(f'gc-{label}.log.*'):
        old.unlink()
    build_path = OUT / 'builds' / 'main'
    t.start('main', settings={'PYTHONPATH': f'{Path(__file__).parent}:{build_path}', 'QAG_GC_LOG': str(log),
                              **settings})
    urls, key = t.url_file('order_status', 'uniform'), t.reader_key()
    t.oha(urls, key, c=8, secs=5)
    os.sched_setaffinity(0, {12, 13, 14, 15, 16, 17, 18, 19})  # the client on the load cores
    calls = trace(urls.read_text().split(), key)
    t.stop()
    gcs = []
    for f in OUT.glob(f'gc-{label}.log.*'):
        gcs += [tuple(float(x) for x in line.split()) for line in f.read_text().splitlines()]
    json.dump({'calls': calls, 'gcs': sorted(gcs)}, open(OUT / f'gc-{label}.json', 'w'))
    slow = [c for c in calls if c[1] > 10]
    gen2 = [g for g in gcs if g[1] == 2]
    print(f'{label}: {len(calls)} calls, {len(slow)} over 10 ms; collections logged {len(gcs)}, '
          f'generation 2: {len(gen2)}, their ms: {sorted(round(g[2], 1) for g in gen2)}')
    # each slow burst: the collection running when it began, if any
    starts = [c[0] for i, c in enumerate(slow) if i == 0 or c[0] - slow[i - 1][0] > 0.2]
    for s in starts:
        hit = [g for g in gcs if g[0] - 0.05 <= s <= g[0] + g[2] / 1000 + 0.05]
        mine = [g for g in CLIENT_GCS if g[0] - 0.05 <= s <= g[0] + g[2] / 1000 + 0.05]
        print(f'  slow burst at {s - calls[0][0]:6.2f} s: server', [(g[1], round(g[2], 1)) for g in hit] or '-',
              '| client', [(g[1], round(g[2], 1)) for g in mine] or '-')
    print('  client generation-2 collections:', [round(g[2], 1) for g in CLIENT_GCS if g[1] == 2],
          'at', [round(g[0] - calls[0][0], 1) for g in CLIENT_GCS if g[1] == 2])


if __name__ == '__main__':
    main(sys.argv[1], json.loads(sys.argv[2]) if len(sys.argv) > 2 else {})

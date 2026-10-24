#!/usr/bin/env python3
"""Tables from benchmarks/oltp.py's results.jsonl: each cell the median of its rounds with the min-max range, and a
comparison that calls a difference real only when the two ranges don't overlap. Writes <results>.md beside it.

    python benchmarks/oltp_report.py /tmp/qag-oltp-bench/results.jsonl

The access logs (b5) and py-spy profiles (b9, .txt or .txt.gz) are read from the same folder. An access log is
summarised to access_<scenario>.per_second.csv (requests, p50, p99 and slowest per second), which is used when the
log itself isn't there - small enough to commit, where the log isn't.
"""
import collections
import csv
import gzip
import json
import re
import statistics
import sys
from pathlib import Path

SRC = Path(sys.argv[1])
DIR = SRC.parent
rows = [json.loads(line) for line in open(SRC) if line.strip()]
env = [r for r in rows if r.get('block') == 'env']
BUILDS = next((e['builds'] for e in env if e.get('builds')), ['v0.41.0', 'main', 'main+cache'])
BASE, HEAD, CACHED = BUILDS
out = []


def emit(*lines):
    out.extend(lines)


def sel(**kw):
    return [r for r in rows if all(r.get(k) == v for k, v in kw.items())]


def stat(vals):
    vals = [v for v in vals if v is not None]
    return (statistics.median(vals), min(vals), max(vals), len(vals)) if vals else None


def fmt(st, digits=0):
    if st is None:
        return '-'
    f = f'{{:,.{digits}f}}'
    return f'{f.format(st[0])} ({f.format(st[1])}-{f.format(st[2])})'


def col(rs, key, digits=0):
    return fmt(stat([r.get(key) for r in rs]), digits)


def med(rs, key, digits=2):
    vals = [r[key] for r in rs if r.get(key) is not None]
    return f'{statistics.median(vals):.{digits}f}' if vals else '-'


def verdict(a, b):
    """b against a: the change in medians, and whether the two ranges are clear of each other."""
    if a is None or b is None:
        return '-'
    change = f'{(b[0] / a[0] - 1) * 100:+.0f}%'
    return change + (', ranges clear' if b[1] > a[2] or b[2] < a[1] else ', ranges overlap')


def table(head):
    emit(head, '|' + '---|' * (head.count('|') - 1))


emit('## Environment', '')
for e in env:
    emit('```', json.dumps(e, indent=1), '```')

emit('', '## /health floors', '')
table('| block | when | req/s | p50 ms | power profile | AC | EPP | package C | MHz |')
for r in sel(scenario='health_floor'):
    emit(f"| {r['block']} | {r['label']} | {r['rps']:,.0f} | {r['p50'] or 0:.2f} | {r.get('profile')} | "
         f"{r.get('ac')} | {r.get('epp')} | {r.get('pkg_temp_c')} | {r.get('mhz_gw')} |")

emit('', '## pgbench', '')
table('| workload | tps | latency ms | n |')
for label in ['pk_c1', 'pk_c32', 'tpcb_c32']:
    rs = sel(block='pgbench', scenario=label)
    emit(f"| {label} | {col(rs, 'tps')} | {col(rs, 'latency_ms', 3)} | {len(rs)} |")


def build_table(block, scenarios, c):
    emit('', f'## {block}: c={c}', '')
    table('| scenario | build | n | req/s | p50 ms | p99 ms | p99.9 ms | gateway cores | db cores | load cores | '
          f'store tx/req | MHz | vs {BASE} | vs {HEAD} |')
    for sc in scenarios:
        stats = {}
        for b in BUILDS:
            rs = sel(block=block, scenario=sc, build=b, c=c)
            if not rs:
                continue
            st = stats[b] = stat([r['rps'] for r in rs])
            emit(f"| {sc} | {b} | {len(rs)} | {fmt(st)} | {col(rs, 'p50', 2)} | {col(rs, 'p99', 1)} | "
                 f"{col(rs, 'p99.9', 1)} | {med(rs, 'gw_cores')} | {med(rs, 'pg_cores')} | {med(rs, 'load_cores')} | "
                 f"{med(rs, 'store_tx_per_req')} | {med(rs, 'mhz_gw', 0)} | "
                 f"{verdict(stats.get(BASE), st) if b != BASE else ''} | "
                 f"{verdict(stats.get(HEAD), st) if b == CACHED else ''} |")


build_table('b1', ['lookup_plain', 'lookup_gov', 'page_plain', 'write_writer'], 32)
build_table('b2', ['lookup_plain', 'lookup_gov'], 1)

emit('', '## b3: capacity (closed loop, c=32), then open loop, latency corrected', '')
for b in (HEAD, CACHED):
    rs = sel(block='b3cap', build=b)
    if rs:
        emit(f"- capacity, {b}: {col(rs, 'rps')} req/s over {len(rs)} rounds", '')
table('| build | offered (x capacity) | offered req/s | achieved req/s | p50 ms | p99 ms | p99.9 ms | ok | n |')
for b in (HEAD, CACHED):
    by = collections.defaultdict(list)
    for r in sel(block='b3', build=b):
        by[(r['offered_frac'], r['q'])].append(r)
    for (frac, q), rs in sorted(by.items()):
        emit(f"| {b} | {frac:.2f} | {q:,} | {col(rs, 'rps')} | {col(rs, 'p50', 2)} | {col(rs, 'p99', 1)} | "
             f"{col(rs, 'p99.9', 1)} | {min(r['success'] for r in rs):.4f} | {len(rs)} |")

emit('', '## b4: scale-out, c=64', '')
table('| scenario | workers | build | n | req/s | p50 ms | p99 ms | gateway cores | req/s per gateway core | '
      f'db cores | load cores | vs {HEAD} |')
for sc in ['lookup_plain', 'write_writer']:
    for w in (1, 2, 4, 8):
        base = None
        for b in (HEAD, CACHED):
            rs = sel(block='b4', scenario=sc, build=b, workers=w)
            if not rs:
                continue
            st = stat([r['rps'] for r in rs])
            base = st if b == HEAD else base
            gw = statistics.median(r['gw_cores'] for r in rs)
            emit(f"| {sc} | {w} | {b} | {len(rs)} | {fmt(st)} | {col(rs, 'p50', 1)} | {col(rs, 'p99', 1)} | "
                 f"{gw:.2f} | {st[0] / gw:,.0f} | {med(rs, 'pg_cores')} | {med(rs, 'load_cores')} | "
                 f"{verdict(base, st) if b == CACHED else ''} |")


def pct(vals, p):
    return vals[min(len(vals) - 1, int(len(vals) * p))]


def per_second(sc):
    """(per-second rows of (requests, p50, p99, slowest), every duration or None) - from the access log, writing the
    CSV summary beside it, or from that summary alone."""
    log, summary = DIR / f'access_{sc}.log', DIR / f'access_{sc}.per_second.csv'
    if log.exists():
        by = collections.defaultdict(list)
        for line in open(log):
            m = re.match(r'\[(\d+/\w+/\d+:\d+:\d+:\d+) [^\]]+\] (\d+) (\d+)', line)
            if m:
                by[m.group(1)].append(int(m.group(2)) / 1000)
        secs = [sorted(by[s]) for s in sorted(by)]
        rows_ = [(len(v), pct(v, 0.5), pct(v, 0.99), v[-1]) for v in secs]
        with open(summary, 'w', newline='') as f:
            w = csv.writer(f)
            w.writerow(['second', 'requests', 'p50_ms', 'p99_ms', 'slowest_ms'])
            for i, (n, p50, p99, mx) in enumerate(rows_):
                w.writerow([i, n, f'{p50:.2f}', f'{p99:.2f}', f'{mx:.2f}'])
        return rows_, sorted(v for vals in secs for v in vals)
    if summary.exists():
        with open(summary) as f:
            return [(int(r['requests']), float(r['p50_ms']), float(r['p99_ms']), float(r['slowest_ms']))
                    for r in csv.DictReader(f)], None
    return [], None


emit('', '## b5: cold path over 150 s (per-request durations from the access log)', '')
for sc in ['lookup_gov', 'lookup_plain']:
    secs, allv = per_second(sc)
    if not secs:
        continue
    if allv:
        emit(f'### {sc}: {len(allv):,} requests - p50 {pct(allv, 0.5):.2f} ms, p99 {pct(allv, 0.99):.2f}, '
             f'p99.9 {pct(allv, 0.999):.2f}, p99.99 {pct(allv, 0.9999):.2f}, max {allv[-1]:.1f}', '')
    else:
        emit(f'### {sc}: {sum(r[0] for r in secs):,} requests', '')
    typical = statistics.median(r[3] for r in secs)
    spikes = [(i, r[3]) for i, r in enumerate(secs) if r[3] > 4 * typical]
    emit(f"Median of each second's slowest request: {typical:.1f} ms. Seconds whose slowest is over 4x that: "
         + (', '.join(f't={i} s ({mx:.0f} ms)' for i, mx in spikes) or 'none'), '')

emit('', '## b6: access log', '')
table('| access log | n | req/s | p50 ms | p99 ms | gateway cores |')
stats = {}
for on in (True, False):
    rs = sel(block='b6', access_log=on)
    if rs:
        stats[on] = stat([r['rps'] for r in rs])
        emit(f"| {'on' if on else 'off'} | {len(rs)} | {fmt(stats[on])} | {col(rs, 'p50', 2)} | {col(rs, 'p99', 1)} | "
             f"{med(rs, 'gw_cores')} |")
if len(stats) == 2:
    emit('', f'off against on: {verdict(stats[True], stats[False])}')

build_table('b7', ['lookup_plain'], 1)
build_table('b7', ['lookup_plain'], 32)

emit('', '## b8: mixed and hot', '')
table('| scenario | n | req/s | p50 ms | p99 ms |')
for sc in ['mixed_reads', 'mixed_writes', 'write_writer', 'hotwrite_writer']:
    rs = sel(block='b8', scenario=sc)
    if rs:
        emit(f"| {sc} | {len(rs)} | {col(rs, 'rps')} | {col(rs, 'p50', 2)} | {col(rs, 'p99', 1)} |")

emit('', '## b10: threads per worker, c=32', '')
table('| threads | n | req/s | p50 ms | p99 ms | gateway cores |')
for t in (1, 2, 4, 8):
    rs = [r for r in sel(block='b10') if r.get('threads') == t and r.get('scenario') != 'health_floor']
    if rs:
        emit(f"| {t} | {len(rs)} | {col(rs, 'rps')} | {col(rs, 'p50', 2)} | {col(rs, 'p99', 1)} | "
             f"{med(rs, 'gw_cores')} |")

def slope(points):
    """Least-squares slope and intercept of [(x, y)]."""
    n = len(points)
    mx, my = sum(x for x, _ in points) / n, sum(y for _, y in points) / n
    k = sum((x - mx) * (y - my) for x, y in points) / sum((x - mx) ** 2 for x, _ in points)
    return k, my - k * mx


b11 = [r for r in sel(block='b11') if 'netem' in r]
if b11:
    delays = sorted({r['netem'] for r in b11}, key=lambda d: float(d[:-2]))
    emit('', '## b11: single-call latency against network delay (p50 ms; the slope is round trips per call)', '')
    table('| | ' + ' | '.join(f'+{d}' for d in delays) + ' | round trips (slope) | at 0 delay (intercept) |')
    series = [(f'psycopg2 alone, {m}', [r for r in b11 if r['scenario'] == f'probe_{m}'], 'ms_per_call')
              for m in ('one', 'four')] + [(b, [r for r in b11 if r.get('build') == b], 'p50') for b in BUILDS]
    for label, rs, key in series:
        if not rs:
            continue
        cells = [stat([r[key] for r in rs if r['netem'] == d]) for d in delays]
        k, c0 = slope([(float(r['netem'][:-2]), r[key]) for r in rs])
        emit(f"| {label} | " + ' | '.join(fmt(c, 2) for c in cells) + f' | {k:.2f} | {c0:.2f} ms |')


# py-spy: each sample goes to the category of the leaf-most frame that matches one
RULES = [
    ('logging', r'\blogging[/\\]|logging_setup\.py|accesslog\.py|gunicorn/glogging'),
    ('JSON', r'\bjson[/\\]|formats\.py'),
    ('store layer: queries, decoding, checks', r'queryapigate/(db|store|apikeys|metacache|admins)\.py'),
    ('data query: driver and pool', r'queryapigate/(runners|engine|pool)\.py'),
    ('SQL checks, parameters, masking', r'queryapigate/(sqltools|masking|tableguard|params|governance)\.py|sqlglot'),
    ('auth, limits, quotas, metrics', r'queryapigate/(ratelimit|limits|quotas|scope|metrics|usage|tracing|alerts)\.py'),
    ('history writer thread', r'queryapigate/(history|events)\.py'),
    ('app request handling', r'queryapigate/(app|v1|errors|secheaders|cors)\.py'),
    ('Flask / Werkzeug', r'\b(flask|werkzeug)[/\\]'),
    ('gunicorn, sockets, HTTP', r'\b(gunicorn|socket|selectors|ssl|http)[/\\.]'),
]
for build in (HEAD, CACHED):
    path = next((p for p in (DIR / f'profile_{build}.txt', DIR / f'profile_{build}.txt.gz') if p.exists()), None)
    if path is None:
        continue
    totals, frames_by, total = collections.Counter(), collections.Counter(), 0
    for line in (gzip.open(path, 'rt') if path.suffix == '.gz' else open(path)):
        stack, _, n = line.rstrip().rpartition(' ')
        if not n.isdigit():
            continue
        n, frames = int(n), stack.split(';')
        total += n
        cat = 'other'
        for frame in reversed(frames):
            hit = next((c for c, rx in RULES if re.search(rx, frame)), None)
            if hit:
                cat = hit
                frames_by[frame.strip()] += n
                break
        if any('history.py' in f for f in frames):
            cat = 'history writer thread'
        totals[cat] += n
    emit('', f'## b9: profile, {build} ({total:,} samples, threads on CPU only)', '')
    table('| category | share |')
    for cat, n in totals.most_common():
        emit(f'| {cat} | {n / total * 100:.1f}% |')
    emit('')
    table('| busiest attributed frames | share |')
    for frame, n in frames_by.most_common(12):
        emit(f'| `{frame}` | {n / total * 100:.1f}% |')

SRC.with_suffix('.md').write_text('\n'.join(out) + '\n')
print('\n'.join(out))

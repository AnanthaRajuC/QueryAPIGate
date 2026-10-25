#!/usr/bin/env python3
"""Tables from benchmarks/tpcc_mysql.py's results.jsonl: each cell the median of its rounds with the min-max range, and
a comparison that calls a difference real only when the two ranges don't overlap. Writes <results>.md beside it.

    python benchmarks/tpcc_mysql_report.py /tmp/qag-tpcc-bench/results.jsonl

The py-spy profiles (m10, .txt or .txt.gz) are read from the same folder.
"""
import collections
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
BUILDS = next((e['builds'] for e in env if e.get('builds')), ['v0.41.0', 'v0.42.0', 'main', 'main+cache'])
BASE, HEAD, CACHED = BUILDS[0], BUILDS[-2], BUILDS[-1]
QUERIES = ['customer', 'order_status', 'order_history', 'payments', 'profile', 'stock_level']
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

emit('', '## m1: MySQL on its own (sysbench)', '')
table('| test | threads | tps | p50 ms | p99 ms | n |')
for scenario in ['sysbench_oltp_read_only'] + [f'direct_{q}' for q in QUERIES]:
    for c in (1, 32):
        rs = sel(block='m1', scenario=scenario, c=c)
        if rs:
            emit(f"| {scenario} | {c} | {col(rs, 'tps')} | {col(rs, 'p50', 2)} | {col(rs, 'p99', 2)} | {len(rs)} |")


def direct(query, c, key):
    st = stat([r.get(key) for r in sel(block='m1', scenario=f'direct_{query}', c=c)])
    return st[0] if st else None


def build_table(block, queries, c, extra_filter=None, overhead=False):
    emit('', f'## {block}: c={c}' + (f' ({extra_filter})' if extra_filter else ''), '')
    head = ('| query | build | n | req/s | p50 ms | p99 ms | p99.9 ms | gateway cores | db cores | load cores | '
            f'MySQL stmts/req | vs {BASE} | vs {HEAD} |')
    if overhead:
        head += ' direct p50 ms | gateway adds (p50) |' if c == 1 else ' direct tps | gateway, % of direct |'
    table(head)
    for query in queries:
        stats = {}
        for b in BUILDS:
            rs = [r for r in sel(block=block, scenario=query, build=b, c=c)
                  if not extra_filter or all(r.get(k) == v for k, v in extra_filter.items())]
            if not rs:
                continue
            st = stats[b] = stat([r['rps'] for r in rs])
            line = (f"| {query} | {b} | {len(rs)} | {fmt(st)} | {col(rs, 'p50', 2)} | {col(rs, 'p99', 1)} | "
                    f"{col(rs, 'p99.9', 1)} | {med(rs, 'gw_cores')} | {med(rs, 'db_cores')} | "
                    f"{med(rs, 'load_cores')} | {med(rs, 'mysql_statements_per_req')} | "
                    f"{verdict(stats.get(BASE), st) if b != BASE else ''} | "
                    f"{verdict(stats.get(HEAD), st) if b == CACHED else ''} |")
            if overhead:
                if c == 1:
                    d = direct(query, 1, 'p50')
                    p50 = stat([r['p50'] for r in rs])[0]
                    line += f" {d if d is not None else '-'} | {f'+{p50 - d:.2f} ms' if d is not None else '-'} |"
                else:
                    d = direct(query, 32, 'tps')
                    line += f" {f'{d:,.0f}' if d else '-'} | {f'{st[0] / d * 100:.0f}%' if d else '-'} |"
            emit(line)


build_table('m2', QUERIES, 32, overhead=True)
build_table('m3', ['customer', 'order_status', 'stock_level'], 1, overhead=True)

emit('', '## m4: capacity (closed loop, c=32), then open loop on order_status, latency corrected', '')
for b in (HEAD, CACHED):
    rs = sel(block='m4cap', build=b)
    if rs:
        emit(f"- capacity, {b}: {col(rs, 'rps')} req/s over {len(rs)} rounds", '')
table('| build | offered (x capacity) | offered req/s | achieved req/s | p50 ms | p99 ms | p99.9 ms | ok | n |')
for b in (HEAD, CACHED):
    by = collections.defaultdict(list)
    for r in sel(block='m4', build=b):
        by[(r['offered_frac'], r['q'])].append(r)
    for (frac, q), rs in sorted(by.items()):
        emit(f"| {b} | {frac:.2f} | {q:,} | {col(rs, 'rps')} | {col(rs, 'p50', 2)} | {col(rs, 'p99', 1)} | "
             f"{col(rs, 'p99.9', 1)} | {min(r['success'] for r in rs):.4f} | {len(rs)} |")

emit('', '## m5: scale-out, c=64', '')
table('| query | workers | build | n | req/s | p50 ms | p99 ms | gateway cores | req/s per gateway core | db cores | '
      f'load cores | vs {HEAD} |')
for query in ('order_status', 'stock_level'):
    for w in (1, 2, 4, 8):
        base = None
        for b in (HEAD, CACHED):
            rs = sel(block='m5', scenario=query, build=b, workers=w)
            if not rs:
                continue
            st = stat([r['rps'] for r in rs])
            base = st if b == HEAD else base
            gw = statistics.median(r['gw_cores'] for r in rs)
            emit(f"| {query} | {w} | {b} | {len(rs)} | {fmt(st)} | {col(rs, 'p50', 1)} | {col(rs, 'p99', 1)} | "
                 f"{gw:.2f} | {st[0] / gw:,.0f} | {med(rs, 'db_cores')} | {med(rs, 'load_cores')} | "
                 f"{verdict(base, st) if b == CACHED else ''} |")

emit('', '## m6: threads per worker, c=32', '')
table('| query | threads | n | req/s | p50 ms | p99 ms | gateway cores |')
for query in ('customer', 'stock_level'):
    for t in (1, 2, 4, 8):
        rs = sel(block='m6', scenario=query, threads=t)
        if rs:
            emit(f"| {query} | {t} | {len(rs)} | {col(rs, 'rps')} | {col(rs, 'p50', 2)} | {col(rs, 'p99', 1)} | "
                 f"{med(rs, 'gw_cores')} |")

emit('', '## m7: uniform against Zipf-distributed customers, with and without a 30 s response cache', '')
table('| customers | query | n | req/s | p50 ms | p99 ms | cache hit rate | MySQL stmts/req |')
for dist in ('uniform', 'zipf'):
    for query in ('order_status', 'order_status_cached'):
        rs = sel(block='m7', scenario=query, dist=dist)
        if rs:
            emit(f"| {dist} | {query} | {len(rs)} | {col(rs, 'rps')} | {col(rs, 'p50', 2)} | {col(rs, 'p99', 1)} | "
                 f"{col(rs, 'cache_hit_rate', 2)} | {med(rs, 'mysql_statements_per_req')} |")

build_table('m8', ['order_status'], 1)
build_table('m8', ['order_status'], 32)
emit('', '## m8: the store, SQLite (local) against PostgreSQL (0.5 ms away), one caller, order_status', '')
table('| store | build | n | req/s | p50 ms | p99 ms |')
for store in ('sqlite', 'postgres'):
    for b in (HEAD, CACHED):
        rs = sel(block='m8store', store=store, build=b)
        if rs:
            emit(f"| {store} | {b} | {len(rs)} | {col(rs, 'rps')} | {col(rs, 'p50', 2)} | {col(rs, 'p99', 1)} |")

emit('', '## m9: round trips per call', '')
table('| build | statements per call (general log, 100 calls) | of which |')
for r in sel(block='m9', scenario='statements'):
    top = '; '.join(f'{n / r["calls"]:.2f} × `{s}`' for s, n in sorted(r['statements'].items(), key=lambda x: -x[1]))
    emit(f"| {r['build']} | {r['per_call']:.2f} | {top} |")


def slope(points):
    n = len(points)
    mx, my = sum(x for x, _ in points) / n, sum(y for _, y in points) / n
    k = sum((x - mx) * (y - my) for x, y in points) / sum((x - mx) ** 2 for x, _ in points)
    return k, my - k * mx


m9 = [r for r in sel(block='m9') if 'netem' in r]
if m9:
    delays = sorted({r['netem'] for r in m9}, key=lambda d: float(d[:-2]))
    emit('', 'Single-call p50 (ms) against netem delay; the slope is round trips per call:', '')
    table('| | ' + ' | '.join(f'+{d}' for d in delays) + ' | slope | at 0 delay |')
    series = [(f'mysql-connector alone, {m}', [r for r in m9 if r['scenario'] == f'probe_{m}'], 'ms_per_call')
              for m in ('one', 'two')] + [(b, [r for r in m9 if r.get('build') == b], 'p50') for b in BUILDS]
    for label, rs, key in series:
        if rs:
            cells = [stat([r[key] for r in rs if r['netem'] == d]) for d in delays]
            k, c0 = slope([(float(r['netem'][:-2]), r[key]) for r in rs])
            emit(f"| {label} | " + ' | '.join(fmt(c, 2) for c in cells) + f' | {k:.2f} | {c0:.2f} ms |')

RULES = [  # leaf-to-root: the first frame that matches names the category
    ('logging', r'\blogging[/\\]|logging_setup\.py|accesslog\.py|gunicorn/glogging'),
    ('JSON', r'\bjson[/\\]|formats\.py'),
    ('store layer: queries, decoding, checks', r'queryapigate/(db|store|apikeys|metacache|admins)\.py'),
    ('data query: driver and pool', r'queryapigate/(runners|engine|pool)\.py|mysql[/\\]connector'),
    ('SQL checks, parameters', r'queryapigate/(sqltools|masking|tableguard|params|governance)\.py|sqlglot'),
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
    emit('', f'## m10: profile, {build} ({total:,} samples, threads on CPU only)', '')
    table('| category | share |')
    for cat, n in totals.most_common():
        emit(f'| {cat} | {n / total * 100:.1f}% |')
    emit('')
    table('| busiest attributed frames | share |')
    for frame, n in frames_by.most_common(12):
        emit(f'| `{frame}` | {n / total * 100:.1f}% |')

emit('', '## m11: the table allow-list across databases (a key allowed `customer`)', '')
table('| SQL | status | code |')
for r in sel(block='m11'):
    emit(f"| `{r['sql']}` | {r['status']} | {r['code'] or ''} |")

SRC.with_suffix('.md').write_text('\n'.join(out) + '\n')
print('\n'.join(out))

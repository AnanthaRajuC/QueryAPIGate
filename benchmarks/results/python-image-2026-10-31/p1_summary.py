"""ADR 0031's measurement, oltp.py block p1: the base ref's image (the official python image) against the head's
(Debian's Python), per mode and scenario - medians over the rounds, and the head's ratio paired round by round.

    python p1_summary.py results.jsonl [BASE]      BASE: the base build's label (oltp.py's --base, last part), main
"""
import collections
import json
import statistics
import sys

MODES = {'+imgpy+cache': 'Python alone', '+img+cache': 'Data plane in front'}
rows = collections.defaultdict(list)
for line in open(sys.argv[1]):
    run = json.loads(line)
    if run.get('block') != 'p1' or '+img' not in run.get('build', ''):  # not the identity rows or /health floors
        continue
    side, mode = run['build'].split('+', 1)
    rows[(MODES['+' + mode], run['scenario'], side)].append(run)

first = sys.argv[2] if len(sys.argv) > 2 else 'main'
sides = sorted({side for _, _, side in rows}, key=lambda s: s != first)  # the base first
base, head = sides
metrics = [('rps', 'calls/s', 0), ('per_core', 'calls/s per gateway core', 0), ('py_cores', 'Python cores', 2),
           ('dp_cores', 'data plane cores', 2), ('p99', 'p99 ms', 2), ('runs_written_share', 'runs recorded', 3)]


def value(run, key):
    return run['rps'] / run['gw_cores'] if key == 'per_core' else run[key]


for (mode, scenario) in dict.fromkeys((m, s) for m, s, _ in rows):
    b, h = rows[(mode, scenario, base)], rows[(mode, scenario, head)]
    print(f'\n{mode}, {scenario} ({len(b)} and {len(h)} rounds)')
    for key, label, digits in metrics:
        if key == 'dp_cores' and mode == 'Python alone':
            continue
        bv, hv = [value(r, key) for r in b], [value(r, key) for r in h]
        by_round = zip(sorted(b, key=lambda r: r['round']), sorted(h, key=lambda r: r['round']), strict=True)
        paired = [value(y, key) / value(x, key) for x, y in by_round if value(x, key)]
        print(f'  {label:26} {statistics.median(bv):>10.{digits}f} {statistics.median(hv):>10.{digits}f}'
              f'   head/base {statistics.median(paired):.3f} ({min(paired):.3f}-{max(paired):.3f})')

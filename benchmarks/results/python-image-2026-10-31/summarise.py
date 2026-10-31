"""The tables in README.md from a screen's lines: each candidate's median, range and ratio to the pinned image.

    python summarise.py screen-candidates.jsonl
"""
import collections
import json
import statistics
import sys

BASE = 'py104:official-3.12-pinned'
times = collections.defaultdict(list)
temps = []
with open(sys.argv[1]) as f:
    for line in f:
        run = json.loads(line)
        times[(run['cand'], run['work'])].append(run['ms'])
        temps.append(run['temp_mC'] / 1000)
base = {work: statistics.median(times[(BASE, work)]) for work in ('interpreter', 'loop')}
for cand in dict.fromkeys(cand for cand, _ in times):
    cells = []
    for work in ('interpreter', 'loop'):
        ms = times[(cand, work)]
        median = statistics.median(ms)
        cells.append(f'{median:.1f} ms ({min(ms):.1f}-{max(ms):.1f}) x{median / base[work]:.2f}')
    print(f'{cand:32} {cells[0]:32} {cells[1]}')
print(f'thermal: {min(temps):.0f}-{max(temps):.0f} C')

"""Pure Python, no library: dicts, attribute-free arithmetic, string formatting. Median of 9 passes, in ms."""
import statistics
import time


def once():
    started = time.perf_counter()
    total = 0
    for i in range(400_000):
        d = {'a': i, 'b': i * 2}
        total += d['a'] + d['b']
        if i % 1000 == 0:
            total += len(f'{i}-{total}')
    return time.perf_counter() - started


once()
print(round(statistics.median(once() for _ in range(9)) * 1000, 1), 'ms')

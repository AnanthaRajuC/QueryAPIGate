"""The interpreter alone (ADR 0030's measurement): batches of reported runs parsed and re-encoded, as Python's
recording does, pinned to one core - no QueryAPIGate, no database, no network. Prints the median of 9 passes.

    taskset -c 2 python interpreter.py
    docker run --rm --cpuset-cpus 2 -v "$PWD/interpreter.py:/m.py:ro" --entrypoint python <image> /m.py
"""
import json
import statistics
import time

entry = {'executed_at': '2026-10-30 12:00:00', 'connection_name': 'oltp', 'request_id': 'abcdef123456',
         'key_name': 'plain', 'transport': 'rest', 'status': 'success', 'rows': 1, 'duration_ms': 1,
         'serialization_ms': 0.1}
run = {'query': 'account', 'version': 1, 'entry': json.dumps(entry), 'executed_at': entry['executed_at'],
       'status': 'success', 'key': 'plain', 'connection': 'oltp', 'transport': 'rest', 'rows': 1, 'dialect': 'postgres',
       'method': 'GET', 'http_status': 200, 'seconds': 0.002, 'query_seconds': 0.001, 'serialization_seconds': 0.0001,
       'code': None, 'masked': False}
body = json.dumps({'runs': [run] * 500})


def once():
    started = time.perf_counter()
    for _ in range(40):
        for reported in json.loads(body)['runs']:
            parsed = json.loads(reported['entry'])
            key = (reported['query'], reported['version'], reported['key'], reported['connection'], reported['status'])
            json.dumps({**parsed, 'query': key[0]})
    return time.perf_counter() - started


once()
print(round(statistics.median(once() for _ in range(9)) * 1000, 1), 'ms')

"""Logs the garbage collector's slower collections (generations 1 and 2, or any over 1 ms) with their wall-clock time,
for lining up against slow calls. Loaded into the gunicorn worker through PYTHONPATH; QAG_GC_LOG names the file."""
import gc
import os
import time

_path = os.environ.get('QAG_GC_LOG')
if _path:
    _started = [0.0, 0.0]

    def _callback(phase, info):
        if phase == 'start':
            _started[0], _started[1] = time.time(), time.perf_counter()
            return
        ms = (time.perf_counter() - _started[1]) * 1000
        if info['generation'] >= 1 or ms > 1:
            with open(f'{_path}.{os.getpid()}', 'a') as f:
                f.write(f"{_started[0]:.4f} {info['generation']} {ms:.2f} {info['collected']}\n")

    gc.callbacks.append(_callback)

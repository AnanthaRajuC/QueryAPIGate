"""SIGTERM - what Docker, Kubernetes, systemd and `kill` send - ends each long-running command cleanly: it exits 0, and
what was still queued at that moment is written. Run history is the check: its writer batches runs in the background
(history.py), so with a long flush interval only the write on the way out can save the runs these tests make.

`queryapigate mcp` and `queryapigate events` used to end at once, by the signal itself (exit -15), and lost them: the
runs an agent had just made, the re-runs a live query had just sent. `queryapigate serve` already exited cleanly; it is
here so it stays that way. Each test starts the real command, as a separate process, on a SQLite home of its own -
how the process ends doesn't depend on the store."""
import json
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request

try:
    import mcp  # noqa: F401
    HAVE_MCP_SDK = True
except ImportError:
    HAVE_MCP_SDK = False

from tests.test_multinode import call, eventually, free_port

ADMIN = 'shutdown-admin-key'
CALLS = 3


class SigtermTests(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = tmp.name
        # Nothing from the environment the suite runs in (a PostgreSQL store, Redis, a run store): this home only.
        self.env = {**{k: v for k, v in os.environ.items() if not k.startswith('QUERYAPIGATE_')},
                    'QUERYAPIGATE_HOME': self.home, 'QUERYAPIGATE_API_KEY': ADMIN,
                    'QUERYAPIGATE_HISTORY_FLUSH_INTERVAL': '60', 'QUERYAPIGATE_ACCESS_LOG': '0'}
        loaded = subprocess.run([sys.executable, '-m', 'queryapigate', 'examples', 'load'], env=self.env,
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(loaded.returncode, 0, loaded.stderr)
        self.key = next(line.split(': ', 1)[1].strip() for line in loaded.stdout.splitlines()
                        if line.strip().startswith('example-reporting:'))
        self.seeded = self.runs()  # `examples load` seeds sample history, with no transport

    def start(self, *args, env=None):
        port = free_port()
        process = subprocess.Popen([sys.executable, '-m', 'queryapigate', *args, '--port', str(port)],
                                   env={**self.env, **(env or {})}, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.addCleanup(self.stop_if_running, process)
        base = f'http://127.0.0.1:{port}'
        if not eventually(lambda: self.healthy(base), timeout=60):
            self.fail(f'{args[0]} did not start: {self.output(process)}')
        return process, base

    @staticmethod
    def healthy(base):
        try:
            with urllib.request.urlopen(base + '/health', timeout=2) as res:
                return res.status == 200
        except OSError:
            return False

    @staticmethod
    def stop_if_running(process):
        if process.poll() is None:
            process.kill()
            process.wait()
        process.stdout.close()

    @staticmethod
    def output(process):
        if process.poll() is None:
            process.kill()
            process.wait()
        return process.stdout.read().decode(errors='replace')[-3000:]

    def terminate(self, process):
        """SIGTERM, and how the process ended - the call fails if it takes longer than a clean stop should."""
        started = time.monotonic()
        process.send_signal(signal.SIGTERM)
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.fail(f'still running 30 s after SIGTERM: {self.output(process)}')
        self.assertEqual(process.returncode, 0, self.output(process))
        return time.monotonic() - started

    def runs(self, transport=None):
        conn = sqlite3.connect(os.path.join(self.home, 'queryapigate.db'))
        try:
            rows = conn.execute("SELECT json_extract(entry_json, '$.transport') FROM execution_history").fetchall()
        finally:
            conn.close()
        return sum(1 for (t,) in rows if t == transport) if transport else len(rows)

    @unittest.skipUnless(HAVE_MCP_SDK, 'queryapigate[mcp] not installed')
    def test_mcp_writes_its_last_runs_and_exits_0(self):
        process, base = self.start('mcp')
        for i in range(CALLS):  # a tool call, as an agent makes it (stateless: no session to set up first)
            body = json.dumps({'jsonrpc': '2.0', 'id': i, 'method': 'tools/call',
                               'params': {'name': 'example_top_films', 'arguments': {}}}).encode()
            request = urllib.request.Request(base + '/mcp/', data=body, method='POST', headers={
                'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream',
                'X-API-Key': self.key})
            with urllib.request.urlopen(request, timeout=30) as res:
                self.assertEqual(res.status, 200)
                self.assertNotIn('"isError":true', res.read().decode().replace(' ', ''))
        self.terminate(process)
        self.assertEqual(self.runs('mcp'), CALLS)

    def test_events_writes_live_queries_last_runs_ends_open_streams_and_exits_0(self):
        process, base = self.start('events', env={'QUERYAPIGATE_LIVE_QUERIES': '1'})
        streams = []
        for _ in range(CALLS):  # one caller, one query: three streams sharing one group, so one run (live.py)
            res = urllib.request.urlopen(urllib.request.Request(base + '/live/example_top_films',
                                                                headers={'X-API-Key': self.key}), timeout=30)
            self.addCleanup(res.close)
            while not res.readline().startswith(b'data:'):
                pass
            streams.append(res)
        took = self.terminate(process)  # with every stream still open
        self.assertLess(took, 10)
        for res in streams:  # each was ended by the server - read() would time out on one left open
            self.assertNotIn(b'data:', res.read())
        self.assertEqual(self.runs('live'), 1)

    def test_serve_writes_its_last_runs_and_exits_0(self):
        process, base = self.start('serve')
        for _ in range(CALLS):
            status, body, _ = call(base, 'GET', '/q/example_top_films', key=self.key)
            self.assertEqual(status, 200, body)
        self.terminate(process)
        self.assertEqual(self.runs('rest'), CALLS)
        self.assertEqual(self.runs(), self.seeded + CALLS)


if __name__ == '__main__':
    unittest.main()

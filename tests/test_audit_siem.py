"""The audit log's stream against real receivers (ADR 0019): rsyslog and syslog-ng taking RFC 5425 syslog over mutual
TLS, and an OpenTelemetry Collector taking OTLP/HTTP logs - started by .github/scripts/siem_receivers.sh, which
QUERYAPIGATE_TEST_SIEM names the directory of. Each test reads what the receiver itself parsed and wrote, never our own
parse: MSGID, structured data, the message - and checks that copy with `audit verify --file`, as an auditor would.

CI's integration job runs these with every receiver up, and fails if any test skips."""
import json
import os
import subprocess
import tempfile
import time
import unittest
import uuid
from unittest import mock

from queryapigate import auditchain, auditstream, db, store

SIEM = os.environ.get('QUERYAPIGATE_TEST_SIEM')
RECEIVERS = json.load(open(os.path.join(SIEM, 'receivers.json'))) if SIEM else {}
SKIP = 'set QUERYAPIGATE_TEST_SIEM to the directory .github/scripts/siem_receivers.sh started'


def _docker(*args):
    subprocess.run(['docker', *args], check=True, capture_output=True, timeout=120)


class SiemTestCase(unittest.TestCase):
    receiver = None   # 'rsyslog', 'syslog-ng' or 'otelcol'

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.marker = uuid.uuid4().hex[:12]   # every receiver's file is shared between tests: ours are marked
        certs = RECEIVERS['certs']
        env = {'QUERYAPIGATE_HOME': self.tmp.name, 'QUERYAPIGATE_AUDIT_KEY': '', 'QUERYAPIGATE_AUDIT_LOG_LIMIT': '500',
               'QUERYAPIGATE_AUDIT_SYSLOG_URL': '', 'QUERYAPIGATE_AUDIT_OTLP': '',
               'QUERYAPIGATE_AUDIT_SYSLOG_CA': f'{certs}/ca.pem',
               'QUERYAPIGATE_AUDIT_SYSLOG_CERT': f'{certs}/client.pem',
               'QUERYAPIGATE_AUDIT_SYSLOG_KEY': f'{certs}/client.key'}
        if self.receiver == 'otelcol':
            env.update(QUERYAPIGATE_AUDIT_OTLP='1', OTEL_EXPORTER_OTLP_ENDPOINT=RECEIVERS['otelcol']['url'],
                       OTEL_EXPORTER_OTLP_LOGS_ENDPOINT='', OTEL_EXPORTER_OTLP_HEADERS='')
        else:
            env['QUERYAPIGATE_AUDIT_SYSLOG_URL'] = RECEIVERS[self.receiver]['url']
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        auditstream.AUTO_START = False
        self.addCleanup(setattr, auditstream, 'AUTO_START', True)
        self.addCleanup(self._close_senders)
        self._close_senders()
        db.init_schema()

    @staticmethod
    def _close_senders():
        for sender in auditstream._senders.values():
            sender.close()
        auditstream._senders.clear()

    @property
    def output(self):
        return 'otlp' if self.receiver == 'otelcol' else 'syslog'

    def write(self, count, start=0):
        for i in range(start, start + count):
            store.record_audit('alice', 'update_key', f'{self.marker}-key-{i}', {'n': i})

    def deliver(self):
        return auditstream.deliver_once(self.output)

    def received(self, count, timeout=30.0):
        """What the receiver wrote of this test's entries, once it has `count` - [(app, msgid, sd, entry)] for syslog,
        [(record attributes, entry)] for the Collector."""
        deadline = time.monotonic() + timeout
        found = []
        while time.monotonic() < deadline:
            found = self._read()
            if len(found) >= count:
                return found
            time.sleep(0.25)
        self.fail(f'{self.receiver} wrote {len(found)} of {count} entries in {timeout:.0f}s')

    def _read(self):
        path = RECEIVERS[self.receiver]['out']
        if not os.path.exists(path):
            return []
        with open(path, encoding='utf-8') as f:
            lines = f.read().splitlines()
        if self.receiver == 'otelcol':
            records = []
            for line in lines:
                for resource in json.loads(line)['resourceLogs']:
                    for scope in resource['scopeLogs']:
                        for record in scope['logRecords']:
                            entry = json.loads(record['body']['stringValue'])
                            if entry['target'].startswith(self.marker):
                                attributes = {a['key']: next(iter(a['value'].values())) for a in record['attributes']}
                                records.append((attributes, entry))
            return records
        rows = []
        for line in lines:
            app, msgid, sd, message = line.split('\t', 3)
            entry = json.loads(message.lstrip('﻿ '))
            if entry['target'].startswith(self.marker):
                rows.append((app, msgid, sd, entry))
        return rows

    def copy_as_received(self, rows, edit=None):
        """The receiver's copy of this test's entries, as a file `audit verify --file` reads - one entry a line."""
        path = os.path.join(self.tmp.name, f'{self.receiver}-copy.jsonl')
        with open(path, 'w') as f:
            for row in rows:
                entry = dict(row[-1])
                if edit is not None and entry['seq'] == edit:
                    entry['changes'] = {'n': 'forged'}
                f.write(json.dumps(entry) + '\n')
        return path


class SyslogReceiverTests:
    """For rsyslog and syslog-ng alike."""

    def test_every_entry_arrives_in_order_parsed_as_meant(self):
        self.write(5)
        self.assertEqual(self.deliver(), 5)
        rows = self.received(5)
        self.assertEqual([r[3]['seq'] for r in rows], [1, 2, 3, 4, 5])
        for app, msgid, sd, entry in rows:
            self.assertEqual(app, 'queryapigate')
            self.assertEqual(msgid, 'update_key')    # the action, as the receiver parsed it
            self.assertEqual(sd, f'[meta sequenceId="{entry["seq"]}"]')
        stored = [r[0] for r in db.connection().execute('SELECT hash FROM audit_log ORDER BY seq')]
        self.assertEqual([r[3]['hash'] for r in rows], stored)

    def test_its_copy_verifies_and_an_edit_in_it_is_found(self):
        self.write(4)
        self.deliver()
        rows = self.received(4)
        report = auditchain.verify_file(self.copy_as_received(rows))
        self.assertEqual((report['intact'], report['first_seq'], report['last_seq']), (True, 1, 4), report)
        broken = auditchain.verify_file(self.copy_as_received(rows, edit=3))
        self.assertEqual((broken['break']['problem'], broken['break']['seq']), ('changed', 3))

    def test_without_a_client_certificate_it_refuses_and_nothing_is_lost(self):
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_AUDIT_SYSLOG_CERT': '', 'QUERYAPIGATE_AUDIT_SYSLOG_KEY': ''}):
            self.write(2)
            with self.assertRaises(OSError) as caught:
                self.deliver()
        self.assertIn('CERTIFICATE_REQUIRED', str(caught.exception))
        [status] = auditstream.status()
        self.assertEqual((status['delivered_seq'], status['behind']), (0, 2))
        self.assertIn('CERTIFICATE_REQUIRED', status['last_error'])
        self._close_senders()
        self.assertEqual(self.deliver(), 2)   # with its certificate again: both, nothing lost
        self.assertEqual([r[3]['seq'] for r in self.received(2)], [1, 2])

    def test_a_client_certificate_from_another_ca_is_refused(self):
        certs = RECEIVERS['certs']
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_AUDIT_SYSLOG_CERT': f'{certs}/stranger.pem',
                                          'QUERYAPIGATE_AUDIT_SYSLOG_KEY': f'{certs}/stranger.key'}):
            self.write(1)
            with self.assertRaises(OSError) as caught:
                self.deliver()
        self.assertIn('UNKNOWN_CA', str(caught.exception))
        self.assertEqual(auditstream.status()[0]['delivered_seq'], 0)

    def test_an_outage_loses_nothing(self):
        container = RECEIVERS[self.receiver]['container']
        self.write(2)
        self.assertEqual(self.deliver(), 2)
        self.received(2)
        _docker('stop', container)   # QueryAPIGate's connection to it still open: noticed, not written into
        try:
            self.write(3, start=2)
            with self.assertRaises(OSError):
                self.deliver()
            [status] = auditstream.status()
            self.assertEqual((status['delivered_seq'], status['behind']), (2, 3))
        finally:
            _docker('start', container)
        deadline = time.monotonic() + 60
        while True:   # until it listens again
            try:
                delivered = self.deliver()
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(1)
        self.assertEqual(delivered, 3)
        rows = self.received(5)
        self.assertEqual(sorted({r[3]['seq'] for r in rows}), [1, 2, 3, 4, 5])
        self.assertTrue(auditchain.verify_file(self.copy_as_received(rows))['intact'])


@unittest.skipUnless(SIEM, SKIP)
class RsyslogTests(SyslogReceiverTests, SiemTestCase):
    receiver = 'rsyslog'


@unittest.skipUnless(SIEM, SKIP)
class SyslogNgTests(SyslogReceiverTests, SiemTestCase):
    receiver = 'syslog-ng'


@unittest.skipUnless(SIEM, SKIP)
class OtelCollectorTests(SiemTestCase):
    receiver = 'otelcol'

    def test_every_entry_arrives_as_a_log_record_with_its_attributes(self):
        self.write(3)
        self.assertEqual(self.deliver(), 3)
        records = self.received(3)
        self.assertEqual([e['seq'] for _a, e in records], [1, 2, 3])
        for attributes, entry in records:
            self.assertEqual(int(attributes['queryapigate.audit.seq']), entry['seq'])
            self.assertEqual(attributes['queryapigate.audit.hash'], entry['hash'])
            self.assertEqual(attributes['queryapigate.audit.action'], 'update_key')
            self.assertEqual(attributes['queryapigate.audit.actor'], 'alice')
        self.assertTrue(auditchain.verify_file(self.copy_as_received(records))['intact'])

    def test_an_outage_loses_nothing(self):
        container = RECEIVERS['otelcol']['container']
        _docker('stop', container)
        try:
            self.write(2)
            with self.assertRaises(OSError):
                self.deliver()
            self.assertEqual(auditstream.status()[0]['delivered_seq'], 0)
        finally:
            _docker('start', container)
        deadline = time.monotonic() + 60
        while True:
            try:
                delivered = self.deliver()
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(1)
        self.assertEqual(delivered, 2)
        self.assertEqual([e['seq'] for _a, e in self.received(2)], [1, 2])


if __name__ == '__main__':
    unittest.main()

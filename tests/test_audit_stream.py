"""The audit log streamed to a SIEM (ADR 0012, BACKLOG #96): syslog over TLS (and mutual TLS) to a real receiver,
OTLP logs, a cursor and a lease in the store, at-least-once delivery, entries kept past the cap until delivered."""
import datetime
import http.server
import ipaddress
import json
import os
import re
import socket
import ssl
import tempfile
import threading
import time
import unittest
from unittest import mock

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from queryapigate import alerts, auditchain, auditstream, config, create_app, db, store

ADMIN = {'X-API-Key': 'admin-key'}


def _cert(name, issuer=None, issuer_key=None, ca=False, server=False):
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.datetime.now(datetime.timezone.utc)
    builder = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)]))
               .issuer_name(issuer or x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)]))
               .public_key(key.public_key()).serial_number(x509.random_serial_number())
               .not_valid_before(now - datetime.timedelta(minutes=5)).not_valid_after(now + datetime.timedelta(days=1))
               .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True))
    if server:
        builder = builder.add_extension(x509.SubjectAlternativeName(
            [x509.DNSName('localhost'), x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]), critical=False)
    return key, builder.sign(issuer_key or key, hashes.SHA256())


def _write_pem(folder, name, key, cert):
    cert_path, key_path = os.path.join(folder, f'{name}.crt'), os.path.join(folder, f'{name}.key')
    with open(cert_path, 'wb') as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(key_path, 'wb') as f:
        f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption()))
    return cert_path, key_path


class Receiver:
    """A syslog receiver over TLS (RFC 5425): reads octet-counted frames into `messages`."""

    def __init__(self, certs, port=0, require_client_cert=False):
        self.messages, self.certs = [], certs
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certs['server'][0], certs['server'][1])
        if require_client_cert:
            context.verify_mode = ssl.CERT_REQUIRED
            context.load_verify_locations(certs['ca'])
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(('127.0.0.1', port))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self.context, self.running, self.open = context, True, []
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while self.running:
            try:
                raw, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._read, args=(raw,), daemon=True).start()

    def _read(self, raw):
        try:
            conn = self.context.wrap_socket(raw, server_side=True)
        except (ssl.SSLError, OSError):
            raw.close()
            return
        self.open.append(conn)
        buffer = b''
        with conn:
            while self.running:
                try:
                    chunk = conn.recv(65536)
                except (ssl.SSLError, OSError):
                    return
                if not chunk:
                    return
                buffer += chunk
                while b' ' in buffer:
                    length, _, rest = buffer.partition(b' ')
                    if len(rest) < int(length):
                        break
                    self.messages.append(rest[:int(length)])
                    buffer = rest[int(length):]

    def stop(self):
        """The SIEM gone: no new connections, and the open ones closed from this end."""
        self.running = False
        try:
            self.sock.shutdown(socket.SHUT_RDWR)  # wakes the accept() blocked in the other thread
        except OSError:
            pass
        self.sock.close()
        for conn in self.open:
            try:
                conn.shutdown(socket.SHUT_RDWR)
                conn.close()
            except OSError:
                pass
        time.sleep(0.05)

    def entries(self):
        return [json.loads(m.split(b'\xef\xbb\xbf', 1)[1]) for m in self.messages]

    def wait_for(self, count, timeout=5):
        deadline = time.monotonic() + timeout
        while len(self.messages) < count and time.monotonic() < deadline:
            time.sleep(0.02)
        return len(self.messages)


class StreamTestCase(unittest.TestCase):
    limit = '500'

    @classmethod
    def setUpClass(cls):
        cls.pki = tempfile.TemporaryDirectory()
        ca_key, ca = _cert('test CA', ca=True)
        server_key, server = _cert('localhost', ca.subject, ca_key, server=True)
        client_key, client = _cert('queryapigate', ca.subject, ca_key)
        cls.certs = {'ca': _write_pem(cls.pki.name, 'ca', ca_key, ca)[0],
                     'server': _write_pem(cls.pki.name, 'server', server_key, server),
                     'client': _write_pem(cls.pki.name, 'client', client_key, client)}

    @classmethod
    def tearDownClass(cls):
        cls.pki.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.receiver = Receiver(self.certs)
        self.addCleanup(lambda: self.receiver.stop())
        self.env = {'QUERYAPIGATE_HOME': self.tmp.name, 'QUERYAPIGATE_API_KEY': 'admin-key',
                    'QUERYAPIGATE_AUDIT_KEY': '', 'QUERYAPIGATE_AUDIT_LOG_LIMIT': self.limit,
                    'QUERYAPIGATE_AUDIT_SYSLOG_URL': f'tls://localhost:{self.receiver.port}',
                    'QUERYAPIGATE_AUDIT_SYSLOG_CA': self.certs['ca'], 'QUERYAPIGATE_AUDIT_SYSLOG_CERT': '',
                    'QUERYAPIGATE_AUDIT_SYSLOG_KEY': '', 'QUERYAPIGATE_AUDIT_OTLP': ''}
        patcher = mock.patch.dict(os.environ, self.env)
        patcher.start()
        self.addCleanup(patcher.stop)
        auditstream.AUTO_START = False
        self.addCleanup(setattr, auditstream, 'AUTO_START', True)
        for sender in auditstream._senders.values():
            sender.close()
        auditstream._senders.clear()
        db.init_schema()
        with db.transaction() as conn:
            for table in ('audit_log', 'audit_chain', 'audit_delivery'):
                conn.execute(f'DELETE FROM {table}')

    def write(self, count, start=0):
        for i in range(start, start + count):
            store.record_audit('alice', 'update_key', f'key-{i}', {'n': i})

    def cursor(self, output='syslog'):
        row = db.connection().execute('SELECT last_seq FROM audit_delivery WHERE output = ?', (output,)).fetchone()
        return row[0] if row else None


class SyslogTests(StreamTestCase):
    def test_entries_reach_the_receiver_in_order_as_rfc_5424(self):
        self.write(3)
        self.assertEqual(auditstream.deliver_once('syslog'), 3)
        self.receiver.wait_for(3)
        first = self.receiver.messages[0]
        self.assertRegex(first, rb'^<110>1 \S+ \S+ queryapigate \d+ update_key \[meta sequenceId="1"\] \xef\xbb\xbf\{')
        entries = self.receiver.entries()
        self.assertEqual([(e['seq'], e['target']) for e in entries], [(1, 'key-0'), (2, 'key-1'), (3, 'key-2')])
        stored = db.connection().execute('SELECT hash FROM audit_log ORDER BY seq').fetchall()
        self.assertEqual([e['hash'] for e in entries], [r[0] for r in stored])
        self.assertEqual(self.cursor(), 3)
        self.assertEqual(auditstream.deliver_once('syslog'), 0)  # nothing new: nothing sent again

    def test_while_the_siem_is_down_entries_wait_past_the_cap_and_then_all_arrive(self):
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_AUDIT_LOG_LIMIT': '3'}):
            self.write(2)
            auditstream.deliver_once('syslog')
            port = self.receiver.port
            self.receiver.stop()
            self.write(6, start=2)
            with self.assertRaises(OSError):
                auditstream.deliver_once('syslog')
            kept = [r[0] for r in db.connection().execute('SELECT seq FROM audit_log ORDER BY seq').fetchall()]
            self.assertEqual(kept, [3, 4, 5, 6, 7, 8])  # past the cap of 3: every one not delivered yet
            status = auditstream.status()[0]
            self.assertEqual((status['delivered_seq'], status['behind']), (2, 6))
            self.assertRegex(status['last_error'], r'Connection\w*Error')
            self.receiver = Receiver(self.certs, port=port)
            self.assertEqual(auditstream.deliver_once('syslog'), 6)
            self.receiver.wait_for(6)
            self.assertEqual([e['seq'] for e in self.receiver.entries()], [3, 4, 5, 6, 7, 8])
            self.write(1, start=8)  # delivered: the cap applies again
            self.assertEqual(db.connection().execute('SELECT COUNT(*) FROM audit_log').fetchone()[0], 3)
            self.assertTrue(auditchain.verify(db.connection())['intact'])

    def test_the_backlog_is_bounded_at_ten_times_the_cap(self):
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_AUDIT_LOG_LIMIT': '2'}):
            self.write(1)
            auditstream.deliver_once('syslog')
            self.receiver.stop()
            self.write(30, start=1)
            self.assertEqual(db.connection().execute('SELECT COUNT(*) FROM audit_log').fetchone()[0], 20)

    def test_mutual_tls(self):
        self.receiver.stop()
        self.receiver = Receiver(self.certs, require_client_cert=True)
        url = f'tls://localhost:{self.receiver.port}'
        self.write(1)
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_AUDIT_SYSLOG_URL': url}):
            with self.assertRaises(OSError):  # no client certificate
                auditstream.deliver_once('syslog')
            auditstream._senders.clear()
            with mock.patch.dict(os.environ, {'QUERYAPIGATE_AUDIT_SYSLOG_CERT': self.certs['client'][0],
                                              'QUERYAPIGATE_AUDIT_SYSLOG_KEY': self.certs['client'][1]}):
                self.assertEqual(auditstream.deliver_once('syslog'), 1)
        self.assertEqual(self.receiver.wait_for(1), 1)

    def test_an_untrusted_server_certificate_is_refused(self):
        self.write(1)
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_AUDIT_SYSLOG_CA': ''}), self.assertRaises(ssl.SSLError):
            auditstream.deliver_once('syslog')
        self.assertEqual(self.cursor(), 0)


class LeaseTests(StreamTestCase):
    def test_one_instance_delivers_and_another_takes_over_when_its_lease_lapses(self):
        self.write(2)
        self.assertEqual(auditstream.deliver_once('syslog', me='one'), 2)
        self.write(1, start=2)
        self.assertIsNone(auditstream.deliver_once('syslog', me='two'))  # one's lease holds
        with db.transaction() as conn:
            conn.execute('UPDATE audit_delivery SET lease_until = ?', (time.time() - 1,))  # one is gone
        self.assertEqual(auditstream.deliver_once('syslog', me='two'), 1)
        self.receiver.wait_for(3)
        self.assertEqual([e['seq'] for e in self.receiver.entries()], [1, 2, 3])

    def test_a_batch_that_fails_partway_is_sent_again_at_least_once(self):
        self.write(3)
        sent = []
        real = auditstream._Syslog.send

        def flaky(sender, entries):
            sent.append([e['seq'] for e in entries])
            real(sender, entries[:1])  # the first reaches the SIEM, then the connection drops
            raise ConnectionResetError('connection reset')

        with mock.patch.object(auditstream._Syslog, 'send', flaky), self.assertRaises(ConnectionResetError):
            auditstream.deliver_once('syslog')
        self.assertEqual(self.cursor(), 0)  # not moved: the batch counts as undelivered
        self.assertEqual(auditstream.deliver_once('syslog'), 3)
        self.receiver.wait_for(4)
        seqs = [e['seq'] for e in self.receiver.entries()]
        self.assertEqual(seqs, [1, 1, 2, 3])  # one repeat, nothing missing - the SIEM drops it by seq and hash


class _Collector(http.server.BaseHTTPRequestHandler):
    received: list = []
    answer = 200

    def do_POST(self):  # noqa: N802
        body = self.rfile.read(int(self.headers['Content-Length']))
        _Collector.received.append((self.path, {k.lower(): v for k, v in self.headers.items()}, body))
        self.send_response(_Collector.answer)
        self.end_headers()

    def log_message(self, *args):
        pass


class OtlpTests(StreamTestCase):
    def test_entries_as_otlp_log_records_and_a_refusal_keeps_them(self):
        server = http.server.HTTPServer(('127.0.0.1', 0), _Collector)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        _Collector.received, _Collector.answer = [], 503
        with mock.patch.dict(os.environ, {'QUERYAPIGATE_AUDIT_OTLP': '1', 'QUERYAPIGATE_AUDIT_SYSLOG_URL': '',
                                          'OTEL_EXPORTER_OTLP_ENDPOINT': f'http://127.0.0.1:{server.server_port}',
                                          'OTEL_EXPORTER_OTLP_HEADERS': 'x-token=abc%20def'}):
            self.write(2)
            with self.assertRaises(OSError):
                auditstream.deliver_once('otlp')
            self.assertEqual(self.cursor('otlp'), 0)
            _Collector.answer = 200
            self.assertEqual(auditstream.deliver_once('otlp'), 2)
        path, headers, body = _Collector.received[-1]
        self.assertEqual((path, headers['x-token']), ('/v1/logs', 'abc def'))
        records = json.loads(body)['resourceLogs'][0]['scopeLogs'][0]['logRecords']
        attributes = {a['key']: a['value'] for a in records[1]['attributes']}
        self.assertEqual(attributes['queryapigate.audit.seq'], {'intValue': '2'})
        self.assertEqual(json.loads(records[1]['body']['stringValue'])['target'], 'key-1')


class SettingsTests(StreamTestCase):
    def test_bad_settings_stop_the_server(self):
        for env, words in (({'QUERYAPIGATE_AUDIT_SYSLOG_URL': 'udp://siem:514'}, 'UDP loses entries'),
                           ({'QUERYAPIGATE_AUDIT_SYSLOG_URL': 'https://siem'}, 'tls://host:port'),
                           ({'QUERYAPIGATE_AUDIT_SYSLOG_CA': '/no/such/ca.pem'}, 'not a readable file'),
                           ({'QUERYAPIGATE_AUDIT_SYSLOG_CERT': self.certs['client'][0]}, 'go together'),
                           ({'QUERYAPIGATE_AUDIT_SYSLOG_URL': '', 'QUERYAPIGATE_AUDIT_OTLP': '1'}, 'no OTLP endpoint')):
            with mock.patch.dict(os.environ, env), self.assertRaisesRegex(ValueError, words):
                config.check_settings()


class SurfaceTests(StreamTestCase):
    def test_the_api_and_the_alert(self):
        self.write(1)
        auditstream.deliver_once('syslog')
        client = create_app().test_client()
        item = client.get('/api/v1/audit/delivery', headers=ADMIN).get_json()['items'][0]
        self.assertEqual((item['output'], item['delivered_seq'], item['behind'], item['last_error']),
                         ('syslog', 1, 0, None))
        self.receiver.stop()
        self.write(1, start=1)
        with self.assertRaises(OSError):
            auditstream.deliver_once('syslog')
        with db.transaction() as conn:  # waiting more than five minutes
            conn.execute("UPDATE audit_log SET timestamp = '2026-01-01 00:00:00' WHERE seq = 2")
        found = [a for a in alerts.collect() if a['kind'] == 'audit_delivery_behind']
        self.assertEqual(found[0]['severity'], 'critical')
        self.assertTrue(re.match(r"The audit log isn't reaching tls://localhost:\d+", found[0]['title']))


if __name__ == '__main__':
    unittest.main()

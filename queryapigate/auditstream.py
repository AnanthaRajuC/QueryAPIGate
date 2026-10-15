"""The audit log, streamed to a SIEM (ADR 0012, BACKLOG #96) - stable since 0.32, tested against real receivers
(ADR 0019).

Two outputs, each turned on by one setting:

- **syslog** - QUERYAPIGATE_AUDIT_SYSLOG_URL, `tls://host:6514` (RFC 5425) or `tcp://host:514`; UDP is refused, it
  loses entries silently. RFC 5424 messages, octet-counted (RFC 6587): facility 13 (log audit), the entry's action as
  MSGID, its `seq` as the registered `[meta sequenceId="..."]`, and the entry - with `seq`, `prev_hash` and `hash` -
  as JSON. QUERYAPIGATE_AUDIT_SYSLOG_CA trusts a private CA; _CERT and _KEY give a client certificate (mutual TLS).
- **OTLP logs** - QUERYAPIGATE_AUDIT_OTLP=1: OTLP/HTTP with JSON encoding to OTEL_EXPORTER_OTLP_LOGS_ENDPOINT (or
  OTEL_EXPORTER_OTLP_ENDPOINT + /v1/logs), headers from OTEL_EXPORTER_OTLP[_LOGS]_HEADERS. No extra package.

Delivery survives the SIEM being down: each output's cursor - the last `seq` delivered - is in the store
(audit_delivery), so it survives restarts, and one instance delivers at a time, whichever holds the output's lease
(renewed while it works; another takes over when it lapses). At least once: a batch sent just before a crash is sent
again after it - each entry carries `seq` and `hash` for the SIEM to drop a repeat. Entries not yet delivered aren't
trimmed by the rolling cap (auditchain.append() asks delivery_floor()), up to ten times the cap.

A batch counts as delivered once the OTLP endpoint answered 2xx, or syslog's connection took every byte of it. Syslog
over TCP has no acknowledgement: before each batch the connection is checked for a server that has closed its end,
and a refused client certificate is caught as the connection opens - but a SIEM that dies without closing the
connection can lose the batch sent into it. Where that matters, prefer OTLP, or a log forwarder close by.
"""
import json
import logging
import os
import select
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

from . import __version__, config, db

log = logging.getLogger('queryapigate')

BATCH = 500
LEASE_SECONDS = 30.0
INTERVAL = 2.0          # seconds between rounds when nothing wakes the thread
RETRY_AFTER_FAILURE = 10.0
MAX_BACKLOG_FACTOR = 10  # undelivered entries kept past the cap - up to this many times it
FACILITY_LOG_AUDIT = 13
SEVERITY_INFO = 6
AUTO_START = True        # tests turn the background thread off and call deliver_once()


def syslog_url():
    return os.environ.get('QUERYAPIGATE_AUDIT_SYSLOG_URL', '').strip() or None


def otlp_enabled():
    return config.env_flag('QUERYAPIGATE_AUDIT_OTLP')


def outputs():
    """The outputs configured on this process: 'syslog' and/or 'otlp'."""
    return [name for name, on in (('syslog', syslog_url()), ('otlp', otlp_enabled())) if on]


def check():
    """At startup: a syslog URL the stream can use, and the files it names readable."""
    url = syslog_url()
    if url:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme == 'udp':
            raise ValueError('QUERYAPIGATE_AUDIT_SYSLOG_URL: UDP loses entries silently - use tls://host:6514 (or '
                             'tcp://host:514 on a network you trust)')
        if parts.scheme not in ('tls', 'tcp') or not parts.hostname or not parts.port:
            raise ValueError('QUERYAPIGATE_AUDIT_SYSLOG_URL must be tls://host:port or tcp://host:port')
        for name in ('QUERYAPIGATE_AUDIT_SYSLOG_CA', 'QUERYAPIGATE_AUDIT_SYSLOG_CERT', 'QUERYAPIGATE_AUDIT_SYSLOG_KEY'):
            path = os.environ.get(name, '').strip()
            if path and not os.path.isfile(path):
                raise ValueError(f'{name} names {path}, which is not a readable file')
        if bool(os.environ.get('QUERYAPIGATE_AUDIT_SYSLOG_CERT', '').strip()) != \
                bool(os.environ.get('QUERYAPIGATE_AUDIT_SYSLOG_KEY', '').strip()):
            raise ValueError('QUERYAPIGATE_AUDIT_SYSLOG_CERT and QUERYAPIGATE_AUDIT_SYSLOG_KEY go together - the '
                             'client certificate and its key')
    if otlp_enabled() and not _otlp_endpoint():
        raise ValueError('QUERYAPIGATE_AUDIT_OTLP is on but no OTLP endpoint is set - set '
                         'OTEL_EXPORTER_OTLP_LOGS_ENDPOINT or OTEL_EXPORTER_OTLP_ENDPOINT')


# ---- the store side: cursors, leases, what's left ----

def delivery_floor(conn):
    """The lowest `seq` an output configured here still has to deliver past - the rolling cap keeps everything after
    it - or None when nothing's waiting on one."""
    names = outputs()
    if not names:
        return None
    rows = conn.execute(f"SELECT last_seq FROM audit_delivery WHERE output IN ({', '.join('?' * len(names))})",
                        names).fetchall()
    return min(row[0] for row in rows) if rows else None


def _ensure(conn, output):
    if conn.execute('SELECT 1 FROM audit_delivery WHERE output = ?', (output,)).fetchone() is None:
        # a new output starts from the oldest entry stored: what the store has is what the SIEM gets
        start = conn.execute('SELECT MIN(seq) FROM audit_log').fetchone()[0]
        conn.execute('INSERT INTO audit_delivery (output, last_seq, lease_until, details_json) '
                     "VALUES (?, ?, NULL, '{}')", (output, (start or 1) - 1))


def _take_lease(output, me):
    """Hold `output`'s lease for LEASE_SECONDS if it's free, lapsed or already ours. Returns the cursor, or None."""
    with db.transaction() as conn:
        _ensure(conn, output)
        row = conn.execute('SELECT last_seq, lease_until, details_json FROM audit_delivery WHERE output = ?',
                           (output,)).fetchone()
        details = json.loads(row['details_json'] or '{}')
        now = time.time()
        if row['lease_until'] and row['lease_until'] > now and details.get('instance') != me:
            return None
        details['instance'] = me
        conn.execute('UPDATE audit_delivery SET lease_until = ?, details_json = ? WHERE output = ?',
                     (now + LEASE_SECONDS, json.dumps(details), output))
        return row['last_seq']


def _advance(output, me, last_seq=None, error=None):
    with db.transaction() as conn:
        row = conn.execute('SELECT details_json FROM audit_delivery WHERE output = ?', (output,)).fetchone()
        details = json.loads(row['details_json'] or '{}')
        if details.get('instance') != me:
            return  # someone took over meanwhile: their cursor stands
        stamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        if error is None:
            details.update(last_delivered_at=stamp, last_error=None)
            conn.execute('UPDATE audit_delivery SET last_seq = ?, details_json = ? WHERE output = ?',
                         (last_seq, json.dumps(details), output))
        else:
            details.update(last_error=error, last_error_at=stamp)
            conn.execute('UPDATE audit_delivery SET details_json = ? WHERE output = ?', (json.dumps(details), output))


def _pending(after):
    rows = db.connection().execute('SELECT seq, prev_hash, hash, entry_json FROM audit_log WHERE seq > ? '
                                   'ORDER BY seq LIMIT ?', (after, BATCH)).fetchall()
    return [{**json.loads(r['entry_json']), 'seq': r['seq'], 'prev_hash': r['prev_hash'], 'hash': r['hash']}
            for r in rows]


def status():
    """Each configured output: how far it has delivered, how far behind, its last error - /api/v1/audit/delivery."""
    conn = db.connection()
    head = conn.execute('SELECT MAX(seq) FROM audit_log').fetchone()[0] or 0
    out = []
    for name in outputs():
        row = conn.execute('SELECT last_seq, lease_until, details_json FROM audit_delivery WHERE output = ?',
                           (name,)).fetchone()
        details = json.loads(row['details_json'] or '{}') if row else {}
        delivered = row['last_seq'] if row else None
        oldest = conn.execute('SELECT MIN(timestamp) FROM audit_log WHERE seq > ?', (delivered or 0,)).fetchone()[0]
        out.append({'output': name, 'delivered_seq': delivered, 'head_seq': head,
                    'behind': max(0, head - (delivered or 0)), 'oldest_undelivered_at': oldest if head else None,
                    'last_delivered_at': details.get('last_delivered_at'), 'last_error': details.get('last_error'),
                    'last_error_at': details.get('last_error_at'), 'instance': details.get('instance'),
                    'target': _describe(name)})
    return out


def _describe(name):
    if name == 'syslog':
        return config.redact_url(syslog_url())
    return config.redact_url(_otlp_endpoint() or '')


# ---- syslog ----

def _syslog_frame(entry, hostname, pid):
    """One RFC 5424 message, octet-counted (RFC 6587 / RFC 5425)."""
    stamp = datetime.strptime(entry['timestamp'], '%Y-%m-%d %H:%M:%S').astimezone().isoformat()
    msgid = ''.join(c for c in str(entry.get('action') or '-') if 33 <= ord(c) <= 126)[:32] or '-'
    body = json.dumps(entry, default=str, sort_keys=True, ensure_ascii=False)
    message = (f'<{FACILITY_LOG_AUDIT * 8 + SEVERITY_INFO}>1 {stamp} {hostname} queryapigate {pid} {msgid} '
               f'[meta sequenceId="{entry["seq"]}"] ').encode() + b'\xef\xbb\xbf' + body.encode()
    return str(len(message)).encode() + b' ' + message


class _Syslog:
    def __init__(self):
        self.sock = None

    def _connect(self):
        parts = urllib.parse.urlsplit(syslog_url())
        raw = socket.create_connection((parts.hostname, parts.port), timeout=10)
        if parts.scheme != 'tls':
            return raw
        context = ssl.create_default_context(cafile=os.environ.get('QUERYAPIGATE_AUDIT_SYSLOG_CA', '').strip() or None)
        context.minimum_version = ssl.TLSVersion.TLSv1_2  # the default; said, so nothing lowers it
        cert = os.environ.get('QUERYAPIGATE_AUDIT_SYSLOG_CERT', '').strip()
        if cert:
            context.load_cert_chain(cert, os.environ.get('QUERYAPIGATE_AUDIT_SYSLOG_KEY', '').strip())
        try:
            sock = context.wrap_socket(raw, server_hostname=parts.hostname)
        except Exception:
            raw.close()
            raise
        # TLS 1.3 completes the handshake before the server judges the client: a refused client certificate arrives
        # as an alert just after. Wait for it briefly, so the refusal fails this connect rather than a later batch.
        sock.settimeout(0.25)
        try:
            if sock.recv(1) == b'':
                sock.close()
                raise ConnectionError('the syslog server closed the connection after the handshake')
        except (socket.timeout, ssl.SSLWantReadError):
            pass
        except Exception:
            sock.close()
            raise
        sock.settimeout(10)
        return sock

    def _closed_by_server(self):
        """A syslog server never sends anything: a socket with something to read has been closed (or refused us) from
        the other end - found before a batch is written into it, where TCP would take it and lose it."""
        try:
            readable, _, _ = select.select([self.sock], [], [], 0)
        except (OSError, ValueError):
            return True
        return bool(readable)

    def send(self, entries):
        hostname, pid = socket.gethostname(), os.getpid()
        payload = b''.join(_syslog_frame(e, hostname, pid) for e in entries)
        if self.sock is not None and self._closed_by_server():
            self.close()
        for attempt in (1, 2):  # a connection the receiver closed since: one fresh try
            if self.sock is None:
                self.sock = self._connect()
            try:
                self.sock.sendall(payload)
                return
            except OSError:
                self.close()
                if attempt == 2:
                    raise

    def close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            finally:
                self.sock = None


# ---- OTLP logs ----

def _otlp_endpoint():
    explicit = os.environ.get('OTEL_EXPORTER_OTLP_LOGS_ENDPOINT', '').strip()
    if explicit:
        return explicit
    base = os.environ.get('OTEL_EXPORTER_OTLP_ENDPOINT', '').strip()
    return base.rstrip('/') + '/v1/logs' if base else None


def _otlp_headers():
    raw = os.environ.get('OTEL_EXPORTER_OTLP_LOGS_HEADERS', '').strip() or \
        os.environ.get('OTEL_EXPORTER_OTLP_HEADERS', '').strip()
    headers = {}
    for item in raw.split(','):
        if '=' in item:
            key, value = item.split('=', 1)
            headers[urllib.parse.unquote(key.strip())] = urllib.parse.unquote(value.strip())
    return headers


def _value(value):
    if isinstance(value, bool):
        return {'boolValue': value}
    if isinstance(value, int):
        return {'intValue': str(value)}
    return {'stringValue': value if isinstance(value, str) else json.dumps(value, default=str)}


def _otlp_payload(entries):
    from . import instances
    records = []
    for entry in entries:
        stamp = datetime.strptime(entry['timestamp'], '%Y-%m-%d %H:%M:%S').astimezone().timestamp()
        attributes = {'queryapigate.audit.seq': entry['seq'], 'queryapigate.audit.hash': entry['hash'],
                      'queryapigate.audit.prev_hash': entry['prev_hash'], 'queryapigate.audit.action': entry['action'],
                      'queryapigate.audit.actor': entry.get('actor') or '-',
                      'queryapigate.audit.target': entry.get('target') or ''}
        records.append({'timeUnixNano': str(int(stamp * 1e9)), 'severityNumber': 9, 'severityText': 'INFO',
                        'body': {'stringValue': json.dumps(entry, default=str, sort_keys=True)},
                        'attributes': [{'key': k, 'value': _value(v)} for k, v in attributes.items()]})
    resource = {'service.name': os.environ.get('OTEL_SERVICE_NAME', '').strip() or 'queryapigate',
                'service.version': __version__, 'service.instance.id': instances.instance_id()}
    return {'resourceLogs': [{
        'resource': {'attributes': [{'key': k, 'value': _value(v)} for k, v in resource.items()]},
        'scopeLogs': [{'scope': {'name': 'queryapigate.audit', 'version': __version__}, 'logRecords': records}]}]}


class _Otlp:
    def send(self, entries):
        request = urllib.request.Request(_otlp_endpoint(), data=json.dumps(_otlp_payload(entries)).encode(),
                                         headers={'Content-Type': 'application/json', **_otlp_headers()},
                                         method='POST')
        with urllib.request.urlopen(request, timeout=10) as response:
            if not 200 <= response.status < 300:
                raise OSError(f'the OTLP endpoint answered {response.status}')

    def close(self):
        pass


# ---- the deliverer ----

_senders: dict = {}
_wake = threading.Event()
_started = {'pid': None}


def _sender(output):
    if output not in _senders:
        _senders[output] = _Syslog() if output == 'syslog' else _Otlp()
    return _senders[output]


def deliver_once(output, me=None):
    """One round for `output`: take its lease, send what's after its cursor in batches, move the cursor. Returns how
    many entries were delivered, or None when another instance holds the lease."""
    from . import instances
    me = me or instances.instance_id()
    delivered = 0
    cursor = _take_lease(output, me)
    if cursor is None:
        return None
    while True:
        batch = _pending(cursor)
        if not batch:
            return delivered
        try:
            _sender(output).send(batch)
        except Exception as error:  # the SIEM down, a TLS failure: keep the cursor, say so, try again later
            _sender(output).close()
            _advance(output, me, error=f'{type(error).__name__}: {error}'[:300])
            raise
        cursor = batch[-1]['seq']
        _advance(output, me, last_seq=cursor)
        delivered += len(batch)
        if _take_lease(output, me) is None:  # renewed - or lost to another instance meanwhile
            return delivered


def wake():
    """A new entry: deliver it soon rather than at the next round."""
    _wake.set()


def _run():
    failed_until = {}
    while True:
        _wake.wait(INTERVAL)
        _wake.clear()
        for output in outputs():
            if time.monotonic() < failed_until.get(output, 0):
                continue
            try:
                deliver_once(output)
            except Exception as error:
                failed_until[output] = time.monotonic() + RETRY_AFTER_FAILURE
                log.warning('Audit log to %s: delivery failed, retrying in %.0fs - %s', output, RETRY_AFTER_FAILURE,
                            error)


def release_leases(me=None):
    """Give back the leases this instance holds - at a clean exit, so another instance (or this one, restarted) takes
    over at once rather than when the lease would have lapsed."""
    from . import instances
    me = me or instances.instance_id()
    try:
        with db.transaction() as conn:
            for row in conn.execute('SELECT output, details_json FROM audit_delivery').fetchall():
                if json.loads(row['details_json'] or '{}').get('instance') == me:
                    conn.execute('UPDATE audit_delivery SET lease_until = NULL WHERE output = ?', (row['output'],))
    except Exception:  # at exit the store may be gone already: the lease lapses by itself
        pass


def start():
    """The delivery thread, once per process, when an output is configured."""
    if not AUTO_START or not outputs() or _started['pid'] == os.getpid():
        return
    _started['pid'] = os.getpid()
    import atexit
    atexit.register(release_leases)
    threading.Thread(target=_run, name='queryapigate-audit-stream', daemon=True).start()

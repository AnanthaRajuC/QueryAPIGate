# ADR 0019: The SIEM stream, against real receivers

- **Status:** Accepted
- **Date:** 2026-10-13
- **Amended 2026-10-13, from the first runs:** rsyslog 8.2604, syslog-ng 4.12.0 and the Collector 0.160.0 each parse
  the stream as it is - APP-NAME, MSGID the action, `[meta sequenceId]`, the JSON message - so the format stays.
  Both syslog receivers refuse a missing client certificate (TLS 1.3 `certificate required`) and one from another CA
  (`unknown ca`), and QueryAPIGate notices each before counting anything delivered. A receiver stopped while
  QueryAPIGate holds a connection to it closes its end, and the next batch notices that rather than writing into it.
- **Backlog:** the "Before 1.0.0-rc1" item in BACKLOG.md's roadmap: the SIEM stream against a real receiver. Graduates
  `audit_stream` (ADR 0012), on ADR 0017's terms.

## Context

The audit log streams to a SIEM two ways (ADR 0012, 0.25): RFC 5424 syslog over TLS or TCP, octet-counted (RFC 6587),
mutual TLS optional; and OTLP/HTTP logs with JSON encoding. It stayed experimental in 0.30 on one count of
`experimental.GRADUATION` - it isn't **tested for real in CI**:

- **CI's receiver is ours.** `tests/test_audit_stream.py` runs a small TLS server in the test and parses what arrives
  with the test's own code. That proves we send what we think RFC 5424 says - not that rsyslog, syslog-ng or an
  OpenTelemetry Collector reads it as we meant: the MSGID, the structured data, the octet count, the TLS handshake a
  real server wants, a client certificate a real server checks.
- **Guide 53 was run against syslog-ng 4.11** - by hand, once, at 0.25. Nothing has checked it since.
- **OTLP has never met a real Collector**; its test collector is a `BaseHTTPRequestHandler`.

And one gap the stream's own promise opens: delivery is **at least once** - a batch that failed partway is sent again
- so a SIEM's copy may hold an entry twice. `queryapigate audit verify --file` walks a copy's hash chain, but a repeat
breaks it (`out_of_order`): the SIEM's copy of an untouched log fails the very check guide 53 offers for it.

## Decision

### 1. Real receivers in CI, none allowed to skip

The integration job starts three, as containers, each pinned by digest:

- **rsyslog** - `imtcp` with its OpenSSL driver, `ossl` (RFC 5425; the `rsyslog/rsyslog-collector` image carries it,
  not `gtls`), requiring a client certificate from the test CA
  (`StreamDriver.AuthMode="x509/certvalid"`), writing what it parsed - `APP-NAME`, `MSGID`, the structured data, the
  message - through a template, so the test reads rsyslog's parse, not ours.
- **syslog-ng** - its `syslog()` source over TLS, the same way: the parser Splunk Connect for Syslog is built on.
- **OpenTelemetry Collector** (contrib) - an `otlp` receiver over HTTP, a `file` exporter.

A new test module, `tests/test_audit_siem.py`, run against them (`QUERYAPIGATE_TEST_RSYSLOG`, `_SYSLOG_NG`,
`_OTELCOL`; the job fails if any skips), checks, for each:

- **Every entry arrives, in order, parsed as meant:** MSGID the action, `[meta sequenceId]` the `seq`, the JSON body
  intact - for the Collector, the body and the `queryapigate.audit.*` attributes.
- **Mutual TLS is real:** with a client certificate the receiver accepts, entries arrive; without one, the receiver
  refuses the handshake and QueryAPIGate notices before writing anything (`last_error`, `audit_delivery_behind`).
- **An outage is survived:** the receiver stopped, entries written, the receiver started - all of them arrive, none
  lost, and the server says how far behind it was meanwhile.
- **The SIEM's copy verifies** (below): what the receiver wrote, run through `queryapigate audit verify --file`, is
  intact - and an entry edited in that copy is found.

### 2. A SIEM's copy, repeats included, verifies

`audit verify --file` accepts what at-least-once delivery produces: entries in the order received, an entry repeated.
An exact repeat - the same `seq` and the same `hash` - is dropped before the walk; entries are walked in `seq` order.
A repeat whose `hash` differs is never dropped: it's reported as `conflicting_copies`, with both. The report says how
many repeats it dropped. A file of syslog lines is read too: the JSON is taken from each line's message.

### 3. Then the format is the format

If the three receivers parse the stream as it is, it graduates in this release: `audit_stream` leaves
`experimental.py`, and its documentation says what may still be added - attributes, structured-data parameters,
outputs - and its limits. If one of them shows the format has to change, the change ships here and graduation
waits a release, as ADR 0017's "unchanged for a release" asks.

### Limits it keeps, written down

- **Syslog over TCP has no acknowledgement.** A receiver that dies without closing the connection can lose the batch
  sent into it; one that closes it is noticed first. OTLP's 2xx is an acknowledgement. Unchanged, and the
  documentation says to prefer OTLP or a forwarder close by where that matters.
- **No UDP**, as now.

## Alternatives considered

- **Splunk or Elastic themselves in CI.** Heavy images, licences, and what they ingest is still rsyslog-, syslog-ng-
  or Collector-shaped at the edge - which is what this tests.
- **Fluent Bit or Vector.** Both reasonable; rsyslog and syslog-ng are the two RFC 5425 implementations SIEM
  collectors are most often built on, and the Collector is the reference for OTLP.
- **Exactly-once delivery** (a receiver acknowledging each entry). No standard SIEM input offers it; at least once,
  with `seq` and `hash` to drop repeats, is what they all handle.

## Consequences

- Every push tests the stream against the software that will receive it.
- The SIEM's copy becomes something an auditor can check without the server - repeats and all.
- CI's integration job gets three more containers, a few seconds each.

## Delivery

1. **`audit verify --file` for a SIEM's copy:** repeats, `seq` order, `conflicting_copies`, syslog lines.
2. **The receivers in CI** and `tests/test_audit_siem.py`: rsyslog, syslog-ng, the Collector - in order, mutual TLS,
   an outage, the copy verified.
3. **Graduation** (if nothing changed the format): documentation, the threat model's review note, guide 53 re-run
   against rsyslog and the Collector, release prep for 0.32.

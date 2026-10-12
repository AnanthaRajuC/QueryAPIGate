# How to send the audit log to your SIEM, and prove nobody edited it

**Time:** 20 minutes. **You'll end up with:** every administrative change signed into a hash chain, streamed over
mutual TLS to your SIEM as it happens - and a check, in one command, that what QueryAPIGate holds is what was written.

> **Experimental** - the SIEM stream is new in 0.25; its settings and message format may change in any minor
> release, always noted in the changelog ([what that means](../CHANGELOG.md#versioning-and-compatibility)). The chain
> itself is stable.

Every command and output below was run against a real server and **syslog-ng 4.11** as the SIEM, receiving RFC 5425
syslog over TLS and requiring a client certificate.

## Step 1: Sign the chain

```bash
python -c "import secrets; print(secrets.token_hex(32))"   # the audit key
QUERYAPIGATE_AUDIT_KEY=<that key>
QUERYAPIGATE_AUDIT_LOG_EXPORT_FILE=/var/log/queryapigate/audit.jsonl   # a copy on disk too
```

Keep the key outside the store, as you keep `QUERYAPIGATE_SECRET_KEY`. Every audit entry is then signed with it
(`hmac-sha256:...`), chained to the one before - someone who can write to the store but hasn't the key can't re-sign an
edit. Setting it is the chain's first entry here: `audit_key_set`.

## Step 2: The SIEM side

syslog-ng, taking TLS on 6514 and accepting only clients with a certificate from your CA:

```
source s_queryapigate {
  syslog(transport("tls") port(6514)
         tls(key-file("/pki/server.key") cert-file("/pki/server.crt")
             ca-file("/pki/ca.crt") peer-verify(required-trusted)));
};
destination d_audit {
  file("/siem/audit.log" template("${ISODATE} ${HOST} ${PROGRAM} msgid=${MSGID} seq=${.SDATA.meta.sequenceId} ${MESSAGE}\n"));
};
log { source(s_queryapigate); destination(d_audit); };
```

(rsyslog's `imtcp` with the `gtls` driver, Splunk's syslog input, or Sentinel's agent take the same messages.)

## Step 3: Point QueryAPIGate at it

```bash
QUERYAPIGATE_AUDIT_SYSLOG_URL=tls://siem.example.com:6514
QUERYAPIGATE_AUDIT_SYSLOG_CA=/etc/queryapigate/siem-ca.crt   # if the SIEM's certificate isn't from a public CA
```

Started without a client certificate, the SIEM refuses the connection - and that shows straight away, with nothing
counted as delivered:

```json
GET /api/v1/audit/delivery
{"output": "syslog", "delivered_seq": 0, "behind": 2,
 "last_error": "SSLError: [SSL: TLSV13_ALERT_CERTIFICATE_REQUIRED] tlsv13 alert certificate required"}
```

Add the client certificate:

```bash
QUERYAPIGATE_AUDIT_SYSLOG_CERT=/etc/queryapigate/client.crt
QUERYAPIGATE_AUDIT_SYSLOG_KEY=/etc/queryapigate/client.key
```

## Step 4: Changes, as the SIEM sees them

A key created, changed, rotated, deleted - in the SIEM's file, in order:

```
2026-10-12T09:50:21+05:30 web-1 queryapigate msgid=audit_key_set seq=1 {"action": "audit_key_set", "actor": "startup", ..., "hash": "hmac-sha256:5fb779b8...", "prev_hash": "0000...", "seq": 1, ...}
2026-10-12T09:50:25+05:30 web-1 queryapigate msgid=create_key seq=2 {"action": "create_key", "actor": "admin", ...}
2026-10-12T09:50:46+05:30 web-1 queryapigate msgid=update_key seq=3 {"action": "update_key", "changes": {"rate_limit": {"from": null, "to": "60/minute"}}, ...}
2026-10-12T09:50:46+05:30 web-1 queryapigate msgid=rotate_api_key seq=4 {"action": "rotate_api_key", ...}
2026-10-12T09:50:50+05:30 web-1 queryapigate msgid=delete_key seq=5 {"action": "delete_key", ...}
```

RFC 5424, facility 13 (log audit): the action is the message ID, `seq` is `[meta sequenceId]`, and the body is the entry
with its `seq`, `prev_hash` and `hash` - what the SIEM indexes, and what it can drop a repeat by.

## Step 5: When the SIEM is down

The key above was deleted while syslog-ng was stopped:

```json
{"delivered_seq": 0, "head_seq": 5, "behind": 5, "last_error": "ConnectionRefusedError: [Errno 111] Connection refused"}
```

Nothing is lost: each output's cursor is in the store, and entries it hasn't had are kept even past
`QUERYAPIGATE_AUDIT_LOG_LIMIT` (up to ten times it). Started again, it caught up within seconds:

```json
{"delivered_seq": 5, "head_seq": 5, "behind": 0, "last_error": null}
```

More than five minutes behind raises `audit_delivery_behind`. With several instances, one delivers at a time - the one
holding the output's lease - and another takes over if it stops.

## Step 6: Prove it

```bash
queryapigate audit verify
# Intact: entries 1 to 5, keyed with QUERYAPIGATE_AUDIT_KEY.
queryapigate audit verify --file /var/log/queryapigate/audit.jsonl
# Intact: entries 1 to 5, keyed with QUERYAPIGATE_AUDIT_KEY.
```

A copy of that file with one entry changed - the rate limit made `6000/minute`:

```
BROKEN at entry 3 (update_key, 2026-10-12 09:50:46): changed
```

(exit 1). The store is held to the same: the server checks it every five minutes and raises `audit_chain_broken` at
the first entry that doesn't verify - changed, removed, inserted, reordered, or re-written without the key.

And against the SIEM: each of the 5 entries' `hash` there matches the store's. The SIEM's copy is what catches what
the chain can't on its own - the newest entries deleted, or a rewrite by someone holding both the store and the key.

Without the key, nothing can be checked, and the command says so rather than claim it's intact:

```
Not checked: all 5 entries are keyed, and QUERYAPIGATE_AUDIT_KEY is not set - set it to the key they were written with.
```

(exit 2).

## Good to know

- **Rotating the audit key:** `QUERYAPIGATE_AUDIT_KEY=<new>,<old>` - the first signs, both verify; startup records
  `audit_key_rotated`. Keep the old key while entries signed with it are kept.
- **Syslog over TCP has no acknowledgement.** A SIEM that closes its end, or refuses the certificate, is noticed before
  anything is written into the connection; one that dies without closing it can lose the batch sent into it. If that
  matters, use OTLP logs (`QUERYAPIGATE_AUDIT_OTLP=1`), acknowledged per batch - or a forwarder next to QueryAPIGate.
- **At least once:** after a failure partway through a batch, the SIEM may see an entry twice - same `seq`, same
  `hash`.
- **Upgrading from 0.24** chains the entries already stored and records `audit_chain_started`; the chain proves nothing
  about them before then.

## Next steps

- [Read the audit log](20-read-the-audit-log.md).
- [The chain](../documentation/API.md#a-tamper-evident-chain) and
  [streaming](../documentation/API.md#streaming-the-audit-log-to-a-siem), in the reference.
- [Read the threat model](40-read-the-threat-model.md).

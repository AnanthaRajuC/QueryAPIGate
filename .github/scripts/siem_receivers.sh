#!/usr/bin/env bash
# Real SIEM receivers for tests/test_audit_siem.py (ADR 0019): rsyslog and syslog-ng taking RFC 5425 syslog over TLS,
# each requiring a client certificate from the test CA, and an OpenTelemetry Collector taking OTLP/HTTP logs - each
# writing what it parsed to a file the tests read. CI's integration job runs it; so can you:
#
#   .github/scripts/siem_receivers.sh start /tmp/qag-siem    # then QUERYAPIGATE_TEST_SIEM=/tmp/qag-siem
#   .github/scripts/siem_receivers.sh stop /tmp/qag-siem
set -euo pipefail

RSYSLOG=rsyslog/rsyslog-collector:2026-04@sha256:44af7972e9b78c61f442d1567cd095638c76fbc6001d334b6f914687c34b4f40
SYSLOG_NG=balabit/syslog-ng:4.12.0@sha256:ead5340a157ffd05d626b7a2560e0d09acfd8553c85cd17855a9388f63c368b4
OTELCOL=otel/opentelemetry-collector-contrib:0.160.0@sha256:799dc6cf12c96192af37b5bdba804da8c10b3bc563b43cb90c3f3c58d9572ad6
NAMES=(qag-siem-rsyslog qag-siem-syslog-ng qag-siem-otelcol)

action=${1:?start or stop}
dir=$(realpath -m "${2:?a directory for the certificates, configurations and what the receivers wrote}")

if [ "$action" = stop ]; then
  docker rm -f -v "${NAMES[@]}" >/dev/null 2>&1 || true
  exit 0
fi

mkdir -p "$dir/certs" "$dir/conf" "$dir/out"
chmod 777 "$dir/out"   # the receivers run as their own users
rm -f "$dir"/out/*     # what an earlier run received
cd "$dir/certs"

# A test CA, a server certificate for localhost, a client certificate from it - and one from a CA no receiver trusts.
# Authority and Subject Key Identifiers on each: Python 3.13's strict verification wants them (RFC 5280).
ext() { printf 'basicConstraints=%s\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid\n%s\n' "$1" "$2"; }
for ca in ca other-ca; do
  openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 2 -subj "/CN=$ca" \
    -keyout "$ca.key" -out "$ca.pem" -addext basicConstraints=critical,CA:TRUE \
    -addext keyUsage=critical,keyCertSign,cRLSign 2>/dev/null
done
issue() {  # name, issuing CA, extensions
  openssl req -new -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -subj "/CN=$1" -keyout "$1.key" \
    -out "$1.csr" 2>/dev/null
  openssl x509 -req -in "$1.csr" -CA "$2.pem" -CAkey "$2.key" -CAcreateserial -days 2 -out "$1.pem" \
    -extfile <(ext CA:FALSE "$3") 2>/dev/null
}
issue server ca 'subjectAltName=DNS:localhost,IP:127.0.0.1
extendedKeyUsage=serverAuth'
issue client ca 'extendedKeyUsage=clientAuth'
issue stranger other-ca 'extendedKeyUsage=clientAuth'
chmod 644 ./*.key ./*.pem

# rsyslog: imtcp with OpenSSL (RFC 5425), a client certificate required; what it parsed, one entry a line
cat > "$dir/conf/rsyslog.conf" <<'EOF'
global(DefaultNetstreamDriver="ossl" DefaultNetstreamDriverCAFile="/certs/ca.pem"
       DefaultNetstreamDriverCertFile="/certs/server.pem" DefaultNetstreamDriverKeyFile="/certs/server.key")
module(load="imtcp" StreamDriver.Name="ossl" StreamDriver.Mode="1" StreamDriver.AuthMode="x509/certvalid")
input(type="imtcp" port="6514")
template(name="parsed" type="list") {
  property(name="app-name") constant(value="\t") property(name="msgid") constant(value="\t")
  property(name="structured-data") constant(value="\t") property(name="msg") constant(value="\n")
}
action(type="omfile" file="/out/rsyslog.log" template="parsed")
EOF

# syslog-ng: its syslog() source over TLS (what Splunk Connect for Syslog is built on), the same way
cat > "$dir/conf/syslog-ng.conf" <<'EOF'
@version: 4.12
source s_tls {
  syslog(ip(0.0.0.0) port(6514) transport("tls")
         tls(key-file("/certs/server.key") cert-file("/certs/server.pem") ca-file("/certs/ca.pem")
             peer-verify(required-trusted)));
};
destination d_file { file("/out/syslog-ng.log" perm(0644) template("${PROGRAM}\t${MSGID}\t${SDATA}\t${MESSAGE}\n")); };
log { source(s_tls); destination(d_file); };
EOF

# The OpenTelemetry Collector: OTLP/HTTP in, a file out
cat > "$dir/conf/otelcol.yaml" <<'EOF'
receivers:
  otlp:
    protocols:
      http:
        endpoint: 0.0.0.0:4318
exporters:
  file:
    path: /out/otlp.jsonl
service:
  pipelines:
    logs:
      receivers: [otlp]
      exporters: [file]
EOF
chmod 644 "$dir"/conf/*

docker rm -f -v "${NAMES[@]}" >/dev/null 2>&1 || true
docker run -d --name qag-siem-rsyslog -p 127.0.0.1:16514:6514 -v "$dir/certs:/certs:ro" \
  -v "$dir/conf/rsyslog.conf:/etc/rsyslog.conf:ro" -v "$dir/out:/out" --entrypoint rsyslogd "$RSYSLOG" -n \
  -f /etc/rsyslog.conf >/dev/null
docker run -d --name qag-siem-syslog-ng -p 127.0.0.1:26514:6514 -v "$dir/certs:/certs:ro" \
  -v "$dir/conf/syslog-ng.conf:/etc/syslog-ng/syslog-ng.conf:ro" -v "$dir/out:/out" "$SYSLOG_NG" -F >/dev/null
docker run -d --name qag-siem-otelcol -p 127.0.0.1:14318:4318 -v "$dir/conf/otelcol.yaml:/etc/otelcol.yaml:ro" \
  -v "$dir/out:/out" "$OTELCOL" --config /etc/otelcol.yaml >/dev/null

for port in 16514 26514 14318; do   # until each is listening
  for _ in $(seq 60); do (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null && break; sleep 1; done
done
cat > "$dir/receivers.json" <<EOF
{"rsyslog": {"url": "tls://localhost:16514", "out": "$dir/out/rsyslog.log", "container": "qag-siem-rsyslog"},
 "syslog-ng": {"url": "tls://localhost:26514", "out": "$dir/out/syslog-ng.log", "container": "qag-siem-syslog-ng"},
 "otelcol": {"url": "http://localhost:14318", "out": "$dir/out/otlp.jsonl", "container": "qag-siem-otelcol"},
 "certs": "$dir/certs"}
EOF
echo "receivers up: $dir/receivers.json"

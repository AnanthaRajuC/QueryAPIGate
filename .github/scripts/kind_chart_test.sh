#!/usr/bin/env bash
# The Helm chart on a real cluster (ADR 0013): kind, both shapes, and an upgrade with requests running.
#
#   IMAGE=queryapigate:chart-test PREVIOUS_IMAGE=ghcr.io/anantharajuc/queryapigate:0.25.0 \
#     .github/scripts/kind_chart_test.sh
#
# Needs docker, kind, kubectl and helm on PATH, and IMAGE built locally (docker build -t "$IMAGE" .). PREVIOUS_CHART, when
# set (oci://ghcr.io/anantharajuc/charts/queryapigate --version X), installs that release's chart first; otherwise this
# chart with PREVIOUS_IMAGE. Either way the release is then upgraded to this chart and IMAGE while a pod in the cluster
# keeps calling it - and the test fails if a single request fails.
set -euo pipefail

CLUSTER=${CLUSTER:-qag-chart}
CHART=${CHART:-deploy/helm/queryapigate}
IMAGE=${IMAGE:?build the image and set IMAGE}
PREVIOUS_IMAGE=${PREVIOUS_IMAGE:?set PREVIOUS_IMAGE to the previous release image}
KEEP_CLUSTER=${KEEP_CLUSTER:-}
repo() { echo "${1%:*}"; }
tag() { echo "${1##*:}"; }
say() { printf '\n== %s\n' "$*"; }

# `rollout status` can return while the old ReplicaSet's pods are still in their preStop pause (10 s, the chart's
# deployment.yaml) - still alive, and running the old release's settings. A check of a setting that just changed waits
# for them to go first (CI, 2026-10-15: a DELETE the new lock refuses was answered by a pod without it).
only_new_pods() {
  for _ in $(seq 60); do
    [ "$(kubectl -n "$1" get pods -l app.kubernetes.io/component=server --no-headers | wc -l)" = "$2" ] && return 0
    sleep 2
  done
  echo "old server pods still there after 2 minutes:"; kubectl -n "$1" get pods -o wide; return 1
}

cleanup() {
  if [ -z "$KEEP_CLUSTER" ]; then kind delete cluster --name "$CLUSTER" >/dev/null 2>&1 || true; fi
}
trap cleanup EXIT

say "a cluster, with both images in it"
kind create cluster --name "$CLUSTER" --wait 180s
docker image inspect "$PREVIOUS_IMAGE" >/dev/null 2>&1 || docker pull -q "$PREVIOUS_IMAGE"
kind load docker-image --name "$CLUSTER" "$IMAGE" "$PREVIOUS_IMAGE"

# ---- several pods on PostgreSQL and Redis ----
say "the PostgreSQL shape: three pods, the events and MCP servers"
kubectl create namespace pg
kubectl -n pg apply -f deploy/kubernetes/dev-dependencies.yaml
kubectl -n pg rollout status deploy/postgres --timeout=180s
kubectl -n pg rollout status deploy/redis --timeout=180s
kubectl -n pg create secret generic qag \
  --from-literal=QUERYAPIGATE_API_KEY=ci-key \
  --from-literal=QUERYAPIGATE_DATABASE_URL=postgresql://queryapigate:dev-only-password@postgres:5432/queryapigate \
  --from-literal=QUERYAPIGATE_REDIS_URL=redis://redis:6379/0
COMMON=(--namespace pg --set store.type=postgres --set replicas=3 --set existingSecret=qag
        --set redis.inExistingSecret=true --set events.enabled=true --set mcp.enabled=true
        --set resources.requests.cpu=50m
        --set events.resources.requests.cpu=20m --set mcp.resources.requests.cpu=20m)
if [ -n "${PREVIOUS_CHART:-}" ]; then
  # shellcheck disable=SC2086
  helm install qag $PREVIOUS_CHART "${COMMON[@]}" --set image.repository="$(repo "$PREVIOUS_IMAGE")" \
    --set image.tag="$(tag "$PREVIOUS_IMAGE")" --wait --timeout 6m
else
  helm install qag "$CHART" "${COMMON[@]}" --set image.repository="$(repo "$PREVIOUS_IMAGE")" \
    --set image.tag="$(tag "$PREVIOUS_IMAGE")" --wait --timeout 6m
fi
kubectl -n pg get pods -o wide

say "it answers - through the Service, from inside the cluster"
kubectl -n pg run smoke --rm -i --restart=Never --image="$IMAGE" --command -- python -c "
import json, time, urllib.request
def get(url, key=True):
    # a Service just created can refuse connections for a few seconds, until its routes reach every node
    for attempt in range(30):
        try:
            request = urllib.request.Request(url, headers={'X-API-Key': 'ci-key'} if key else {})
            return json.load(urllib.request.urlopen(request, timeout=10))
        except OSError:
            if attempt == 29:
                raise
            time.sleep(1)
print('server', get('http://qag-queryapigate/health', key=False))
print('events', get('http://qag-queryapigate-events/health', key=False))
print('mcp   ', get('http://qag-queryapigate-mcp/health', key=False))
def post(path, body):
    request = urllib.request.Request('http://qag-queryapigate' + path, method='POST', data=json.dumps(body).encode(),
                                     headers={'X-API-Key': 'ci-key', 'Content-Type': 'application/json'})
    return urllib.request.urlopen(request, timeout=10).status
# a real connection - the cluster's PostgreSQL - and a saved query on it, made through the API
print('connection', post('/api/v1/connections', {'name': 'cluster_db', 'db': 'postgres', 'host': 'postgres',
      'port': 5432, 'user': 'queryapigate', 'password': 'dev-only-password', 'database': 'queryapigate',
      'active': True}))
print('query', post('/api/v1/queries', {'name': 'tables', 'connection_name': 'cluster_db', 'description': 'Tables',
      'publish': True, 'sql': \"SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'\"}))
print('it answers', len(get('http://qag-queryapigate/q/tables')), 'rows')
"

say "upgrade to this chart and $IMAGE while a pod keeps calling it"
QUERY=tables
kubectl -n pg run load --restart=Never --image="$IMAGE" --command -- python -c "
import time, urllib.request
ok, failed, end = 0, [], time.time() + 150
while time.time() < end:
    for url in ('http://qag-queryapigate/health', 'http://qag-queryapigate/q/$QUERY'):
        try:
            urllib.request.urlopen(urllib.request.Request(url, headers={'X-API-Key': 'ci-key'}), timeout=10).read()
            ok += 1
        except Exception as error:
            failed.append(f'{time.strftime(\"%H:%M:%S\")} {url}: {error}')
    time.sleep(0.05)
print(f'RESULT ok={ok} failed={len(failed)}')
for line in failed[:20]:
    print('FAILED', line)
"
kubectl -n pg wait --for=condition=Ready pod/load --timeout=120s
sleep 10
helm upgrade qag "$CHART" "${COMMON[@]}" --set image.repository="$(repo "$IMAGE")" --set image.tag="$(tag "$IMAGE")" \
  --wait --timeout 6m
kubectl -n pg rollout status deploy/qag-queryapigate --timeout=180s
kubectl -n pg wait --for=jsonpath='{.status.phase}'=Succeeded pod/load --timeout=300s
RESULT=$(kubectl -n pg logs load)
echo "$RESULT"
echo "$RESULT" | grep -q '^RESULT ok=[1-9][0-9]* failed=0$' || { echo 'requests failed during the upgrade'; exit 1; }
kubectl -n pg get pods -o jsonpath='{range .items[*]}{.metadata.name} {.spec.containers[0].image}{"\n"}{end}' \
  | grep qag-queryapigate

say "/metrics needs the chart's own token (metricsAuth, ADR 0017): 401 without it, the counts with it"
METRICS_TOKEN=$(kubectl -n pg get secret qag-queryapigate-metrics -o jsonpath='{.data.token}' | base64 -d)
[ "${#METRICS_TOKEN}" -ge 48 ] || { echo 'no metrics token in qag-queryapigate-metrics'; exit 1; }
kubectl -n pg run metrics-check --rm -i --restart=Never --image="$IMAGE" --env="METRICS_TOKEN=$METRICS_TOKEN" \
  --command -- python -c "
import os, urllib.error, urllib.request
for url in ('http://qag-queryapigate/metrics', 'http://qag-queryapigate-mcp/metrics'):
    try:
        urllib.request.urlopen(url, timeout=10)
        raise SystemExit(url + ' answered without its token')
    except urllib.error.HTTPError as error:
        assert error.code == 401, (url, error.code)
    request = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + os.environ['METRICS_TOKEN']})
    assert 'queryapigate_' in urllib.request.urlopen(request, timeout=10).read().decode(), url
    print(url, '- 401 without the token, the counts with it')
"

# ---- configuration as code (ADR 0014) ----
say "a configuration file through the chart: applied once, by the first new pod, and locked to the file"
CONFIG_DIR=$(mktemp -d)
cat > "$CONFIG_DIR/good.yaml" <<'YAML'
format: queryapigate-config
format_version: 1
queries:
  - name: from_the_file
    connection_name: cluster_db
    description: Made by the configuration file
    sql: SELECT COUNT(*) AS tables FROM information_schema.tables
YAML
CURRENT=(--set image.repository="$(repo "$IMAGE")" --set image.tag="$(tag "$IMAGE")")
helm upgrade qag "$CHART" "${COMMON[@]}" "${CURRENT[@]}" --set configAsCode.enabled=true \
  --set-file configAsCode.file="$CONFIG_DIR/good.yaml" --set configAsCode.locked=true --wait --timeout 6m
kubectl -n pg rollout status deploy/qag-queryapigate --timeout=180s
only_new_pods pg 3
[ "$(kubectl -n pg get secret qag-queryapigate-metrics -o jsonpath='{.data.token}' | base64 -d)" = "$METRICS_TOKEN" ] \
  || { echo 'helm upgrade changed the metrics token - lookup should have kept it'; exit 1; }
echo 'the metrics token is the one from before this upgrade'
LOGS=$(kubectl -n pg logs -l app.kubernetes.io/component=server --tail=-1)
APPLIED=$(echo "$LOGS" | grep -c 'Applied QUERYAPIGATE_CONFIG_FILE' || true)
ALREADY=$(echo "$LOGS" | grep -c 'is already applied' || true)
echo "pods that applied the file: $APPLIED, that found it applied: $ALREADY"
[ "$APPLIED" = 1 ] && [ "$ALREADY" = 2 ] || { echo 'expected one pod to apply the file and two to find it applied'; exit 1; }
kubectl -n pg run config-check --rm -i --restart=Never --image="$IMAGE" --command -- python -c "
import json, urllib.error, urllib.request
headers = {'X-API-Key': 'ci-key', 'Content-Type': 'application/json'}
rows = json.load(urllib.request.urlopen(urllib.request.Request('http://qag-queryapigate/q/from_the_file', headers=headers),
                                        timeout=10))
print('the query from the file answers:', rows)
try:
    urllib.request.urlopen(urllib.request.Request('http://qag-queryapigate/api/v1/queries/from_the_file', method='DELETE',
                                                  headers=headers), timeout=10)
    raise SystemExit('a managed query was deleted through the API, with the lock on')
except urllib.error.HTTPError as error:
    body = json.load(error)
    assert (error.code, body['code']) == (409, 'config_managed'), (error.code, body)
    print('deleting it through the API:', error.code, body['code'])
" || {
  echo 'which pod answered the DELETE, and did each have the lock:'
  kubectl -n pg logs -l app.kubernetes.io/component=server --tail=-1 --prefix | grep 'DELETE /api/v1/queries/from_the_file' || true
  for pod in $(kubectl -n pg get pods -l app.kubernetes.io/component=server -o name); do
    echo "$pod: $(kubectl -n pg exec "$pod" -- printenv QUERYAPIGATE_CONFIG_LOCKED || echo 'no lock')"
  done
  exit 1
}

say "a file that can't be applied stops the rollout at its first pod; the others keep serving"
cat > "$CONFIG_DIR/bad.yaml" <<'YAML'
format: queryapigate-config
format_version: 1
connections:
  - {name: leaky, db: postgres, host: postgres, password: a-literal-password}
YAML
if helm upgrade qag "$CHART" "${COMMON[@]}" "${CURRENT[@]}" --set configAsCode.enabled=true \
    --set-file configAsCode.file="$CONFIG_DIR/bad.yaml" --wait --timeout 90s; then
  echo 'a file with a literal password was applied'; exit 1
fi
kubectl -n pg logs -l app.kubernetes.io/component=server --tail=-1 --prefix | grep 'was not applied' | head -1 || true
READY=$(kubectl -n pg get deploy qag-queryapigate -o jsonpath='{.status.readyReplicas}')
echo "pods still serving: $READY"
[ "$READY" = 3 ] || { echo 'the rollout took serving pods down'; exit 1; }
helm rollback qag --namespace pg --wait --timeout 6m
kubectl -n pg rollout status deploy/qag-queryapigate --timeout=180s

# A Service just created, or a pod just replaced, can refuse connections for a few seconds: retry those (a refused
# request never reached the server, so retrying the POST can't make the key twice).
CALL="
import json, time, urllib.error, urllib.request
def call(request):
    for attempt in range(30):
        try:
            return json.load(urllib.request.urlopen(request, timeout=10))
        except urllib.error.HTTPError:
            raise
        except OSError:
            if attempt == 29:
                raise
            time.sleep(1)
"

# ---- the Rust data plane (ADR 0030) ----
say "the data plane in front of each API server pod, behind the ingress's trusted hop"
# ingress.enabled sets QUERYAPIGATE_TRUST_PROXY=1: the calls below carry X-Forwarded-For as the controller would send it
helm upgrade qag "$CHART" "${COMMON[@]}" "${CURRENT[@]}" --set dataplane.enabled=true --set ingress.enabled=true \
  --set dataplane.resources.requests.cpu=20m --wait --timeout 6m
kubectl -n pg rollout status deploy/qag-queryapigate --timeout=180s
only_new_pods pg 3
kubectl -n pg get pods -l app.kubernetes.io/component=server \
  -o jsonpath='{range .items[*]}{.metadata.name} {range .status.containerStatuses[*]}{.name}={.ready} {end}{"\n"}{end}'
NOT_READY=$(kubectl -n pg get pods -l app.kubernetes.io/component=server \
  -o jsonpath='{range .items[*]}{range .status.containerStatuses[*]}{.ready}{"\n"}{end}{end}' | grep -vc true || true)
[ "$NOT_READY" = 0 ] || { echo 'a server pod has a container that is not ready'; exit 1; }
kubectl -n pg run dataplane-check --rm -i --restart=Never --image="$IMAGE" --command -- python -c "$CALL
admin = {'X-API-Key': 'ci-key', 'Content-Type': 'application/json'}
def make(name, **grants):
    r = urllib.request.Request('http://qag-queryapigate/api/v1/api-keys', method='POST', headers=admin,
                               data=json.dumps({'name': name, 'connections': ['cluster_db'], **grants}).encode())
    return call(r)['secret']
def tables(key, client):
    r = urllib.request.Request('http://qag-queryapigate/q/tables', headers={'X-API-Key': key, 'X-Forwarded-For': client})
    try:
        return urllib.request.urlopen(r, timeout=10).status
    except urllib.error.HTTPError as error:
        return error.code
office = make('office', allowed_ips=['203.0.113.0/24'])
# Python answers each pod's first calls, then the data plane serves; every answer must be Python's
statuses = []
for _ in range(40):
    statuses.append(tables(office, '203.0.113.9'))
    time.sleep(0.25)
assert set(statuses) == {200}, statuses
assert tables(office, '198.51.100.1') == 401  # judged by the client the ingress names, not the ingress
print('an IP-restricted key: 200 from its range, 401 from outside it, by X-Forwarded-For')
"
SERVED=$(kubectl -n pg logs -l app.kubernetes.io/component=server -c dataplane --tail=-1 | grep -c 'Executing on' || true)
echo "calls the data plane served: $SERVED"
[ "$SERVED" -gt 0 ] || { kubectl -n pg logs -l app.kubernetes.io/component=server -c dataplane --tail=20; \
  echo 'the data plane served none of the calls behind the trusted hop'; exit 1; }

# ---- one pod on SQLite ----
say "the SQLite shape: one pod, its store on a volume that outlives the pod"
kubectl create namespace lite
# with a file mounted by extraVolumes, as the SIEM's CA would be
kubectl -n lite create secret generic siem-tls --from-literal=ca.crt=not-a-real-certificate
helm install lite "$CHART" --namespace lite --set secret.create=true --set secret.values.QUERYAPIGATE_API_KEY=ci-key \
  --set image.repository="$(repo "$IMAGE")" --set image.tag="$(tag "$IMAGE")" --set resources.requests.cpu=50m \
  --set 'extraVolumes[0].name=siem-tls' --set 'extraVolumes[0].secret.secretName=siem-tls' \
  --set 'extraVolumeMounts[0].name=siem-tls' --set 'extraVolumeMounts[0].mountPath=/etc/queryapigate/siem' \
  --set 'extraVolumeMounts[0].readOnly=true' --wait --timeout 4m
test "$(kubectl -n lite exec deploy/lite-queryapigate -- cat /etc/queryapigate/siem/ca.crt)" = not-a-real-certificate
echo "the mounted file is there"
kubectl -n lite run make --rm -i --restart=Never --image="$IMAGE" --command -- python -c "$CALL
r = urllib.request.Request('http://lite-queryapigate/api/v1/api-keys', method='POST', data=json.dumps(
    {'name': 'kept', 'connections': []}).encode(), headers={'X-API-Key': 'ci-key', 'Content-Type': 'application/json'})
print('created', call(r)['name'])"
kubectl -n lite delete pod -l app.kubernetes.io/component=server --wait=true
kubectl -n lite rollout status deploy/lite-queryapigate --timeout=180s
kubectl -n lite run check --rm -i --restart=Never --image="$IMAGE" --command -- python -c "$CALL
r = urllib.request.Request('http://lite-queryapigate/api/v1/api-keys/kept', headers={'X-API-Key': 'ci-key'})
print('still there after the pod was replaced:', call(r)['name'])"

say "passed"

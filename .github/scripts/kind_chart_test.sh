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

# ---- one pod on SQLite ----
say "the SQLite shape: one pod, its store on a volume that outlives the pod"
kubectl create namespace lite
helm install lite "$CHART" --namespace lite --set secret.create=true --set secret.values.QUERYAPIGATE_API_KEY=ci-key \
  --set image.repository="$(repo "$IMAGE")" --set image.tag="$(tag "$IMAGE")" --set resources.requests.cpu=50m \
  --wait --timeout 4m
kubectl -n lite run make --rm -i --restart=Never --image="$IMAGE" --command -- python -c "
import json, urllib.request
r = urllib.request.Request('http://lite-queryapigate/api/v1/api-keys', method='POST', data=json.dumps(
    {'name': 'kept', 'connections': []}).encode(), headers={'X-API-Key': 'ci-key', 'Content-Type': 'application/json'})
print('created', json.load(urllib.request.urlopen(r, timeout=10))['name'])"
kubectl -n lite delete pod -l app.kubernetes.io/component=server --wait=true
kubectl -n lite rollout status deploy/lite-queryapigate --timeout=180s
kubectl -n lite run check --rm -i --restart=Never --image="$IMAGE" --command -- python -c "
import json, urllib.request
r = urllib.request.Request('http://lite-queryapigate/api/v1/api-keys/kept', headers={'X-API-Key': 'ci-key'})
print('still there after the pod was replaced:', json.load(urllib.request.urlopen(r, timeout=10))['name'])"

say "passed"

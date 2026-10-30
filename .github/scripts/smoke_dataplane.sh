#!/usr/bin/env bash
# The image as two containers sharing one network, Python behind the Rust data plane (ADR 0030): a saved query on
# PostgreSQL, called with a scoped key, must be answered rightly every time, and served by the data plane once Python
# has answered it and recorded the key's use - within a history flush (1 s).
# Run by the Docker workflow on each image it builds; locally: .github/scripts/smoke_dataplane.sh <image> [port]
set -euo pipefail
image="$1"
port="${2:-5000}"
run="qag-smoke-$$"
cleanup() {
    docker rm -fv "$run-dp" "$run-py" "$run-pg" >/dev/null 2>&1 || true
    docker volume rm "$run-data" >/dev/null 2>&1 || true
    docker network rm "$run" >/dev/null 2>&1 || true
}
trap cleanup EXIT
fail() {
    echo "::error::$1"
    echo "--- Python"; docker logs "$run-py" 2>&1 | tail -40
    echo "--- the data plane"; docker logs "$run-dp" 2>&1 | tail -40
    exit 1
}

docker network create "$run" >/dev/null
docker run -d --name "$run-pg" --network "$run" -e POSTGRES_PASSWORD=pw postgres:16 >/dev/null
secret="$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')"
shared=(-e QUERYAPIGATE_API_KEY=smoke-admin-key -e "QUERYAPIGATE_DATAPLANE_SECRET=$secret" -v "$run-data:/data")
# Python first, on loopback: the data plane reads the store it makes, and takes the published port
docker run -d --name "$run-py" --network "$run" -p "127.0.0.1:$port:5000" "${shared[@]}" \
    -e QUERYAPIGATE_BIND=127.0.0.1:5001 "$image" >/dev/null
for _ in $(seq 1 60); do
    docker exec "$run-py" python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5001/health')" \
        2>/dev/null && break
    sleep 1
done
docker run -d --name "$run-dp" --network "container:$run-py" "${shared[@]}" \
    -e QUERYAPIGATE_DATAPLANE_LISTEN=0.0.0.0:5000 "$image" queryapigate-dataplane >/dev/null
for _ in $(seq 1 30); do curl -fs "localhost:$port/health" >/dev/null && break; sleep 1; done
curl -fs "localhost:$port/health" >/dev/null || fail "the data plane didn't answer /health"
for _ in $(seq 1 30); do docker exec "$run-pg" pg_isready -q && break; sleep 1; done

api() {  # method path json -> body
    curl -fs -X "$1" "localhost:$port$2" -H 'X-API-Key: smoke-admin-key' -H 'Content-Type: application/json' -d "$3"
}
api POST /api/v1/connections "{\"name\": \"pg\", \"db\": \"postgres\", \"host\": \"$run-pg\", \"port\": 5432,
    \"user\": \"postgres\", \"password\": \"pw\", \"database\": \"postgres\", \"active\": true}" >/dev/null \
    || fail "couldn't add the connection"
api POST /api/v1/queries '{"name": "smoke", "connection_name": "pg", "description": "smoke", "publish": true,
    "sql": "SELECT 1 AS one, :n AS n", "parameters": {"n": "int"}}' >/dev/null || fail "couldn't save the query"
key="$(api POST /api/v1/api-keys '{"name": "smoke", "connections": ["pg"]}' \
    | python3 -c 'import json, sys; print(json.load(sys.stdin)["secret"])')" || fail "couldn't make a key"

served=0
for n in $(seq 1 10); do
    body="$(curl -fs "localhost:$port/q/smoke?n=$n" -H "X-API-Key: $key")" || fail "call $n failed"
    [ "$body" = "[{\"one\":1,\"n\":$n}]" ] || fail "call $n answered $body"
    served="$(docker logs "$run-dp" 2>&1 | grep -c 'Executing on' || true)"
    [ "$served" -gt 0 ] && break
    sleep 0.5
done
[ "$served" -gt 0 ] || fail "the data plane served none of $n calls"
docker logs "$run-dp" 2>&1 | head -1
echo "The data plane served call $n; Python answered the ones before it."

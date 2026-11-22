# How to push a query's new result to your app when the data changes

**Time:** 30 minutes, with Docker. **You'll end up with:** an app's product screen that shows a price and stock, and
updates within about a second when that row changes in PostgreSQL - changed by any application, not only through
QueryAPIGate. No polling, and no change to the saved query the app already calls.

> **Experimental** - live queries are new and may change in any minor release
> ([ADR 0033](../documentation/adr/0033-live-queries.md),
> [what experimental means](../CHANGELOG.md#versioning-and-compatibility)).

How it works: the app subscribes to a saved query with its parameters, `GET /live/product?id=1` on the events server,
and gets the result at once. [Debezium Server](https://debezium.io/documentation/reference/stable/operations/debezium-server.html)
reads PostgreSQL's change log and tells QueryAPIGate which tables changed - table names only, never rows. QueryAPIGate
then runs the query again, as the app's key, and sends the new result only if it's different. Who may see what is
decided exactly as for `GET /q/product`: the key's grants, masking, rate limit and quota. The full reference is
[Live queries](../documentation/API.md#live-queries) in the API documentation.

## Step 1: PostgreSQL, with its change log readable

Debezium needs `wal_level=logical`. A test database, on a Docker network that Debezium will join in step 6:

```bash
docker network create shop
docker run -d --name shop-db --network shop -p 5433:5432 \
    -e POSTGRES_PASSWORD=pw -e POSTGRES_DB=shop postgres:16 -c wal_level=logical
docker exec -i shop-db psql -U postgres -d shop <<'SQL'
CREATE TABLE product (id int PRIMARY KEY, name text NOT NULL, price numeric(8, 2) NOT NULL, stock int NOT NULL);
INSERT INTO product VALUES (1, 'Espresso machine', 249.00, 12), (2, 'Grinder', 89.00, 30), (3, 'Milk jug', 14.50, 80);
SQL
```

On a database of your own, set `wal_level = logical` in `postgresql.conf` and restart it; the user Debezium connects
as needs the `REPLICATION` attribute (or a managed service's equivalent - RDS's `rds.logical_replication`, say).

## Step 2: QueryAPIGate, with live queries on

Two processes on one store: the server, and the events server that holds the streams. In two terminals, with the same
three settings:

```bash
export QUERYAPIGATE_HOME=~/shop-demo QUERYAPIGATE_API_KEY=admin-secret QUERYAPIGATE_LIVE_QUERIES=1
queryapigate serve --host 0.0.0.0
```

```bash
export QUERYAPIGATE_HOME=~/shop-demo QUERYAPIGATE_API_KEY=admin-secret QUERYAPIGATE_LIVE_QUERIES=1
queryapigate events
```

`--host 0.0.0.0` lets Debezium, in a container, reach the server on port 5000; with a key set, every route still needs
one. The events server listens on port 5002. Both log `Experimental: Live queries ...` at startup.

## Step 3: A saved query, and a key for the app

The connection, the query the app shows a product with, and the app's key - which may run that query and nothing else:

```bash
curl -s -X POST localhost:5000/api/v1/connections -H 'X-API-Key: admin-secret' -H 'Content-Type: application/json' \
    -d '{"name": "shop", "db": "postgres", "host": "127.0.0.1", "port": 5433, "user": "postgres",
         "password": "pw", "database": "shop"}'
curl -s -X POST localhost:5000/api/v1/queries -H 'X-API-Key: admin-secret' -H 'Content-Type: application/json' \
    -d '{"name": "product", "description": "One product: its price and stock", "connection_name": "shop",
         "sql": "SELECT id, name, price, stock FROM product WHERE id = :id",
         "parameters": {"id": {"type": "int"}}, "publish": true}'
curl -s -X POST localhost:5000/api/v1/api-keys -H 'X-API-Key: admin-secret' -H 'Content-Type: application/json' \
    -d '{"name": "shop-app", "connections": [], "queries": ["product"]}'
```

Keep the `secret` from the last response: it's the app's key. Nothing here is specific to live queries - an app that
already calls `GET /q/product?id=1` has all of it.

## Step 4: Subscribe

```bash
curl -N 'localhost:5002/live/product?id=1' -H 'X-API-Key: <the app key>'
```

```
event: result
data: {"query": "product", "params": {"id": "1"}, "rows": [{"id": 1, "name": "Espresso machine", "price": 249.0, "stock": 12}], "truncated": false, "reason": "subscribe", "refreshed_at": "2026-11-16 06:35:49"}
```

The current result comes first, with `reason` `subscribe`; then `: keepalive` every 15 seconds while nothing changes.
Leave it running. A key that may not run `product` gets the same refusal `GET /q/product` would answer, before any
stream opens.

## Step 5: A CDC source for Debezium

Each Debezium Server that reports changes is a *CDC source*: a name, the connection its changes belong to, and a
secret it signs its posts with. In the Console, open **Data -> CDC sources** and press **New CDC source**: name `shop`,
connection `shop`, server name `shop` (Debezium's `topic.prefix`, so posts from any other server are refused). Or:

```bash
curl -s -X POST localhost:5000/api/v1/cdc-sources -H 'X-API-Key: admin-secret' -H 'Content-Type: application/json' \
    -d '{"name": "shop", "connection": "shop", "server_name": "shop"}'
```

```json
{"name": "shop", "connection": "shop", "server_name": "shop", "ingest_path": "/ingest/cdc/shop", "managed_by": null,
 "last_received_at": null, "previous_expires_at": null, "secret": "whsec_Gl8O...", ...}
```

**The secret is shown only this once** - the Console shows it with the sink lines for Debezium, ready to copy. Lost,
it can't be read back: rotate it (step 8). Debezium posts to `ingest_path` on the server, `/ingest/cdc/shop` here (a
source in another workspace gets `/w/<workspace>/ingest/cdc/<name>`).

## Step 6: Debezium Server

Its configuration, `application.properties` - the source lines read PostgreSQL, the sink lines post to QueryAPIGate:

```properties
debezium.source.connector.class=io.debezium.connector.postgresql.PostgresConnector
debezium.source.plugin.name=pgoutput
debezium.source.database.hostname=shop-db
debezium.source.database.port=5432
debezium.source.database.user=postgres
debezium.source.database.password=${DB_PASSWORD}
debezium.source.database.dbname=shop
debezium.source.topic.prefix=shop
debezium.source.table.include.list=public.product
debezium.source.snapshot.mode=no_data
debezium.source.offset.storage.file.filename=data/offsets.dat
debezium.source.offset.flush.interval.ms=0
debezium.sink.type=http
debezium.sink.http.url=http://host.docker.internal:5000/ingest/cdc/shop
debezium.sink.http.batch.enabled=true
debezium.sink.http.authentication.type=standard-webhooks
debezium.sink.http.authentication.webhook.secret=${WEBHOOK_SECRET}
debezium.format.value=json
```

- `snapshot.mode=no_data`: live queries need to hear about changes, not every row that exists.
- `batch.enabled=true`: one post per batch of changes - a 1,000-row `UPDATE` is a handful of posts, not 1,000.
- `${DB_PASSWORD}` and `${WEBHOOK_SECRET}` come from Debezium's environment, so neither is written in the file.

Run it on the network from step 1, with the secret from step 5:

```bash
DB_PASSWORD=pw WEBHOOK_SECRET='whsec_...' docker run -d --name shop-cdc --network shop \
    --add-host=host.docker.internal:host-gateway -e DB_PASSWORD -e WEBHOOK_SECRET \
    -v "$PWD/application.properties:/debezium/config/application.properties:ro" \
    quay.io/debezium/server:3.7.1.Final
until docker logs shop-cdc 2>&1 | grep -q 'Processing messages'; do sleep 1; done
```

It reaches PostgreSQL by its container name, and QueryAPIGate on your machine as `host.docker.internal`. If no post
arrives in step 7, and Debezium's log shows the sink timing out, a firewall (`ufw`, say) is keeping containers from
your machine's port 5000: allow it from the Docker network.

## Step 7: Change the data behind QueryAPIGate's back

Straight in PostgreSQL, as any other application would:

```bash
docker exec shop-db psql -U postgres -d shop -c "UPDATE product SET price = 229.00, stock = 11 WHERE id = 1"
```

The stream from step 4 gets the new result - 0.8 seconds after the `UPDATE` in the run below:

```
event: result
data: {"query": "product", "params": {"id": "1"}, "rows": [{"id": 1, "name": "Espresso machine", "price": 229.0, "stock": 11}], "truncated": false, "reason": "change", "refreshed_at": "2026-11-16 06:36:12"}
```

Now change another product - `UPDATE product SET stock = stock - 1 WHERE id = 2`. Debezium posts it, QueryAPIGate
runs `product?id=1` again, gets the same result, and sends nothing. The **CDC sources** screen shows when the source
last heard from Debezium (`last_received_at`), so a source that has gone quiet is easy to spot.

Changes made *through* QueryAPIGate - a saved write query, ad-hoc SQL, an agent's approved write - need no Debezium:
they signal the tables they wrote by themselves.

## Step 8: In your app

A browser can't send `X-API-Key` with `EventSource`, so read the stream with `fetch`. This function works in a browser
and in Node 18 or later:

```javascript
// Subscribe to a live query and call onResult with each new result.
export async function watch(url, key, onResult, signal) {
  const response = await fetch(url, { headers: { 'X-API-Key': key }, signal });
  if (!response.ok) throw new Error(`refused: ${response.status} ${await response.text()}`);
  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = '';
  for (;;) {
    const { value, done } = await reader.read();
    if (done) return; // the server ended the stream: reconnect, and the current result comes first
    buffer += value;
    let end;
    while ((end = buffer.indexOf('\n\n')) >= 0) {
      const block = buffer.slice(0, end);
      buffer = buffer.slice(end + 2);
      let type = 'message', data = '';
      for (const line of block.split('\n')) {
        if (line.startsWith('event: ')) type = line.slice(7);
        else if (line.startsWith('data: ')) data += line.slice(6);
      }
      if (!data) continue; // ": keepalive"
      const payload = JSON.parse(data);
      if (type === 'error') console.warn('re-run failed:', payload.code, payload.error);
      else onResult(payload);
    }
  }
}

watch('http://localhost:5002/live/product?id=3', appKey, (result) => {
  showProduct(result.rows[0]);                           // the whole current result, every time
  if (result.reason === 'change') flash();               // 'subscribe' for the first one
});
```

- **A web page on another origin** needs it allowed: `QUERYAPIGATE_CORS_ORIGINS=https://shop.example.com` on the events
  server ([guide 38](38-allow-a-browser-frontend-cors.md)).
- **Reconnecting** needs no bookkeeping: there are no event ids to resume from. A live query is a state, so a client
  that reconnects gets the current result first.
- **Signed-in users** send `Authorization: Bearer <token>` instead of a key - each sees the result their own token's
  claims give them ([Signed-in users](../documentation/API.md#signed-in-users-jwt)).

## Going to production

- **Route `/live/` to the events server** beside `/events`, from the reverse proxy in front of both - the Caddyfile
  in [DEPLOYMENT.md](../documentation/DEPLOYMENT.md#9-live-events-for-many-clients-optional) does. The main server
  answers `/live/...` with `404`.
- **Set `QUERYAPIGATE_ENCRYPTION_KEY`**, so CDC sources' secrets are stored encrypted, as connection passwords are.
- **Rotate a secret** with **Rotate** on the CDC sources screen (or `POST /api/v1/cdc-sources/shop/rotate`): the new
  one is shown once, and the old one keeps working for the grace period you choose - an hour by default - so
  Debezium can be restarted with the new one at any point in it.
- **How often a query runs again** is bounded: at most once a second per caller, query and parameters
  (`QUERYAPIGATE_LIVE_MIN_INTERVAL`), however many changes arrive and however many streams share it. Each run counts
  against the key's own rate limit and quota, as a `GET /q/` call does; a run over the limit is postponed, not
  dropped.
- **Watch it** on the events server's `/metrics`: `queryapigate_live_subscriptions`, `_signals_total`,
  `_reruns_total`, `_pushed_total` and `_unchanged_total`. Many unchanged runs for each push means many changes to
  rows the streams don't show.
- **Other databases** - MySQL, MariaDB, MongoDB and SQL Server - change only Debezium's source lines; what each needs
  is in [Live queries](../documentation/API.md#live-queries).

## Clean up

```bash
docker rm -fv shop-cdc shop-db
docker network rm shop
```

*Verified on 2026-11-16 with PostgreSQL 16, Debezium Server 3.7.1.Final, Node 22 for the client and Caddy 2 for the
proxy: the first result on subscribing, the change 0.8 seconds after the `UPDATE`, nothing sent for another product,
`last_received_at` set, and a change delivered through Caddy.*

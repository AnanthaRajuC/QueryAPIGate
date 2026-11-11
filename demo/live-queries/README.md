# Live queries demo (ADR 0033 prototype)

A web page shows a film's title and price, read through the saved query `film_lookup` with a key that may run only
that query. Change the row in PostgreSQL, directly with `psql`, and the page updates within a couple of seconds.
Debezium reads the change from PostgreSQL's log and tells QueryAPIGate that `public.film` changed. QueryAPIGate
re-runs `film_lookup` as the subscriber and pushes the new result only if it's different.

Everything here is from the `feat/live-queries` branch, behind `QUERYAPIGATE_LIVE_QUERIES`. The design is in
[ADR 0033](../../documentation/adr/0033-live-queries.md).

## 1. PostgreSQL and Debezium Server

```sh
cd demo/live-queries
docker compose up -d postgres
```

Debezium comes up in step 3, once QueryAPIGate is listening.

## 2. QueryAPIGate from this branch

Run these from the branch's folder (the worktree), so `python -m queryapigate` imports the branch's code rather than
the editable install from your main checkout. Use two terminals:

```sh
export QUERYAPIGATE_HOME=$(mktemp -d) QUERYAPIGATE_API_KEY=admin QUERYAPIGATE_LIVE_QUERIES=1
export QUERYAPIGATE_CORS_ORIGINS=http://localhost:8000
export QUERYAPIGATE_CDC_SOURCES='{"shop": {"connection": "shop", "server_name": "shop", "secret": "whsec_bGl2ZS1xdWVyaWVzLWRlbW8tc2VjcmV0LTAxMjM0NTY="}}'
python -m queryapigate serve --port 5050
```

In the second terminal, with the same five exports:

```sh
python -m queryapigate events --port 5052
```

Then the connection, the saved query, and Alice's key, which may run `film_lookup` and nothing else:

```sh
curl -s -X POST localhost:5050/api/v1/connections -H 'X-API-Key: admin' -H 'Content-Type: application/json' -d '{"name": "shop", "db": "postgres", "host": "127.0.0.1", "port": 55432, "user": "postgres", "password": "pw", "database": "shop"}'
curl -s -X POST localhost:5050/api/v1/queries -H 'X-API-Key: admin' -H 'Content-Type: application/json' -d '{"name": "film_lookup", "description": "One film", "connection_name": "shop", "sql": "SELECT id, title, rental_rate FROM film WHERE id = :film_id", "parameters": {"film_id": {"type": "int"}}, "publish": true}'
curl -s -X POST localhost:5050/api/v1/api-keys -H 'X-API-Key: admin' -H 'Content-Type: application/json' -d '{"name": "alice-phone", "connections": [], "queries": ["film_lookup"]}'
```

Copy the `secret` from the last response: that's Alice's key.

## 3. Debezium

```sh
docker compose up -d debezium
docker compose logs -f debezium
```

Wait for a line containing `Processing messages`, then stop following the log with Ctrl+C.

## 4. Watch it

**With curl:**

```sh
curl -N 'localhost:5052/live/film_lookup?film_id=1' -H 'X-API-Key: <alice key>'
```

**Or in a browser:** serve the page and open http://localhost:8000:

```sh
python3 -m http.server 8000
```

Then paste Alice's key and press Subscribe.

Now change the data behind QueryAPIGate's back:

```sh
docker compose exec postgres psql -U postgres -d shop -c "UPDATE film SET rental_rate = 1.49 WHERE id = 1"
```

A new `event: result` arrives with the new price. Things to try:

- **Change film 2** (`... WHERE id = 2`). Nothing arrives on film 1's stream. `/metrics` on port 5052 counts the
  re-run as `queryapigate_live_unchanged_total`.
- **Run ten updates in a row.** Re-runs are throttled to one a second (`QUERYAPIGATE_LIVE_MIN_INTERVAL`), so you get
  the latest state, not ten events.
- **Change it through QueryAPIGate** (`POST /execute_sql` with the admin key and `QUERYAPIGATE_ALLOW_WRITES=1`). The
  write signals the table itself, so no Debezium is needed for that.
- **Watch the signals:** `curl -N localhost:5052/events -H 'X-API-Key: admin'` shows each `table.changed` event.
  They hold table names only, never row values.
- **Try another key** with no grant for `film_lookup`. It gets the same `403` that `GET /q/film_lookup` would give.

## 5. Clean up

```sh
docker compose down -v
```

`-v` removes PostgreSQL's volume too.

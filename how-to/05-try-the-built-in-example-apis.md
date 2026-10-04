# How to try QueryAPIGate without setting anything up

**Time:** 2 minutes to load, as long as you like to explore. **You'll end up with:** four realistic,
fully-worked example APIs - reporting, a live dashboard, a bulk export, and a partner integration - each
with its own scoped key, running against a small generated database. No external database, no config
file to write by hand.

This is the fastest way to see what a finished, production-shaped setup looks like before building your
own - real collections, real roles, real per-scenario API keys, real run history, not a placeholder.

## Step 1: Load them

```bash
QUERYAPIGATE_API_KEY=demo-key queryapigate examples load
```

```
Loaded the example APIs: 11 queries in 4 collections, 5 roles, and the 'examples' connection.
Example API keys (store these now - they cannot be shown again):
  example-dashboard: sk_...
  example-executive: sk_...
  example-export: sk_...
  example-partner: sk_...
  example-reporting: sk_...
Try:  queryapigate serve   then open /console, or  curl http://127.0.0.1:5000/q/example_top_films -H 'X-API-Key: <one of the secrets above>'
Watch it live:  curl -N http://127.0.0.1:5000/events -H 'X-API-Key: <one of the secrets above>' (then call a query in another terminal to see the event arrive)
Remove them again with:  queryapigate examples unload
```

Copy those five secrets somewhere now - like any API key, they're generated fresh and shown exactly once;
there's no way to retrieve them again later, only to remove the keys and start over. Setting
`QUERYAPIGATE_API_KEY=demo-key` first gives you an admin key too - the moment any key exists, the server
requires one for every request, examples included.

```bash
QUERYAPIGATE_API_KEY=demo-key queryapigate serve
# Serving on http://127.0.0.1:5000
```

Running `examples load` again is safe and idempotent - "The example APIs are already loaded - nothing
changed," no duplicates, no new keys generated. `queryapigate examples status` tells you the current state
at a glance without changing anything.

## What just got installed

**A generated SQLite database** (`examples` connection) - 8 tables with real foreign keys (`film`,
`customer`, `rental`, `category`, `store`, `staff`, `address`, `payment`), 60 films, 200 customers, and
20,000 rentals dated relative to *today*, so "rentals so far today" and "overdue" queries always have
something real to show, not stale fixture data.

**Four collections, each shaped like a real scenario:**

| Collection | Simulates | Queries |
|---|---|---|
| `examples-reporting` | A read-only, rate-limited reporting API | `example_monthly_revenue`, `example_top_films`, `example_revenue_by_category` |
| `examples-dashboard` | Small polled queries, cached 30s each | `example_kpi_rentals_today`, `example_kpi_active_rentals`, `example_kpi_overdue`, `example_recent_rentals` |
| `examples-export` | A bulk `?stream=true` export | `example_all_rentals` (~20,000 rows), `example_rentals_since` |
| `examples-partner` | A narrow, validated external-client surface | `example_film_lookup`, `example_film_search` |

**Five roles, and one real API key created from each** - `example-reporting`, `example-dashboard`,
`example-export`, `example-partner` (one per collection above, rate-limited to match what that kind of
traffic would realistically need - e.g. `example-export`'s key is capped at 20 requests/hour, a bulk
export isn't meant to be polled), plus a fifth, `example-executive`, granted *both* the reporting and
dashboard collections at once - the one thing none of the other four shows on its own: a role reaching
several collections together, not just one.

**Seeded run history** - each query already has realistic-looking (synthetic, clearly not real traffic)
execution history, so the admin UI's History tab, Home tab and requests-per-day charts show something
immediately instead of an empty state. This is the one part that's synthesized; live counters
(`/metrics`, the Usage columns on API Keys/Connections) are never faked - those reset on restart and
stay honest.

## Step 2: Call one

```bash
curl 'http://127.0.0.1:5000/q/example_top_films?top_n=3' -H 'X-API-Key: demo-key'
```

```json
[{"title": "Electric Signal", "category": "Comedy", "rating": "PG", "rentals": 977, "rank": 1},
 {"title": "Electric Garden", "category": "Documentary", "rating": "PG", "rentals": 919, "rank": 2},
 {"title": "Distant Garden", "category": "Drama", "rating": "R", "rentals": 821, "rank": 3}]
```

Or use one of the scoped keys `load` just printed instead of the admin key - it only reaches its own
collection:

```bash
curl 'http://127.0.0.1:5000/q/example_top_films?top_n=3' -H 'X-API-Key: <the example-reporting secret>'
```

Try the bulk export too, streamed rather than paged (see
[Export a large result without running out of memory](09-stream-a-large-export.md)):

```bash
curl 'http://127.0.0.1:5000/q/example_all_rentals?stream=true&format=csv' -H 'X-API-Key: demo-key' | head
```

## Step 3: Explore it in the admin UI

Open **<http://127.0.0.1:5000/console>**. Two things worth noticing right away:

- **API Repository** shows a banner across the top: *"Example APIs are loaded: 11 queries in 4
  collections, 5 roles and an 'examples' connection. Removing them touches nothing else."* - with a
  **Remove examples** button right there, the UI equivalent of `queryapigate examples unload`.
- Click any example query (e.g. `example_kpi_active_rentals`) and its **Run** tab already shows real
  numbers - total runs, success rate, average duration, a requests-per-day bar chart - from the seeded
  history above, with no need to call it yourself first.

From there, the same tabs work as they would for a query you wrote yourself: **SQL** (the query text),
**History** (every run, seeded or real), **Curl**/**CLI** (copy-pasteable commands), **API Keys**/**Roles**
(which of the five keys can reach this specific query), **Access**, **Cache** (for the dashboard scenario's
`cache_ttl=30` queries), **Metrics**. Visit **Access map** (under **Access** in the sidebar) to see all
five keys and every query they reach laid out at once - the fastest way to build intuition for how
`connections`/`queries`/`collections` grants actually compose, before designing your own.

## Removing them

```bash
queryapigate examples unload
```

```
Removed 11 example queries, 5 roles, 5 API keys and the connection.
```

Removes exactly what was marked as an example - nothing you've added yourself, even if it happens to share
a connection or collection name. Real data you connected in the meantime is untouched.

## Next steps

- [Turn your first SQL query into a REST API](01-turn-your-first-sql-query-into-a-rest-api.md) - the same
  walkthrough, but starting from a blank query instead of an example.
- [Connect your own database](02-connect-a-database.md) once you're ready to move past the generated one.
- The **Access map** (under **Access**) and [`GET /catalog`](../documentation/API.md#the-api-catalogue) -
  who can reach what, explored here against a known-good example setup first.

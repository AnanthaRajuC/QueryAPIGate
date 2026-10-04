# How to get the exact CLI command for a saved query

**Time:** 2 minutes. **You'll end up with:** a ready-to-run `queryapigate export` command for one saved query - its
name, parameters and a dated output path filled in - copied from the Console instead of written by hand.

## Step 1: Open the query's CLI tab

**API Repository → (the query) → CLI**. For `example_rentals_since` from the
[example APIs](05-try-the-built-in-example-apis.md):

```bash
queryapigate export 'example_rentals_since' \
  --param 'since=2000-01-01' \
  --format csv \
  --out '/exports/example_rentals_since_{date}.csv'
```

**Copy command** puts it on the clipboard.

How each line is built:

- **`--param`** - one per parameter a caller supplies. A parameter with a `default` shows that default (`since`
  above); one without shows a placeholder such as `'film_id=<film_id>'`, and the tab reminds you to replace it.
  Parameters filled from a signed-in user's token (`from_claim`) are left out - there's no token on the command line.
- **`--format csv`** - change it to `tsv` or `ndjson` if you prefer.
- **`--out`** - `/exports/<name>_{date}.csv`; `{date}` becomes today's date (`YYYY-MM-DD`) when it runs.

The tab is for the **published** version. On a draft or an older version it says so instead - `queryapigate export`
always runs the published one.

## Step 2: Run it where the store is

The command needs no server and no API key, but it does need the store: run it on the machine (or in the container)
where `QUERYAPIGATE_HOME` - or `QUERYAPIGATE_DATABASE_URL` - points at the same data the server uses:

```bash
QUERYAPIGATE_HOME=/srv/queryapigate queryapigate export 'example_rentals_since' \
  --param 'since=2026-10-01' --format csv --out '/exports/example_rentals_since_{date}.csv'
# Wrote 604 rows to /exports/example_rentals_since_2026-10-04.csv
```

In Docker: `docker compose exec queryapigate queryapigate export ...` (and choose an `--out` inside a mounted volume,
so the file outlives the container).

## The same, for curl

The **Curl** tab next to it gives the equivalent HTTP request - the same parameters, against `/q/<name>` with an API
key. For an export, add `stream=true` so the whole result comes back in one response instead of a page:

```bash
curl 'http://127.0.0.1:5000/q/example_rentals_since?since=2026-10-01&stream=true&format=csv' \
  -H 'X-API-Key: <a key that can run it>' -o rentals.csv
```

Use the CLI on the server's own machine (cron, systemd); use curl from anywhere else, with a scoped key.

## Next steps

- [Run a scheduled export with cron, systemd or a Kubernetes CronJob](30-schedule-an-export.md) - what to do with this
  command next, and how failures are reported.
- [Export a large result without running out of memory](09-stream-a-large-export.md).

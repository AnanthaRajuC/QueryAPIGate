# How to serve a lakehouse through Trino, to apps and AI agents

**Time:** 15 minutes, with Docker. **You'll end up with:** Iceberg tables on S3-compatible storage, read through
Trino and joined with a PostgreSQL database, served as governed REST endpoints and MCP tools. An AI agent gets hashed
emails and three tables, a partner's app gets one endpoint, and repeat calls skip Trino entirely.

> Trino connections are **experimental** ([ADR 0026](../documentation/adr/0026-trino-connections.md)): what they cover
> and their limits are in [Trino connections](../documentation/DATABASE_CONNECTION_CONFIGURATION.md#trino-connections).
> [Connect to Trino](61-connect-to-trino.md) is the same connection, step by step, against a Trino you already run.

Every command and result below was run against [`deploy/lakehouse`](https://github.com/AnanthaRajuC/QueryAPIGate/tree/main/deploy/lakehouse),
on one laptop.

## Step 1: Start it

```bash
docker compose -f deploy/lakehouse/docker-compose.yml up --build -d --wait
```

The first run builds QueryAPIGate's image from the repository; after that it takes about 35 seconds. It starts:

| Service | What it is |
|---|---|
| `storage` | S3-compatible object storage (SeaweedFS). The `lakehouse` bucket holds the tables' Parquet files. |
| `postgres` | The Iceberg catalog (which tables exist, where their metadata is), and `crm`, an operational database. |
| `trino` | Trino 484, with an `iceberg` catalog over the two above and a `crm` catalog over PostgreSQL. Its UI is on <http://127.0.0.1:8085>. |
| `seed` | Writes `iceberg.sales.orders` (15,000 orders, partitioned by month) and `iceberg.sales.customers` from Trino's built-in TPC-H data, then exits. |
| `queryapigate` | The REST API on <http://127.0.0.1:5000>, configured from [`queryapigate.yaml`](https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/deploy/lakehouse/queryapigate.yaml) at startup (`QUERYAPIGATE_CONFIG_FILE`). |
| `mcp` | The MCP server on <http://127.0.0.1:5001/mcp>, with the same store, keys and history. |

`queryapigate.yaml` holds everything QueryAPIGate serves: one Trino connection, three saved queries and two scoped
keys. The connection's `database` is the Trino catalog (`iceberg`), and `options.schema` is its default schema
(`sales`). Tables in other catalogs are named in full, as `crm.public.accounts`.

## Step 2: A lakehouse table as an endpoint

```bash
curl 'http://127.0.0.1:5000/q/revenue_by_month?year=1995&page_size=3' -H 'X-API-Key: lakehouse-admin'
```
```json
[{"month":"1995-01-01","orders":165,"revenue":23852700.49},{"month":"1995-02-01","orders":172,"revenue":24978868.07},{"month":"1995-03-01","orders":181,"revenue":24248460.26}]
```

`year` is a typed parameter (an integer from 1992 to 1998), sent to Trino as a bound parameter, never pasted into the
SQL. The orders table is partitioned by month, so Trino reads only that year's: 12 of its 80 partitions, 2,204 of
15,000 rows (`EXPLAIN ANALYZE` in Trino shows it).

**Repeat calls don't reach Trino.** Each call costs Trino 100 to 280 ms here, most of it planning. Each saved query
sets `cache_ttl: 60`, so a repeat within a minute is answered from QueryAPIGate's response cache:

```text
year 1993:  0.210 s (X-Cache: MISS)   0.0024 s (HIT)   0.0022 s (HIT)   0.0025 s (HIT)
```

A lakehouse table usually changes in batches, so a cache time a little shorter than the batch interval rarely serves
stale data.

## Step 3: The lakehouse and PostgreSQL in one query

`top_accounts` joins the Iceberg tables with `crm.public.accounts` in PostgreSQL, in one statement that Trino runs:

```sql
SELECT c.name AS customer, c.nation, a.account_manager, a.tier, a.contact_email, count(*) AS orders, ...
FROM orders o JOIN customers c ON o.custkey = c.custkey JOIN crm.public.accounts a ON a.custkey = o.custkey
WHERE year(o.orderdate) = :year ...
```

Called with the agent's key, every contact email comes back hashed:

```bash
curl 'http://127.0.0.1:5000/q/top_accounts?year=1995&page_size=2' -H 'X-API-Key: lakehouse-agent-key'
```
```json
[{"customer":"Customer#000000349","nation":"UNITED KINGDOM","account_manager":"Emil Larsen","tier":"silver","contact_email":"6f3dba5952272d9c","orders":12,"revenue":1861780.42},
 {"customer":"Customer#000000814","nation":"MOROCCO","account_manager":"Emil Larsen","tier":"standard","contact_email":"bc20b8dfde9544ae","orders":7,"revenue":1418923.15}]
```

With the admin key, the same row shows `buyer349@customer58.example.com`. The rule is on the agent's role,
`{column: "*.contact_email", action: hash}`, and it's traced through the join to the source column. A hash stays the
same for the same value on this server, so the agent can still count and group by it.

## Step 4: What each key can't do

The agent's role reaches the connection, but only three tables:
`allowed_tables: [sales.orders, sales.customers, crm.public.accounts]`. On Trino these names are matched in full, as
`catalog.schema.table`, so a grant can't reach a table of the same name in another catalog:

```bash
curl -X POST http://127.0.0.1:5000/execute_sql -H 'X-API-Key: lakehouse-agent-key' -H 'Content-Type: application/json' \
  -d '{"connection_name": "lakehouse", "sql": "SELECT count(*) AS n FROM tpch.tiny.lineitem"}'
```
```json
{"error":"This API key may only query these tables: crm.public.accounts, iceberg.sales.customers, iceberg.sales.orders. Forbidden: tpch.tiny.lineitem.","code":"table_not_allowed"}
```

Nothing writes: `DELETE FROM orders` is refused before Trino sees it (`read_only`). The partner's key runs
`revenue_by_month` and nothing else, so `/q/top_accounts` answers `connection_forbidden`.

## Step 5: The same queries as MCP tools

The agent connects to the MCP server with its own key (the [whole script](24-let-an-agent-call-your-queries-via-mcp.md)
is in guide 24):

```python
async with streamablehttp_client('http://127.0.0.1:5001/mcp', headers={'X-API-Key': 'lakehouse-agent-key'}) as (r, w, _):
    async with ClientSession(r, w) as session:
        await session.initialize()
        print([t.name for t in (await session.list_tools()).tools])
        result = await session.call_tool('execute_sql', {'connection_name': 'lakehouse', 'sql':
            'SELECT a.tier, count(*) AS orders FROM orders o JOIN crm.public.accounts a ON a.custkey = o.custkey '
            'GROUP BY 1 ORDER BY 2 DESC'})
        print(result.structuredContent['rows'])
```
```text
['revenue_by_month', 'revenue_by_region', 'top_accounts', 'list_tables', 'execute_sql', 'describe_table']
[{'tier': 'standard', 'orders': 10565}, {'tier': 'silver', 'orders': 2939}, {'tier': 'gold', 'orders': 1496}]
```

Each saved query is a tool, with the parameters it declares. Ad-hoc SQL over MCP is always read-only and bound by the
same `allowed_tables` and masking, and every call is in the same run history as the REST ones.

## Step 6: Point it at your own lakehouse

- **The catalog.** This example's Iceberg catalog is JDBC on PostgreSQL. Trino reads a REST catalog (Polaris,
  Lakekeeper, Nessie), AWS Glue or a Hive metastore the same way, and Delta Lake and Hive tables through their own
  connectors. QueryAPIGate sees none of that: to it, each is a Trino catalog.
- **A scoped Trino user.** A connection reaches whatever its Trino user can. Give QueryAPIGate a read-only user,
  limited to the catalogs and schemas it serves, and let `allowed_tables` narrow from there
  ([guide 61](61-connect-to-trino.md#the-outer-boundary-a-scoped-trino-user)).
- **Secrets.** Here the passwords are in the open and every port is bound to 127.0.0.1. With TLS
  (`http_scheme: https`), the Trino password is a `${VAR}` reference in the file, never a value.
- **Time limits.** `QUERYAPIGATE_QUERY_TIMEOUT`, or a key's own `query_timeout`, cancels a slow query on the Trino
  cluster, not only in QueryAPIGate.

Clean up with `docker compose -f deploy/lakehouse/docker-compose.yml down -v`.

## Next steps

- [Connect to Trino](61-connect-to-trino.md) - the connection's fields and switching catalogs.
- [Hide personal data from a partner or an AI agent](45-hide-personal-data-from-a-partner-or-an-ai-agent.md) - every
  masking action.
- [Configuration as code](../documentation/API.md#configuration-as-code) - the file this example starts from.

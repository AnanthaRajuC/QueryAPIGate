# A tour of the Console

The Console is at `/console` on every QueryAPIGate server - nothing separate to install. Each screen below, with what
it's for; click one for the full size. The [README](https://github.com/AnanthaRajuC/QueryAPIGate#readme) has the
SQL editor, the demo video and the architecture.

<p align="center">
  <a href="https://raw.githubusercontent.com/AnanthaRajuC/QueryAPIGate/main/documentation/screenshots/home.png">
    <img src="https://raw.githubusercontent.com/AnanthaRajuC/QueryAPIGate/main/documentation/screenshots/home.png"
         alt="Home: stat tiles for connections, the API Repository, API keys, roles, requests and error rate, System health listing the current alerts (an expiring key, a key over its rate limit, a failing query), recent audit activity, quick actions, and recent ad-hoc and saved-query requests" width="820">
  </a>
  <br>
  <sub>Home: live totals, what needs attention, recent activity, and every recent request - saved APIs and ad-hoc SQL alike.</sub>
</p>

<table>
<tr>
<td width="50%">
<a href="https://raw.githubusercontent.com/AnanthaRajuC/QueryAPIGate/main/documentation/screenshots/sign-in.png">
<img src="https://raw.githubusercontent.com/AnanthaRajuC/QueryAPIGate/main/documentation/screenshots/sign-in.png" alt="The Console's sign-in page: a centred card with the QueryAPIGate wordmark, the heading Sign in to the Console, a field for an admin token or API key, a Sign in button, and a note on where tokens come from">
</a>
<br><sub>Signed out, a tab sees only the sign-in page - Sign in with SSO when it's set up, or an admin token</sub>
</td>
<td width="50%">
<a href="https://raw.githubusercontent.com/AnanthaRajuC/QueryAPIGate/main/documentation/screenshots/sign-in-dark.png">
<img src="https://raw.githubusercontent.com/AnanthaRajuC/QueryAPIGate/main/documentation/screenshots/sign-in-dark.png" alt="The same sign-in page in the dark theme">
</a>
<br><sub>The same page in the dark theme, following the operating system's</sub>
</td>
</tr>
</table>

<p align="center">
  <a href="https://raw.githubusercontent.com/AnanthaRajuC/QueryAPIGate/main/documentation/screenshots/mcp.png">
    <img src="https://raw.githubusercontent.com/AnanthaRajuC/QueryAPIGate/main/documentation/screenshots/mcp.png"
         alt="The MCP server screen: your saved queries listed as tools an AI agent can call over MCP, each marked read-only with its parameters and description and a 30-day MCP-activity column; above the list, the connect-a-client config snippet with the MCP server URL and X-API-Key header, and a reachability check" width="820">
  </a>
  <br>
  <sub>MCP server: your saved queries as governed tools for AI agents - read-only by default, with the connect snippet and a reachability check, the same identity, masking and audit as every other caller.</sub>
</p>

<p align="center">
  <a href="https://raw.githubusercontent.com/AnanthaRajuC/QueryAPIGate/main/documentation/screenshots/workspaces.png">
    <img src="https://raw.githubusercontent.com/AnanthaRajuC/QueryAPIGate/main/documentation/screenshots/workspaces.png"
         alt="The Workspaces screen (experimental): three workspaces - catalogue, default (this tab's) and payments - with their descriptions, member counts and Work here, edit and remove actions; below, the members of payments, bea as owner and dmitri as developer, each role changeable, and a form to add a member. The sidebar's workspace switcher sits under the wordmark" width="820">
  </a>
  <br>
  <sub>Workspaces (experimental): teams on one server, each running its own connections, queries and keys - with the switcher under the wordmark, and each workspace's members.</sub>
</p>

<table>
<tr>
<td width="25%">
<a href="https://raw.githubusercontent.com/AnanthaRajuC/QueryAPIGate/main/documentation/screenshots/connections.png">
<img src="https://raw.githubusercontent.com/AnanthaRajuC/QueryAPIGate/main/documentation/screenshots/connections.png" alt="Connections screen with All/Active/Inactive/Deleted tabs, listing ClickHouse, MySQL, PostgreSQL and SQLite connections with their status, each connection's live usage (queries run, failures, average latency), a 30-day activity sparkline per connection, and Query, edit and delete actions">
</a>
<br><sub>Manage connections across every supported database, with live usage per connection</sub>
</td>
<td width="25%">
<a href="https://raw.githubusercontent.com/AnanthaRajuC/QueryAPIGate/main/documentation/screenshots/saved-queries.png">
<img src="https://raw.githubusercontent.com/AnanthaRajuC/QueryAPIGate/main/documentation/screenshots/saved-queries.png" alt="The API Repository: saved queries grouped into collapsible collections, each with its key count and Postman/Rename actions, beside the selected query's connection, collection, Run/SQL/History/Curl tabs and generated GET /q/ endpoint">
</a>
<br><sub>Every saved query becomes a documented REST endpoint - grouped into collections</sub>
</td>
<td width="25%">
<a href="https://raw.githubusercontent.com/AnanthaRajuC/QueryAPIGate/main/documentation/screenshots/api-keys.png">
<img src="https://raw.githubusercontent.com/AnanthaRajuC/QueryAPIGate/main/documentation/screenshots/api-keys.png" alt="API keys screen showing keys created from roles and scoped to a collection (shown as a dashed name/ tag), an expiring partner key, per-key rate limits, each key's last-used time and live usage, and a 30-day activity sparkline per key">
</a>
<br><sub>Per-key permissions: connections, specific saved queries, expiry, rate limits - and what each key has actually done</sub>
</td>
<td width="25%">
<a href="https://raw.githubusercontent.com/AnanthaRajuC/QueryAPIGate/main/documentation/screenshots/roles.png">
<img src="https://raw.githubusercontent.com/AnanthaRajuC/QueryAPIGate/main/documentation/screenshots/roles.png" alt="Roles screen listing reusable permission templates - connections, read/write access, rate limit and IP allowlisting - with a 30-day activity sparkline for the keys created from each role and a New key from this shortcut">
</a>
<br><sub>Named roles: a reusable grant template, copied onto a key once at creation</sub>
</td>
</tr>
</table>

<table>
<tr>
<td width="33%">
<a href="https://raw.githubusercontent.com/AnanthaRajuC/QueryAPIGate/main/documentation/screenshots/saved-query-history.png">
<img src="https://raw.githubusercontent.com/AnanthaRajuC/QueryAPIGate/main/documentation/screenshots/saved-query-history.png" alt="A saved query's History tab: every run with its time, calling API key, rows, duration and request ID, with a status filter and search box">
</a>
<br><sub>Run history per saved query: who called it, how long it took, and the request ID to find it in the logs</sub>
</td>
<td width="33%">
<a href="https://raw.githubusercontent.com/AnanthaRajuC/QueryAPIGate/main/documentation/screenshots/audit-log.png">
<img src="https://raw.githubusercontent.com/AnanthaRajuC/QueryAPIGate/main/documentation/screenshots/audit-log.png" alt="Audit log screen listing every administrative change - saved queries, roles and API keys created or updated - with an action filter and search box">
</a>
<br><sub>A durable audit log of every configuration change, filterable by action, actor and target</sub>
</td>
<td width="33%">
<a href="https://raw.githubusercontent.com/AnanthaRajuC/QueryAPIGate/main/documentation/screenshots/metrics.png">
<img src="https://raw.githubusercontent.com/AnanthaRajuC/QueryAPIGate/main/documentation/screenshots/metrics.png" alt="Metrics screen with stat tiles for requests, error rate, active queries, pool and rate limiting, bars for requests by status and queries by connection, and a per-connection latency table">
</a>
<br><sub>Live metrics with no Prometheus required - or import the bundled Grafana dashboard for history</sub>
</td>
</tr>
</table>

<table>
<tr>
<td width="50%">
<a href="https://raw.githubusercontent.com/AnanthaRajuC/QueryAPIGate/main/documentation/screenshots/collections-move.png">
<img src="https://raw.githubusercontent.com/AnanthaRajuC/QueryAPIGate/main/documentation/screenshots/collections-move.png" alt="The Move to a collection drawer: choosing a collection previews which API keys will gain access to the query and which will lose it, and which roles will include it">
</a>
<br><sub>Collections: grant a key a whole group of queries - and see exactly which keys gain or lose access before a query moves</sub>
</td>
<td width="50%">
<a href="https://raw.githubusercontent.com/AnanthaRajuC/QueryAPIGate/main/documentation/screenshots/caching.png">
<img src="https://raw.githubusercontent.com/AnanthaRajuC/QueryAPIGate/main/documentation/screenshots/caching.png" alt="Caching screen: response cache backend, entry count, hit rate, hits and misses, and the list of saved queries with a cache_ttl set, each with a 30-day activity sparkline">
</a>
<br><sub>Caching: live hit rate for the response cache backing every saved query's <code>cache_ttl</code></sub>
</td>
</tr>
</table>

<table>
<tr>
<td width="33%">
<a href="https://raw.githubusercontent.com/AnanthaRajuC/QueryAPIGate/main/documentation/screenshots/alerts.png">
<img src="https://raw.githubusercontent.com/AnanthaRajuC/QueryAPIGate/main/documentation/screenshots/alerts.png" alt="Alerts screen with a tab per check - key expiry, unused keys, failing connections, query errors, slow queries, rate limits, open access and run history - listing a critical failing connection and warnings for an expiring key, a key over its rate limit and a failing query, each with what to do and a link to fix it">
</a>
<br><sub>Alerts: failing connections, expiring keys, failing or slow queries, rate limits being hit - each with what to do, clearing by itself once fixed</sub>
</td>
<td width="33%">
<a href="https://raw.githubusercontent.com/AnanthaRajuC/QueryAPIGate/main/documentation/screenshots/access-map.png">
<img src="https://raw.githubusercontent.com/AnanthaRajuC/QueryAPIGate/main/documentation/screenshots/access-map.png" alt="Access map: every saved query against every API key, showing whether each key reaches it through a named query, a collection or a whole connection, with filters by connection, database and reach">
</a>
<br><sub>Access map: which key can call which query, and why - one grid instead of reading every grant</sub>
</td>
<td width="33%">
<a href="https://raw.githubusercontent.com/AnanthaRajuC/QueryAPIGate/main/documentation/screenshots/settings.png">
<img src="https://raw.githubusercontent.com/AnanthaRajuC/QueryAPIGate/main/documentation/screenshots/settings.png" alt="Settings: the server's effective configuration by section, and this browser's Appearance preferences - theme, font size, time zone, time format, table density and reduced motion">
</a>
<br><sub>Settings: the server's effective configuration, read-only, plus per-browser appearance and editor preferences</sub>
</td>
</tr>
</table>

<table>
<tr>
<td width="50%">
<a href="https://raw.githubusercontent.com/AnanthaRajuC/QueryAPIGate/main/documentation/screenshots/help.png">
<img src="https://raw.githubusercontent.com/AnanthaRajuC/QueryAPIGate/main/documentation/screenshots/help.png" alt="Help: the how-to guides browsed inside the Console, with an On this page list of the guide's sections">
</a>
<br><sub>Help: the docs and how-to guides for the version you run, inside the Console</sub>
</td>
<td width="50%">
<a href="https://raw.githubusercontent.com/AnanthaRajuC/QueryAPIGate/main/documentation/screenshots/home-dark.png">
<img src="https://raw.githubusercontent.com/AnanthaRajuC/QueryAPIGate/main/documentation/screenshots/home-dark.png" alt="Home in the dark theme, with the font size and light/dark switches in the top right corner">
</a>
<br><sub>Light or dark, three font sizes - switched from the top right corner</sub>
</td>
</tr>
</table>

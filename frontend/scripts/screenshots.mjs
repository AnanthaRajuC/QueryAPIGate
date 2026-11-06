// Retakes the documentation screenshots (documentation/screenshots/*.png) of the Console.
//
//   npm run build && QUERYAPIGATE_BIN=../.venv/bin/queryapigate npm run screenshots
//
// Starts a throwaway `queryapigate serve` (fresh home, the example APIs loaded), seeds it through /api/v1 with a
// realistic estate - more connections, keys from roles, traffic, one connection that is down and a key that keeps
// hitting its rate limit, so Alerts has something real to show; an agent over MCP (with `queryapigate mcp` beside it
// for a while), administrators and an export - then photographs each screen at 1400px wide, 2x. The file names are
// the ones README.md, documentation/SCREENSHOTS.md and the how-to guides (guide-NN-*.png) link to; keep them.
import { spawn } from 'node:child_process';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { DatabaseSync } from 'node:sqlite';

import { chromium } from '@playwright/test';

const PORT = process.env.PORT ?? '5088';
const BASE = `http://127.0.0.1:${PORT}`;
const ADMIN = 'demo-admin';
const OUT = process.env.OUT ?? new URL('../../documentation/screenshots/', import.meta.url).pathname;

const MCP_PORT = String(Number(PORT) + 1);
const MCP = `http://127.0.0.1:${MCP_PORT}/mcp/`;

const home = mkdtempSync(join(tmpdir(), 'queryapigate-shots-'));
const exportsDir = mkdtempSync(join(tmpdir(), 'queryapigate-shots-exports-')); // never inside the home
const dataDir = mkdtempSync(join(tmpdir(), 'queryapigate-shots-data-'));
const env = {
  ...process.env,
  QUERYAPIGATE_HOME: home,
  QUERYAPIGATE_API_KEY: ADMIN,
  QUERYAPIGATE_LOAD_EXAMPLES: '1',
  // For the guides: a write an agent asks for and a person approves (49), an export to a folder (44), and the port
  // of the MCP server the agent calls (47).
  QUERYAPIGATE_ALLOW_WRITES: '1',
  QUERYAPIGATE_CONSOLE_URL: `http://localhost:${PORT}`,
  QUERYAPIGATE_EXPORT_DIRS: exportsDir,
  QUERYAPIGATE_MCP_PORT: MCP_PORT,
};
const bin = process.env.QUERYAPIGATE_BIN ?? 'queryapigate';
const server = spawn(bin, ['serve', '--port', PORT], { stdio: 'ignore', env });
let mcpServer = null;
const stop = () => {
  server.kill('SIGTERM');
  mcpServer?.kill('SIGTERM');
  rmSync(home, { recursive: true, force: true });
  rmSync(exportsDir, { recursive: true, force: true });
  rmSync(dataDir, { recursive: true, force: true });
};
process.on('exit', stop);

async function call(method, path, body, key = ADMIN) {
  const res = await fetch(BASE + path, {
    method,
    headers: { 'X-API-Key': key, 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await res.text();
  return { status: res.status, body: text ? JSON.parse(text) : null };
}

const day = (offset) => new Date(Date.now() + offset * 86_400_000).toISOString().slice(0, 10);

async function waitUp(base = BASE) {
  for (let i = 0; i < 100; i++) {
    try {
      if ((await fetch(base + '/health')).ok) return;
    } catch {
      // not up yet
    }
    await new Promise((r) => setTimeout(r, 300));
  }
}

// One MCP tool call, as an agent's client sends it over Streamable HTTP; returns the tool's own JSON result.
async function mcpCall(key, name, args) {
  const res = await fetch(MCP, {
    method: 'POST',
    headers: {
      'X-API-Key': key,
      'Content-Type': 'application/json',
      Accept: 'application/json, text/event-stream',
    },
    body: JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'tools/call', params: { name, arguments: args } }),
  });
  const data = (await res.text()).split('\n').find((line) => line.startsWith('data: '));
  try {
    return JSON.parse(JSON.parse(data.slice(6)).result.content[0].text);
  } catch {
    return null;
  }
}

let approvalId = null;

async function seed() {
  // More of an estate around the examples: databases listed but never connected to, and one that is down.
  const connection = (name, details) =>
    call('POST', '/api/v1/connections', { name, active: true, ...details });
  await connection('analytics', {
    db: 'clickhouse',
    host: 'clickhouse.internal',
    port: 8123,
    database: 'events',
    user: 'reporting',
  });
  await connection('warehouse', {
    db: 'postgres',
    host: 'warehouse.internal',
    port: 5432,
    database: 'warehouse',
    user: 'svc_reporting',
  });
  await connection('legacy_orders', {
    db: 'mysql',
    host: '127.0.0.1',
    port: 1,
    database: 'orders',
    user: 'orders_ro',
  });
  await call('POST', '/api/v1/queries', {
    name: 'open_orders',
    description: 'Orders not yet shipped, oldest first',
    sql: "SELECT id, customer_id, placed_at FROM orders WHERE status = 'open' ORDER BY placed_at",
    connection_name: 'legacy_orders',
    collection: 'operations',
    publish: true,
  });
  // Keys from the example roles, one expiring soon, and a scoped analyst key with a tight rate limit.
  const key = async (body) => (await call('POST', '/api/v1/api-keys', body)).body?.secret;
  const acme = await key({ name: 'acme-corp', role: 'example-partner', expires_at: day(3) });
  const finance = await key({ name: 'finance-team', role: 'example-reporting' });
  const wall = await key({ name: 'dashboard-wall', role: 'example-dashboard' });
  await key({ name: 'nightly-export', role: 'example-export', expires_at: day(60) });
  const analyst = await key({ name: 'internal-analyst', connections: ['examples'], rate_limit: '5/minute' });
  await key({ name: 'temp-migration', connections: ['warehouse'] });
  await call('DELETE', '/api/v1/api-keys/temp-migration');
  // Traffic: each key using what it is for, the down connection failing, the analyst over its limit.
  const run = (path, k) => fetch(BASE + path, { headers: { 'X-API-Key': k } });
  for (let id = 1; id <= 8; id++) await run(`/q/example_film_lookup?film_id=${id}`, acme);
  for (const m of [3, 6, 12]) await run(`/q/example_monthly_revenue?months=${m}`, finance);
  for (let i = 0; i < 5; i++) {
    await run('/q/example_kpi_rentals_today', wall);
    await run('/q/example_kpi_active_rentals', wall);
  }
  for (const n of [5, 10, 10, 5, 4]) await run(`/q/example_top_films?top_n=${n}`, finance);
  for (let i = 0; i < 12; i++) await run('/q/open_orders', ADMIN);
  for (let i = 0; i < 16; i++) {
    await fetch(BASE + '/execute_sql', {
      method: 'POST',
      headers: { 'X-API-Key': analyst, 'Content-Type': 'application/json' },
      body: JSON.stringify({ sql: 'SELECT COUNT(*) AS films FROM film', connection_name: 'examples' }),
    });
  }
  // AI agents over MCP (guides 47 and 49): one reading - saved queries and ad-hoc SQL - and guide 49's support bot,
  // asking to close a ticket in its helpdesk database, a write that waits for a person.
  const helpdesk = new DatabaseSync(join(dataDir, 'helpdesk.db'));
  helpdesk.exec(
    "CREATE TABLE tickets (id INTEGER PRIMARY KEY, customer TEXT NOT NULL, title TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open');" +
      "INSERT INTO tickets (customer, title) VALUES ('acme', 'Invoice shows the wrong VAT'), " +
      "('globex', 'Export file is empty'), ('acme', 'Cannot reset my password');",
  );
  helpdesk.close();
  await connection('helpdesk', { db: 'sqlite', database: join(dataDir, 'helpdesk.db') });
  await call('POST', '/api/v1/queries', {
    name: 'close_ticket',
    connection_name: 'helpdesk',
    description: 'Close a ticket',
    publish: true,
    sql: "UPDATE tickets SET status = 'closed' WHERE id = :id",
    parameters: { id: { type: 'integer', min: 1 } },
    agent_writes: 'approval',
  });
  const agent = await key({ name: 'rentals-assistant', connections: ['examples'] });
  const supportBot = await key({
    name: 'support-bot',
    connections: [],
    queries: [{ name: 'close_ticket', allow_writes: true }],
  });
  mcpServer = spawn(bin, ['mcp'], { stdio: 'ignore', env });
  await waitUp(`http://127.0.0.1:${MCP_PORT}`);
  for (const n of [5, 10]) await mcpCall(agent, 'example_top_films', { top_n: n });
  for (const id of [3, 7, 12]) await mcpCall(agent, 'example_film_lookup', { film_id: id });
  for (const sql of [
    'SELECT category, COUNT(*) AS films FROM film GROUP BY category ORDER BY films DESC',
    'SELECT title, rental_rate FROM film ORDER BY rental_rate DESC LIMIT 5',
    'SELECT title, rental_rate FROM film ORDER BY rental_rate DESC LIMIT 5',
  ]) {
    await mcpCall(agent, 'execute_sql', { connection_name: 'examples', sql });
  }
  approvalId = (await mcpCall(supportBot, 'close_ticket', { id: 2 }))?.request ?? null;
  // Its part done, the MCP server stops - once its runs are in run history, which it writes in batches - and this
  // one waits until it no longer counts it as running beside it (instances.ALIVE, 90 seconds), or Alerts would warn
  // about two processes sharing a store without Redis.
  for (let i = 0; i < 50; i++) {
    const agents = (await call('GET', '/api/v1/usage/agents')).body?.items ?? [];
    if ((agents.find((a) => a.agent === 'rentals-assistant')?.calls ?? 0) >= 8) break;
    await new Promise((r) => setTimeout(r, 200));
  }
  mcpServer.kill('SIGTERM');
  mcpServer = null;
  for (let i = 0; i < 60; i++) {
    if (!JSON.stringify((await call('GET', '/api/v1/alerts')).body).includes('instances_not_shared')) break;
    await new Promise((r) => setTimeout(r, 3000));
  }
  // The team's own administrators (guide 43).
  for (const [name, role] of [
    ['alice', 'owner'],
    ['bob', 'developer'],
    ['carol', 'auditor'],
  ]) {
    await call('POST', '/api/v1/administrators', { name, role, email: `${name}@corp.example` });
    await call('POST', `/api/v1/administrators/${name}/tokens`, { label: 'laptop', expires_at: day(90) });
  }
  // A daily file for a partner (guide 44), to a folder rather than a bucket, run once.
  await call('POST', '/api/v1/destinations', { name: 'acme', url: exportsDir + '/acme/' });
  await call('POST', '/api/v1/exports', {
    name: 'acme-rentals',
    query: 'example_rentals_since',
    destination: 'acme',
    format: 'csv',
    path: 'rentals/{date}/rentals_{run}.csv',
    params: { since: '2026-01-01' },
  });
  await call('POST', '/api/v1/exports/acme-rentals/runs');
}

await waitUp();

const browser = await chromium.launch();
const context = await browser.newContext({ viewport: { width: 1400, height: 900 }, deviceScaleFactor: 2 });
await context.addInitScript((key) => {
  try {
    sessionStorage.setItem('queryapigate-key', key);
    localStorage.setItem('queryapigate-ui-star-cta-dismissed', '1'); // the Star card would cover the sidebar
  } catch {
    // storage unavailable
  }
}, ADMIN);
const page = await context.newPage();
const errors = [];
page.on('pageerror', (e) => errors.push(e.message));
const wait = (ms) => page.waitForTimeout(ms);
const go = async (path, ready) => {
  await page.goto(BASE + '/console' + path);
  if (ready) await page.waitForSelector(ready, { timeout: 15_000 });
  await wait(700);
};
const shot = async (name, height = 900, width = 1400) => {
  await page.setViewportSize({ width, height });
  await wait(400);
  await page.mouse.move(0, 0);
  await page.screenshot({ path: OUT + name + '.png' });
  console.log('wrote', name + '.png');
};

// Guide 01 (how-to/01-turn-your-first-sql-query-into-a-rest-api.md), before the estate below is seeded: the server
// as its reader has it right after `examples load` - one connection, and the guide's own query.
await go('/connections', 'table.grid tbody tr');
await shot('guide-01-connections', 560);
await go('/designer', '.cm-content');
await page.selectOption('#run-connection', 'examples');
await page.locator('.cm-content').first().click();
await page.keyboard.press('Control+a');
await page.keyboard.type(
  'SELECT film_id, title, rating, rental_rate\nFROM film\nWHERE rating = :rating\nORDER BY title',
);
await page.keyboard.press('Escape'); // close the completion popup
await page.locator('.runner-side').getByRole('tab', { name: 'Schema' }).click();
await wait(700);
await shot('guide-01-api-designer', 820);

await seed();

await go('/', '#home-health .home-activity-row');
await shot('home', 1250);

await go('/alerts', '.alert-row');
await shot('alerts', 1000);

await go('/connections', 'table.grid tbody tr');
await shot('connections', 720, 1820); // wide enough for every column and the row actions

await go('/queries/example_top_films', '.subtabs');
await shot('saved-queries');
await page.locator('[data-subtab="history"]').click();
await wait(700);
await shot('saved-query-history');

await page.getByRole('button', { name: 'Move…' }).click();
await page.waitForSelector('#mv-select');
await page.selectOption('#mv-select', 'examples-partner');
await wait(700);
await shot('collections-move');
await page.keyboard.press('Escape');

await go('/designer', '.cm-content');
await page.selectOption('#run-connection', 'examples');
await page.locator('.cm-content').first().click();
await page.keyboard.press('Control+a');
await page.keyboard.type(
  '-- Films rented at least :min_rentals times\nSELECT f.title, f.category, COUNT(*) AS rentals\nFROM rental r\n' +
    'JOIN film f ON f.film_id = r.film_id\nGROUP BY f.title, f.category\nHAVING COUNT(*) >= :min_rentals\n' +
    'ORDER BY rentals DESC',
);
await page.keyboard.press('Escape'); // close the completion popup
const side = page.locator('.runner-side');
await side.getByRole('tab', { name: 'Settings' }).click();
await page.fill('#run-params', '{"min_rentals": 5}');
await side.getByRole('tab', { name: 'Schema' }).click();
await page.click('#run-button');
await page.waitForSelector('table.rs tbody tr', { timeout: 15_000 });
await shot('run-sql', 1180);

await go('/api-keys', 'table.grid tbody tr');
await shot('api-keys', 760);
await go('/roles', 'table.grid tbody tr');
await shot('roles', 620);
await go('/access-map', 'table');
await shot('access-map', 900, 1820); // a column per key
await go('/caching', 'h1');
await shot('caching', 760);
await go('/mcp', '#mcp-tools table tbody tr');
await shot('mcp', 1240);
await go('/metrics', '.stat-tiles');
await shot('metrics');
await go('/audit-log', 'table.grid tbody tr');
await shot('audit-log', 1000);

// The how-to guides' own shots (guide-NN-*.png, in how-to/NN-*.md): the forms, tabs and screens the tour above
// doesn't show, each as the guide's step has it.
const drawer = page.locator('#drawer');
const blur = () => page.evaluate(() => document.activeElement?.blur());
await go('/connections', 'table.grid tbody tr');
await page.getByRole('button', { name: 'New connection' }).click();
await page.waitForSelector('#c-db');
await page.selectOption('#c-db', 'postgres');
await page.locator('#c-name').last().fill('orders');
await page.fill('#c-host', 'orders-db.internal');
await page.fill('#c-port', '5432');
await page.fill('#c-user', 'reporting');
await page.locator('#c-database').last().fill('orders');
await shot('guide-02-new-connection', 900);

await go('/designer', '.cm-content');
await page.selectOption('#run-connection', 'examples');
await page.locator('.runner-side').getByRole('tab', { name: 'Schema' }).click();
await page.locator('.runner-side').getByText('film', { exact: true }).first().click();
await page.locator('.cm-content').first().click();
await page.keyboard.press('Control+a');
await page.keyboard.type('SELECT title, rating\nFROM film\nWHERE ');
await page.keyboard.press('Escape'); // close the completion popup
await wait(700);
await shot('guide-11-schema-columns', 820);

await go('/api-keys', 'table.grid tbody tr');
await page.getByRole('button', { name: 'New API key' }).first().click();
await page.waitForSelector('#k-name');
await page.fill('#k-name', 'acme-reporting');
await page.fill('#k-expires', day(90));
await blur();
await shot('guide-19-new-api-key', 900);
await page.fill('#k-max-concurrent', '2');
await page.fill('#k-query-timeout', '30');
await page.fill('#k-max-rows', '5000');
await page.fill('#k-quota-calls', '10000');
await page.locator('#k-max-concurrent').evaluate((el) => el.scrollIntoView({ block: 'center' }));
await blur();
await wait(300);
await shot('guide-52-key-limits', 900);
await drawer.getByRole('button', { name: 'Add a masking rule' }).click();
await page.getByLabel('Masked column 1').fill('customer.email');
await page.getByLabel('Masking action 1').selectOption('mask');
await page.getByLabel('Masked column 1').evaluate((el) => el.scrollIntoView({ block: 'center' }));
await blur();
await wait(300);
await shot('guide-45-column-masking', 900);

await go('/api-keys', 'table.grid tbody tr');
await page.locator('tr', { hasText: 'finance-team' }).getByRole('button', { name: 'Edit' }).click();
await page.waitForSelector('#k-grace');
await page.locator('#k-grace').evaluate((el) => el.scrollIntoView({ block: 'center' }));
await wait(300);
await shot('guide-50-rotate-key', 900);

await go('/api-keys/acme-corp', 'h1');
await shot('guide-21-key-details', 1100);

await go('/queries/example_rentals_since', '.subtabs');
await page.locator('[data-subtab="cli"]').click();
await page.locator('.subtabs').evaluate((el) => el.scrollIntoView({ block: 'start' }));
await wait(700);
await shot('guide-31-cli-tab', 900);

await go('/administrators', 'table.grid tbody tr');
await shot('guide-43-administrators', 720);

await go('/exports', 'table.grid tbody tr');
await shot('guide-44-exports', 620);
await page.getByRole('button', { name: 'Runs' }).first().click();
await page.waitForSelector('#export-runs tbody tr');
await shot('guide-44-export-runs', 700);

await go('/agents', 'table.grid tbody tr');
await page.locator('table.grid tbody tr button').first().click();
await wait(700);
await shot('guide-47-agents', 1000);

if (approvalId) {
  await go(`/approvals/${approvalId}`, '#drawer');
  await shot('guide-49-approval', 900);
} else {
  console.log('no approval request: guide-49-approval not taken');
}

await go('/settings', '#settings-nav');
await page.getByRole('button', { name: 'Appearance' }).click();
await wait(400);
await shot('settings', 820);

await go('/help', '#help-docs');
// How-to guides: the docs come from GitHub at the server's release tag, and the guides change least between releases
await page.getByRole('button', { name: 'How-to guides' }).click();
await page.locator('#docs-nav').getByRole('button', { name: 'Your first SQL-to-API' }).click();
await page
  .waitForSelector('#docs-toc a', { timeout: 20_000 })
  .catch(() => console.log('help: docs not reachable'));
await shot('help', 900);

await context.addInitScript(() => {
  try {
    localStorage.setItem('queryapigate-ui-prefs', JSON.stringify({ theme: 'Dark' }));
  } catch {
    // storage unavailable
  }
});
await go('/', '#home-health .home-activity-row');
await shot('home-dark', 900);

// Workspaces (ADR 0023) - last, since a second workspace puts the switcher in every screen's sidebar: one for the
// payments team, its lead an owner there and nothing on the server, a developer beside her.
await context.addInitScript(() => {
  try {
    localStorage.setItem('queryapigate-ui-prefs', JSON.stringify({ theme: 'Light' }));
  } catch {
    // storage unavailable
  }
});
await call('POST', '/api/v1/workspaces', { name: 'payments', description: 'The payments team' });
await call('POST', '/api/v1/workspaces', { name: 'catalogue', description: 'Product catalogue and search' });
for (const [name, role] of [
  ['bea', 'owner'],
  ['dmitri', 'developer'],
]) {
  await call('POST', '/api/v1/administrators', { name, role: null, email: `${name}@corp.example` });
  await call('PUT', `/api/v1/workspaces/payments/members/${name}`, { role });
}
await call('POST', '/w/payments/api/v1/connections', {
  name: 'orders-db',
  db: 'postgres',
  host: 'payments-db.internal',
  port: 5432,
  database: 'orders',
  user: 'svc_payments',
  active: true,
});
await go('/workspaces', '#workspaces-table tr[data-name="payments"]');
await page.locator('#workspaces-table tr[data-name="payments"]').click();
await page.waitForSelector('#workspace-members tr[data-name="bea"]');
await shot('workspaces', 820);

// Signed out: the sign-in page, in a tab with no key - the operating system's light and dark.
for (const [name, colorScheme] of [
  ['sign-in', 'light'],
  ['sign-in-dark', 'dark'],
]) {
  const signedOut = await browser.newContext({
    viewport: { width: 1000, height: 640 },
    deviceScaleFactor: 2,
    colorScheme,
  });
  const tab = await signedOut.newPage();
  tab.on('pageerror', (e) => errors.push(e.message));
  await tab.goto(BASE + '/console/');
  await tab.getByRole('heading', { name: 'Sign in to the Console' }).waitFor({ timeout: 15_000 });
  await tab.waitForTimeout(400);
  await tab.screenshot({ path: OUT + name + '.png' });
  console.log('wrote', name + '.png');
  await signedOut.close();
}

await browser.close();
console.log(errors.length ? 'page errors: ' + JSON.stringify(errors) : 'no page errors');
stop();

// Retakes the documentation screenshots (documentation/screenshots/*.png) of the Console.
//
//   npm run build && QUERYAPIGATE_BIN=../.venv/bin/queryapigate npm run screenshots
//
// Starts a throwaway `queryapigate serve` (fresh home, the example APIs loaded), seeds it through /api/v1 with a
// realistic estate - more connections, keys from roles, traffic, one connection that is down and a key that keeps
// hitting its rate limit, so Alerts has something real to show - then photographs each screen at 1400px wide, 2x.
// The file names are the ones README.md links to; keep them when adding screens.
import { spawn } from 'node:child_process';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { chromium } from '@playwright/test';

const PORT = process.env.PORT ?? '5088';
const BASE = `http://127.0.0.1:${PORT}`;
const ADMIN = 'demo-admin';
const OUT = process.env.OUT ?? new URL('../../documentation/screenshots/', import.meta.url).pathname;

const home = mkdtempSync(join(tmpdir(), 'queryapigate-shots-'));
const server = spawn(process.env.QUERYAPIGATE_BIN ?? 'queryapigate', ['serve', '--port', PORT], {
  stdio: 'ignore',
  env: {
    ...process.env,
    QUERYAPIGATE_HOME: home,
    QUERYAPIGATE_API_KEY: ADMIN,
    QUERYAPIGATE_LOAD_EXAMPLES: '1',
  },
});
const stop = () => {
  server.kill('SIGTERM');
  rmSync(home, { recursive: true, force: true });
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

async function seed() {
  for (let i = 0; i < 100; i++) {
    try {
      if ((await fetch(BASE + '/health')).ok) break;
    } catch {
      // not up yet
    }
    await new Promise((r) => setTimeout(r, 300));
  }
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
}

await seed();

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
await go('/metrics', '.stat-tiles');
await shot('metrics');
await go('/audit-log', 'table.grid tbody tr');
await shot('audit-log', 1000);

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

await browser.close();
console.log(errors.length ? 'page errors: ' + JSON.stringify(errors) : 'no page errors');
stop();

import { api, ADMIN_KEY, expect, open, test } from './fixtures';

// The main things a person does in the Console, end to end against a real server.

test('a connection: create, test, edit, delete with a reason', async ({ page, allowStatus }) => {
  allowStatus(502); // the failing test below
  await open(page, '/connections', 'Connections');
  await page.getByRole('button', { name: 'New connection' }).click();
  const drawer = page.locator('#drawer');
  await drawer.getByLabel('Name').fill('e2e-lite');
  await drawer.getByLabel('Database type').selectOption('sqlite');
  await drawer.getByLabel('Default database').fill('/nonexistent/dir/x.db');
  await drawer.getByRole('button', { name: 'Test connection' }).click();
  await expect(drawer.getByText(/^✗ /)).toBeVisible();
  const examples = await (await api(page, 'GET', '/api/v1/connections/examples')).json();
  await drawer.getByLabel('Default database').fill(examples.database); // a SQLite file that exists
  await drawer.getByRole('button', { name: 'Test connection' }).click();
  await expect(drawer.getByText(/^✓ /)).toBeVisible();
  await drawer.getByRole('button', { name: 'Create' }).click();
  const row = page.locator('#connections-table tr[data-name="e2e-lite"]');
  await expect(row).toBeVisible();
  await row.getByRole('button', { name: 'Edit' }).click();
  await drawer.getByLabel('Active').uncheck();
  await drawer.getByRole('button', { name: 'Save' }).click();
  await expect(row).toContainText('Inactive');
  await row.getByRole('button', { name: 'Delete' }).click();
  await drawer.getByLabel('Reason').fill('end-to-end test');
  await drawer.getByLabel('Type "e2e-lite" to confirm').fill('e2e-lite');
  await drawer.getByRole('button', { name: 'Delete' }).click();
  await expect(row).toHaveCount(0);
  await page
    .locator('#conn-tabs')
    .getByRole('button', { name: /Deleted/ })
    .click();
  await expect(page.locator('#connections-table')).toContainText('end-to-end test');
});

test('an API key: create from explicit grants, use its secret once, revoke it', async ({
  page,
  allowStatus,
}) => {
  allowStatus(401);
  await open(page, '/api-keys', 'API keys');
  await page.getByRole('button', { name: 'New API key' }).click();
  const drawer = page.locator('#drawer');
  await drawer.getByLabel('Name').fill('e2e-key');
  await drawer.getByLabel('All connections').uncheck();
  await drawer.getByRole('checkbox', { name: 'examples', exact: true }).check();
  await drawer.getByLabel('Rate limit').fill('30/minute');
  await drawer.getByRole('button', { name: 'Create key' }).click();
  await expect(page.locator('#drawer-title')).toHaveText('API key created');
  const secret = await drawer.getByLabel('Secret key').inputValue();
  expect(secret).toMatch(/^sk_/);
  const run = await page.request.post('/execute_sql', {
    headers: { 'X-API-Key': secret },
    data: { sql: 'SELECT 1 AS one', connection_name: 'examples' },
  });
  expect(run.status()).toBe(200);
  await drawer.getByRole('button', { name: 'Done' }).click();
  const row = page.locator('#apikeys-table tr[data-name="e2e-key"]');
  await expect(row).toContainText('conn: examples');
  await expect(row).toContainText('30/minute');
  page.once('dialog', (dialog) => dialog.accept());
  await row.getByRole('button', { name: 'Revoke' }).click();
  await expect(row).toHaveCount(0);
  expect((await page.request.get('/catalog', { headers: { 'X-API-Key': secret } })).status()).toBe(401);
});

test('a role, and a key created from it', async ({ page }) => {
  await open(page, '/roles', 'Roles');
  await page.getByRole('button', { name: 'New role' }).click();
  const drawer = page.locator('#drawer');
  await drawer.getByLabel('Name').fill('e2e-role');
  await drawer.getByLabel('Allowed IPs').fill('10.0.0.0/8');
  await drawer.getByRole('button', { name: 'Create role' }).click();
  const row = page.locator('#roles-table tr[data-name="e2e-role"]');
  await row.getByRole('button', { name: 'New key from this' }).click();
  await expect(drawer.getByLabel('Create from')).toHaveValue('e2e-role');
  await drawer.getByLabel('Name').fill('e2e-from-role');
  await drawer.getByRole('button', { name: 'Create key' }).click();
  await drawer.getByRole('button', { name: 'Done' }).click();
  await expect(row.locator('td').nth(5)).toHaveText('1'); // keys created from it
  page.on('dialog', (dialog) => dialog.accept());
  await row.getByRole('button', { name: 'Delete' }).click();
  await expect(row).toHaveCount(0);
  await api(page, 'DELETE', '/api/v1/api-keys/e2e-from-role');
});

test('API Designer to a published API: run, save as draft, publish, call it', async ({
  page,
  allowStatus,
}) => {
  allowStatus(404); // the draft isn't served yet
  await open(page, '/designer', 'API Designer');
  await expect(page.getByLabel('Connection', { exact: true })).toHaveValue('examples');
  const editor = page.locator('#run-sql');
  await editor.click();
  await page.keyboard.type('SELECT title FROM film WHERE film_id = :id');
  await page.locator('.side-tab[data-side-tab="settings"]').click();
  await page.getByLabel(/Bound parameters/).fill('{"id": 1}');
  await page.keyboard.press('Control+Enter');
  await expect(page.locator('#run-quick-stats')).toContainText('200 OK');
  await page.getByRole('button', { name: 'Save as New API' }).click();
  const panel = page.locator('#run-save-panel');
  await panel.getByLabel('Filename').fill('e2e_film_title');
  await panel.getByLabel('Author').fill('e2e');
  await panel.getByLabel('Description').fill('One film title');
  await panel.getByRole('button', { name: 'Save as draft' }).click();
  await expect(page).toHaveURL(/\/console\/queries\/e2e_film_title$/);
  expect(
    (await page.request.get('/q/e2e_film_title?id=1', { headers: { 'X-API-Key': ADMIN_KEY } })).status(),
  ).toBe(404);
  await page.locator('#query-detail').getByRole('button', { name: 'Publish' }).click();
  await expect(page.locator('#query-detail')).toContainText('Published');
  const served = await page.request.get('/q/e2e_film_title?id=1', { headers: { 'X-API-Key': ADMIN_KEY } });
  expect(served.status()).toBe(200);
  page.on('dialog', (dialog) => dialog.accept());
  await api(page, 'DELETE', '/api/v1/queries/e2e_film_title');
});

test('Home updates live when a query runs elsewhere', async ({ page }) => {
  await open(page, '/', 'Home');
  await expect(page.locator('#home-requests-panel')).toBeVisible();
  await page.request.get('/q/example_monthly_revenue', { headers: { 'X-API-Key': ADMIN_KEY } });
  await expect(page.locator('#home-requests-panel tbody tr').first()).toContainText(
    'example_monthly_revenue',
    {
      timeout: 10_000,
    },
  );
  await page
    .locator('#home-requests-panel')
    .getByRole('button', { name: 'example_monthly_revenue' })
    .first()
    .click();
  await expect(page.locator('#query-detail button[data-subtab="history"]')).toHaveClass(/on/);
});

test('Caching: a cached response is listed, viewable and evictable', async ({ page }) => {
  const query = await (await api(page, 'GET', '/api/v1/queries/example_top_films')).json();
  const patched = await api(
    page,
    'PATCH',
    `/api/v1/queries/example_top_films/versions/${query.published_version}`,
    {
      cache_ttl: 300,
    },
  );
  expect(patched.status()).toBe(200);
  expect(
    (await page.request.get('/q/example_top_films', { headers: { 'X-API-Key': ADMIN_KEY } })).status(),
  ).toBe(200);
  await open(page, '/caching', 'Caching');
  const entries = page.locator('#cache-entries-panel');
  await expect(entries).toContainText('1 live entry right now.');
  await entries.getByRole('button', { name: 'example_top_films' }).click();
  await expect(page.locator('#drawer')).toContainText('200 OK');
  await page.keyboard.press('Escape');
  await entries.getByRole('button', { name: 'Delete' }).click();
  await expect(entries).toContainText('0 live entries right now.');
});

test('Access map: the table drill-down and View in Access map', async ({ page }) => {
  await open(page, '/access-map', 'Access map');
  await page.getByLabel('Connection', { exact: true }).selectOption('examples');
  await page.getByLabel('Table').selectOption('rental');
  await expect(page.locator('#accessmap-body tbody tr').first()).toBeVisible();
  const touching = await page.locator('#accessmap-body tbody tr').count();
  expect(touching).toBeGreaterThan(0);
  await page.getByLabel('Connection', { exact: true }).selectOption('');
  await page.goto('/console/queries/example_top_films');
  await page.locator('#query-detail button[data-subtab="access"]').click();
  await page.getByRole('button', { name: 'View in Access map' }).click();
  await expect(page.getByPlaceholder('Filter by query or key…')).toHaveValue('example_top_films');
  await expect(page.locator('#accessmap-body tbody tr')).toHaveCount(1);
});

test('a collection: rename it and its grants follow', async ({ page }) => {
  await open(page, '/queries/example_kpi_overdue', 'API Repository');
  await page.locator('.qg-foot').getByRole('link', { name: 'Rename' }).click();
  const drawer = page.locator('#drawer');
  await drawer.getByLabel('New name').fill('e2e-dashboard');
  await drawer.getByRole('button', { name: 'Rename' }).click();
  await expect(page.getByText('Renamed examples-dashboard → e2e-dashboard')).toBeVisible();
  const role = await (await api(page, 'GET', '/api/v1/roles/example-dashboard')).json();
  expect(role.collections).toEqual(['e2e-dashboard']);
  await api(page, 'PATCH', '/api/v1/collections/e2e-dashboard', { name: 'examples-dashboard' });
});

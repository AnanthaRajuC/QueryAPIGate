import { expect, open, test } from './fixtures';

// Every screen, reached through the sidebar as a person would: it renders, its data loads, and nothing fails.

const SCREENS: [string, string, string][] = [
  // sidebar label, heading, something only that screen's data shows
  ['Home', 'Home', 'System health'],
  ['Connections', 'Connections', 'examples'],
  ['Caching', 'Caching', 'Saved queries with cache_ttl set'],
  ['API Repository', 'API Repository', 'examples-dashboard'],
  ['API Designer', 'API Designer', 'Recent Queries'],
  ['API keys', 'API keys', 'example-partner'],
  ['Roles', 'Roles', 'example-partner'],
  ['Access map', 'Access map', 'API KEYS'],
  ['Metrics', 'Metrics', 'Requests by status'],
  ['Audit log', 'Audit log', 'load_examples'],
  ['Settings', 'Settings', 'QUERYAPIGATE_HOME'],
  ['Help', 'Help', 'Quick reference'],
];

test('every sidebar screen renders its data', async ({ page, allowStatus }) => {
  allowStatus(404); // Help reads docs from GitHub at this version's tag, falling back to main
  await open(page, '/', 'Home');
  for (const [label, heading, marker] of SCREENS) {
    await page.locator('#tabs, .side-foot').getByRole('tab', { name: label, exact: false }).first().click();
    await expect(page.getByRole('heading', { level: 1, name: heading })).toBeVisible();
    await expect(
      page.locator('main section').getByText(marker, { exact: false }).locator('visible=true').first(),
    ).toBeVisible();
  }
});

test('the breadcrumb, Ctrl+K search and sidebar collapse work', async ({ page }) => {
  await open(page, '/api-keys', 'API keys');
  await expect(page.locator('#crumb-page')).toHaveText('API keys');
  await page.keyboard.press('Control+k');
  await page.keyboard.type('example_top');
  await page.keyboard.press('Enter');
  await expect(page).toHaveURL(/\/console\/queries\/example_top_films$/);
  await page.keyboard.press('Control+b');
  await expect(page.locator('body')).toHaveClass(/side-collapsed|collapsed/);
  await page.keyboard.press('Control+b');
});

test('dark theme and compact density apply at once and survive a reload', async ({ page }) => {
  await open(page, '/settings', 'Settings');
  await page.getByRole('button', { name: 'Interface' }).click();
  await page.getByRole('group', { name: 'Theme' }).getByRole('button', { name: 'Dark' }).click();
  await page.getByRole('group', { name: 'Table density' }).getByRole('button', { name: 'Compact' }).click();
  await page.reload();
  await expect(page.locator('html')).toHaveCSS('color-scheme', 'dark');
  await expect(page.locator('body')).toHaveClass(/compact/);
  await page.evaluate(() => localStorage.removeItem('queryapigate-ui-prefs'));
});

test('/ui, where the admin UI used to be, opens the Console', async ({ page }) => {
  await page.goto('/ui');
  await expect(page).toHaveURL(/\/console\/$/);
  await expect(page.getByRole('heading', { level: 1, name: 'Home' })).toBeVisible();
});

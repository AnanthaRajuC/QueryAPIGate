import { api, expect, open, test } from './fixtures';

// Workspaces (ADR 0023) end to end: a second workspace with its own connection, the switcher sending every screen
// there - and only there - and back again.

test('a second workspace: switch to it, see only its own, switch back', async ({ page }) => {
  const examples = await (await api(page, 'GET', '/api/v1/connections/examples')).json();
  expect((await api(page, 'POST', '/api/v1/workspaces', { name: 'e2e-team' })).status()).toBe(201);
  try {
    const created = await api(page, 'POST', '/w/e2e-team/api/v1/connections', {
      name: 'team-only',
      db: 'sqlite',
      database: examples.database,
      active: true,
    });
    expect(created.status()).toBe(201);

    await open(page, '/connections', 'Connections');
    const table = page.locator('#connections-table');
    await expect(table.locator('tr[data-name="examples"]')).toBeVisible();
    await page.locator('#workspace').selectOption('e2e-team');
    await open(page, '/connections', 'Connections');
    await expect(table.locator('tr[data-name="team-only"]')).toBeVisible();
    await expect(table.locator('tr[data-name="examples"]')).toHaveCount(0); // default's - not here

    await page.locator('#workspace').selectOption('default');
    await open(page, '/connections', 'Connections');
    await expect(table.locator('tr[data-name="examples"]')).toBeVisible();
    await expect(table.locator('tr[data-name="team-only"]')).toHaveCount(0);
  } finally {
    await api(page, 'DELETE', '/w/e2e-team/api/v1/connections/team-only', { reason: 'end-to-end test' });
    await api(page, 'DELETE', '/api/v1/workspaces/e2e-team');
  }
});

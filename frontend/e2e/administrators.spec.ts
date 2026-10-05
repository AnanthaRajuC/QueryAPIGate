import { ADMIN_KEY, api, expect, open, test } from './fixtures';

// Named administrators (ADR 0003), end to end: an owner's shared key creates a developer in the Console, the developer
// signs in with the token shown once, and the Console then shows only what a developer may do.

test('a developer, created in the Console, signs in with their own token', async ({ page }) => {
  await open(page, '/administrators', 'Administrators');
  await page.locator('#new-administrator').click();
  const drawer = page.locator('#drawer');
  await drawer.getByLabel('Name').fill('e2e-dev');
  await drawer.getByLabel('Role').selectOption('developer');
  await drawer.getByRole('button', { name: 'Create' }).click();
  const secret = await drawer.locator('#k-secret').inputValue();
  expect(secret).toMatch(/^qagadm_/);
  await drawer.getByRole('button', { name: 'Done' }).click();
  await expect(page.locator('#administrators-table tr[data-name="e2e-dev"]')).toContainText('Developer');

  try {
    // sign in as them, in the sidebar's key box
    await page.locator('#key-change').click();
    await page.locator('#key').fill(secret);
    await page.locator('#save-key').click();
    await expect(page.locator('#key-state')).toHaveText('Signed in as e2e-dev · Developer');
    const nav = page.locator('#tabs');
    await expect(nav.getByRole('tab', { name: 'API Repository' })).toBeVisible();
    await expect(nav.getByRole('tab', { name: 'API keys' })).toHaveCount(0);
    await expect(page.locator('.side-foot').getByRole('tab', { name: 'Settings' })).toHaveCount(0);
    // their own account and tokens, not everyone's
    await expect(page.getByText('Your account and your admin tokens.')).toBeVisible();
    await expect(page.locator('#admin-tokens')).toContainText('first token');
    // the server agrees: a developer may not create API keys
    const refused = await api(page, 'POST', '/api/v1/api-keys', { name: 'nope' }, { 'X-API-Key': secret });
    expect(refused.status()).toBe(403);
    expect((await refused.json()).code).toBe('role_forbidden');
  } finally {
    await page.evaluate((key) => sessionStorage.setItem('queryapigate-key', key), ADMIN_KEY);
    await api(page, 'DELETE', '/api/v1/administrators/e2e-dev');
  }
});

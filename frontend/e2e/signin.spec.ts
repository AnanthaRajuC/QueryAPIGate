import { ADMIN_KEY, expect, test } from './fixtures';

// The sign-in page against the real server: a tab with no key gets it instead of the Console - the server's own 401
// decides - and the admin key takes it straight to the Console. Signing out from the sidebar brings it back.

test('a tab with no key signs in first, and signs out back to it', async ({ page, allowStatus }) => {
  allowStatus(401); // what the server answers a tab with no key, before it signs in
  // after the fixture's own script, so this tab starts with no key at all
  await page.addInitScript(() => sessionStorage.removeItem('queryapigate-key'));
  await page.goto('/console/');
  await expect(page.getByRole('heading', { name: 'Sign in to the Console' })).toBeVisible();
  await expect(page.locator('#side')).toHaveCount(0);

  await page.getByLabel('Admin token or API key').fill('not-the-key');
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page.getByRole('alert')).toContainText('wasn’t accepted');

  await page.getByLabel('Admin token or API key').fill(ADMIN_KEY);
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page.locator('#key-state')).toHaveText('Shared admin key (owner)');
  await expect(page.getByRole('heading', { name: 'Sign in to the Console' })).toHaveCount(0);

  await page.locator('#key-sign-out').click();
  await expect(page.getByRole('heading', { name: 'Sign in to the Console' })).toBeVisible();
});

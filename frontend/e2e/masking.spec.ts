import { api, expect, open, test } from './fixtures';

// Column masking end to end (ADR 0005): a key given a rule in the Console, then used - the example customers' emails
// come back hashed, whatever the SQL calls the column.

test('a key with a masking rule never sees the real emails', async ({ page }) => {
  await open(page, '/api-keys', 'API keys');
  await page.locator('#new-apikey').click();
  const drawer = page.locator('#drawer');
  await drawer.getByLabel('Name').fill('e2e-masked');
  await drawer.getByRole('button', { name: 'Add a masking rule' }).click();
  await drawer.getByLabel('Masked column 1').fill('customer.email');
  await drawer.getByRole('button', { name: 'Create key' }).click();
  const secret = await drawer.locator('#k-secret').inputValue();
  await drawer.getByRole('button', { name: 'Done' }).click();
  await expect(page.locator('#apikeys-table tr[data-name="e2e-masked"]')).toContainText('masked');
  try {
    for (const sql of [
      'SELECT email FROM customer LIMIT 3',
      'SELECT lower(email) AS contact FROM customer LIMIT 3',
    ]) {
      const res = await api(
        page,
        'POST',
        '/execute_sql',
        { sql, connection_name: 'examples' },
        { 'X-API-Key': secret },
      );
      expect(res.status()).toBe(200);
      for (const row of (await res.json()) as Record<string, string>[]) {
        expect(Object.values(row)[0]).toMatch(/^[0-9a-f]{16}$/);
      }
    }
    const plain = await api(page, 'POST', '/execute_sql', {
      sql: 'SELECT email FROM customer LIMIT 1',
      connection_name: 'examples',
    });
    expect(((await plain.json()) as { email: string }[])[0]!.email).toContain('@');
  } finally {
    await api(page, 'DELETE', '/api/v1/api-keys/e2e-masked');
  }
});

import { mkdtempSync, readdirSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { api, expect, open, test } from './fixtures';

// Exports end to end (ADR 0004): a folder destination made and tested in the Console, an export of an example query
// defined and run there, and the Parquet file it wrote, on disk.

test('a destination, an export, a run - and the file it wrote', async ({ page }) => {
  const folder = mkdtempSync(join(tmpdir(), 'qag-e2e-drop-'));
  try {
    await open(page, '/destinations', 'Destinations');
    await page.locator('#new-destination').click();
    const drawer = page.locator('#drawer');
    await drawer.getByLabel('Name').fill('e2e-drop');
    await drawer.getByLabel('Writes under').fill(folder + '/');
    await drawer.getByRole('button', { name: 'Test' }).click();
    await expect(drawer.locator('.test-ok')).toContainText('_queryapigate_probe.csv');
    await drawer.getByRole('button', { name: 'Create' }).click();
    await expect(page.locator('#destinations-table tr[data-name="e2e-drop"]')).toContainText(folder);

    await open(page, '/exports', 'Exports');
    await page.locator('#new-export').click();
    await drawer.getByLabel('Name').fill('e2e-rentals');
    await drawer.getByLabel('Saved query').selectOption('example_rentals_since');
    await drawer.getByLabel('Destination').selectOption('e2e-drop');
    await drawer.getByLabel('File path').fill('rentals/{date}/rentals_{run}.parquet');
    await drawer.getByRole('button', { name: 'Create' }).click();
    const row = page.locator('#exports-table tr[data-name="e2e-rentals"]');
    await expect(row).toContainText('example_rentals_since');

    await row.getByRole('button', { name: 'Run now' }).click();
    await expect(page.getByText(/rows written to .*rentals_.*\.parquet/)).toBeVisible();
    const [day] = readdirSync(join(folder, 'rentals'));
    expect(readdirSync(join(folder, 'rentals', day!)).filter((f) => f.endsWith('.parquet'))).toHaveLength(1);

    await row.getByRole('button', { name: 'Runs' }).click();
    await expect(page.locator('#export-runs')).toContainText('success');
  } finally {
    await api(page, 'DELETE', '/api/v1/exports/e2e-rentals');
    await api(page, 'DELETE', '/api/v1/destinations/e2e-drop');
    rmSync(folder, { recursive: true, force: true });
  }
});

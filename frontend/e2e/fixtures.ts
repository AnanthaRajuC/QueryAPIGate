import { test as base, expect, type Page } from '@playwright/test';

// Every test signs in with the admin key (the same per-tab storage /docs uses) and fails if the page threw,
// logged an error, or got an error response it didn't expect - the class of bug the unit tests, with their fake
// backend, can't see. A test that expects an error status says so with allowStatus().

export const ADMIN_KEY = 'e2e-admin-key';

interface Guard {
  allow: Set<number>;
}

export const test = base.extend<{ guard: Guard; allowStatus: (status: number) => void }>({
  guard: [
    async ({ page }, use) => {
      const problems: string[] = [];
      const guard: Guard = { allow: new Set() };
      page.on('pageerror', (error) => problems.push(`page error: ${error.message}`));
      page.on('console', (message) => {
        // a failed response is reported (or allowed) below, by status
        if (message.type() === 'error' && !message.text().startsWith('Failed to load resource'))
          problems.push(`console: ${message.text()}`);
      });
      page.on('response', (response) => {
        const url = new URL(response.url());
        // this server's responses only - Help also reads GitHub, which may answer 404 for an untagged version
        if (url.hostname === '127.0.0.1' && response.status() >= 400 && !guard.allow.has(response.status()))
          problems.push(`HTTP ${response.status()} ${response.request().method()} ${url.pathname}`);
      });
      await page.addInitScript((key) => sessionStorage.setItem('queryapigate-key', key), ADMIN_KEY);
      await use(guard);
      expect(problems, 'errors on the page or unexpected error responses').toEqual([]);
    },
    { auto: true },
  ],
  allowStatus: async ({ guard }, use) => {
    await use((status) => guard.allow.add(status));
  },
});

export { expect };

/** Open a Console screen and wait for its heading. */
export async function open(page: Page, path: string, heading: string) {
  await page.goto(`/console${path}`);
  await expect(page.getByRole('heading', { level: 1, name: heading })).toBeVisible();
}

/** A call to the API as the admin, outside the page. */
export async function api(
  page: Page,
  method: string,
  path: string,
  body?: unknown,
  headers: Record<string, string> = {},
) {
  const response = await page.request.fetch(path, {
    method,
    headers: { 'X-API-Key': ADMIN_KEY, 'Content-Type': 'application/json', ...headers },
    data: body === undefined ? undefined : JSON.stringify(body),
  });
  return response;
}

import { defineConfig, devices } from '@playwright/test';

// End-to-end tests of the built Console against a real QueryAPIGate (e2e/serve.mjs): a fresh home with the example APIs
// loaded and an admin key, so each run starts from the same state. Build first (npm run build); the server serves the
// Console from queryapigate/console_dist. QUERYAPIGATE_BIN points at the CLI when it isn't on PATH (a venv).
const PORT = Number(process.env.E2E_PORT ?? 5077);

export default defineConfig({
  testDir: './e2e',
  fullyParallel: false, // one server, one database: the tests change shared state
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [['list'], ['html', { open: 'never' }]] : 'list',
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    viewport: { width: 1400, height: 900 },
  },
  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'], viewport: { width: 1400, height: 900 } } },
  ],
  webServer: {
    command: `node e2e/serve.mjs ${PORT}`,
    url: `http://127.0.0.1:${PORT}/health`,
    reuseExistingServer: false,
    timeout: 60_000,
    stdout: 'ignore',
    stderr: 'pipe',
  },
});

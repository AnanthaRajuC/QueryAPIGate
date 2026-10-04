import path from 'node:path';

import react from '@vitejs/plugin-react';
import { defineConfig } from 'vitest/config';

// The Console is served by the Python backend at /console (queryapigate/console.py), from the package's
// console_dist/ directory - so that's where the build goes. See ADR 0001.
const backend = process.env.QUERYAPIGATE_URL ?? 'http://127.0.0.1:5000';

export default defineConfig({
  base: '/console/',
  plugins: [react()],
  resolve: {
    alias: { '@': path.resolve(import.meta.dirname, 'src') },
  },
  build: {
    outDir: '../queryapigate/console_dist',
    emptyOutDir: true,
    // Browsers with light-dark() built in. For older ones the CSS minifier rewrites it into variables switched by a
    // prefers-color-scheme media query, which ignores the page's own color-scheme - so the Theme preference and the
    // header's light/dark switch would do nothing, the page always following the operating system.
    cssTarget: ['chrome123', 'edge123', 'firefox120', 'safari17.5'],
  },
  server: {
    // During development, everything that isn't the Console itself is the API: forward it to a running
    // `queryapigate serve` (override with QUERYAPIGATE_URL).
    proxy: {
      '^/(?!console(/|$)).*': { target: backend, changeOrigin: true },
    },
  },
  test: {
    include: ['src/**/*.test.{ts,tsx}'], // e2e/ is Playwright's
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.ts'],
    css: false,
    testTimeout: 15000,
  },
});

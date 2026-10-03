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
  },
  server: {
    // During development, everything that isn't the Console itself is the API: forward it to a running
    // `queryapigate serve` (override with QUERYAPIGATE_URL).
    proxy: {
      '^/(?!console(/|$)).*': { target: backend, changeOrigin: true },
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.ts'],
    css: false,
    testTimeout: 15000,
  },
});

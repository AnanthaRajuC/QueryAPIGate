// Starts `queryapigate serve` for the end-to-end tests: a throwaway home, the example APIs, and the admin key the
// tests use. Removes the home when the server stops.
import { spawn } from 'node:child_process';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const port = process.argv[2] ?? '5077';
const home = mkdtempSync(join(tmpdir(), 'queryapigate-e2e-'));
const server = spawn(process.env.QUERYAPIGATE_BIN ?? 'queryapigate', ['serve', '--port', port], {
  stdio: 'inherit',
  env: {
    ...process.env,
    QUERYAPIGATE_HOME: home,
    QUERYAPIGATE_API_KEY: 'e2e-admin-key',
    QUERYAPIGATE_LOAD_EXAMPLES: '1',
  },
});
const stop = () => server.kill('SIGTERM');
process.on('SIGTERM', stop);
process.on('SIGINT', stop);
server.on('exit', (code) => {
  rmSync(home, { recursive: true, force: true });
  process.exit(code ?? 0);
});

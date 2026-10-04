import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { resetSettingsSection } from '@/features/settings/state';
import { fakeBackend, jsonResponse } from '@/test/fakeBackend';
import { baseRoutes } from '@/test/fixtures';
import { renderAt } from '@/test/renderApp';

vi.mock('@/components/SqlEditor', () => ({ SqlEditor: () => <textarea aria-label="SQL" /> }));

beforeEach(() => {
  localStorage.clear();
  resetSettingsSection();
});
afterEach(() => vi.unstubAllGlobals());

const row = (
  env: string,
  label: string,
  value: string,
  source: 'env' | 'default',
  envValue: string | null,
) => ({
  label,
  description: label + ' description',
  env,
  value,
  source,
  env_value: envValue,
});

const routes = () => ({
  ...baseRoutes(),
  'GET /api/v1/settings': () => ({
    items: [
      {
        id: 'general',
        title: 'General',
        description: 'Where this server keeps its files.',
        rows: [row('QUERYAPIGATE_HOME', 'Home directory', '/data', 'env', '/data')],
      },
      {
        id: 'security',
        title: 'Security',
        description: 'Admin access.',
        rows: [
          row('QUERYAPIGATE_API_KEY', 'Admin API key', 'configured', 'env', null),
          row('QUERYAPIGATE_ALLOW_WRITES', 'Allow writes', 'off', 'default', null),
        ],
      },
      {
        id: 'mcp',
        title: 'MCP server',
        description: 'The MCP server.',
        rows: [row('QUERYAPIGATE_MCP_PORT', 'Port', '8765', 'default', null)],
      },
    ],
  }),
  'GET /api/v1/mcp/tools': () => ({
    items: [
      {
        name: 'execute_sql',
        description: 'Run SQL',
        kind: 'ad-hoc',
        params: ['connection', 'sql'],
        read_only: true,
      },
      { name: 'films', description: 'All films', kind: 'saved query', params: [], read_only: true },
    ],
  }),
  'GET /api/v1/mcp/status': () => jsonResponse({ reachable: false, port: 8765 }),
});

describe('Settings', () => {
  it('shows each section read-only, with where each value comes from', async () => {
    fakeBackend(routes());
    renderAt('/settings');
    await waitFor(() => expect(document.getElementById('settings-nav')).not.toBeNull());
    const nav = document.getElementById('settings-nav')!;
    await waitFor(() => expect(within(nav).getByRole('button', { name: /Security/ })).toBeInTheDocument());
    expect(within(nav).getByRole('button', { name: /General/ })).toHaveClass('on');
    expect(screen.getByText('/data')).toBeInTheDocument();
    await userEvent.click(within(nav).getByRole('button', { name: /Security/ }));
    expect(within(nav).getByRole('button', { name: /Security/ })).toHaveTextContent('Security2');
    expect(screen.getByText('configured')).toBeInTheDocument();
    expect(screen.getByText('QUERYAPIGATE_ALLOW_WRITES')).toBeInTheDocument();
    expect(document.querySelector('.set-note')).toHaveTextContent('Read-only.');
  });

  it('copies only what is set, never a secret, as .env', async () => {
    const user = userEvent.setup(); // with a clipboard stub
    fakeBackend(routes());
    renderAt('/settings');
    await screen.findByText('/data');
    await user.click(screen.getByRole('button', { name: 'Copy as .env' }));
    expect(await screen.findByText('Copied')).toBeInTheDocument();
    expect(await navigator.clipboard.readText()).toBe('QUERYAPIGATE_HOME=/data\n');
  });

  it('MCP: lists the tools, and checks reachability only when asked', async () => {
    const { calls } = fakeBackend(routes());
    renderAt('/settings');
    const nav = document.getElementById('settings-nav')!;
    await userEvent.click(await within(nav).findByRole('button', { name: /MCP server/ }));
    expect(await screen.findByText('execute_sql')).toBeInTheDocument();
    expect(screen.getByText('connection, sql')).toBeInTheDocument();
    expect(screen.getByText('Not checked yet')).toBeInTheDocument();
    expect(calls.some((c) => c.path === '/api/v1/mcp/status')).toBe(false);
    await userEvent.click(screen.getByRole('button', { name: 'Check now' }));
    expect(await screen.findByText('Not reachable on port 8765')).toBeInTheDocument();
    expect(document.querySelector('.set-row .dot')).toHaveClass('bad');
  });

  it('Interface preferences apply at once and are shared with /ui', async () => {
    fakeBackend(routes());
    renderAt('/settings');
    await userEvent.click(screen.getByRole('button', { name: 'Interface' }));
    const density = screen.getByRole('group', { name: 'Table density' });
    expect(within(density).getByRole('button', { name: 'Comfortable' })).toHaveClass('on');
    await userEvent.click(within(density).getByRole('button', { name: 'Compact' }));
    expect(document.body).toHaveClass('compact');
    await userEvent.click(
      within(screen.getByRole('group', { name: 'Theme' })).getByRole('button', { name: 'Dark' }),
    );
    expect(document.documentElement.style.colorScheme).toBe('dark');
    await userEvent.click(
      within(screen.getByRole('group', { name: 'Default result format' })).getByRole('button', {
        name: 'csv',
      }),
    );
    expect(JSON.parse(localStorage.getItem('queryapigate-ui-prefs')!)).toEqual({
      theme: 'Dark',
      density: 'Compact',
      format: 'csv',
    });
  });

  it('says settings are admin-only when they cannot be read', async () => {
    fakeBackend({
      ...routes(),
      'GET /api/v1/settings': () =>
        jsonResponse({ error: 'Forbidden', code: 'forbidden', request_id: 'r' }, 403),
    });
    renderAt('/settings');
    expect(await screen.findByText('Settings unavailable')).toBeInTheDocument();
  });
});

import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { resetSettingsTab } from '@/features/settings/state';
import { fakeBackend, jsonResponse } from '@/test/fakeBackend';
import { baseRoutes } from '@/test/fixtures';
import { renderAt } from '@/test/renderApp';

vi.mock('@/components/SqlEditor', () => ({ SqlEditor: () => <textarea aria-label="SQL" /> }));

beforeEach(() => {
  localStorage.clear();
  resetSettingsTab();
});
afterEach(() => vi.unstubAllGlobals());

const row = (
  env: string,
  label: string,
  value: string,
  source: 'env' | 'file' | 'default',
  envValue: string | null,
  extra: { group?: string; secret?: boolean; experimental?: boolean } = {},
) => ({
  label,
  description: label + ' description',
  env,
  value,
  source,
  env_value: envValue,
  experimental: extra.experimental ?? false,
  secret: extra.secret ?? false,
  group: extra.group ?? null,
});

const LONG_PATH = '/var/lib/queryapigate/a-home-folder-with-a-long-name';

const routes = () => ({
  ...baseRoutes(),
  'GET /api/v1/settings': () => ({
    items: [
      {
        id: 'general',
        area: 'general',
        title: 'General',
        description: 'Where this server keeps its files.',
        rows: [
          row('QUERYAPIGATE_HOME', 'Home directory', LONG_PATH, 'env', LONG_PATH, { group: 'Storage' }),
          row('QUERYAPIGATE_RUN_STORE_URL', 'Run store', 'the metadata store', 'default', null, {
            group: 'Storage',
            secret: true,
          }),
          row('QUERYAPIGATE_LOAD_EXAMPLES', 'Load examples', 'on', 'env', 'yes', { group: 'Startup' }),
          row('QUERYAPIGATE_CONFIG_FILE', 'Configuration file', 'none', 'default', null, {
            group: 'Configuration file',
          }),
          row('QUERYAPIGATE_CONFIG_PRUNE', 'Prune at startup', 'off', 'default', null, {
            group: 'Configuration file',
          }),
          row('QUERYAPIGATE_H2_JAR', 'H2 driver', 'h2.jar (bundled)', 'default', null, {
            group: 'Drivers',
            experimental: true,
          }),
        ],
      },
      {
        id: 'security',
        area: 'security',
        title: 'Security',
        description: 'Admin access.',
        rows: [
          row(
            'QUERYAPIGATE_API_KEY',
            'Admin API key',
            'configured (from QUERYAPIGATE_API_KEY_FILE)',
            'file',
            null,
            {
              group: 'Access',
              secret: true,
            },
          ),
          row('QUERYAPIGATE_ALLOW_WRITES', 'Allow writes', 'off', 'default', null, { group: 'Access' }),
          row(
            'QUERYAPIGATE_DATABASE_URL',
            'Metadata database',
            'PostgreSQL (postgresql://qag:***@db.internal.example:5432/queryapigate)',
            'env',
            null,
            { group: 'Storage', secret: true },
          ),
        ],
      },
      {
        id: 'sso',
        area: 'sign-in',
        title: 'Console sign-in (SSO)',
        description: 'Sign in with your identity provider.',
        rows: [row('QUERYAPIGATE_SSO_ISSUER', 'Issuer', 'not set', 'default', null)],
      },
      {
        id: 'mcp',
        area: 'mcp',
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

const tabs = () => screen.getByRole('tablist', { name: 'Settings' });
const item = (label: string) =>
  screen.getByRole('heading', { name: label, level: 3 }).closest<HTMLElement>('.set-item')!;

describe('Settings', () => {
  it('has a tab per area, with counts, and a heading per group - read-only', async () => {
    fakeBackend(routes());
    renderAt('/settings');
    await screen.findByText(LONG_PATH);
    expect(
      within(tabs())
        .getAllByRole('tab')
        .map((t) => t.textContent),
    ).toEqual(['General6', 'Security3', 'Sign-in1', 'MCP server1', 'This browser']);
    expect(within(tabs()).getByRole('tab', { name: /General/ })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getAllByRole('heading', { level: 2 }).map((h) => h.textContent)).toEqual([
      'Storage',
      'Startup',
      'Configuration file',
      'Drivers',
    ]);
    expect(document.querySelector('.set-note')).toHaveTextContent('Read-only. These values come from');
    await userEvent.click(within(tabs()).getByRole('tab', { name: /Security/ }));
    expect(within(tabs()).getByRole('tab', { name: /Security/ })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByText('QUERYAPIGATE_ALLOW_WRITES')).toBeInTheDocument();
    // a section of its own (no groups) is its heading, with its description
    await userEvent.click(within(tabs()).getByRole('tab', { name: /Sign-in/ }));
    expect(screen.getByRole('heading', { name: 'Console sign-in (SSO)', level: 2 })).toBeInTheDocument();
    expect(screen.getByText('Sign in with your identity provider.')).toBeInTheDocument();
  });

  it('tags only what something set, by what set it', async () => {
    fakeBackend(routes());
    renderAt('/settings/security');
    await screen.findByText('QUERYAPIGATE_ALLOW_WRITES');
    expect(within(item('Admin API key')).getByText('Set by file')).toBeInTheDocument();
    expect(within(item('Metadata database')).getByText('Set by env')).toBeInTheDocument();
    expect(within(item('Allow writes')).queryByText(/Set by/)).toBeNull();
    expect(screen.queryByText('default')).toBeNull();
  });

  it('shows values for reading: On and Off, Not set, and a long one boxed with a copy button', async () => {
    const user = userEvent.setup(); // with a clipboard stub
    fakeBackend(routes());
    renderAt('/settings/general');
    await screen.findByText(LONG_PATH);
    expect(item('Load examples').querySelector('.set-bool')).toHaveTextContent('On');
    expect(item('Load examples').querySelector('.set-dot.on')).not.toBeNull();
    expect(item('Prune at startup').querySelector('.set-bool')).toHaveTextContent('Off');
    expect(item('Configuration file').querySelector('.set-unset')).toHaveTextContent('Not set');
    expect(item('Run store')).toHaveTextContent('Metadata store');
    expect(item('H2 driver')).toHaveTextContent('experimental');
    // Prune applies only with a configuration file, and there is none
    expect(item('Prune at startup')).toHaveClass('dim');
    expect(item('Load examples')).not.toHaveClass('dim');
    expect(item('Home directory').querySelector('.set-long')).toHaveTextContent(LONG_PATH);
    await user.click(
      within(item('Home directory')).getByRole('button', { name: 'Copy the value of Home directory' }),
    );
    expect(await navigator.clipboard.readText()).toBe(LONG_PATH);
    // the variable's name copies too
    await user.click(
      screen.getByRole('button', { name: 'Copy the variable name QUERYAPIGATE_LOAD_EXAMPLES' }),
    );
    expect(await navigator.clipboard.readText()).toBe('QUERYAPIGATE_LOAD_EXAMPLES');
  });

  it('never offers to copy a secret, however long its masked value', async () => {
    fakeBackend(routes());
    renderAt('/settings/security');
    await screen.findByRole('heading', { name: 'Metadata database', level: 3 });
    const database = item('Metadata database');
    expect(database.querySelector('.set-long')).not.toBeNull();
    expect(within(database).queryByRole('button', { name: /Copy the value/ })).toBeNull();
    expect(screen.queryAllByRole('button', { name: /Copy the value/ })).toHaveLength(0);
  });

  it("a section's own link opens the tab it is under, and Settings opens the last tab again", async () => {
    fakeBackend(routes());
    const first = renderAt('/settings/sso');
    expect(await screen.findByText('QUERYAPIGATE_SSO_ISSUER')).toBeInTheDocument();
    expect(within(tabs()).getByRole('tab', { name: /Sign-in/ })).toHaveAttribute('aria-selected', 'true');
    first.unmount();
    renderAt('/settings');
    expect(await screen.findByText('QUERYAPIGATE_SSO_ISSUER')).toBeInTheDocument();
  });

  it('filters every tab at once by name, description or variable, each match under its tab', async () => {
    fakeBackend(routes());
    renderAt('/settings/general');
    await screen.findByText(LONG_PATH);
    const filter = screen.getByRole('searchbox', { name: 'Filter settings by name or variable' });
    await userEvent.type(filter, 'allow');
    expect(screen.getAllByRole('heading', { level: 2 }).map((h) => h.textContent)).toEqual([
      'Security · Access',
    ]);
    expect(screen.getAllByRole('heading', { level: 3 }).map((h) => h.textContent)).toEqual(['Allow writes']);
    // no tab is the open one while the filter spans them all
    expect(within(tabs()).queryByRole('tab', { selected: true })).toBeNull();
    await userEvent.clear(filter);
    await userEvent.type(filter, 'mcp_port'); // a variable, in any case
    expect(screen.getAllByRole('heading', { level: 2 }).map((h) => h.textContent)).toEqual(['MCP server']);
    await userEvent.clear(filter);
    await userEvent.type(filter, 'identity provider'); // a section's description is not a setting's
    expect(screen.queryAllByRole('heading', { level: 3 })).toHaveLength(0);
    await userEvent.clear(filter);
    await userEvent.type(filter, 'issuer description'); // a setting's description is
    expect(screen.getAllByRole('heading', { level: 2 }).map((h) => h.textContent)).toEqual([
      'Sign-in · Console sign-in (SSO)',
    ]);
    await userEvent.clear(filter);
    await userEvent.type(filter, 'theme'); // this browser's preferences too
    expect(screen.getAllByRole('heading', { level: 2 }).map((h) => h.textContent)).toEqual([
      'This browser · Appearance',
    ]);
    await userEvent.keyboard('{Escape}'); // clears it: back to the open tab
    expect(filter).toHaveValue('');
    expect(within(tabs()).getByRole('tab', { name: /General/ })).toHaveAttribute('aria-selected', 'true');
    await userEvent.type(filter, 'allow');
    await userEvent.click(within(tabs()).getByRole('tab', { name: /MCP server/ })); // a tab clears it too
    expect(filter).toHaveValue('');
    expect(screen.getByText('QUERYAPIGATE_MCP_PORT')).toBeInTheDocument();
  });

  it('never matches a value - nothing masked can be found by its text', async () => {
    fakeBackend(routes());
    renderAt('/settings/security');
    await screen.findByText('QUERYAPIGATE_ALLOW_WRITES');
    const filter = screen.getByRole('searchbox', { name: 'Filter settings by name or variable' });
    for (const text of ['db.internal', 'configured', 'qag:***']) {
      await userEvent.clear(filter);
      await userEvent.type(filter, text);
      expect(screen.getByText(`No setting matches “${text}”.`)).toBeInTheDocument();
      expect(screen.queryAllByRole('heading', { level: 3 })).toHaveLength(0);
    }
    await userEvent.click(screen.getByRole('button', { name: 'Clear the filter' }));
    expect(filter).toHaveValue('');
    expect(screen.getByText('QUERYAPIGATE_ALLOW_WRITES')).toBeInTheDocument();
  });

  it('shows only what was changed, counted for the open tab - or wherever the filter matches', async () => {
    fakeBackend(routes());
    renderAt('/settings/general');
    await screen.findByText(LONG_PATH);
    const show = screen.getByRole('group', { name: 'Show' });
    const changed = within(show).getByRole('button', { name: /Changed/ });
    expect(changed).toHaveTextContent('Changed 2');
    expect(within(show).getByRole('button', { name: 'All' })).toHaveAttribute('aria-pressed', 'true');
    await userEvent.click(changed);
    expect(changed).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getAllByRole('heading', { level: 3 }).map((h) => h.textContent)).toEqual([
      'Home directory',
      'Load examples',
    ]);
    // a group with nothing left has no heading
    expect(screen.getAllByRole('heading', { level: 2 }).map((h) => h.textContent)).toEqual([
      'Storage',
      'Startup',
    ]);
    // it stays on from tab to tab, counting each
    await userEvent.click(within(tabs()).getByRole('tab', { name: /Security/ }));
    expect(changed).toHaveTextContent('Changed 2');
    expect(screen.getAllByRole('heading', { level: 3 }).map((h) => h.textContent)).toEqual([
      'Admin API key',
      'Metadata database',
    ]);
    await userEvent.click(within(tabs()).getByRole('tab', { name: /Sign-in/ }));
    expect(changed).toHaveTextContent('Changed 0');
    expect(screen.getByText(/Nothing in this tab was changed/)).toBeInTheDocument();
    // with the filter, counted in every tab it matches
    await userEvent.type(screen.getByRole('searchbox', { name: /Filter settings/ }), 'queryapigate_');
    expect(changed).toHaveTextContent('Changed 4');
    expect(screen.getAllByRole('heading', { level: 3 })).toHaveLength(4);
    await userEvent.click(within(show).getByRole('button', { name: 'All' }));
    expect(screen.getAllByRole('heading', { level: 3 })).toHaveLength(11); // every server setting: all have a variable
  });

  it('copies only what is set, never a secret, as .env', async () => {
    const user = userEvent.setup(); // with a clipboard stub
    fakeBackend(routes());
    renderAt('/settings');
    await screen.findByText(LONG_PATH);
    await user.click(screen.getByRole('button', { name: 'Copy as .env' }));
    expect(await screen.findByText('Copied')).toBeInTheDocument();
    // exactly as before: the raw values set, in the API's order - none of the labels the screen shows
    expect(await navigator.clipboard.readText()).toBe(
      `QUERYAPIGATE_HOME=${LONG_PATH}\nQUERYAPIGATE_LOAD_EXAMPLES=yes\n`,
    );
  });

  it('keeps the MCP server section as read-only settings (tools/reachability moved to the MCP screen)', async () => {
    fakeBackend(routes());
    renderAt('/settings');
    await screen.findByText(LONG_PATH);
    await userEvent.click(within(tabs()).getByRole('tab', { name: /MCP server/ }));
    expect(await screen.findByText('QUERYAPIGATE_MCP_PORT')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Check now' })).toBeNull();
    expect(screen.queryByText('execute_sql')).toBeNull();
  });

  it('Appearance preferences apply at once and are kept in this browser', async () => {
    fakeBackend(routes());
    renderAt('/settings');
    await userEvent.click(within(tabs()).getByRole('tab', { name: 'This browser' }));
    expect(
      screen.getByText('Stored in this browser only. Nothing is sent to the server.'),
    ).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Appearance', level: 2 })).toBeInTheDocument();
    const density = screen.getByRole('group', { name: 'Table density' });
    expect(within(density).getByRole('button', { name: 'Comfortable' })).toHaveClass('on');
    await userEvent.click(within(density).getByRole('button', { name: 'Compact' }));
    expect(document.body).toHaveClass('compact');
    await userEvent.click(
      within(screen.getByRole('group', { name: 'Theme' })).getByRole('button', { name: 'Dark' }),
    );
    expect(document.documentElement.style.colorScheme).toBe('dark');
    const fontSize = within(document.getElementById('settings-body')!).getByRole('group', {
      name: 'Font size',
    });
    expect(within(fontSize).getByRole('button', { name: 'Medium' })).toHaveClass('on');
    await userEvent.click(within(fontSize).getByRole('button', { name: 'Large' }));
    expect(document.documentElement.style.zoom).toBe('1.15');
    await userEvent.click(within(fontSize).getByRole('button', { name: 'Small' }));
    expect(document.documentElement.style.zoom).toBe('0.9');
    await userEvent.click(
      within(screen.getByRole('group', { name: 'Reduce motion' })).getByRole('button', { name: 'On' }),
    );
    expect(document.body).toHaveClass('reduce-motion');
    expect(screen.getByRole('heading', { name: 'Editor & results', level: 2 })).toBeInTheDocument();
    const pick = async (group: string, option: string) =>
      userEvent.click(
        within(screen.getByRole('group', { name: group })).getByRole('button', { name: option }),
      );
    await pick('Default result format', 'csv');
    await pick('Rows per page', '50');
    await pick('NULL values', 'Blank');
    await pick('Numbers', 'Grouped');
    await pick('Wrap long lines', 'On');
    await pick('Line numbers', 'Off');
    expect(JSON.parse(localStorage.getItem('queryapigate-ui-prefs')!)).toEqual({
      theme: 'Dark',
      fontSize: 'Small',
      timeZone: 'Local',
      timeFormat: 'Absolute',
      motion: 'On',
      density: 'Compact',
      format: 'csv',
      pageSize: '50',
      nullDisplay: 'Blank',
      numberFormat: 'Grouped',
      lineWrap: 'On',
      lineNumbers: 'Off',
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

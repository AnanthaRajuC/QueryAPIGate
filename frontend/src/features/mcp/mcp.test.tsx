import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { fakeBackend, jsonResponse } from '@/test/fakeBackend';
import { baseRoutes } from '@/test/fixtures';
import { renderAt } from '@/test/renderApp';

vi.mock('@/components/SqlEditor', () => ({ SqlEditor: () => <textarea aria-label="SQL" /> }));

beforeEach(() => localStorage.clear());

const routes = () => ({
  ...baseRoutes(),
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
  // A tool's sparkline counts only its MCP calls: the page fetches the window once, groups by query, and keeps
  // only mcp-transport runs - so films has two (the rest run is excluded).
  'GET /api/v1/history': () => ({
    items: [
      { executed_at: recent(1), status: 'success', transport: 'mcp', query: 'films' },
      { executed_at: recent(4), status: 'success', transport: 'mcp', query: 'films' },
      { executed_at: recent(2), status: 'success', transport: 'rest', query: 'films' },
    ],
    next_cursor: null,
  }),
});

const recent = (daysAgo: number) =>
  new Date(Date.now() - daysAgo * 86_400_000).toISOString().slice(0, 19).replace('T', ' ');

describe('MCP page', () => {
  it('shows how to connect a client, then lists this workspace’s tools in their own tab', async () => {
    fakeBackend(routes());
    renderAt('/mcp');
    expect(await screen.findByRole('heading', { name: 'MCP server' })).toBeInTheDocument();
    // three tabs; Connect a client first, with the Claude Desktop config snippet
    expect(
      within(screen.getByRole('tablist', { name: 'MCP server' }))
        .getAllByRole('tab')
        .map((t) => t.textContent?.replace(/\d+$/, '')),
    ).toEqual(['Connect a client', 'Reachability', 'Tools']);
    expect(screen.getByRole('tab', { name: 'Connect a client' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByText(/mcpServers/)).toBeInTheDocument();
    expect(screen.queryByText('execute_sql')).toBeNull();
    // the Tools tab counts them, and lists them
    const toolsTab = screen.getByRole('tab', { name: /Tools/ });
    expect(await within(toolsTab).findByText('2')).toBeInTheDocument();
    await userEvent.click(toolsTab);
    expect(await screen.findByText('execute_sql')).toBeInTheDocument();
    expect(screen.queryByText(/mcpServers/)).toBeNull();
    expect(screen.getByText('films')).toBeInTheDocument();
    expect(screen.getByText('connection, sql')).toBeInTheDocument();
    // The saved-query tool shows a sparkline of its MCP calls (2 mcp runs; the rest run is excluded).
    const filmsRow = screen.getByText('films').closest('tr') as HTMLElement;
    expect(
      await within(filmsRow).findByRole('img', { name: /films: 2 runs over 30 days/ }),
    ).toBeInTheDocument();
    // The built-in ad-hoc tool isn't tracked per tool.
    expect(
      within(screen.getByText('execute_sql').closest('tr') as HTMLElement).getByTitle(/Built-in tool/),
    ).toHaveTextContent('—');
  });

  it('checks reachability only when asked', async () => {
    const { calls } = fakeBackend(routes());
    renderAt('/mcp');
    await userEvent.click(await screen.findByRole('tab', { name: 'Reachability' }));
    expect(await screen.findByText('Not checked yet')).toBeInTheDocument();
    expect(calls.some((c) => c.path === '/api/v1/mcp/status')).toBe(false);
    await userEvent.click(screen.getByRole('button', { name: 'Check now' }));
    expect(await screen.findByText('Not reachable on port 8765')).toBeInTheDocument();
  });

  it('opens the tab the URL names', async () => {
    fakeBackend(routes());
    renderAt('/mcp?tab=tools');
    expect(await screen.findByText('execute_sql')).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: /Tools/ })).toHaveAttribute('aria-selected', 'true');
  });

  it('sorts and filters the tools', async () => {
    fakeBackend(routes());
    renderAt('/mcp?tab=tools');
    await screen.findByText('execute_sql');
    const names = () =>
      [...document.querySelectorAll('#mcp-tools tbody tr')].map((r) => r.querySelector('td')?.textContent);
    // Parameters: most first on the second click
    const params = within(screen.getByRole('columnheader', { name: 'Parameters' })).getByRole('button');
    await userEvent.click(params);
    await userEvent.click(params);
    expect(names()).toEqual(['execute_sql', 'films']);
    await userEvent.type(screen.getByRole('searchbox', { name: 'Filter tools' }), 'saved query');
    expect(names()).toEqual(['films']);
  });
});

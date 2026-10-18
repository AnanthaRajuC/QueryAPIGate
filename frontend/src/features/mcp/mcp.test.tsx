import { screen } from '@testing-library/react';
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
      { name: 'execute_sql', description: 'Run SQL', kind: 'ad-hoc', params: ['connection', 'sql'], read_only: true },
      { name: 'films', description: 'All films', kind: 'saved query', params: [], read_only: true },
    ],
  }),
  'GET /api/v1/mcp/status': () => jsonResponse({ reachable: false, port: 8765 }),
});

describe('MCP page', () => {
  it('lists this workspace’s tools and shows how to connect a client', async () => {
    fakeBackend(routes());
    renderAt('/mcp');
    expect(await screen.findByRole('heading', { name: 'MCP server' })).toBeInTheDocument();
    expect(await screen.findByText('execute_sql')).toBeInTheDocument();
    expect(screen.getByText('films')).toBeInTheDocument();
    expect(screen.getByText('connection, sql')).toBeInTheDocument();
    expect(screen.getByText(/mcpServers/)).toBeInTheDocument(); // the Claude Desktop config snippet
  });

  it('checks reachability only when asked', async () => {
    const { calls } = fakeBackend(routes());
    renderAt('/mcp');
    expect(await screen.findByText('Not checked yet')).toBeInTheDocument();
    expect(calls.some((c) => c.path === '/api/v1/mcp/status')).toBe(false);
    await userEvent.click(screen.getByRole('button', { name: 'Check now' }));
    expect(await screen.findByText('Not reachable on port 8765')).toBeInTheDocument();
  });
});

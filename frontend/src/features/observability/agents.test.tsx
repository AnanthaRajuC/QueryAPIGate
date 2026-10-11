import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { fakeBackend } from '@/test/fakeBackend';
import { baseRoutes } from '@/test/fixtures';
import { renderAt } from '@/test/renderApp';

vi.mock('@/components/SqlEditor', () => ({ SqlEditor: () => <textarea aria-label="SQL" /> }));

afterEach(() => vi.unstubAllGlobals());

const agent = {
  agent: 'jwt:alice',
  calls: 41,
  rows: 1830,
  errors: 3,
  masked_calls: 12,
  first_seen: '2026-10-10 09:12:44',
  last_seen: '2026-10-11 10:40:03',
  connections: ['examples'],
  queries: { example_top_films: 20 },
  adhoc_calls: 21,
  refusals: { mask_unresolvable: 2, table_not_allowed: 1 },
  top_statements: [
    {
      sql: 'SELECT first_name, email FROM customer LIMIT 2',
      sql_sha256: null,
      count: 9,
      rows: 18,
      errors: 0,
    },
  ],
};

describe('Agents', () => {
  it("lists each agent's calls, rows, masking and refusals, and opens one", async () => {
    const { calls } = fakeBackend({
      ...baseRoutes(),
      'GET /api/v1/usage/agents': () => ({
        since: '2026-10-10 10:42:07',
        until: '2026-10-11 10:42:07',
        sample_rate: 1,
        truncated: false,
        items: [agent],
      }),
    });
    renderAt('/agents');
    const row = (await screen.findByRole('button', { name: 'jwt:alice' })).closest('tr')!;
    expect(row).toHaveTextContent('1,830');
    expect(within(row).getByText('mask_unresolvable × 2')).toBeInTheDocument();
    expect(within(row).getByText('table_not_allowed × 1')).toBeInTheDocument();
    await userEvent.click(within(row).getByRole('button', { name: 'jwt:alice' }));
    expect(screen.getByText('example_top_films × 20')).toBeInTheDocument();
    expect(screen.getByText('9 calls · 18 rows')).toBeInTheDocument();
    expect(calls.find((c) => c.path === '/api/v1/usage/agents')!.search.get('hours')).toBe('24');
    await userEvent.selectOptions(screen.getByLabelText('Window'), '7d');
    await waitFor(() =>
      expect(
        calls.filter((c) => c.path === '/api/v1/usage/agents').map((c) => c.search.get('hours')),
      ).toContain('168'),
    );
  });

  it('says when history is sampled, and when nobody called', async () => {
    fakeBackend({
      ...baseRoutes(),
      'GET /api/v1/usage/agents': () => ({
        since: 'x',
        until: 'y',
        sample_rate: 0.25,
        truncated: false,
        items: [],
      }),
    });
    renderAt('/agents');
    expect(await screen.findByText('No agent called the MCP server in this window')).toBeInTheDocument();
    expect(screen.getByText(/keeps 25% of successful runs/)).toBeInTheDocument();
  });
});

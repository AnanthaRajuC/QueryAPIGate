import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { fakeBackend } from '@/test/fakeBackend';
import { apiKey, baseRoutes } from '@/test/fixtures';
import { renderAt } from '@/test/renderApp';

vi.mock('@/components/SqlEditor', () => ({ SqlEditor: () => <textarea aria-label="SQL" /> }));

afterEach(() => vi.unstubAllGlobals());

const run = (query: string, executed_at: string, duration_ms: number | null, status = 'success') => ({
  query,
  version: 1,
  executed_at,
  status,
  key_name: 'partner',
  rows: 3,
  duration_ms,
});

const routes = () => ({
  ...baseRoutes(),
  'GET /metrics': () =>
    new Response(
      'queryapigate_requests_total{status="200"} 9\nqueryapigate_requests_total{status="500"} 1\n' +
        'queryapigate_pool_active_connections 2\n',
    ),
  'GET /api/v1/api-keys': () => ({
    items: [apiKey('old', { expires_at: '2000-01-01', expired: true })],
  }),
  'GET /api/v1/audit': () => ({
    items: [
      {
        timestamp: '2026-10-04 10:00:00',
        actor: 'admin',
        action: 'create_key',
        target: 'old',
        changes: null,
      },
    ],
    total: 1,
    actions: ['create_key'],
    retention: 500,
  }),
  'GET /api/v1/history': (call: { search: URLSearchParams }) => ({
    items:
      call.search.get('limit') === '20'
        ? [run('films', '2026-10-04 10:00:02', 5), run('rentals', '2026-10-04 10:00:01', null, 'error')]
        : [run('films', '2026-10-04 10:00:02', 5), run('rentals', '2026-10-04 09:00:00', 90)],
    next_cursor: null,
  }),
});

/** The requests panel, once the lazily loaded page has rendered it. */
const panel = () =>
  waitFor(() => {
    const el = document.getElementById('home-requests-panel');
    if (!el) throw new Error('not rendered yet');
    return el;
  });

describe('Home', () => {
  it('is the screen the Console opens on: tiles, health, activity and recent requests', async () => {
    fakeBackend(routes());
    renderAt('/');
    expect(await screen.findByRole('heading', { name: 'Home' })).toBeInTheDocument();
    await waitFor(() => expect(document.getElementById('home-stats')).toHaveTextContent('Requests10'));
    expect(document.getElementById('home-stats')).toHaveTextContent('Error rate10.0%');
    expect(document.getElementById('home-pool-active')).toHaveTextContent('2');
    expect(
      await within(document.getElementById('home-health')!).findByText('1 API key expired'),
    ).toBeInTheDocument();
    expect(within(document.getElementById('home-activity')!).getByText('create_key')).toHaveClass(
      'tag act ok',
    );
    const requests = document.getElementById('home-requests-panel')!;
    await waitFor(() => expect(within(requests).getByText('films')).toBeInTheDocument());
    expect(requests.querySelector('.dot.bad')).not.toBeNull(); // the failed run
  });

  it('Slowest ranks the stored runs by duration', async () => {
    fakeBackend(routes());
    renderAt('/');
    const requests = await panel();
    await userEvent.click(within(requests).getByRole('button', { name: 'Slowest' }));
    expect(within(requests).getByRole('heading', { name: 'Slowest queries' })).toBeInTheDocument();
    await waitFor(() => expect(within(requests).getAllByRole('row')).toHaveLength(3));
    expect(within(requests).getAllByRole('row')[1]).toHaveTextContent('rentals');
  });

  it('a request opens its query on the History tab', async () => {
    fakeBackend(routes());
    renderAt('/');
    const requests = await panel();
    await userEvent.click(await within(requests).findByRole('button', { name: 'films' }));
    await waitFor(() =>
      expect(document.querySelector('#query-detail button[data-subtab="history"]')).toHaveClass('on'),
    );
  });
});

import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { fakeBackend, jsonResponse } from '@/test/fakeBackend';
import { baseRoutes, summary } from '@/test/fixtures';
import { renderAt } from '@/test/renderApp';

vi.mock('@/components/SqlEditor', () => ({ SqlEditor: () => <textarea aria-label="SQL" /> }));

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const entry = {
  key: 'abc123def456ghi',
  content_type: 'application/json',
  size_bytes: 2048,
  ttl_remaining_s: 41.6,
  meta: { name: 'films', version: 1, connection: 'lite', format: 'json' },
};

const routes = () => ({
  ...baseRoutes(),
  'GET /api/v1/queries': () => ({
    items: [summary('films', 1, 1, { cache_ttl: 60 }), summary('rentals', 1, 1, { collection: null })],
  }),
  'GET /api/v1/settings': () => ({
    items: [
      {
        id: 'cache',
        title: 'Response cache',
        description: '',
        rows: [
          {
            label: 'Backend',
            description: '',
            env: 'X',
            value: 'in-process',
            source: 'default',
            env_value: null,
          },
        ],
      },
    ],
  }),
  'GET /metrics': () =>
    new Response(
      'queryapigate_cache_hits_total 3\nqueryapigate_cache_misses_total 1\nqueryapigate_cache_entries 1\n',
    ),
  'GET /api/v1/cache/entries': () => ({ items: [entry] }),
  'GET /api/v1/cache/entries/abc123def456ghi': () => jsonResponse([{ id: 1, title: 'Alpha' }]),
});

describe('Caching', () => {
  it('shows the cache backend, hit rate, the cached queries and what is cached now', async () => {
    fakeBackend(routes());
    renderAt('/caching');
    await waitFor(() => expect(document.querySelector('.stat-tiles')).toHaveTextContent('Hit rate75.0%'));
    expect(document.querySelector('.stat-tiles')).toHaveTextContent('Cache backendin-process');
    const body = document.getElementById('caching-body')!;
    expect(within(body).getByRole('button', { name: 'films' })).toBeInTheDocument();
    expect(within(body).queryByText('rentals')).toBeNull(); // no cache_ttl
    expect(within(body).getByText('60s')).toBeInTheDocument();
    const entries = document.getElementById('cache-entries-panel')!;
    await waitFor(() => expect(entries).toHaveTextContent('1 live entry right now.'));
    expect(entries).toHaveTextContent('2.0 KB');
    expect(entries).toHaveTextContent('42s');
  });

  it('opens an entry body in the drawer, as a caller receives it', async () => {
    fakeBackend(routes());
    renderAt('/caching');
    const entries = document.getElementById('cache-entries-panel')!;
    await userEvent.click(await within(entries).findByRole('button', { name: 'films' }));
    const drawer = document.getElementById('drawer')!;
    expect(document.getElementById('drawer-kicker')).toHaveTextContent('abc123def456ghi');
    expect(await within(drawer).findByText('Alpha')).toBeInTheDocument();
  });

  it('evicts one entry, and clears all after confirming', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    const { calls } = fakeBackend({
      ...routes(),
      'DELETE /api/v1/cache/entries/abc123def456ghi': () => new Response(null, { status: 204 }),
      'DELETE /api/v1/cache/entries': () => new Response(null, { status: 204 }),
    });
    renderAt('/caching');
    const entries = document.getElementById('cache-entries-panel')!;
    await userEvent.click(await within(entries).findByRole('button', { name: 'Delete' }));
    expect(await screen.findByText('Entry deleted')).toBeInTheDocument();
    await userEvent.click(within(entries).getByRole('button', { name: 'Clear cache' }));
    expect(await screen.findByText('Cache cleared')).toBeInTheDocument();
    expect(calls.filter((c) => c.method === 'DELETE').map((c) => c.path)).toEqual([
      '/api/v1/cache/entries/abc123def456ghi',
      '/api/v1/cache/entries',
    ]);
  });
});

import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { fakeBackend } from '@/test/fakeBackend';
import { apiKey, baseRoutes, FILMS, role, summary } from '@/test/fixtures';
import { renderAt } from '@/test/renderApp';

import { presetQuery, resetAmapState } from './state';

vi.mock('@/components/SqlEditor', () => ({ SqlEditor: () => <textarea aria-label="SQL" /> }));

beforeEach(() => resetAmapState());
afterEach(() => vi.unstubAllGlobals());

const conn = (name: string, db: string) => ({
  name,
  db,
  active: true,
  host: null,
  port: null,
  database: null,
  user: null,
  example: false,
  created_at: null,
  updated_at: null,
  usage: { queries: 0, errors: 0, rows: 0, avg_duration_ms: null },
});

const routes = () => ({
  ...baseRoutes(),
  'GET /api/v1/connections': () => ({ items: [conn('lite', 'sqlite'), conn('warehouse', 'postgres')] }),
  'GET /api/v1/queries': () => ({
    items: [
      summary('films', 1, 1, { last_used_at: '2026-10-04 09:00:00' }),
      summary('rentals', 1, 3, { collection: null }),
      summary('orders', 1, 1, { collection: null, connection_name: 'warehouse' }),
    ],
  }),
  'GET /api/v1/api-keys': () => ({
    items: [
      apiKey('partner', { connections: [], collections: ['catalog'] }),
      apiKey('ops', { connections: ['lite'], active: false }),
    ],
  }),
  'GET /api/v1/roles': () => ({ items: [role('analyst', { connections: [], queries: ['orders'] })] }),
});

const row = (name: string) =>
  [...document.querySelectorAll('#accessmap-body tbody tr')].find(
    (tr) => tr.querySelector('.amap-q-name')?.textContent === name,
  ) as HTMLElement | undefined;

describe('Access map', () => {
  it('shows how each key and role reaches each query, with per-column totals', async () => {
    fakeBackend(routes());
    renderAt('/access-map');
    await waitFor(() => expect(row('films')).toBeDefined());
    const cells = (name: string) =>
      [...row(name)!.querySelectorAll('td.amap-cell')].map((td) => td.textContent);
    // columns: ops, partner (keys), analyst (role)
    expect(cells('films')).toEqual(['W', 'C', '']);
    expect(cells('rentals')).toEqual(['W', '', '']);
    expect(cells('orders')).toEqual(['', '', 'Q']);
    expect(row('films')).toHaveTextContent('2026-10-04 09:00:00');
    expect(row('rentals')).toHaveTextContent('v3');
    expect(document.querySelector('th[title="ops (revoked)"]')).not.toBeNull();
    expect(document.querySelector('#accessmap-body tfoot')).toHaveTextContent('2 / 3');
  });

  it('filters by connection, database and reach; search narrows the axis it matches', async () => {
    fakeBackend(routes());
    renderAt('/access-map');
    await waitFor(() => expect(row('films')).toBeDefined());
    await userEvent.selectOptions(screen.getByLabelText('Reach'), 'unreachable');
    expect(row('orders')).toBeDefined();
    expect(row('films')).toBeUndefined();
    await userEvent.selectOptions(screen.getByLabelText('Reach'), '');
    await userEvent.selectOptions(screen.getByLabelText('Database'), 'postgres');
    expect(row('films')).toBeUndefined();
    await userEvent.selectOptions(screen.getByLabelText('Database'), '');
    await userEvent.type(screen.getByPlaceholderText('Filter by query or key…'), 'partner');
    expect(row('films')).toBeDefined(); // the search matched a key column, so every query stays
    expect(document.querySelectorAll('#accessmap-body thead th.amap-key-head')).toHaveLength(2); // partner + analyst
  });

  it('sorts by a column, then reverses, then returns to the default order', async () => {
    fakeBackend(routes());
    renderAt('/access-map');
    await waitFor(() => expect(row('films')).toBeDefined());
    const names = () =>
      [...document.querySelectorAll('#accessmap-body .amap-q-name')].map((b) => b.textContent);
    expect(names()).toEqual(['films', 'orders', 'rentals']); // collection first, then by name
    const versionHeader = screen.getByRole('columnheader', { name: /^Version/ });
    await userEvent.click(versionHeader);
    expect(names()).toEqual(['films', 'orders', 'rentals']);
    await userEvent.click(versionHeader);
    expect(names()[0]).toBe('rentals');
    expect(versionHeader).toHaveTextContent('Version ▼');
    await userEvent.click(versionHeader);
    expect(versionHeader).toHaveTextContent(/^Version$/);
  });

  it('opens on one query from elsewhere, and its details in the drawer', async () => {
    presetQuery('films');
    fakeBackend({
      ...routes(),
      'GET /api/v1/queries/films': () => ({
        ...FILMS,
        created_at: null,
        last_used_at: null,
        cache_ttl: null,
      }),
    });
    renderAt('/access-map');
    await waitFor(() => expect(row('films')).toBeDefined());
    expect(row('rentals')).toBeUndefined();
    expect(screen.getByPlaceholderText('Filter by query or key…')).toHaveValue('films');
    await userEvent.click(within(row('films')!).getByRole('button', { name: 'Details for films' }));
    const drawer = document.getElementById('drawer')!;
    expect(await within(drawer).findByText('Versions')).toBeInTheDocument();
    expect(within(drawer).getByRole('button', { name: 'Open in API Repository' })).toBeInTheDocument();
  });
});

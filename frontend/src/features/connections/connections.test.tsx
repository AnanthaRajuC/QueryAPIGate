import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { fakeBackend, jsonResponse } from '@/test/fakeBackend';
import { baseRoutes } from '@/test/fixtures';
import { renderAt } from '@/test/renderApp';

vi.mock('@/components/SqlEditor', () => ({ SqlEditor: () => <textarea aria-label="SQL" /> }));

afterEach(() => vi.unstubAllGlobals());

const conn = (name: string, extra: Record<string, unknown> = {}) => ({
  name,
  db: 'postgres',
  active: true,
  host: 'db.internal',
  port: 5432,
  database: 'shop',
  user: 'app',
  example: false,
  created_at: '2026-10-01 10:00:00',
  updated_at: '2026-10-02 10:00:00',
  usage: { queries: 4, errors: 1, rows: 40, avg_duration_ms: 2.5 },
  ...extra,
});

const routes = () => ({
  ...baseRoutes(),
  'GET /api/v1/connections': () => ({
    items: [
      conn('shop'),
      conn('old', { active: false, usage: { queries: 0, errors: 0, rows: 0, avg_duration_ms: null } }),
    ],
  }),
  'GET /api/v1/connections/deleted': () => ({
    items: [
      {
        name: 'gone',
        db: 'mysql',
        host: 'h',
        port: 3306,
        database: 'x',
        deleted_at: '2026-10-03',
        deleted_by: 'admin',
        reason: 'retired',
      },
    ],
  }),
  'GET /api/v1/connections/shop': () =>
    jsonResponse({ ...conn('shop'), password: '********', options: { sslmode: 'require' } }, 200, {
      ETag: '"c1"',
    }),
  'POST /api/v1/connections/databases': () => ({ databases: ['shop', 'analytics'] }),
});

const table = () => document.getElementById('connections-table')!;

describe('Connections', () => {
  it('lists connections with status and live usage, filtered by the All/Active/Inactive/Deleted segments', async () => {
    fakeBackend(routes());
    renderAt('/connections');
    await waitFor(() => expect(table().querySelector('tr[data-name="shop"]')).not.toBeNull());
    expect(table()).toHaveTextContent('4 queries · 1 failed · 2.5ms avg');
    expect(table()).toHaveTextContent('db.internal:5432');
    expect(document.getElementById('connections-foot')).toHaveTextContent('Showing 2 of 2 connections');
    await userEvent.click(
      within(document.getElementById('conn-tabs')!).getByRole('button', { name: /Inactive/ }),
    );
    expect(table().querySelector('tr[data-name="shop"]')).toBeNull();
    expect(table().querySelector('tr[data-name="old"]')).not.toBeNull();
    await userEvent.click(
      within(document.getElementById('conn-tabs')!).getByRole('button', { name: /Deleted/ }),
    );
    expect(table()).toHaveTextContent('retired');
  });

  it("shows timestamps in the viewer's chosen zone and format, the exact time on hover", async () => {
    fakeBackend({
      ...routes(),
      'GET /health': () => ({
        status: 'ok',
        version: '9.9.9',
        time_zone: 'Asia/Kolkata',
        utc_offset: '+05:30',
      }),
    });
    renderAt('/connections');
    await waitFor(() => expect(table().querySelector('tr[data-name="shop"]')).not.toBeNull());
    const created = () => table().querySelector('tr[data-name="shop"] time')!;
    // Local by default - this computer is UTC under test, the server is 5:30 ahead
    await waitFor(() => expect(created()).toHaveTextContent('2026-10-01 04:30:00'));
    expect(created()).toHaveAttribute('dateTime', '2026-10-01T04:30:00.000Z');
    expect(created().getAttribute('title')).toContain("this computer's time zone");
    localStorage.setItem('queryapigate-ui-prefs', JSON.stringify({ timeZone: 'Server' }));
    window.dispatchEvent(new StorageEvent('storage'));
    await waitFor(() => expect(created()).toHaveTextContent('2026-10-01 10:00:00'));
    expect(created().getAttribute('title')).toContain('Asia/Kolkata');
    vi.useFakeTimers({ toFake: ['Date'], now: new Date('2026-10-01T06:30:00Z') });
    localStorage.setItem('queryapigate-ui-prefs', JSON.stringify({ timeFormat: 'Relative' }));
    window.dispatchEvent(new StorageEvent('storage'));
    await waitFor(() => expect(created()).toHaveTextContent('2 h ago'));
    expect(created().getAttribute('title')).toContain('2026-10-01 04:30:00');
    vi.useRealTimers();
    localStorage.clear();
  });

  it('creates a connection from the drawer', async () => {
    const { calls } = fakeBackend({
      ...routes(),
      'POST /api/v1/connections': () => jsonResponse({ ...conn('films'), password: null, options: {} }, 201),
    });
    renderAt('/connections');
    await userEvent.click(await screen.findByRole('button', { name: 'New connection' }));
    const drawer = document.getElementById('drawer')!;
    await userEvent.type(within(drawer).getByLabelText('Name'), 'films');
    await userEvent.selectOptions(within(drawer).getByLabelText('Database type'), 'sqlite');
    await userEvent.type(within(drawer).getByLabelText('Default database'), '/data/films.db');
    await userEvent.click(within(drawer).getByRole('button', { name: 'Create' }));
    await waitFor(() =>
      expect(calls.some((c) => c.method === 'POST' && c.path === '/api/v1/connections')).toBe(true),
    );
    expect(calls.find((c) => c.method === 'POST' && c.path === '/api/v1/connections')!.body).toEqual({
      name: 'films',
      db: 'sqlite',
      password: '',
      database: '/data/films.db',
      active: true,
    });
    expect(await screen.findByText('Created films')).toBeInTheDocument();
  });

  it('edits a connection: the mask keeps the password, an emptied field is removed, If-Match is sent', async () => {
    const { calls } = fakeBackend({
      ...routes(),
      'PATCH /api/v1/connections/shop': () =>
        jsonResponse({ ...conn('shop'), password: '********', options: {} }),
    });
    renderAt('/connections');
    await waitFor(() => expect(table().querySelector('tr[data-name="shop"]')).not.toBeNull());
    await userEvent.click(
      within(table().querySelector('tr[data-name="shop"]')!).getByRole('button', { name: 'Edit' }),
    );
    const drawer = document.getElementById('drawer')!;
    await waitFor(() => expect(within(drawer).getByLabelText('Host')).toHaveValue('db.internal'));
    expect(within(drawer).getByLabelText('Password')).toHaveValue('********');
    // a saved connection of a switchable type loads its server's databases by itself
    await waitFor(() => expect(within(drawer).getByText('✓ 2 databases')).toBeInTheDocument());
    await userEvent.clear(within(drawer).getByLabelText('User'));
    await userEvent.click(within(drawer).getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(calls.some((c) => c.method === 'PATCH')).toBe(true));
    const patch = calls.find((c) => c.method === 'PATCH')!;
    expect(patch.headers.get('If-Match')).toBe('"c1"');
    expect(patch.body).toMatchObject({ host: 'db.internal', user: null, password: '********', active: true });
  });

  it('shows why a test connection failed, with the driver message', async () => {
    fakeBackend({
      ...routes(),
      'POST /api/v1/connections/test': () =>
        jsonResponse(
          {
            error: 'Could not connect',
            code: 'connection_failed',
            request_id: 'r',
            detail: 'timeout expired',
          },
          502,
        ),
    });
    renderAt('/connections');
    await userEvent.click(await screen.findByRole('button', { name: 'New connection' }));
    const drawer = document.getElementById('drawer')!;
    await userEvent.click(within(drawer).getByRole('button', { name: 'Test connection' }));
    expect(await within(drawer).findByText('✗ Could not connect: timeout expired')).toBeInTheDocument();
  });

  it('deletes only after a reason and the name typed back', async () => {
    const { calls } = fakeBackend({
      ...routes(),
      'DELETE /api/v1/connections/shop': () => new Response(null, { status: 204 }),
    });
    renderAt('/connections');
    await waitFor(() => expect(table().querySelector('tr[data-name="shop"]')).not.toBeNull());
    await userEvent.click(
      within(table().querySelector('tr[data-name="shop"]')!).getByRole('button', { name: 'Delete' }),
    );
    const drawer = document.getElementById('drawer')!;
    const button = within(drawer).getByRole('button', { name: 'Delete' });
    expect(button).toBeDisabled();
    await userEvent.type(within(drawer).getByLabelText('Reason'), 'moved to the new cluster');
    expect(button).toBeDisabled();
    await userEvent.type(within(drawer).getByLabelText('Type "shop" to confirm'), 'shop');
    expect(button).toBeEnabled();
    await userEvent.click(button);
    await waitFor(() => expect(calls.some((c) => c.method === 'DELETE')).toBe(true));
    expect(calls.find((c) => c.method === 'DELETE')!.body).toEqual({ reason: 'moved to the new cluster' });
    expect(await screen.findByText('Deleted shop')).toBeInTheDocument();
  });
});

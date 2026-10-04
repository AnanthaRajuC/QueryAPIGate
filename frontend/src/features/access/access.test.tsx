import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { fakeBackend, jsonResponse } from '@/test/fakeBackend';
import { apiKey, baseRoutes, role } from '@/test/fixtures';
import { renderAt } from '@/test/renderApp';

vi.mock('@/components/SqlEditor', () => ({ SqlEditor: () => <textarea aria-label="SQL" /> }));

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const soon = new Date(Date.now() + 3 * 24 * 60 * 60 * 1000).toISOString().slice(0, 10);

const routes = () => ({
  ...baseRoutes(),
  'GET /api/v1/api-keys': () => ({
    items: [
      apiKey('partner', {
        connections: ['lite'],
        queries: ['films', { name: 'rentals', allow_writes: true }],
        collections: ['catalog'],
        allow_writes: true,
        allowed_write_ops: ['insert'],
        allowed_ips: ['10.0.0.0/8'],
        expires_at: soon,
        usage: { queries: 5, errors: 2, rows: 50 },
      }),
      apiKey('old', { active: false, expires_at: '2000-01-01', expired: true }),
    ],
  }),
  'GET /api/v1/roles': () => ({
    items: [
      role('analyst', { connections: ['lite'], rate_limit: '100/minute', keys_created: 2, example: true }),
    ],
  }),
  'GET /api/v1/api-keys/partner': () =>
    jsonResponse(
      apiKey('partner', {
        connections: ['lite'],
        queries: ['films'],
        rate_limit: '60/minute',
      }),
      200,
      { ETag: '"k1"' },
    ),
});

const keysTable = () => document.getElementById('apikeys-table')!;
const rolesTable = () => document.getElementById('roles-table')!;
const row = (name: string) => keysTable().querySelector(`tr[data-name="${name}"]`) as HTMLElement;

describe('API keys', () => {
  it('lists keys with their scope, access, IPs, expiry and usage', async () => {
    fakeBackend(routes());
    renderAt('/api-keys');
    await waitFor(() => expect(row('partner')).not.toBeNull());
    expect(document.getElementById('apikeys-sub')).toHaveTextContent(
      '2 keys. Scope each one to connections, collections or roles.',
    );
    const partner = row('partner');
    expect(partner).toHaveTextContent('conn: lite');
    expect(partner).toHaveTextContent('rentals (write)');
    expect(partner).toHaveTextContent('catalog/');
    expect(partner).toHaveTextContent('Read/write (1 op)');
    expect(partner).toHaveTextContent('1 IP');
    expect(partner).toHaveTextContent(`${soon} (expires soon)`);
    expect(partner).toHaveTextContent('5 queries · 2 failed');
    expect(row('old')).toHaveTextContent('2000-01-01 (expired)');
    expect(row('old').querySelector('.dot.off')).toHaveAttribute('title', 'Revoked');
  });

  it('creates a key with explicit grants, then shows its secret once', async () => {
    const { calls } = fakeBackend({
      ...routes(),
      'POST /api/v1/api-keys': () =>
        jsonResponse({ ...apiKey('reporting'), secret: 'sk_secret123' }, 201, {
          'Cache-Control': 'no-store',
        }),
    });
    renderAt('/api-keys');
    await userEvent.click(await screen.findByRole('button', { name: 'New API key' }));
    const drawer = document.getElementById('drawer')!;
    await userEvent.type(within(drawer).getByLabelText('Name'), 'reporting');
    await userEvent.click(within(drawer).getByLabelText('All connections'));
    await userEvent.click(await within(drawer).findByRole('checkbox', { name: 'lite' }));
    await userEvent.type(within(drawer).getByLabelText('Rate limit'), '10/minute');
    await userEvent.type(within(drawer).getByLabelText('Allowed IPs'), '10.0.0.1, 10.0.0.2');
    await userEvent.click(within(drawer).getByRole('button', { name: 'Create key' }));
    await waitFor(() => expect(calls.some((c) => c.method === 'POST')).toBe(true));
    expect(calls.find((c) => c.method === 'POST')!.body).toEqual({
      name: 'reporting',
      connections: ['lite'],
      allow_writes: false,
      queries: [],
      collections: [],
      rate_limit: '10/minute',
      allowed_ips: ['10.0.0.1', '10.0.0.2'],
      allowed_write_ops: null,
      allowed_tables: null,
      expires_at: null,
    });
    expect(await within(drawer).findByLabelText('Secret key')).toHaveValue('sk_secret123');
    expect(document.getElementById('drawer-title')).toHaveTextContent('API key created');
    expect(drawer).toHaveTextContent('Store this now — it cannot be shown again.');
  });

  it('creates a key from a role, sending only the role and the expiry', async () => {
    const { calls } = fakeBackend({
      ...routes(),
      'POST /api/v1/api-keys': () => jsonResponse({ ...apiKey('k'), secret: 'sk_x' }, 201),
    });
    renderAt('/api-keys');
    await userEvent.click(await screen.findByRole('button', { name: 'New API key' }));
    const drawer = document.getElementById('drawer')!;
    await userEvent.type(within(drawer).getByLabelText('Name'), 'k');
    await waitFor(() => expect(within(drawer).getByLabelText('Create from')).toBeInTheDocument());
    await userEvent.selectOptions(within(drawer).getByLabelText('Create from'), 'analyst');
    await userEvent.click(within(drawer).getByRole('button', { name: 'Create key' }));
    await waitFor(() => expect(calls.some((c) => c.method === 'POST')).toBe(true));
    expect(calls.find((c) => c.method === 'POST')!.body).toEqual({
      name: 'k',
      role: 'analyst',
      expires_at: null,
    });
  });

  it('edits a key with If-Match, and can revoke it through Active', async () => {
    const { calls } = fakeBackend({
      ...routes(),
      'PATCH /api/v1/api-keys/partner': () => jsonResponse(apiKey('partner', { active: false })),
    });
    renderAt('/api-keys');
    await waitFor(() => expect(row('partner')).not.toBeNull());
    await userEvent.click(within(row('partner')).getByRole('button', { name: 'Edit' }));
    const drawer = document.getElementById('drawer')!;
    await waitFor(() => expect(within(drawer).getByLabelText('Rate limit')).toHaveValue('60/minute'));
    expect(within(drawer).getByLabelText('Name')).toBeDisabled();
    expect(within(drawer).queryByLabelText('Create from')).toBeNull();
    await userEvent.click(within(drawer).getByLabelText(/^Active/));
    await userEvent.click(within(drawer).getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(calls.some((c) => c.method === 'PATCH')).toBe(true));
    const patch = calls.find((c) => c.method === 'PATCH')!;
    expect(patch.headers.get('If-Match')).toBe('"k1"');
    expect(patch.body).toMatchObject({ active: false, connections: ['lite'], queries: ['films'] });
    expect(await screen.findByText('Updated partner')).toBeInTheDocument();
  });

  it('revokes a key after confirming', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
    const { calls } = fakeBackend({
      ...routes(),
      'DELETE /api/v1/api-keys/old': () => new Response(null, { status: 204 }),
    });
    renderAt('/api-keys');
    await waitFor(() => expect(row('old')).not.toBeNull());
    await userEvent.click(within(row('old')).getByRole('button', { name: 'Revoke' }));
    expect(confirm).toHaveBeenCalledWith(
      "Revoke API key 'old'? Anything still using it will stop working immediately.",
    );
    await waitFor(() => expect(calls.some((c) => c.method === 'DELETE')).toBe(true));
    expect(await screen.findByText('Revoked old')).toBeInTheDocument();
  });

  it('shows the classic empty state', async () => {
    fakeBackend({ ...routes(), 'GET /api/v1/api-keys': () => ({ items: [] }) });
    renderAt('/api-keys');
    expect(await screen.findByText('No scoped API keys yet')).toBeInTheDocument();
  });
});

describe('Roles', () => {
  it('lists roles, and "New key from this" preselects the role', async () => {
    fakeBackend(routes());
    renderAt('/roles');
    await waitFor(() => expect(rolesTable().querySelector('tr[data-name="analyst"]')).not.toBeNull());
    const analyst = rolesTable().querySelector('tr[data-name="analyst"]') as HTMLElement;
    expect(analyst).toHaveTextContent('example');
    expect(analyst).toHaveTextContent('lite');
    expect(analyst).toHaveTextContent('100/minute');
    expect(analyst).toHaveTextContent('2');
    await userEvent.click(within(analyst).getByRole('button', { name: 'New key from this' }));
    const drawer = document.getElementById('drawer')!;
    await waitFor(() => expect(within(drawer).getByLabelText('Create from')).toHaveValue('analyst'));
  });

  it('creates a role', async () => {
    const { calls } = fakeBackend({
      ...routes(),
      'POST /api/v1/roles': () => jsonResponse(role('writer'), 201),
    });
    renderAt('/roles');
    await userEvent.click(await screen.findByRole('button', { name: 'New role' }));
    const drawer = document.getElementById('drawer')!;
    await userEvent.type(within(drawer).getByLabelText('Name'), 'writer');
    await userEvent.click(within(drawer).getByLabelText(/^Allow writes/));
    await userEvent.type(within(drawer).getByLabelText('Allowed write operations'), 'INSERT, update');
    await userEvent.click(within(drawer).getByRole('button', { name: 'Create role' }));
    await waitFor(() => expect(calls.some((c) => c.method === 'POST')).toBe(true));
    expect(calls.find((c) => c.method === 'POST')!.body).toMatchObject({
      name: 'writer',
      connections: '*',
      allow_writes: true,
      allowed_write_ops: ['insert', 'update'],
    });
    expect(await screen.findByText('Created role writer')).toBeInTheDocument();
  });

  it('deletes a role after confirming', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    const { calls } = fakeBackend({
      ...routes(),
      'DELETE /api/v1/roles/analyst': () => new Response(null, { status: 204 }),
    });
    renderAt('/roles');
    await waitFor(() => expect(rolesTable().querySelector('tr[data-name="analyst"]')).not.toBeNull());
    await userEvent.click(
      within(rolesTable().querySelector('tr[data-name="analyst"]') as HTMLElement).getByRole('button', {
        name: 'Delete',
      }),
    );
    await waitFor(() => expect(calls.some((c) => c.method === 'DELETE')).toBe(true));
    expect(await screen.findByText('Deleted role analyst')).toBeInTheDocument();
  });
});

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

  it('sends column-masking rules - complete rows only', async () => {
    const { calls } = fakeBackend({
      ...routes(),
      'POST /api/v1/api-keys': () => jsonResponse({ ...apiKey('partner'), secret: 'sk_1' }, 201),
    });
    renderAt('/api-keys');
    await userEvent.click(await screen.findByRole('button', { name: 'New API key' }));
    const drawer = document.getElementById('drawer')!;
    await userEvent.type(within(drawer).getByLabelText('Name'), 'partner');
    await userEvent.click(within(drawer).getByRole('button', { name: 'Add a masking rule' }));
    await userEvent.type(within(drawer).getByLabelText('Masked column 1'), 'customers.email');
    await userEvent.click(within(drawer).getByRole('button', { name: 'Add a masking rule' }));
    await userEvent.type(within(drawer).getByLabelText('Masked column 2'), '*.phone');
    await userEvent.selectOptions(within(drawer).getByLabelText('Masking action 2'), 'mask');
    await userEvent.click(within(drawer).getByRole('button', { name: 'Add a masking rule' })); // left empty
    await userEvent.click(within(drawer).getByRole('button', { name: 'Create key' }));
    await waitFor(() => expect(calls.some((c) => c.method === 'POST')).toBe(true));
    expect((calls.find((c) => c.method === 'POST')!.body as { mask: unknown }).mask).toEqual([
      { column: 'customers.email', action: 'hash' },
      { column: '*.phone', action: 'mask' },
    ]);
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
      mask: null,
      max_concurrent: null,
      query_timeout: null,
      max_rows: null,
      quota: null,
      expires_at: null,
    });
    expect(await within(drawer).findByLabelText('Secret key')).toHaveValue('sk_secret123');
    expect(document.getElementById('drawer-title')).toHaveTextContent('API key created');
    expect(drawer).toHaveTextContent('Store this now — it cannot be shown again.');
  });

  it("sends a key's own limits and its quota, and leaves blanks unset", async () => {
    const { calls } = fakeBackend({
      ...routes(),
      'POST /api/v1/api-keys': () => jsonResponse({ ...apiKey('metered'), secret: 'sk_m' }, 201),
    });
    renderAt('/api-keys');
    await userEvent.click(await screen.findByRole('button', { name: 'New API key' }));
    const drawer = document.getElementById('drawer')!;
    await userEvent.type(within(drawer).getByLabelText('Name'), 'metered');
    await userEvent.type(within(drawer).getByLabelText('Queries at once'), '2');
    await userEvent.type(within(drawer).getByLabelText('Rows per call'), '500');
    await userEvent.type(within(drawer).getByLabelText('Quota: calls'), '100000');
    await userEvent.selectOptions(within(drawer).getByLabelText('Per'), 'day');
    await userEvent.click(within(drawer).getByRole('button', { name: 'Create key' }));
    await waitFor(() => expect(calls.some((c) => c.method === 'POST')).toBe(true));
    expect(calls.find((c) => c.method === 'POST')!.body).toMatchObject({
      max_concurrent: 2,
      query_timeout: null,
      max_rows: 500,
      quota: { calls: 100000, per: 'day' },
    });
  });

  it('shows how much of its quota a key has used', async () => {
    fakeBackend({
      ...routes(),
      'GET /api/v1/api-keys': () => ({
        items: [
          apiKey('metered', {
            quota: { calls: 1000, rows: 100000, per: 'month' },
            usage_this_period: {
              per: 'month',
              period_start: '2026-10-01 00:00:00',
              resets_at: '2026-11-01 00:00:00',
              calls: 850,
              rows: 20000,
              calls_limit: 1000,
              rows_limit: 100000,
            },
          }),
        ],
      }),
    });
    renderAt('/api-keys');
    await waitFor(() => expect(row('metered')).not.toBeNull());
    expect(row('metered')).toHaveTextContent('850 / 1,000 calls this month');
    expect(row('metered').querySelector('.quota-bar')).toHaveClass('near');
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

  it('rotates the secret, keeping the old one working for the chosen time, and shows the new one once', async () => {
    const { calls } = fakeBackend({
      ...routes(),
      'POST /api/v1/api-keys/partner/rotate': () =>
        jsonResponse(
          { ...apiKey('partner', { previous_expires_at: '2026-10-12 18:00:00' }), secret: 'sk_new456' },
          200,
          { 'Cache-Control': 'no-store' },
        ),
    });
    renderAt('/api-keys');
    await waitFor(() => expect(row('partner')).not.toBeNull());
    await userEvent.click(within(row('partner')).getByRole('button', { name: 'Edit' }));
    const drawer = document.getElementById('drawer')!;
    await userEvent.selectOptions(await within(drawer).findByLabelText('Secret'), '1440');
    await userEvent.click(within(drawer).getByRole('button', { name: 'Rotate secret' }));
    await waitFor(() => expect(calls.some((c) => c.path.endsWith('/rotate'))).toBe(true));
    expect(calls.find((c) => c.path.endsWith('/rotate'))!.body).toEqual({ grace_minutes: 1440 });
    expect(await within(drawer).findByLabelText('New secret')).toHaveValue('sk_new456');
    expect(document.getElementById('drawer-title')).toHaveTextContent('Secret rotated');
    expect(drawer).toHaveTextContent('The old secret keeps working until');
  });

  it('shows a rotation in progress, and ends the old secret after confirming', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
    const rotating = apiKey('partner', {
      previous_expires_at: '2026-10-12 18:00:00',
      previous_last_used_at: '2026-10-11 17:58:00',
    });
    const { calls } = fakeBackend({
      ...routes(),
      'GET /api/v1/api-keys': () => ({ items: [rotating] }),
      'GET /api/v1/api-keys/partner': () => jsonResponse(rotating, 200, { ETag: '"k2"' }),
      'DELETE /api/v1/api-keys/partner/previous': () => jsonResponse(apiKey('partner')),
    });
    renderAt('/api-keys');
    await waitFor(() => expect(row('partner')).not.toBeNull());
    expect(within(row('partner')).getByText('rotating')).toBeInTheDocument();
    await userEvent.click(within(row('partner')).getByRole('button', { name: 'Edit' }));
    const drawer = document.getElementById('drawer')!;
    await waitFor(() => expect(drawer).toHaveTextContent('The old secret works until'));
    expect(within(drawer).queryByRole('button', { name: 'Rotate secret' })).toBeNull();
    await userEvent.click(within(drawer).getByRole('button', { name: 'End now' }));
    expect(confirm).toHaveBeenCalledWith(
      "Stop the old secret of 'partner' now? Anything still using it fails.",
    );
    await waitFor(() => expect(calls.some((c) => c.method === 'DELETE')).toBe(true));
    expect(await screen.findByText('The old secret of partner stopped working')).toBeInTheDocument();
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

describe('Managed by a configuration file', () => {
  const managed = (locked: boolean) => ({
    ...routes(),
    'GET /api/v1/config/managed': () => ({
      locked,
      items: [
        { kind: 'roles', name: 'analyst', applied_at: '2026-10-12 09:00:00', file_sha256: 'ab'.repeat(32) },
      ],
    }),
  });

  it('tags what a file manages, and says when it is locked to the file', async () => {
    fakeBackend(managed(false));
    renderAt('/roles');
    const analyst = await waitFor(() => {
      const found = rolesTable().querySelector('tr[data-name="analyst"]') as HTMLElement;
      expect(within(found).getByText('managed')).toBeInTheDocument();
      return found;
    });
    expect(within(analyst).getByText('managed').getAttribute('title')).toContain('shows as drift');
  });

  it('says locked when the server takes changes only from a file', async () => {
    fakeBackend(managed(true));
    renderAt('/roles');
    expect(await screen.findByText('managed · locked')).toBeInTheDocument();
  });

  it('tags nothing a file does not manage', async () => {
    fakeBackend(routes());
    renderAt('/roles');
    await waitFor(() => expect(rolesTable().querySelector('tr[data-name="analyst"]')).not.toBeNull());
    expect(screen.queryByText('managed')).toBeNull();
  });
});

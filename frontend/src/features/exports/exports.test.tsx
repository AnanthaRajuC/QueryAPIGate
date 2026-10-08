import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { fakeBackend, jsonResponse } from '@/test/fakeBackend';
import { baseRoutes } from '@/test/fixtures';
import { renderAt } from '@/test/renderApp';

vi.mock('@/components/SqlEditor', () => ({ SqlEditor: () => <textarea aria-label="SQL" /> }));

afterEach(() => vi.unstubAllGlobals());

const me = (role: string, capabilities: string[]) => () => ({
  name: `${role}-1`,
  role,
  via: 'token',
  capabilities,
  data_access: role !== 'auditor',
});
const ADMIN_CAPS = [
  'exports.read',
  'exports.write',
  'exports.run',
  'destinations.write',
  'queries.read',
  'self',
];
const DEVELOPER_CAPS = ['exports.read', 'exports.run', 'queries.read', 'queries.write', 'self'];

const anExport = {
  name: 'acme-daily',
  query: 'films',
  params: { region: 'EU' },
  format: 'parquet',
  destination: 'drop',
  path: 'orders/{date}/{run}.parquet',
  incremental: { column: 'updated_at', parameter: 'since', start: '2026-01-01' },
  skip_empty: true,
  watermark: '2026-10-02 09:00:00',
  running: false,
  created_at: '2026-10-08 09:00:00',
  updated_at: '2026-10-08 09:00:00',
};
const aDestination = {
  name: 'drop',
  url: 's3://acme/from-us/',
  storage: 's3',
  region: 'eu-west-1',
  user: 'AKIA1',
  password: '********',
  created_at: '2026-10-08 09:00:00',
  updated_at: '2026-10-08 09:00:00',
};

describe('Exports', () => {
  it('lists exports with their watermark, and runs one', async () => {
    const { calls } = fakeBackend({
      ...baseRoutes(),
      'GET /api/v1/me': me('admin', ADMIN_CAPS),
      'GET /api/v1/exports': () => ({ items: [anExport] }),
      'POST /api/v1/exports/acme-daily/runs': () =>
        jsonResponse(
          {
            id: 'r1',
            export: 'acme-daily',
            object: 's3://acme/from-us/orders/2026-10-08/r1.parquet',
            rows: 42,
            bytes: null,
            watermark: { from: '2026-10-02 09:00:00', to: '2026-10-08 09:00:00' },
            duration_ms: 80,
          },
          201,
        ),
    });
    renderAt('/exports');
    const row = (await screen.findByText('acme-daily')).closest('tr')!;
    expect(row).toHaveTextContent('drop:orders/{date}/{run}.parquet');
    expect(row).toHaveTextContent('updated_at > 2026-10-02 09:00:00');
    await userEvent.click(within(row).getByRole('button', { name: 'Run now' }));
    expect(await screen.findByText(/42 rows written to s3:\/\/acme\/from-us\/orders/)).toBeInTheDocument();
    expect(calls.some((c) => c.method === 'POST' && c.path === '/api/v1/exports/acme-daily/runs')).toBe(true);
  });

  it('a developer may run an export but not define one', async () => {
    fakeBackend({
      ...baseRoutes(),
      'GET /api/v1/me': me('developer', DEVELOPER_CAPS),
      'GET /api/v1/exports': () => ({ items: [anExport] }),
    });
    renderAt('/exports');
    const row = (await screen.findByText('acme-daily')).closest('tr')!;
    expect(within(row).getByRole('button', { name: 'Run now' })).toBeInTheDocument();
    expect(within(row).queryByRole('button', { name: 'Edit' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'New export' })).toBeNull();
  });

  it('creates an incremental export from the form', async () => {
    const { calls } = fakeBackend({
      ...baseRoutes(),
      'GET /api/v1/me': me('admin', ADMIN_CAPS),
      'GET /api/v1/exports': () => ({ items: [] }),
      'GET /api/v1/destinations': () => ({ items: [aDestination] }),
      'POST /api/v1/exports': () => jsonResponse({ ...anExport, name: 'nightly' }, 201),
    });
    renderAt('/exports');
    await userEvent.click(await screen.findByRole('button', { name: 'New export' }));
    const drawer = document.getElementById('drawer')!;
    await userEvent.type(within(drawer).getByLabelText('Name'), 'nightly');
    await waitFor(() => expect(within(drawer).getByRole('option', { name: 'films' })).toBeInTheDocument());
    await userEvent.selectOptions(within(drawer).getByLabelText(/Saved query/), 'films');
    await userEvent.selectOptions(within(drawer).getByLabelText('Destination'), 'drop');
    await userEvent.selectOptions(within(drawer).getByLabelText('Format'), 'csv');
    expect(within(drawer).getByLabelText(/File path/)).toHaveValue('{name}/{date}/{name}_{run}.csv');
    await userEvent.click(within(drawer).getByLabelText(/Parameters/));
    await userEvent.paste('{"region": "EU"}');
    await userEvent.click(within(drawer).getByLabelText(/Incremental/));
    await userEvent.type(within(drawer).getByLabelText('Column'), 'updated_at');
    await userEvent.type(within(drawer).getByLabelText('Parameter'), 'since');
    await userEvent.type(within(drawer).getByLabelText('Start'), '2026-01-01');
    await userEvent.click(within(drawer).getByRole('button', { name: 'Create' }));
    await waitFor(() =>
      expect(calls.some((c) => c.method === 'POST' && c.path === '/api/v1/exports')).toBe(true),
    );
    expect(calls.find((c) => c.method === 'POST' && c.path === '/api/v1/exports')!.body).toEqual({
      name: 'nightly',
      query: 'films',
      destination: 'drop',
      format: 'csv',
      path: '{name}/{date}/{name}_{run}.csv',
      params: { region: 'EU' },
      skip_empty: true,
      incremental: { column: 'updated_at', parameter: 'since', start: '2026-01-01' },
    });
  });
});

describe('Destinations', () => {
  it('creates a bucket destination, testing it first', async () => {
    const { calls } = fakeBackend({
      ...baseRoutes(),
      'GET /api/v1/me': me('admin', ADMIN_CAPS),
      'GET /api/v1/destinations': () => ({ items: [] }),
      'POST /api/v1/destinations/test': () => ({
        object: 's3://acme/from-us/_queryapigate_probe.csv',
        elapsed_ms: 12,
      }),
      'POST /api/v1/destinations': () => jsonResponse({ ...aDestination, name: 'acme' }, 201),
    });
    renderAt('/destinations');
    await userEvent.click(await screen.findByRole('button', { name: 'New destination' }));
    const drawer = document.getElementById('drawer')!;
    await userEvent.type(within(drawer).getByLabelText('Name'), 'acme');
    expect(within(drawer).queryByLabelText('Access key ID')).toBeNull(); // only for object storage
    await userEvent.type(within(drawer).getByLabelText(/Writes under/), 's3://acme/from-us/');
    await userEvent.type(within(drawer).getByLabelText('Access key ID'), 'AKIA1');
    await userEvent.type(within(drawer).getByLabelText(/Secret access key/), 's3cret');
    await userEvent.type(within(drawer).getByLabelText('Region'), 'eu-west-1');
    await userEvent.click(within(drawer).getByRole('button', { name: 'Test' }));
    expect(
      await within(drawer).findByText(/✓ Wrote s3:\/\/acme\/from-us\/_queryapigate_probe.csv/),
    ).toBeInTheDocument();
    await userEvent.click(within(drawer).getByRole('button', { name: 'Create' }));
    await waitFor(() =>
      expect(calls.some((c) => c.method === 'POST' && c.path === '/api/v1/destinations')).toBe(true),
    );
    expect(calls.find((c) => c.method === 'POST' && c.path === '/api/v1/destinations')!.body).toEqual({
      name: 'acme',
      url: 's3://acme/from-us/',
      region: 'eu-west-1',
      user: 'AKIA1',
      password: 's3cret',
    });
  });

  it('an auditor sees destinations but cannot change them', async () => {
    fakeBackend({
      ...baseRoutes(),
      'GET /api/v1/me': me('auditor', ['exports.read', 'queries.read', 'self']),
      'GET /api/v1/destinations': () => ({ items: [aDestination] }),
    });
    renderAt('/destinations');
    const row = (await screen.findByText('s3://acme/from-us/')).closest('tr')!;
    expect(row).toHaveTextContent('AKIA1 · ********');
    expect(within(row).queryByRole('button', { name: 'Test' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'New destination' })).toBeNull();
  });
});

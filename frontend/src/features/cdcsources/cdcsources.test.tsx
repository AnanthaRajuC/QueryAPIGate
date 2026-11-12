import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { fakeBackend, jsonResponse } from '@/test/fakeBackend';
import { baseRoutes } from '@/test/fixtures';
import { renderAt } from '@/test/renderApp';

import { sinkProperties } from './shared';

afterEach(() => vi.unstubAllGlobals());

const me = (role: string, capabilities: string[]) => () => ({
  name: `${role}-1`,
  role,
  via: 'token',
  capabilities,
  data_access: role !== 'auditor',
});
const ADMIN_CAPS = ['connections.read', 'connections.write', 'settings.read', 'queries.read', 'self'];

const aSource = {
  name: 'rental',
  connection: 'lite',
  server_name: 'rental',
  ingest_path: '/ingest/cdc/rental',
  managed_by: null,
  created_at: '2026-11-12 09:00:00',
  updated_at: '2026-11-12 09:00:00',
  last_received_at: '2026-11-12 09:05:00',
  previous_expires_at: null,
};
const fromSetting = {
  ...aSource,
  name: 'legacy',
  server_name: null,
  ingest_path: '/ingest/cdc/legacy',
  managed_by: 'QUERYAPIGATE_CDC_SOURCES',
  created_at: null,
  updated_at: null,
  last_received_at: null,
};
const SECRET = 'whsec_MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=';
const settings = (live: 'on' | 'off') => () => ({
  items: [
    {
      id: 'live',
      title: 'Live',
      description: '',
      rows: [
        {
          label: 'Live queries',
          description: '',
          env: 'QUERYAPIGATE_LIVE_QUERIES',
          value: live,
          source: 'env',
          env_value: null,
          experimental: true,
        },
      ],
    },
  ],
});

describe('CDC sources', () => {
  it('lists sources with their status; one from the setting is read-only', async () => {
    fakeBackend({
      ...baseRoutes(),
      'GET /api/v1/me': me('admin', ADMIN_CAPS),
      'GET /api/v1/settings': settings('on'),
      'GET /api/v1/cdc-sources': () => ({ items: [fromSetting, aSource] }),
    });
    renderAt('/cdc-sources');
    const row = (await screen.findByText('/ingest/cdc/rental')).closest('tr')!;
    expect(row).toHaveTextContent('lite');
    expect(row).toHaveTextContent('last post');
    expect(within(row).getByRole('button', { name: 'Rotate' })).toBeVisible();
    const legacy = screen.getByText('/ingest/cdc/legacy').closest('tr')!;
    expect(legacy).toHaveTextContent('from QUERYAPIGATE_CDC_SOURCES');
    expect(legacy).toHaveTextContent('any');
    expect(within(legacy).queryByRole('button', { name: 'Rotate' })).toBeNull();
    expect(screen.queryByText(/Live queries are off/)).toBeNull();
  });

  it('creates a source and shows its secret once, with the sink lines', async () => {
    const { calls } = fakeBackend({
      ...baseRoutes(),
      'GET /api/v1/me': me('admin', ADMIN_CAPS),
      'GET /api/v1/settings': settings('off'),
      'GET /api/v1/cdc-sources': () => ({ items: [] }),
      'POST /api/v1/cdc-sources': () =>
        jsonResponse({ ...aSource, last_received_at: null, secret: SECRET }, 201),
    });
    renderAt('/cdc-sources');
    expect(await screen.findByText(/Live queries are off/)).toBeInTheDocument();
    await userEvent.click((await screen.findAllByRole('button', { name: 'New CDC source' }))[0]!);
    const drawer = document.getElementById('drawer')!;
    await userEvent.type(within(drawer).getByLabelText('Name'), 'rental');
    await waitFor(() => expect(within(drawer).getByRole('option', { name: 'lite' })).toBeInTheDocument());
    await userEvent.selectOptions(within(drawer).getByLabelText('Connection'), 'lite');
    await userEvent.type(within(drawer).getByLabelText('Server name'), 'rental');
    await userEvent.click(within(drawer).getByRole('button', { name: 'Create' }));
    expect(await within(drawer).findByDisplayValue(SECRET)).toBeInTheDocument();
    expect(calls.find((c) => c.method === 'POST' && c.path === '/api/v1/cdc-sources')!.body).toEqual({
      name: 'rental',
      connection: 'lite',
      server_name: 'rental',
    });
    const sink = drawer.querySelector('#c-sink')!;
    expect(sink).toHaveTextContent(`debezium.sink.http.url=${window.location.origin}/ingest/cdc/rental`);
    expect(sink).toHaveTextContent('webhook.secret=${WEBHOOK_SECRET}');
    expect(sink.textContent).not.toContain(SECRET);
  });

  it('rotates the secret, keeping the old one for the chosen time', async () => {
    const { calls } = fakeBackend({
      ...baseRoutes(),
      'GET /api/v1/me': me('admin', ADMIN_CAPS),
      'GET /api/v1/cdc-sources': () => ({ items: [aSource] }),
      'POST /api/v1/cdc-sources/rental/rotate': () => ({
        ...aSource,
        previous_expires_at: '2026-11-13 09:00:00',
        secret: SECRET,
      }),
    });
    renderAt('/cdc-sources');
    const row = (await screen.findByText('/ingest/cdc/rental')).closest('tr')!;
    await userEvent.click(within(row).getByRole('button', { name: 'Rotate' }));
    const drawer = document.getElementById('drawer')!;
    await userEvent.selectOptions(within(drawer).getByLabelText(/Keep the old secret working/), '1440');
    await userEvent.click(within(drawer).getByRole('button', { name: 'Rotate secret' }));
    expect(await within(drawer).findByDisplayValue(SECRET)).toBeInTheDocument();
    expect(within(drawer).getByText(/The old secret keeps working until/)).toBeInTheDocument();
    expect(calls.find((c) => c.path === '/api/v1/cdc-sources/rental/rotate')!.body).toEqual({
      grace_minutes: 1440,
    });
  });

  it('an auditor sees sources but cannot change them', async () => {
    fakeBackend({
      ...baseRoutes(),
      'GET /api/v1/me': me('auditor', ['connections.read', 'queries.read', 'self']),
      'GET /api/v1/cdc-sources': () => ({ items: [aSource] }),
    });
    renderAt('/cdc-sources');
    const row = (await screen.findByText('/ingest/cdc/rental')).closest('tr')!;
    expect(within(row).queryByRole('button', { name: 'Rotate' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'New CDC source' })).toBeNull();
  });

  it('sink lines name where to post and keep the secret out of the file', () => {
    expect(sinkProperties('/w/acme/ingest/cdc/shop', 'https://qag.example')).toBe(
      [
        'debezium.sink.type=http',
        'debezium.sink.http.url=https://qag.example/w/acme/ingest/cdc/shop',
        'debezium.sink.http.batch.enabled=true',
        'debezium.sink.http.authentication.type=standard-webhooks',
        'debezium.sink.http.authentication.webhook.secret=${WEBHOOK_SECRET}',
        'debezium.format.value=json',
      ].join('\n'),
    );
  });
});

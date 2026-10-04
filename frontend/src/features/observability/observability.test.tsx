import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { parseMetricsText } from '@/lib/metrics';
import { fakeBackend } from '@/test/fakeBackend';
import { baseRoutes } from '@/test/fixtures';
import { renderAt } from '@/test/renderApp';

vi.mock('@/components/SqlEditor', () => ({ SqlEditor: () => <textarea aria-label="SQL" /> }));

afterEach(() => vi.unstubAllGlobals());

const METRICS = `# HELP queryapigate_requests_total Requests
# TYPE queryapigate_requests_total counter
queryapigate_requests_total{method="GET",status="200"} 18
queryapigate_requests_total{method="GET",status="404"} 2
queryapigate_active_queries 1
queryapigate_pool_active_connections 3
queryapigate_pool_idle_connections 4
queryapigate_rows_returned_total{connection="lite",dialect="sqlite"} 120
queryapigate_queries_total{connection="lite",dialect="sqlite",status="success",key="admin"} 9
queryapigate_queries_total{connection="lite",dialect="sqlite",status="error",key="admin"} 1
queryapigate_query_duration_seconds_sum{connection="lite",dialect="sqlite"} 0.5
queryapigate_query_duration_seconds_count{connection="lite",dialect="sqlite"} 10
`;

const metricsRoutes = () => ({
  ...baseRoutes(),
  'GET /metrics': () => new Response(METRICS, { headers: { 'Content-Type': 'text/plain' } }),
});

describe('Metrics', () => {
  it('parses Prometheus text, labels and escapes included', () => {
    const series = parseMetricsText('# c\nm_total{a="x\\"y",b="z"} 2.5\nbad line\ng 7\n');
    expect(series).toEqual([
      { name: 'm_total', labels: { a: 'x"y', b: 'z' }, value: 2.5 },
      { name: 'g', labels: {}, value: 7 },
    ]);
  });

  it('shows the tiles, the charts and the per-connection table', async () => {
    fakeBackend(metricsRoutes());
    renderAt('/metrics');
    await waitFor(() => expect(document.querySelector('.stat-tiles')).not.toBeNull());
    const tiles = document.querySelector('.stat-tiles')!;
    expect(tiles).toHaveTextContent('Requests20');
    expect(tiles).toHaveTextContent('Error rate10.0%');
    expect(within(tiles as HTMLElement).getByText('10.0%')).toHaveClass('warn');
    expect(document.getElementById('metrics-pool-active')).toHaveTextContent('3');
    expect(screen.getByText('Requests by status')).toBeInTheDocument();
    const row = screen.getByRole('row', { name: /^lite/ });
    expect(row).toHaveTextContent('lite10150 ms120');
    expect(document.getElementById('metrics-updated')).toHaveTextContent('Updated just now');
  });

  it('offers Retry when /metrics fails', async () => {
    fakeBackend({ ...baseRoutes(), 'GET /metrics': () => new Response('nope', { status: 500 }) });
    renderAt('/metrics');
    expect(await screen.findByText('Couldn’t load metrics')).toBeInTheDocument();
  });
});

const entries = [
  {
    timestamp: '2026-10-04 10:00:02',
    actor: 'admin',
    action: 'update_key',
    target: 'partner',
    changes: { rate_limit: { from: null, to: '60/minute' } },
  },
  {
    timestamp: '2026-10-04 10:00:01',
    actor: 'admin',
    action: 'delete_role',
    target: 'analyst',
    changes: { connections: ['lite'], allowed_ips: null, queries: [{ name: 'films', allow_writes: true }] },
  },
  {
    timestamp: '2026-10-04 10:00:00',
    actor: 'ops',
    action: 'create_connection',
    target: 'shop',
    changes: null,
  },
];

const auditRoutes = () => ({
  ...baseRoutes(),
  'GET /api/v1/audit': () => ({
    items: entries,
    total: 3,
    actions: ['create_connection', 'delete_role', 'update_key'],
    retention: 500,
  }),
});

describe('Audit log', () => {
  it('lists entries newest first with their changes, toned by action', async () => {
    fakeBackend(auditRoutes());
    renderAt('/audit-log');
    const table = () => document.getElementById('auditlog-table')!;
    await waitFor(() => expect(table().querySelectorAll('tbody tr')).toHaveLength(3));
    expect(document.getElementById('auditlog-sub')).toHaveTextContent('Keeps the last 500 entries.');
    expect(document.getElementById('auditlog-count')).toHaveTextContent('3 entries');
    expect(table()).toHaveTextContent('rate_limit: none → 60/minute');
    expect(table()).toHaveTextContent('connections: lite');
    expect(table()).toHaveTextContent('queries: {"name":"films","allow_writes":true}');
    expect(table()).not.toHaveTextContent('allowed_ips'); // an empty field of a snapshot is left out
    const tags = [...table().querySelectorAll('.tag.act')].map((t) => t.className);
    expect(tags).toEqual(['tag act ', 'tag act bad', 'tag act ok']);
  });

  it('filters by action and by actor / target / time', async () => {
    fakeBackend(auditRoutes());
    renderAt('/audit-log');
    const table = () => document.getElementById('auditlog-table')!;
    await waitFor(() => expect(table().querySelectorAll('tbody tr')).toHaveLength(3));
    await userEvent.selectOptions(screen.getByLabelText('Action'), 'delete_role');
    expect(table().querySelectorAll('tbody tr')).toHaveLength(1);
    expect(document.getElementById('auditlog-count')).toHaveTextContent('1 of 3 entries');
    await userEvent.selectOptions(screen.getByLabelText('Action'), '');
    await userEvent.type(screen.getByPlaceholderText('Filter by actor, target, time…'), 'ops');
    expect(table().querySelectorAll('tbody tr')).toHaveLength(1);
    await userEvent.type(screen.getByPlaceholderText('Filter by actor, target, time…'), 'zzz');
    expect(table()).toHaveTextContent('No entries match this filter.');
  });

  it('exports what is shown, as JSON', async () => {
    const createObjectURL = vi.fn(() => 'blob:x');
    vi.stubGlobal('URL', Object.assign(URL, { createObjectURL, revokeObjectURL: vi.fn() }));
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
    fakeBackend(auditRoutes());
    renderAt('/audit-log');
    await waitFor(() =>
      expect(document.getElementById('auditlog-table')!.querySelectorAll('tbody tr')).toHaveLength(3),
    );
    await userEvent.click(screen.getByRole('button', { name: 'Export' }));
    expect(click).toHaveBeenCalled();
    expect(await screen.findByText('Exported 3 entries')).toBeInTheDocument();
    click.mockRestore();
  });

  it('shows the classic empty state', async () => {
    fakeBackend({
      ...baseRoutes(),
      'GET /api/v1/audit': () => ({ items: [], total: 0, actions: [], retention: 500 }),
    });
    renderAt('/audit-log');
    expect(await screen.findByText('No administrative changes recorded yet')).toBeInTheDocument();
  });
});

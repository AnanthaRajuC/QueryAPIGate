import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { fakeBackend } from '@/test/fakeBackend';
import { baseRoutes } from '@/test/fixtures';
import { renderAt } from '@/test/renderApp';

vi.mock('@/components/SqlEditor', () => ({ SqlEditor: () => <textarea aria-label="SQL" /> }));

afterEach(() => vi.unstubAllGlobals());

const request = (extra = {}) => ({
  id: 'wr_7f3a9c',
  query: 'close_ticket',
  version: 1,
  caller: 'support-bot',
  status: 'pending',
  created_at: '2026-10-11 14:15:00',
  expires_at: '2026-10-11 14:30:00',
  mode: 'approval',
  connection: 'helpdesk',
  sql: 'UPDATE tickets SET status = :status WHERE id = :id',
  arguments: { status: 'done', id: 42 },
  max_affected_rows: 1,
  decided_by: null,
  decided_via: null,
  decided_at: null,
  reason: null,
  result: null,
  ...extra,
});

const element = (selector: string) =>
  waitFor(() => {
    const found = document.querySelector<HTMLElement>(selector);
    if (!found) throw new Error(`no ${selector} yet`);
    return found;
  });

describe('Approvals', () => {
  it('lists the writes waiting, and approves one after showing exactly what it will run', async () => {
    const { calls } = fakeBackend({
      ...baseRoutes(),
      'GET /api/v1/write-requests': () => ({ items: [request()] }),
      'GET /api/v1/write-requests/wr_7f3a9c': () => request(),
      'POST /api/v1/write-requests/wr_7f3a9c/approve': () =>
        request({ status: 'approved', decided_by: 'alice', decided_via: 'sso' }),
    });
    renderAt('/approvals');
    const row = await element('tr[data-request="wr_7f3a9c"]');
    expect(row).toHaveTextContent('support-bot');
    expect(row).toHaveTextContent('{"status":"done","id":42}');
    await userEvent.click(within(row).getByRole('button', { name: 'close_ticket' }));
    const drawer = await element('#drawer');
    await waitFor(() =>
      expect(drawer).toHaveTextContent('UPDATE tickets SET status = :status WHERE id = :id'),
    );
    expect(within(drawer).getByText('42')).toBeInTheDocument();
    expect(drawer).toHaveTextContent('It changes at most 1 row');
    await userEvent.click(within(drawer).getByRole('button', { name: 'Approve' }));
    expect(await screen.findByText('Approved - support-bot may run it now')).toBeInTheDocument();
    expect(calls.some((c) => c.method === 'POST' && c.path.endsWith('/approve'))).toBe(true);
  });

  it('the link an agent hands out opens its request, and a rejection carries its reason', async () => {
    const { calls } = fakeBackend({
      ...baseRoutes(),
      'GET /api/v1/write-requests': () => ({ items: [] }),
      'GET /api/v1/write-requests/wr_7f3a9c': () => request(),
      'POST /api/v1/write-requests/wr_7f3a9c/reject': () => request({ status: 'rejected' }),
    });
    renderAt('/approvals/wr_7f3a9c');
    const drawer = await element('#drawer');
    await userEvent.type(await within(drawer).findByLabelText(/Reason/), 'Ticket 42 is still open');
    await userEvent.click(within(drawer).getByRole('button', { name: 'Reject' }));
    await waitFor(() => expect(calls.some((c) => c.path.endsWith('/reject'))).toBe(true));
    expect(calls.find((c) => c.path.endsWith('/reject'))!.body).toEqual({
      reason: 'Ticket 42 is still open',
    });
  });

  it('counts the writes waiting in the sidebar, for a role that may decide them', async () => {
    fakeBackend({
      ...baseRoutes(),
      'GET /api/v1/write-requests': () => ({ items: [request(), request({ id: 'wr_2' })] }),
    });
    renderAt('/');
    const nav = await screen.findByRole('tablist', { name: 'Sections' });
    await waitFor(() =>
      expect(within(nav).getByRole('tab', { name: /Approvals/ })).toHaveTextContent('Approvals2'),
    );
  });
});

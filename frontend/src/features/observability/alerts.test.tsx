import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { fakeBackend } from '@/test/fakeBackend';
import { baseRoutes } from '@/test/fixtures';
import { renderAt } from '@/test/renderApp';

vi.mock('@/components/SqlEditor', () => ({ SqlEditor: () => <textarea aria-label="SQL" /> }));

afterEach(() => {
  vi.unstubAllGlobals();
  localStorage.clear();
});

const alert = (
  id: string,
  severity: string,
  title: string,
  target: Record<string, string> | null = null,
) => ({
  id,
  severity,
  kind: id.split(':')[0],
  title,
  detail: 'What to do about it.',
  since: '2026-10-04 09:00:00',
  target,
});

const ALERTS = [
  alert('connection_failing:pg', 'critical', "Connection 'pg' is failing", {
    type: 'connection',
    name: 'pg',
  }),
  alert('query_slow:films', 'warning', "Query 'films' is slow", { type: 'query', name: 'films' }),
  alert('key_expiring:partner', 'warning', "API key 'partner' expires in 3 days", {
    type: 'key',
    name: 'partner',
  }),
  alert('key_unused:old', 'info', "API key 'old' hasn't been used in 120 days", { type: 'key', name: 'old' }),
];

const routes = (items: unknown[] = ALERTS) => ({
  ...baseRoutes(),
  'GET /api/v1/alerts': () => ({ items, checked_at: '2026-10-04 12:00:00' }),
});

describe('Alerts', () => {
  it('groups them by severity, most severe first, with where to fix each', async () => {
    fakeBackend(routes());
    renderAt('/alerts');
    const critical = await screen.findByText("Connection 'pg' is failing");
    expect(critical.closest('#alerts-critical')).not.toBeNull();
    expect(within(document.getElementById('alerts-warning')!).getAllByText(/slow|expires/)).toHaveLength(2);
    expect(within(document.getElementById('alerts-info')!).getByText(/hasn't been used/)).toBeInTheDocument();
    const slow = document.querySelector('[data-alert="query_slow:films"]')!;
    await userEvent.click(within(slow as HTMLElement).getByRole('button', { name: 'Open query' }));
    expect(await screen.findByRole('heading', { name: 'API Repository' })).toBeInTheDocument();
  });

  it('has a tab per check, each with its count, its alerts and what it watches', async () => {
    fakeBackend(routes());
    renderAt('/alerts');
    const tabs = await screen.findByRole('tablist', { name: 'Checks' });
    await waitFor(() =>
      expect([...tabs.querySelectorAll('[role=tab]')].map((t) => t.textContent)).toEqual([
        'All4',
        'Key expiry1',
        'Unused keys1',
        'Failing connections1',
        'Query errors0',
        'Slow queries1',
        'Rate limits0',
        'Open access0',
        'Run history0',
      ]),
    );
    expect(
      within(tabs)
        .getByRole('tab', { name: /Failing connections/ })
        .querySelector('.n'),
    ).toHaveClass('critical');
    await userEvent.click(within(tabs).getByRole('tab', { name: /Slow queries/ }));
    expect(within(tabs).getByRole('tab', { name: /Slow queries/ })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByText("Query 'films' is slow")).toBeInTheDocument();
    expect(screen.queryByText("Connection 'pg' is failing")).toBeNull();
    expect(screen.getByText(/typical \(median\) run/)).toBeInTheDocument();
    await userEvent.click(within(tabs).getByRole('tab', { name: /Rate limits/ }));
    expect(screen.getByText('Nothing to report from this check right now.')).toBeInTheDocument();
    expect(screen.getByText(/refused by their own rate_limit/)).toBeInTheDocument();
  });

  it("opens a check's tab from its address, as Home links to it", async () => {
    fakeBackend(routes());
    renderAt('/');
    await waitFor(() => expect(document.getElementById('home-health')).not.toBeNull());
    await userEvent.click(
      await within(document.getElementById('home-health')!).findByText("API key 'partner' expires in 3 days"),
    );
    const checks = await screen.findByRole('tablist', { name: 'Checks' });
    const selected = within(checks).getByRole('tab', { selected: true });
    expect(selected).toHaveTextContent('Key expiry');
    expect(screen.queryByText("Query 'films' is slow")).toBeNull();
  });

  it('counts critical and warning alerts on the bell and in the sidebar, red while any is critical', async () => {
    fakeBackend(routes());
    renderAt('/');
    const bell = await screen.findByRole('button', { name: 'Alerts: 3 need attention' });
    expect(bell.querySelector('.badge')).toHaveTextContent('3');
    expect(bell.querySelector('.badge')).toHaveClass('critical');
    await waitFor(() => expect(screen.getByRole('tab', { name: /Alerts/ })).toHaveTextContent('Alerts3'));
    await userEvent.click(bell);
    expect(await screen.findByRole('heading', { name: /^Alerts/ })).toBeInTheDocument();
  });

  it('dismisses one in this browser, and can bring it back', async () => {
    fakeBackend(routes());
    renderAt('/alerts');
    const row = () => document.querySelector('[data-alert="connection_failing:pg"]') as HTMLElement | null;
    await waitFor(() => expect(row()).not.toBeNull());
    await userEvent.click(within(row()!).getByRole('button', { name: 'Dismiss' }));
    expect(document.getElementById('alerts-critical')).toBeNull();
    expect(screen.getByRole('button', { name: 'Alerts: 2 need attention' })).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: /1 dismissed in this browser/ }));
    await userEvent.click(within(row()!).getByRole('button', { name: 'Restore' }));
    expect(row()!.closest('#alerts-critical')).not.toBeNull();
  });

  it('forgets a dismissal once its alert clears, so the problem coming back is shown again', async () => {
    localStorage.setItem(
      'queryapigate-ui-dismissed-alerts',
      JSON.stringify({ 'connection_failing:pg': Date.now(), 'query_slow:gone': Date.now() }),
    );
    fakeBackend(routes());
    renderAt('/alerts');
    await screen.findByText("Query 'films' is slow");
    await waitFor(() =>
      expect(JSON.parse(localStorage.getItem('queryapigate-ui-dismissed-alerts')!)).toEqual({
        'connection_failing:pg': expect.any(Number),
      }),
    );
  });

  it('says all clear when there is nothing', async () => {
    fakeBackend(routes([]));
    renderAt('/alerts');
    expect(await screen.findByText('All clear')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Alerts: all clear' })).toBeInTheDocument();
  });

  it('has no bell for a key that cannot read alerts', async () => {
    fakeBackend({
      ...baseRoutes(),
      'GET /api/v1/alerts': () =>
        new Response(JSON.stringify({ error: 'Forbidden', code: 'admin_only', request_id: 'r' }), {
          status: 403,
        }),
    });
    renderAt('/alerts');
    expect(await screen.findByText('Couldn’t check for alerts')).toBeInTheDocument();
    expect(document.getElementById('alerts-bell')).toBeNull();
  });
});

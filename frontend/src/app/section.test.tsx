import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { fakeBackend } from '@/test/fakeBackend';
import { baseRoutes } from '@/test/fixtures';
import { renderAt } from '@/test/renderApp';

vi.mock('@/components/SqlEditor', () => ({ SqlEditor: () => <textarea aria-label="SQL" /> }));

// The Access section: one sidebar item, whose page has the group's screens as tabs - each still at its own route.

const tabs = () => within(screen.getByRole('navigation', { name: 'Access' }));
const tabNames = () =>
  tabs()
    .getAllByRole('link')
    .map((a) => a.textContent);

describe('the Access section', () => {
  it('is one sidebar item that opens its first screen, with the others as tabs', async () => {
    fakeBackend({ ...baseRoutes(), 'GET /api/v1/workspaces': () => ({ items: [] }) });
    renderAt('/queries');
    const nav = screen.getByRole('tablist', { name: 'Sections' });
    await userEvent.click(within(nav).getByRole('tab', { name: 'Access' }));

    expect(await screen.findByRole('heading', { level: 1, name: 'Access' })).toBeInTheDocument();
    expect(document.getElementById('crumb-page')).toHaveTextContent('API keys');
    await waitFor(() =>
      expect(tabNames()).toEqual(['API keys1', 'Roles1', 'Access map', 'Workspaces0', 'Administrators']),
    );
    expect(tabs().getByRole('link', { name: /API keys/ })).toHaveAttribute('aria-current', 'page');
    // the screen's own title is the tab's: kept for a screen reader, a level down, not shown again
    expect(await screen.findByRole('heading', { level: 2, name: 'API keys' })).toHaveClass('visually-hidden');
    expect(within(nav).getByRole('tab', { name: 'Access' })).toHaveClass('active');

    await userEvent.click(tabs().getByRole('link', { name: /Roles/ }));
    expect(await screen.findByRole('heading', { level: 2, name: 'Roles' })).toBeInTheDocument();
    expect(tabs().getByRole('link', { name: /Roles/ })).toHaveAttribute('aria-current', 'page');
    expect(within(nav).getByRole('tab', { name: 'Access' })).toHaveClass('active'); // still the section's
    expect(document.getElementById('crumb-page')).toHaveTextContent('Roles');
  });

  it('keeps each screen at its own address, an API key’s details on the API keys tab', async () => {
    fakeBackend(baseRoutes());
    renderAt('/roles');
    expect(await screen.findByRole('heading', { level: 1, name: 'Access' })).toBeInTheDocument();
    expect(tabs().getByRole('link', { name: /Roles/ })).toHaveAttribute('aria-current', 'page');
  });

  it('shows a role only the tabs it may open, and starts on the first of them', async () => {
    fakeBackend({
      ...baseRoutes(),
      'GET /api/v1/me': () => ({
        name: 'dev',
        role: 'developer',
        via: 'token',
        capabilities: ['connections.read', 'observe', 'queries.read', 'queries.write', 'self'],
        data_access: true,
      }),
      'GET /api/v1/workspaces': () => ({ items: [] }),
    });
    renderAt('/access');
    expect(await screen.findByRole('heading', { level: 2, name: 'Workspaces' })).toBeInTheDocument();
    await waitFor(() => expect(tabNames()).toEqual(['Workspaces0', 'Administrators']));
  });
});

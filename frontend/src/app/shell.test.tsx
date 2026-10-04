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
  document.body.className = '';
});

describe('Console shell - the classic frame', () => {
  it('has the classic sidebar: same groups, names and order, with counts', async () => {
    fakeBackend(baseRoutes());
    renderAt('/queries/films');
    const nav = screen.getByRole('tablist', { name: 'Sections' });
    const groups = [...nav.querySelectorAll('.nav-label')].map((n) => n.textContent);
    expect(groups).toEqual(['Overview', 'Data', 'API', 'Access', 'Observability']);
    const labels = [...nav.querySelectorAll('.nav-text')].map((n) => n.textContent);
    expect(labels).toEqual([
      'Home',
      'Connections',
      'Caching',
      'API Repository',
      'API Designer',
      'API keys',
      'Roles',
      'Access map',
      'Metrics',
      'Audit log',
    ]);
    await waitFor(() =>
      expect(within(nav).getByRole('tab', { name: /API Repository/ })).toHaveTextContent('API Repository2'),
    );
    expect(within(nav).getByRole('tab', { name: /API Repository/ })).toHaveClass('active');
    expect(await screen.findByText('v9.9.9')).toBeInTheDocument();
  });

  it('shows the breadcrumb for the current screen', () => {
    fakeBackend(baseRoutes());
    renderAt('/queries');
    expect(document.getElementById('crumb-group')).toHaveTextContent('API');
    expect(document.getElementById('crumb-page')).toHaveTextContent('API Repository');
  });

  it('opens screens it has not rebuilt in the classic UI, on the matching tab', async () => {
    fakeBackend(baseRoutes());
    renderAt('/queries');
    await userEvent.click(screen.getByRole('tab', { name: /Settings/ }));
    expect(sessionStorage.getItem('queryapigate-ui-tab')).toBe('settings');
  });

  it('applies an API key to this tab, in the same storage /ui and /docs use', async () => {
    const { calls } = fakeBackend(baseRoutes());
    renderAt('/queries');
    expect(screen.getByText('No API key')).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText('API key'), 'secret-key-1234567890');
    await userEvent.click(screen.getByRole('button', { name: 'Apply' }));
    expect(sessionStorage.getItem('queryapigate-key')).toBe('secret-key-1234567890');
    expect(screen.getByText('API key applied')).toBeInTheDocument();
    expect(document.getElementById('key-mask')).toHaveTextContent('••••••••7890');
    await waitFor(() =>
      expect(calls.some((c) => c.headers.get('X-API-Key') === 'secret-key-1234567890')).toBe(true),
    );
  });

  it('collapses the sidebar with its toggle, remembered like the classic UI', async () => {
    fakeBackend(baseRoutes());
    renderAt('/queries');
    await userEvent.click(screen.getByRole('button', { name: 'Collapse sidebar' }));
    expect(document.body).toHaveClass('side-collapsed');
    expect(localStorage.getItem('queryapigate-ui-side-collapsed')).toBe('1');
  });

  it('searches across queries, connections, keys and roles with Ctrl K', async () => {
    fakeBackend(baseRoutes());
    renderAt('/queries/films');
    await screen.findByText('v9.9.9');
    await userEvent.keyboard('{Control>}k{/Control}');
    const palette = await screen.findByRole('dialog', { name: 'Search' });
    await waitFor(() => expect(within(palette).getByText('rentals')).toBeInTheDocument());
    expect(within(palette).getByText('partner')).toBeInTheDocument();
    await userEvent.type(within(palette).getByRole('searchbox'), 'rent');
    expect(within(palette).queryByText('films')).not.toBeInTheDocument();
  });
});

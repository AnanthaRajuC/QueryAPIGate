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
    expect(groups).toEqual(['Overview', 'Data', 'API', 'Delivery', 'Access', 'Observability']);
    const labels = [...nav.querySelectorAll('.nav-text')].map((n) => n.textContent);
    expect(labels).toEqual([
      'Home',
      'Connections',
      'Caching',
      'API Repository',
      'API Designer',
      'Exports',
      'Destinations',
      'API keys',
      'Roles',
      'Access map',
      'Administrators',
      'Alerts',
      'Metrics',
      'Audit log',
    ]);
    await waitFor(() =>
      expect(within(nav).getByRole('tab', { name: /API Repository/ })).toHaveTextContent('API Repository2'),
    );
    expect(within(nav).getByRole('tab', { name: /API Repository/ })).toHaveClass('active');
    expect(await screen.findByText('v9.9.9')).toBeInTheDocument();
  });

  it("shows who is signed in, and hides the screens their role can't use", async () => {
    const developer = ['connections.read', 'observe', 'queries.read', 'queries.write', 'self'];
    fakeBackend({
      ...baseRoutes(),
      'GET /api/v1/me': () => ({
        name: 'alice',
        role: 'developer',
        via: 'token',
        capabilities: developer,
        data_access: true,
      }),
    });
    renderAt('/queries');
    expect(await screen.findByText('Signed in as alice · Developer')).toBeInTheDocument();
    const nav = screen.getByRole('tablist', { name: 'Sections' });
    const labels = [...nav.querySelectorAll('.nav-text')].map((n) => n.textContent);
    expect(labels).toEqual([
      'Home',
      'Connections',
      'API Repository',
      'API Designer',
      'Administrators',
      'Alerts',
      'Metrics',
      'Audit log',
    ]);
    expect(screen.queryByRole('tab', { name: /Settings/ })).toBeNull();
  });

  it('switches between light and dark from the top right corner, as the Theme preference', async () => {
    fakeBackend(baseRoutes());
    renderAt('/queries');
    const toLight = () => screen.getByRole('button', { name: 'Switch to light mode' });
    await userEvent.click(screen.getByRole('button', { name: 'Switch to dark mode' }));
    expect(document.documentElement.style.colorScheme).toBe('dark');
    expect(JSON.parse(localStorage.getItem('queryapigate-ui-prefs') ?? '{}').theme).toBe('Dark');
    await userEvent.click(toLight());
    expect(document.documentElement.style.colorScheme).toBe('light');
    expect(screen.getByRole('button', { name: 'Switch to dark mode' })).toBeInTheDocument();
  });

  it('sets the font size from the top right corner, as the Font size preference', async () => {
    fakeBackend(baseRoutes());
    renderAt('/queries');
    const group = screen.getByRole('group', { name: 'Font size' });
    expect(within(group).getByRole('button', { name: 'Medium text' })).toHaveAttribute(
      'aria-pressed',
      'true',
    );
    await userEvent.click(within(group).getByRole('button', { name: 'Large text' }));
    expect(document.documentElement.style.zoom).toBe('1.15');
    expect(within(group).getByRole('button', { name: 'Large text' })).toHaveAttribute('aria-pressed', 'true');
    expect(JSON.parse(localStorage.getItem('queryapigate-ui-prefs') ?? '{}').fontSize).toBe('Large');
  });

  it('starts from the operating system when the theme follows it', () => {
    vi.stubGlobal(
      'matchMedia',
      vi.fn(() => ({ matches: true, addEventListener: vi.fn(), removeEventListener: vi.fn() })),
    );
    fakeBackend(baseRoutes());
    renderAt('/queries');
    expect(screen.getByRole('button', { name: 'Switch to light mode' })).toBeInTheDocument();
  });

  it("opens API docs and OpenAPI in a new tab that keeps this tab's API key", () => {
    fakeBackend(baseRoutes());
    renderAt('/queries');
    for (const name of ['API docs', 'OpenAPI']) {
      const link = screen.getByRole('link', { name: new RegExp(name) });
      expect(link).toHaveAttribute('target', '_blank');
      expect(link).toHaveAttribute('rel', 'opener'); // noopener would start /docs with empty sessionStorage
    }
  });

  it('collapses the sidebar on Help, and leaves the saved choice alone', async () => {
    fakeBackend(baseRoutes());
    renderAt('/queries');
    expect(document.body).not.toHaveClass('side-collapsed');
    await userEvent.click(screen.getByRole('tab', { name: /Help/ }));
    await waitFor(() => expect(document.body).toHaveClass('side-collapsed'));
    await userEvent.click(screen.getByRole('button', { name: 'Expand sidebar' })); // still yours to open on Help
    expect(document.body).not.toHaveClass('side-collapsed');
    await userEvent.click(screen.getByRole('tab', { name: /API keys/ }));
    await userEvent.click(screen.getByRole('tab', { name: /Help/ }));
    await waitFor(() => expect(document.body).toHaveClass('side-collapsed')); // collapsed again on the next visit
    await userEvent.click(screen.getByRole('tab', { name: /API keys/ }));
    await waitFor(() => expect(document.body).not.toHaveClass('side-collapsed'));
    expect(localStorage.getItem('queryapigate-ui-side-collapsed')).toBe('0');
  });

  it('shows the breadcrumb for the current screen', () => {
    fakeBackend(baseRoutes());
    renderAt('/queries');
    expect(document.getElementById('crumb-group')).toHaveTextContent('API');
    expect(document.getElementById('crumb-page')).toHaveTextContent('API Repository');
  });

  it('every sidebar item is a Console screen', async () => {
    fakeBackend(baseRoutes());
    renderAt('/queries');
    await userEvent.click(screen.getByRole('tab', { name: /Help/ }));
    expect(await screen.findByRole('heading', { name: 'Help' })).toBeInTheDocument();
  });

  it('applies an API key to this tab, in the same storage /docs uses', async () => {
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

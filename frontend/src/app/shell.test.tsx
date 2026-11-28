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
    // one entry per group: a group of several screens is a section, whose page has them as tabs; a group of one is it
    expect(nav.querySelectorAll('.nav-label')).toHaveLength(0);
    const labels = [...nav.querySelectorAll('.nav-text')].map((n) => n.textContent);
    expect(labels).toEqual(['Home', 'Data', 'API', 'MCP server', 'Delivery', 'Access', 'Observability']);
    expect(within(nav).getByRole('tab', { name: 'API' })).toHaveClass('active');
    // the screen's count is on its tab
    const tabs = within(screen.getByRole('navigation', { name: 'API' }));
    await waitFor(() =>
      expect(tabs.getByRole('link', { name: /API Repository/ })).toHaveTextContent('API Repository2'),
    );
    expect(tabs.getByRole('link', { name: /API Repository/ })).toHaveAttribute('aria-current', 'page');
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
    expect(
      await screen.findByRole('button', { name: 'Account: alice, Developer · this tab' }),
    ).toBeInTheDocument();
    const nav = screen.getByRole('tablist', { name: 'Sections' });
    const labels = [...nav.querySelectorAll('.nav-text')].map((n) => n.textContent);
    // no Delivery: a developer may open neither of its screens (each section holds only the tabs a role may open)
    expect(labels).toEqual(['Home', 'Data', 'API', 'MCP server', 'Access', 'Observability']);
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

  it("has Help, API docs and OpenAPI in one menu - the docs in a new tab that keeps this tab's API key", async () => {
    fakeBackend(baseRoutes());
    renderAt('/queries');
    const foot = document.querySelector<HTMLElement>('.side-foot')!;
    expect(within(foot).queryByRole('link')).toBeNull(); // nothing of it until it's opened
    const button = within(foot).getByRole('button', { name: 'Help and docs' });
    expect(button).toHaveAttribute('aria-haspopup', 'menu');
    await userEvent.click(button);
    const menu = screen.getByRole('menu', { name: 'Help and docs' });
    const items = within(menu).getAllByRole('menuitem');
    expect(items.map((i) => i.textContent)).toEqual(['Help', 'API docs/docs', 'OpenAPI.json']);
    for (const [item, href] of [
      [items[1]!, '/docs'],
      [items[2]!, '/openapi.json'],
    ] as const) {
      expect(item).toHaveAttribute('href', href);
      expect(item).toHaveAttribute('target', '_blank');
      expect(item).toHaveAttribute('rel', 'opener'); // noopener would start /docs with empty sessionStorage
    }
  });

  it('collapses the sidebar on Help, and leaves the saved choice alone', async () => {
    fakeBackend(baseRoutes());
    renderAt('/queries');
    expect(document.body).not.toHaveClass('side-collapsed');
    // Await each page settling before leaving it: some tabs (API keys) load data-heavy pages whose async
    // queries, if still resolving as we navigate on, re-render mid-transition and can race the collapse toggle.
    const open = async (tab: RegExp, heading: string) => {
      if (heading === 'Help') {
        await userEvent.click(screen.getByRole('button', { name: 'Help and docs' }));
        await userEvent.click(screen.getByRole('menuitem', { name: 'Help' }));
      } else await userEvent.click(screen.getByRole('tab', { name: tab }));
      await screen.findByRole('heading', { name: heading });
    };
    await open(/Help/, 'Help');
    await waitFor(() => expect(document.body).toHaveClass('side-collapsed'));
    await userEvent.click(screen.getByRole('button', { name: 'Expand sidebar' })); // still yours to open on Help
    expect(document.body).not.toHaveClass('side-collapsed');
    await open(/Access/, 'Access');
    await open(/Help/, 'Help');
    await waitFor(() => expect(document.body).toHaveClass('side-collapsed')); // collapsed again on the next visit
    await open(/Access/, 'Access');
    await waitFor(() => expect(document.body).not.toHaveClass('side-collapsed'));
    expect(localStorage.getItem('queryapigate-ui-side-collapsed')).toBe('0');
  });

  it('shows the breadcrumb for the current screen', () => {
    fakeBackend(baseRoutes());
    renderAt('/queries');
    expect(document.getElementById('crumb-group')).toHaveTextContent('API');
    expect(document.getElementById('crumb-page')).toHaveTextContent('API Repository');
  });

  it('Help is a Console screen, and its entry is active there', async () => {
    fakeBackend(baseRoutes());
    renderAt('/queries');
    const button = screen.getByRole('button', { name: 'Help and docs' });
    expect(button).not.toHaveClass('active');
    await userEvent.click(button);
    await userEvent.click(screen.getByRole('menuitem', { name: 'Help' }));
    expect(await screen.findByRole('heading', { name: 'Help' })).toBeInTheDocument();
    expect(screen.queryByRole('menu')).toBeNull();
    expect(button).toHaveClass('active');
  });

  it('applies an API key to this tab, in the same storage /docs uses', async () => {
    const { calls } = fakeBackend(baseRoutes());
    renderAt('/queries');
    // the account, at the foot: who this tab is, as a button opening a menu over it
    const account = screen.getByRole('button', { name: 'Account: No API key, this tab' });
    expect(account).toHaveAttribute('aria-haspopup', 'menu');
    await userEvent.click(account);
    await userEvent.click(screen.getByRole('menuitem', { name: 'Use an API key' }));
    const form = screen.getByRole('dialog', { name: 'API key for this tab' });
    const input = within(form).getByLabelText('Admin token or API key');
    expect(input).toHaveFocus();
    await userEvent.type(input, 'secret-key-1234567890');
    await userEvent.click(within(form).getByRole('button', { name: 'Apply' }));
    expect(sessionStorage.getItem('queryapigate-key')).toBe('secret-key-1234567890');
    expect(screen.queryByRole('dialog', { name: 'API key for this tab' })).toBeNull();
    const applied = screen.getByRole('button', { name: 'Account: API key applied, this tab' });
    expect(applied).toHaveFocus();
    await userEvent.click(applied);
    expect(document.getElementById('key-mask')).toHaveTextContent('••••7890');
    expect(screen.getAllByRole('menuitem').map((i) => i.textContent)).toEqual(['Change key', 'Sign out']);
    await waitFor(() =>
      expect(calls.some((c) => c.headers.get('X-API-Key') === 'secret-key-1234567890')).toBe(true),
    );
  });

  it('closes the key form on Cancel and on Escape, changing nothing', async () => {
    sessionStorage.setItem('queryapigate-key', 'kept-key-1234567890');
    fakeBackend(baseRoutes());
    renderAt('/queries');
    const account = screen.getByRole('button', { name: /^Account:/ });
    for (const close of ['Cancel', 'Escape']) {
      await userEvent.click(account);
      await userEvent.click(screen.getByRole('menuitem', { name: 'Change key' }));
      await userEvent.type(screen.getByLabelText('Admin token or API key'), 'other');
      if (close === 'Cancel') await userEvent.click(screen.getByRole('button', { name: 'Cancel' }));
      else await userEvent.keyboard('{Escape}');
      expect(screen.queryByRole('dialog')).toBeNull();
      expect(account).toHaveFocus();
      expect(sessionStorage.getItem('queryapigate-key')).toBe('kept-key-1234567890');
    }
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

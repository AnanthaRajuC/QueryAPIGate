import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { fakeBackend } from '@/test/fakeBackend';
import { baseRoutes } from '@/test/fixtures';
import { renderAt } from '@/test/renderApp';

vi.mock('@/components/SqlEditor', () => ({ SqlEditor: () => <textarea aria-label="SQL" /> }));

afterEach(() => vi.unstubAllGlobals());

describe('Help', () => {
  it("renders a doc from GitHub at the server's version, sanitized", async () => {
    const { calls } = fakeBackend({
      ...baseRoutes(),
      'GET /AnanthaRajuC/QueryAPIGate/v9.9.9/README.md': () =>
        new Response('# QueryAPIGate\n\nHello <img src=x onerror="alert(1)"> world', { status: 200 }),
    });
    renderAt('/help');
    expect(await screen.findByRole('heading', { name: 'QueryAPIGate' })).toBeInTheDocument();
    expect(document.querySelector('#docs-content img')?.getAttribute('onerror')).toBeNull();
    expect(calls.some((c) => c.path === '/AnanthaRajuC/QueryAPIGate/v9.9.9/README.md')).toBe(true);
    expect(document.querySelector('#docs-nav button[data-doc="readme"]')).toHaveClass('active');
  });

  it('falls back to main for a version with no tag, and says when GitHub cannot be reached', async () => {
    const { calls } = fakeBackend({
      ...baseRoutes(),
      'GET /AnanthaRajuC/QueryAPIGate/main/documentation/API.md': () =>
        new Response('# API', { status: 200 }),
    });
    renderAt('/help');
    expect(
      await screen.findByText(/Could not load this doc from GitHub \(GitHub returned 404\)/),
    ).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'API Reference' }));
    expect(await screen.findByRole('heading', { name: 'API' })).toBeInTheDocument();
    expect(calls.map((c) => c.path)).toContain('/AnanthaRajuC/QueryAPIGate/v9.9.9/documentation/API.md');
  });

  it('keeps the how-to guides in a tab of their own, each tab remembering its page', async () => {
    fakeBackend({
      ...baseRoutes(),
      'GET /AnanthaRajuC/QueryAPIGate/v9.9.9/README.md': () =>
        new Response('# QueryAPIGate', { status: 200 }),
      'GET /AnanthaRajuC/QueryAPIGate/v9.9.9/how-to/how-to.md': () =>
        new Response('# How-to', { status: 200 }),
    });
    renderAt('/help');
    const nav = () => [...document.querySelectorAll('#docs-nav button')].map((b) => b.textContent);
    await userEvent.click(await screen.findByRole('button', { name: 'Overview' })); // the page is remembered across tests
    await screen.findByRole('heading', { name: 'QueryAPIGate' });
    expect(nav()).toContain('API Reference');
    expect(nav()).not.toContain('All how-to guides');
    await userEvent.click(screen.getByRole('button', { name: 'How-to guides' }));
    expect(await screen.findByRole('heading', { name: 'How-to' })).toBeInTheDocument();
    expect(nav()[0]).toBe('All how-to guides');
    expect(nav()).not.toContain('API Reference');
    await userEvent.click(screen.getByRole('button', { name: 'Docs' }));
    expect(await screen.findByRole('heading', { name: 'QueryAPIGate' })).toBeInTheDocument();
  });

  it('lists the sections of the open doc under On this page, and goes to one', async () => {
    fakeBackend({
      ...baseRoutes(),
      'GET /AnanthaRajuC/QueryAPIGate/v9.9.9/README.md': () =>
        new Response('# QueryAPIGate\n\n## Install\n\nSee [usage](#usage).\n\n## Usage\n\n### Flags\n', {
          status: 200,
        }),
    });
    renderAt('/help');
    await userEvent.click(await screen.findByRole('button', { name: 'Overview' }));
    const toc = await screen.findByRole('navigation', { name: 'On this page' });
    expect([...toc.querySelectorAll('a')].map((a) => [a.textContent, a.getAttribute('href')])).toEqual([
      ['Install', '#install'],
      ['Usage', '#usage'],
      ['Flags', '#flags'],
    ]);
    await userEvent.click(within(toc).getByRole('link', { name: 'Usage' }));
    expect(within(toc).getByRole('link', { name: 'Usage' })).toHaveAttribute('aria-current', 'location');
    await userEvent.click(screen.getByRole('link', { name: 'usage' })); // the doc's own #link
    expect(within(toc).getByRole('link', { name: 'Usage' })).toHaveClass('active');
  });

  it('has the quick reference', async () => {
    fakeBackend(baseRoutes());
    renderAt('/help');
    await userEvent.click(await screen.findByRole('button', { name: 'Quick reference' }));
    await waitFor(() => expect(document.getElementById('help-quickref')).not.toHaveAttribute('hidden'));
    expect(screen.getByRole('heading', { name: 'Keyboard shortcuts' })).toBeInTheDocument();
  });
});

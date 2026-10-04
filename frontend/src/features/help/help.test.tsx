import { screen, waitFor } from '@testing-library/react';
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

  it('has the quick reference', async () => {
    fakeBackend(baseRoutes());
    renderAt('/help');
    await userEvent.click(await screen.findByRole('button', { name: 'Quick reference' }));
    await waitFor(() => expect(document.getElementById('help-quickref')).not.toHaveAttribute('hidden'));
    expect(screen.getByRole('heading', { name: 'Keyboard shortcuts' })).toBeInTheDocument();
  });
});

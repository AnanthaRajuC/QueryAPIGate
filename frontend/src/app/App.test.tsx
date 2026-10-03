import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { App } from './App';

const ADMIN_CATALOG = {
  queries: [{ name: 'films' }, { name: 'rentals' }],
  caller: { name: null, admin: true, allow_writes: true },
};

/** A fake backend: /health always answers; /catalog needs the key 'good-key'. */
function stubBackend() {
  const fetchMock = vi.fn(async (request: Request) => {
    const path = new URL(request.url).pathname;
    const json = (body: unknown, status = 200) =>
      new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
    if (path === '/health') return json({ status: 'ok', version: '9.9.9' });
    if (path === '/catalog') {
      return request.headers.get('X-API-Key') === 'good-key'
        ? json(ADMIN_CATALOG)
        : json({ error: 'Unauthorized' }, 401);
    }
    return json({ error: 'Not found' }, 404);
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

function renderAt(path: string) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[path]}>
        <App />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('Console shell', () => {
  it('organises navigation by workflow, not by backend table', () => {
    stubBackend();
    renderAt('/');
    const nav = screen.getByRole('navigation', { name: 'Console' });
    for (const section of ['Build', 'Explore', 'Govern', 'Observe', 'AI', 'Admin']) {
      expect(within(nav).getByText(section)).toBeInTheDocument();
    }
    expect(within(nav).getByRole('link', { name: /Queries/ })).toBeInTheDocument();
  });

  it('shows the server version from /health', async () => {
    stubBackend();
    renderAt('/');
    expect(await screen.findByText('9.9.9')).toBeInTheDocument();
  });

  it('signs in with an API key, sends it as X-API-Key, and keeps it in the same storage /ui uses', async () => {
    const fetchMock = stubBackend();
    renderAt('/');
    await userEvent.type(screen.getByLabelText('API key'), 'good-key');
    await userEvent.click(screen.getByRole('button', { name: 'Sign in' }));

    expect(await screen.findByText(/Signed in as/)).toBeInTheDocument();
    expect(sessionStorage.getItem('queryapigate-key')).toBe('good-key');
    const sentKeys = fetchMock.mock.calls.map(([request]) => request.headers.get('X-API-Key'));
    expect(sentKeys).toContain('good-key');
    expect(await screen.findByText('Saved queries you can run: 2')).toBeInTheDocument();
  });

  it('reports a refused key without signing in', async () => {
    stubBackend();
    renderAt('/');
    await userEvent.type(screen.getByLabelText('API key'), 'wrong-key');
    await userEvent.click(screen.getByRole('button', { name: 'Sign in' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('That key was refused.');
    expect(screen.queryByText(/Signed in as/)).not.toBeInTheDocument();
  });

  it('sends screens that have not moved yet to the right tab of the classic UI', async () => {
    stubBackend();
    renderAt('/api-keys');
    expect(screen.getByRole('heading', { name: 'API Keys' })).toBeInTheDocument();
    const link = screen.getByRole('link', { name: 'Open in the classic UI' });
    expect(link).toHaveAttribute('href', '/ui');
    link.addEventListener('click', (event) => event.preventDefault()); // jsdom can't navigate
    await userEvent.click(link);
    expect(sessionStorage.getItem('queryapigate-ui-tab')).toBe('apikeys');
  });

  it('shows a not-found page for unknown routes', () => {
    stubBackend();
    renderAt('/no-such-screen');
    expect(screen.getByRole('heading', { name: 'Page not found' })).toBeInTheDocument();
  });
});

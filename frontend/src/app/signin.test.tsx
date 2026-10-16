import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { getApiKey, setApiKey } from '@/auth/apiKey';
import { ssoRefusal } from '@/auth/ssoRefusal';
import { fakeBackend, jsonResponse, type Call } from '@/test/fakeBackend';
import { baseRoutes } from '@/test/fixtures';
import { renderAt } from '@/test/renderApp';

vi.mock('@/components/SqlEditor', () => ({ SqlEditor: () => <textarea aria-label="SQL" /> }));

afterEach(() => {
  vi.unstubAllGlobals();
  setApiKey('');
});

const OWNER = {
  name: 'alice',
  role: 'owner',
  via: 'token',
  capabilities: ['queries.read', 'self'],
  data_access: true,
};
const UNAUTHORIZED = () => jsonResponse({ error: 'Unauthorized', code: 'unauthorized' }, 401);

/** /api/v1/me as a protected server answers it: 401 unless the request carries `accepted` as its key. */
const meFor = (accepted: string) => (call: Call) =>
  call.headers.get('X-API-Key') === accepted ? OWNER : UNAUTHORIZED();

const shell = () => document.getElementById('side');

describe('the sign-in page', () => {
  it('stands in for the whole Console until a token is accepted', async () => {
    fakeBackend({ ...baseRoutes(), 'GET /api/v1/me': meFor('tok_alice') });
    renderAt('/queries');
    expect(await screen.findByRole('heading', { name: 'Sign in to the Console' })).toBeInTheDocument();
    expect(shell()).toBeNull(); // no sidebar, no pages
    expect(screen.queryByRole('link', { name: 'Sign in with SSO' })).toBeNull(); // the server has no SSO

    await userEvent.type(screen.getByLabelText('Admin token or API key'), 'tok_alice');
    await userEvent.click(screen.getByRole('button', { name: 'Sign in' }));
    await waitFor(() => expect(shell()).not.toBeNull());
    expect(screen.queryByRole('heading', { name: 'Sign in to the Console' })).toBeNull();
    expect(getApiKey()).toBe('tok_alice'); // this tab's key, as the sidebar's panel keeps it
    await waitFor(() => expect(document.getElementById('key-state')).toHaveTextContent('Signed in as alice'));
  });

  it('says so when a token is refused, and stays', async () => {
    fakeBackend({ ...baseRoutes(), 'GET /api/v1/me': meFor('tok_alice') });
    renderAt('/');
    await userEvent.type(await screen.findByLabelText('Admin token or API key'), 'tok_wrong');
    await userEvent.click(screen.getByRole('button', { name: 'Sign in' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('wasn’t accepted');
    expect(shell()).toBeNull();
  });

  it('stays up while a tried token is being checked - the shell never shows in between', async () => {
    const { calls } = fakeBackend({
      ...baseRoutes(),
      'GET /api/v1/me': (call) =>
        call.headers.get('X-API-Key')
          ? new Promise((resolve) => setTimeout(() => resolve(UNAUTHORIZED()), 150)) // a slow refusal
          : UNAUTHORIZED(),
    });
    renderAt('/');
    await userEvent.type(await screen.findByLabelText('Admin token or API key'), 'tok_wrong');
    const before = calls.length;
    await userEvent.click(screen.getByRole('button', { name: 'Sign in' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('wasn’t accepted');
    expect(shell()).toBeNull();
    // nothing but the token's own check was asked while it was out: no screen of the shell came and went
    expect(calls.slice(before).map((c) => c.path)).toEqual(['/api/v1/me']);
  });

  it('offers SSO first when the server has it, coming back to this page', async () => {
    fakeBackend({
      ...baseRoutes(),
      'GET /auth/session': () => ({ sso: true, signed_in: false, sso_only: false }),
      'GET /api/v1/me': UNAUTHORIZED,
    });
    renderAt('/alerts');
    const link = await screen.findByRole('link', { name: 'Sign in with SSO' });
    expect(link.getAttribute('href')).toBe('/auth/sso/login?next=' + encodeURIComponent('/console/'));
    expect(screen.getByLabelText('Admin token or API key')).toBeInTheDocument(); // a token still works
  });

  it('says why the identity provider sign-in was refused', async () => {
    fakeBackend({
      ...baseRoutes(),
      'GET /auth/session': () => ({ sso: true, signed_in: false, sso_only: false }),
      'GET /api/v1/me': UNAUTHORIZED,
    });
    renderAt('/?sso_error=not_allowed');
    await screen.findByRole('heading', { name: 'Sign in to the Console' });
    expect(screen.getByRole('alert')).toHaveTextContent(ssoRefusal('not_allowed'));
    expect(shell()).toBeNull();
  });

  it('isn’t shown on a server open to all, or for a key that isn’t an administrator’s', async () => {
    for (const me of [
      () => ({ ...OWNER, name: 'open', via: 'open' }),
      () => jsonResponse({ error: 'Forbidden', code: 'forbidden' }, 403),
    ]) {
      fakeBackend({ ...baseRoutes(), 'GET /api/v1/me': me });
      const { unmount } = renderAt('/');
      await waitFor(() => expect(shell()).not.toBeNull());
      expect(screen.queryByRole('heading', { name: 'Sign in to the Console' })).toBeNull();
      unmount();
    }
  });

  it('comes back when a key is signed out from the sidebar', async () => {
    setApiKey('tok_alice');
    fakeBackend({ ...baseRoutes(), 'GET /api/v1/me': meFor('tok_alice') });
    renderAt('/');
    await userEvent.click(await screen.findByRole('button', { name: 'Sign out' }));
    expect(await screen.findByRole('heading', { name: 'Sign in to the Console' })).toBeInTheDocument();
    expect(getApiKey()).toBe('');
  });
});

import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { setApiKey } from '@/auth/apiKey';
import { fakeBackend } from '@/test/fakeBackend';
import { baseRoutes } from '@/test/fixtures';
import { renderAt } from '@/test/renderApp';

vi.mock('@/components/SqlEditor', () => ({ SqlEditor: () => <textarea aria-label="SQL" /> }));

afterEach(() => vi.unstubAllGlobals());

const OWNER_CAPS = ['admins.read', 'admins.write', 'access.read', 'observe', 'queries.read', 'self'];
const SIGNED_IN = {
  sso: true,
  signed_in: true,
  sso_only: false,
  admin: 'alice',
  role: 'owner',
  email: 'alice@example.com',
  csrf: 'the-csrf-token',
  expires_at: '2026-10-11 20:00:00',
};
const me = (via: string) => () => ({
  name: 'alice',
  role: 'owner',
  via,
  capabilities: OWNER_CAPS,
  data_access: true,
});
const admin = (name: string, extra = {}) => ({
  name,
  role: 'admin',
  email: `${name}@example.com`,
  active: true,
  created_at: '2026-10-05 09:00:00',
  created_by: 'sso',
  last_seen_at: null,
  tokens: 0,
  sso: null,
  ...extra,
});

/** The element, once it's there. */
const element = (selector: string) =>
  waitFor(() => {
    const found = document.querySelector<HTMLElement>(selector);
    if (!found) throw new Error(`no ${selector} yet`);
    return found;
  });

describe('Console sign-in through SSO', () => {
  it('offers SSO first when the server has it, coming back to this page', async () => {
    fakeBackend({
      ...baseRoutes(),
      'GET /auth/session': () => ({ sso: true, signed_in: false, sso_only: false }),
    });
    renderAt('/alerts');
    const link = await screen.findByRole('link', { name: 'Sign in with SSO' });
    expect(link.getAttribute('href')).toMatch(/^\/auth\/sso\/login\?next=/);
    expect(screen.getByLabelText('API key')).toBeInTheDocument(); // a token still works
  });

  it('signed in, every request carries the CSRF token and no key; signing out ends it', async () => {
    const { calls } = fakeBackend({
      ...baseRoutes(),
      'GET /auth/session': () => SIGNED_IN,
      'GET /api/v1/me': me('sso'),
      'POST /auth/sso/logout': () => ({ signed_out: true, provider_sign_out: null }),
    });
    const reload = vi.fn();
    vi.stubGlobal('location', { ...globalThis.location, reload, assign: vi.fn() });
    renderAt('/');
    expect(await screen.findByText('Signed in as alice · Owner via SSO')).toBeInTheDocument();
    const apiCalls = calls.filter((c) => c.path.startsWith('/api/v1/'));
    expect(apiCalls.length).toBeGreaterThan(0);
    for (const call of apiCalls) {
      expect(call.headers.get('X-QAG-CSRF')).toBe('the-csrf-token');
      expect(call.headers.get('X-API-Key')).toBeNull();
    }
    expect(screen.queryByLabelText('API key')).not.toBeVisible();
    await userEvent.click(screen.getByRole('button', { name: 'Sign out' }));
    await waitFor(() => expect(reload).toHaveBeenCalled());
    expect(calls.find((c) => c.path === '/auth/sso/logout')!.headers.get('X-QAG-CSRF')).toBe(
      'the-csrf-token',
    );
  });

  it('a key applied to the tab is what is sent, not the session', async () => {
    setApiKey('qagadm_key');
    const { calls } = fakeBackend({
      ...baseRoutes(),
      'GET /auth/session': () => SIGNED_IN,
      'GET /api/v1/me': me('token'),
    });
    renderAt('/');
    expect(await screen.findByText('Signed in as alice · Owner')).toBeInTheDocument();
    const call = calls.find((c) => c.path === '/api/v1/me')!;
    expect(call.headers.get('X-API-Key')).toBe('qagadm_key');
    expect(call.headers.get('X-QAG-CSRF')).toBeNull();
  });

  it('says why a sign-in was refused, once', async () => {
    fakeBackend({
      ...baseRoutes(),
      'GET /auth/session': () => ({ sso: true, signed_in: false, sso_only: false }),
    });
    renderAt('/?sso_error=sso_no_role&sso_message=You%20are%20in%20no%20group');
    expect(
      await screen.findByText("Sign-in through SSO didn't go ahead: You are in no group (sso_no_role)"),
    ).toBeInTheDocument();
  });

  it('a linked administrator has their role from their groups; another can be linked', async () => {
    const { calls } = fakeBackend({
      ...baseRoutes(),
      'GET /auth/session': () => SIGNED_IN,
      'GET /api/v1/me': me('sso'),
      'GET /api/v1/administrators': () => ({
        items: [
          admin('alice', { role: 'owner', sso: { status: 'linked', issuer: 'https://idp' } }),
          admin('bob'),
        ],
      }),
      'GET /api/v1/administrators/alice': () => admin('alice', { role: 'owner', sso: { status: 'linked' } }),
      'GET /api/v1/administrators/bob': () => admin('bob'),
      'PATCH /api/v1/administrators/bob': () => admin('bob', { sso: { status: 'pending' } }),
    });
    renderAt('/administrators');
    const row = await element('tr[data-name="alice"]');
    expect(within(row).getByText('SSO')).toBeInTheDocument();
    expect(within(row).getByText('you')).toBeInTheDocument();
    await userEvent.click(within(row).getByRole('button', { name: 'Edit' }));
    let drawer = await element('#drawer');
    expect(await within(drawer).findByLabelText('Role')).toBeDisabled();
    await userEvent.click(within(drawer).getByRole('button', { name: 'Cancel' }));
    const bob = document.querySelector<HTMLElement>('tr[data-name="bob"]')!;
    await userEvent.click(within(bob).getByRole('button', { name: 'Edit' }));
    drawer = await element('#drawer');
    await userEvent.selectOptions(await within(drawer).findByLabelText('Sign-in through SSO'), 'pending');
    await userEvent.click(within(drawer).getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(calls.some((c) => c.method === 'PATCH')).toBe(true));
    expect(calls.find((c) => c.method === 'PATCH')!.body).toMatchObject({ sso: 'pending', role: 'admin' });
  });
});

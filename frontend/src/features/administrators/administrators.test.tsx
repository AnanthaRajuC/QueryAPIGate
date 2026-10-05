import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { fakeBackend, jsonResponse } from '@/test/fakeBackend';
import { baseRoutes } from '@/test/fixtures';
import { renderAt } from '@/test/renderApp';

vi.mock('@/components/SqlEditor', () => ({ SqlEditor: () => <textarea aria-label="SQL" /> }));

afterEach(() => vi.unstubAllGlobals());

const OWNER_CAPS = ['admins.read', 'admins.write', 'access.read', 'observe', 'queries.read', 'self'];

const admin = (name: string, role: string, extra = {}) => ({
  name,
  role,
  email: null,
  active: true,
  created_at: '2026-10-05 09:00:00',
  created_by: 'admin',
  last_seen_at: null,
  tokens: 1,
  ...extra,
});

const token = (id: string, adminName: string, extra = {}) => ({
  id,
  admin: adminName,
  label: 'laptop',
  created_at: '2026-10-05 09:00:00',
  expires_at: '2027-01-03',
  expired: false,
  last_used_at: null,
  ...extra,
});

describe('Administrators', () => {
  it('an owner creates an administrator and is shown their first token, once', async () => {
    const { calls } = fakeBackend({
      ...baseRoutes(),
      'GET /api/v1/me': () => ({
        name: 'boss',
        role: 'owner',
        via: 'token',
        capabilities: OWNER_CAPS,
        data_access: true,
      }),
      'GET /api/v1/administrators': () => ({ items: [admin('boss', 'owner', { email: 'boss@corp.com' })] }),
      'POST /api/v1/administrators': () => jsonResponse(admin('alice', 'auditor', { tokens: 0 }), 201),
      'POST /api/v1/administrators/alice/tokens': () =>
        jsonResponse({ ...token('tok_1', 'alice', { label: 'first token' }), secret: 'qagadm_secret' }, 201),
    });
    renderAt('/administrators');
    const row = await screen.findByText('boss@corp.com');
    expect(within(row.closest('tr')!).getByText('you')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'New administrator' }));
    const drawer = document.getElementById('drawer')!;
    await userEvent.type(within(drawer).getByLabelText('Name'), 'alice');
    await userEvent.selectOptions(within(drawer).getByLabelText('Role'), 'auditor');
    await userEvent.click(within(drawer).getByRole('button', { name: 'Create' }));
    expect(await within(drawer).findByDisplayValue('qagadm_secret')).toBeInTheDocument();
    expect(calls.find((c) => c.method === 'POST' && c.path === '/api/v1/administrators')!.body).toEqual({
      name: 'alice',
      role: 'auditor',
      email: null,
    });
    const issued = calls.find((c) => c.path === '/api/v1/administrators/alice/tokens')!.body as {
      label: string;
      expires_at: string;
    };
    expect(issued.label).toBe('first token');
    expect(issued.expires_at).toMatch(/^\d{4}-\d{2}-\d{2}$/);
  });

  it('anyone else sees their own account and manages their own tokens', async () => {
    const { calls } = fakeBackend({
      ...baseRoutes(),
      'GET /api/v1/me': () => ({
        name: 'dev',
        role: 'developer',
        via: 'token',
        capabilities: ['queries.read', 'queries.write', 'self'],
        data_access: true,
      }),
      'GET /api/v1/administrators/dev/tokens': () => ({ items: [token('tok_1', 'dev')] }),
      'POST /api/v1/administrators/dev/tokens': () =>
        jsonResponse({ ...token('tok_2', 'dev', { label: 'ci' }), secret: 'qagadm_new' }, 201),
    });
    renderAt('/administrators');
    expect(await screen.findByText('laptop')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'New administrator' })).toBeNull();
    expect(calls.some((c) => c.path === '/api/v1/administrators')).toBe(false);
    await userEvent.type(screen.getByLabelText('New token: label'), 'ci');
    await userEvent.click(screen.getByRole('button', { name: 'Issue token' }));
    expect(await screen.findByDisplayValue('qagadm_new')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Done' }));
    await waitFor(() => expect(screen.queryByDisplayValue('qagadm_new')).toBeNull());
  });
});

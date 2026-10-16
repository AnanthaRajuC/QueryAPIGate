import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { setApiKey } from '@/auth/apiKey';
import { getWorkspace, setWorkspace } from '@/auth/workspace';
import { fakeBackend, jsonResponse, type Call } from '@/test/fakeBackend';
import { baseRoutes } from '@/test/fixtures';
import { renderAt } from '@/test/renderApp';

vi.mock('@/components/SqlEditor', () => ({ SqlEditor: () => <textarea aria-label="SQL" /> }));

afterEach(() => {
  vi.unstubAllGlobals();
  setApiKey('');
  setWorkspace('default');
});

const OWNER_CAPS = ['workspaces.read', 'workspaces.write', 'queries.read', 'connections.read', 'self'];
const me = (extra = {}) => ({
  name: 'admin',
  role: 'owner',
  via: 'break-glass',
  capabilities: OWNER_CAPS,
  data_access: true,
  workspace: 'default',
  server_wide: true,
  workspaces: {},
  ...extra,
});
const workspace = (name: string, members = 0) => ({
  name,
  description: null,
  created_at: '2026-10-16 09:00:00',
  created_by: 'admin',
  members,
});
const TWO = () => ({ items: [workspace('default'), workspace('team-b', 1)] });

/** baseRoutes() again under /w/<name>: what the same screens ask a workspace for. */
const under = (name: string) =>
  Object.fromEntries(
    Object.entries(baseRoutes()).map(([route, handler]) => {
      const [method, path] = route.split(' ');
      return [`${method} /w/${name}${path}`, handler];
    }),
  );

const paths = (calls: Call[]) => calls.map((c) => c.path);

describe('workspaces in the Console', () => {
  it('shows no switcher while there is only default', async () => {
    fakeBackend({
      ...baseRoutes(),
      'GET /api/v1/me': () => me(),
      'GET /api/v1/workspaces': () => ({ items: [workspace('default')] }),
    });
    renderAt('/');
    await waitFor(() => expect(document.getElementById('side')).not.toBeNull());
    await waitFor(() => expect(screen.getByText('v9.9.9')).toBeInTheDocument());
    expect(document.getElementById('workspace')).toBeNull();
  });

  it('switching sends every request to that workspace', async () => {
    const { calls } = fakeBackend({
      ...baseRoutes(),
      ...under('team-b'),
      'GET /api/v1/me': () => me(),
      'GET /w/team-b/api/v1/me': () => me({ workspace: 'team-b' }),
      'GET /api/v1/workspaces': TWO,
      'GET /w/team-b/api/v1/workspaces': TWO,
    });
    renderAt('/connections');
    const picker = (await waitFor(() => {
      const found = document.getElementById('workspace');
      if (!found) throw new Error('no switcher yet');
      return found;
    })) as HTMLSelectElement;
    expect(picker.value).toBe('default');
    calls.length = 0;
    await userEvent.selectOptions(picker, 'team-b');
    expect(getWorkspace()).toBe('team-b');
    await waitFor(() => expect(paths(calls)).toContain('/w/team-b/api/v1/me'));
    const api = paths(calls).filter((p) => p.includes('/api/v1/'));
    expect(api.every((p) => p.startsWith('/w/team-b/'))).toBe(true); // none left in default
  });

  it('an administrator whose roles are all in workspaces starts in theirs', async () => {
    setApiKey('qagadm_bea');
    fakeBackend({
      ...baseRoutes(),
      ...under('team-b'),
      'GET /api/v1/me': () =>
        me({
          name: 'bea',
          via: 'token',
          role: null,
          server_wide: false,
          capabilities: [],
          workspaces: { 'team-b': 'admin' },
        }),
      'GET /w/team-b/api/v1/me': () =>
        me({
          name: 'bea',
          via: 'token',
          role: 'admin',
          server_wide: false,
          workspaces: { 'team-b': 'admin' },
        }),
      'GET /api/v1/workspaces': () => ({ items: [workspace('team-b', 1)] }),
      'GET /w/team-b/api/v1/workspaces': () => ({ items: [workspace('team-b', 1)] }),
    });
    renderAt('/');
    await waitFor(() => expect(getWorkspace()).toBe('team-b'));
    expect(await screen.findByText('Signed in as bea · Admin')).toBeInTheDocument();
  });

  it('a tab left in a workspace that is gone goes back to default', async () => {
    setWorkspace('gone');
    fakeBackend({
      ...baseRoutes(),
      'GET /w/gone/api/v1/me': () =>
        jsonResponse({ error: "No workspace named 'gone'", code: 'workspace_not_found' }, 404),
      'GET /api/v1/me': () => me(),
    });
    renderAt('/');
    await waitFor(() => expect(getWorkspace()).toBe('default'));
  });
});

describe('the Workspaces screen', () => {
  it('lists them, creates one, and manages a workspace’s members', async () => {
    const { calls } = fakeBackend({
      ...baseRoutes(),
      'GET /api/v1/me': () => me(),
      'GET /api/v1/workspaces': TWO,
      'GET /api/v1/workspaces/default/members': () => ({ items: [] }),
      'GET /api/v1/workspaces/team-b/members': () => ({
        items: [{ admin: 'bea', role: 'admin', created_at: '2026-10-16 09:00:00', added_by: 'admin' }],
      }),
      'POST /api/v1/workspaces': () => jsonResponse(workspace('team-c'), 201),
      'PUT /api/v1/workspaces/team-b/members/carl': () => ({
        admin: 'carl',
        role: 'developer',
        created_at: '2026-10-16 10:00:00',
        added_by: 'admin',
      }),
    });
    renderAt('/workspaces');
    const table = await waitFor(() => {
      const found = document.querySelector('#workspaces-table tr[data-name="team-b"]');
      if (!found) throw new Error('no row yet');
      return found as HTMLElement;
    });
    await userEvent.click(table);
    expect(await screen.findByText('Members of team-b')).toBeInTheDocument();
    expect(document.querySelector('#workspace-members tr[data-name="bea"]')).not.toBeNull();

    const form = document.getElementById('add-member')!;
    await userEvent.type(within(form).getByLabelText('Administrator'), 'carl');
    await userEvent.click(within(form).getByRole('button', { name: 'Add member' }));
    await waitFor(() => expect(calls.some((c) => c.method === 'PUT')).toBe(true));
    expect(calls.find((c) => c.method === 'PUT')!.body).toEqual({ role: 'developer' });

    await userEvent.click(screen.getByRole('button', { name: 'New workspace' }));
    const drawer = document.getElementById('drawer')!;
    await userEvent.type(within(drawer).getByLabelText('Name'), 'team-c');
    await userEvent.click(within(drawer).getByRole('button', { name: 'Create workspace' }));
    await waitFor(() => expect(calls.some((c) => c.method === 'POST')).toBe(true));
    expect(calls.find((c) => c.method === 'POST')!.body).toEqual({ name: 'team-c', description: null });
  });
});

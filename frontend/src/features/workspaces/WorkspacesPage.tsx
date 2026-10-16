import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState, type FormEvent } from 'react';

import { ApiError, api, unwrap, unwrapEmpty, type Schemas } from '@/api/client';
import { useCan, useMe, useWorkspaces, type Workspace } from '@/app/data';
import { Field, FormActions, Loading, useFeedback } from '@/app/feedback';
import { useSwitchWorkspace } from '@/app/workspaceHooks';
import { getWorkspace } from '@/auth/workspace';
import { ExperimentalTag } from '@/components/Experimental';
import { PencilIcon, TrashIcon } from '@/components/icons';
import { Time } from '@/components/Time';

// Workspaces and their members (ADR 0023): delegated administration for teams on one server. Everyone sees the
// workspaces they belong to - server-wide roles see them all; owners create and remove them; a workspace's own owner
// manages who belongs to it.

type Member = Schemas['WorkspaceMember'];

const ROLES = ['owner', 'admin', 'developer', 'auditor'] as const;

function useReport() {
  const { showError } = useFeedback();
  return (error: unknown) =>
    error instanceof ApiError
      ? showError(error.message, { status: error.status })
      : showError((error as Error).message);
}

function useMembers(name: string | null) {
  return useQuery({
    queryKey: ['workspaces', name, 'members'],
    queryFn: async () =>
      unwrap(await api.GET('/api/v1/workspaces/{name}/members', { params: { path: { name: name! } } })).items,
    enabled: name !== null,
    retry: false,
  });
}

export function WorkspacesPage() {
  const me = useMe();
  const can = useCan();
  const list = useWorkspaces(Boolean(me.data));
  const { openDrawer } = useFeedback();
  const [selected, setSelected] = useState<string | null>(null);
  const owner = Boolean(me.data) && can('workspaces.write');
  const shown = selected ?? list.data?.[0]?.name ?? null;

  let body: React.ReactNode;
  if (list.isPending) body = <Loading text="Loading workspaces…" />;
  else if (list.isError) body = <div className="hint">{list.error.message}</div>;
  else body = <WorkspaceTable items={list.data} selected={shown} onSelect={setSelected} owner={owner} />;

  return (
    <>
      <div className="page-head">
        <div className="titles">
          <h1>
            Workspaces <ExperimentalTag />
          </h1>
          <span className="sub">
            Teams on one server: each manages its own connections, queries, keys and exports, without reaching
            another&apos;s.
          </span>
        </div>
        <span className="spacer" />
        {owner && (
          <button
            id="new-workspace"
            type="button"
            className="btn primary"
            onClick={() => openDrawer({ title: 'New workspace', content: <WorkspaceForm /> })}
          >
            New workspace
          </button>
        )}
      </div>
      <div id="workspaces-table" className="panel">
        {body}
      </div>
      {shown && <Members name={shown} />}
    </>
  );
}

function WorkspaceTable({
  items,
  selected,
  onSelect,
  owner,
}: {
  items: Workspace[];
  selected: string | null;
  onSelect: (name: string) => void;
  owner: boolean;
}) {
  const { openDrawer, toast } = useFeedback();
  const report = useReport();
  const client = useQueryClient();
  const switchTo = useSwitchWorkspace();
  const current = getWorkspace();
  const remove = useMutation({
    mutationFn: async (name: string) =>
      unwrapEmpty(await api.DELETE('/api/v1/workspaces/{name}', { params: { path: { name } } })),
    onSuccess: (_, name) => {
      void client.invalidateQueries({ queryKey: ['workspaces'] });
      toast(`Removed workspace ${name}`);
    },
    onError: report,
  });
  return (
    <table className="grid">
      <thead>
        <tr>
          <th>Name</th>
          <th>Description</th>
          <th>Members</th>
          <th>Created</th>
          <th />
        </tr>
      </thead>
      <tbody>
        {items.map((w) => (
          <tr
            key={w.name}
            data-name={w.name}
            className={w.name === selected ? 'selected' : undefined}
            onClick={() => onSelect(w.name)}
          >
            <td className="mono">
              {w.name}
              {w.name === current && <span className="tag">this tab</span>}
            </td>
            <td className={w.description ? undefined : 'dim'}>{w.description ?? '—'}</td>
            <td className="mono">{w.members}</td>
            <td className="mono dim">
              <Time value={w.created_at} />
            </td>
            <td className="actions">
              {w.name !== current && (
                <button type="button" className="btn ghost sm" onClick={() => switchTo(w.name)}>
                  Work here
                </button>
              )}
              {owner && (
                <button
                  type="button"
                  className="btn ghost sm icon"
                  title={`Edit ${w.name}`}
                  aria-label="Edit"
                  onClick={(e) => {
                    e.stopPropagation();
                    openDrawer({
                      title: 'Edit workspace',
                      kicker: w.name,
                      content: <WorkspaceForm existing={w} />,
                    });
                  }}
                >
                  <PencilIcon />
                </button>
              )}
              {owner && w.name !== 'default' && (
                <button
                  type="button"
                  className="btn ghost sm icon danger"
                  title="Remove - only once it owns nothing"
                  aria-label={`Remove ${w.name}`}
                  onClick={(e) => {
                    e.stopPropagation();
                    if (window.confirm(`Remove workspace '${w.name}'? Only an empty one can be removed.`))
                      remove.mutate(w.name);
                  }}
                >
                  <TrashIcon />
                </button>
              )}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function WorkspaceForm({ existing }: { existing?: Workspace }) {
  const { closeDrawer, showError, toast } = useFeedback();
  const report = useReport();
  const client = useQueryClient();
  const [name, setName] = useState(existing?.name ?? '');
  const [description, setDescription] = useState(existing?.description ?? '');
  const save = useMutation({
    mutationFn: async () => {
      const text = description.trim() || null;
      if (existing)
        return unwrap(
          await api.PATCH('/api/v1/workspaces/{name}', {
            params: { path: { name: existing.name } },
            body: { description: text },
          }),
        );
      return unwrap(await api.POST('/api/v1/workspaces', { body: { name: name.trim(), description: text } }));
    },
    onSuccess: (workspace) => {
      showError('');
      void client.invalidateQueries({ queryKey: ['workspaces'] });
      closeDrawer();
      toast(existing ? `Updated ${workspace.name}` : `Created workspace ${workspace.name}`);
    },
    onError: report,
  });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!existing && !name.trim()) {
      showError('A name is required.', { errors: { name: 'This field is required' } });
      return;
    }
    save.mutate();
  };
  return (
    <form className="form" noValidate onSubmit={submit}>
      <Field
        id="w-name"
        label="Name"
        hint={existing ? undefined : "Letters, digits, '.', '_' or '-' - e.g. team-a."}
      >
        <input
          id="w-name"
          value={name}
          placeholder="team-a"
          autoComplete="off"
          spellCheck={false}
          disabled={Boolean(existing)}
          autoFocus={!existing}
          onChange={(e) => setName(e.target.value)}
        />
      </Field>
      <Field id="w-description" label="Description">
        <input id="w-description" value={description} onChange={(e) => setDescription(e.target.value)} />
      </Field>
      <FormActions onCancel={closeDrawer}>
        <button type="submit" className="btn primary" disabled={save.isPending}>
          {existing ? 'Save' : 'Create workspace'}
        </button>
      </FormActions>
    </form>
  );
}

function Members({ name }: { name: string }) {
  const me = useMe();
  const can = useCan();
  const members = useMembers(name);
  const report = useReport();
  const client = useQueryClient();
  const { toast } = useFeedback();
  const [admin, setAdmin] = useState('');
  const [role, setRole] = useState<string>('developer');
  // a server owner, or this workspace's own owner (ADR 0023 §3)
  const manages = can('workspaces.write') || me.data?.workspaces?.[name] === 'owner';
  const refresh = () => {
    void client.invalidateQueries({ queryKey: ['workspaces'] });
  };
  const put = useMutation({
    mutationFn: async ({ who, as }: { who: string; as: string }) =>
      unwrap(
        await api.PUT('/api/v1/workspaces/{name}/members/{admin}', {
          params: { path: { name, admin: who } },
          body: { role: as as Member['role'] },
        }),
      ),
    onSuccess: (member) => {
      refresh();
      setAdmin('');
      toast(`${member.admin} is ${member.role} in ${name}`);
    },
    onError: report,
  });
  const remove = useMutation({
    mutationFn: async (who: string) =>
      unwrapEmpty(
        await api.DELETE('/api/v1/workspaces/{name}/members/{admin}', {
          params: { path: { name, admin: who } },
        }),
      ),
    onSuccess: (_, who) => {
      refresh();
      toast(`${who} is no longer in ${name}`);
    },
    onError: report,
  });
  return (
    <div className="panel" id="workspace-members">
      <div className="panel-head">
        <strong>Members of {name}</strong>
        <span className="dim"> — administrators with a role in this workspace only</span>
      </div>
      {members.isPending ? (
        <Loading />
      ) : members.isError ? (
        <div className="hint">{members.error.message}</div>
      ) : !members.data.length ? (
        <div className="empty">
          <span>No members: only server-wide roles reach this workspace.</span>
        </div>
      ) : (
        <table className="grid">
          <thead>
            <tr>
              <th>Administrator</th>
              <th>Role</th>
              <th>Added by</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {members.data.map((m) => (
              <tr key={m.admin} data-name={m.admin}>
                <td className="mono">{m.admin}</td>
                <td>
                  {manages ? (
                    <select
                      aria-label={`Role of ${m.admin}`}
                      value={m.role}
                      onChange={(e) => put.mutate({ who: m.admin, as: e.target.value })}
                    >
                      {ROLES.map((r) => (
                        <option key={r} value={r}>
                          {r}
                        </option>
                      ))}
                    </select>
                  ) : (
                    m.role
                  )}
                </td>
                <td className={m.added_by ? 'mono' : 'dim'}>{m.added_by ?? '—'}</td>
                <td className="actions">
                  {manages && (
                    <button
                      type="button"
                      className="btn ghost sm"
                      onClick={() => {
                        if (window.confirm(`Take ${m.admin} out of ${name}?`)) remove.mutate(m.admin);
                      }}
                    >
                      Remove
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {manages && (
        <form
          className="form-inline"
          id="add-member"
          onSubmit={(e) => {
            e.preventDefault();
            if (admin.trim()) put.mutate({ who: admin.trim(), as: role });
          }}
        >
          <input
            aria-label="Administrator"
            placeholder="administrator's name"
            value={admin}
            onChange={(e) => setAdmin(e.target.value)}
          />
          <select aria-label="Role" value={role} onChange={(e) => setRole(e.target.value)}>
            {ROLES.map((r) => (
              <option key={r} value={r}>
                {r}
              </option>
            ))}
          </select>
          <button type="submit" className="btn" disabled={!admin.trim() || put.isPending}>
            Add member
          </button>
        </form>
      )}
    </div>
  );
}

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState, type FormEvent } from 'react';

import { ApiError, api, unwrap, unwrapEmpty, unwrapWithEtag, type Schemas } from '@/api/client';
import { useCan, useGate, useMe } from '@/app/data';
import { Field, FormActions, Loading, useFeedback } from '@/app/feedback';
import { PencilIcon, TrashIcon } from '@/components/icons';
import { Time } from '@/components/Time';
import { SecretReveal } from '@/features/access/forms';

// Named administrators and their admin tokens (ADR 0003), on /api/v1/administrators. Owners see everyone; every other
// administrator sees their own account and manages their own tokens.

type Administrator = Schemas['Administrator'];
type AdminToken = Schemas['AdminToken'];

const ROLES = [
  { value: 'owner', label: 'Owner', text: 'everything, including administrators' },
  { value: 'admin', label: 'Admin', text: 'connections, API keys and roles, queries, cache, settings' },
  { value: 'developer', label: 'Developer', text: 'saved queries and SQL; no keys, no connection changes' },
  { value: 'auditor', label: 'Auditor', text: 'read only - audit log, history, configuration; no SQL' },
] as const;

const roleLabel = (role: string) => ROLES.find((r) => r.value === role)?.label ?? role;
const TOKEN_DAYS = 90;
const inDays = (days: number) => new Date(Date.now() + days * 86_400_000).toISOString().slice(0, 10);
const TOKEN_HINT =
  'Store this now — it cannot be shown again. Paste it into the API key box at the bottom of the sidebar, or send ' +
  'it as the X-API-Key header.';

function useReport() {
  const { showError } = useFeedback();
  return (error: unknown) =>
    error instanceof ApiError
      ? showError(error.message, { status: error.status })
      : showError((error as Error).message);
}

function useAdministrators(enabled: boolean) {
  const gate = useGate('admins.read', 'other administrators');
  return useQuery({
    queryKey: ['administrators'],
    queryFn: async () => {
      await gate.check();
      return unwrap(await api.GET('/api/v1/administrators')).items;
    },
    enabled,
    retry: false,
  });
}

function useTokens(name: string) {
  return useQuery({
    queryKey: ['administrators', name, 'tokens'],
    queryFn: async () =>
      unwrap(await api.GET('/api/v1/administrators/{name}/tokens', { params: { path: { name } } })).items,
    retry: false,
  });
}

export function AdministratorsPage() {
  const me = useMe();
  const can = useCan();
  const owner = Boolean(me.data) && can('admins.read');
  const list = useAdministrators(owner);
  const { openDrawer } = useFeedback();

  const newAdmin = () => openDrawer({ title: 'New administrator', content: <AdministratorForm /> });

  let body: React.ReactNode;
  if (me.isPending) {
    body = <Loading />;
  } else if (me.isError) {
    body = (
      <div className="empty">
        <strong>Not signed in as an administrator</strong>
        <span>
          Apply an admin token (qagadm_…) or the shared admin key in the box at the bottom of the sidebar.
        </span>
      </div>
    );
  } else if (!owner) {
    body = <OwnAccount name={me.data.name ?? ''} role={me.data.role} />;
  } else if (list.isPending) {
    body = <Loading text="Loading administrators…" />;
  } else if (list.isError) {
    body = <div className="hint">{list.error.message}</div>;
  } else if (!list.data.length) {
    body = (
      <div className="empty">
        <strong>No administrators yet</strong>
        <span>
          Everyone shares QUERYAPIGATE_API_KEY today, so the audit log can only say “admin”. Give each person
          their own sign-in, with a role that fits what they do — then the shared key becomes a break-glass
          key.
        </span>
        <button type="button" className="btn primary" onClick={newAdmin}>
          New administrator
        </button>
      </div>
    );
  } else {
    body = <AdministratorTable items={list.data} self={me.data.via === 'token' ? me.data.name : null} />;
  }

  return (
    <>
      <div className="page-head">
        <div className="titles">
          <h1>Administrators</h1>
          <span className="sub" id="administrators-sub">
            {owner
              ? 'The people and pipelines that manage this server, each with a role and their own tokens.'
              : 'Your account and your admin tokens.'}
          </span>
        </div>
        <span className="spacer" />
        {owner && (
          <button id="new-administrator" type="button" className="btn primary" onClick={newAdmin}>
            New administrator
          </button>
        )}
      </div>
      <div id="administrators-table" className="panel">
        {body}
      </div>
    </>
  );
}

function AdministratorTable({ items, self }: { items: Administrator[]; self: string | null }) {
  const { openDrawer, toast } = useFeedback();
  const report = useReport();
  const client = useQueryClient();

  const remove = async (name: string) => {
    if (!window.confirm(`Remove administrator '${name}'? Every token of theirs stops working immediately.`))
      return;
    try {
      unwrapEmpty(await api.DELETE('/api/v1/administrators/{name}', { params: { path: { name } } }));
      toast('Removed ' + name);
      void client.invalidateQueries({ queryKey: ['administrators'] });
    } catch (error) {
      report(error);
    }
  };

  return (
    <div style={{ overflowX: 'auto' }}>
      <table className="grid">
        <thead>
          <tr>
            {['Name', 'Role', 'Email', 'Last seen', 'Tokens', ''].map((t, i) => (
              <th key={i}>{t}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {items.map((a) => (
            <tr key={a.name} data-name={a.name}>
              <td>
                <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                  <span
                    className={`dot ${a.active ? 'ok' : 'off'}`}
                    title={a.active ? 'Active' : 'Inactive'}
                  />
                  <span className="name">{a.name}</span>
                  {a.name === self && <span className="tag">you</span>}
                </div>
              </td>
              <td>{roleLabel(a.role)}</td>
              <td className={a.email ? undefined : 'dim'}>{a.email ?? '—'}</td>
              <td className="mono dim" style={{ whiteSpace: 'nowrap' }}>
                <Time value={a.last_seen_at} fallback="never" />
              </td>
              <td className="mono">{a.tokens}</td>
              <td>
                <div className="actions">
                  <button
                    type="button"
                    className="btn ghost sm"
                    onClick={() =>
                      openDrawer({
                        title: 'Admin tokens',
                        kicker: a.name,
                        wide: true,
                        content: <Tokens name={a.name} />,
                      })
                    }
                  >
                    Tokens
                  </button>
                  <button
                    type="button"
                    className="btn ghost sm icon"
                    title={`Edit ${a.name}`}
                    aria-label="Edit"
                    onClick={() =>
                      openDrawer({
                        title: 'Edit administrator',
                        kicker: a.name,
                        content: <AdministratorForm name={a.name} />,
                      })
                    }
                  >
                    <PencilIcon />
                  </button>
                  <button
                    type="button"
                    className="btn ghost sm icon danger"
                    title={`Remove ${a.name}`}
                    aria-label="Remove"
                    onClick={() => void remove(a.name)}
                  >
                    <TrashIcon />
                  </button>
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** A non-owner's view: who they are, and their own tokens. */
function OwnAccount({ name, role }: { name: string; role: string }) {
  return (
    <div className="form" style={{ padding: 16 }}>
      <div>
        Signed in as <strong>{name}</strong> · {roleLabel(role)} — {ROLES.find((r) => r.value === role)?.text}
        .
      </div>
      <Tokens name={name} />
    </div>
  );
}

/** New administrator - and its first token, shown once - or edit one's role, email and active. */
function AdministratorForm({ name }: { name?: string }) {
  const detail = useQuery({
    queryKey: ['administrators', 'detail', name],
    queryFn: async () =>
      unwrapWithEtag(await api.GET('/api/v1/administrators/{name}', { params: { path: { name: name! } } })),
    enabled: Boolean(name),
    retry: false,
    gcTime: 0,
  });
  if (name && detail.isPending) return <Loading />;
  if (name && detail.isError) return <div className="hint">{detail.error.message}</div>;
  return (
    <AdministratorFields
      key={name ?? ''}
      name={name}
      existing={detail.data?.data}
      etag={detail.data?.etag ?? null}
    />
  );
}

function AdministratorFields({
  name,
  existing,
  etag,
}: {
  name?: string;
  existing?: Administrator;
  etag: string | null;
}) {
  const isEdit = Boolean(name);
  const { closeDrawer, openDrawer, showError, toast } = useFeedback();
  const report = useReport();
  const client = useQueryClient();
  const [adminName, setAdminName] = useState(name ?? '');
  const [role, setRole] = useState<string>(existing?.role ?? 'developer');
  const [email, setEmail] = useState(existing?.email ?? '');
  const [active, setActive] = useState(existing ? existing.active : true);

  const save = useMutation({
    mutationFn: async () => {
      const fields = { role: role as Administrator['role'], email: email.trim() || null };
      if (isEdit) {
        unwrap(
          await api.PATCH('/api/v1/administrators/{name}', {
            params: { path: { name: name! }, header: etag ? { 'If-Match': etag } : {} },
            body: { ...fields, active },
          }),
        );
        return null;
      }
      const created = unwrap(
        await api.POST('/api/v1/administrators', { body: { name: adminName.trim(), ...fields } }),
      );
      return unwrap(
        await api.POST('/api/v1/administrators/{name}/tokens', {
          params: { path: { name: created.name } },
          body: { label: 'first token', expires_at: inDays(TOKEN_DAYS) },
        }),
      );
    },
    onSuccess: (token) => {
      showError('');
      void client.invalidateQueries({ queryKey: ['administrators'] });
      if (!token) {
        closeDrawer();
        toast('Updated ' + name);
        return;
      }
      openDrawer({
        title: 'Administrator created',
        kicker: token.admin,
        content: (
          <SecretReveal
            secret={token.secret}
            label={`Admin token for ${token.admin} (expires ${token.expires_at})`}
            hint={TOKEN_HINT}
          />
        ),
      });
    },
    onError: report,
  });

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!isEdit && !adminName.trim()) {
      showError('A name is required.', { errors: { name: 'This field is required' } });
      return;
    }
    save.mutate();
  };

  return (
    <form className="form" noValidate onSubmit={submit}>
      <Field
        id="a-name"
        label="Name"
        hint={isEdit ? undefined : 'A person (alice) or a pipeline (ci-deploy). Shown in the audit log.'}
      >
        <input
          id="a-name"
          value={adminName}
          placeholder="alice"
          autoComplete="off"
          spellCheck={false}
          disabled={isEdit}
          autoFocus={!isEdit}
          onChange={(e) => setAdminName(e.target.value)}
        />
      </Field>
      <Field id="a-role" label="Role" hint={ROLES.find((r) => r.value === role)?.text}>
        <select id="a-role" value={role} onChange={(e) => setRole(e.target.value)}>
          {ROLES.map((r) => (
            <option key={r.value} value={r.value}>
              {r.label}
            </option>
          ))}
        </select>
      </Field>
      <Field id="a-email" label="Email" hint="Optional.">
        <input
          id="a-email"
          type="email"
          value={email}
          autoComplete="off"
          onChange={(e) => setEmail(e.target.value)}
        />
      </Field>
      {isEdit && (
        <label className="switch">
          <input
            id="a-active"
            type="checkbox"
            checked={active}
            onChange={(e) => setActive(e.target.checked)}
          />
          Active<span className="hint">— unchecking stops every token of theirs immediately</span>
        </label>
      )}
      {!isEdit && (
        <div className="hint">
          Their first admin token, valid for {TOKEN_DAYS} days, is shown once after you create them.
        </div>
      )}
      <FormActions onCancel={closeDrawer}>
        <button type="submit" className="btn primary" disabled={save.isPending}>
          {isEdit ? 'Save' : 'Create'}
        </button>
      </FormActions>
    </form>
  );
}

/** An administrator's tokens: list, issue (the secret shown once, here), revoke. */
function Tokens({ name }: { name: string }) {
  const tokens = useTokens(name);
  const client = useQueryClient();
  const { toast } = useFeedback();
  const report = useReport();
  const [label, setLabel] = useState('');
  const [expires, setExpires] = useState(inDays(TOKEN_DAYS));
  const [issued, setIssued] = useState<Schemas['AdminTokenCreated'] | null>(null);
  const refresh = () => void client.invalidateQueries({ queryKey: ['administrators'] });

  const issue = useMutation({
    mutationFn: async () =>
      unwrap(
        await api.POST('/api/v1/administrators/{name}/tokens', {
          params: { path: { name } },
          body: { label: label.trim() || null, expires_at: expires || null },
        }),
      ),
    onSuccess: (token) => {
      setIssued(token);
      setLabel('');
      refresh();
    },
    onError: report,
  });

  const revoke = async (token: AdminToken) => {
    if (
      !window.confirm(
        `Revoke ${token.label ? `'${token.label}'` : token.id}? Anything using it stops working now.`,
      )
    )
      return;
    try {
      unwrapEmpty(
        await api.DELETE('/api/v1/administrators/{name}/tokens/{token_id}', {
          params: { path: { name, token_id: token.id } },
        }),
      );
      toast('Revoked ' + (token.label ?? token.id));
      refresh();
    } catch (error) {
      report(error);
    }
  };

  const today = new Date().toISOString().slice(0, 10);
  return (
    <div className="form" id="admin-tokens">
      {tokens.isPending ? (
        <Loading text="Loading tokens…" />
      ) : tokens.isError ? (
        <div className="hint">{tokens.error.message}</div>
      ) : !tokens.data.length ? (
        <div className="hint">No tokens. Issue one below to sign in with.</div>
      ) : (
        <table className="grid">
          <thead>
            <tr>
              {['Label', 'Expires', 'Last used', ''].map((t, i) => (
                <th key={i}>{t}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {tokens.data.map((t) => (
              <tr key={t.id} data-token={t.id}>
                <td title={t.id}>{t.label ?? <span className="dim mono">{t.id}</span>}</td>
                <td style={{ whiteSpace: 'nowrap' }}>
                  {t.expires_at ? (
                    <span style={t.expired ? { color: 'var(--danger)' } : undefined}>
                      {t.expires_at + (t.expired ? ' (expired)' : t.expires_at === today ? ' (today)' : '')}
                    </span>
                  ) : (
                    <span className="dim">never</span>
                  )}
                </td>
                <td className="mono dim">
                  <Time value={t.last_used_at} fallback="never" />
                </td>
                <td>
                  <button type="button" className="btn ghost sm danger" onClick={() => void revoke(t)}>
                    Revoke
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {issued ? (
        <SecretReveal
          secret={issued.secret}
          label={`New token${issued.label ? ` '${issued.label}'` : ''} (${issued.expires_at ? `expires ${issued.expires_at}` : 'never expires'})`}
          hint={TOKEN_HINT}
          onDone={() => setIssued(null)}
        />
      ) : (
        <form
          className="grid2"
          noValidate
          onSubmit={(e) => {
            e.preventDefault();
            issue.mutate();
          }}
        >
          <Field id="t-label" label="New token: label" hint="What it's for - laptop, ci.">
            <input id="t-label" value={label} autoComplete="off" onChange={(e) => setLabel(e.target.value)} />
          </Field>
          <Field id="t-expires" label="Expires" hint="Leave blank for no expiry.">
            <input id="t-expires" type="date" value={expires} onChange={(e) => setExpires(e.target.value)} />
          </Field>
          <div>
            <button type="submit" className="btn" disabled={issue.isPending}>
              Issue token
            </button>
          </div>
        </form>
      )}
    </div>
  );
}

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState, type FormEvent } from 'react';

import { ApiError, api, unwrap, unwrapEmpty, unwrapWithEtag, type Schemas } from '@/api/client';
import { useRoles } from '@/app/data';
import { copyText, Field, FormActions, Loading, useFeedback } from '@/app/feedback';

import { GrantFields, grantsPayload, initialGrants, useGrantChoices, type GrantState } from './grants';

// The classic key and role drawers (ui.py openApiKeyForm, revealApiKey, openRoleForm) and their deletes, on
// /api/v1/api-keys and /api/v1/roles.

type ApiKey = Schemas['ApiKey'];
type Role = Schemas['Role'];

const NAME_HINT = 'Letters, digits, spaces, “.”, “_” and “-”.';

function useReportError() {
  const { showError } = useFeedback();
  return (error: unknown, what: string) => {
    if (error instanceof ApiError && error.code === 'precondition_failed') {
      showError(`This ${what} changed since you opened the form - close it, check it and try again.`, {
        status: 412,
      });
    } else if (error instanceof ApiError) {
      showError(error.message, { status: error.status });
    } else {
      showError((error as Error).message);
    }
  };
}

/** Everything that shows a key or role's grants or reach - refreshed after any change to one. */
function useInvalidateAccess() {
  const client = useQueryClient();
  return () => {
    void client.invalidateQueries({ queryKey: ['apikeys'] });
    void client.invalidateQueries({ queryKey: ['roles'] });
    void client.invalidateQueries({ queryKey: ['collections'] });
  };
}

function useDetail<T>(kind: 'api-keys' | 'roles', name?: string) {
  return useQuery({
    queryKey: [kind === 'api-keys' ? 'apikeys' : 'roles', 'detail', name],
    queryFn: async () => {
      const result =
        kind === 'api-keys'
          ? await api.GET('/api/v1/api-keys/{name}', { params: { path: { name: name! } } })
          : await api.GET('/api/v1/roles/{name}', { params: { path: { name: name! } } });
      return unwrapWithEtag(result as { data?: T; error?: unknown; response: Response });
    },
    enabled: Boolean(name),
    retry: false,
    gcTime: 0,
  });
}

// ---- API keys ----

/** New / Edit API key. `fromRole` preselects "Create from" (the Roles table's "New key from this"). */
export function ApiKeyForm({ name, fromRole }: { name?: string; fromRole?: string }) {
  const detail = useDetail<ApiKey>('api-keys', name);
  if (name && detail.isPending) return <Loading />;
  if (name && detail.isError) return <div className="hint">{detail.error.message}</div>;
  return (
    <ApiKeyFormFields
      key={name ?? ''}
      name={name}
      existing={detail.data?.data}
      etag={detail.data?.etag ?? null}
      fromRole={fromRole}
    />
  );
}

function ApiKeyFormFields({
  name,
  existing,
  etag,
  fromRole,
}: {
  name?: string;
  existing?: ApiKey;
  etag: string | null;
  fromRole?: string;
}) {
  const isEdit = Boolean(name);
  const { closeDrawer, openDrawer, showError, toast } = useFeedback();
  const reportError = useReportError();
  const invalidate = useInvalidateAccess();
  const { connectionNames, queryNames } = useGrantChoices();
  const roles = useRoles();
  const roleNames = Object.keys(roles.data ?? {}).sort();
  const [keyName, setKeyName] = useState(name ?? '');
  const [role, setRole] = useState(fromRole ?? '');
  const [grants, setGrants] = useState<GrantState>(() => initialGrants(existing));
  const [expires, setExpires] = useState(existing?.expires_at ?? '');
  const [active, setActive] = useState(existing ? existing.active : true);

  const save = useMutation({
    mutationFn: async () => {
      const expiresAt = expires || null;
      if (!isEdit && role) {
        return unwrap(
          await api.POST('/api/v1/api-keys', { body: { name: keyName.trim(), role, expires_at: expiresAt } }),
        );
      }
      const body = { ...grantsPayload(grants, connectionNames, queryNames), expires_at: expiresAt };
      if (isEdit) {
        return unwrap(
          await api.PATCH('/api/v1/api-keys/{name}', {
            params: { path: { name: name! }, header: etag ? { 'If-Match': etag } : {} },
            body: { ...body, active },
          }),
        );
      }
      return unwrap(await api.POST('/api/v1/api-keys', { body: { name: keyName.trim(), ...body } }));
    },
    onSuccess: (saved) => {
      showError('');
      invalidate();
      if (isEdit) {
        closeDrawer();
        toast('Updated ' + saved.name);
      } else {
        openDrawer({
          title: 'API key created',
          kicker: saved.name,
          content: <SecretReveal secret={(saved as Schemas['ApiKeyCreated']).secret} />,
        });
      }
    },
    onError: (error) => reportError(error, 'API key'),
  });

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!isEdit && !keyName.trim()) {
      showError('An API key name is required.', { errors: { name: 'This field is required' } });
      document.getElementById('k-name')?.focus();
      return;
    }
    save.mutate();
  };

  const showRole = !isEdit && roleNames.length > 0;
  return (
    <form className="form" noValidate onSubmit={submit}>
      <Field id="k-name" label="Name" hint={isEdit ? undefined : NAME_HINT}>
        <input
          id="k-name"
          value={keyName}
          placeholder="reporting"
          autoComplete="off"
          spellCheck={false}
          required={!isEdit}
          disabled={isEdit}
          autoFocus
          onChange={(e) => setKeyName(e.target.value)}
        />
      </Field>
      {showRole && (
        <Field
          id="k-role"
          label="Create from"
          hint="Optional — copies that role’s connections, queries, collections, write access, rate limit and allowed IPs onto this key once, at creation. Editing or deleting the role afterward never changes this key."
        >
          <select id="k-role" value={role} onChange={(e) => setRole(e.target.value)}>
            <option value="">Custom (choose grants below)</option>
            {roleNames.map((r) => (
              <option key={r} value={r}>
                {r}
              </option>
            ))}
          </select>
        </Field>
      )}
      <div style={showRole && role ? { opacity: 0.4, pointerEvents: 'none' } : { opacity: 1 }}>
        <GrantFields prefix="k" state={grants} onChange={setGrants} />
      </div>
      <Field
        id="k-expires"
        label="Expires"
        hint="Optional — valid through the end of this date. Leave blank for no expiry."
      >
        <input id="k-expires" type="date" value={expires} onChange={(e) => setExpires(e.target.value)} />
      </Field>
      {isEdit && (
        <label className="switch">
          <input
            id="k-active"
            type="checkbox"
            checked={active}
            onChange={(e) => setActive(e.target.checked)}
          />
          Active<span className="hint">— unchecking revokes it immediately</span>
        </label>
      )}
      <FormActions onCancel={closeDrawer}>
        <button type="submit" className="btn primary" disabled={save.isPending}>
          {isEdit ? 'Save' : 'Create key'}
        </button>
      </FormActions>
    </form>
  );
}

/** Shown once, right after creation - the secret is never retrievable again after this. */
export function SecretReveal({
  secret,
  label = 'Secret key',
  hint = 'Store this now — it cannot be shown again. To rotate it, revoke this key and create a new one.',
  onDone,
}: {
  secret: string;
  label?: string;
  hint?: string;
  /** What Done does - closes the drawer unless given. */
  onDone?: () => void;
}) {
  const { closeDrawer, toast } = useFeedback();
  return (
    <div className="form">
      <div className="field">
        <label htmlFor="k-secret">{label}</label>
        <input
          id="k-secret"
          readOnly
          className="mono"
          value={secret}
          onClick={(e) => e.currentTarget.select()}
        />
        <div className="hint">{hint}</div>
      </div>
      <div className="form-actions">
        <button type="button" className="btn ghost" onClick={() => copyText(secret, toast)}>
          Copy
        </button>
        <button type="button" className="btn primary" onClick={onDone ?? closeDrawer}>
          Done
        </button>
      </div>
    </div>
  );
}

export function useRevokeKey() {
  const { toast } = useFeedback();
  const reportError = useReportError();
  const invalidate = useInvalidateAccess();
  return async (name: string) => {
    if (!window.confirm(`Revoke API key '${name}'? Anything still using it will stop working immediately.`))
      return;
    try {
      unwrapEmpty(await api.DELETE('/api/v1/api-keys/{name}', { params: { path: { name } } }));
      toast('Revoked ' + name);
      invalidate();
    } catch (error) {
      reportError(error, 'API key');
    }
  };
}

// ---- roles ----

export function RoleForm({ name }: { name?: string }) {
  const detail = useDetail<Role>('roles', name);
  if (name && detail.isPending) return <Loading />;
  if (name && detail.isError) return <div className="hint">{detail.error.message}</div>;
  return (
    <RoleFormFields
      key={name ?? ''}
      name={name}
      existing={detail.data?.data}
      etag={detail.data?.etag ?? null}
    />
  );
}

function RoleFormFields({ name, existing, etag }: { name?: string; existing?: Role; etag: string | null }) {
  const isEdit = Boolean(name);
  const { closeDrawer, showError, toast } = useFeedback();
  const reportError = useReportError();
  const invalidate = useInvalidateAccess();
  const { connectionNames, queryNames } = useGrantChoices();
  const [roleName, setRoleName] = useState(name ?? '');
  const [grants, setGrants] = useState<GrantState>(() => initialGrants(existing));

  const save = useMutation({
    mutationFn: async () => {
      const body = grantsPayload(grants, connectionNames, queryNames);
      if (isEdit) {
        return unwrap(
          await api.PATCH('/api/v1/roles/{name}', {
            params: { path: { name: name! }, header: etag ? { 'If-Match': etag } : {} },
            body,
          }),
        );
      }
      return unwrap(await api.POST('/api/v1/roles', { body: { name: roleName.trim(), ...body } }));
    },
    onSuccess: (saved) => {
      showError('');
      closeDrawer();
      toast((isEdit ? 'Updated ' : 'Created role ') + saved.name);
      invalidate();
    },
    onError: (error) => reportError(error, 'role'),
  });

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!isEdit && !roleName.trim()) {
      showError('A role name is required.', { errors: { name: 'This field is required' } });
      document.getElementById('r-name')?.focus();
      return;
    }
    save.mutate();
  };

  return (
    <form className="form" noValidate onSubmit={submit}>
      <Field id="r-name" label="Name" hint={isEdit ? undefined : NAME_HINT}>
        <input
          id="r-name"
          value={roleName}
          placeholder="reporting"
          autoComplete="off"
          spellCheck={false}
          required={!isEdit}
          disabled={isEdit}
          autoFocus
          onChange={(e) => setRoleName(e.target.value)}
        />
      </Field>
      <GrantFields prefix="r" state={grants} onChange={setGrants} />
      <div className="hint">
        A role is a template: it’s copied onto a key once, when the key is created &quot;from&quot; it.
        Editing or deleting this role afterward never changes a key already created from it.
      </div>
      <FormActions onCancel={closeDrawer}>
        <button type="submit" className="btn primary" disabled={save.isPending}>
          {isEdit ? 'Save' : 'Create role'}
        </button>
      </FormActions>
    </form>
  );
}

export function useDeleteRole() {
  const { toast } = useFeedback();
  const reportError = useReportError();
  const invalidate = useInvalidateAccess();
  return async (name: string) => {
    if (
      !window.confirm(
        `Delete role '${name}'? Keys already created from it are unaffected — this only removes the template.`,
      )
    )
      return;
    try {
      unwrapEmpty(await api.DELETE('/api/v1/roles/{name}', { params: { path: { name } } }));
      toast('Deleted role ' + name);
      invalidate();
    } catch (error) {
      reportError(error, 'role');
    }
  };
}

/** Drawer openers, shared with the API Repository's API Keys and Roles tabs. */
export function useAccessDrawers() {
  const { openDrawer } = useFeedback();
  return {
    newKey: (fromRole?: string) =>
      openDrawer({
        title: 'New API key',
        kicker: 'POST /api/v1/api-keys',
        content: <ApiKeyForm key={'new:' + (fromRole ?? '')} fromRole={fromRole} />,
      }),
    editKey: (name: string) =>
      openDrawer({ title: 'Edit API key', kicker: name, content: <ApiKeyForm key={name} name={name} /> }),
    newRole: () =>
      openDrawer({ title: 'New role', kicker: 'POST /api/v1/roles', content: <RoleForm key="new" /> }),
    editRole: (name: string) =>
      openDrawer({ title: 'Edit role', kicker: name, content: <RoleForm key={name} name={name} /> }),
  };
}

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState, type FormEvent, type ReactNode } from 'react';

import { ApiError, api, unwrap, unwrapEmpty, unwrapWithEtag, type Schemas } from '@/api/client';
import { useRoles } from '@/app/data';
import { copyText, Field, FormActions, Loading, useFeedback } from '@/app/feedback';
import { Time } from '@/components/Time';

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
      {isEdit && existing && <KeySecret apiKey={existing} />}
      <FormActions onCancel={closeDrawer}>
        <button type="submit" className="btn primary" disabled={save.isPending}>
          {isEdit ? 'Save' : 'Create key'}
        </button>
      </FormActions>
    </form>
  );
}

const GRACE_CHOICES: [string, string][] = [
  ['60', 'for 1 hour'],
  ['1440', 'for 1 day'],
  ['10080', 'for 1 week'],
  ['0', 'not at all - it stops now'],
];

/** Rotating a key's secret (ADR 0009): a new one, shown once, and the old one kept working for a while - or, while
 * an old one still works, when it stops and whether it is still being used. */
function KeySecret({ apiKey }: { apiKey: ApiKey }) {
  const { openDrawer, toast } = useFeedback();
  const reportError = useReportError();
  const invalidate = useInvalidateAccess();
  const [grace, setGrace] = useState('60');
  const path = { params: { path: { name: apiKey.name } } };
  const rotate = useMutation({
    mutationFn: async () =>
      unwrap(
        await api.POST('/api/v1/api-keys/{name}/rotate', { ...path, body: { grace_minutes: Number(grace) } }),
      ),
    onSuccess: (rotated) => {
      invalidate();
      openDrawer({
        title: 'Secret rotated',
        kicker: rotated.name,
        content: (
          <SecretReveal
            secret={rotated.secret}
            label="New secret"
            hint={
              rotated.previous_expires_at ? (
                <>
                  Store this now — it cannot be shown again. The old secret keeps working until{' '}
                  <Time value={rotated.previous_expires_at} />: switch whoever uses this key before then.
                </>
              ) : (
                'Store this now — it cannot be shown again. The old secret has stopped working.'
              )
            }
          />
        ),
      });
    },
    onError: (error) => reportError(error, 'API key'),
  });
  const end = useMutation({
    mutationFn: async () => unwrap(await api.DELETE('/api/v1/api-keys/{name}/previous', path)),
    onSuccess: () => {
      invalidate();
      toast('The old secret of ' + apiKey.name + ' stopped working');
    },
    onError: (error) => reportError(error, 'API key'),
  });

  if (apiKey.previous_expires_at) {
    return (
      <Field label="Secret" hint="Rotated: the new secret and the old one both work until then.">
        <div className="key-secret" id="k-previous">
          <span>
            The old secret works until <Time value={apiKey.previous_expires_at} /> · last used{' '}
            <Time value={apiKey.previous_last_used_at} fallback="not since the rotation" />
          </span>
          <button
            type="button"
            className="btn sm danger"
            disabled={end.isPending}
            onClick={() => {
              if (
                window.confirm(`Stop the old secret of '${apiKey.name}' now? Anything still using it fails.`)
              )
                end.mutate();
            }}
          >
            End now
          </button>
        </div>
      </Field>
    );
  }
  return (
    <Field
      id="k-grace"
      label="Secret"
      hint="Rotate: a new secret, shown once. Name, grants and history stay the same."
    >
      <div className="key-secret">
        <span>Keep the old secret working</span>
        <select id="k-grace" value={grace} onChange={(e) => setGrace(e.target.value)}>
          {GRACE_CHOICES.map(([value, label]) => (
            <option key={value} value={value}>
              {label}
            </option>
          ))}
        </select>
        <button type="button" className="btn sm" disabled={rotate.isPending} onClick={() => rotate.mutate()}>
          Rotate secret
        </button>
      </div>
    </Field>
  );
}

/** Shown once, right after creation - the secret is never retrievable again after this. */
export function SecretReveal({
  secret,
  label = 'Secret key',
  hint = 'Store this now — it cannot be shown again. To change it later, rotate it from Edit.',
  onDone,
}: {
  secret: string;
  label?: string;
  hint?: ReactNode;
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
  const [claimBound, setClaimBound] = useState(Boolean(existing?.require_claim_binding));

  const save = useMutation({
    mutationFn: async () => {
      const body = {
        ...grantsPayload(grants, connectionNames, queryNames),
        require_claim_binding: claimBound,
        // a claim-bound role reaches queries one by one, never a connection (the server refuses any other value)
        ...(claimBound ? { connections: [] } : {}),
      };
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
      <label className="switch">
        <input
          id="r-claim-bound"
          type="checkbox"
          checked={claimBound}
          onChange={(e) => setClaimBound(e.target.checked)}
        />
        Only queries that bind the signed-in user
        <span className="hint">
          — for a role your customers sign in with: its users run only saved queries that take a parameter
          from their token (<code>from_claim</code>), so each sees only their own rows
        </span>
      </label>
      {claimBound && (
        <div className="hint" id="r-claim-bound-note">
          Its users reach the queries and collections chosen above, one by one — never a connection, so no
          ad-hoc SQL. The connections above aren’t used, and are saved as none. API keys created from this
          role don’t take this setting.
        </div>
      )}
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

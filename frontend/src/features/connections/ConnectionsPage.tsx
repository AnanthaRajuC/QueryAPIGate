import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState, type FormEvent } from 'react';
import { useNavigate } from 'react-router';

import { ApiError, api, unwrap, unwrapEmpty, unwrapWithEtag, type Schemas } from '@/api/client';
import { Empty, Field, FormActions, Loading, useFeedback } from '@/app/feedback';
import { PencilIcon, TrashIcon } from '@/components/icons';
import { Time } from '@/components/Time';

// The classic Connections screen (ui.py #tab-connections, renderConnections, openConnectionForm,
// openDeleteConnectionForm): All / Active / Inactive / Deleted, a filter, the table with each connection's live
// usage, and the New / Edit / Delete drawers - on /api/v1/connections.

type Connection = Schemas['Connection'];
type ConnectionInput = Schemas['ConnectionInput'];

const DB_TYPES = ['mysql', 'postgres', 'clickhouse', 'sqlite', 'h2', 'duckdb', 'mongo'];
// Outside the compatibility promise (queryapigate/experimental.py) - labelled where an admin picks one.
const EXPERIMENTAL_DB_TYPES = ['h2', 'jdbc', 'mongo'];
const DB_SWITCHABLE_TYPES = ['mysql', 'postgres', 'clickhouse', 'mongo'];

type Filter = 'all' | 'active' | 'inactive' | 'deleted';

function usageText(usage: Connection['usage']) {
  if (!usage || !usage.queries) return null;
  const parts = [usage.queries + (usage.queries === 1 ? ' query' : ' queries')];
  if (usage.errors) parts.push(usage.errors + ' failed');
  if (usage.avg_duration_ms !== null && usage.avg_duration_ms !== undefined)
    parts.push(usage.avg_duration_ms + 'ms avg');
  return parts.join(' · ');
}

function endpoint(c: { host?: string | null; port?: number | string | null }) {
  return c.host ? c.host + (c.port ? ':' + c.port : '') : '';
}

function useConnectionList() {
  return useQuery({
    queryKey: ['connections'],
    queryFn: async () => unwrap(await api.GET('/api/v1/connections')).items,
    retry: false,
  });
}

function useDeleted(enabled: boolean) {
  return useQuery({
    queryKey: ['connections', 'deleted'],
    queryFn: async () => unwrap(await api.GET('/api/v1/connections/deleted')).items,
    enabled,
    retry: false,
  });
}

export function ConnectionsPage() {
  const navigate = useNavigate();
  const { openDrawer } = useFeedback();
  const list = useConnectionList();
  const [filter, setFilter] = useState<Filter>('all');
  const [q, setQ] = useState('');
  const deleted = useDeleted(true);
  const all = list.data ?? [];
  const activeCount = all.filter((c) => c.active).length;
  const deletedItems = deleted.data ?? [];
  const needle = q.trim().toLowerCase();
  const newConnection = () =>
    openDrawer({ title: 'New connection', kicker: 'POST /api/v1/connections', content: <ConnectionForm /> });

  let body: React.ReactNode;
  let foot: React.ReactNode = null;
  if (filter === 'deleted') {
    const shown = deletedItems.filter(
      (e) =>
        !needle || [e.name, e.deleted_by, e.reason, e.db, e.host].join(' ').toLowerCase().includes(needle),
    );
    if (!deletedItems.length) {
      body = (
        <Empty title="No deleted connections">
          Every connection deletion is recorded here, with who did it, when, and why.
        </Empty>
      );
    } else {
      foot = (
        <span>{`Showing ${shown.length} of ${deletedItems.length}${deletedItems.length === 1 ? ' deleted connection' : ' deleted connections'}`}</span>
      );
      body = !shown.length ? (
        <Empty>{`No deleted connections match “${needle}”.`}</Empty>
      ) : (
        <div style={{ overflowX: 'auto' }}>
          <table className="grid">
            <thead>
              <tr>
                {['Name', 'Type', 'Host', 'Database', 'Deleted at', 'Deleted by', 'Reason'].map((t) => (
                  <th key={t}>{t}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {shown.map((e, i) => (
                <tr key={`${e.name}-${i}`}>
                  <td>
                    <span className="name">{e.name}</span>
                  </td>
                  <td>
                    <span className="tag">{e.db || '?'}</span>
                  </td>
                  <td className="mono" style={{ whiteSpace: 'nowrap' }}>
                    {endpoint(e) || <span className="dim">—</span>}
                  </td>
                  <td className="mono">{e.database || ''}</td>
                  <td className="mono dim" style={{ whiteSpace: 'nowrap' }}>
                    <Time value={e.deleted_at} />
                  </td>
                  <td className="mono">{e.deleted_by || '—'}</td>
                  <td>{e.reason || '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      );
    }
  } else if (list.isPending) {
    body = <Loading text="Loading connections…" />;
  } else if (list.isError) {
    body = (
      <Empty title="Couldn’t load connections">
        <button type="button" className="btn sm" onClick={() => list.refetch()}>
          Retry
        </button>
      </Empty>
    );
  } else if (!all.length) {
    body = (
      <div className="empty">
        <strong>No connections yet</strong>
        <span>Add a MySQL, PostgreSQL, ClickHouse, SQLite or H2 database to start running SQL.</span>
        <button type="button" className="btn primary" onClick={newConnection}>
          New connection
        </button>
      </div>
    );
  } else {
    const shown = all.filter((c) => {
      if (filter === 'active' && !c.active) return false;
      if (filter === 'inactive' && c.active) return false;
      return !needle || [c.name, c.db, c.host, c.database, c.user].join(' ').toLowerCase().includes(needle);
    });
    foot = (
      <span>{`Showing ${shown.length} of ${all.length}${all.length === 1 ? ' connection' : ' connections'}`}</span>
    );
    body = !shown.length ? (
      <Empty>{needle ? `No connections match “${needle}”.` : `No ${filter} connections.`}</Empty>
    ) : (
      <div style={{ overflowX: 'auto' }}>
        <table className="grid">
          <thead>
            <tr>
              {[
                'Name',
                'Type',
                'Host',
                'Database',
                'User',
                'Status',
                'Usage',
                'Created',
                'Last modified',
                '',
              ].map((t, i) => (
                <th key={i}>{t}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {shown.map((c) => {
              const usage = usageText(c.usage);
              return (
                <tr key={c.name} data-name={c.name}>
                  <td>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                      <span className="name">{c.name}</span>
                      {c.example && (
                        <span
                          className="tag example"
                          title="Installed by the example APIs — removing the examples removes it"
                        >
                          example
                        </span>
                      )}
                    </div>
                  </td>
                  <td>
                    <span className="tag">{c.db || '?'}</span>
                  </td>
                  <td className="mono" style={{ whiteSpace: 'nowrap' }}>
                    {endpoint(c) || <span className="dim">—</span>}
                  </td>
                  <td className="mono">{c.database || ''}</td>
                  <td className="mono dim">{c.user || '—'}</td>
                  <td>
                    <span className={`pill ${c.active ? 'ok' : 'off'}`}>
                      <i />
                      {c.active ? 'Active' : 'Inactive'}
                    </span>
                  </td>
                  {usage ? (
                    <td
                      title={`${c.usage.rows}${c.usage.rows === 1 ? ' row' : ' rows'} returned in total · since this process started`}
                      style={{ whiteSpace: 'nowrap' }}
                    >
                      {usage}
                    </td>
                  ) : (
                    <td className="dim" style={{ whiteSpace: 'nowrap' }}>
                      No activity yet
                    </td>
                  )}
                  <td className="mono dim" style={{ whiteSpace: 'nowrap' }}>
                    <Time value={c.created_at} />
                  </td>
                  <td className="mono dim" style={{ whiteSpace: 'nowrap' }}>
                    <Time value={c.updated_at} />
                  </td>
                  <td>
                    <div className="actions">
                      <button
                        type="button"
                        className="btn sm outlined"
                        disabled={!c.active}
                        title="Open in API Designer"
                        onClick={() => navigate('/designer', { state: { select: c.name } })}
                      >
                        Query
                      </button>
                      <button
                        type="button"
                        className="btn ghost sm icon"
                        title={`Edit ${c.name}`}
                        aria-label="Edit"
                        onClick={() =>
                          openDrawer({
                            title: 'Edit connection',
                            kicker: c.name,
                            content: <ConnectionForm name={c.name} />,
                          })
                        }
                      >
                        <PencilIcon />
                      </button>
                      <button
                        type="button"
                        className="btn ghost sm icon danger"
                        title={`Delete ${c.name}`}
                        aria-label="Delete"
                        onClick={() =>
                          openDrawer({
                            title: 'Delete connection',
                            kicker: c.name,
                            content: <DeleteConnectionForm name={c.name} />,
                          })
                        }
                      >
                        <TrashIcon />
                      </button>
                    </div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    );
  }

  const segments: [Filter, string, number][] = [
    ['all', 'All', all.length],
    ['active', 'Active', activeCount],
    ['inactive', 'Inactive', all.length - activeCount],
    ['deleted', 'Deleted', deletedItems.length],
  ];
  return (
    <>
      <div className="page-head">
        <div className="titles">
          <h1>Connections</h1>
          <span className="sub">Databases this gateway can run saved queries against.</span>
        </div>
        <span className="spacer" />
        <button
          id="new-connection"
          type="button"
          className="btn primary"
          hidden={filter === 'deleted'}
          onClick={newConnection}
        >
          New connection
        </button>
      </div>
      <div className="panel" style={{ overflow: 'hidden' }}>
        <div className="panel-bar">
          <div className="seg" id="conn-tabs" role="group" aria-label="Connection status">
            {segments.map(([id, label, n]) => (
              <button
                key={id}
                type="button"
                className={filter === id ? 'on' : ''}
                onClick={() => setFilter(id)}
              >
                {label}
                <span className="n">{n}</span>
              </button>
            ))}
          </div>
          <span className="spacer" />
          <input
            id="conn-filter"
            className="search"
            type="search"
            placeholder={
              filter === 'deleted' ? 'Filter by name, actor, reason…' : 'Filter by name, type, host…'
            }
            value={q}
            onChange={(e) => setQ(e.target.value)}
          />
        </div>
        <div id="connections-table">{body}</div>
        <div className="panel-foot" id="connections-foot">
          {foot}
        </div>
      </div>
    </>
  );
}

function reportError(showError: ReturnType<typeof useFeedback>['showError'], error: unknown) {
  if (error instanceof ApiError) {
    if (error.code === 'precondition_failed') {
      showError('This connection changed since you opened the form - close it, check it and try again.', {
        status: 412,
      });
    } else {
      showError(error.message, { status: error.status });
    }
  } else {
    showError((error as Error).message);
  }
}

/** New / Edit connection (ui.py openConnectionForm): same fields, Load databases…, Test connection. */
export function ConnectionForm({ name }: { name?: string }) {
  const isEdit = Boolean(name);
  const client = useQueryClient();
  const { closeDrawer, showError, toast } = useFeedback();
  const existing = useQuery({
    queryKey: ['connections', 'detail', name],
    queryFn: async () =>
      unwrapWithEtag(await api.GET('/api/v1/connections/{name}', { params: { path: { name: name! } } })),
    enabled: isEdit,
    retry: false,
  });
  if (isEdit && existing.isPending) return <Loading />;
  if (isEdit && existing.isError) return <div className="hint">{existing.error.message}</div>;
  return (
    <ConnectionFormFields
      name={name}
      existing={existing.data?.data}
      etag={existing.data?.etag ?? null}
      onSaved={(saved) => {
        showError('');
        closeDrawer();
        toast((isEdit ? 'Saved ' : 'Created ') + saved);
        void client.invalidateQueries({ queryKey: ['connections'] });
      }}
    />
  );
}

function ConnectionFormFields({
  name,
  existing,
  etag,
  onSaved,
}: {
  name?: string;
  existing?: Schemas['ConnectionDetail'];
  etag: string | null;
  onSaved: (name: string) => void;
}) {
  const isEdit = Boolean(name);
  const { closeDrawer, showError } = useFeedback();
  const [connName, setConnName] = useState(name ?? '');
  const [db, setDb] = useState(existing?.db ?? 'mysql');
  const [host, setHost] = useState(existing?.host ?? '');
  const [port, setPort] = useState(existing?.port != null ? String(existing.port) : '');
  const [user, setUser] = useState(existing?.user ?? '');
  const [password, setPassword] = useState(isEdit ? (existing?.password ?? '') : '');
  const [showPassword, setShowPassword] = useState(false);
  const [database, setDatabase] = useState(existing?.database ?? '');
  const [active, setActive] = useState(existing ? existing.active : true);
  const [databases, setDatabases] = useState<string[] | null>(null);
  const [dbResult, setDbResult] = useState<{ ok: boolean; text: string } | null>(null);
  const [testResult, setTestResult] = useState<{ ok: boolean; text: string } | null>(null);
  const [autoLoaded, setAutoLoaded] = useState(false);
  // DuckDB's file access (BACKLOG #75): which files and URLs it may read, object-storage settings, views over files.
  const opts = (existing?.options ?? {}) as Record<string, unknown>;
  const [paths, setPaths] = useState(((opts.allowed_paths as string[] | undefined) ?? []).join('\n'));
  const [storage, setStorage] = useState((opts.storage as string | undefined) ?? 's3');
  const [region, setRegion] = useState((opts.region as string | undefined) ?? '');
  const [endpoint, setEndpoint] = useState((opts.endpoint as string | undefined) ?? '');
  const [urlStyle, setUrlStyle] = useState((opts.url_style as string | undefined) ?? '');
  const [useSsl, setUseSsl] = useState(opts.use_ssl !== false);
  const [views, setViews] = useState(opts.views ? JSON.stringify(opts.views, null, 2) : '');
  const isDuck = db === 'duckdb';

  /** DuckDB's file-access fields; an emptied one as `empty` (undefined to leave out, null to remove on edit). */
  const duckFields = (empty: null | undefined) => {
    if (!isDuck) return {};
    const list = paths
      .split('\n')
      .map((p) => p.trim())
      .filter(Boolean);
    let parsedViews: unknown = empty;
    if (views.trim()) {
      try {
        parsedViews = JSON.parse(views);
      } catch {
        throw new Error('Views must be a JSON object of {"name": "SELECT ..."}.');
      }
    }
    return {
      allowed_paths: list.length ? list : empty,
      storage: storage === 's3' ? empty : storage,
      region: region.trim() || empty,
      endpoint: endpoint.trim() || empty,
      url_style: urlStyle || empty,
      use_ssl: useSsl ? empty : false,
      views: parsedViews,
    };
  };

  const details = (): ConnectionInput => ({
    db,
    host: host || undefined,
    port: port ? Number(port) : undefined,
    user: user || undefined,
    password,
    database: database || undefined,
    ...(isEdit ? { name } : {}),
    ...duckFields(undefined),
  });

  const probe = useMutation({
    mutationFn: async (kind: 'test' | 'databases') => {
      const path = kind === 'test' ? '/api/v1/connections/test' : '/api/v1/connections/databases';
      const result = await api.POST(path, { body: details() });
      return { kind, data: unwrap(result as { data?: unknown; error?: unknown; response: Response }) };
    },
  });
  const probeError = (error: unknown) =>
    '✗ ' +
    (error instanceof ApiError ? error.message + (error.detail ? ': ' + error.detail : '') : 'Network error');
  const loadDatabases = () => {
    setDbResult(null);
    probe.mutate('databases', {
      onSuccess: ({ data }) => {
        const list = (data as { databases: string[] }).databases;
        setDatabases(list);
        setDbResult({ ok: true, text: `✓ ${list.length}${list.length === 1 ? ' database' : ' databases'}` });
      },
      onError: (e) => setDbResult({ ok: false, text: probeError(e) }),
    });
  };
  const test = () => {
    setTestResult(null);
    probe.mutate('test', {
      onSuccess: ({ data }) => {
        const ms = (data as { elapsed_ms?: number }).elapsed_ms;
        setTestResult({ ok: true, text: '✓ Connected' + (ms !== undefined ? ` (${ms} ms)` : '') });
      },
      onError: (e) => setTestResult({ ok: false, text: probeError(e) }),
    });
  };
  const switchable = DB_SWITCHABLE_TYPES.includes(db);
  // Editing a saved connection has real credentials already, so its databases load once, on their own.
  if (isEdit && switchable && !autoLoaded) {
    setAutoLoaded(true);
    setTimeout(loadDatabases, 0);
  }

  const save = useMutation({
    mutationFn: async () => {
      if (isEdit) {
        // Every form field is sent: an emptied one as null, so it is removed rather than kept.
        const body: ConnectionInput = {
          db,
          active,
          host: host || null,
          port: port ? Number(port) : null,
          user: user || null,
          password,
          database: database || null,
          ...duckFields(null),
        };
        unwrap(
          await api.PATCH('/api/v1/connections/{name}', {
            params: { path: { name: name! }, header: etag ? { 'If-Match': etag } : undefined },
            body,
          }),
        );
        return name!;
      }
      const body: ConnectionInput = { ...details(), name: connName.trim(), active };
      return unwrap(await api.POST('/api/v1/connections', { body })).name;
    },
    onSuccess: onSaved,
    onError: (e) => (e instanceof ApiError ? reportError(showError, e) : showError((e as Error).message)),
  });

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!isEdit && !connName.trim()) {
      showError('A connection name is required.', { errors: { name: 'This field is required' } });
      return;
    }
    save.mutate();
  };

  return (
    <form className="form" noValidate onSubmit={submit}>
      <Field
        id="c-name"
        label="Name"
        hint={
          isEdit
            ? 'Renaming isn’t supported — create a new connection instead.'
            : 'Referenced as connection_name by queries and the API.'
        }
      >
        <input
          id="c-name"
          placeholder="reporting-db"
          autoComplete="off"
          spellCheck={false}
          disabled={isEdit}
          autoFocus={!isEdit}
          value={connName}
          onChange={(e) => setConnName(e.target.value)}
        />
      </Field>
      <Field id="c-db" label="Database type">
        <select id="c-db" value={db} onChange={(e) => setDb(e.target.value)}>
          {DB_TYPES.map((t) => (
            <option key={t} value={t}>
              {EXPERIMENTAL_DB_TYPES.includes(t) ? `${t} (experimental)` : t}
            </option>
          ))}
        </select>
      </Field>
      <div className="grid-host" hidden={isDuck}>
        <Field id="c-host" label="Host">
          <input
            id="c-host"
            placeholder="localhost"
            autoComplete="off"
            spellCheck={false}
            autoFocus={isEdit}
            value={host}
            onChange={(e) => setHost(e.target.value)}
          />
        </Field>
        <Field id="c-port" label="Port">
          <input
            id="c-port"
            type="number"
            autoComplete="off"
            value={port}
            onChange={(e) => setPort(e.target.value)}
          />
        </Field>
      </div>
      <div className="grid2">
        <Field
          id="c-user"
          label={isDuck ? 'Access key ID' : 'User'}
          hint={isDuck ? 'For object storage; leave empty for public files.' : undefined}
        >
          <input
            id="c-user"
            autoComplete="off"
            spellCheck={false}
            value={user}
            onChange={(e) => setUser(e.target.value)}
          />
        </Field>
        <Field
          id="c-password"
          label={isDuck ? 'Secret access key' : 'Password'}
          hint={
            isEdit
              ? 'Leave the mask to keep the stored password.'
              : '${ENV_VAR} references are resolved on the server.'
          }
        >
          <div className="password-field">
            <input
              id="c-password"
              type={showPassword ? 'text' : 'password'}
              autoComplete="off"
              spellCheck={false}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
            <button
              type="button"
              className="btn ghost sm"
              aria-label={(showPassword ? 'Hide' : 'Show') + ' password'}
              onClick={() => setShowPassword((s) => !s)}
            >
              {showPassword ? 'Hide' : 'Show'}
            </button>
          </div>
        </Field>
      </div>
      <Field
        id="c-database"
        label="Default database"
        hint={
          isDuck
            ? 'A .duckdb file that already exists, or :memory: for a source made only of files.'
            : 'SQLite, DuckDB and H2 take a file path here instead.'
        }
      >
        <div>
          {databases ? (
            <select
              id="c-database"
              value={databases.includes(database) ? database : (databases[0] ?? '')}
              onChange={(e) => setDatabase(e.target.value)}
            >
              {databases.map((d) => (
                <option key={d} value={d}>
                  {d}
                </option>
              ))}
            </select>
          ) : (
            <input
              id="c-database"
              placeholder="SQLite, DuckDB and H2 take a file path"
              autoComplete="off"
              spellCheck={false}
              value={database}
              onChange={(e) => setDatabase(e.target.value)}
            />
          )}
        </div>
      </Field>
      <div className="test-row">
        <button
          type="button"
          className="btn sm ghost"
          hidden={!switchable}
          disabled={probe.isPending}
          onClick={loadDatabases}
        >
          {probe.isPending && probe.variables === 'databases' ? 'Loading…' : 'Load databases…'}
        </button>
        <span className="test-result">
          {dbResult && <span className={dbResult.ok ? 'test-ok' : 'test-fail'}>{dbResult.text}</span>}
        </span>
      </div>
      {isDuck && (
        <fieldset className="duck-files">
          <legend>Files it may read</legend>
          <Field
            id="c-paths"
            label="Allowed paths"
            hint="One per line. Folders and buckets end in / (s3://sales/2026/, /data/files/); a web address must name one file. Nothing else is readable - an empty list means no files at all."
          >
            <textarea
              id="c-paths"
              rows={3}
              spellCheck={false}
              placeholder={'s3://sales/2026/\nhttps://data.example.com/prices.parquet'}
              value={paths}
              onChange={(e) => setPaths(e.target.value)}
            />
          </Field>
          <div className="grid2">
            <Field id="c-storage" label="Object storage">
              <select id="c-storage" value={storage} onChange={(e) => setStorage(e.target.value)}>
                <option value="s3">S3 or S3-compatible</option>
                <option value="gcs">Google Cloud Storage</option>
                <option value="r2">Cloudflare R2</option>
              </select>
            </Field>
            <Field id="c-region" label="Region">
              <input
                id="c-region"
                placeholder="us-east-1"
                spellCheck={false}
                value={region}
                onChange={(e) => setRegion(e.target.value)}
              />
            </Field>
          </div>
          <div className="grid2">
            <Field
              id="c-endpoint"
              label="Endpoint"
              hint="Only for an S3-compatible service, e.g. minio.internal:9000."
            >
              <input
                id="c-endpoint"
                spellCheck={false}
                value={endpoint}
                onChange={(e) => setEndpoint(e.target.value)}
              />
            </Field>
            <Field id="c-url-style" label="URL style">
              <select id="c-url-style" value={urlStyle} onChange={(e) => setUrlStyle(e.target.value)}>
                <option value="">Default</option>
                <option value="vhost">Virtual-hosted (bucket.host)</option>
                <option value="path">Path (host/bucket)</option>
              </select>
            </Field>
          </div>
          <label className="switch">
            <input
              id="c-ssl"
              type="checkbox"
              checked={useSsl}
              onChange={(e) => setUseSsl(e.target.checked)}
            />
            Use HTTPS for object storage
          </label>
          <Field
            id="c-views"
            label="Views"
            hint={`Optional JSON: {"orders": "SELECT * FROM read_parquet('s3://sales/2026/*.parquet')"}. They appear in the schema browser and can be granted with allowed_tables.`}
          >
            <textarea
              id="c-views"
              rows={3}
              spellCheck={false}
              value={views}
              onChange={(e) => setViews(e.target.value)}
            />
          </Field>
        </fieldset>
      )}
      <label className="switch">
        <input id="c-active" type="checkbox" checked={active} onChange={(e) => setActive(e.target.checked)} />
        Active
        <span className="hint">— inactive connections refuse queries</span>
      </label>
      <div className="test-row">
        <button type="button" className="btn" disabled={probe.isPending} onClick={test}>
          {probe.isPending && probe.variables === 'test' ? 'Testing…' : 'Test connection'}
        </button>
        <span className="test-result">
          {testResult && <span className={testResult.ok ? 'test-ok' : 'test-fail'}>{testResult.text}</span>}
        </span>
      </div>
      {isEdit && existing?.created_at && (
        <div className="hint">
          {'Created ' +
            existing.created_at +
            (existing.updated_at && existing.updated_at !== existing.created_at
              ? ' · last edited ' + existing.updated_at
              : '')}
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

/** Type the connection's name back, and say why, before Delete enables (ui.py openDeleteConnectionForm). */
function DeleteConnectionForm({ name }: { name: string }) {
  const client = useQueryClient();
  const { closeDrawer, showError, toast } = useFeedback();
  const [reason, setReason] = useState('');
  const [confirm, setConfirm] = useState('');
  const remove = useMutation({
    mutationFn: async () =>
      unwrapEmpty(
        await api.DELETE('/api/v1/connections/{name}', {
          params: { path: { name } },
          body: { reason: reason.trim() },
        }),
      ),
    onSuccess: () => {
      closeDrawer();
      toast('Deleted ' + name);
      void client.invalidateQueries({ queryKey: ['connections'] });
    },
    onError: (e) => reportError(showError, e),
  });
  const ready = Boolean(reason.trim()) && confirm === name;
  return (
    <form
      className="form"
      noValidate
      onSubmit={(e) => {
        e.preventDefault();
        if (ready) remove.mutate();
      }}
    >
      <p className="d-desc">
        This deletes <span className="name">{name}</span> permanently. Every saved query that uses it will
        stop working. It moves to the Deleted tab, with the reason and who deleted it.
      </p>
      <Field id="del-reason" label="Reason" hint="Recorded in the audit log - required.">
        <textarea
          id="del-reason"
          placeholder="Why is this connection being deleted?"
          autoFocus
          value={reason}
          onChange={(e) => setReason(e.target.value)}
        />
      </Field>
      <Field id="del-confirm" label={`Type "${name}" to confirm`}>
        <input
          id="del-confirm"
          type="text"
          autoComplete="off"
          spellCheck={false}
          placeholder={name}
          value={confirm}
          onChange={(e) => setConfirm(e.target.value)}
        />
      </Field>
      <FormActions onCancel={closeDrawer}>
        <button type="submit" className="btn danger" disabled={!ready || remove.isPending}>
          Delete
        </button>
      </FormActions>
    </form>
  );
}

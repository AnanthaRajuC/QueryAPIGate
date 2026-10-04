import { useLayoutEffect, useMemo, useRef, useState, type FormEvent, type ReactElement } from 'react';
import { useNavigate } from 'react-router';

import { timedFetch } from '@/api/client';
import type { ApiKeyEntry, RoleEntry } from '@/app/data';
import { useConnections } from '@/app/data';
import { copyText, Empty, Field, Loading, useFeedback } from '@/app/feedback';
import { preferredPageSize, readPrefs } from '@/app/prefs';
import { CodeBox } from '@/components/CodeBox';
import { readResult, Results, type ResultData } from '@/components/Results';
import { formatSql, PARSEABLE_DIALECTS, shQuote } from '@/lib/sql';

import {
  useQueryActions,
  useQueryFlow,
  type HistoryEntry,
  type ParameterRule,
  type Query,
  type QueryVersion,
} from './api';
import { useAccessDrawers } from '@/features/access/forms';
import { presetQuery } from '@/features/accessmap/state';

import { AccessCell, AccessPill, ReachDot, type Reach } from './reach';
import { Time } from '@/components/Time';

// The saved-query detail subtabs, each ported from its classic renderer (ui.py renderRunTab, renderSqlTab,
// renderHistoryTab, renderCurlTab, renderQueryKeysTab, renderQueryRolesTab, the Access box + flow diagram,
// renderCacheTab, renderQueryMetricsTab, renderCliTab), reading the Management API instead of the legacy routes.

const preferredFormat = () => readPrefs().format;

/** A parameter's declared shape as the classic tables show it: type, constraints (key=value) and description. */
function declared(rule: ParameterRule | undefined) {
  if (!rule) return { type: '', constraints: '', description: '' };
  const { type, description, required, ...rest } = rule;
  const parts = Object.entries(rest).map(([k, v]) => `${k}=${JSON.stringify(v)}`);
  if (required === false && !('default' in rest)) parts.unshift('required=false');
  return { type: type ?? '', constraints: parts.join('  '), description: description ?? '' };
}

/** Parameters callers supply - every placeholder except those bound from the caller's token (from_claim). */
function callerParams(v: QueryVersion) {
  return v.placeholders.filter((p) => !v.parameters[p]?.from_claim);
}

/** ?version= is needed for anything but the published version. */
function versionSuffix(query: Query, v: QueryVersion) {
  return v.version === query.published_version ? null : v.version;
}

// ---- Run ----

export function RunTab({ query, v }: { query: Query; v: QueryVersion }) {
  const connections = useConnections();
  const { showError } = useFeedback();
  const params = callerParams(v);
  const [values, setValues] = useState<Record<string, string>>({});
  const [connection, setConnection] = useState('');
  const [format, setFormat] = useState(preferredFormat);
  const [size, setSize] = useState(preferredPageSize);
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<ResultData | null>(null);
  const version = versionSuffix(query, v);
  const url = `q/${encodeURIComponent(query.name)}`;

  async function run(page: number, pageSize = size) {
    const qs = params
      .filter((p) => values[p])
      .map((p) => `${encodeURIComponent(p)}=${encodeURIComponent(values[p]!)}`);
    qs.push(`format=${format}`, `page=${page}`, `page_size=${pageSize}`);
    if (version) qs.push(`version=${version}`);
    if (connection) qs.push(`connection_name=${encodeURIComponent(connection)}`);
    showError('');
    setRunning(true);
    try {
      const { response, elapsed } = await timedFetch(`/${url}?${qs.join('&')}`);
      const data = await readResult(response, {
        page,
        size: pageSize,
        format,
        elapsed,
        filename: query.name,
      });
      if (data.error) showError(data.error, { status: data.status });
      setResult(data);
    } catch (e) {
      setResult({
        ok: false,
        status: 0,
        statusText: '',
        contentType: '',
        headers: [],
        page,
        size: pageSize,
        hasMore: false,
        elapsed: 0,
        format,
        error: 'Network error: ' + (e as Error).message,
      });
    } finally {
      setRunning(false);
    }
  }

  return (
    <>
      <form
        noValidate
        style={{ display: 'flex', flexDirection: 'column', gap: 14 }}
        onSubmit={(e: FormEvent) => {
          e.preventDefault();
          void run(1);
        }}
      >
        {params.length ? (
          <div className="panel" style={{ overflow: 'auto' }}>
            <table className="grid">
              <thead>
                <tr>
                  {['Name', 'Type', 'Constraints', 'Description'].map((t) => (
                    <th key={t}>{t}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {params.map((p) => {
                  const rule = v.parameters[p];
                  const decl = declared(rule);
                  const required = !rule || (rule.required && !('default' in rule));
                  return (
                    <tr key={p}>
                      <td>
                        <span className="name">{p}</span>
                        {required ? <span className="req"> *</span> : null}
                      </td>
                      <td className="mono">{decl.type}</td>
                      <td>
                        <input
                          id={`run-q-${p}`}
                          className="mono"
                          spellCheck={false}
                          autoComplete="off"
                          placeholder={decl.constraints}
                          required={required}
                          type={rule?.type === 'integer' ? 'number' : 'text'}
                          value={values[p] ?? ''}
                          aria-label={p}
                          onChange={(e) => setValues((all) => ({ ...all, [p]: e.target.value }))}
                        />
                      </td>
                      <td>{decl.description}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="hint">No parameters — this query runs as-is.</div>
        )}
        <div className="get-row">
          <span className="method">GET</span>
          <span className="endpoint" title={`/${url}`}>
            {`/${url}${version ? `?version=${version}` : ''}`}
          </span>
          <select
            id="rq-connection"
            aria-label="Connection"
            value={connection}
            onChange={(e) => setConnection(e.target.value)}
          >
            <option value="">{`saved: ${v.connection_name || 'none'}`}</option>
            {(connections.data ?? []).map((c) => (
              <option key={c.name} value={c.name}>
                {c.name + (c.active ? '' : ' (inactive)')}
              </option>
            ))}
          </select>
          <select
            id="rq-format"
            aria-label="Format"
            value={format}
            onChange={(e) => setFormat(e.target.value)}
          >
            {['json', 'csv', 'tsv', 'xml', 'yaml', 'ndjson', 'xlsx'].map((x) => (
              <option key={x} value={x}>
                {x}
              </option>
            ))}
          </select>
          <select
            id="rq-page-size"
            aria-label="Page size"
            value={String(size)}
            onChange={(e) => setSize(Number(e.target.value))}
          >
            {[10, 25, 50, 100, 500].map((n) => (
              <option key={n} value={String(n)}>
                {n} rows
              </option>
            ))}
          </select>
          <button type="submit" className="btn primary md" style={{ padding: '0 16px' }} disabled={running}>
            Run
          </button>
        </div>
        {v.status === 'draft' && (
          <div className="hint">
            A draft: only the admin key can run it (with <code>?version={v.version}</code>) until it is
            published.
          </div>
        )}
      </form>
      <Results
        result={result}
        running={running}
        filename={query.name}
        onPage={(n) => void run(n)}
        onPageSize={(n) => {
          setSize(n);
          void run(1, n);
        }}
      />
    </>
  );
}

// ---- SQL ----

export function SqlTab({ query, v }: { query: Query; v: QueryVersion }) {
  const { toast } = useFeedback();
  const connections = useConnections();
  const dialect = connections.data?.find((c) => c.name === v.connection_name)?.db;
  const [raw, setRaw] = useState(false);
  const source = v.query_type === 'mongo' ? JSON.stringify(v.mongo, null, 2) : (v.sql ?? '');
  const needsFormat = v.query_type === 'sql' && !source.includes('\n');
  const flow = useQueryFlow(query.name, v.version, v.query_type === 'sql');
  let shown = source;
  if (needsFormat) {
    shown =
      dialect && PARSEABLE_DIALECTS.includes(dialect) && flow.data?.formatted
        ? flow.data.formatted
        : formatSql(source);
  }
  const keys = Object.keys(v.parameters);
  return (
    <>
      <p className="sub-h">Parameters</p>
      {!keys.length ? (
        <div className="hint">This version declares no query_parameters.</div>
      ) : (
        <div className="panel" style={{ overflow: 'auto' }}>
          <table className="grid">
            <thead>
              <tr>
                {['Name', 'Type', 'Constraints', 'Description'].map((t) => (
                  <th key={t}>{t}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {keys.map((k) => {
                const decl = declared(v.parameters[k]);
                return (
                  <tr key={k}>
                    <td>
                      <span className="name">{k}</span>
                    </td>
                    <td className="mono">{decl.type}</td>
                    <td className="mono dim">{decl.constraints}</td>
                    <td>{decl.description}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      <p className="sub-h" style={{ marginTop: 14 }}>
        Query
      </p>
      {needsFormat && flow.isPending && dialect && PARSEABLE_DIALECTS.includes(dialect) ? (
        <Loading />
      ) : (
        <CodeBox sql={shown} />
      )}
      <div className="toolbar" style={{ margin: '-6px 0 0' }}>
        <button type="button" className="btn sm ghost" onClick={() => copyText(shown, toast)}>
          Copy SQL
        </button>
        <button type="button" className="btn sm ghost" onClick={() => setRaw((r) => !r)}>
          {raw ? 'Hide raw file' : 'Show raw file'}
        </button>
      </div>
      <pre className="code" hidden={!raw}>
        {JSON.stringify(v, null, 2)}
      </pre>
      <FlowPanel query={query} v={v} reach={null} />
    </>
  );
}

// ---- History ----

export function HistoryTab({ v, runs }: { v: QueryVersion; runs: HistoryEntry[] }) {
  const [status, setStatus] = useState('');
  const [q, setQ] = useState('');
  const newest = useMemo(() => [...runs].reverse(), [runs]);
  if (!runs.length)
    return <Empty title="No runs recorded">{`Runs of v${v.version} through /q/ appear here.`}</Empty>;
  const needle = q.trim().toLowerCase();
  const rows = newest.filter(
    (x) =>
      (!status || x.status === status) &&
      (!needle ||
        [x.connection_name, x.key_name, x.request_id, x.error].join(' ').toLowerCase().includes(needle)),
  );
  return (
    <>
      <div className="toolbar" style={{ margin: '0 0 10px' }}>
        <select value={status} aria-label="Filter by status" onChange={(e) => setStatus(e.target.value)}>
          <option value="">All statuses</option>
          <option value="success">Success</option>
          <option value="error">Failed</option>
        </select>
        <input
          className="search"
          type="search"
          placeholder="Filter by connection, caller, request id, error…"
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
      </div>
      <div className="panel" style={{ overflow: 'auto', maxHeight: '60vh' }}>
        {!rows.length ? (
          <Empty>No runs match this filter.</Empty>
        ) : (
          <table className="grid">
            <thead>
              <tr>
                {['', 'Executed at', 'Caller', 'Rows', 'Duration', 'Request ID', 'Error'].map((t, i) => (
                  <th key={i} className={i === 3 || i === 4 ? 'num' : ''}>
                    {t}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((x, i) => (
                <tr key={`${x.request_id ?? ''}${i}`}>
                  <td style={{ width: 20, paddingRight: 0 }}>
                    <span
                      className={`dot ${x.status === 'success' ? 'ok' : 'bad'}`}
                      title={String(x.status ?? '')}
                    />
                  </td>
                  <td
                    className="mono dim"
                    style={{ whiteSpace: 'nowrap' }}
                    title={x.connection_name ? 'connection: ' + x.connection_name : undefined}
                  >
                    <Time value={x.executed_at} fallback="" />
                  </td>
                  <td className="mono">{x.key_name ?? ''}</td>
                  <td className="mono num">
                    {x.rows === undefined || x.rows === null ? '—' : String(x.rows)}
                  </td>
                  <td className="mono num">
                    {x.duration_ms === undefined || x.duration_ms === null ? '' : `${x.duration_ms} ms`}
                  </td>
                  <td
                    className="mono dim"
                    title={String(x.request_id ?? '')}
                    style={{
                      maxWidth: 200,
                      overflow: 'hidden',
                      textOverflow: 'ellipsis',
                      whiteSpace: 'nowrap',
                    }}
                  >
                    {x.request_id ?? ''}
                  </td>
                  <td
                    style={{
                      color: 'var(--danger)',
                      whiteSpace: 'normal',
                      wordBreak: 'break-word',
                      maxWidth: 320,
                    }}
                  >
                    {x.error ? String(x.error) : '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </>
  );
}

// ---- Curl and CLI ----

function paramValue(v: QueryVersion, p: string) {
  const rule = v.parameters[p];
  return rule && 'default' in rule && rule.default !== undefined ? String(rule.default) : `<${p}>`;
}

export function CurlTab({ query, v }: { query: Query; v: QueryVersion }) {
  const { toast } = useFeedback();
  const params = callerParams(v);
  const version = versionSuffix(query, v);
  const qs = params.map((p) => {
    const value = paramValue(v, p);
    return value.startsWith('<')
      ? `${encodeURIComponent(p)}=${value}`
      : `${encodeURIComponent(p)}=${encodeURIComponent(value)}`;
  });
  qs.push('format=json');
  if (version) qs.push(`version=${version}`);
  const base = new URL(
    `/q/${encodeURIComponent(query.name)}`,
    globalThis.location?.href ?? 'http://localhost',
  ).href;
  const lines = [
    `curl ${shQuote(`${base}?${qs.join('&')}`)}`,
    "  -H 'X-API-Key: YOUR_KEY_HERE'  # replace with your own key",
  ];
  const cmd = lines.join(' \\\n');
  return (
    <>
      <pre className="curlbox">{cmd}</pre>
      <div>
        <button type="button" className="btn md" onClick={() => copyText(cmd, toast)}>
          Copy command
        </button>
      </div>
      {params.length > 0 && (
        <div className="hint">
          Replace the &lt;placeholder&gt; value(s) with real parameters before running it.
        </div>
      )}
    </>
  );
}

export function CliTab({ query, v }: { query: Query; v: QueryVersion }) {
  const { toast } = useFeedback();
  if (v.status !== 'published') {
    return (
      <div className="hint">
        `queryapigate export` always runs a saved query’s published version - switch to it to see this
        command.
      </div>
    );
  }
  const params = callerParams(v);
  const lines = [`queryapigate export ${shQuote(query.name)}`];
  params.forEach((p) => lines.push(`  --param ${shQuote(`${p}=${paramValue(v, p)}`)}`));
  lines.push('  --format csv', `  --out ${shQuote(`/exports/${query.name}_{date}.csv`)}`);
  const cmd = lines.join(' \\\n');
  return (
    <>
      <pre className="curlbox">{cmd}</pre>
      <div>
        <button type="button" className="btn md" onClick={() => copyText(cmd, toast)}>
          Copy command
        </button>
      </div>
      <div className="hint">
        Runs entirely against <code>QUERYAPIGATE_HOME</code> - no server needs to be running, no API key
        needed. <code>{'{date}'}</code> and <code>{'{name}'}</code> in <code>--out</code> are filled in
        automatically.
      </div>
      {params.length > 0 && (
        <div className="hint">
          Replace the &lt;placeholder&gt; value(s) with real parameters before running it.
        </div>
      )}
      <div className="hint">
        See{' '}
        <a
          href="https://github.com/AnanthaRajuC/QueryAPIGate/blob/main/documentation/INSTALLATION_AND_SETUP.md#scheduled-exports-to-a-file"
          target="_blank"
          rel="noopener"
        >
          Scheduled exports to a file
        </a>{' '}
        for scheduling it with cron, systemd or a Kubernetes CronJob.
      </div>
    </>
  );
}

// ---- API Keys and Roles ----

export function KeysTab({ reach, keys }: { reach: Reach; keys: Record<string, ApiKeyEntry> }) {
  const drawers = useAccessDrawers();
  if (!reach.keys.length)
    return <Empty>No API key reaches this query yet - only the admin key can run it.</Empty>;
  const today = new Date().toISOString().slice(0, 10);
  return (
    <div style={{ overflowX: 'auto' }}>
      <table className="grid">
        <thead>
          <tr>
            {['Key', 'Reach', 'Access', 'Rate limit', 'Expires', 'Last used', ''].map((t, i) => (
              <th key={i}>{t}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {reach.keys.map((k) => {
            const full: Partial<ApiKeyEntry> = keys[k.name] ?? {};
            const expires = full.expires_at ?? undefined;
            const expired = Boolean(expires && expires < today);
            return (
              <tr key={k.name} data-name={k.name}>
                <td>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                    <span
                      className={`dot ${k.active && !expired ? 'ok' : 'off'}`}
                      title={!k.active ? 'Revoked' : expired ? 'Expired' : 'Active'}
                    />
                    <span className="name">{k.name}</span>
                  </div>
                </td>
                <td>
                  <ReachDot via={k.via} />
                </td>
                <AccessCell entry={full} />
                <td className={full.rate_limit ? 'mono' : 'mono dim'} style={{ whiteSpace: 'nowrap' }}>
                  {full.rate_limit || 'server default'}
                </td>
                <td style={{ whiteSpace: 'nowrap' }}>
                  {expires ? (
                    <span style={expired ? { color: 'var(--danger)' } : undefined}>
                      {expires + (expired ? ' (expired)' : '')}
                    </span>
                  ) : (
                    <span className="dim">never</span>
                  )}
                </td>
                <td className="mono dim" style={{ whiteSpace: 'nowrap' }}>
                  <Time value={full.last_used_at} fallback="never" />
                </td>
                <td>
                  <button type="button" className="btn ghost sm" onClick={() => drawers.editKey(k.name)}>
                    Edit
                  </button>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export function RolesTab({
  reach,
  keys,
  roles,
}: {
  reach: Reach;
  keys: Record<string, ApiKeyEntry>;
  roles: Record<string, RoleEntry>;
}) {
  const drawers = useAccessDrawers();
  if (!reach.roles.length) return <Empty>No role grants reach to this query.</Empty>;
  const keysFrom: Record<string, number> = {};
  Object.values(keys).forEach((k) => {
    const from = k.created_from_role;
    if (from) keysFrom[from] = (keysFrom[from] ?? 0) + 1;
  });
  return (
    <div style={{ overflowX: 'auto' }}>
      <table className="grid">
        <thead>
          <tr>
            {['Role', 'Reach', 'Access', 'Rate limit', 'Keys', ''].map((t, i) => (
              <th key={i}>{t}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {reach.roles.map((r) => {
            const full: Partial<RoleEntry> = roles[r.name] ?? {};
            return (
              <tr key={r.name} data-name={r.name}>
                <td>
                  <span className="name">{r.name}</span>
                </td>
                <td>
                  <ReachDot via={r.via} role />
                </td>
                <AccessCell entry={full} />
                <td className={full.rate_limit ? 'mono' : 'mono dim'} style={{ whiteSpace: 'nowrap' }}>
                  {full.rate_limit || 'server default'}
                </td>
                <td className={keysFrom[r.name] ? 'mono' : 'mono dim'} title="Keys created from this role">
                  {keysFrom[r.name] ?? 0}
                </td>
                <td>
                  <button type="button" className="btn ghost sm" onClick={() => drawers.editRole(r.name)}>
                    Edit
                  </button>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

// ---- Access ----

export function AccessTab({ query, v, reach }: { query: Query; v: QueryVersion; reach: Reach }) {
  const navigate = useNavigate();
  return (
    <>
      <div className="access-box">
        <div className="access-summary">
          <b>{reach.keys.length}</b>
          {` ${reach.keys.length === 1 ? 'API key' : 'API keys'}${reach.keys.length ? ' reach' : ' reaches'} this query`}
          {reach.keys.length ? (
            <span className="legend">
              <span className="amap-dot">Q</span> named query <span className="amap-dot">C</span> collection{' '}
              <span className="amap-dot conn">W</span> whole connection
            </span>
          ) : null}
          {!reach.keys.length && <span className="hint">Only the admin key can run it.</span>}
          <button
            type="button"
            className="btn sm ghost"
            style={{ marginLeft: 'auto' }}
            onClick={() => {
              presetQuery(query.name);
              navigate('/access-map');
            }}
          >
            View in Access map
          </button>
        </div>
        {reach.keys.length > 0 && (
          <div className="access-reach">
            {reach.keys.map((k) => (
              <AccessPill key={k.name} entry={k} />
            ))}
          </div>
        )}
        {reach.roles.length > 0 && (
          <div className="access-roles hint">
            {'Also granted to role' + (reach.roles.length > 1 ? 's' : '') + ': '}
            <div className="access-reach" style={{ display: 'inline-flex', marginLeft: 4 }}>
              {reach.roles.map((r) => (
                <AccessPill key={r.name} entry={r} role />
              ))}
            </div>
            {' - a key must be created from one of these to actually call it.'}
          </div>
        )}
      </div>
      <FlowPanel query={query} v={v} reach={reach} />
    </>
  );
}

const FLOW_MAX_PER_COL = 10;

/** "Query flow": the tables (and joins) the SQL touches, flowing into the query and - on the Access tab - out to
 * the keys and roles that can call it. Nodes are HTML; only the connecting lines are SVG, drawn from the nodes'
 * measured positions (ui.py renderFlowDiagram). */
function FlowPanel({ query, v, reach }: { query: Query; v: QueryVersion; reach: Reach | null }) {
  const flow = useQueryFlow(query.name, v.version, v.query_type === 'sql');
  return (
    <>
      <p className="sub-h" style={{ marginTop: 14 }}>
        Query flow
        <span className="hint" style={{ marginLeft: 8, fontWeight: 400 }}>
          best effort, detected from this query’s SQL
        </span>
      </p>
      <div className="access-box">
        {v.query_type !== 'sql' ? (
          <div className="hint">SQL analysis isn&apos;t available for this query</div>
        ) : flow.isPending ? (
          <div className="hint">Analyzing…</div>
        ) : flow.isError ? (
          <div className="hint">{flow.error.message}</div>
        ) : flow.data.error ? (
          <div className="hint">{flow.data.error}</div>
        ) : !flow.data.tables.length ? (
          <div className="hint">No tables detected.</div>
        ) : (
          <FlowDiagram tables={flow.data.tables} joins={flow.data.joins} name={query.name} reach={reach} />
        )}
      </div>
    </>
  );
}

function FlowDiagram({
  tables: allTables,
  joins,
  name,
  reach,
}: {
  tables: string[];
  joins: { left: string; right: string; type: string }[];
  name: string;
  reach: Reach | null;
}) {
  const wrap = useRef<HTMLDivElement>(null);
  const [edges, setEdges] = useState<
    { d: string; join: boolean; label?: { x: number; y: number; text: string } }[]
  >([]);
  const [box, setBox] = useState({ w: 0, h: 0 });
  const tables = allTables.slice(0, FLOW_MAX_PER_COL);
  const keys = reach ? reach.keys.slice(0, FLOW_MAX_PER_COL) : [];
  const roles = reach ? reach.roles.slice(0, Math.max(0, FLOW_MAX_PER_COL - keys.length)) : [];
  const omitted = reach ? reach.keys.length - keys.length + (reach.roles.length - roles.length) : 0;

  useLayoutEffect(() => {
    const root = wrap.current;
    if (!root) return;
    const b = root.getBoundingClientRect();
    const node = (sel: string) => root.querySelector<HTMLElement>(sel);
    const anchor = (el: HTMLElement, side: 'left' | 'right') => {
      const a = el.getBoundingClientRect();
      return { x: (side === 'right' ? a.left + a.width : a.left) - b.left, y: a.top - b.top + a.height / 2 };
    };
    const curve = (p1: { x: number; y: number }, p2: { x: number; y: number }) => {
      const mx = (p1.x + p2.x) / 2;
      return `M ${p1.x} ${p1.y} C ${mx} ${p1.y}, ${mx} ${p2.y}, ${p2.x} ${p2.y}`;
    };
    const queryNode = node('.flow-query');
    if (!queryNode) return;
    const out: typeof edges = [];
    joins.forEach((j) => {
      const l = node(`[data-table="${CSS.escape(j.left)}"]`);
      const r = node(`[data-table="${CSS.escape(j.right)}"]`);
      if (!l || !r) return;
      const p1 = anchor(l, 'left');
      const p2 = anchor(r, 'left');
      p1.x -= 32;
      p2.x -= 32;
      out.push({
        d: curve(p1, p2),
        join: true,
        label: { x: (p1.x + p2.x) / 2, y: (p1.y + p2.y) / 2 - 4, text: j.type },
      });
    });
    root
      .querySelectorAll<HTMLElement>('[data-table]')
      .forEach((t) => out.push({ d: curve(anchor(t, 'right'), anchor(queryNode, 'left')), join: false }));
    root
      .querySelectorAll<HTMLElement>('.flow-col-reach .access-pill')
      .forEach((p) => out.push({ d: curve(anchor(queryNode, 'right'), anchor(p, 'left')), join: false }));
    setEdges(out);
    setBox({ w: b.width, h: b.height });
  }, [tables.join(','), joins, keys.length, roles.length]); // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <div className="flow-diagram" ref={wrap}>
      <div className="flow-cols">
        <div className="flow-col">
          {tables.map((t) => (
            <span key={t} className="tag flow-node" data-table={t}>
              {t}
            </span>
          ))}
          {allTables.length > tables.length && (
            <div className="hint">{`+${allTables.length - tables.length} more`}</div>
          )}
        </div>
        <div className="flow-col flow-col-mid">
          <span className="flow-node flow-query">{name}</span>
        </div>
        {reach && (
          <div className="flow-col flow-col-reach">
            {keys.map((k) => (
              <AccessPill key={k.name} entry={k} />
            ))}
            {roles.map((r) => (
              <AccessPill key={r.name} entry={r} role />
            ))}
            {!keys.length && !roles.length ? (
              <div className="hint">Only the admin key can run it.</div>
            ) : omitted > 0 ? (
              <div className="hint">{`+${omitted} more — see above`}</div>
            ) : null}
          </div>
        )}
      </div>
      <svg className="flow-edges" width={box.w} height={box.h} viewBox={`0 0 ${box.w} ${box.h}`}>
        {edges.map((e, i) => (
          <g key={i}>
            <path d={e.d} className={e.join ? 'flow-edge join' : 'flow-edge'} />
            {e.label && (
              <text x={e.label.x} y={e.label.y} className="flow-edge-label">
                {e.label.text}
              </text>
            )}
          </g>
        ))}
      </svg>
    </div>
  );
}

// ---- Cache ----

export function CacheTab({ query, v, etag }: { query: Query; v: QueryVersion; etag: string | null }) {
  const { showError, toast } = useFeedback();
  const actions = useQueryActions(query.name);
  const [enabled, setEnabled] = useState(Boolean(v.cache_ttl));
  const [ttl, setTtl] = useState(String(v.cache_ttl || 60));
  const save = () => {
    const seconds = enabled ? Number(ttl) : 0;
    if (enabled && (!Number.isInteger(seconds) || seconds < 1)) {
      showError('TTL must be a whole number of seconds, at least 1.');
      return;
    }
    actions.setCacheTtl.mutate(
      { version: v.version, ttl: seconds || null, etag },
      {
        onSuccess: () => {
          showError('');
          toast(
            seconds
              ? `Caching enabled for v${v.version} (${seconds}s)`
              : `Caching disabled for v${v.version}`,
          );
        },
        onError: (e) => showError((e as Error).message),
      },
    );
  };
  return (
    <div className="form" style={{ maxWidth: 420 }}>
      <label className="switch">
        <input
          id="cache-on"
          type="checkbox"
          checked={enabled}
          onChange={(e) => setEnabled(e.target.checked)}
        />
        Cache this version’s responses
        <span className="hint">
          — only takes effect for read-only SQL; a query that writes is never cached, regardless of this
          setting.
        </span>
      </label>
      {enabled && (
        <Field
          id="cache-ttl"
          label="TTL (seconds)"
          hint="How long a response is served from cache before the query runs again for real."
        >
          <input
            id="cache-ttl"
            type="number"
            min="1"
            step="1"
            value={ttl}
            onChange={(e) => setTtl(e.target.value)}
          />
        </Field>
      )}
      <button
        type="button"
        id="cache-save"
        className="btn primary"
        disabled={actions.setCacheTtl.isPending}
        onClick={save}
      >
        Save
      </button>
    </div>
  );
}

// ---- Metrics and the stat tiles ----

function StatTile({
  label,
  value,
  warn,
}: {
  label: string;
  value: string | number | ReactElement;
  warn?: boolean;
}) {
  return (
    <div className="stat-tile">
      <div className="label">{label}</div>
      <div className={warn ? 'value warn' : 'value'}>{typeof value === 'object' ? value : String(value)}</div>
    </div>
  );
}

/** Total runs, success rate, average/slowest duration, average rows, last run (ui.py queryStatTilesRow). */
export function StatTiles({ runs }: { runs: HistoryEntry[] }) {
  if (!runs.length) return null;
  const successes = runs.filter((x) => x.status === 'success');
  const errorRate = (100 * (runs.length - successes.length)) / runs.length;
  const durations = runs.map((x) => x.duration_ms).filter((d): d is number => d !== undefined && d !== null);
  const avgDuration = durations.length
    ? Math.round(durations.reduce((a, d) => a + d, 0) / durations.length)
    : null;
  const maxDuration = durations.length ? Math.max(...durations) : null;
  const rowCounts = successes.map((x) => x.rows).filter((r): r is number => r !== undefined && r !== null);
  const avgRows = rowCounts.length
    ? Math.round(rowCounts.reduce((a, r) => a + r, 0) / rowCounts.length)
    : null;
  const last = runs[runs.length - 1];
  return (
    <div className="stat-tiles">
      <StatTile label="Total runs" value={runs.length} />
      <StatTile label="Success rate" value={`${(100 - errorRate).toFixed(1)}%`} warn={errorRate >= 5} />
      <StatTile label="Avg duration" value={avgDuration === null ? '—' : `${avgDuration} ms`} />
      <StatTile label="Slowest run" value={maxDuration === null ? '—' : `${maxDuration} ms`} />
      <StatTile label="Avg rows" value={avgRows === null ? '—' : avgRows} />
      <StatTile label="Last run" value={<Time value={last?.executed_at} />} />
    </div>
  );
}

export function MetricsTab({ v, runs }: { v: QueryVersion; runs: HistoryEntry[] }) {
  if (!runs.length)
    return <Empty title="No runs recorded">{`Runs of v${v.version} through /q/ appear here.`}</Empty>;
  const byCaller: Record<string, number> = {};
  runs.forEach((x) => {
    const k = x.key_name || '-';
    byCaller[k] = (byCaller[k] ?? 0) + 1;
  });
  const callers = Object.keys(byCaller).sort();
  const max = Math.max(...Object.values(byCaller));
  return (
    <>
      <StatTiles runs={runs} />
      {callers.length > 1 && (
        <div className="metrics-charts">
          <div className="chart-card">
            <h3>Runs by caller</h3>
            {callers.map((c) => (
              <div className="bar-row" key={c}>
                <span className="bl">{c}</span>
                <div className="bar-track">
                  <div
                    className="bar-fill"
                    style={{
                      width: `${Math.max(1, (100 * byCaller[c]!) / max)}%`,
                      background: 'var(--accent)',
                    }}
                  />
                </div>
                <span className="bv">{byCaller[c]}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </>
  );
}

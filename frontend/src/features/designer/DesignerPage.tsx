import { useQuery } from '@tanstack/react-query';
import { useEffect, useMemo, useRef, useState, type FormEvent } from 'react';
import { useLocation, useNavigate } from 'react-router';

import { api, timedFetch, unwrap } from '@/api/client';
import { getApiKey } from '@/auth/apiKey';
import { useAllQueries, useCollections, useConnections, type Connection } from '@/app/data';
import { Field, FormActions, useFeedback } from '@/app/feedback';
import { readResult, Results, widestColumn, type ResultData } from '@/components/Results';
import { SchemaField, useSchema } from '@/components/SchemaBrowser';
import { SqlEditor, type SqlEditorHandle } from '@/components/SqlEditor';
import { useSaveQuery, type QueryCreateInput, type QueryVersionInput } from '@/features/repository/api';
import { Impact } from '@/features/repository/forms';
import { shQuote, sqlParams } from '@/lib/sql';

import {
  candidateAt,
  literalToValue,
  loadDesignerState,
  loadRunHistory,
  mongoDocParams,
  recordResult,
  recordRun,
  saveDesignerState,
  type Candidate,
  type DesignerState,
  type HistoryEntry,
} from './state';

// The classic "API Designer" screen (ui.py #tab-run): ad-hoc SQL (or a Mongo find()) against any connection, with
// the Type → Host → Connection → Database picker, the runner's sidebar (Recent Queries, Settings, Schema) behind a
// draggable splitter, Explain, the quick-stats strip, the results panel, and "Save as New API" inline below the
// editor. Same markup and classes, so classic.css lays it out exactly as before.

const DB_SWITCHABLE_TYPES = ['mysql', 'postgres', 'clickhouse', 'mongo'];
const SIDE_TAB_KEY = 'queryapigate-ui-run-side-tab';
const SIDE_W_KEY = 'queryapigate-ui-runner-side-w';
const MIN_SIDE_W = 220;
const MAX_SIDE_W = 560;
const MIN_MAIN_W = 320;

function hostLabel(c: Connection) {
  return c.host ? c.host + (c.port ? ':' + c.port : '') : '(local file)';
}

function usageText(usage: Connection['usage'] | undefined) {
  if (!usage || !usage.queries) return null;
  const parts = [usage.queries + (usage.queries === 1 ? ' query' : ' queries')];
  if (usage.errors) parts.push(usage.errors + ' failed');
  if (usage.avg_duration_ms !== undefined && usage.avg_duration_ms !== null)
    parts.push(usage.avg_duration_ms + 'ms avg');
  return parts.join(' · ');
}

function readLocal(key: string) {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}
function writeLocal(key: string, value: string) {
  try {
    localStorage.setItem(key, value);
  } catch {
    // a preference only
  }
}

interface QuickStats {
  status: number;
  rows: number | null;
  elapsed: number;
  format: string;
  widest: { column: string; maxLen: number } | null;
}

export function DesignerPage() {
  const navigate = useNavigate();
  const location = useLocation();
  const { showError } = useFeedback();
  const connections = useConnections();
  const conns = useMemo(() => connections.data ?? [], [connections.data]);
  const byName = useMemo(() => Object.fromEntries(conns.map((c) => [c.name, c])), [conns]);

  const [state, setState] = useState<DesignerState>(loadDesignerState);
  useEffect(() => saveDesignerState(state), [state]);
  const set = (patch: Partial<DesignerState>) => setState((s) => ({ ...s, ...patch }));
  const editor = useRef<SqlEditorHandle>(null);

  // Type → Host → Connection: each narrows the next (ui.py paintRunConnType/Host/Connection).
  const types = [...new Set(conns.map((c) => c.db))].sort();
  const type = types.includes(state.type) ? state.type : '';
  const hosts = [...new Set(conns.filter((c) => !type || c.db === type).map(hostLabel))].sort();
  const host = hosts.includes(state.host) ? state.host : '';
  const names = conns
    .filter((c) => (!type || c.db === type) && (!host || hostLabel(c) === host))
    .map((c) => c.name)
    .sort();
  const connection = names.includes(state.connection)
    ? state.connection
    : (names.find((n) => byName[n]?.active) ?? '');
  const conn = byName[connection];
  const isMongo = conn?.db === 'mongo';

  // Database: another database on the same server, where the type supports listing them.
  const switchable = Boolean(conn && DB_SWITCHABLE_TYPES.includes(conn.db));
  const databases = useQuery({
    queryKey: ['connections', connection, 'databases'],
    queryFn: async () =>
      unwrap(await api.POST('/api/v1/connections/databases', { body: { name: connection } })),
    enabled: switchable,
    retry: false,
    staleTime: 5 * 60_000,
  });
  const dbList = databases.data?.databases ?? [];
  const database = dbList.includes(state.database) ? state.database : (conn?.database ?? '');
  const databaseOverride = conn && database && database !== conn.database ? database : null;

  const schema = useSchema(connection || undefined, databaseOverride);
  const completion = useMemo(
    () => Object.fromEntries((schema.data?.tables ?? []).map((t) => [t.name, t.columns.map((c) => c.name)])),
    [schema.data],
  );

  // ---- running ----
  const [result, setResult] = useState<ResultData | null>(null);
  const [running, setRunning] = useState<false | 'run' | 'explain'>(false);
  const [stats, setStats] = useState<QuickStats | null>(null);
  const [history, setHistory] = useState<HistoryEntry[]>(loadRunHistory);
  const [curl, setCurl] = useState<string | undefined>();
  const [lastExplain, setLastExplain] = useState(false);

  async function run(
    opts: { explain?: boolean; page?: number; pageSize?: number; sql?: string; connection?: string } = {},
  ) {
    const target = opts.connection ?? connection;
    const targetConn = byName[target];
    if (!target || !targetConn) {
      showError('Pick a connection first — add one on the Connections tab.');
      return;
    }
    const mongo = targetConn.db === 'mongo';
    const text = opts.sql ?? state.sql;
    if (opts.explain && mongo) {
      showError('EXPLAIN is not supported for Mongo connections yet.');
      return;
    }
    if (!text.trim()) {
      showError(mongo ? 'Write a query first.' : 'Write some SQL to run.', {
        errors: { sql: 'This field is required' },
      });
      return;
    }
    let params: Record<string, unknown> = {};
    if (state.params.trim()) {
      try {
        params = JSON.parse(state.params) as Record<string, unknown>;
      } catch (e) {
        showError('Bound parameters must be valid JSON: ' + (e as Error).message, {
          errors: { params: (e as Error).message },
        });
        return;
      }
    }
    const page = opts.page ?? 1;
    const pageSize = opts.pageSize ?? (state.pageSize || 10);
    const qs = `format=${encodeURIComponent(state.format)}&page=${page}&page_size=${pageSize}${state.timeout ? '&timeout=' + encodeURIComponent(state.timeout) : ''}`;
    let path: string;
    let body: Record<string, unknown>;
    if (mongo) {
      let doc: { collection?: string; filter?: object; projection?: object; sort?: object };
      try {
        doc = JSON.parse(text) as typeof doc;
      } catch (e) {
        showError('The query must be valid JSON: ' + (e as Error).message, {
          errors: { sql: (e as Error).message },
        });
        return;
      }
      if (!doc || typeof doc !== 'object' || Array.isArray(doc) || !doc.collection) {
        showError(
          'The query must be a JSON object with a "collection" field, e.g. {"collection": "users", "filter": {}}.',
          {
            errors: { sql: 'collection is required' },
          },
        );
        return;
      }
      path = `/execute_mongo?${qs}`;
      body = { collection: doc.collection, filter: doc.filter ?? {}, connection_name: target, params };
      if (doc.projection) body.projection = doc.projection;
      if (doc.sort) body.sort = doc.sort;
    } else {
      path = `/execute_sql?${qs}`;
      body = { sql: opts.explain ? 'EXPLAIN ' + text : text, connection_name: target, params };
      if (target === connection && databaseOverride) body.database = databaseOverride;
    }
    setLastExplain(Boolean(opts.explain));
    set({ page });
    showError('');
    if (!opts.explain && page === 1 && opts.page === undefined)
      setHistory(recordRun(text, target, state.format));
    setRunning(opts.explain ? 'explain' : 'run');
    setCurl(
      [
        `curl -X POST ${shQuote(new URL(path, globalThis.location?.href ?? 'http://localhost').href)}`,
        "  -H 'Content-Type: application/json'",
        ...(getApiKey() ? ["  -H 'X-API-Key: YOUR_KEY_HERE'  # replace with your own key"] : []),
        `  -d ${shQuote(JSON.stringify(body))}`,
      ].join(' \\\n'),
    );
    try {
      const { response, elapsed } = await timedFetch(path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const data = await readResult(response, {
        page,
        size: pageSize,
        format: state.format,
        elapsed,
        filename: opts.explain ? 'explain' : 'query',
      });
      if (data.error) showError(data.error, { status: data.status });
      setResult(data);
      const rows = data.rows ? data.rows.length : null;
      setStats({
        status: data.status,
        rows,
        elapsed,
        format: state.format,
        widest: data.rows ? widestColumn(data.rows) : null,
      });
      if (!opts.explain) setHistory(recordResult(data.status, rows, elapsed));
    } catch (e) {
      showError('Network error: ' + (e as Error).message);
    } finally {
      setRunning(false);
    }
  }

  // A table previewed from a schema browser elsewhere (the drawer's 👁) lands here and runs at once.
  const navState = location.state as {
    preview?: { connection: string; table: string };
    select?: string;
  } | null;
  const preview = navState?.preview;
  // The Connections screen's "Query" button: select that connection, ready to type.
  const select = navState?.select;
  const handledSelect = useRef<unknown>(null);
  useEffect(() => {
    if (!select || handledSelect.current === location.key || !byName[select]) return;
    handledSelect.current = location.key;
    const c = byName[select]!;
    setState((s) => ({ ...s, type: c.db, host: hostLabel(c), connection: c.name, database: '' }));
    navigate(location.pathname, { replace: true, state: null });
  }, [select, location.key, location.pathname, byName, navigate]);
  const handledPreview = useRef<unknown>(null);
  useEffect(() => {
    if (!preview || handledPreview.current === location.key || !byName[preview.connection]) return;
    handledPreview.current = location.key;
    const c = byName[preview.connection]!;
    const sql =
      c.db === 'mongo'
        ? JSON.stringify({ collection: preview.table, filter: {} }, null, 2)
        : 'SELECT * FROM ' + preview.table;
    setState((s) => ({
      ...s,
      type: c.db,
      host: hostLabel(c),
      connection: c.name,
      database: '',
      sql,
      page: 1,
    }));
    void run({ sql, connection: c.name });
    navigate(location.pathname, { replace: true, state: null });
  }, [preview, location.key, location.pathname, byName, run, navigate]);

  function selectFromHistory(entry: HistoryEntry) {
    const c = byName[entry.connection];
    set({
      sql: entry.sql,
      format: entry.format || state.format,
      ...(c ? { type: c.db, host: hostLabel(c), connection: c.name, database: '' } : {}),
    });
  }

  // ---- "Parameterize" (double-click a column or its literal) ----
  const [paramize, setParamize] = useState<{ x: number; y: number; candidate: Candidate } | null>(null);
  function onDoubleClick(event: MouseEvent) {
    const sel = editor.current?.selection();
    if (!sel || sel.from === sel.to) return;
    const candidate = candidateAt(sel.doc, sel.from, sel.to);
    if (candidate) setParamize({ x: event.clientX, y: event.clientY, candidate });
  }
  function applyParamize(c: Candidate) {
    setParamize(null);
    editor.current?.replaceRange(c.absStart, c.absEnd, ':' + c.name);
    let params: Record<string, unknown>;
    try {
      params = state.params.trim() ? (JSON.parse(state.params) as Record<string, unknown>) : {};
    } catch {
      params = {};
    }
    params[c.name] = literalToValue(c.valueText);
    set({ params: JSON.stringify(params, null, 2) });
  }

  const [saveOpen, setSaveOpen] = useState(false);
  function openSave() {
    if (!connection) {
      showError('Pick a connection first — add one on the Connections tab.');
      return;
    }
    if (!state.sql.trim()) {
      showError('Write a query first.', { errors: { sql: 'This field is required' } });
      return;
    }
    setSaveOpen(true);
  }

  const refs = isMongo ? mongoDocParams(state.sql) : sqlParams(state.sql);
  const contextUsage = usageText(conn?.usage);

  return (
    <>
      <div className="page-head">
        <div className="titles">
          <h1>API Designer</h1>
          <span className="sub">Ad-hoc statements against any active connection. Nothing here is saved.</span>
        </div>
      </div>
      <form
        id="run-form"
        className="panel runner"
        noValidate
        onSubmit={(e: FormEvent) => {
          e.preventDefault();
          void run();
        }}
        style={{ ['--runner-side-w' as string]: undefined }}
        ref={(el) => {
          const saved = Number(readLocal(SIDE_W_KEY));
          if (el && saved && !el.style.getPropertyValue('--runner-side-w')) {
            el.style.setProperty('--runner-side-w', Math.max(MIN_SIDE_W, Math.min(MAX_SIDE_W, saved)) + 'px');
          }
        }}
      >
        <div className="runner-main">
          <div className="runner-bar">
            <span className="lbl">Type</span>
            <select
              id="run-conn-type"
              aria-label="Database type"
              value={type}
              onChange={(e) => set({ type: e.target.value })}
            >
              <option value="">All types</option>
              {types.map((t) => (
                <option key={t} value={t}>
                  {t}
                </option>
              ))}
            </select>
            <span className="lbl">Host</span>
            <select
              id="run-conn-host"
              aria-label="Host"
              value={host}
              onChange={(e) => set({ host: e.target.value })}
            >
              <option value="">All hosts</option>
              {hosts.map((h) => (
                <option key={h} value={h}>
                  {h}
                </option>
              ))}
            </select>
            <span className="lbl">Connection</span>
            <select
              id="run-connection"
              required
              aria-label="Connection"
              value={connection}
              onChange={(e) => set({ connection: e.target.value, database: '' })}
            >
              {names.map((n) => (
                <option key={n} value={n}>
                  {`${n} · ${byName[n]?.database || '?'}${byName[n]?.active ? '' : ' (inactive)'}`}
                </option>
              ))}
            </select>
            <span className="lbl">Database</span>
            <select
              id="run-database"
              aria-label="Database"
              disabled={!switchable || !databases.isSuccess}
              value={switchable && databases.isSuccess ? database : ''}
              onChange={(e) => set({ database: e.target.value })}
            >
              {!conn ? (
                <option value="">—</option>
              ) : !switchable ? (
                <option value="">{conn.database || '(default)'}</option>
              ) : databases.isPending ? (
                <option value="">Loading…</option>
              ) : databases.isError ? (
                <option value="">Unavailable</option>
              ) : (
                dbList.map((d) => (
                  <option key={d} value={d}>
                    {d}
                  </option>
                ))
              )}
            </select>
            <span className="lbl">Format</span>
            <select
              id="run-format"
              aria-label="Format"
              value={state.format}
              onChange={(e) => set({ format: e.target.value })}
            >
              {['json', 'ndjson', 'csv', 'tsv', 'xml', 'yaml', 'xlsx'].map((f) => (
                <option key={f} value={f}>
                  {f}
                </option>
              ))}
            </select>
            <span className="spacer" />
            <span className="hint hide-sm">
              <kbd>Ctrl</kbd> <kbd>Enter</kbd>
            </span>
            <button
              type="button"
              className="btn ghost md"
              id="run-save-as-api-button"
              title="Save this query as a new API"
              onClick={openSave}
            >
              Save as New API
            </button>
            {!isMongo && (
              <button
                type="button"
                className="btn ghost md"
                id="run-explain-button"
                title="Run EXPLAIN on this query"
                disabled={Boolean(running)}
                onClick={() => void run({ explain: true })}
              >
                Explain
              </button>
            )}
            <button
              type="submit"
              className="btn primary md"
              id="run-button"
              style={{ padding: '0 16px' }}
              disabled={Boolean(running)}
            >
              Run
            </button>
          </div>
          <div className="runner-conn-context" id="run-conn-context">
            {conn && (
              <>
                <span
                  className={`dot ${conn.active ? 'ok' : 'bad'}`}
                  title={conn.active ? 'Active' : 'Inactive'}
                />
                <span>{conn.name}</span>
                <span className="dim">
                  {conn.host
                    ? `${hostLabel(conn)} · ${conn.database || '?'}`
                    : conn.database || '(local file)'}
                </span>
                <span className="dim">{contextUsage || 'No activity yet'}</span>
              </>
            )}
          </div>
          <SqlEditor
            handle={editor}
            id="run-sql"
            boxed={false}
            value={state.sql}
            onChange={(sql) => set({ sql })}
            dialect={conn?.db}
            schema={completion}
            label={isMongo ? 'Mongo find query (JSON)' : 'SQL'}
            placeholder={
              isMongo
                ? '{"collection": "orders", "filter": {"status": ":status"}}'
                : 'SELECT * FROM t WHERE id = :id'
            }
            onRun={() => void run()}
            onDoubleClick={onDoubleClick}
          />
        </div>
        <Splitter />
        <div className="runner-side">
          <SideTabs
            recent={<RunHistory entries={history} onPick={selectFromHistory} />}
            settings={
              <>
                <div className="field">
                  <label htmlFor="run-params">
                    Bound parameters <span className="type">JSON</span>
                  </label>
                  <textarea
                    id="run-params"
                    spellCheck={false}
                    placeholder='{"id": 1}'
                    value={state.params}
                    onChange={(e) => set({ params: e.target.value })}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
                        e.preventDefault();
                        void run();
                      }
                    }}
                  />
                  <div className="refs" id="run-refs">
                    {!isMongo && refs.length > 0 && <span className="hint">In SQL:</span>}
                    {!isMongo &&
                      refs.map((n) => (
                        <span key={n} className="tag">
                          :{n}
                        </span>
                      ))}
                  </div>
                </div>
                <div className="grid2">
                  <div className="field">
                    <label htmlFor="run-page">Page</label>
                    <input
                      id="run-page"
                      type="number"
                      min="1"
                      value={state.page}
                      onChange={(e) => set({ page: Number(e.target.value) || 1 })}
                    />
                  </div>
                  <div className="field">
                    <label htmlFor="run-page-size">Page size</label>
                    <input
                      id="run-page-size"
                      type="number"
                      min="1"
                      value={state.pageSize}
                      onChange={(e) => set({ pageSize: Number(e.target.value) || 10 })}
                    />
                  </div>
                </div>
                <div className="field">
                  <label htmlFor="run-timeout">
                    Timeout <span className="type">seconds, optional</span>
                  </label>
                  <input
                    id="run-timeout"
                    type="number"
                    min="0"
                    step="any"
                    placeholder="server default"
                    value={state.timeout}
                    onChange={(e) => set({ timeout: e.target.value })}
                  />
                </div>
              </>
            }
            schema={
              <div id="run-schema-slot">
                <SchemaField
                  connection={connection}
                  dialect={conn?.db}
                  database={databaseOverride}
                  editor={editor}
                  isMongo={isMongo}
                  onPreview={(table) => {
                    const sql = isMongo
                      ? JSON.stringify({ collection: table, filter: {} }, null, 2)
                      : 'SELECT * FROM ' + table;
                    set({ sql, page: 1 });
                    void run({ sql });
                  }}
                  showDdl
                  showUsage
                />
              </div>
            }
          />
        </div>
      </form>
      <div id="run-quick-stats">
        {stats && (
          <>
            <span className={`dot ${stats.status < 400 ? 'ok' : 'bad'}`} />
            <span>
              <b>{stats.status}</b>
              {stats.status < 400 ? ' OK' : ' error'}
            </span>
            {stats.rows !== null && (
              <span>
                <b>{stats.rows}</b>
                {stats.rows === 1 ? ' row' : ' rows'}
              </span>
            )}
            <span>
              <b>{stats.elapsed}</b> ms
            </span>
            <span className="dim">{stats.format}</span>
            {stats.widest && (
              <span className="dim">{`Widest column: ${stats.widest.column} (~${stats.widest.maxLen} chars)`}</span>
            )}
          </>
        )}
      </div>
      <div className="panel" id="run-save-panel" hidden={!saveOpen} style={{ marginTop: 12 }}>
        {saveOpen && (
          <SaveAsApi
            connection={connection}
            sql={state.sql}
            isMongo={isMongo}
            onClose={() => setSaveOpen(false)}
            onSaved={(name) => {
              setSaveOpen(false);
              navigate(`/queries/${encodeURIComponent(name)}`);
            }}
          />
        )}
      </div>
      <Results
        result={result}
        running={Boolean(running)}
        filename={lastExplain ? 'explain' : 'query'}
        curl={curl}
        panelProps={{ id: 'run-results-panel', className: 'panel results' }}
        onPage={(n) => void run({ explain: lastExplain, page: n })}
        onPageSize={(n) => {
          set({ pageSize: n });
          void run({ explain: lastExplain, page: 1, pageSize: n });
        }}
      />
      {paramize && (
        <ParamizePopover {...paramize} onApply={applyParamize} onClose={() => setParamize(null)} />
      )}
    </>
  );
}

/** Recent Queries / Settings / Schema, remembered per browser (ui.py side-tabs). */
function SideTabs({
  recent,
  settings,
  schema,
}: {
  recent: React.ReactNode;
  settings: React.ReactNode;
  schema: React.ReactNode;
}) {
  const [tab, setTab] = useState(() => {
    const saved = readLocal(SIDE_TAB_KEY);
    return saved === 'recent' || saved === 'settings' ? saved : 'schema';
  });
  const pick = (name: string) => {
    setTab(name);
    writeLocal(SIDE_TAB_KEY, name);
  };
  const tabButton = (name: string, label: string) => (
    <button
      type="button"
      className={tab === name ? 'side-tab active' : 'side-tab'}
      data-side-tab={name}
      role="tab"
      aria-selected={tab === name}
      onClick={() => pick(name)}
    >
      {label}
    </button>
  );
  return (
    <>
      <div className="side-tabs" role="tablist">
        {tabButton('recent', 'Recent Queries')}
        {tabButton('settings', 'Settings')}
        {tabButton('schema', 'Schema')}
      </div>
      <div className="side-panels">
        <div className="side-tab-panel" data-side-panel="recent" hidden={tab !== 'recent'}>
          <div id="run-history-slot">{recent}</div>
        </div>
        <div className="side-tab-panel" data-side-panel="settings" hidden={tab !== 'settings'}>
          {settings}
        </div>
        <div className="side-tab-panel" data-side-panel="schema" hidden={tab !== 'schema'}>
          {schema}
        </div>
      </div>
    </>
  );
}

/** The draggable divider between the editor and the sidebar, width remembered per browser (ui.py runner splitter). */
function Splitter() {
  const ref = useRef<HTMLDivElement>(null);
  const drag = useRef<{ startX: number; startW: number } | null>(null);
  const runner = () => ref.current?.closest<HTMLElement>('.runner') ?? null;
  const sideWidth = () => runner()?.querySelector('.runner-side')?.getBoundingClientRect().width ?? 280;
  const setWidth = (px: number) => runner()?.style.setProperty('--runner-side-w', px + 'px');
  const persist = () => {
    const value = parseFloat(getComputedStyle(runner()!).getPropertyValue('--runner-side-w'));
    if (value) writeLocal(SIDE_W_KEY, String(value));
  };
  return (
    <div
      ref={ref}
      className="runner-splitter"
      id="run-splitter"
      role="separator"
      aria-orientation="vertical"
      aria-label="Resize sidebar"
      tabIndex={0}
      onPointerDown={(e) => {
        drag.current = { startX: e.clientX, startW: sideWidth() };
        ref.current?.classList.add('dragging');
        ref.current?.setPointerCapture(e.pointerId);
        document.body.style.userSelect = 'none';
      }}
      onPointerMove={(e) => {
        if (!drag.current) return;
        const proposed = drag.current.startW - (e.clientX - drag.current.startX);
        const maxAllowed = Math.min(
          MAX_SIDE_W,
          (runner()?.getBoundingClientRect().width ?? 0) - MIN_MAIN_W - 7,
        );
        setWidth(Math.max(MIN_SIDE_W, Math.min(maxAllowed, proposed)));
      }}
      onPointerUp={() => {
        if (!drag.current) return;
        drag.current = null;
        ref.current?.classList.remove('dragging');
        document.body.style.userSelect = '';
        persist();
      }}
      onPointerCancel={() => {
        drag.current = null;
        ref.current?.classList.remove('dragging');
        document.body.style.userSelect = '';
      }}
      onKeyDown={(e) => {
        const step = e.shiftKey ? 40 : 12;
        if (e.key === 'ArrowLeft') setWidth(Math.min(MAX_SIDE_W, sideWidth() + step));
        else if (e.key === 'ArrowRight') setWidth(Math.max(MIN_SIDE_W, sideWidth() - step));
        else return;
        e.preventDefault();
        persist();
      }}
    />
  );
}

function RunHistory({ entries, onPick }: { entries: HistoryEntry[]; onPick: (e: HistoryEntry) => void }) {
  if (!entries.length) return <div className="hint">Queries you run will show up here.</div>;
  return (
    <div className="history-list">
      {entries.map((entry) => {
        const meta: string[] = [];
        if (entry.status !== null && entry.status >= 400) meta.push(`failed (${entry.status})`);
        if (entry.rows !== null) meta.push(entry.rows + (entry.rows === 1 ? ' row' : ' rows'));
        if (entry.duration_ms !== null) meta.push(entry.duration_ms + ' ms');
        return (
          <button
            key={`${entry.connection}:${entry.sql}`}
            type="button"
            className="history-item"
            title={entry.sql}
            onClick={() => onPick(entry)}
          >
            <span className="history-sql">
              {entry.sql.length > 64 ? entry.sql.slice(0, 64) + '…' : entry.sql}
            </span>
            {meta.length > 0 && <span className="history-meta">{meta.join(' · ')}</span>}
          </button>
        );
      })}
    </div>
  );
}

function ParamizePopover({
  x,
  y,
  candidate,
  onApply,
  onClose,
}: {
  x: number;
  y: number;
  candidate: Candidate;
  onApply: (c: Candidate) => void;
  onClose: () => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [left, setLeft] = useState(x);
  useEffect(() => {
    const width = ref.current?.offsetWidth ?? 260;
    setLeft(Math.max(8, Math.min(x, window.innerWidth - width - 12)));
    const outside = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose();
    };
    const esc = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    const t = setTimeout(() => {
      document.addEventListener('mousedown', outside, true);
      document.addEventListener('keydown', esc, true);
    }, 0);
    return () => {
      clearTimeout(t);
      document.removeEventListener('mousedown', outside, true);
      document.removeEventListener('keydown', esc, true);
    };
  }, [x, onClose]);
  return (
    <div ref={ref} id="paramize-popover" className="paramize-popover" style={{ left, top: y + 12 }}>
      <div className="preview">
        {`${candidate.name} = ${candidate.valueText}  →  ${candidate.name} = `}
        <b>:{candidate.name}</b>
      </div>
      <button type="button" onClick={() => onApply(candidate)}>
        Parameterize
      </button>
    </div>
  );
}

/** "Save as New API", inline below the editor: the query and connection are read live from the editor at save
 * time (ui.py saveInlineApi). Save publishes at once, as it always did; Save as draft keeps it unpublished. */
function SaveAsApi({
  connection,
  sql,
  isMongo,
  onClose,
  onSaved,
}: {
  connection: string;
  sql: string;
  isMongo: boolean;
  onClose: () => void;
  onSaved: (name: string) => void;
}) {
  const { showError, toast } = useFeedback();
  const queries = useAllQueries();
  const collections = useCollections();
  const save = useSaveQuery();
  const [filename, setFilename] = useState('');
  const [author, setAuthor] = useState('');
  const [description, setDescription] = useState('');
  const [collection, setCollection] = useState('');
  const [tags, setTags] = useState('');
  const [params, setParams] = useState(() => {
    const names = isMongo ? mongoDocParams(sql) : sqlParams(sql);
    return names.length ? JSON.stringify(Object.fromEntries(names.map((n) => [n, {}])), null, 2) : '';
  });

  function submit(publish: boolean) {
    let parameters: Record<string, unknown> = {};
    if (params.trim()) {
      try {
        parameters = JSON.parse(params) as Record<string, unknown>;
      } catch (e) {
        showError('query_parameters is not valid JSON: ' + (e as Error).message, {
          errors: { query_parameters: (e as Error).message },
        });
        return;
      }
    }
    const name = filename.trim();
    const body: QueryVersionInput = {
      author,
      description,
      tags: tags
        .split(',')
        .map((t) => t.trim())
        .filter(Boolean),
      connection_name: connection || undefined,
      parameters: parameters as QueryVersionInput['parameters'],
      publish,
    };
    if (isMongo) {
      let doc: { collection?: string; filter?: object; projection?: object; sort?: object };
      try {
        doc = sql.trim() ? (JSON.parse(sql) as typeof doc) : {};
      } catch (e) {
        showError('The query must be valid JSON: ' + (e as Error).message, {
          errors: { sql_query: (e as Error).message },
        });
        return;
      }
      if (!doc || typeof doc !== 'object' || Array.isArray(doc) || !doc.collection) {
        showError(
          'The query must be a JSON object with a "collection" field, e.g. {"collection": "users", "filter": {}}.',
          {
            errors: { sql_query: 'collection is required' },
          },
        );
        return;
      }
      Object.assign(body, {
        query_type: 'mongo',
        mongo_collection: doc.collection,
        mongo_filter: doc.filter ?? {},
      });
      if (doc.projection) body.mongo_projection = doc.projection as Record<string, never>;
      if (doc.sort) body.mongo_sort = doc.sort as Record<string, never>;
    } else {
      body.sql = sql;
    }
    const existing = queries.data?.find((q) => q.name === name);
    const chosen = collection.trim();
    if (chosen && existing && (existing.collection ?? '') !== chosen) {
      showError(
        `“${name}” already exists${existing.collection ? ' in collection ' + existing.collection : ' with no collection'}. Save it without a collection here, then use “Move…” — that shows which keys gain or lose access first.`,
      );
      return;
    }
    const createBody: QueryCreateInput = { ...body, name, ...(chosen ? { collection: chosen } : {}) };
    save.mutate(
      existing ? { kind: 'version', name, etag: null, body } : { kind: 'create', body: createBody },
      {
        onSuccess: (saved) => {
          showError('');
          toast(`Saved ${saved.name} v${saved.latest_version}${publish ? '' : ' as a draft'}`);
          onSaved(saved.name);
        },
        onError: (e) => showError((e as Error).message),
      },
    );
  }

  return (
    <>
      <div className="panel-head">
        <h2>Save as New API</h2>
        <span className="spacer" />
        <button type="button" className="btn sm ghost" title="Close" onClick={onClose}>
          ✕
        </button>
      </div>
      <form
        className="form"
        noValidate
        onSubmit={(e) => {
          e.preventDefault();
          submit(true);
        }}
      >
        <Field
          id="rs-filename"
          label="Filename"
          hint="Letters, digits, spaces, “.”, “_” and “-”. Saving an existing name adds a version."
        >
          <input
            id="rs-filename"
            placeholder="films_by_rating"
            required
            autoComplete="off"
            spellCheck={false}
            autoFocus
            value={filename}
            onChange={(e) => setFilename(e.target.value)}
          />
        </Field>
        <div className="grid2">
          <Field id="rs-author" label="Author">
            <input
              id="rs-author"
              required
              autoComplete="off"
              spellCheck={false}
              value={author}
              onChange={(e) => setAuthor(e.target.value)}
            />
          </Field>
          <Field id="rs-description" label="Description">
            <input
              id="rs-description"
              required
              autoComplete="off"
              spellCheck={false}
              value={description}
              onChange={(e) => setDescription(e.target.value)}
            />
          </Field>
        </div>
        <div className="field">
          <label htmlFor="rs-collection">Collection</label>
          <input
            id="rs-collection"
            list="rs-collection-list"
            placeholder="optional — e.g. reporting"
            autoComplete="off"
            spellCheck={false}
            value={collection}
            onChange={(e) => setCollection(e.target.value)}
          />
          <datalist id="rs-collection-list">
            {Object.keys(collections.data?.collections ?? {})
              .sort()
              .map((c) => (
                <option key={c} value={c} />
              ))}
          </datalist>
          <div className="hint">
            Optional. Lowercase letters, digits, “.”, “_” and “-”. Later, use “Move…” to change it.
          </div>
          {collection.trim() && <Impact moves={[[null, collection.trim()]]} info={collections.data} />}
        </div>
        <Field id="rs-tags" label="Tags">
          <input
            id="rs-tags"
            placeholder="comma-separated"
            autoComplete="off"
            spellCheck={false}
            value={tags}
            onChange={(e) => setTags(e.target.value)}
          />
        </Field>
        <Field
          id="rs-params"
          label="query_parameters"
          hint="JSON object: name → type, or {type, min, max, default, description}. Read from the editor above at save time."
        >
          <textarea
            id="rs-params"
            spellCheck={false}
            placeholder='{"id": {"type": "int", "min": 1}}'
            value={params}
            onChange={(e) => setParams(e.target.value)}
          />
        </Field>
        <FormActions onCancel={onClose}>
          <button
            type="button"
            className="btn"
            disabled={save.isPending}
            title="Save without publishing"
            onClick={() => submit(false)}
          >
            Save as draft
          </button>
          <button type="submit" className="btn primary" disabled={save.isPending}>
            Save
          </button>
        </FormActions>
      </form>
    </>
  );
}

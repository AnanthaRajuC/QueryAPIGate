import { lazy, Suspense, useState, type ReactNode } from 'react';
import { useNavigate } from 'react-router';

import {
  useAllQueries,
  useApiKeys,
  useConnections,
  useRoles,
  type ApiKeyEntry,
  type QuerySummary,
  type RoleEntry,
} from '@/app/data';
import { Loading, useFeedback } from '@/app/feedback';
import { useSchema, useTableUsage } from '@/components/SchemaBrowser';
import { reachVia } from '@/features/repository/reach';

import { QueryInfo } from './QueryInfo';
import { amapState, setAmapState, type AmapState } from './state';

// The classic Access map (ui.py #tab-accessmap, renderAccessMap and its helpers): every saved query against every
// API key and role - how each reaches it (Q a named query, C a collection, W a whole connection) - with the
// connection → table drill-down ("which queries already expose this table?"), database and reach filters, a search
// that narrows whichever axis it matches, and sortable columns.

// "New API on <table>" opens the query form, which carries the SQL editor: loaded on first use.
const QueryForm = lazy(() => import('@/features/repository/forms').then((m) => ({ default: m.QueryForm })));

const CODE = { query: 'Q', collection: 'C', connection: 'W' } as const;
const INFO_HEADERS = ['Database', 'Created', 'Last modified', 'Last used', 'Version'];
const INFO_FIELDS = ['database', 'created', 'modified', 'lastUsed', 'version'];

interface Row {
  q: QuerySummary;
  dbType: string | null;
  reach: number;
}

const today = () => new Date().toISOString().slice(0, 10);

export function AccessMapPage() {
  const navigate = useNavigate();
  const { openDrawer } = useFeedback();
  const queries = useAllQueries();
  const keys = useApiKeys();
  const roles = useRoles();
  const connections = useConnections();
  const [st, setSt] = useState<AmapState>(amapState);
  const update = (patch: Partial<AmapState>) => {
    const next = { ...st, ...patch };
    setAmapState(next);
    setSt(next);
  };

  const conns = connections.data ?? [];
  const dbOf = (connection: string | null) => conns.find((c) => c.name === connection)?.db ?? null;
  const schema = useSchema(st.conn || undefined);
  const tables = schema.data?.tables ?? [];
  const usage = useTableUsage(
    st.conn || undefined,
    dbOf(st.conn),
    st.table && !st.tableMatches ? [st.table] : [],
  );
  const tableMatches: string[] | null = !st.table
    ? null
    : (st.tableMatches ?? (usage.data ? (usage.data[st.table] ?? []) : null));

  const all = queries.data ?? [];
  const dbTypes = [
    ...new Set(all.map((q) => dbOf(q.connection_name)).filter((t): t is string => Boolean(t))),
  ].sort();
  const keyMap = keys.data ?? {};
  const roleMap = roles.data ?? {};
  const keyNames = Object.keys(keyMap).sort();
  const roleNames = Object.keys(roleMap).sort();

  // The table select: pick a connection first, then its tables (or why there are none).
  let tableOptions: ReactNode;
  let tableDisabled = true;
  if (!st.conn) tableOptions = <option value="">Select a connection first</option>;
  else if (schema.isPending) tableOptions = <option value="">Loading tables…</option>;
  else if (schema.isError) tableOptions = <option value="">Schema unavailable</option>;
  else {
    tableDisabled = false;
    tableOptions = (
      <>
        <option value="">{`All tables (${tables.length})`}</option>
        {tables.map((t) => (
          <option key={t.name} value={t.name}>
            {`${t.name} · ${t.columns.length}${t.columns.length === 1 ? ' col' : ' cols'}`}
          </option>
        ))}
      </>
    );
  }

  const reaches = (entry: RoleEntry | ApiKeyEntry, q: QuerySummary) =>
    reachVia(entry, q.name, q.collection, q.connection_name);

  let body: ReactNode;
  if (queries.isPending) body = <Loading />;
  else if (!all.length)
    body = (
      <div className="empty">
        <strong>No saved queries yet</strong>
      </div>
    );
  else if (!keyNames.length && !roleNames.length)
    body = (
      <div className="empty">
        <strong>No scoped API keys or roles yet</strong>
        <span>Every query is reachable by the admin key only until a scoped key is created.</span>
      </div>
    );
  else {
    const q = st.q.trim().toLowerCase();
    const allRows = [...all].sort((a, b) =>
      (a.collection || '￿') + a.name < (b.collection || '￿') + b.name ? -1 : 1,
    );
    const match = (name: string) => name.toLowerCase().includes(q);
    // A search narrows whichever axis it matches; an axis it doesn't touch stays whole.
    const rowsMatch = q
      ? allRows.filter((r) => match(r.name) || (r.collection && match(r.collection)))
      : allRows;
    const keyColsMatch = q ? keyNames.filter(match) : keyNames;
    const roleColsMatch = q ? roleNames.filter(match) : roleNames;
    const anyMatch = rowsMatch.length || keyColsMatch.length || roleColsMatch.length;
    let rows = rowsMatch.length ? rowsMatch : anyMatch ? allRows : [];
    const keyCols = keyColsMatch.length ? keyColsMatch : anyMatch ? keyNames : [];
    const roleCols = roleColsMatch.length ? roleColsMatch : anyMatch ? roleNames : [];
    if (st.conn) rows = rows.filter((r) => r.connection_name === st.conn);

    if (st.table && tableMatches === null) {
      body = <Loading text={`Checking which queries reference ${st.table}…`} />;
    } else {
      if (tableMatches) rows = rows.filter((r) => tableMatches.includes(r.name));
      if (!rows.length) {
        const reason = q
          ? `Nothing matches “${q}”.`
          : st.table
            ? `“${st.table}” on ${st.conn} is not exposed by any saved query yet.`
            : st.conn
              ? `No saved query uses “${st.conn}” yet.`
              : 'Nothing matches these filters.';
        const newApi = () => {
          const isMongo = dbOf(st.conn) === 'mongo';
          const sql = st.table
            ? isMongo
              ? JSON.stringify({ collection: st.table, filter: {} }, null, 2)
              : 'SELECT * FROM ' + st.table
            : undefined;
          navigate('/queries');
          openDrawer({
            title: 'New API',
            kicker: 'POST /api/v1/queries',
            content: (
              <Suspense fallback={<Loading />}>
                <QueryForm starter={{ connection: st.conn, sql }} />
              </Suspense>
            ),
          });
        };
        body = (
          <div className="empty">
            <span>{reason}</span>
            {!q && st.conn ? (
              <button type="button" className="btn primary" onClick={newApi}>
                {'New API' + (st.table ? ' on ' + st.table : '')}
              </button>
            ) : null}
          </div>
        );
      } else {
        let enriched: Row[] = rows.map((r) => ({
          q: r,
          dbType: dbOf(r.connection_name),
          reach: keyCols.filter((name) => reaches(keyMap[name]!, r).length).length,
        }));
        if (st.db) enriched = enriched.filter((item) => item.dbType === st.db);
        if (st.reach === 'reachable') enriched = enriched.filter((item) => item.reach > 0);
        else if (st.reach === 'unreachable') enriched = enriched.filter((item) => item.reach === 0);
        const sort = st.sort;
        if (sort.field) {
          const value = (item: Row): string | number => {
            switch (sort.field) {
              case 'query':
                return item.q.name.toLowerCase();
              case 'database':
                return (item.dbType || '').toLowerCase();
              case 'created':
                return item.q.created_at || '';
              case 'modified':
                return item.q.updated_at || '';
              case 'lastUsed':
                return item.q.last_used_at || '';
              case 'version':
                return item.q.latest_version;
              case 'connection':
                return (item.q.connection_name || '').toLowerCase();
              case 'reach':
                return item.reach;
              case 'col': {
                const entry = sort.colType === 'key' ? keyMap[sort.colName!] : roleMap[sort.colName!];
                return entry && reaches(entry, item.q).length ? 1 : 0;
              }
              default:
                return '';
            }
          };
          enriched = [...enriched].sort((a, b) => {
            const va = value(a);
            const vb = value(b);
            return va < vb ? -sort.dir : va > vb ? sort.dir : 0;
          });
        }
        body = !enriched.length ? (
          <div className="empty">
            <span>No queries match these filters.</span>
          </div>
        ) : (
          <Matrix
            rows={enriched}
            keyCols={keyCols}
            roleCols={roleCols}
            keyMap={keyMap}
            roleMap={roleMap}
            st={st}
            onSort={(sort) => update({ sort })}
            onOpen={(name) => navigate(`/queries/${encodeURIComponent(name)}`)}
            onInfo={(row) =>
              openDrawer({
                title: row.q.name,
                kicker: `${row.q.collection || 'uncollected'} · ${row.q.version_count}${row.q.version_count === 1 ? ' version' : ' versions'}`,
                content: <QueryInfo name={row.q.name} dbType={row.dbType} />,
              })
            }
          />
        );
      }
    }
  }

  return (
    <>
      <div className="page-head">
        <div className="titles">
          <h1>Access map</h1>
          <span className="sub">
            Which API keys can reach which saved queries - the whole point of collections and connection
            grants, in one place.{' '}
            <span className="legend">
              <span className="amap-dot">Q</span> named query &nbsp;<span className="amap-dot">C</span>{' '}
              collection &nbsp;<span className="amap-dot conn">W</span> whole connection
            </span>
          </span>
        </div>
        <span className="spacer" />
        <select
          id="accessmap-conn-filter"
          aria-label="Connection"
          value={conns.some((c) => c.name === st.conn) ? st.conn : ''}
          onChange={(e) => update({ conn: e.target.value, table: '', tableMatches: null })}
        >
          <option value="">All connections</option>
          {[...conns]
            .sort((a, b) => (a.name < b.name ? -1 : 1))
            .map((c) => (
              <option key={c.name} value={c.name}>
                {c.name}
              </option>
            ))}
        </select>
        <select
          id="accessmap-table-filter"
          aria-label="Table"
          disabled={tableDisabled}
          value={tables.some((t) => t.name === st.table) ? st.table : ''}
          onChange={(e) => update({ table: e.target.value, tableMatches: null })}
        >
          {tableOptions}
        </select>
        <select
          id="accessmap-db-filter"
          aria-label="Database"
          value={dbTypes.includes(st.db) ? st.db : ''}
          onChange={(e) => update({ db: e.target.value })}
        >
          <option value="">All databases</option>
          {dbTypes.map((t) => (
            <option key={t} value={t}>
              {t}
            </option>
          ))}
        </select>
        <select
          id="accessmap-reach-filter"
          aria-label="Reach"
          value={st.reach}
          onChange={(e) => update({ reach: e.target.value as AmapState['reach'] })}
        >
          <option value="">Any reach</option>
          <option value="reachable">Reachable by a key</option>
          <option value="unreachable">Reachable by no key</option>
        </select>
        <input
          id="accessmap-filter"
          className="search"
          type="search"
          placeholder="Filter by query or key…"
          value={st.q}
          onChange={(e) => update({ q: e.target.value })}
        />
      </div>
      <div id="accessmap-body" className="panel">
        {body}
      </div>
    </>
  );
}

function Matrix({
  rows,
  keyCols,
  roleCols,
  keyMap,
  roleMap,
  st,
  onSort,
  onOpen,
  onInfo,
}: {
  rows: Row[];
  keyCols: string[];
  roleCols: string[];
  keyMap: Record<string, ApiKeyEntry>;
  roleMap: Record<string, RoleEntry>;
  st: AmapState;
  onSort: (sort: AmapState['sort']) => void;
  onOpen: (name: string) => void;
  onInfo: (row: Row) => void;
}) {
  const sort = st.sort;
  const isOn = (field: string, colType: 'key' | 'role' | null = null, colName: string | null = null) =>
    sort.field === field && sort.colType === colType && sort.colName === colName;
  const arrow = (field: string, colType: 'key' | 'role' | null = null, colName: string | null = null) =>
    isOn(field, colType, colName) ? (sort.dir === 1 ? ' ▲' : ' ▼') : '';
  // ascending, then descending, then back to the default order
  const toggle = (field: string, colType: 'key' | 'role' | null = null, colName: string | null = null) => {
    const same = isOn(field, colType, colName);
    onSort(
      same && sort.dir === 1
        ? { field, colType, colName, dir: -1 }
        : same
          ? { field: null, colType: null, colName: null, dir: 1 }
          : { field, colType, colName, dir: 1 },
    );
  };
  const th = (label: string, className: string, field: string) => (
    <th key={field} className={className} onClick={() => toggle(field)}>
      {label + arrow(field)}
    </th>
  );
  const colHead = (name: string, entry: RoleEntry, revoked: string, colType: 'key' | 'role') => (
    <th
      key={colType + ':' + name}
      className={'amap-key-head amap-sortable' + (revoked ? ' revoked' : '')}
      title={revoked ? name + revoked : name}
      onClick={() => toggle('col', colType, name)}
    >
      <span className="amap-key-name">
        {name}
        {arrow('col', colType, name)}
        {revoked ? <span className="dim">{revoked}</span> : null}
      </span>
      <span className="amap-key-sub">
        {(entry.allow_writes ? 'Read/write' : 'Read-only') + ' · ' + (entry.rate_limit || 'server default')}
      </span>
    </th>
  );
  const cell = (entry: RoleEntry, q: QuerySummary, role: boolean, key: string) => {
    const via = reachVia(entry, q.name, q.collection, q.connection_name);
    if (!via.length) return <td key={key} className="amap-cell" />;
    const codes = [...new Set(via.map((v) => CODE[v.kind]))];
    const connOnly = codes.length === 1 && codes[0] === 'W';
    return (
      <td key={key} className="amap-cell" title={via.map((v) => v.label).join(', ')}>
        <span className={'amap-dot' + (role ? ' role' : '') + (connOnly ? ' conn' : '')}>
          {codes.join('·')}
        </span>
      </td>
    );
  };
  const now = today();
  return (
    <div style={{ overflow: 'auto' }}>
      <table className="amap">
        <thead>
          <tr>
            <th colSpan={3 + INFO_HEADERS.length} />
            {keyCols.length ? (
              <th colSpan={keyCols.length} className="amap-group">
                API KEYS
              </th>
            ) : null}
            {roleCols.length ? (
              <th
                colSpan={roleCols.length}
                className="amap-group role"
                title="What a key created from each role would reach - a role grants nothing on its own"
              >
                ROLES
              </th>
            ) : null}
          </tr>
          <tr>
            {th('Query', 'amap-query amap-sortable', 'query')}
            {INFO_FIELDS.map((field, i) => th(INFO_HEADERS[i]!, 'amap-sortable', field))}
            {th('Connection', 'amap-sortable', 'connection')}
            {th('Reach', 'num amap-sortable', 'reach')}
            {keyCols.map((name) => {
              const k = keyMap[name]!;
              const expired = Boolean(k.expires_at && k.expires_at < now);
              return colHead(
                name,
                k,
                !k.active ? ' (revoked)' : expired ? ` (expired ${k.expires_at})` : '',
                'key',
              );
            })}
            {roleCols.map((name) => colHead(name, roleMap[name]!, '', 'role'))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const q = row.q;
            return (
              <tr key={q.name}>
                <td className="amap-query">
                  <div className="amap-q-row">
                    <button
                      type="button"
                      className="amap-q-name"
                      title={`Open ${q.name} in API Repository`}
                      onClick={() => onOpen(q.name)}
                    >
                      {q.name}
                    </button>
                    <button
                      type="button"
                      className="amap-info"
                      title={`Details for ${q.name}`}
                      aria-label={`Details for ${q.name}`}
                      onClick={() => onInfo(row)}
                    >
                      i
                    </button>
                  </div>
                  {q.collection ? <span className="amap-coll">{q.collection}</span> : null}
                </td>
                <td>
                  {row.dbType ? <span className="tag">{row.dbType}</span> : <span className="dim">—</span>}
                </td>
                <td className="mono dim">{q.created_at || '—'}</td>
                <td className="mono dim">{q.updated_at || '—'}</td>
                <td className={q.last_used_at ? 'mono' : 'mono dim'}>{q.last_used_at || 'never'}</td>
                <td className="mono">{'v' + q.latest_version}</td>
                <td className="mono dim">{q.connection_name || '—'}</td>
                <td className="num" title="Reachable by an actual API key (roles alone reach nothing)">
                  {row.reach ? String(row.reach) : '—'}
                </td>
                {keyCols.map((name) => cell(keyMap[name]!, q, false, 'k:' + name))}
                {roleCols.map((name) => cell(roleMap[name]!, q, true, 'r:' + name))}
              </tr>
            );
          })}
        </tbody>
        <tfoot>
          <tr>
            <td className="amap-query">Reaches</td>
            {INFO_HEADERS.map((h) => (
              <td key={h} />
            ))}
            <td />
            <td />
            {keyCols.map((name) => (
              <td key={name} className="num">
                {`${rows.filter((r) => reachVia(keyMap[name]!, r.q.name, r.q.collection, r.q.connection_name).length).length} / ${rows.length}`}
              </td>
            ))}
            {roleCols.map((name) => (
              <td key={name} className="num">
                {`${rows.filter((r) => reachVia(roleMap[name]!, r.q.name, r.q.collection, r.q.connection_name).length).length} / ${rows.length}`}
              </td>
            ))}
          </tr>
        </tfoot>
      </table>
    </div>
  );
}

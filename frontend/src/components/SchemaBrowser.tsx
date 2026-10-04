import { useQuery } from '@tanstack/react-query';
import { useState, type ReactNode, type RefObject } from 'react';
import { useNavigate } from 'react-router';

import { api, apiJson, unwrap } from '@/api/client';
import { useAllQueries, useApiKeys, useRoles } from '@/app/data';
import { copyText, Field, Loading, useFeedback } from '@/app/feedback';
import { openClassic } from '@/app/navigation';
import { CodeBox } from '@/components/CodeBox';
import { AccessPill, ReachDot, queryReach, type Reach } from '@/features/repository/reach';
import { formatSql, PARSEABLE_DIALECTS } from '@/lib/sql';

import type { SqlEditorHandle } from './SqlEditor';

// The classic schema browser (ui.py schemaBrowser): Tables, then a table's Columns. Click a column to insert it;
// ⧉ writes a starter query, 👁 previews the table in the API Designer, ⌸ shows its CREATE TABLE, and a Q/C/W
// badge says which saved queries touch it and who can reach them. Each extra is opt-in per caller, as before:
// the drawer form uses insert/⧉/👁, the API Designer everything.

const DDL_DIALECTS = ['mysql', 'sqlite', 'clickhouse'];

type SchemaTable = {
  name: string;
  schema?: string | null;
  type: string;
  columns: {
    name: string;
    type?: string | null;
    nullable?: boolean;
    primary_key?: boolean;
    foreign_key?: unknown;
  }[];
};

export function useSchema(connection: string | undefined, database?: string | null) {
  return useQuery({
    queryKey: ['connections', connection, 'schema', database ?? null],
    queryFn: async () =>
      unwrap(
        await api.GET('/api/v1/connections/{name}/schema', {
          params: { path: { name: connection ?? '' }, query: database ? { database } : {} },
        }),
      ),
    enabled: Boolean(connection),
    staleTime: 5 * 60_000,
    retry: false,
  });
}

export interface SchemaBrowserProps {
  connection: string;
  dialect?: string | null;
  /** Another database on the same server, when the API Designer's Database picker points there. */
  database?: string | null;
  editor: RefObject<SqlEditorHandle | null>;
  isMongo: boolean;
  onPreview?: (table: string) => void;
  showDdl?: boolean;
  showUsage?: boolean;
}

export function SchemaField(props: SchemaBrowserProps) {
  const schema = useSchema(props.connection || undefined, props.database);
  return (
    <Field
      label="Schema"
      extra={
        <button type="button" className="btn ghost sm" title="Refresh" onClick={() => void schema.refetch()}>
          ↻
        </button>
      }
    >
      <SchemaBrowser {...props} />
    </Field>
  );
}

function SchemaBrowser({
  connection,
  dialect,
  database,
  editor,
  isMongo,
  onPreview,
  showDdl,
  showUsage,
}: SchemaBrowserProps) {
  const { openDrawer } = useFeedback();
  const schema = useSchema(connection || undefined, database);
  const [table, setTable] = useState<string | null>(null);
  const [view, setView] = useState<'tables' | 'columns'>('tables');
  const tables = (schema.data?.tables ?? []) as SchemaTable[];
  const active = tables.find((t) => t.name === table);
  const usage = useTableUsage(
    showUsage ? connection : undefined,
    dialect,
    tables.map((t) => t.name),
  );
  const keys = useApiKeys();
  const roles = useRoles();
  const queries = useAllQueries();

  let content: ReactNode;
  if (!connection) content = <div className="hint">Pick a connection to browse its schema.</div>;
  else if (schema.isPending) content = <Loading text="Loading schema…" />;
  else if (schema.isError) content = <div className="hint">{schema.error.message}</div>;
  else if (!tables.length) content = <div className="hint">No tables found.</div>;
  else {
    const ddl = showDdl && dialect && DDL_DIALECTS.includes(dialect);
    content = (
      <>
        <div className="minitabs">
          <button
            type="button"
            className={view === 'tables' ? 'minitab active' : 'minitab'}
            onClick={() => setView('tables')}
          >
            Tables
          </button>
          <button
            type="button"
            className={view === 'columns' ? 'minitab active' : 'minitab'}
            disabled={!table}
            onClick={() => table && setView('columns')}
          >
            {'Columns' + (table ? ' · ' + table : '')}
          </button>
        </div>
        <div className="schema-content">
          {view === 'columns' && active ? (
            active.columns.length ? (
              active.columns.map((c) => {
                let title = `${c.type ?? ''}${c.nullable ? ' · nullable' : ' · not null'}`;
                if (c.primary_key) title += ' · primary key';
                const fk = c.foreign_key as { table: string; column: string } | null | undefined;
                if (fk) title += ` · FK → ${fk.table}.${fk.column}`;
                return (
                  <button
                    key={c.name}
                    type="button"
                    className="schema-col"
                    title={title}
                    onClick={() => editor.current?.insert(c.name)}
                  >
                    <span>{c.name}</span>
                    <span style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                      <span className="dim">{c.type}</span>
                      {c.primary_key ? <span className="tag key-pk">PK</span> : null}
                      {fk ? <span className="tag key-fk">{`FK → ${fk.table}.${fk.column}`}</span> : null}
                    </span>
                  </button>
                );
              })
            ) : (
              <div className="hint">No columns to show.</div>
            )
          ) : (
            tables.map((t) => {
              const users = usage.data?.[t.name];
              let badge: ReactNode = null;
              if (users && users.length) {
                const reach = tableReach(users, keys.data ?? {}, roles.data ?? {}, queries.data ?? []);
                const allVia = [...reach.keys, ...reach.roles].flatMap((e) => e.via);
                const count = users.length + (users.length === 1 ? ' query' : ' queries');
                const title = `Used by ${count}${
                  allVia.length
                    ? ` - click for who can reach ${users.length === 1 ? 'it' : 'them'}`
                    : ` - only the admin key can run ${users.length === 1 ? 'it' : 'them'}`
                }`;
                badge = (
                  <button
                    type="button"
                    className="schema-usage"
                    title={title}
                    aria-label={title}
                    onClick={() =>
                      openDrawer({
                        title: t.name,
                        kicker: `${connection} · used by ${count}`,
                        wide: true,
                        content: (
                          <TableUsage connection={connection} table={t.name} users={users} reach={reach} />
                        ),
                      })
                    }
                  >
                    {allVia.length ? <ReachDot via={allVia} /> : <span className="amap-dot muted" />}
                  </button>
                );
              }
              return (
                <div className="schema-row" key={t.name}>
                  <button
                    type="button"
                    className="schema-table"
                    title={'Columns of ' + t.name}
                    onClick={() => {
                      setTable(t.name);
                      setView('columns');
                    }}
                  >
                    <span className="name">{t.name}</span>
                    <span className="tag">{t.type}</span>
                  </button>
                  <button
                    type="button"
                    className="schema-select"
                    title={'Copy a starter query for ' + t.name + ' into the editor'}
                    aria-label={'Copy a starter query for ' + t.name}
                    onClick={() =>
                      editor.current?.replace(
                        isMongo
                          ? JSON.stringify({ collection: t.name, filter: {}, sort: { _id: 1 } }, null, 2)
                          : buildSqlSelect(t),
                      )
                    }
                  >
                    ⧉
                  </button>
                  {onPreview && (
                    <button
                      type="button"
                      className="schema-preview"
                      title={'Preview ' + t.name + ' in API Designer'}
                      aria-label={'Preview ' + t.name}
                      onClick={() => onPreview(t.name)}
                    >
                      👁
                    </button>
                  )}
                  {ddl && (
                    <button
                      type="button"
                      className="schema-ddl"
                      title={`Show ${t.name}’s CREATE TABLE statement`}
                      aria-label={`Show ${t.name}’s CREATE TABLE statement`}
                      onClick={() =>
                        openDrawer({
                          title: t.name,
                          kicker: `${connection} · CREATE TABLE`,
                          content: (
                            <TableDdl connection={connection} table={t.name} database={database ?? null} />
                          ),
                        })
                      }
                    >
                      ⌸
                    </button>
                  )}
                  {badge}
                </div>
              );
            })
          )}
          {schema.data?.truncated && <div className="hint">Showing the first 5000 columns.</div>}
        </div>
      </>
    );
  }
  return <div className="schema-browser">{content}</div>;
}

function pickAlias(name: string, used: Record<string, string>) {
  const lower = name.toLowerCase();
  for (const candidate of [lower.charAt(0), lower.slice(0, 2), lower]) if (!used[candidate]) return candidate;
  return `${lower}_${Object.keys(used).length}`;
}

/** A starter SELECT: every column by name, a JOIN per foreign key, ordered, LIMIT 100 (ui.py buildSqlSelect). */
export function buildSqlSelect(table: Pick<SchemaTable, 'name' | 'schema' | 'columns'>) {
  const fkCols = table.columns.filter((c) => c.foreign_key);
  const used: Record<string, string> = {};
  const mainAlias = fkCols.length ? pickAlias(table.name, used) : null;
  if (mainAlias) used[mainAlias] = table.name;
  const joins = fkCols.map((c) => {
    const fk = c.foreign_key as { table: string; column: string };
    const alias = pickAlias(fk.table, used);
    used[alias] = fk.table;
    return `JOIN ${fk.table} ${alias} ON ${mainAlias}.${c.name} = ${alias}.${fk.column}`;
  });
  const prefix = mainAlias ? mainAlias + '.' : '';
  const cols = table.columns.length ? table.columns.map((c) => '  ' + prefix + c.name).join(',\n') : '  *';
  const target = (table.schema ? table.schema + '.' : '') + table.name + (mainAlias ? ' ' + mainAlias : '');
  let sql = 'SELECT\n' + cols + '\nFROM ' + target;
  if (joins.length) sql += '\n' + joins.join('\n');
  if (table.columns.length) sql += '\nORDER BY ' + prefix + table.columns[0]!.name;
  return sql + '\nLIMIT 100';
}

// ---- Which saved queries touch each table (ui.py buildTableUsageIndex) ----

interface Flow {
  tables: string[];
  error?: string | null;
  formatted?: string | null;
}

type Detail = {
  name: string;
  collection: string | null;
  connection: string | null;
  version: number;
  sql: string;
};

async function queryDetail(name: string): Promise<Detail | null> {
  try {
    const q = unwrap(await api.GET('/api/v1/queries/{name}', { params: { path: { name } } }));
    const v = q.versions.find((x) => x.version === q.published_version) ?? q.versions[q.versions.length - 1]!;
    const sql = v.query_type === 'mongo' ? JSON.stringify(v.mongo ?? {}) : (v.sql ?? '');
    return { name: q.name, collection: q.collection, connection: v.connection_name, version: v.version, sql };
  } catch {
    return null;
  }
}

/** {table: [query names]} for the queries on `connection`: real parsing (query_flow) where the dialect allows,
 * a whole-word search of the SQL otherwise. */
function useTableUsage(
  connection: string | undefined,
  dialect: string | null | undefined,
  tableNames: string[],
) {
  const queries = useAllQueries();
  const candidates = (queries.data ?? []).filter((q) => q.connection_name === connection).map((q) => q.name);
  return useQuery({
    queryKey: ['table-usage', connection, tableNames.join(','), candidates.join(',')],
    enabled: Boolean(connection) && tableNames.length > 0 && queries.isSuccess,
    staleTime: 60_000,
    queryFn: async () => {
      const index: Record<string, string[]> = Object.fromEntries(tableNames.map((t) => [t, [] as string[]]));
      const parseable = Boolean(dialect && PARSEABLE_DIALECTS.includes(dialect));
      await Promise.all(
        candidates.map(async (name) => {
          const d = await queryDetail(name);
          if (!d) return;
          if (parseable) {
            try {
              const flow = await apiJson<Flow>(
                `/query_flow?filename=${encodeURIComponent(name)}&version=${d.version}`,
              );
              if (!flow.error) {
                flow.tables.forEach((ft) => {
                  const match = tableNames.find((t) => t.toLowerCase() === ft.toLowerCase());
                  if (match && !index[match]!.includes(name)) index[match]!.push(name);
                });
                return;
              }
            } catch {
              // fall through to the text search
            }
          }
          tableNames.forEach((t) => {
            const needle = new RegExp('\\b' + t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '\\b', 'i');
            if (needle.test(d.sql) && !index[t]!.includes(name)) index[t]!.push(name);
          });
        }),
      );
      return index;
    },
  });
}

/** Every key and role reaching any of `names`, their ways in unioned (ui.py tableReachSummary). */
function tableReach(
  names: string[],
  keys: Parameters<typeof queryReach>[0],
  roles: Parameters<typeof queryReach>[1],
  list: { name: string; collection: string | null; connection_name: string | null }[],
): Reach {
  const acc: { keys: Record<string, Reach['keys'][number]>; roles: Record<string, Reach['roles'][number]> } =
    { keys: {}, roles: {} };
  names.forEach((n) => {
    const s = list.find((q) => q.name === n);
    if (!s) return;
    const r = queryReach(keys, roles, n, s.collection, s.connection_name);
    r.keys.forEach((k) => (acc.keys[k.name] = { ...k, via: [...(acc.keys[k.name]?.via ?? []), ...k.via] }));
    r.roles.forEach(
      (k) => (acc.roles[k.name] = { ...k, via: [...(acc.roles[k.name]?.via ?? []), ...k.via] }),
    );
  });
  return { keys: Object.values(acc.keys), roles: Object.values(acc.roles) };
}

// ---- Drawers ----

function TableDdl({
  connection,
  table,
  database,
}: {
  connection: string;
  table: string;
  database: string | null;
}) {
  const { closeDrawer, toast } = useFeedback();
  const ddl = useQuery({
    queryKey: ['ddl', connection, table, database],
    queryFn: () =>
      apiJson<{ ddl: string }>(
        `/connections/${encodeURIComponent(connection)}/table_ddl?table=${encodeURIComponent(table)}${database ? '&database=' + encodeURIComponent(database) : ''}`,
      ),
    retry: false,
  });
  if (ddl.isPending) return <Loading />;
  if (ddl.isError) return <div className="hint">Could not load this table’s DDL - {ddl.error.message}</div>;
  return (
    <>
      <CodeBox sql={ddl.data.ddl} />
      <div className="form-actions">
        <button type="button" className="btn" onClick={() => copyText(ddl.data.ddl, toast)}>
          Copy
        </button>
        <button type="button" className="btn" onClick={closeDrawer}>
          Close
        </button>
      </div>
    </>
  );
}

function TableUsage({
  table,
  users,
  reach,
}: {
  connection: string;
  table: string;
  users: string[];
  reach: Reach;
}) {
  const { closeDrawer } = useFeedback();
  const sorted = [...users].sort();
  return (
    <>
      <p className="sub-h">Queries</p>
      <div className="table-usage-queries">
        {sorted.map((name) => (
          <UsageQuery key={name} name={name} />
        ))}
      </div>
      <p className="sub-h" style={{ marginTop: 14 }}>
        Access
      </p>
      {!reach.keys.length && !reach.roles.length ? (
        <div className="hint">Only the admin key can run queries touching this table.</div>
      ) : (
        <>
          {reach.keys.length > 0 && (
            <div className="access-reach">
              {reach.keys.map((k) => (
                <AccessPill key={k.name} entry={k} />
              ))}
            </div>
          )}
          {reach.roles.length > 0 && (
            <div className="access-roles hint" style={{ marginTop: 8 }}>
              {'Also granted to role' + (reach.roles.length > 1 ? 's' : '') + ': '}
              <div className="access-reach" style={{ display: 'inline-flex', marginLeft: 4 }}>
                {reach.roles.map((r) => (
                  <AccessPill key={r.name} entry={r} role />
                ))}
              </div>
            </div>
          )}
        </>
      )}
      <div className="form-actions">
        <button
          type="button"
          className="btn"
          title={`Opens the Access map in the classic UI, for ${table}`}
          onClick={() => {
            closeDrawer();
            openClassic('accessmap');
          }}
        >
          View in Access map
        </button>
        <button type="button" className="btn" onClick={closeDrawer}>
          Close
        </button>
      </div>
    </>
  );
}

function UsageQuery({ name }: { name: string }) {
  const { closeDrawer } = useFeedback();
  const navigate = useNavigate();
  const detail = useQuery({ queryKey: ['usage-query', name], queryFn: () => queryDetail(name) });
  const flow = useQuery({
    queryKey: ['queries', 'flow', name, detail.data?.version],
    queryFn: () =>
      apiJson<Flow>(`/query_flow?filename=${encodeURIComponent(name)}&version=${detail.data!.version}`),
    enabled: Boolean(detail.data && !detail.data.sql.includes('\n')),
  });
  let body: ReactNode = <div className="hint">{`Loading ${name}…`}</div>;
  if (detail.isSuccess && !detail.data) body = <div className="hint">This query no longer exists.</div>;
  else if (detail.data) {
    const sql = detail.data.sql.includes('\n')
      ? detail.data.sql
      : (flow.data?.formatted ?? formatSql(detail.data.sql));
    body = <CodeBox sql={sql} />;
  }
  return (
    <div className="table-usage-query">
      <button
        type="button"
        className="target"
        onClick={() => {
          closeDrawer();
          navigate(`/queries/${encodeURIComponent(name)}`);
        }}
      >
        {name}
      </button>
      {body}
    </div>
  );
}

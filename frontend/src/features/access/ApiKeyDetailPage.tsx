import { useQuery } from '@tanstack/react-query';
import { Link, useParams } from 'react-router';

import { api, unwrap } from '@/api/client';
import { useAllQueries, useApiKeys, useConnections } from '@/app/data';
import { Loading, useFeedback } from '@/app/feedback';
import { Time } from '@/components/Time';
import { QueryInfo } from '@/features/accessmap/QueryInfo';
import { reachVia, type Via } from '@/features/repository/reach';

import { ActivitySpark } from './KeyActivity';
import { useKeyQueryActivity } from './activity';

// A single API key in full (reached from the Details button on the API keys table, /api-keys/:name): the APIs
// (saved queries) and connections it can reach, the tables and write surface it is allowed, its limits, and its
// usage history (/api/v1/history?key=<name>).

const VIA_LABEL: Record<Via['kind'], string> = {
  query: 'named query',
  collection: 'collection',
  connection: 'connection',
};

function Fact({ term, children }: { term: string; children: React.ReactNode }) {
  return (
    <div>
      <dt>{term}</dt>
      <dd>{children}</dd>
    </div>
  );
}

function Panel({ title, sub, children }: { title: string; sub?: string; children: React.ReactNode }) {
  return (
    <section className="panel" style={{ marginTop: 16 }}>
      <div className="panel-bar">
        <strong>{title}</strong>
        {sub ? <span className="dim">{sub}</span> : null}
      </div>
      {children}
    </section>
  );
}

export function ApiKeyDetailPage() {
  const { name = '' } = useParams();
  const keys = useApiKeys();
  const queries = useAllQueries();
  const connections = useConnections();
  const perApi = useKeyQueryActivity(name);
  const { openDrawer } = useFeedback();
  const today = new Date().toISOString().slice(0, 10);

  // Usage history for this key only. The log may be unreadable for a role without `observe` - treated as "none".
  const history = useQuery({
    queryKey: ['apikey-history', name],
    queryFn: async () =>
      unwrap(await api.GET('/api/v1/history', { params: { query: { key: name, limit: 50 } } })).items,
    retry: false,
  });

  if (keys.isPending) return <Loading text="Loading API key…" />;

  const k = keys.data?.[name];
  if (!k) {
    return (
      <>
        <div className="page-head">
          <div className="titles">
            <Link to="/api-keys" className="sub">
              ← API keys
            </Link>
            <h1>{name || 'API key'}</h1>
          </div>
        </div>
        <div className="panel">
          <div className="empty">
            <strong>No such API key</strong>
            <span>It may have been revoked and removed, or the name is wrong.</span>
            <Link to="/api-keys" className="btn sm">
              Back to API keys
            </Link>
          </div>
        </div>
      </>
    );
  }

  const expired = Boolean(k.expires_at && k.expires_at < today);
  const status = !k.active ? 'Revoked' : expired ? 'Expired' : 'Active';

  // APIs this key can reach: every saved query a grant covers (a named `queries` grant, a `collections` grant over
  // its collection, or a `connections` grant over its connection) - the same rule the reach column uses.
  const reachable = (queries.data ?? [])
    .map((q) => ({ q, via: reachVia(k, q.name, q.collection, q.connection_name) }))
    .filter((r) => r.via.length);

  // Connections (databases) it can reach: a `connections: '*'` grant is every one; otherwise the named grants plus
  // the connections of the saved queries above (a query or collection grant reaches that query's connection).
  const allConnNames = (connections.data ?? []).map((c) => c.name);
  const direct = k.connections === '*' ? allConnNames : (k.connections ?? []);
  const viaQueries = reachable
    .map((r) => r.q.connection_name)
    .filter((n): n is string => n != null && !direct.includes(n));
  const reachedConns = [...new Set([...direct, ...viaQueries])].sort();
  const dbType = (n: string) => (connections.data ?? []).find((c) => c.name === n)?.db ?? null;

  // Clicking a query in "APIs it can reach" opens its SQL (and versions) in the shared query-details drawer.
  const openQuery = (q: { name: string; collection: string | null; connection_name: string | null }) =>
    openDrawer({
      title: q.name,
      kicker: `${q.collection || 'uncollected'} · ${q.connection_name || 'no connection'}`,
      content: <QueryInfo name={q.name} dbType={q.connection_name ? dbType(q.connection_name) : null} />,
    });

  const tables = k.allowed_tables ?? [];
  const ips = k.allowed_ips ?? [];
  const period = k.usage_this_period;

  return (
    <>
      <div className="page-head">
        <div className="titles">
          <Link to="/api-keys" className="sub">
            ← API keys
          </Link>
          <h1 style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <span className={`dot ${k.active && !expired ? 'ok' : 'off'}`} title={status} />
            {name}
            {k.example ? <span className="tag">example</span> : null}
          </h1>
          <span className="sub">
            {status}
            {k.created_from_role ? ` · created from role ${k.created_from_role}` : ''}
          </span>
        </div>
        <span className="spacer" />
        <Link to="/api-keys" className="btn sm">
          Back
        </Link>
      </div>

      <dl className="meta">
        <Fact term="status">{status}</Fact>
        <Fact term="access">{k.allow_writes ? 'Read / write' : 'Read-only'}</Fact>
        <Fact term="created">
          <Time value={k.created_at} />
        </Fact>
        <Fact term="last used">
          <Time value={k.last_used_at} fallback="never" />
        </Fact>
        <Fact term="expires">{k.expires_at ? k.expires_at + (expired ? ' (expired)' : '') : 'never'}</Fact>
        <Fact term="rate limit">{k.rate_limit || 'server default'}</Fact>
        <Fact term="query timeout">
          {k.effective_limits?.query_timeout != null
            ? `${k.effective_limits.query_timeout}s`
            : 'server default'}
        </Fact>
        <Fact term="row limit (page)">{k.effective_limits?.max_rows?.page ?? '—'}</Fact>
        <Fact term="max concurrent">{k.effective_limits?.max_concurrent ?? 'unlimited'}</Fact>
        <Fact term="allowed IPs">{ips.length ? ips.join(', ') : 'any'}</Fact>
      </dl>

      <Panel
        title="APIs it can reach"
        sub={
          k.queries === '*'
            ? 'all saved queries'
            : `${reachable.length} saved ${reachable.length === 1 ? 'query' : 'queries'}`
        }
      >
        {k.queries === '*' ? (
          <div className="empty">
            <strong>Every saved query</strong>
            <span>This key carries a wildcard query grant, so it can run any saved query.</span>
          </div>
        ) : reachable.length ? (
          <div style={{ overflowX: 'auto' }}>
            <table className="grid">
              <thead>
                <tr>
                  {['API endpoint', 'Query', 'Connection', 'Collection', 'Granted via', 'Activity (30d)'].map(
                    (t) => (
                      <th key={t}>{t}</th>
                    ),
                  )}
                </tr>
              </thead>
              <tbody>
                {reachable.map(({ q, via }) => (
                  <tr key={q.name} data-query={q.name}>
                    <td className="mono">
                      {q.published_version ? (
                        q.endpoint
                      ) : (
                        <span className="dim" title="No published version yet">
                          {q.endpoint} (draft)
                        </span>
                      )}
                    </td>
                    <td>
                      <button
                        type="button"
                        className="as-link name"
                        title={`Show the SQL for ${q.name}`}
                        onClick={() => openQuery(q)}
                      >
                        {q.name}
                      </button>
                    </td>
                    <td className="dim">{q.connection_name || '—'}</td>
                    <td className="dim">{q.collection || '—'}</td>
                    <td>
                      {via.map((v, i) => (
                        <span key={i} className="tag" style={{ marginRight: 4 }}>
                          {v.kind === 'query' ? VIA_LABEL.query : v.label}
                        </span>
                      ))}
                    </td>
                    <td className="spark-cell">
                      <ActivitySpark
                        label={q.name}
                        items={perApi.by[q.name]}
                        isPending={perApi.isPending}
                        isError={perApi.isError}
                      />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="empty">
            <strong>No saved queries</strong>
            <span>
              This key reaches no saved query by name or collection. It may still run ad-hoc SQL on the
              connections below.
            </span>
          </div>
        )}
      </Panel>

      <Panel
        title="Databases it can reach"
        sub={k.connections === '*' ? 'all connections' : `${reachedConns.length} connection(s)`}
      >
        {reachedConns.length ? (
          <div style={{ overflowX: 'auto' }}>
            <table className="grid">
              <thead>
                <tr>
                  {['Connection', 'Type', 'Reached by'].map((t) => (
                    <th key={t}>{t}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {reachedConns.map((n) => (
                  <tr key={n} data-conn={n}>
                    <td className="name">{n}</td>
                    <td>
                      {dbType(n) ? <span className="tag">{dbType(n)}</span> : <span className="dim">—</span>}
                    </td>
                    <td className="dim">
                      {k.connections === '*'
                        ? 'all connections'
                        : direct.includes(n)
                          ? 'direct grant (ad-hoc SQL)'
                          : 'saved query only'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="empty">
            <strong>No connections</strong>
            <span>This key cannot reach any connection directly or through a saved query.</span>
          </div>
        )}
      </Panel>

      <Panel
        title="Tables it can reach"
        sub={tables.length ? `${tables.length} allowed` : 'all tables on reachable connections'}
      >
        {tables.length ? (
          <div style={{ padding: '12px 14px', display: 'flex', flexWrap: 'wrap', gap: 6 }}>
            {tables.map((t) => (
              <span key={t} className="tag mono">
                {t}
              </span>
            ))}
          </div>
        ) : (
          <div style={{ padding: '12px 14px' }} className="dim">
            No table allow-list — this key may read every table on the connections it reaches.
          </div>
        )}
        {k.allow_writes ? (
          <div style={{ padding: '0 14px 12px' }}>
            <span className="tag">
              writes: {k.allowed_write_ops?.length ? k.allowed_write_ops.join(', ') : 'all operations'}
            </span>
          </div>
        ) : null}
        {k.mask?.length ? (
          <div style={{ padding: '0 14px 12px', display: 'flex', flexWrap: 'wrap', gap: 6 }}>
            {k.mask.map((m, i) => (
              <span key={i} className="tag" title={`${m.column}: ${m.action}`}>
                masked {m.column}
              </span>
            ))}
          </div>
        ) : null}
      </Panel>

      <Panel
        title="Usage history"
        sub={
          period
            ? `${k.usage.queries} queries total · ${k.usage.rows.toLocaleString()} rows`
            : `${k.usage.queries} queries · ${k.usage.errors} failed · since this process started`
        }
      >
        {history.isPending ? (
          <Loading text="Loading usage…" />
        ) : history.isError ? (
          <div style={{ padding: '12px 14px' }} className="dim">
            Usage history isn’t available for this key (the run log may be off, or your role can’t read it).
          </div>
        ) : history.data!.length ? (
          <div style={{ overflowX: 'auto' }}>
            <table className="grid">
              <thead>
                <tr>
                  {['When', 'What', 'Connection', 'Via', 'Rows', 'Duration', 'Result'].map((t) => (
                    <th key={t}>{t}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {history.data!.map((h, i) => (
                  <tr key={h.request_id ?? i}>
                    <td style={{ whiteSpace: 'nowrap' }}>
                      <Time value={h.executed_at} />
                    </td>
                    <td className="name">
                      {h.kind === 'adhoc' ? <span className="dim">ad-hoc SQL</span> : h.query}
                    </td>
                    <td className="dim">{h.connection_name || '—'}</td>
                    <td className="dim">{h.transport || '—'}</td>
                    <td className="mono">{h.rows ?? '—'}</td>
                    <td className="mono dim" style={{ whiteSpace: 'nowrap' }}>
                      {h.duration_ms != null ? `${h.duration_ms} ms` : '—'}
                    </td>
                    <td style={h.status === 'error' ? { color: 'var(--danger)' } : undefined}>
                      {h.status === 'error' ? (
                        <span title={h.error ?? undefined}>{h.code || 'error'}</span>
                      ) : (
                        'ok'
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="empty">
            <strong>No runs recorded</strong>
            <span>This key hasn’t run a query yet, or its runs have aged out of the log.</span>
          </div>
        )}
      </Panel>
    </>
  );
}

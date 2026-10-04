import { useQuery } from '@tanstack/react-query';
import { useNavigate } from 'react-router';

import { api, unwrap } from '@/api/client';
import { copyText, Loading, useFeedback } from '@/app/feedback';
import { CodeBox } from '@/components/CodeBox';
import { fetchFlow } from '@/features/repository/api';
import { Time } from '@/components/Time';

// The Access map's query details drawer (ui.py openQueryInfo): everything about one saved query that the matrix
// leaves out - its meta, the latest version's SQL and every version with its runs.

function MetaItem({ term, children }: { term: string; children: React.ReactNode }) {
  return (
    <div>
      <dt>{term}</dt>
      <dd title={typeof children === 'string' ? children : undefined}>{children}</dd>
    </div>
  );
}

export function QueryInfo({ name, dbType }: { name: string; dbType: string | null }) {
  const navigate = useNavigate();
  const { closeDrawer, toast } = useFeedback();
  const detail = useQuery({
    queryKey: ['queries', 'detail', name, 'info'],
    queryFn: async () => unwrap(await api.GET('/api/v1/queries/{name}', { params: { path: { name } } })),
    retry: false,
  });
  const query = detail.data;
  const latest = query?.versions[query.versions.length - 1];
  const raw =
    latest?.query_type === 'mongo' ? JSON.stringify(latest.mongo ?? {}, null, 2) : (latest?.sql ?? '');
  // A query written on one line (as the examples are) is an unreadable wall; reformat only those - a query
  // someone laid out by hand keeps its layout.
  const flow = useQuery({
    queryKey: ['queries', 'flow', name, latest?.version],
    queryFn: () => fetchFlow(name, latest!.version),
    enabled: Boolean(latest && latest.query_type === 'sql' && !raw.includes('\n')),
    staleTime: Infinity,
  });
  if (detail.isPending) return <Loading />;
  if (!query || !latest) return <div className="hint">Could not load the SQL.</div>;
  const display = flow.data?.formatted || raw;
  const created = query.created_at;
  const lastUsed = query.last_used_at;

  return (
    <div className="form">
      {latest.description ? <p className="d-desc">{latest.description}</p> : null}
      <dl className="meta">
        <MetaItem term="connection">{latest.connection_name || '—'}</MetaItem>
        <MetaItem term="database">
          {dbType ? <span className="tag">{dbType}</span> : <span className="dim">—</span>}
        </MetaItem>
        <MetaItem term="collection">{query.collection || '—'}</MetaItem>
        <MetaItem term="status">{latest.status || '—'}</MetaItem>
        <MetaItem term="author">{latest.author || '—'}</MetaItem>
        <MetaItem term="created">
          <Time value={created} />
        </MetaItem>
        <MetaItem term="last modified">
          <Time value={latest.last_modified_at || latest.created_at} />
        </MetaItem>
        <MetaItem term="last used">
          <Time value={lastUsed} fallback="never" />
        </MetaItem>
        {latest.tags.length ? <MetaItem term="tags">{latest.tags.join(', ')}</MetaItem> : null}
      </dl>
      <p className="sub-h" style={{ marginTop: 6 }}>
        {`SQL · v${latest.version}${query.versions.length > 1 ? ' (latest)' : ''}`}
      </p>
      <div>
        {flow.isFetching ? (
          <Loading text="Loading SQL…" />
        ) : (
          <>
            <CodeBox sql={display} />
            <div style={{ marginTop: 6 }}>
              <button type="button" className="btn sm ghost" onClick={() => copyText(display, toast)}>
                Copy SQL
              </button>
            </div>
          </>
        )}
      </div>
      <p className="sub-h" style={{ marginTop: 6 }}>
        Versions
      </p>
      <div className="panel" style={{ overflow: 'auto' }}>
        <table className="grid qi-versions">
          <thead>
            <tr>
              {['Version', 'Created', 'Last modified', 'Runs'].map((t) => (
                <th key={t}>{t}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {[...query.versions].reverse().map((v) => (
              <tr key={v.version}>
                <td className="mono">{'v' + v.version}</td>
                <td className="mono dim">
                  <Time value={v.created_at} />
                </td>
                <td className="mono dim">
                  <Time value={v.last_modified_at} />
                </td>
                <td className="mono" style={{ textAlign: 'right' }}>
                  {String(v.run_count ?? 0)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="form-actions">
        <button type="button" className="btn" onClick={closeDrawer}>
          Close
        </button>
        <button
          type="button"
          className="btn primary"
          onClick={() => {
            closeDrawer();
            navigate(`/queries/${encodeURIComponent(name)}`);
          }}
        >
          Open in API Repository
        </button>
      </div>
    </div>
  );
}

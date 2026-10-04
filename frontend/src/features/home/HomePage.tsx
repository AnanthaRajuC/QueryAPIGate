import { useQuery, useQueryClient } from '@tanstack/react-query';
import { lazy, Suspense, useEffect, useState } from 'react';
import { useNavigate } from 'react-router';

import { api, apiFetch, unwrap, type Schemas } from '@/api/client';
import { useAllQueries, useApiKeys, useConnections, useRoles } from '@/app/data';
import { Loading, useFeedback } from '@/app/feedback';
import { useMetricsSeries, usePoolPoll } from '@/app/metrics';
import { StatTile } from '@/components/StatTile';
import { metricGauge, metricSum } from '@/lib/metrics';
import { useAlertFeed } from '@/app/alerts';
import { Time } from '@/components/Time';

// The classic Home screen (ui.py #tab-home, renderHome, renderHomeHealth, renderHomeRequestsPanel): the stat tiles,
// System health, Recent activity, Quick actions, and Recent API requests / Slowest queries - kept live from
// GET /events while the screen is open, with a 5s poll if the stream can't be reached.

// The forms open in the drawer, loaded on first use: the query form carries the SQL editor.
const QueryForm = lazy(() => import('@/features/repository/forms').then((m) => ({ default: m.QueryForm })));
const ConnectionForm = lazy(() =>
  import('@/features/connections/ConnectionsPage').then((m) => ({ default: m.ConnectionForm })),
);

type Run = Schemas['HistoryEntry'];

/** Which screen an audit action most likely belongs to - a substring match, good enough for "go look". */
function activityTarget(action: string) {
  if (action.includes('connection')) return '/connections';
  if (action.includes('query') || action.includes('sql') || action.includes('collection')) return '/queries';
  if (action.includes('key')) return '/api-keys';
  if (action.includes('role')) return '/roles';
  return '/audit-log';
}

function tone(action: string) {
  if (/^(create|load|save)/.test(action)) return 'ok';
  if (/^(delete|unload|revoke)/.test(action)) return 'bad';
  return '';
}

/** What an ad-hoc run shows in place of a query name: the start of its SQL, or that it wasn't kept. */
function adhocLabel(run: Run) {
  if (run.sql) return run.sql.length > 60 ? run.sql.slice(0, 60) + '…' : run.sql;
  return run.sql_sha256
    ? 'sql ' + run.sql_sha256.slice(0, 12)
    : String(run.mongo_collection ?? 'SQL not kept');
}

async function runs(limit: number): Promise<Run[]> {
  return unwrap(await api.GET('/api/v1/history', { params: { query: { limit } } })).items;
}

/** GET /events while Home is open: any run refreshes the requests panel. A stream that can't be reached (or
 * drops) falls back to polling every 5s, so the panel never just goes quiet. */
function useLiveRuns() {
  const client = useQueryClient();
  useEffect(() => {
    const controller = new AbortController();
    let poll: ReturnType<typeof setInterval> | null = null;
    const refresh = () => void client.invalidateQueries({ queryKey: ['home', 'runs'] });
    (async () => {
      try {
        const response = await apiFetch('/events', { signal: controller.signal });
        if (!response.ok || !response.body) throw new Error('stream unavailable');
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        for (;;) {
          const chunk = await reader.read();
          if (chunk.done) break;
          buffer += decoder.decode(chunk.value, { stream: true });
          const parts = buffer.split('\n\n');
          buffer = parts.pop() ?? '';
          if (parts.some((part) => part.startsWith('data: ') && part.includes('"execution"'))) refresh();
        }
      } catch {
        // falls through to the poll
      }
      if (!controller.signal.aborted) poll = setInterval(refresh, 5000);
    })();
    return () => {
      controller.abort();
      if (poll) clearInterval(poll);
    };
  }, [client]);
}

export function HomePage() {
  const navigate = useNavigate();
  const { openDrawer } = useFeedback();
  const connections = useConnections();
  const queries = useAllQueries();
  const keys = useApiKeys();
  const roles = useRoles();
  const metrics = useMetricsSeries();
  const pool = usePoolPoll(metrics.isSuccess);
  const audit = useQuery({
    queryKey: ['audit'],
    queryFn: async () => unwrap(await api.GET('/api/v1/audit')),
    retry: false,
  });
  useLiveRuns();

  const series = metrics.data ?? [];
  const live = pool.data ?? series;
  const conns = connections.data ?? [];
  const totalRequests = metricSum(series, 'queryapigate_requests_total');
  const errorCount = metricSum(series, 'queryapigate_requests_total', (l) =>
    ['4', '5'].includes((l.status || '')[0] ?? ''),
  );
  const errorRate = totalRequests ? (100 * errorCount) / totalRequests : 0;
  const rejections = metricSum(series, 'queryapigate_rate_limit_rejections_total');

  return (
    <>
      <div className="page-head">
        <div className="titles">
          <h1>Home</h1>
          <span className="sub">An at-a-glance overview of this gateway.</span>
        </div>
      </div>
      <div id="home-stats">
        <div className="stat-tiles">
          <StatTile label="Connections" value={`${conns.filter((c) => c.active).length} / ${conns.length}`} />
          <StatTile label="API Repository" value={queries.data?.length ?? 0} />
          <StatTile label="API keys" value={Object.keys(keys.data ?? {}).length} />
          <StatTile label="Roles" value={Object.keys(roles.data ?? {}).length} />
          <StatTile label="Requests" value={totalRequests} />
          <StatTile label="Error rate" value={errorRate.toFixed(1) + '%'} warn={errorRate >= 5} />
          <StatTile label="Active queries" value={metricGauge(series, 'queryapigate_active_queries')} />
          <StatTile
            label="Pool active connections"
            value={metricGauge(live, 'queryapigate_pool_active_connections')}
            id="home-pool-active"
            live
          />
          <StatTile
            label="Pool idle connections"
            value={metricGauge(live, 'queryapigate_pool_idle_connections')}
            id="home-pool-idle"
            live
          />
          <StatTile label="Rate limit rejections" value={rejections} warn={rejections > 0} />
        </div>
      </div>
      <Health />
      <div className="home-grid">
        <div className="panel" id="home-activity">
          <h2>Recent activity</h2>
          {(audit.data?.items ?? []).length ? (
            audit.data!.items.slice(0, 5).map((e, i) => (
              <div key={i} className="home-activity-row">
                <Time value={e.timestamp} fallback={<time />} />
                <span className={'tag act ' + tone(e.action)}>{e.action || ''}</span>
                <button type="button" className="target" onClick={() => navigate(activityTarget(e.action))}>
                  {e.target || '(unknown)'}
                </button>
              </div>
            ))
          ) : (
            <div className="empty">
              <span>No administrative changes recorded yet.</span>
            </div>
          )}
          <a
            className="side-link"
            style={{ marginTop: 8, display: 'inline-block' }}
            href="#"
            onClick={(ev) => {
              ev.preventDefault();
              navigate('/audit-log');
            }}
          >
            View audit log →
          </a>
        </div>
        <div className="panel" id="home-actions">
          <h2>Quick actions</h2>
          <div className="home-actions">
            <button type="button" className="btn" onClick={() => navigate('/designer')}>
              API Designer
            </button>
            <button
              type="button"
              className="btn"
              onClick={() => {
                navigate('/queries');
                openDrawer({
                  title: 'New API',
                  kicker: 'POST /api/v1/queries',
                  content: (
                    <Suspense fallback={<Loading />}>
                      <QueryForm />
                    </Suspense>
                  ),
                });
              }}
            >
              New API
            </button>
            <button
              type="button"
              className="btn"
              onClick={() => {
                navigate('/connections');
                openDrawer({
                  title: 'New connection',
                  kicker: 'POST /api/v1/connections',
                  content: (
                    <Suspense fallback={<Loading />}>
                      <ConnectionForm />
                    </Suspense>
                  ),
                });
              }}
            >
              New connection
            </button>
            <button type="button" className="btn" onClick={() => navigate('/help')}>
              Help
            </button>
          </div>
        </div>
      </div>
      <Requests />
    </>
  );
}

/** System health: the most pressing alerts (GET /api/v1/alerts, the Alerts screen's feed) - or an all-clear. */
function Health() {
  const navigate = useNavigate();
  const { query, active, urgent } = useAlertFeed();
  const shown = active.slice(0, 5);
  let body: React.ReactNode;
  if (query.isError) {
    body = <div className="hint">Alerts need the admin key.</div>;
  } else if (!query.data) {
    body = <div className="hint">Checking…</div>;
  } else if (!shown.length) {
    body = (
      <div className="home-activity-row">
        <span className="tag health-ok">OK</span>
        <span>No issues detected.</span>
      </div>
    );
  } else {
    body = shown.map((a) => {
      const tone = a.severity === 'critical' ? 'danger' : a.severity === 'warning' ? 'warn' : 'ok';
      return (
        <div key={a.id} className="home-activity-row">
          <span className={'tag health-' + tone}>{a.severity === 'critical' ? '!' : '·'}</span>
          <button type="button" className={'target health-text-' + tone} onClick={() => navigate('/alerts')}>
            {a.title}
          </button>
        </div>
      );
    });
  }
  return (
    <div className="panel" id="home-health" style={{ marginBottom: 16 }}>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 8 }}>
        <h2>System health</h2>
        <span className="spacer" style={{ flex: 1 }} />
        {query.data && (
          <button type="button" className="btn ghost sm" onClick={() => navigate('/alerts')}>
            {active.length > shown.length ? `All ${active.length} alerts` : urgent ? 'Open alerts' : 'Alerts'}
          </button>
        )}
      </div>
      {body}
    </div>
  );
}

// Which view the requests panel shows, kept while the page lives - as the classic tab did.
let requestsView: 'recent' | 'slowest' = 'recent';
const rememberView = (view: 'recent' | 'slowest') => {
  requestsView = view;
};

/** Recent API requests (the newest 20 runs) and Slowest queries (the 10 slowest of the newest 1000). */
function Requests() {
  const navigate = useNavigate();
  const queries = useAllQueries();
  const [view, setViewState] = useState(requestsView);
  const setView = (next: 'recent' | 'slowest') => {
    rememberView(next);
    setViewState(next);
  };
  const recent = useQuery({ queryKey: ['home', 'runs', 'recent'], queryFn: () => runs(20), retry: false });
  const wide = useQuery({
    queryKey: ['home', 'runs', 'window'],
    queryFn: () => runs(1000),
    retry: false,
    enabled: view === 'slowest',
  });
  const connectionOf = (name: string) => queries.data?.find((q) => q.name === name)?.connection_name ?? '';

  const rows =
    view === 'recent'
      ? (recent.data ?? [])
      : (wide.data ?? [])
          .filter((e) => e.duration_ms !== undefined && e.duration_ms !== null)
          .sort((a, b) => (b.duration_ms ?? 0) - (a.duration_ms ?? 0))
          .slice(0, 10);
  const pending = view === 'recent' ? recent.isPending : wide.isPending;

  let table: React.ReactNode;
  if (pending) table = <Loading />;
  else if (!rows.length)
    table =
      view === 'recent' ? (
        <div className="empty">
          <strong>No saved-query runs recorded yet</strong>
          <span>Runs of a saved query through /q/&lt;name&gt;, and ad-hoc SQL, show up here.</span>
        </div>
      ) : (
        <div className="empty">
          <strong>No timed runs recorded yet</strong>
          <span>Shows up once a query has run at least once.</span>
        </div>
      );
  else
    table = (
      <div style={{ overflowX: 'auto' }}>
        <table className="grid">
          <thead>
            <tr>
              {['', 'Time', 'Query', 'Connection', 'Caller', 'Rows', 'Duration'].map((t, i) => (
                <th key={i} className={i === 5 || i === 6 ? 'num' : ''}>
                  {t}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((e, i) => (
              <tr key={i}>
                <td style={{ width: 20, paddingRight: 0 }}>
                  <span
                    className={'dot ' + (e.status === 'success' ? 'ok' : 'bad')}
                    title={String(e.status ?? '')}
                  />
                </td>
                <td className="mono dim" style={{ whiteSpace: 'nowrap' }}>
                  <Time value={e.executed_at} fallback="" />
                </td>
                <td>
                  {e.query === null ? (
                    // ad-hoc SQL (BACKLOG #62): no saved query to open - its SQL, as history keeps it
                    <span title={e.sql ?? undefined}>
                      <span className="tag">ad-hoc</span> <span className="mono dim">{adhocLabel(e)}</span>
                    </span>
                  ) : (
                    <button
                      type="button"
                      className="target"
                      onClick={() =>
                        navigate(`/queries/${encodeURIComponent(e.query!)}`, {
                          state: { tab: 'history', version: e.version },
                        })
                      }
                    >
                      {e.query}
                    </button>
                  )}
                </td>
                <td className="mono dim">{e.connection_name ?? (e.query ? connectionOf(e.query) : '')}</td>
                <td className="mono">{String(e.key_name ?? '')}</td>
                <td className="mono num">{e.rows === undefined || e.rows === null ? '—' : String(e.rows)}</td>
                <td className="mono num">
                  {e.duration_ms === undefined || e.duration_ms === null ? '' : e.duration_ms + ' ms'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    );

  return (
    <div className="panel" id="home-requests-panel" style={{ marginTop: 16 }}>
      <div className="panel-head">
        <h2>{view === 'recent' ? 'Recent API requests' : 'Slowest queries'}</h2>
        <div className="minitabs">
          <button
            type="button"
            className={'minitab' + (view === 'recent' ? ' active' : '')}
            onClick={() => setView('recent')}
          >
            Recent
          </button>
          <button
            type="button"
            className={'minitab' + (view === 'slowest' ? ' active' : '')}
            onClick={() => setView('slowest')}
          >
            Slowest
          </button>
        </div>
        <span className="spacer" />
      </div>
      {table}
    </div>
  );
}

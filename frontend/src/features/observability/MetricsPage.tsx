import { useEffect, useState } from 'react';

import { useConnections } from '@/app/data';
import { Loading } from '@/app/feedback';
import { useMetricsSeries, usePoolPoll } from '@/app/metrics';
import { StatTile } from '@/components/StatTile';
import { metricGauge, metricGroupSum, metricSum } from '@/lib/metrics';

// The classic Metrics screen (ui.py #tab-metrics, loadMetrics, renderMetrics, pollPoolStats): live totals since
// this process started, from /metrics. The two pool tiles refresh every 2s; the rest on Refresh.

/** One card of horizontal bars: label, a track filled in proportion to the largest value, and the number. */
function BarCard({
  title,
  rows,
  colorOf,
}: {
  title: string;
  rows: { label: string; value: number }[];
  colorOf: (label: string) => string;
}) {
  const max = rows.reduce((m, r) => Math.max(m, r.value), 0);
  return (
    <div className="chart-card">
      <h3>{title}</h3>
      {rows.map((r) => (
        <div key={r.label} className="bar-row">
          <span className="bl">{r.label}</span>
          <div className="bar-track">
            <div
              className="bar-fill"
              style={{
                width: (r.value ? Math.max(1, (100 * r.value) / max) : 0) + '%',
                background: colorOf(r.label),
              }}
            />
          </div>
          <span className="bv">{String(r.value)}</span>
        </div>
      ))}
    </div>
  );
}

const STATUS_COLOR: Record<string, string> = {
  '2xx': 'var(--accent)',
  '3xx': 'var(--accent)',
  '4xx': 'var(--warn)',
  '5xx': 'var(--danger)',
};

function useAge(at: number) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 5000);
    return () => clearInterval(timer);
  }, []);
  if (!at) return '';
  const secs = Math.round((Math.max(now, at) - at) / 1000);
  return secs < 5
    ? 'Updated just now'
    : 'Updated ' + (secs < 90 ? secs + 's' : Math.round(secs / 60) + ' min') + ' ago';
}

export function MetricsPage() {
  const metrics = useMetricsSeries();
  const pool = usePoolPoll(metrics.isSuccess); // just the two pool gauges - the rest stays as loaded until Refresh
  const connections = useConnections();
  const age = useAge(metrics.dataUpdatedAt);

  let body: React.ReactNode;
  if (metrics.isPending) {
    body = <Loading text="Loading metrics…" />;
  } else if (metrics.isError) {
    body = (
      <div className="empty">
        <strong>Couldn’t load metrics</strong>
        <button type="button" className="btn sm" onClick={() => metrics.refetch()}>
          Retry
        </button>
      </div>
    );
  } else {
    const series = metrics.data;
    const live = pool.data ?? series;
    const totalRequests = metricSum(series, 'queryapigate_requests_total');
    const byStatusClass: Record<string, number> = {};
    series
      .filter((s) => s.name === 'queryapigate_requests_total')
      .forEach((s) => {
        const cls = (s.labels.status || '?').charAt(0) + 'xx';
        byStatusClass[cls] = (byStatusClass[cls] ?? 0) + s.value;
      });
    const errorCount = (byStatusClass['4xx'] ?? 0) + (byStatusClass['5xx'] ?? 0);
    const errorRate = totalRequests ? (100 * errorCount) / totalRequests : 0;
    const statusRows = ['2xx', '3xx', '4xx', '5xx']
      .map((c) => ({ label: c, value: byStatusClass[c] ?? 0 }))
      .filter((r) => r.value > 0);
    const byConnection = metricGroupSum(series, 'queryapigate_queries_total', 'connection');
    const allNames = [
      ...new Set([...(connections.data ?? []).map((c) => c.name), ...Object.keys(byConnection)]),
    ].sort();
    const connRows = allNames.map((c) => ({ label: c, value: byConnection[c] ?? 0 }));
    const errorsByConnection = metricGroupSum(
      series.filter((s) => s.labels.status === 'error'),
      'queryapigate_queries_total',
      'connection',
    );
    const rowsByConnection = metricGroupSum(series, 'queryapigate_rows_returned_total', 'connection');
    const sumByConn = metricGroupSum(series, 'queryapigate_query_duration_seconds_sum', 'connection');
    const countByConn = metricGroupSum(series, 'queryapigate_query_duration_seconds_count', 'connection');
    const connNames = Object.keys(byConnection).sort();
    body = (
      <>
        <div className="stat-tiles">
          <StatTile label="Requests" value={totalRequests} />
          <StatTile label="Error rate" value={errorRate.toFixed(1) + '%'} warn={errorRate >= 5} />
          <StatTile label="Active queries" value={metricGauge(series, 'queryapigate_active_queries')} />
          <StatTile
            label="Pool active connections"
            value={metricGauge(live, 'queryapigate_pool_active_connections')}
            id="metrics-pool-active"
            live
          />
          <StatTile
            label="Pool idle connections"
            value={metricGauge(live, 'queryapigate_pool_idle_connections')}
            id="metrics-pool-idle"
            live
          />
          <StatTile
            label="Rate limit rejections"
            value={metricGauge(series, 'queryapigate_rate_limit_rejections_total')}
          />
          <StatTile label="Rows returned" value={metricSum(series, 'queryapigate_rows_returned_total')} />
        </div>
        <div className="metrics-charts">
          {statusRows.length ? (
            <BarCard title="Requests by status" rows={statusRows} colorOf={(l) => STATUS_COLOR[l]!} />
          ) : null}
          {connRows.length ? (
            <BarCard title="Queries by connection" rows={connRows} colorOf={() => 'var(--accent)'} />
          ) : null}
        </div>
        {connNames.length ? (
          <div className="panel" style={{ overflowX: 'auto' }}>
            <table className="grid">
              <thead>
                <tr>
                  {['Connection', 'Queries', 'Errors', 'Avg latency', 'Rows returned'].map((t, i) => (
                    <th key={t} className={i ? 'num' : ''}>
                      {t}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {connNames.map((c) => {
                  const count = countByConn[c] ?? 0;
                  const avgMs = count ? Math.round((1000 * (sumByConn[c] ?? 0)) / count) : null;
                  return (
                    <tr key={c}>
                      <td>
                        <span className="name">{c}</span>
                      </td>
                      <td className="mono num">{String(byConnection[c] ?? 0)}</td>
                      <td className="mono num">{String(errorsByConnection[c] ?? 0)}</td>
                      <td className="mono num">{avgMs === null ? '—' : avgMs + ' ms'}</td>
                      <td className="mono num">{String(rowsByConnection[c] ?? 0)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : null}
      </>
    );
  }

  return (
    <>
      <div className="page-head">
        <div className="titles">
          <h1>Metrics</h1>
          <span className="sub">
            Live totals since this process started - with several instances, this one only. For trends and
            every instance together, scrape each one's <code>/metrics</code> with Prometheus and import the
            bundled Grafana dashboard.
          </span>
        </div>
        <span className="spacer" />
        <span className="updated" id="metrics-updated">
          {age}
        </span>
        <button id="refresh-metrics" type="button" className="btn" onClick={() => void metrics.refetch()}>
          Refresh
        </button>
      </div>
      <div id="metrics-body">{body}</div>
    </>
  );
}

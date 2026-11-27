import { useEffect, useState } from 'react';

import { useConnections } from '@/app/data';
import { Loading } from '@/app/feedback';
import { useMetricsSeries, usePoolPoll } from '@/app/metrics';
import { StatTile } from '@/components/StatTile';
import { NoMatches, SortHeader, TableFilter } from '@/components/TableControls';
import { useTableControls, type Column } from '@/components/useTableControls';
import { metricGauge, metricGroupSum, metricSum } from '@/lib/metrics';
import { PageTitle } from '@/components/PageTitle';

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

type ConnectionMetrics = {
  connection: string;
  queries: number;
  errors: number;
  avgMs: number | null;
  rows: number;
};

const METRIC_COLUMNS: Column<ConnectionMetrics>[] = [
  { key: 'connection', label: 'Connection', sort: (r) => r.connection },
  { key: 'queries', label: 'Queries', className: 'num', sort: (r) => r.queries },
  { key: 'errors', label: 'Errors', className: 'num', sort: (r) => r.errors },
  { key: 'latency', label: 'Avg latency', className: 'num', sort: (r) => r.avgMs },
  { key: 'rows', label: 'Rows returned', className: 'num', sort: (r) => r.rows },
];

const metricText = (r: ConnectionMetrics) => [r.connection];

/** Each connection's totals since this process started, sortable and filterable. */
function ConnectionMetricsTable({ items }: { items: ConnectionMetrics[] }) {
  const table = useTableControls(items, { columns: METRIC_COLUMNS, text: metricText });
  return (
    <div className="panel" id="metrics-connections" style={{ overflowX: 'auto' }}>
      <div className="panel-bar">
        <span className="spacer" />
        <TableFilter
          controls={table}
          id="metrics-filter"
          placeholder="Filter connections…"
          label="Filter connections"
        />
      </div>
      <table className="grid">
        <thead>
          <tr>
            {table.columns.map((c) => (
              <SortHeader key={c.key} controls={table} column={c} />
            ))}
          </tr>
        </thead>
        <tbody>
          {table.rows.map((r) => (
            <tr key={r.connection}>
              <td>
                <span className="name">{r.connection}</span>
              </td>
              <td className="mono num">{String(r.queries)}</td>
              <td className="mono num">{String(r.errors)}</td>
              <td className="mono num">{r.avgMs === null ? '—' : r.avgMs + ' ms'}</td>
              <td className="mono num">{String(r.rows)}</td>
            </tr>
          ))}
          {!table.rows.length && <NoMatches controls={table} span={METRIC_COLUMNS.length} />}
        </tbody>
      </table>
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
    const connMetrics: ConnectionMetrics[] = Object.keys(byConnection)
      .sort()
      .map((c) => {
        const count = countByConn[c] ?? 0;
        return {
          connection: c,
          queries: byConnection[c] ?? 0,
          errors: errorsByConnection[c] ?? 0,
          avgMs: count ? Math.round((1000 * (sumByConn[c] ?? 0)) / count) : null,
          rows: rowsByConnection[c] ?? 0,
        };
      });
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
        {connMetrics.length ? <ConnectionMetricsTable items={connMetrics} /> : null}
      </>
    );
  }

  return (
    <>
      <div className="page-head">
        <div className="titles">
          <PageTitle tab>Metrics</PageTitle>
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

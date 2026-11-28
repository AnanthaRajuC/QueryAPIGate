import { useState, type ReactNode } from 'react';

import { useHealth } from '@/app/data';
import { Time } from '@/components/Time';
import { parseOffset } from '@/lib/time';

import type { HistoryEntry } from './api';
import { dailyCounts, lastDays } from './runDays';

// A query's runs at a glance. Under its header, one borderless strip: total runs, success, average duration and the
// last run, with a seven-day sparkline of requests per day. The Metrics tab has the rest - the slowest run, average
// rows (StatTiles) and the full requests-per-day chart (RequestsPerDay).

/** Today in the server's time zone - as of when the query was opened, which is close enough for a sparkline. */
function useServerToday() {
  const [now] = useState(() => Date.now());
  const offset = useHealth().data?.utc_offset ?? '+00:00';
  return new Date(now + parseOffset(offset) * 60_000).toISOString().slice(0, 10);
}

const formatPercent = (p: number) => (Number.isInteger(p) ? `${p}%` : `${p.toFixed(1)}%`);

export function StatStrip({ runs }: { runs: HistoryEntry[] }): ReactNode {
  const today = useServerToday();
  if (!runs.length) return null;
  const successes = runs.filter((x) => x.status === 'success').length;
  const success = (100 * successes) / runs.length;
  const durations = runs.map((x) => x.duration_ms).filter((d): d is number => d !== undefined && d !== null);
  const avg = durations.length ? Math.round(durations.reduce((a, d) => a + d, 0) / durations.length) : null;
  const week = lastDays(runs, today);
  const max = Math.max(1, ...week.map((d) => d.count));
  const total = week.reduce((a, d) => a + d.count, 0);
  return (
    <div className="stat-strip">
      {/* each label before its value, for a screen reader; shown value first (CSS) */}
      <dl className="stat-strip-figures">
        <div>
          <dt>Total runs</dt>
          <dd>{runs.length}</dd>
        </div>
        <div>
          <dt>Success</dt>
          <dd className={success <= 95 ? 'warn' : undefined}>
            {formatPercent(Math.round(success * 10) / 10)}
          </dd>
        </div>
        <div>
          <dt>Avg duration</dt>
          <dd>{avg === null ? '—' : `${avg} ms`}</dd>
        </div>
        <div>
          <dt>Last run</dt>
          <dd>
            <Time value={runs[runs.length - 1]?.executed_at} seconds={false} />
          </dd>
        </div>
      </dl>
      <div className="sparkline-wrap">
        <div
          className="sparkline"
          role="img"
          aria-label={`Requests per day over the last 7 days: ${week.map((d) => d.count).join(', ')}`}
        >
          {week.map((d) => (
            <span
              key={d.date}
              className={d.count ? 'spark-bar' : 'spark-bar zero'}
              style={{ height: `${(100 * d.count) / max}%` }}
              title={`${d.date} — ${d.count}${d.count === 1 ? ' request' : ' requests'}`}
            />
          ))}
        </div>
        <span className="sparkline-label" title={`${total} in the last 7 days`}>
          Requests per day, 7 days
        </span>
      </div>
    </div>
  );
}

/** The full chart, on the Metrics tab: every day from the first run recorded to the last. */
export function RequestsPerDay({ runs }: { runs: HistoryEntry[] }): ReactNode {
  const days = dailyCounts(runs);
  if (!days.length) return null;
  const max = days.reduce((m, d) => Math.max(m, d.count), 0);
  return (
    <div className="req-day-chart">
      <div className="req-day-head">
        <h3>Requests per day</h3>
        <span className="hint">{days.length === 1 ? '1 day' : `${days.length} days`}</span>
      </div>
      <div className="req-day-cols">
        {days.map((d) => (
          <div
            key={d.date}
            className="req-day-col"
            title={`${d.date} — ${d.count}${d.count === 1 ? ' request' : ' requests'}`}
          >
            {d.count === max && max > 0 ? <div className="req-day-label">{d.count}</div> : null}
            <div
              className={d.count ? 'req-day-bar' : 'req-day-bar zero'}
              style={{ height: `${max ? (100 * d.count) / max : 0}%` }}
            />
          </div>
        ))}
      </div>
      <div className="req-day-axis">
        <span>{days[0]!.date}</span>
        {days.length > 1 ? <span>{days[days.length - 1]!.date}</span> : null}
      </div>
    </div>
  );
}

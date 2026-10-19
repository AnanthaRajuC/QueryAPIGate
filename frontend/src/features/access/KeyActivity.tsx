import { Link } from 'react-router';

import { daily, H, PAD, paths, type Run, W } from './activity';

// A GitHub-style activity sparkline over an already-fetched run list: daily counts over the last 30 days, one
// series (so no legend - the caller's column header names it), failures tint the fill, wrapped in a link when `to`
// is given. Pages fetch the window once and group it (activity.ts), then render one of these per row.

export function ActivitySpark({
  label,
  items,
  isPending,
  isError,
  to,
}: {
  label: string;
  items: Run[] | undefined;
  isPending: boolean;
  isError: boolean;
  to?: string;
}) {
  if (isPending) return <span className="spark-skeleton" aria-hidden="true" />;

  const { counts, errors } = daily(isError ? [] : (items ?? []));
  const total = counts.reduce((a, b) => a + b, 0);
  const peak = Math.max(0, ...counts);

  if (!total) {
    return (
      <span className="spark-empty dim" title={`${label}: no activity in 30 days`}>
        <svg width={W} height={H} role="img" aria-label={`${label}: no activity in 30 days`}>
          <line x1={PAD} y1={H - PAD} x2={W - PAD} y2={H - PAD} className="spark-base" />
        </svg>
      </span>
    );
  }

  const { line, area } = paths(counts);
  const title =
    `${label}: ${total} run${total === 1 ? '' : 's'} over 30 days` +
    (errors ? ` · ${errors} failed` : '') +
    ` · peak ${peak}/day`;
  const svg = (
    <svg width={W} height={H} role="img" aria-label={title} className={errors ? 'spark has-err' : 'spark'}>
      <line x1={PAD} y1={H - PAD} x2={W - PAD} y2={H - PAD} className="spark-base" />
      <path d={area} className="spark-area" />
      <path d={line} className="spark-line" />
    </svg>
  );
  return to ? (
    <Link to={to} className="spark-link" title={title}>
      {svg}
    </Link>
  ) : (
    <span className="spark-link" title={title}>
      {svg}
    </span>
  );
}

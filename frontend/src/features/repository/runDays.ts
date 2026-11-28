import type { HistoryEntry } from './api';

// Runs counted by day, for the stat strip's sparkline and the Metrics tab's chart (stats.tsx).

/** A run's day, as the server wrote it (its timestamps are in the server's time zone). */
const dayOf = (e: HistoryEntry) => String(e.executed_at || '').slice(0, 10);

function addDays(day: string, n: number) {
  const d = new Date(day + 'T00:00:00Z');
  d.setUTCDate(d.getUTCDate() + n);
  return d.toISOString().slice(0, 10);
}

function countByDay(runs: HistoryEntry[]) {
  const counts: Record<string, number> = {};
  runs.forEach((e) => {
    const day = dayOf(e);
    if (day) counts[day] = (counts[day] ?? 0) + 1;
  });
  return counts;
}

/** Daily request counts, zero-filled from the first to the last day recorded (ui.py dailyRequestCounts). */
export function dailyCounts(runs: HistoryEntry[]) {
  const counts = countByDay(runs);
  const days = Object.keys(counts).sort();
  if (!days.length) return [];
  const out: { date: string; count: number }[] = [];
  for (let day = days[0]!; day <= days[days.length - 1]!; day = addDays(day, 1)) {
    out.push({ date: day, count: counts[day] ?? 0 });
  }
  return out;
}

/** The last `n` days' counts, zero-filled, ending today in the server's time zone (or on the last run's day, when
 * that is later - a clock ahead of this computer's). */
export function lastDays(runs: HistoryEntry[], today: string, n = 7) {
  const counts = countByDay(runs);
  const latest = runs.map(dayOf).reduce((a, d) => (d > a ? d : a), '');
  const end = latest > today ? latest : today;
  return Array.from({ length: n }, (_, i) => {
    const date = addDays(end, i - (n - 1));
    return { date, count: counts[date] ?? 0 };
  });
}

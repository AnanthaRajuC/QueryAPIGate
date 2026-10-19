import { useQuery } from '@tanstack/react-query';

import { api, unwrap } from '@/api/client';

// Shared data layer for the activity sparklines (ActivitySpark in KeyActivity.tsx): the 30-day geometry, the
// day-bucketing, and the history fetches. History has no caller/connection/query filter beyond key & query, so a
// page that wants a spark per row fetches the 30-day window once and groups client-side (one request, not one per
// row). Kept apart from the component so each file exports one kind of thing.

export const DAYS = 30;
export const W = 116;
export const H = 30;
export const PAD = 3;

export type Run = {
  executed_at: string;
  status?: string;
  transport?: string | null;
  connection_name?: string | null;
  key_name?: string | null;
  query?: string | null;
};

/** The ISO date (YYYY-MM-DD) that starts the 30-day window - the `since` the history calls pass. */
export function since30() {
  const d = new Date();
  d.setDate(d.getDate() - (DAYS - 1));
  return d.toISOString().slice(0, 10);
}

/** Runs bucketed into one count per day across the window, plus how many failed. */
export function daily(items: Run[]): { counts: number[]; errors: number } {
  const start = new Date();
  start.setHours(0, 0, 0, 0);
  start.setDate(start.getDate() - (DAYS - 1));
  const counts = new Array(DAYS).fill(0);
  let errors = 0;
  for (const run of items) {
    // executed_at is 'YYYY-MM-DD HH:MM:SS' in the server's local time; parse as local.
    const when = new Date(run.executed_at.replace(' ', 'T'));
    const idx = Math.floor((when.getTime() - start.getTime()) / 86_400_000);
    if (idx >= 0 && idx < DAYS) {
      counts[idx] += 1;
      if (run.status === 'error') errors += 1;
    }
  }
  return { counts, errors };
}

/** The line and the area-under-it as two SVG paths, anchored to the baseline. */
export function paths(counts: number[]): { line: string; area: string } {
  const max = Math.max(1, ...counts);
  const x = (i: number) => PAD + (i * (W - 2 * PAD)) / (DAYS - 1);
  const y = (v: number) => H - PAD - (v / max) * (H - 2 * PAD);
  const pts = counts.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`);
  const line = 'M' + pts.join(' L');
  const area =
    `M${x(0).toFixed(1)},${(H - PAD).toFixed(1)} L` +
    pts.join(' L') +
    ` L${x(DAYS - 1).toFixed(1)},${(H - PAD).toFixed(1)} Z`;
  return { line, area };
}

function group(items: Run[] | undefined, field: keyof Run): Record<string, Run[]> {
  const by: Record<string, Run[]> = {};
  for (const run of items ?? []) {
    const name = run[field];
    if (typeof name === 'string' && name) (by[name] ??= []).push(run);
  }
  return by;
}

/** The whole 30-day window in one request (shared across pages via its query key), for grouping client-side. */
function useWindow() {
  return useQuery({
    queryKey: ['activity-window'],
    queryFn: async () =>
      unwrap(await api.GET('/api/v1/history', { params: { query: { since: since30(), limit: 1000 } } }))
        .items as Run[],
    retry: false,
    staleTime: 60_000,
  });
}

/** Activity per connection (Connections table): the window grouped by connection_name. */
export function useConnectionActivity() {
  const q = useWindow();
  return { by: group(q.data, 'connection_name'), isPending: q.isPending, isError: q.isError };
}

/** Activity per API key (API keys & Roles tables): the window grouped by key_name. */
export function useKeyActivityMap() {
  const q = useWindow();
  return { by: group(q.data, 'key_name'), isPending: q.isPending, isError: q.isError };
}

/** Activity per saved query (Caching & MCP tables): the window grouped by query name. */
export function useQueryActivityMap() {
  const q = useWindow();
  return { by: group(q.data, 'query'), isPending: q.isPending, isError: q.isError };
}

/** One key's runs over the window, grouped by query - for a key's "APIs it can reach" (how this key used each API),
 * in a single request rather than one per API. */
export function useKeyQueryActivity(keyName: string) {
  const q = useQuery({
    queryKey: ['key-query-activity', keyName],
    queryFn: async () =>
      unwrap(
        await api.GET('/api/v1/history', {
          params: { query: { key: keyName, since: since30(), limit: 1000 } },
        }),
      ).items as Run[],
    retry: false,
    staleTime: 60_000,
  });
  return { by: group(q.data, 'query'), isPending: q.isPending, isError: q.isError };
}

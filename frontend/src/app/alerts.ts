import { useQuery } from '@tanstack/react-query';
import { useEffect, useSyncExternalStore } from 'react';

import { api, unwrap, type Schemas } from '@/api/client';

// The alerts feed (GET /api/v1/alerts) as this browser sees it: checked every minute, minus the ones dismissed here.
// A dismissal holds while its alert keeps being reported; once the condition clears, the dismissal is forgotten, so
// the same problem coming back later is shown again. Dismissals also lapse after 30 days.

export type Alert = Schemas['Alert'];

const KEY = 'queryapigate-ui-dismissed-alerts';
const LAPSE_MS = 30 * 24 * 60 * 60 * 1000;

const listeners = new Set<() => void>();
let cached: { raw: string | null; value: Record<string, number> } | null = null;

function stored(): string | null {
  try {
    return localStorage.getItem(KEY);
  } catch {
    return null;
  }
}

function readDismissed(): Record<string, number> {
  const raw = stored();
  if (cached && cached.raw === raw) return cached.value;
  let value: Record<string, number> = {};
  try {
    value = (JSON.parse(raw || '{}') as Record<string, number>) || {};
  } catch {
    // unreadable: nothing dismissed
  }
  cached = { raw, value };
  return value;
}

function writeDismissed(value: Record<string, number>) {
  try {
    localStorage.setItem(KEY, JSON.stringify(value));
  } catch {
    cached = { raw: null, value }; // storage unavailable: kept for this page only
  }
  listeners.forEach((l) => l());
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  window.addEventListener('storage', listener);
  return () => {
    listeners.delete(listener);
    window.removeEventListener('storage', listener);
  };
}

export function dismissAlert(id: string) {
  writeDismissed({ ...readDismissed(), [id]: Date.now() });
}

export function restoreAlert(id: string) {
  const rest = { ...readDismissed() };
  delete rest[id];
  writeDismissed(rest);
}

export function useAlerts() {
  return useQuery({
    queryKey: ['alerts'],
    queryFn: async () => unwrap(await api.GET('/api/v1/alerts')),
    refetchInterval: 60_000,
    retry: false,
  });
}

/** The feed split into what still needs attention and what was dismissed here; `urgent` counts critical + warning. */
export function useAlertFeed() {
  const query = useAlerts();
  const dismissed = useSyncExternalStore(subscribe, readDismissed);
  const items = query.data?.items;
  useEffect(() => {
    if (!items) return;
    const now = Date.now();
    const reported = new Set(items.map((a) => a.id));
    const current = readDismissed();
    const kept = Object.fromEntries(
      Object.entries(current).filter(([id, at]) => reported.has(id) && now - at < LAPSE_MS),
    );
    if (Object.keys(kept).length !== Object.keys(current).length) writeDismissed(kept);
  }, [items]);
  const active = (items ?? []).filter((a) => !(a.id in dismissed));
  const hidden = (items ?? []).filter((a) => a.id in dismissed);
  return {
    query,
    active,
    dismissed: hidden,
    urgent: active.filter((a) => a.severity !== 'info').length,
    critical: active.some((a) => a.severity === 'critical'),
  };
}

/** The Alerts screen's tabs after All: one per check, each listing the alert kinds it covers and what it watches. */
export const ALERT_TABS: { id: string; label: string; kinds: Alert['kind'][]; about: string }[] = [
  {
    id: 'key-expiry',
    label: 'Key expiry',
    kinds: ['key_expired', 'key_expiring'],
    about:
      'Active API keys that have expired, or expire within 7 days - their callers are refused from then on.',
  },
  {
    id: 'unused-keys',
    label: 'Unused keys',
    kinds: ['key_unused'],
    about:
      'Active API keys not used for QUERYAPIGATE_ALERT_KEY_UNUSED_DAYS (default 90) - risk without benefit.',
  },
  {
    id: 'connections',
    label: 'Failing connections',
    kinds: ['connection_failing'],
    about:
      'Connections whose last 3 runs in 24 hours all failed for a connection reason - the database unreachable, a ' +
      'driver missing, settings incomplete. Bad SQL on a working connection is not counted here.',
  },
  {
    id: 'query-errors',
    label: 'Query errors',
    kinds: ['query_errors'],
    about:
      'Saved queries where at least QUERYAPIGATE_ALERT_ERROR_RATE% (default 20) of their last 50 runs in 7 days ' +
      'failed, once they have 10 runs or more.',
  },
  {
    id: 'slow-queries',
    label: 'Slow queries',
    kinds: ['query_slow', 'query_timeouts'],
    about:
      'Saved queries whose typical (median) run is over QUERYAPIGATE_SLOW_QUERY_THRESHOLD, or that timed out in ' +
      'the last 24 hours.',
  },
  {
    id: 'rate-limits',
    label: 'Rate limits',
    kinds: ['key_rate_limited', 'client_rate_limited'],
    about:
      'API keys refused by their own rate_limit, and client addresses refused by QUERYAPIGATE_RATE_LIMIT, 10 times ' +
      'or more in the last hour (counted by this server since it started).',
  },
  {
    id: 'open-access',
    label: 'Open access',
    kinds: ['open_server'],
    about: 'Whether the server runs with no API key at all, letting anyone query and change it.',
  },
  {
    id: 'run-history',
    label: 'Run history',
    kinds: ['history_failed', 'history_dropped'],
    about: 'Runs that could not be recorded, or were dropped because the store could not keep up.',
  },
];

/** The tab an alert kind belongs to. */
export function alertTab(kind: string): string {
  return ALERT_TABS.find((t) => (t.kinds as string[]).includes(kind))?.id ?? 'all';
}

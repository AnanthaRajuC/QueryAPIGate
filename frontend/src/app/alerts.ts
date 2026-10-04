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

import { useSyncExternalStore } from 'react';

import { useHealth } from '@/app/data';
import { usePrefValues } from '@/app/prefs';

import { absolute, relative, serverInstant, type ServerZone, type ShowIn } from './time';

// One clock for every relative time on the page: a re-render every 30 seconds while any is shown.
let tick = 0;
const listeners = new Set<() => void>();
let timer: ReturnType<typeof setInterval> | null = null;

function subscribe(listener: () => void) {
  listeners.add(listener);
  if (!timer) {
    timer = setInterval(() => {
      tick += 1;
      listeners.forEach((l) => l());
    }, 30_000);
  }
  return () => {
    listeners.delete(listener);
    if (!listeners.size && timer) {
      clearInterval(timer);
      timer = null;
    }
  };
}

const noSubscribe = () => () => {};

export interface FormattedTime {
  text: string;
  title: string;
  iso: string;
}

/** Formats server timestamps by the viewer's preferences; a value that isn't a date and time comes back null. */
export function useFormatTime(ticking = true): (value: string | null | undefined) => FormattedTime | null {
  const { timeZone, timeFormat } = usePrefValues();
  const health = useHealth();
  const zone: ServerZone | null = health.data
    ? { name: health.data.time_zone ?? null, offset: health.data.utc_offset ?? '+00:00' }
    : null;
  const rel = timeFormat === 'Relative';
  useSyncExternalStore(rel && ticking ? subscribe : noSubscribe, () => tick);
  return (value) => {
    if (!value || !zone) return null;
    const instant = serverInstant(value, zone);
    if (instant === null) return null;
    const exact = absolute(instant, timeZone as ShowIn, zone);
    const title = `${exact.text} (${exact.zone})`;
    const text = (rel && relative(instant, Date.now())) || exact.text;
    return { text, title, iso: new Date(instant).toISOString() };
  };
}

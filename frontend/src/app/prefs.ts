import { useEffect, useSyncExternalStore } from 'react';

// The classic UI's interface preferences (Settings > Interface: theme, density and the default result format),
// stored per browser under the same key, so changing them in either UI changes both.
const PREFS_KEY = 'queryapigate-ui-prefs';

export interface Prefs {
  theme: string;
  density: string;
  format: string;
}

const DEFAULTS: Prefs = { theme: 'System', density: 'Comfortable', format: 'json' };

const listeners = new Set<() => void>();
let cached: { raw: string | null; prefs: Prefs } | null = null;

function stored(): string | null {
  try {
    return localStorage.getItem(PREFS_KEY);
  } catch {
    return null;
  }
}

function parse(raw: string | null): Partial<Prefs> {
  try {
    return (JSON.parse(raw || '{}') as Partial<Prefs>) || {};
  } catch {
    return {};
  }
}

/** The current preferences, the defaults filling in whatever was never set. */
export function readPrefs(): Prefs {
  const raw = stored();
  if (cached && cached.raw === raw) return cached.prefs; // the same object while unchanged, for useSyncExternalStore
  cached = { raw, prefs: { ...DEFAULTS, ...parse(raw) } };
  return cached.prefs;
}

export function setPref(key: keyof Prefs, value: string) {
  try {
    localStorage.setItem(PREFS_KEY, JSON.stringify({ ...readPrefs(), [key]: value }));
  } catch {
    // storage unavailable: the choice lasts for this page only
    cached = { raw: null, prefs: { ...readPrefs(), [key]: value } };
  }
  listeners.forEach((listener) => listener());
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  window.addEventListener('storage', listener); // a change made in another tab, or in /ui
  return () => {
    listeners.delete(listener);
    window.removeEventListener('storage', listener);
  };
}

export function usePrefValues(): Prefs {
  return useSyncExternalStore(subscribe, readPrefs);
}

/** Applies theme and density to the page, now and whenever they change. */
export function usePrefs() {
  const prefs = usePrefValues();
  useEffect(() => {
    document.documentElement.style.colorScheme =
      { System: 'light dark', Light: 'light', Dark: 'dark' }[prefs.theme] ?? 'light dark';
    document.body.classList.toggle('compact', prefs.density === 'Compact');
  }, [prefs.theme, prefs.density]);
}

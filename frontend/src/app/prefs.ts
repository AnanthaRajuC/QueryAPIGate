import { useEffect, useSyncExternalStore } from 'react';

// The preferences of Settings > Appearance and Settings > Editor & results, stored per browser
// - under the classic UI's key, so preferences set before the Console replaced it carry over.
const PREFS_KEY = 'queryapigate-ui-prefs';

export interface Prefs {
  theme: string;
  fontSize: string;
  timeZone: string;
  timeFormat: string;
  motion: string;
  pageSize: string;
  nullDisplay: string;
  numberFormat: string;
  lineWrap: string;
  lineNumbers: string;
  density: string;
  format: string;
}

const DEFAULTS: Prefs = {
  theme: 'System',
  fontSize: 'Medium',
  timeZone: 'Local',
  timeFormat: 'Absolute',
  motion: 'System',
  density: 'Comfortable',
  format: 'json',
  pageSize: '10',
  nullDisplay: 'NULL',
  numberFormat: 'Plain',
  lineWrap: 'Off',
  lineNumbers: 'On',
};

// Font size scales the whole page, like the browser's own zoom: the stylesheet sizes text in px (as the classic UI's
// did), so scaling everything keeps text, spacing and icons in proportion instead of crowding larger text.
const FONT_SCALE: Record<string, string> = { Small: '0.9', Medium: '1', Large: '1.15' };

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
  window.addEventListener('storage', listener); // a change made in another tab
  return () => {
    listeners.delete(listener);
    window.removeEventListener('storage', listener);
  };
}

export function usePrefValues(): Prefs {
  return useSyncExternalStore(subscribe, readPrefs);
}

/** The page's font-size scale. Mouse positions (clientX/Y) and getBoundingClientRect() are in screen pixels, while a
 *  left/top/width set on an element inside the page is scaled by it - so convert one to the other by dividing. */
export function pageZoom(): number {
  return parseFloat(document.documentElement.style.zoom) || 1;
}

/** Rows per page to start a query with (Settings > Editor & results). */
export function preferredPageSize(): number {
  return Number(readPrefs().pageSize) || 10;
}

/** Applies theme, font size, density and motion to the page, now and whenever they change. */
export function usePrefs() {
  const prefs = usePrefValues();
  useEffect(() => {
    document.documentElement.style.colorScheme =
      { System: 'light dark', Light: 'light', Dark: 'dark' }[prefs.theme] ?? 'light dark';
    const zoom = FONT_SCALE[prefs.fontSize] ?? '1';
    document.documentElement.style.zoom = zoom;
    document.documentElement.style.setProperty('--zoom', zoom); // for the stylesheet's viewport sizes (console.css)
    document.body.classList.toggle('compact', prefs.density === 'Compact');
    document.body.classList.toggle('reduce-motion', prefs.motion === 'On'); // System: console.css's media query
  }, [prefs.theme, prefs.fontSize, prefs.density, prefs.motion]);
}

const DARK_QUERY = '(prefers-color-scheme: dark)';

function systemIsDark() {
  return typeof window.matchMedia === 'function' && window.matchMedia(DARK_QUERY).matches;
}

function subscribeSystem(listener: () => void) {
  if (typeof window.matchMedia !== 'function') return () => {};
  const query = window.matchMedia(DARK_QUERY);
  query.addEventListener('change', listener);
  return () => query.removeEventListener('change', listener);
}

/** Whether the page is showing dark right now: the theme preference, or the operating system's when it is System. */
export function useIsDark(): boolean {
  const { theme } = usePrefValues();
  const system = useSyncExternalStore(subscribeSystem, systemIsDark);
  return theme === 'Dark' || (theme !== 'Light' && system);
}

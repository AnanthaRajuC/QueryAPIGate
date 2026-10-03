import { useEffect } from 'react';

// The classic UI's interface preferences (Settings > Interface: theme and density), stored per browser under the
// same key, so changing them in either UI changes both.
const PREFS_KEY = 'queryapigate-ui-prefs';

export function usePrefs() {
  useEffect(() => {
    const apply = () => {
      let prefs: { theme?: string; density?: string };
      try {
        prefs = JSON.parse(localStorage.getItem(PREFS_KEY) || '{}') || {};
      } catch {
        prefs = {};
      }
      const scheme =
        { System: 'light dark', Light: 'light', Dark: 'dark' }[prefs.theme ?? 'System'] ?? 'light dark';
      document.documentElement.style.colorScheme = scheme;
      document.body.classList.toggle('compact', prefs.density === 'Compact');
    };
    apply();
    window.addEventListener('storage', apply);
    return () => window.removeEventListener('storage', apply);
  }, []);
}

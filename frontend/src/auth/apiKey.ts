// The API key lives in sessionStorage under the same name /docs uses, so signing in to either signs in to both for
// this tab. Storage can be unavailable (private windows, blocked site data), so every access is
// guarded and the Console simply behaves as signed out.
const STORAGE_KEY = 'queryapigate-key';

export function getApiKey(): string {
  try {
    return sessionStorage.getItem(STORAGE_KEY) ?? '';
  } catch {
    return '';
  }
}

export function setApiKey(value: string): void {
  try {
    if (value) sessionStorage.setItem(STORAGE_KEY, value);
    else sessionStorage.removeItem(STORAGE_KEY);
  } catch {
    // ignored - see above
  }
}

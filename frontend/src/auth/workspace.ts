// The workspace this tab works in (ADR 0023): every request goes to /w/<workspace>/... - except `default`, whose paths
// are the plain ones, as before workspaces. Kept per tab in sessionStorage, like the API key, and guarded the same
// way: storage unavailable reads as `default`.
const STORAGE_KEY = 'queryapigate-workspace';
export const DEFAULT_WORKSPACE = 'default';

export function getWorkspace(): string {
  try {
    return sessionStorage.getItem(STORAGE_KEY) || DEFAULT_WORKSPACE;
  } catch {
    return DEFAULT_WORKSPACE;
  }
}

export function setWorkspace(name: string): void {
  try {
    if (name && name !== DEFAULT_WORKSPACE) sessionStorage.setItem(STORAGE_KEY, name);
    else sessionStorage.removeItem(STORAGE_KEY);
  } catch {
    // ignored - see above
  }
}

/** `path` in this tab's workspace. Sign-in (/auth/...) is the server's, and a path that names one already keeps it. */
export function workspacePath(path: string): string {
  const workspace = getWorkspace();
  if (workspace === DEFAULT_WORKSPACE || path.startsWith('/auth/') || path.startsWith('/w/')) return path;
  return `/w/${encodeURIComponent(workspace)}${path}`;
}

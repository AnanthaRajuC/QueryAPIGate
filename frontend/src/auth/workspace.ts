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
  moved.forEach((listener) => listener());
}

// Where a tab has to be before it asks for anything. /api/v1/me says who the tab is - and so whether it is in the
// wrong workspace: an administrator whose roles are all in workspaces is refused everything in `default`, and a tab
// left in a workspace that's gone is refused everything there. The shell moves it (app/workspaceHooks.ts); until it
// has, the API client holds every other request (settleWorkspace(), api/client.ts), so no screen, count or bell asks
// the workspace the tab is about to leave - they drew 403s, and 404s, before the move. And a tab that has to sign in
// first asks nothing else at all: the server would refuse it (401), and count every refusal as an error.

/** What /api/v1/me answered: who the tab is, or why it couldn't say. */
export type MeAnswer = {
  data?: { role: string | null; workspaces?: Record<string, string> | null };
  error?: unknown;
};

/** The workspace this tab has to move to before it asks for anything - or null when it's where it should be. */
export function workspaceToSettle(me: MeAnswer): string | null {
  const current = getWorkspace();
  const error = me.error as { status?: number; code?: string } | undefined;
  if (error?.status === 404 && error.code === 'workspace_not_found' && current !== DEFAULT_WORKSPACE)
    return DEFAULT_WORKSPACE;
  const memberships = Object.keys(me.data?.workspaces ?? {});
  const first = memberships[0];
  if (me.data && me.data.role === null && first !== undefined && !memberships.includes(current)) return first;
  return null;
}

const moved = new Set<() => void>();
let meAnswer: (() => Promise<MeAnswer>) | null = null;
/** How long a request waits for the move before going anyway - the server answers it either way. */
const SETTLE_WAIT_MS = 10_000;

/** Where settleWorkspace() reads /api/v1/me's answer: the Console's own query of it (app/data.ts), so asking is free
 * once the shell has. Unset - nothing has asked - requests go straight out. */
export function answerMeWith(source: (() => Promise<MeAnswer>) | null): void {
  meAnswer = source;
}

/** Resolves when this tab is in the workspace it should be - at once, or once the shell has moved it - or with
 * `signed-out` when /api/v1/me said the tab has to sign in first (401): anything else it asked would be refused too. */
export async function settleWorkspace(): Promise<'ready' | 'signed-out'> {
  if (!meAnswer) return 'ready';
  const answer = await meAnswer();
  if ((answer.error as { status?: number } | undefined)?.status === 401) return 'signed-out';
  if (workspaceToSettle(answer) === null) return 'ready';
  await new Promise<void>((resolve) => {
    const done = () => {
      moved.delete(done);
      clearTimeout(timer);
      resolve();
    };
    const timer = setTimeout(done, SETTLE_WAIT_MS);
    moved.add(done);
  });
  return 'ready';
}

/** `path` in this tab's workspace. Sign-in (/auth/...) is the server's, and a path that names one already keeps it. */
export function workspacePath(path: string): string {
  const workspace = getWorkspace();
  if (workspace === DEFAULT_WORKSPACE || path.startsWith('/auth/') || path.startsWith('/w/')) return path;
  return `/w/${encodeURIComponent(workspace)}${path}`;
}

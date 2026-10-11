import { useQuery } from '@tanstack/react-query';

// Console sign-in through SSO (ADR 0007). The session itself is an HttpOnly cookie the page can't read; what the page
// holds is the session's CSRF token, which the server wants with every request the cookie should authenticate. It is
// kept in memory only - a reload asks /auth/session again - and sent whenever this tab has no API key of its own (a
// key, when there is one, is what the server judges).

export interface Session {
  sso: boolean;
  signed_in: boolean;
  sso_only: boolean;
  admin?: string;
  role?: string;
  email?: string | null;
  csrf?: string;
  expires_at?: string;
}

let csrf = '';

export function getCsrf(): string {
  return csrf;
}

let ready: Promise<Session> | null = null;
const SIGNED_OUT: Session = { sso: false, signed_in: false, sso_only: false };

async function fetchSession(): Promise<Session> {
  const response = await globalThis.fetch(
    new URL('/auth/session', globalThis.location?.origin ?? 'http://localhost').toString(),
  );
  if (!response.ok) return SIGNED_OUT;
  const session = (await response.json()) as Session;
  csrf = session.signed_in && session.csrf ? session.csrf : '';
  return session;
}

/** The session, asked once per page: every request without an API key waits for it (api/client.ts), so the first
 * ones already carry the CSRF token. Never rejects - an unreadable answer is "signed out". */
export function loadSession(): Promise<Session> {
  if (!ready) ready = fetchSession().catch(() => SIGNED_OUT);
  return ready;
}

/** Forget what was asked (tests, and after signing out). */
export function resetSession(): void {
  ready = null;
  csrf = '';
}

export const SESSION_QUERY = {
  queryKey: ['session'],
  queryFn: loadSession,
  retry: false,
  staleTime: Infinity,
};

export function useSession() {
  return useQuery(SESSION_QUERY);
}

/** Where "Sign in with SSO" goes: the server's sign-in, coming back to the page the person is on. */
export function signInUrl(): string {
  const here = globalThis.location ? globalThis.location.pathname + globalThis.location.search : '/console/';
  return '/auth/sso/login?next=' + encodeURIComponent(here.startsWith('/console/') ? here : '/console/');
}

/** End the session; resolves to the provider's own sign-out page, where it has one. */
export async function signOut(): Promise<string | null> {
  const response = await globalThis.fetch(
    new URL('/auth/sso/logout', globalThis.location?.origin ?? 'http://localhost').toString(),
    { method: 'POST', headers: { 'X-QAG-CSRF': csrf } },
  );
  resetSession();
  if (!response.ok) return null;
  const body = (await response.json()) as { provider_sign_out?: string | null };
  return body.provider_sign_out ?? null;
}

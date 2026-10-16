import { useQueryClient } from '@tanstack/react-query';
import { useState, type FormEvent } from 'react';
import { useLocation, useNavigate } from 'react-router';

import { setApiKey } from '@/auth/apiKey';
import { signInUrl, useSession } from '@/auth/session';
import { ssoRefusal } from '@/auth/ssoRefusal';

import type { useMe } from './data';

/** The Console's front door, in place of the whole shell while signed out: SSO first when the server has it, and a
 * token - an administrator's own, or the shared admin key - always. */
export function SignIn({ me }: { me: ReturnType<typeof useMe> }) {
  const queryClient = useQueryClient();
  const session = useSession();
  const location = useLocation();
  const navigate = useNavigate();
  const [draft, setDraft] = useState('');
  const [tried, setTried] = useState(false);
  const refused = new URLSearchParams(location.search).get('sso_error');
  const sso = session.data?.sso ?? false;

  function submit(event: FormEvent) {
    event.preventDefault();
    const value = draft.trim();
    if (!value) return;
    setApiKey(value);
    setDraft('');
    setTried(true);
    if (refused) navigate({ pathname: location.pathname }, { replace: true }); // that refusal is answered now
    // Asked afresh. Invalidated rather than reset, so this page stays up - with its answer - until /me says who
    // the token is; the shell's queries start once it does.
    void queryClient.invalidateQueries();
  }

  return (
    <div className="signin" id="signin">
      <div className="signin-card">
        <div className="wordmark signin-mark">
          Query<b>API</b>Gate
        </div>
        <h1>Sign in to the Console</h1>
        {refused ? (
          <p className="signin-error" role="alert">
            {ssoRefusal(refused)}
          </p>
        ) : tried && !me.isFetching ? (
          <p className="signin-error" role="alert">
            That token wasn’t accepted - it may be mistyped, expired or revoked.
          </p>
        ) : null}
        {sso && (
          <>
            <a
              className="btn primary signin-sso"
              id="signin-sso"
              href={signInUrl()}
              onClick={() => setApiKey('')} // the session, not a key, from here on
            >
              Sign in with SSO
            </a>
            <div className="signin-or">or with a token</div>
          </>
        )}
        <form onSubmit={submit} autoComplete="off">
          <label htmlFor="signin-token">Admin token or API key</label>
          <input
            id="signin-token"
            type="password"
            autoComplete="off"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            autoFocus={!sso}
          />
          <button type="submit" className={sso ? 'btn' : 'btn primary'} disabled={!draft.trim()}>
            Sign in
          </button>
        </form>
        <p className="hint">
          Your own token comes from <code>queryapigate admins create</code> or an owner; the shared admin key
          is <code>QUERYAPIGATE_API_KEY</code>. It is kept for this tab only.
        </p>
      </div>
    </div>
  );
}

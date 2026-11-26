import { useQueryClient } from '@tanstack/react-query';
import { useState, type FormEvent } from 'react';
import { useLocation, useNavigate } from 'react-router';

import { setApiKey } from '@/auth/apiKey';
import { signInUrl, useSession } from '@/auth/session';
import { ssoRefusal } from '@/auth/ssoRefusal';
import {
  AlertIcon,
  CheckIcon,
  EyeIcon,
  EyeOffIcon,
  KeyIcon,
  LockIcon,
  LogoMark,
  ShieldIcon,
} from '@/components/icons';

import { useHealth, type useMe } from './data';

/** The Console's front door, in place of the whole shell while signed out: SSO first when the server has it, and a
 * token - an administrator's own, or the shared admin key - always. On a wide screen a brand panel sits beside the
 * form, with this server's version and health (GET /health, which needs no key); on a narrow one, the form alone. */
export function SignIn({
  me,
  tried,
  onTry,
}: {
  me: ReturnType<typeof useMe>;
  /** A token was tried and /me refused it - kept by the shell, which stays mounted (AppShell). */
  tried: boolean;
  onTry: () => void;
}) {
  const queryClient = useQueryClient();
  const session = useSession();
  const health = useHealth();
  const location = useLocation();
  const navigate = useNavigate();
  const [draft, setDraft] = useState('');
  const [shown, setShown] = useState(false);
  const refused = new URLSearchParams(location.search).get('sso_error');
  const sso = session.data?.sso ?? false;
  const checking = tried && me.isFetching;
  const error = refused
    ? ssoRefusal(refused)
    : tried && me.isError && !me.isFetching
      ? 'That token wasn’t accepted - it may be mistyped, expired or revoked.'
      : null;

  function submit(event: FormEvent) {
    event.preventDefault();
    const value = draft.trim();
    if (!value) return;
    setApiKey(value);
    setDraft('');
    setShown(false);
    onTry();
    if (refused) navigate({ pathname: location.pathname }, { replace: true }); // that refusal is answered now
    // Asked afresh. Invalidated rather than reset, so this page stays up - with its answer - until /me says who
    // the token is; the shell's queries start once it does. /health, which this page shows, needs no key: a token
    // doesn't change it, so it isn't asked again.
    void queryClient.invalidateQueries({ predicate: (query) => query.queryKey[0] !== 'health' });
  }

  return (
    <div className="signin" id="signin">
      <aside className="signin-brand">
        <div className="signin-lockup">
          <LogoMark size={40} />
          <span className="wordmark">
            Query<b>API</b>Gate
          </span>
        </div>
        <div className="signin-pitch">
          <p className="signin-tagline">Turn SQL into governed data access.</p>
          <ul className="signin-points">
            <li>
              <CheckIcon />
              Saved queries as REST endpoints for your apps
            </li>
            <li>
              <CheckIcon />
              The same queries as MCP tools for AI agents
            </li>
            <li>
              <CheckIcon />
              Keys, roles, masking and a tamper-evident audit log
            </li>
          </ul>
        </div>
        <div className="signin-server">
          <span className={`dot ${health.isError ? 'bad' : health.isSuccess ? 'ok' : ''}`} />
          {health.data
            ? `This server: v${health.data.version}, up`
            : health.isError
              ? 'This server isn’t answering /health'
              : 'This server: checking…'}
        </div>
      </aside>

      <main className="signin-main">
        <div className="signin-card">
          <div className="signin-lockup signin-lockup-small">
            <LogoMark size={32} />
            <span className="wordmark">
              Query<b>API</b>Gate
            </span>
          </div>
          <div className="signin-head">
            <h1>Sign in to the Console</h1>
            <p>
              {sso
                ? 'With your organisation’s single sign-on, or a token.'
                : 'With your admin token, or the shared admin key.'}
            </p>
          </div>
          {error && (
            <p className="signin-error" role="alert">
              <AlertIcon />
              <span>{error}</span>
            </p>
          )}
          {sso && (
            <>
              <a
                className="btn primary signin-sso"
                id="signin-sso"
                href={signInUrl()}
                onClick={() => setApiKey('')} // the session, not a key, from here on
              >
                <ShieldIcon />
                Sign in with SSO
              </a>
              <div className="signin-or">
                <span>or with a token</span>
              </div>
            </>
          )}
          <form onSubmit={submit} autoComplete="off">
            <label htmlFor="signin-token">Admin token or API key</label>
            <div className="signin-field">
              <KeyIcon />
              <input
                id="signin-token"
                type={shown ? 'text' : 'password'}
                autoComplete="off"
                spellCheck={false}
                placeholder="qagadm_… or your admin key"
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                autoFocus={!sso}
              />
              <button
                type="button"
                className="signin-reveal"
                aria-label={shown ? 'Hide token' : 'Show token'}
                title={shown ? 'Hide token' : 'Show token'}
                onClick={() => setShown((s) => !s)}
              >
                {shown ? <EyeOffIcon /> : <EyeIcon />}
              </button>
            </div>
            <button
              type="submit"
              className={sso ? 'btn signin-submit' : 'btn primary signin-submit'}
              disabled={!draft.trim() || checking}
            >
              {checking ? 'Signing in…' : 'Sign in'}
            </button>
          </form>
          <div className="signin-note">
            <LockIcon />
            <p>
              Kept for this tab only - closing it signs you out. Your own token comes from{' '}
              <code>queryapigate admins create</code> or an owner; the shared admin key is{' '}
              <code>QUERYAPIGATE_API_KEY</code>.
            </p>
          </div>
        </div>
        <footer className="signin-foot">
          <a href="/docs" target="_blank" rel="noreferrer">
            API reference
          </a>
          <span aria-hidden="true">·</span>
          <a href="https://anantharajuc.github.io/QueryAPIGate/" target="_blank" rel="noreferrer">
            Documentation
          </a>
          {health.data && (
            <>
              <span aria-hidden="true">·</span>
              <span className="signin-version">v{health.data.version}</span>
            </>
          )}
        </footer>
      </main>
    </div>
  );
}

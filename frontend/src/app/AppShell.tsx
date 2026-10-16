import { useEffect, useState } from 'react';
import { Outlet, useLocation, useNavigate } from 'react-router';

import { DrawerHost, ErrorBanner, FeedbackProvider, Toasts, useFeedback } from './feedback';
import { Header } from './Header';
import { usePrefs } from './prefs';
import { mustSignIn, useMe } from './data';
import { activeItem, Sidebar } from './Sidebar';
import { SignIn } from './SignIn';
import { useSettleWorkspace } from './workspaceHooks';
import { ssoRefusal } from '@/auth/ssoRefusal';

/** The classic page frame (ui.py <body>): sidebar, then content (header, error banner, main), then the drawer,
 * palette and toasts. #root is display:contents, so these lay out exactly as the classic body grid. */
export function AppShell() {
  usePrefs();
  // The classic section id (tab-queries, tab-run, ...): some classic.css rules key on it, e.g. the API Designer's
  // full-width page (main:has(> #tab-run.active)).
  const section = activeItem(useLocation().pathname);
  // A server that needs a sign-in shows only that, once it has said so (401 from /api/v1/me).
  const me = useMe();
  useSettleWorkspace();
  const signedOut = mustSignIn(me);
  // Once this tab has been shown the sign-in page, it stays there until /me says who the tab is - not the shell for
  // the moment a sign-in attempt's /me is in flight, which unmounted the page and lost what it had to say. Whether a
  // token was tried lives here, in the frame that stays, for the same reason.
  const [gated, setGated] = useState(false);
  const [tried, setTried] = useState(false);
  // adjusted while rendering, React's way for state that follows other state - each settles in one pass
  if (signedOut && !gated) setGated(true);
  if (me.isSuccess && (gated || tried)) {
    setGated(false);
    setTried(false);
  }
  if (signedOut || (gated && !me.isSuccess))
    return <SignIn me={me} tried={tried} onTry={() => setTried(true)} />;
  return (
    <FeedbackProvider>
      {/* Not before /me has answered: signed out, the sign-in page says why SSO refused, not the banner. */}
      {!me.isPending && <SignInRefused />}
      <Sidebar />
      <div className="content">
        <Header />
        <ErrorBanner />
        <main>
          <section className="active" id={section ? `tab-${section.tab}` : undefined}>
            <Outlet />
          </section>
        </main>
      </div>
      <DrawerHost />
      <Toasts />
    </FeedbackProvider>
  );
}

/** A sign-in through SSO the server refused comes back as ?sso_error=...: said once, in the error banner, and taken
 * off the address so a reload doesn't say it again. */
function SignInRefused() {
  const { showError } = useFeedback();
  const location = useLocation();
  const navigate = useNavigate();
  useEffect(() => {
    const params = new URLSearchParams(location.search);
    const code = params.get('sso_error');
    if (!code) return;
    showError(ssoRefusal(code));
    params.delete('sso_error');
    params.delete('sso_message');
    const rest = params.toString();
    navigate({ pathname: location.pathname, search: rest ? '?' + rest : '' }, { replace: true });
  }, [location.pathname, location.search, navigate, showError]);
  return null;
}

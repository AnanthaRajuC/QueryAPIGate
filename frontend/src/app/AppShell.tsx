import { useEffect } from 'react';
import { Outlet, useLocation, useNavigate } from 'react-router';

import { DrawerHost, ErrorBanner, FeedbackProvider, Toasts, useFeedback } from './feedback';
import { Header } from './Header';
import { usePrefs } from './prefs';
import { activeItem, Sidebar } from './Sidebar';

/** The classic page frame (ui.py <body>): sidebar, then content (header, error banner, main), then the drawer,
 * palette and toasts. #root is display:contents, so these lay out exactly as the classic body grid. */
export function AppShell() {
  usePrefs();
  // The classic section id (tab-queries, tab-run, ...): some classic.css rules key on it, e.g. the API Designer's
  // full-width page (main:has(> #tab-run.active)).
  const section = activeItem(useLocation().pathname);
  return (
    <FeedbackProvider>
      <SignInRefused />
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

/** A sign-in through SSO the server refused comes back as ?sso_error=...&sso_message=...: said once, in the error
 * banner, and taken off the address so a reload doesn't say it again. */
function SignInRefused() {
  const { showError } = useFeedback();
  const location = useLocation();
  const navigate = useNavigate();
  useEffect(() => {
    const params = new URLSearchParams(location.search);
    const code = params.get('sso_error');
    if (!code) return;
    showError(`Sign-in through SSO didn't go ahead: ${params.get('sso_message') || code} (${code})`);
    params.delete('sso_error');
    params.delete('sso_message');
    const rest = params.toString();
    navigate({ pathname: location.pathname, search: rest ? '?' + rest : '' }, { replace: true });
  }, [location.pathname, location.search, navigate, showError]);
  return null;
}

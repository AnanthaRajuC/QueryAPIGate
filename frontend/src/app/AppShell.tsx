import { Outlet, useLocation } from 'react-router';

import { DrawerHost, ErrorBanner, FeedbackProvider, Toasts } from './feedback';
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

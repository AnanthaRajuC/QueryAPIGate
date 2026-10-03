import { Outlet } from 'react-router';

import { DrawerHost, ErrorBanner, FeedbackProvider, Toasts } from './feedback';
import { Header } from './Header';
import { usePrefs } from './prefs';
import { Sidebar } from './Sidebar';

/** The classic page frame (ui.py <body>): sidebar, then content (header, error banner, main), then the drawer,
 * palette and toasts. #root is display:contents, so these lay out exactly as the classic body grid. */
export function AppShell() {
  usePrefs();
  return (
    <FeedbackProvider>
      <Sidebar />
      <div className="content">
        <Header />
        <ErrorBanner />
        <main>
          <section className="active">
            <Outlet />
          </section>
        </main>
      </div>
      <DrawerHost />
      <Toasts />
    </FeedbackProvider>
  );
}

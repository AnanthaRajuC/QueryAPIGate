import { lazy, Suspense } from 'react';
import { Navigate, Route, Routes } from 'react-router';

import { AppShell } from './AppShell';
import { Loading } from './feedback';

// The API Repository carries the SQL editor - loaded on first visit, so the shell stays small.
const RepositoryPage = lazy(() =>
  import('@/features/repository/RepositoryPage').then((m) => ({ default: m.RepositoryPage })),
);

const DesignerPage = lazy(() =>
  import('@/features/designer/DesignerPage').then((m) => ({ default: m.DesignerPage })),
);

const ConnectionsPage = lazy(() =>
  import('@/features/connections/ConnectionsPage').then((m) => ({ default: m.ConnectionsPage })),
);

const ApiKeysPage = lazy(() =>
  import('@/features/access/ApiKeysPage').then((m) => ({ default: m.ApiKeysPage })),
);

const RolesPage = lazy(() => import('@/features/access/RolesPage').then((m) => ({ default: m.RolesPage })));

const MetricsPage = lazy(() =>
  import('@/features/observability/MetricsPage').then((m) => ({ default: m.MetricsPage })),
);

const AuditLogPage = lazy(() =>
  import('@/features/observability/AuditLogPage').then((m) => ({ default: m.AuditLogPage })),
);

const SettingsPage = lazy(() =>
  import('@/features/settings/SettingsPage').then((m) => ({ default: m.SettingsPage })),
);

const HomePage = lazy(() => import('@/features/home/HomePage').then((m) => ({ default: m.HomePage })));

const CachingPage = lazy(() =>
  import('@/features/caching/CachingPage').then((m) => ({ default: m.CachingPage })),
);

const AccessMapPage = lazy(() =>
  import('@/features/accessmap/AccessMapPage').then((m) => ({ default: m.AccessMapPage })),
);

const HelpPage = lazy(() => import('@/features/help/HelpPage').then((m) => ({ default: m.HelpPage })));

export function App() {
  return (
    <Routes>
      <Route element={<AppShell />}>
        <Route
          index
          element={
            <Suspense fallback={<Loading />}>
              <HomePage />
            </Suspense>
          }
        />
        <Route
          path="queries/:name?"
          element={
            <Suspense fallback={<Loading />}>
              <RepositoryPage />
            </Suspense>
          }
        />
        <Route
          path="designer"
          element={
            <Suspense fallback={<Loading />}>
              <DesignerPage />
            </Suspense>
          }
        />
        <Route
          path="connections"
          element={
            <Suspense fallback={<Loading />}>
              <ConnectionsPage />
            </Suspense>
          }
        />
        <Route
          path="api-keys"
          element={
            <Suspense fallback={<Loading />}>
              <ApiKeysPage />
            </Suspense>
          }
        />
        <Route
          path="roles"
          element={
            <Suspense fallback={<Loading />}>
              <RolesPage />
            </Suspense>
          }
        />
        <Route
          path="metrics"
          element={
            <Suspense fallback={<Loading />}>
              <MetricsPage />
            </Suspense>
          }
        />
        <Route
          path="audit-log"
          element={
            <Suspense fallback={<Loading />}>
              <AuditLogPage />
            </Suspense>
          }
        />
        <Route
          path="settings"
          element={
            <Suspense fallback={<Loading />}>
              <SettingsPage />
            </Suspense>
          }
        />
        <Route
          path="caching"
          element={
            <Suspense fallback={<Loading />}>
              <CachingPage />
            </Suspense>
          }
        />
        <Route
          path="access-map"
          element={
            <Suspense fallback={<Loading />}>
              <AccessMapPage />
            </Suspense>
          }
        />
        <Route
          path="help"
          element={
            <Suspense fallback={<Loading />}>
              <HelpPage />
            </Suspense>
          }
        />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}

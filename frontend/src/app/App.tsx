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

export function App() {
  return (
    <Routes>
      <Route element={<AppShell />}>
        {/* Every other screen still lives in the classic UI (the sidebar links there), so the Console opens on the
            one it has rebuilt. */}
        <Route index element={<Navigate to="/queries" replace />} />
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
        <Route path="*" element={<Navigate to="/queries" replace />} />
      </Route>
    </Routes>
  );
}

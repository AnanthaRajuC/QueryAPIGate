import { lazy, Suspense } from 'react';
import { Route, Routes } from 'react-router';

import { ClassicScreen } from '@/features/classic/ClassicScreen';
import { OverviewPage } from '@/features/overview/OverviewPage';

import { AppShell } from './AppShell';
import { ALL_ITEMS } from './navigation';
import { NotFound } from './NotFound';

// The Queries screens carry the SQL editor, tables and forms - loaded on first visit, so the rest of the Console
// stays small.
const QueriesListPage = lazy(() =>
  import('@/features/queries/QueriesListPage').then((m) => ({ default: m.QueriesListPage })),
);
const QueryDetailPage = lazy(() =>
  import('@/features/queries/QueryDetailPage').then((m) => ({ default: m.QueryDetailPage })),
);
const QueryEditorPage = lazy(() =>
  import('@/features/queries/QueryEditorPage').then((m) => ({ default: m.QueryEditorPage })),
);

function Loading() {
  return <p className="text-sm text-ink-3">Loading…</p>;
}

export function App() {
  return (
    <Routes>
      <Route element={<AppShell />}>
        <Route index element={<OverviewPage />} />
        <Route path="queries">
          <Route
            index
            element={
              <Suspense fallback={<Loading />}>
                <QueriesListPage />
              </Suspense>
            }
          />
          <Route
            path="new"
            element={
              <Suspense fallback={<Loading />}>
                <QueryEditorPage />
              </Suspense>
            }
          />
          <Route
            path=":name"
            element={
              <Suspense fallback={<Loading />}>
                <QueryDetailPage />
              </Suspense>
            }
          />
          <Route
            path=":name/edit"
            element={
              <Suspense fallback={<Loading />}>
                <QueryEditorPage />
              </Suspense>
            }
          />
        </Route>
        {ALL_ITEMS.filter((item) => item.classicTab).map((item) => (
          <Route key={item.path} path={item.path} element={<ClassicScreen item={item} />} />
        ))}
        <Route path="*" element={<NotFound />} />
      </Route>
    </Routes>
  );
}

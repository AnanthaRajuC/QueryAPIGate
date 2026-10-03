import { Route, Routes } from 'react-router';

import { ClassicScreen } from '@/features/classic/ClassicScreen';
import { OverviewPage } from '@/features/overview/OverviewPage';

import { AppShell } from './AppShell';
import { ALL_ITEMS } from './navigation';
import { NotFound } from './NotFound';

export function App() {
  return (
    <Routes>
      <Route element={<AppShell />}>
        <Route index element={<OverviewPage />} />
        {ALL_ITEMS.filter((item) => item.classicTab).map((item) => (
          <Route key={item.path} path={item.path} element={<ClassicScreen item={item} />} />
        ))}
        <Route path="*" element={<NotFound />} />
      </Route>
    </Routes>
  );
}

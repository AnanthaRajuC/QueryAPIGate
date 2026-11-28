import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render } from '@testing-library/react';
import { MemoryRouter } from 'react-router';

import { App } from '@/app/App';

/** The app at `path`; `state` is the location state a link would carry (Home's request rows open a query's tab). */
export function renderAt(path: string, state?: unknown) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[state === undefined ? path : { pathname: path, state }]}>
        <App />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

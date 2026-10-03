import { Link } from 'react-router';

import { useCatalog, useHealth } from '@/api/queries';
import { ALL_ITEMS } from '@/app/navigation';
import { getApiKey } from '@/auth/apiKey';
import { Badge } from '@/components/ui/badge';
import { Card, CardTitle } from '@/components/ui/card';

export function OverviewPage() {
  const health = useHealth();
  const catalog = useCatalog(getApiKey());
  const moved = ALL_ITEMS.filter((item) => !item.classicTab);
  const classic = ALL_ITEMS.filter((item) => item.classicTab);

  return (
    <div className="flex max-w-4xl flex-col gap-4">
      <h1 className="text-lg font-semibold">Overview</h1>

      <div className="grid gap-4 sm:grid-cols-2">
        <Card>
          <CardTitle>Server</CardTitle>
          {health.isPending && <p className="mt-2 text-sm text-ink-3">Checking…</p>}
          {health.isError && (
            <p role="alert" className="mt-2 text-sm text-danger">
              Unreachable: {health.error.message}
            </p>
          )}
          {health.isSuccess && (
            <p className="mt-2 flex items-center gap-2 text-sm">
              <Badge variant="accent">{health.data.status}</Badge>
              version <span className="font-mono">{health.data.version}</span>
            </p>
          )}
        </Card>

        <Card>
          <CardTitle>You</CardTitle>
          {catalog.isPending && <p className="mt-2 text-sm text-ink-3">Checking…</p>}
          {catalog.isError && (
            <p className="mt-2 text-sm text-ink-2">Sign in with an API key to see your access.</p>
          )}
          {catalog.isSuccess && (
            <ul className="mt-2 space-y-1 text-sm">
              <li>
                Key: <strong>{catalog.data.caller?.name ?? 'admin'}</strong>
                {catalog.data.caller?.admin && <span className="text-ink-3"> (admin)</span>}
              </li>
              <li>Saved queries you can run: {catalog.data.queries?.length ?? 0}</li>
              <li>Writes: {catalog.data.caller?.allow_writes ? 'allowed' : 'read-only'}</li>
            </ul>
          )}
        </Card>
      </div>

      <Card>
        <CardTitle>Migration progress</CardTitle>
        <p className="mt-1 text-sm text-ink-2">
          {moved.length} of {ALL_ITEMS.length} screens are in the Console. The rest open in the classic UI
          until they move over.
        </p>
        <ul className="mt-3 flex flex-wrap gap-2">
          {classic.map((item) => (
            <li key={item.path}>
              <Link to={item.path} className="text-xs">
                <Badge>{item.label}</Badge>
              </Link>
            </li>
          ))}
        </ul>
      </Card>
    </div>
  );
}

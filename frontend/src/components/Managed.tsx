import { useManaged } from '@/app/data';

export type ManagedKind =
  'connections' | 'destinations' | 'queries' | 'roles' | 'api_keys' | 'descriptions' | 'exports';

// "managed": a configuration file (ADR 0014) created, changed or named this - and with QUERYAPIGATE_CONFIG_LOCKED,
// the server takes changes to it only from a file.
export function ManagedTag({ kind, name }: { kind: ManagedKind; name: string }) {
  const managed = useManaged();
  const items = managed.data?.items ?? [];
  if (!items.some((item) => item.kind === kind && item.name === name)) return null;
  const locked = managed.data?.locked ?? false;
  return (
    <span
      className="tag managed"
      title={
        locked
          ? 'Managed by a configuration file, and locked to it: change it in the file'
          : 'Managed by a configuration file: a change made here shows as drift in `queryapigate config diff`, ' +
            'and the next apply of the file undoes it'
      }
    >
      {locked ? 'managed · locked' : 'managed'}
    </span>
  );
}

import type { ApiKeyEntry, RoleEntry } from '@/app/data';

// Who can reach a saved query, and why (ui.py reachVia/queryReach/reachDot/accessPill) - mirrors
// apikeys.can_run_saved(): a named `queries` grant, a `collections` grant covering the query's collection, or a
// `connections` grant covering its connection. The admin key is never listed: it reaches everything.

export type Via = { label: string; kind: 'query' | 'collection' | 'connection' };
export type Reacher = { name: string; active?: boolean; via: Via[] };
export type Reach = { keys: Reacher[]; roles: Reacher[] };

const CODE: Record<Via['kind'], string> = { query: 'Q', collection: 'C', connection: 'W' };

export function reachVia(
  entry: RoleEntry,
  query: string,
  collection: string | null,
  connection: string | null,
): Via[] {
  const via: Via[] = [];
  if (entry.queries === '*') via.push({ label: 'all queries', kind: 'query' });
  else if ((entry.queries ?? []).some((q) => (typeof q === 'object' ? q.name : q) === query))
    via.push({ label: 'named', kind: 'query' });
  if (collection && Array.isArray(entry.collections) && entry.collections.includes(collection)) {
    via.push({ label: 'collection ' + collection, kind: 'collection' });
  }
  if (entry.connections === '*') via.push({ label: 'all connections', kind: 'connection' });
  else if (connection && Array.isArray(entry.connections) && entry.connections.includes(connection)) {
    via.push({ label: 'connection ' + connection, kind: 'connection' });
  }
  return via;
}

export function queryReach(
  keys: Record<string, ApiKeyEntry>,
  roles: Record<string, RoleEntry>,
  query: string,
  collection: string | null,
  connection: string | null,
): Reach {
  return {
    keys: Object.keys(keys)
      .sort()
      .map((name) => ({
        name,
        active: keys[name]!.active,
        via: reachVia(keys[name]!, query, collection, connection),
      }))
      .filter((k) => k.via.length),
    roles: Object.keys(roles)
      .sort()
      .map((name) => ({ name, via: reachVia(roles[name]!, query, collection, connection) }))
      .filter((r) => r.via.length),
  };
}

export function ReachDot({ via, role }: { via: Via[]; role?: boolean }) {
  const codes = [...new Set(via.map((v) => CODE[v.kind]))];
  const connOnly = codes.length === 1 && codes[0] === 'W';
  return (
    <span
      className={['amap-dot', role ? 'role' : '', connOnly ? 'conn' : ''].filter(Boolean).join(' ')}
      title={via.map((v) => v.label).join(', ')}
    >
      {codes.join('·')}
    </span>
  );
}

export function AccessPill({ entry, role }: { entry: Reacher; role?: boolean }) {
  return (
    <span className="access-pill">
      <span className="name">{entry.name + (entry.active === false ? ' (revoked)' : '')}</span>
      <ReachDot via={entry.via} role={role} />
    </span>
  );
}

export function AccessCell({
  entry,
}: {
  entry: Partial<Pick<RoleEntry, 'allow_writes' | 'allowed_write_ops' | 'allowed_tables'>>;
}) {
  const allowWrites = Boolean(entry.allow_writes);
  const ops = entry.allowed_write_ops ?? [];
  const tables = entry.allowed_tables ?? [];
  const text = !allowWrites
    ? 'Read-only'
    : ops.length
      ? `Read/write (${ops.length}${ops.length === 1 ? ' op' : ' ops'})`
      : 'Read/write';
  const main = <span title={ops.length && allowWrites ? ops.join(', ') : undefined}>{text}</span>;
  const badge = tables.length ? (
    <span className="tag" title={tables.join(', ')}>
      {tables.length + (tables.length === 1 ? ' table' : ' tables')}
    </span>
  ) : null;
  return (
    <td className={!allowWrites ? 'dim' : ''} style={{ whiteSpace: 'nowrap' }}>
      {badge ? (
        <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
          {main}
          {badge}
        </div>
      ) : (
        main
      )}
    </td>
  );
}

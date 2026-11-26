import type { RoleEntry } from '@/app/data';

// A key's or role's grants as values the list tables sort and filter by (TableControls): the API keys and Roles
// screens share them.

/** A key's or role's grants as words, for the table filter: "all connections", connection, query and collection names. */
export function grantWords(entry: RoleEntry): string[] {
  const words = entry.connections === '*' ? ['all connections'] : [...(entry.connections ?? [])];
  if (entry.queries === '*') words.push('all queries');
  else (entry.queries ?? []).forEach((q) => words.push(typeof q === 'object' ? q.name : q));
  (entry.collections ?? []).forEach((c) => words.push(c + '/'));
  return words;
}

/** How wide a grant is, to sort by: everything first when sorted descending, then by how many things it names. */
export function grantBreadth(entry: RoleEntry): number {
  const all = (entry.connections === '*' ? 1 : 0) + (entry.queries === '*' ? 1 : 0);
  return all * 10_000 + grantWords(entry).length;
}

/** "100/minute" as calls a second, so rate limits sort by how much they allow; none (the server default) is blank. */
export function ratePerSecond(limit: string | null | undefined): number | null {
  const m = /^\s*(\d+)\s*\/\s*(second|minute|hour|day)s?\s*$/i.exec(limit ?? '');
  if (!m) return null;
  const seconds: Record<string, number> = { second: 1, minute: 60, hour: 3600, day: 86_400 };
  return Number(m[1]) / seconds[m[2]!.toLowerCase()]!;
}

/** Write access as a number to sort by: read-only, read/write limited to some operations, read/write. */
export function writeLevel(entry: Pick<RoleEntry, 'allow_writes' | 'allowed_write_ops'>): number {
  return !entry.allow_writes ? 0 : entry.allowed_write_ops?.length ? 1 : 2;
}

import { useQuery, useQueryClient, type QueryClient } from '@tanstack/react-query';

import { api, ApiError, unwrap, type Schemas } from '@/api/client';

// Data the shell and several screens share. /api/v1 where it exists; the legacy routes (typed loosely here) for
// what hasn't got a v1 resource yet - collections.

export type Connection = Schemas['Connection'];
export type QuerySummary = Schemas['QuerySummary'];

/** The grants a key or role carries - what reach and access cells read. */
export type RoleEntry = Schemas['Grants'];
export type Role = Schemas['Role'];
export type ApiKeyEntry = Schemas['ApiKey'];
export interface CollectionsInfo {
  collections: Record<string, { queries: string[]; keys: string[]; roles: string[] }>;
  uncollected: string[];
}

export function useHealth() {
  return useQuery({ queryKey: ['health'], queryFn: async () => unwrap(await api.GET('/health')) });
}

export function useConnections() {
  return useQuery({
    queryKey: ['connections'],
    queryFn: async () => unwrap(await api.GET('/api/v1/connections')).items,
    retry: false,
  });
}

export function useAllQueries() {
  return useQuery({
    queryKey: ['queries', 'list', '', ''],
    queryFn: async () => unwrap(await api.GET('/api/v1/queries')).items,
    retry: false,
  });
}

const byName = <T extends { name: string }>(items: T[]) =>
  Object.fromEntries(items.map((item) => [item.name, item])) as Record<string, T>;

/** Every API key, by name (never secrets). */
export function useApiKeys() {
  const gate = useGate('access.read', 'API keys');
  return useQuery({
    queryKey: ['apikeys'],
    queryFn: async () => {
      await gate.check();
      return byName(unwrap(await api.GET('/api/v1/api-keys')).items);
    },
    enabled: gate.ready,
    retry: false,
  });
}

/** Every role, by name. */
export function useRoles() {
  const gate = useGate('access.read', 'roles');
  return useQuery({
    queryKey: ['roles'],
    queryFn: async () => {
      await gate.check();
      return byName(unwrap(await api.GET('/api/v1/roles')).items);
    },
    enabled: gate.ready,
    retry: false,
  });
}

// What a configuration file manages (ADR 0014) - and whether the server takes changes to it only from a file.
export function useManaged() {
  return useQuery({
    queryKey: ['config-managed'],
    queryFn: async () => unwrap(await api.GET('/api/v1/config/managed')),
    retry: false,
    staleTime: 60_000,
  });
}

export function useCollections() {
  return useQuery({
    queryKey: ['collections'],
    queryFn: async (): Promise<CollectionsInfo> => {
      const listing = unwrap(await api.GET('/api/v1/collections'));
      // keyed by name, as the screens look collections up
      return {
        collections: Object.fromEntries(listing.items.map(({ name, ...entry }) => [name, entry])),
        uncollected: listing.uncollected,
      };
    },
    retry: false,
  });
}

/** The server's configuration by section (admin only) - Settings, and Caching's backend tile. */
export function useSettings() {
  return useQuery({
    queryKey: ['settings'],
    queryFn: async () => unwrap(await api.GET('/api/v1/settings')).items,
    retry: false,
  });
}

export type Me = Schemas['Me'];

/** Who this tab is signed in as (/api/v1/me): name, admin role and its capabilities. Not an administrator (a scoped
 * key, or a wrong key) is an error here. */
const ME_QUERY = {
  queryKey: ['me'],
  queryFn: async () => unwrap(await api.GET('/api/v1/me')),
  retry: false,
  staleTime: 60_000,
};

export type Workspace = Schemas['Workspace'];

/** The workspaces this caller sees (ADR 0023): every one for a server-wide owner, admin or auditor; their own
 * otherwise. */
export function useWorkspaces(enabled = true) {
  return useQuery({
    queryKey: ['workspaces'],
    queryFn: async () => unwrap(await api.GET('/api/v1/workspaces')).items,
    enabled,
    retry: false,
  });
}

export function useMe() {
  return useQuery(ME_QUERY);
}

/** Whether this tab has to sign in first: the server answered /api/v1/me with 401 - no key, a wrong or expired one,
 * no session. A server open to all answers it, and a key that isn't an administrator's gets 403: neither is asked to
 * sign in here (the pages say what that key may not do, as before). */
export function mustSignIn(me: ReturnType<typeof useMe>): boolean {
  return me.isError && me.error instanceof ApiError && me.error.status === 401;
}

/** Whether the caller's role holds `capability`, asked when a request is about to be made - so a request fired right
 * after the key changes waits for the new answer instead of trusting the old one. Unknown (not an administrator, or
 * /api/v1/me unreadable) counts as allowed: the server decides. */
async function currentlyAllowed(client: QueryClient, capability: string) {
  // Unreadable once is unreadable: asking again would put it back to pending and start every gated query over.
  if (client.getQueryState(ME_QUERY.queryKey)?.status === 'error') return { allowed: true, role: null };
  try {
    const me = await client.ensureQueryData(ME_QUERY);
    return { allowed: me.capabilities.includes(capability), role: me.role };
  } catch {
    return { allowed: true, role: null };
  }
}

/** `can('access.write')`: whether the signed-in administrator's role holds a capability (adminroles.py), so a screen
 * can hide what would only be refused. While that isn't known - loading, or /api/v1/me unreadable - everything is
 * shown, as before roles existed: the server decides either way. `data` asks whether SQL may be run at all. */
export function useCan() {
  const me = useMe();
  return (capability: string) => {
    if (!me.data) return true;
    if (capability === 'data') return me.data.data_access;
    return me.data.capabilities.includes(capability);
  };
}

/** For data only some roles may read: wait until /api/v1/me has answered, then fail without a request - with a
 * message saying why - when the role lacks `capability`, instead of drawing a 403 from the server. */
export function useGate(capability: string, what: string) {
  const me = useMe();
  const client = useQueryClient();
  return {
    ready: me.isFetched, // stays true while it refetches; false again only once the key changes
    check: async () => {
      const { allowed, role } = await currentlyAllowed(client, capability);
      if (!allowed) throw new Error(`The ${role} role can't see ${what}.`);
    },
  };
}

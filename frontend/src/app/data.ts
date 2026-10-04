import { useQuery } from '@tanstack/react-query';

import { api, apiJson, unwrap, type Schemas } from '@/api/client';

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
  return useQuery({
    queryKey: ['apikeys'],
    queryFn: async () => byName(unwrap(await api.GET('/api/v1/api-keys')).items),
    retry: false,
  });
}

/** Every role, by name. */
export function useRoles() {
  return useQuery({
    queryKey: ['roles'],
    queryFn: async () => byName(unwrap(await api.GET('/api/v1/roles')).items),
    retry: false,
  });
}

export function useCollections() {
  return useQuery({
    queryKey: ['collections'],
    queryFn: () => apiJson<CollectionsInfo>('/collections'),
    retry: false,
  });
}

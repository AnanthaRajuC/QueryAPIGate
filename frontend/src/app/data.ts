import { useQuery } from '@tanstack/react-query';

import { api, apiJson, unwrap, type Schemas } from '@/api/client';

// Data the shell and several screens share. /api/v1 where it exists; the legacy routes (typed loosely here) for
// what hasn't got a v1 resource yet - API keys, roles, collections.

export type Connection = Schemas['Connection'];
export type QuerySummary = Schemas['QuerySummary'];

/** A role's grants (legacy GET /roles) - the fields the Console reads. */
export interface RoleEntry {
  connections?: string[] | '*';
  queries?: (string | { name: string; allow_writes?: boolean })[] | '*';
  collections?: string[] | '*';
  allow_writes?: boolean;
  allowed_write_ops?: string[];
  allowed_tables?: string[];
  rate_limit?: string | null;
}
/** An API key (legacy GET /api_keys): a role's grants plus its own state. */
export interface ApiKeyEntry extends RoleEntry {
  active?: boolean;
  expires_at?: string | null;
  last_used_at?: string | null;
  created_from_role?: string | null;
}
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

export function useApiKeys() {
  return useQuery({
    queryKey: ['apikeys'],
    queryFn: async () => (await apiJson<{ keys: Record<string, ApiKeyEntry> }>('/api_keys')).keys,
    retry: false,
  });
}

export function useRoles() {
  return useQuery({
    queryKey: ['roles'],
    queryFn: async () => (await apiJson<{ roles: Record<string, RoleEntry> }>('/roles')).roles,
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

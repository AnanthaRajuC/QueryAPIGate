import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { api, unwrap, unwrapEmpty, unwrapWithEtag, type Schemas } from '@/api/client';

export type Query = Schemas['Query'];
export type QueryVersion = Schemas['QueryVersion'];
export type ParameterRule = Schemas['ParameterRule'];
export type QueryVersionInput = Schemas['QueryVersionInput'];
export type QueryCreateInput = Schemas['QueryCreateInput'];
export type HistoryEntry = Schemas['HistoryEntry'];

export const keys = {
  all: ['queries'] as const,
  detail: (name: string) => ['queries', 'detail', name] as const,
  history: (name: string, version: number) => ['queries', 'history', name, version] as const,
};

/** A query with its ETag, so changes made from its screen are conditional (If-Match). */
export function useQueryDetail(name: string | undefined) {
  return useQuery({
    queryKey: keys.detail(name ?? ''),
    queryFn: async () =>
      unwrapWithEtag(await api.GET('/api/v1/queries/{name}', { params: { path: { name: name ?? '' } } })),
    enabled: Boolean(name),
    retry: false,
  });
}

/** A version's newest runs - what the classic detail view's stat tiles, chart and History tab are built from (the
 * same newest-50 window it always showed). */
export function useVersionHistory(name: string, version: number) {
  return useQuery({
    queryKey: keys.history(name, version),
    queryFn: async () =>
      unwrap(
        await api.GET('/api/v1/queries/{name}/history', {
          params: { path: { name }, query: { version, limit: 50 } },
        }),
      )
        .items.slice()
        .reverse(), // oldest first, as execution_history always was
  });
}

export type QueryFlow = Schemas['QueryFlow'];

/** Tables and joins a version's SQL touches, and sqlglot's formatting of it. */
export async function fetchFlow(name: string, version: number): Promise<QueryFlow> {
  return unwrap(
    await api.GET('/api/v1/queries/{name}/versions/{version}/flow', { params: { path: { name, version } } }),
  );
}

export function useQueryFlow(name: string, version: number, enabled = true) {
  return useQuery({
    queryKey: ['queries', 'flow', name, version],
    queryFn: () => fetchFlow(name, version),
    enabled,
    staleTime: Infinity,
  });
}

function ifMatch(etag: string | null | undefined) {
  return etag ? { 'If-Match': etag } : undefined;
}

export function useQueryActions(name: string) {
  const client = useQueryClient();
  const settle = () => {
    client.invalidateQueries({ queryKey: keys.all });
    client.invalidateQueries({ queryKey: ['collections'] });
  };
  return {
    publish: useMutation({
      mutationFn: async ({ version, etag }: { version: number; etag: string | null }) =>
        unwrap(
          await api.POST('/api/v1/queries/{name}/publish', {
            params: { path: { name }, header: ifMatch(etag) },
            body: { version },
          }),
        ),
      onSettled: settle,
    }),
    unpublish: useMutation({
      mutationFn: async ({ etag }: { etag: string | null }) =>
        unwrap(
          await api.POST('/api/v1/queries/{name}/unpublish', {
            params: { path: { name }, header: ifMatch(etag) },
          }),
        ),
      onSettled: settle,
    }),
    deleteVersion: useMutation({
      mutationFn: async ({ version, etag }: { version: number; etag: string | null }) =>
        unwrapEmpty(
          await api.DELETE('/api/v1/queries/{name}/versions/{version}', {
            params: { path: { name, version }, header: ifMatch(etag) },
          }),
        ),
      onSettled: settle,
    }),
    deleteQuery: useMutation({
      mutationFn: async ({ etag }: { etag: string | null }) =>
        unwrapEmpty(
          await api.DELETE('/api/v1/queries/{name}', { params: { path: { name }, header: ifMatch(etag) } }),
        ),
      onSettled: settle,
    }),
    move: useMutation({
      mutationFn: async ({ collection, etag }: { collection: string | null; etag: string | null }) =>
        unwrap(
          await api.PATCH('/api/v1/queries/{name}', {
            params: { path: { name }, header: ifMatch(etag) },
            body: { collection },
          }),
        ),
      onSettled: settle,
    }),
    setCacheTtl: useMutation({
      mutationFn: async ({
        version,
        ttl,
        etag,
      }: {
        version: number;
        ttl: number | null;
        etag: string | null;
      }) =>
        unwrap(
          await api.PATCH('/api/v1/queries/{name}/versions/{version}', {
            params: { path: { name, version }, header: ifMatch(etag) },
            body: { cache_ttl: ttl },
          }),
        ),
      onSettled: settle,
    }),
  };
}

export type SaveInput =
  | { kind: 'create'; body: QueryCreateInput }
  | { kind: 'version'; name: string; etag: string | null; body: QueryVersionInput };

export function useSaveQuery() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (input: SaveInput) => {
      if (input.kind === 'create') return unwrap(await api.POST('/api/v1/queries', { body: input.body }));
      return unwrap(
        await api.POST('/api/v1/queries/{name}/versions', {
          params: { path: { name: input.name }, header: ifMatch(input.etag) },
          body: input.body,
        }),
      );
    },
    onSettled: () => {
      client.invalidateQueries({ queryKey: keys.all });
      client.invalidateQueries({ queryKey: ['collections'] });
    },
  });
}

export function useValidate() {
  return useMutation({
    mutationFn: async (body: QueryVersionInput) =>
      unwrap(await api.POST('/api/v1/queries/validate', { body })),
  });
}

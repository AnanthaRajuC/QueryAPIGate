import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { api, unwrap, unwrapEmpty, unwrapWithEtag, type Schemas } from '@/api/client';

export type QuerySummary = Schemas['QuerySummary'];
export type Query = Schemas['Query'];
export type QueryVersion = Schemas['QueryVersion'];
export type ParameterRule = Schemas['ParameterRule'];
export type QueryVersionInput = Schemas['QueryVersionInput'];
export type QueryCreateInput = Schemas['QueryCreateInput'];
export type ValidationResult = Schemas['ValidationResult'];
export type HistoryEntry = Schemas['HistoryEntry'];
export type DbSchema = Schemas['Schema'];

export type StatusFilter = 'published' | 'unpublished' | 'draft';

export const queryKeys = {
  all: ['queries'] as const,
  list: (search: string, status: StatusFilter | '') => ['queries', 'list', search, status] as const,
  detail: (name: string) => ['queries', 'detail', name] as const,
  history: (name: string) => ['queries', 'history', name] as const,
};

export function useQueryList(search: string, status: StatusFilter | '') {
  return useQuery({
    queryKey: queryKeys.list(search, status),
    queryFn: async () =>
      unwrap(
        await api.GET('/api/v1/queries', {
          params: { query: { search: search || undefined, status: status || undefined } },
        }),
      ).items,
    placeholderData: (previous) => previous,
  });
}

/** A query with its ETag, so changes made from its screen can be conditional (If-Match). */
export function useQueryDetail(name: string) {
  return useQuery({
    queryKey: queryKeys.detail(name),
    queryFn: async () =>
      unwrapWithEtag(await api.GET('/api/v1/queries/{name}', { params: { path: { name } } })),
  });
}

export function useQueryHistory(name: string) {
  return useInfiniteQuery({
    queryKey: queryKeys.history(name),
    initialPageParam: undefined as string | undefined,
    queryFn: async ({ pageParam }) =>
      unwrap(
        await api.GET('/api/v1/queries/{name}/history', {
          params: { path: { name }, query: { limit: 25, cursor: pageParam } },
        }),
      ),
    getNextPageParam: (page) => page.next_cursor ?? undefined,
  });
}

function ifMatch(etag: string | null | undefined) {
  return etag ? { 'If-Match': etag } : undefined;
}

/** Publish (a draft, or an older version to roll back), unpublish, or delete a version - all conditional on the
 * ETag the screen loaded, so a change someone else made in the meantime is reported instead of overwritten. */
export function useQueryActions(name: string) {
  const client = useQueryClient();
  const settle = () => client.invalidateQueries({ queryKey: queryKeys.all });
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
  };
}

export type SaveInput =
  | { kind: 'create'; body: QueryCreateInput }
  | { kind: 'version'; name: string; etag: string | null; body: QueryVersionInput };

/** Save from the editor: create a new query, or add a version to an existing one (a draft unless publish). */
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
    onSettled: () => client.invalidateQueries({ queryKey: queryKeys.all }),
  });
}

export function useValidate() {
  return useMutation({
    mutationFn: async (body: QueryVersionInput) =>
      unwrap(await api.POST('/api/v1/queries/validate', { body })),
  });
}

export function useConnections() {
  return useQuery({
    queryKey: ['connections'],
    queryFn: async () => unwrap(await api.GET('/api/v1/connections')).items,
    staleTime: 60_000,
  });
}

/** A connection's tables and columns - feeds the SQL editor's completion. */
export function useConnectionSchema(name: string | undefined) {
  return useQuery({
    queryKey: ['connections', name, 'schema'],
    queryFn: async () =>
      unwrap(await api.GET('/api/v1/connections/{name}/schema', { params: { path: { name: name ?? '' } } })),
    enabled: Boolean(name),
    staleTime: 5 * 60_000,
    retry: false,
  });
}

/** Run SQL ad hoc (the editor's Test button) - the same /execute_sql the classic UI's Run SQL uses. */
export function useTestRun() {
  return useMutation({
    mutationFn: async (body: { sql: string; connection_name: string; params: Record<string, unknown> }) => {
      const result = await api.POST('/execute_sql', { params: { query: { page_size: 50 } }, body });
      const rows = unwrap(result) as Record<string, unknown>[];
      return { rows, hasMore: result.response.headers.get('X-Has-More') === 'true' };
    },
  });
}

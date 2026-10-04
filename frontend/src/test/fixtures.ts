import type { Schemas } from '@/api/client';

import { jsonResponse } from './fakeBackend';

type QueryVersion = Schemas['QueryVersion'];
type QuerySummary = Schemas['QuerySummary'];
type Query = Schemas['Query'];

export const ETAG = '"etag-1"';

export function version(
  n: number,
  status: QueryVersion['status'],
  extra: Partial<QueryVersion> = {},
): QueryVersion {
  return {
    version: n,
    status,
    uuid: `u${n}`,
    author: 'alice',
    description: `Films v${n}`,
    tags: ['films'],
    query_type: 'sql',
    sql: 'SELECT title FROM film WHERE film_id = :film_id',
    mongo: null,
    connection_name: 'lite',
    parameters: { film_id: { type: 'integer', required: true, min: 1 } },
    placeholders: ['film_id'],
    cache_ttl: null,
    created_at: '2026-10-03 10:00:00',
    last_modified_at: '2026-10-03 10:00:00',
    ...extra,
  };
}

export function summary(
  name: string,
  published: number | null,
  latest: number,
  extra: Partial<QuerySummary> = {},
): QuerySummary {
  return {
    name,
    description: `About ${name}`,
    query_type: 'sql',
    connection_name: 'lite',
    collection: 'catalog',
    tags: [],
    published_version: published,
    latest_version: latest,
    has_draft: latest > (published ?? 0),
    version_count: latest,
    example: false,
    endpoint: `/q/${name}`,
    updated_at: '2026-10-03 10:00:00',
    created_at: '2026-10-01 10:00:00',
    last_used_at: null,
    cache_ttl: null,
    ...extra,
  };
}

/** films: v1 published, v2 a draft. */
export const FILMS: Query = {
  ...summary('films', 1, 2),
  versions: [version(1, 'published'), version(2, 'draft', { description: 'Films v2' })],
};

/** Every route the shell and the API Repository read, with sensible data; override per test. */
export function baseRoutes(): Record<string, (call: { search: URLSearchParams }) => unknown> {
  return {
    'GET /health': () => ({ status: 'ok', version: '9.9.9', time_zone: 'UTC', utc_offset: '+00:00' }),
    'GET /api/v1/connections': () => ({
      items: [
        { name: 'lite', db: 'sqlite', active: true, host: null, port: null, database: '/data/films.db' },
      ],
    }),
    'GET /api/v1/queries': () => ({
      items: [summary('films', 1, 2), summary('rentals', 1, 1, { collection: null })],
    }),
    'GET /api/v1/queries/films': () => jsonResponse(FILMS, 200, { ETag: ETAG }),
    'GET /api/v1/queries/films/history': () => ({
      items: [
        {
          query: 'films',
          version: 2,
          executed_at: '2026-10-03 09:00:00',
          status: 'success',
          rows: 1,
          duration_ms: 4,
          key_name: 'partner',
        },
      ],
      next_cursor: null,
    }),
    'GET /api/v1/queries/*/versions/*/flow': () => ({
      tables: ['film'],
      joins: [],
      formatted: null,
      error: null,
    }),
    'GET /api/v1/api-keys': () => ({
      items: [apiKey('partner', { connections: [], collections: ['catalog'], rate_limit: '60/minute' })],
    }),
    'GET /api/v1/roles': () => ({ items: [role('analyst', { connections: ['lite'] })] }),
    'GET /api/v1/collections': () => ({
      items: [{ name: 'catalog', queries: ['films'], keys: ['partner'], roles: [] }],
      uncollected: ['rentals'],
    }),
    'GET /api/v1/examples': () => ({
      loaded: false,
      partial: false,
      connection: null,
      queries: [],
      roles: [],
      keys: [],
      collections: [],
    }),
    'GET /api/v1/connections/lite/schema': () => ({
      truncated: false,
      tables: [
        {
          name: 'film',
          type: 'table',
          columns: [
            { name: 'film_id', type: 'INTEGER' },
            { name: 'title', type: 'TEXT' },
          ],
        },
      ],
    }),
  };
}

const GRANTS = {
  connections: '*' as '*' | string[],
  queries: [] as ('*' | string | { name: string; allow_writes?: boolean })[] | '*',
  collections: [] as string[],
  allow_writes: false,
  allowed_write_ops: null as string[] | null,
  allowed_tables: null as string[] | null,
  rate_limit: null as string | null,
  allowed_ips: null as string[] | null,
};

/** An API key as /api/v1/api-keys returns it. */
export function apiKey(name: string, extra: Record<string, unknown> = {}) {
  return {
    name,
    ...GRANTS,
    active: true,
    expires_at: null,
    expired: false,
    created_at: '2026-10-01 10:00:00',
    created_from_role: null,
    last_used_at: null,
    example: false,
    usage: { queries: 0, errors: 0, rows: 0 },
    ...extra,
  };
}

/** A role as /api/v1/roles returns it. */
export function role(name: string, extra: Record<string, unknown> = {}) {
  return { name, ...GRANTS, created_at: '2026-10-01 10:00:00', example: false, keys_created: 0, ...extra };
}

import createClient, { type Middleware } from 'openapi-fetch';

import { getApiKey } from '@/auth/apiKey';

import type { components, paths } from './schema';

// Typed client generated from openapi.json (the backend's own API description - ADR 0001). Screens call the API
// only through this client, never with hand-written URLs or hand-copied response types.
const withApiKey: Middleware = {
  onRequest({ request }) {
    const key = getApiKey();
    if (key) request.headers.set('X-API-Key', key);
    return request;
  },
};

export const api = createClient<paths>({
  // Same origin as the page. Absolute so it also works where relative URLs aren't allowed (tests run in Node).
  baseUrl: globalThis.location?.origin ?? '',
  // Looked up per request rather than captured once, so tests can stub fetch.
  fetch: (request) => globalThis.fetch(request),
});
api.use(withApiKey);

export type Schemas = components['schemas'];

/** An API failure: the server's message, HTTP status, and - for /api/v1 - its stable `code` (e.g. query_exists). */
export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly code?: string,
    readonly details?: Record<string, string>,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

type Result<T> = { data?: T; error?: unknown; response: Response };

function toError(result: Result<unknown>): ApiError {
  const body = result.error as { error?: unknown; code?: unknown; errors?: unknown } | undefined;
  const message =
    typeof body?.error === 'string' ? body.error : `${result.response.status} ${result.response.statusText}`;
  return new ApiError(
    message,
    result.response.status,
    typeof body?.code === 'string' ? body.code : undefined,
    body?.errors && typeof body.errors === 'object' ? (body.errors as Record<string, string>) : undefined,
  );
}

/** Unwrap an openapi-fetch result: the data on success, an ApiError otherwise - the shape TanStack Query wants. */
export function unwrap<T>(result: Result<T>): T {
  if (result.response.ok && result.data !== undefined) return result.data;
  throw toError(result);
}

/** Like unwrap, for a response with no body (204). */
export function unwrapEmpty(result: Result<unknown>): void {
  if (!result.response.ok) throw toError(result);
}

/** Like unwrap, also returning the response's ETag - send it back as If-Match to make a change conditional. */
export function unwrapWithEtag<T>(result: Result<T>): { data: T; etag: string | null } {
  return { data: unwrap(result), etag: result.response.headers.get('ETag') };
}

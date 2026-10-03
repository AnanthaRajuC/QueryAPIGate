import createClient, { type Middleware } from 'openapi-fetch';

import { getApiKey } from '@/auth/apiKey';

import type { paths } from './schema';

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

/** An API failure, carrying the server's own message (`{"error": ...}`) and HTTP status. */
export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

/** Unwrap an openapi-fetch result: the data on success, an ApiError otherwise - the shape TanStack Query wants. */
export function unwrap<T>(result: { data?: T; error?: unknown; response: Response }): T {
  if (result.response.ok && result.data !== undefined) return result.data;
  const body = result.error as { error?: unknown } | undefined;
  const message =
    typeof body?.error === 'string' ? body.error : `${result.response.status} ${result.response.statusText}`;
  throw new ApiError(message, result.response.status);
}

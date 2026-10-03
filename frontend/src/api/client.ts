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
// The classic header's "rate N/M" chip follows the X-RateLimit headers of every response (ui.py noteRate).
export type RateLimit = { limit: number; remaining: number };
const rateListeners = new Set<(rate: RateLimit) => void>();
export function onRateLimit(listener: (rate: RateLimit) => void): () => void {
  rateListeners.add(listener);
  return () => rateListeners.delete(listener);
}
export function noteRate(response: Response) {
  const limit = response.headers.get('x-ratelimit-limit');
  const remaining = response.headers.get('x-ratelimit-remaining');
  if (limit === null || remaining === null) return;
  rateListeners.forEach((listener) => listener({ limit: Number(limit), remaining: Number(remaining) }));
}
const withRate: Middleware = {
  onResponse({ response }) {
    noteRate(response);
    return response;
  },
};

api.use(withApiKey);
api.use(withRate);

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

/** A raw request with the API key - for responses the typed client doesn't model: a saved query's own output
 * (any format, paged by headers) and the legacy management routes the Console still reads (/api_keys, /roles,
 * /collections) until their /api/v1 resources exist. */
export function apiFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const headers = new Headers(init.headers);
  const key = getApiKey();
  if (key) headers.set('X-API-Key', key);
  return globalThis
    .fetch(
      new Request(new URL(path, globalThis.location?.origin ?? 'http://localhost'), { ...init, headers }),
    )
    .then((response) => {
      noteRate(response);
      return response;
    });
}

/** apiFetch, timed - for the result bar's "N ms". */
export async function timedFetch(
  path: string,
  init?: RequestInit,
): Promise<{ response: Response; elapsed: number }> {
  const t0 = performance.now();
  const response = await apiFetch(path, init);
  return { response, elapsed: Math.round(performance.now() - t0) };
}

/** GET a JSON endpoint through apiFetch; an ApiError on failure. */
export async function apiJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await apiFetch(path, init);
  const text = await response.text();
  let body: unknown;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    body = null;
  }
  if (!response.ok) throw toError({ error: body ?? undefined, response });
  return body as T;
}

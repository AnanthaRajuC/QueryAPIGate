import { vi } from 'vitest';

export interface Call {
  method: string;
  path: string;
  search: URLSearchParams;
  headers: Headers;
  body: unknown;
}

type Handler = (call: Call) => unknown | Response;

/** Stub fetch with a route table: { 'GET /api/v1/queries': () => ({...}) }. A handler returns a JSON body (200) or
 * a full Response. An exact route wins; otherwise `*` matches any one path segment
 * (e.g. `GET /api/v1/queries/<star>/versions/<star>/flow`). Unknown routes answer 404 in the v1 error shape. Every request
 * is recorded in `calls`. */
export function fakeBackend(routes: Record<string, Handler>) {
  const calls: Call[] = [];
  const fetchMock = vi.fn(async (input: Request | string, init?: RequestInit) => {
    const request = typeof input === 'string' ? new Request(input, init) : input;
    const url = new URL(request.url);
    const text = await request.text();
    const call: Call = {
      method: request.method,
      path: url.pathname,
      search: url.searchParams,
      headers: request.headers,
      body: text ? JSON.parse(text) : undefined,
    };
    calls.push(call);
    const handler = routes[`${call.method} ${call.path}`] ?? wildcard(routes, `${call.method} ${call.path}`);
    const result = handler ? handler(call) : jsonResponse({ error: 'Not found', code: 'not_found' }, 404);
    return result instanceof Response ? result : jsonResponse(result);
  });
  vi.stubGlobal('fetch', fetchMock);
  return { calls, fetchMock };
}

export function jsonResponse(body: unknown, status = 200, headers: Record<string, string> = {}) {
  return new Response(status === 204 ? null : JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json', ...headers },
  });
}

function wildcard(routes: Record<string, Handler>, route: string): Handler | undefined {
  const parts = route.split('/');
  const key = Object.keys(routes).find((pattern) => {
    const want = pattern.split('/');
    return (
      pattern.includes('*') &&
      want.length === parts.length &&
      want.every((w, i) => w === '*' || w === parts[i])
    );
  });
  return key ? routes[key] : undefined;
}

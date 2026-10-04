import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { fakeBackend, jsonResponse } from '@/test/fakeBackend';
import { baseRoutes, ETAG, FILMS, version } from '@/test/fixtures';
import { renderAt } from '@/test/renderApp';

// CodeMirror needs layout APIs jsdom lacks; a textarea with the same contract stands in for it.
vi.mock('@/components/SqlEditor', () => ({
  SqlEditor: ({ value, onChange, id }: { value: string; onChange?: (v: string) => void; id?: string }) => (
    <textarea id={id} aria-label="SQL" value={value} onChange={(e) => onChange?.(e.target.value)} />
  ),
}));

afterEach(() => {
  vi.unstubAllGlobals();
});

const detail = () => document.getElementById('query-detail')!;

describe('API Repository - list', () => {
  it('groups queries into collections, as the classic list does', async () => {
    fakeBackend(baseRoutes());
    renderAt('/queries/films');
    const item = await waitFor(() => {
      const el = document.querySelector('.qitem[data-name="films"]');
      expect(el).not.toBeNull();
      return el!;
    });
    expect(item).toHaveClass('sel');
    expect(screen.getByRole('button', { name: 'Queries · catalog' })).toHaveClass('active');
    await userEvent.click(screen.getByRole('button', { name: 'Collections' }));
    expect(screen.getByRole('button', { name: /catalog1/ })).toHaveClass('qcoll-row');
    expect(screen.getByRole('button', { name: /No collection1/ })).toBeInTheDocument();
  });

  it('a text filter searches every collection at once', async () => {
    fakeBackend(baseRoutes());
    renderAt('/queries/films');
    await waitFor(() => expect(document.querySelector('.qitem[data-name="films"]')).not.toBeNull());
    await userEvent.type(screen.getByPlaceholderText('Filter by name, description, tag…'), 'rent');
    expect(document.querySelector('.qitem[data-name="rentals"]')).not.toBeNull();
    expect(document.querySelector('.qitem[data-name="films"]')).toBeNull();
  });

  it('heads the page like the classic one', async () => {
    fakeBackend(baseRoutes());
    renderAt('/queries/films');
    expect(await screen.findByText(/3 versions across 1 collection\./)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'New API' })).toHaveClass('btn', 'primary');
  });
});

describe('API Repository - detail and publishing', () => {
  it('shows the newest version, marked as a draft, with Publish', async () => {
    fakeBackend(baseRoutes());
    renderAt('/queries/films');
    await waitFor(() => expect(detail()).toHaveTextContent('Draft'));
    expect(within(detail()).getByRole('group', { name: 'Version' })).toHaveTextContent('v1v2');
    expect(within(detail()).getByRole('button', { name: 'Publish v2' })).toBeInTheDocument();
    await userEvent.click(within(detail()).getByRole('button', { name: 'v1' }));
    expect(detail()).toHaveTextContent('Published');
    expect(within(detail()).getByRole('button', { name: 'Unpublish' })).toBeInTheDocument();
  });

  it('publishes conditionally on the version it loaded', async () => {
    const { calls } = fakeBackend({
      ...baseRoutes(),
      'POST /api/v1/queries/films/publish': () => ({ ...FILMS, published_version: 2 }),
    });
    renderAt('/queries/films');
    await userEvent.click(await screen.findByRole('button', { name: 'Publish v2' }));
    await waitFor(() => expect(calls.some((c) => c.path === '/api/v1/queries/films/publish')).toBe(true));
    const publish = calls.find((c) => c.path === '/api/v1/queries/films/publish')!;
    expect(publish.body).toEqual({ version: 2 });
    expect(publish.headers.get('If-Match')).toBe(ETAG);
    expect(await screen.findByText('Published v2 at /q/films')).toBeInTheDocument(); // the classic toast
  });

  it('reports a change refused because someone changed the query first, in the error banner', async () => {
    fakeBackend({
      ...baseRoutes(),
      'POST /api/v1/queries/films/publish': () =>
        jsonResponse({ error: 'changed', code: 'precondition_failed', request_id: 'r' }, 412),
    });
    renderAt('/queries/films');
    await userEvent.click(await screen.findByRole('button', { name: 'Publish v2' }));
    const banner = await waitFor(() => {
      const el = document.getElementById('error-banner')!;
      expect(el).not.toHaveAttribute('hidden');
      return el;
    });
    expect(banner).toHaveTextContent('This query changed while you were looking at it');
  });

  it('runs a draft with ?version= and shows the rows in the classic results table', async () => {
    const { calls } = fakeBackend({
      ...baseRoutes(),
      'GET /q/films': () =>
        jsonResponse([{ title: 'Alpha' }], 200, {
          'X-Page': '1',
          'X-Page-Size': '10',
          'X-Has-More': 'false',
        }),
    });
    renderAt('/queries/films');
    await userEvent.type(await screen.findByLabelText('film_id'), '7');
    await userEvent.click(within(detail()).getByRole('button', { name: 'Run' }));
    expect(await screen.findByText('Alpha')).toBeInTheDocument();
    const run = calls.find((c) => c.path === '/q/films')!;
    expect(run.search.get('film_id')).toBe('7');
    expect(run.search.get('version')).toBe('2'); // the draft, not the published version
    expect(document.querySelector('table.rs')).not.toBeNull();
  });

  it('builds its stat tiles and history from the version runs', async () => {
    fakeBackend(baseRoutes());
    renderAt('/queries/films');
    await waitFor(() => expect(detail().querySelector('.stat-tiles')).not.toBeNull());
    expect(detail().querySelector('.stat-tiles')).toHaveTextContent('Total runs1');
    await userEvent.click(within(detail()).getByRole('tab', { name: /History/ }));
    expect(within(detail()).getByText('partner')).toBeInTheDocument();
  });

  it('lists the keys that reach the query, from their grants', async () => {
    fakeBackend(baseRoutes());
    renderAt('/queries/films');
    await waitFor(() =>
      expect(within(detail()).getByRole('tab', { name: /API Keys/ })).toHaveTextContent('API Keys1'),
    );
    await userEvent.click(within(detail()).getByRole('tab', { name: /API Keys/ }));
    expect(within(detail()).getByText('partner')).toBeInTheDocument();
    expect(within(detail()).getByText('60/minute')).toBeInTheDocument();
  });
});

describe('API Repository - the New version drawer', () => {
  it('saves a new version as a draft, keeping the published one serving', async () => {
    const { calls } = fakeBackend({
      ...baseRoutes(),
      'POST /api/v1/queries/films/versions': () =>
        jsonResponse(
          { ...FILMS, latest_version: 3, versions: [...FILMS.versions, version(3, 'draft')] },
          201,
        ),
    });
    renderAt('/queries/films');
    await userEvent.click(await screen.findByRole('button', { name: 'New version' }));
    const drawer = document.getElementById('drawer')!;
    expect(drawer).toHaveClass('open');
    expect(within(drawer).getByLabelText('Filename')).toHaveValue('films');
    expect(within(drawer).getByLabelText('Filename')).toHaveAttribute('readonly');
    expect(within(drawer).getByText(':film_id')).toBeInTheDocument(); // "Bound in the query"
    await userEvent.click(within(drawer).getByRole('button', { name: 'Save as draft' }));
    await waitFor(() => expect(calls.some((c) => c.path === '/api/v1/queries/films/versions')).toBe(true));
    const saved = calls.find((c) => c.path === '/api/v1/queries/films/versions')!;
    expect(saved.headers.get('If-Match')).toBe(ETAG);
    expect(saved.body).toMatchObject({
      publish: false,
      sql: 'SELECT title FROM film WHERE film_id = :film_id',
      parameters: { film_id: { type: 'integer', required: true, min: 1 } },
    });
    expect(await screen.findByText('Saved films v3 as a draft')).toBeInTheDocument();
  });

  it('refuses query_parameters that are not JSON, before sending anything', async () => {
    const { calls } = fakeBackend(baseRoutes());
    renderAt('/queries/films');
    await userEvent.click(await screen.findByRole('button', { name: 'New version' }));
    const drawer = document.getElementById('drawer')!;
    const params = within(drawer).getByLabelText('query_parameters');
    await userEvent.clear(params);
    await userEvent.type(params, '{{not json');
    await userEvent.click(within(drawer).getByRole('button', { name: 'Save as v3' }));
    expect(document.getElementById('error-banner')).toHaveTextContent('query_parameters is not valid JSON');
    expect(calls.some((c) => c.method === 'POST')).toBe(false);
  });

  it('New API creates a query, published at once like Save always did', async () => {
    const { calls } = fakeBackend({
      ...baseRoutes(),
      'POST /api/v1/queries': () =>
        jsonResponse(
          { ...FILMS, name: 'by_id', latest_version: 1, versions: [version(1, 'published')] },
          201,
        ),
      'GET /api/v1/queries/by_id': () => jsonResponse({ ...FILMS, name: 'by_id' }, 200, { ETag: ETAG }),
    });
    renderAt('/queries/films');
    await userEvent.click(await screen.findByRole('button', { name: 'New API' }));
    const drawer = document.getElementById('drawer')!;
    await userEvent.type(within(drawer).getByLabelText('Filename'), 'by_id');
    await userEvent.type(within(drawer).getByLabelText('Author'), 'bob');
    await userEvent.type(within(drawer).getByLabelText('SQL'), 'SELECT 1');
    await userEvent.type(within(drawer).getByLabelText('Description'), 'One');
    await userEvent.click(within(drawer).getByRole('button', { name: 'Save' }));
    await waitFor(() =>
      expect(calls.some((c) => c.method === 'POST' && c.path === '/api/v1/queries')).toBe(true),
    );
    expect(calls.find((c) => c.method === 'POST' && c.path === '/api/v1/queries')!.body).toMatchObject({
      name: 'by_id',
      author: 'bob',
      sql: 'SELECT 1',
      description: 'One',
      publish: true,
    });
  });
});

describe('API Repository - collections and the example APIs', () => {
  it('renames a collection through /api/v1/collections', async () => {
    const { calls } = fakeBackend({
      ...baseRoutes(),
      'PATCH /api/v1/collections/catalog': () => ({
        name: 'films-v2',
        moved: { queries: ['films'], keys: ['partner'], roles: [] },
      }),
    });
    renderAt('/queries/films');
    await userEvent.click(await screen.findByRole('link', { name: 'Rename' }));
    const drawer = document.getElementById('drawer')!;
    await userEvent.clear(within(drawer).getByLabelText('New name')); // it starts as the current name
    await userEvent.type(within(drawer).getByLabelText('New name'), 'films-v2');
    await userEvent.click(within(drawer).getByRole('button', { name: 'Rename' }));
    await waitFor(() => expect(calls.some((c) => c.method === 'PATCH')).toBe(true));
    expect(calls.find((c) => c.method === 'PATCH')!.body).toEqual({ name: 'films-v2', merge: false });
    expect(await screen.findByText('Renamed catalog → films-v2')).toBeInTheDocument();
  });

  it('exports a collection for Postman', async () => {
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
    const { calls } = fakeBackend({
      ...baseRoutes(),
      'GET /api/v1/collections/catalog/postman': () => ({ info: { name: 'catalog' }, item: [] }),
    });
    renderAt('/queries/films');
    await userEvent.click(await screen.findByRole('link', { name: 'Postman' }));
    await waitFor(() => expect(click).toHaveBeenCalled());
    expect(calls.some((c) => c.path === '/api/v1/collections/catalog/postman')).toBe(true);
    click.mockRestore();
  });

  it('removes the example APIs, warning about grants that now reach nothing', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    const status = {
      loaded: true,
      partial: false,
      connection: 'examples',
      queries: ['films'],
      roles: ['example-partner'],
      keys: [],
      collections: ['catalog'],
    };
    const { calls } = fakeBackend({
      ...baseRoutes(),
      'GET /api/v1/examples': () => status,
      'DELETE /api/v1/examples': () => ({
        removed: { connection: true, queries: ['films'], roles: ['example-partner'], keys: [] },
        keys_still_granted: ['partner'],
        status: { ...status, loaded: false, connection: null, queries: [], roles: [], collections: [] },
      }),
    });
    renderAt('/queries/films');
    const strip = await waitFor(() => {
      const el = document.getElementById('examples-strip');
      if (!el || el.hidden) throw new Error('not shown yet');
      return el;
    });
    expect(strip).toHaveTextContent('Example APIs are loaded: 1 queries in 1 collections, 1 role');
    await userEvent.click(within(strip).getByRole('button', { name: 'Remove examples' }));
    await waitFor(() => expect(calls.some((c) => c.method === 'DELETE')).toBe(true));
    expect(await screen.findByText('Example APIs removed')).toBeInTheDocument();
    expect(document.getElementById('error-banner')).toHaveTextContent('partner');
    vi.restoreAllMocks();
  });
});

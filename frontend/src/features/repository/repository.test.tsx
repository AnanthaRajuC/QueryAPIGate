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
    expect(item).toHaveAttribute('aria-current', 'true');
    // the selected query's collection, headed by its name and count, with the way back
    expect(document.querySelector('.qg-head')).toHaveTextContent('catalog1 query');
    await userEvent.click(screen.getByRole('button', { name: 'Back to collections' }));
    expect(screen.getByRole('button', { name: /^catalog/ })).toHaveClass('qcoll-row');
    expect(screen.getByRole('button', { name: /^No collection/ })).toBeInTheDocument();
    expect(document.querySelector('.qg-head')).toBeNull();
    await userEvent.click(screen.getByRole('button', { name: /^catalog/ }));
    expect(document.querySelector('.qitem[data-name="films"]')).not.toBeNull();
  });

  it('shows a version chip only on a query with more than one version', async () => {
    fakeBackend(baseRoutes());
    renderAt('/queries/films');
    const films = await waitFor(() => {
      const el = document.querySelector('.qitem[data-name="films"]');
      expect(el).not.toBeNull();
      return el!;
    });
    expect(films.querySelector('.tag.v')).toHaveTextContent('v2');
    await userEvent.click(screen.getByRole('button', { name: 'Back to collections' }));
    await userEvent.click(screen.getByRole('button', { name: /^No collection/ }));
    expect(document.querySelector('.qitem[data-name="rentals"] .tag.v')).toBeNull();
  });

  it('keeps the connection filters in a popover, counting the ones set', async () => {
    fakeBackend(baseRoutes());
    renderAt('/queries/films');
    await waitFor(() => expect(document.querySelector('.qitem[data-name="films"]')).not.toBeNull());
    const filters = screen.getByRole('button', { name: 'Filters: type, host, database' });
    expect(filters).toHaveAttribute('aria-expanded', 'false');
    expect(document.getElementById('qf-type')).toBeNull();
    await userEvent.click(filters);
    const panel = screen.getByRole('dialog', { name: 'Filters: type, host, database' });
    const type = within(panel).getByLabelText('Type');
    expect(type).toHaveFocus();
    await userEvent.selectOptions(type, within(type).getAllByRole('option')[1]!);
    expect(filters).toHaveTextContent('1');
    await userEvent.keyboard('{Escape}');
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(filters).toHaveFocus();
    expect(filters).toHaveClass('on');
  });

  it('a text filter searches every collection at once', async () => {
    fakeBackend(baseRoutes());
    renderAt('/queries/films');
    await waitFor(() => expect(document.querySelector('.qitem[data-name="films"]')).not.toBeNull());
    await userEvent.type(screen.getByPlaceholderText('Filter by name, description, tag…'), 'rent');
    expect(document.querySelector('.qitem[data-name="rentals"]')).not.toBeNull();
    expect(document.querySelector('.qitem[data-name="films"]')).toBeNull();
  });

  it('puts its actions on the section’s tab row, with no header band of its own', async () => {
    fakeBackend(baseRoutes());
    renderAt('/queries/films');
    const newApi = await screen.findByRole('button', { name: 'New API' });
    expect(newApi).toHaveClass('btn', 'primary');
    const row = document.querySelector('.section-tabbar .section-actions')!;
    expect(row).toContainElement(newApi);
    expect(row).toContainElement(screen.getByRole('button', { name: 'New collection' }));
    expect(screen.queryByText(/versions? across/)).not.toBeInTheDocument();
  });
});

describe('API Repository - detail and publishing', () => {
  it('shows the newest version, marked as a draft, with Publish', async () => {
    fakeBackend(baseRoutes());
    renderAt('/queries/films');
    await waitFor(() => expect(detail()).toHaveTextContent('Draft'));
    expect(within(detail()).getByRole('group', { name: 'Version' })).toHaveTextContent('v1v2');
    expect(within(detail()).getByRole('button', { name: 'Publish v2' })).toBeInTheDocument();
    expect(detail()).not.toHaveTextContent('older version'); // the latest is the usual case: no label
    await userEvent.click(within(detail()).getByRole('button', { name: 'v1' }));
    expect(detail()).toHaveTextContent('Published');
    expect(detail()).toHaveTextContent('older version');
    // a published version has no Publish button; Unpublish is in the ⋯ menu
    expect(within(detail()).queryByRole('button', { name: /Publish/ })).toBeNull();
    await userEvent.click(within(detail()).getByRole('button', { name: /^More actions/ }));
    expect(screen.getByRole('menuitem', { name: 'Unpublish' })).toBeInTheDocument();
  });

  it('keeps New version in view and the rest in the ⋯ menu, Delete red and confirmed', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
    const { calls } = fakeBackend(baseRoutes());
    renderAt('/queries/films');
    await waitFor(() => expect(detail()).toHaveTextContent('Draft'));
    expect(within(detail()).getByRole('button', { name: 'New version' })).toBeInTheDocument();
    const more = within(detail()).getByRole('button', { name: /^More actions/ });
    expect(more).toHaveAttribute('aria-haspopup', 'menu');
    await userEvent.click(more);
    const items = screen.getAllByRole('menuitem');
    // a draft isn't served, so there is nothing to unpublish
    expect(items.map((i) => i.textContent)).toEqual([
      'Move…',
      'Delete v2…',
      'Delete query (all 2 versions)…',
    ]);
    expect(items[1]).toHaveClass('danger');
    expect(items[2]).toHaveClass('danger');
    await userEvent.click(items[2]!);
    expect(confirm).toHaveBeenCalledWith('Delete films and all 2 versions?');
    expect(calls.some((c) => c.method === 'DELETE')).toBe(false); // declined: nothing sent
    expect(more).toHaveFocus();
    confirm.mockRestore();
  });

  it('says what the query runs on as one line: connection, author, modified to the minute, tags as chips', async () => {
    fakeBackend(baseRoutes());
    renderAt('/queries/films');
    const meta = await waitFor(() => {
      const el = detail().querySelector('.d-meta');
      expect(el).not.toBeNull();
      return el!;
    });
    expect(meta).toHaveTextContent('Connectionlite');
    expect(meta).toHaveTextContent('Authoralice');
    expect(meta).not.toHaveTextContent(/collection/i); // the list's head already names it
    await waitFor(() => expect(meta.querySelector('time')).not.toBeNull());
    const modified = meta.querySelector('time')!;
    expect(modified.textContent).toMatch(/^\d{4}-\d\d-\d\d \d\d:\d\d$/);
    expect(modified.getAttribute('title')).toMatch(/\d\d:\d\d:\d\d/); // the tooltip keeps the seconds
    expect([...meta.querySelectorAll('.tag-chip')].map((t) => t.textContent)).toEqual(['films']);
  });

  it('shows status and cache as quiet text, not pills', async () => {
    fakeBackend({
      ...baseRoutes(),
      'GET /api/v1/queries/films': () =>
        jsonResponse({ ...FILMS, versions: [version(1, 'published', { cache_ttl: 30 })] }, 200, {
          ETag: ETAG,
        }),
    });
    renderAt('/queries/films');
    const status = await waitFor(() => {
      const el = detail().querySelector('.d-status');
      expect(el).not.toBeNull();
      return el!;
    });
    expect(status).toHaveTextContent('Published');
    expect(status).toHaveClass('ok');
    expect(detail().querySelector('.d-cache')).toHaveTextContent('Cached 30s');
    expect(detail().querySelector('.pill')).toBeNull();
    expect(detail().querySelector('.vbadge')).toHaveTextContent('v1');
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

  it('builds its stat strip and history from the version runs, and Metrics has the rest', async () => {
    fakeBackend(baseRoutes());
    renderAt('/queries/films');
    const strip = await waitFor(() => {
      const el = detail().querySelector('.stat-strip');
      expect(el).not.toBeNull();
      return el!;
    });
    expect(strip).toHaveTextContent('Total runs1');
    expect(strip).toHaveTextContent('Success100%');
    expect(strip).toHaveTextContent('Avg duration4 ms');
    expect(strip).not.toHaveTextContent('Slowest run');
    expect(
      within(detail()).getByRole('img', { name: /^Requests per day over the last 7 days: / }),
    ).toBeInTheDocument();
    expect(detail().querySelector('.stat-tiles')).toBeNull();
    expect(detail().querySelector('.req-day-chart')).toBeNull();
    await userEvent.click(within(detail()).getByRole('tab', { name: 'Metrics' }));
    expect(detail().querySelector('.stat-tiles')).toHaveTextContent('Slowest run4 ms');
    expect(detail().querySelector('.stat-tiles')).toHaveTextContent('Avg rows1');
    expect(detail().querySelector('.req-day-chart')).toHaveTextContent('Requests per day');
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

  it('toggles MCP exposure in place from the MCP tab, conditionally on the loaded version', async () => {
    const { calls } = fakeBackend({
      ...baseRoutes(),
      'PATCH /api/v1/queries/films/versions/2': () => ({ ...FILMS }),
    });
    renderAt('/queries/films');
    await userEvent.click(await screen.findByRole('tab', { name: 'MCP' }));
    const toggle = within(detail()).getByRole('checkbox', { name: /Expose this query as an MCP tool/ });
    expect(toggle).toBeChecked();
    await userEvent.click(toggle);
    await waitFor(() => expect(calls.some((c) => c.path === '/api/v1/queries/films/versions/2')).toBe(true));
    const patch = calls.find((c) => c.path === '/api/v1/queries/films/versions/2')!;
    expect(patch.method).toBe('PATCH');
    expect(patch.body).toEqual({ mcp: false });
    expect(patch.headers.get('If-Match')).toBe(ETAG);
    expect(await screen.findByText("'films' is no longer an MCP tool")).toBeInTheDocument();
  });

  it('filters History to MCP-only runs', async () => {
    fakeBackend(baseRoutes());
    renderAt('/queries/films');
    await userEvent.click(await screen.findByRole('tab', { name: /History/ }));
    expect(within(detail()).getByText('partner')).toBeInTheDocument();
    await userEvent.click(within(detail()).getByRole('checkbox', { name: /MCP only/ }));
    expect(within(detail()).queryByText('partner')).not.toBeInTheDocument();
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

  it('carries a query-level column mask into the saved version', async () => {
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
    await userEvent.click(within(drawer).getByRole('button', { name: 'Add a masking rule' }));
    await userEvent.type(within(drawer).getByLabelText('Masked column 1'), 'film.title');
    await userEvent.selectOptions(within(drawer).getByLabelText('Masking action 1'), 'hash');
    await userEvent.click(within(drawer).getByRole('button', { name: 'Save as draft' }));
    await waitFor(() => expect(calls.some((c) => c.path === '/api/v1/queries/films/versions')).toBe(true));
    const saved = calls.find((c) => c.path === '/api/v1/queries/films/versions')!;
    expect(saved.body).toMatchObject({ mask: [{ column: 'film.title', action: 'hash' }] });
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
    await userEvent.click(await screen.findByRole('button', { name: 'Collection actions' }));
    await userEvent.click(screen.getByRole('menuitem', { name: 'Rename…' }));
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
    await userEvent.click(await screen.findByRole('button', { name: 'Collection actions' }));
    await userEvent.click(screen.getByRole('menuitem', { name: 'Download Postman collection' }));
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
      workspace: {
        name: 'example-stores',
        workspace: true,
        connection: true,
        queries: ['example_store_revenue'],
        roles: ['example-stores'],
        keys: ['example-stores'],
        members: ['example-auditor', 'example-developer', 'example-owner'],
      },
    };
    const { calls } = fakeBackend({
      ...baseRoutes(),
      'GET /api/v1/examples': () => status,
      'DELETE /api/v1/examples': () => ({
        removed: { connection: true, queries: ['films'], roles: ['example-partner'], keys: [] },
        keys_still_granted: ['partner'],
        workspace_kept: null,
        status: {
          ...status,
          loaded: false,
          connection: null,
          queries: [],
          roles: [],
          collections: [],
          workspace: null,
        },
      }),
    });
    renderAt('/queries/films');
    const strip = await waitFor(() => {
      const el = document.getElementById('examples-strip');
      if (!el || el.hidden) throw new Error('not shown yet');
      return el;
    });
    expect(strip).toHaveTextContent('Example data is loaded: 1 query in 1 collection.');
    // the full account is one click away, behind a disclosure
    const included = within(strip).getByRole('button', { name: 'What’s included' });
    expect(included).toHaveAttribute('aria-expanded', 'false');
    const account = strip.querySelector('.examples-included')!;
    expect(account).not.toBeVisible();
    await userEvent.click(included);
    expect(included).toHaveAttribute('aria-expanded', 'true');
    expect(account).toBeVisible();
    expect(strip).toHaveTextContent('Example APIs are loaded: 1 query in 1 collection, 1 role');
    expect(strip).toHaveTextContent('and the example-stores workspace with 3 members');
    await userEvent.click(within(strip).getByRole('button', { name: 'Remove examples' }));
    await waitFor(() => expect(calls.some((c) => c.method === 'DELETE')).toBe(true));
    expect(await screen.findByText('Example APIs removed')).toBeInTheDocument();
    expect(document.getElementById('error-banner')).toHaveTextContent('partner');
    vi.restoreAllMocks();
  });
});

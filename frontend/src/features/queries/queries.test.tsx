import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { fakeBackend, jsonResponse } from '@/test/fakeBackend';
import { renderAt } from '@/test/renderApp';

import type { Query, QuerySummary, QueryVersion } from './api';

// CodeMirror needs layout APIs jsdom doesn't have; a textarea with the same contract stands in for it here.
vi.mock('@/components/sql/SqlEditor', () => ({
  SqlEditor: ({
    value,
    onChange,
    label,
    readOnly,
  }: {
    value: string;
    onChange?: (v: string) => void;
    label: string;
    readOnly?: boolean;
  }) => (
    <textarea
      aria-label={label}
      value={value}
      readOnly={readOnly}
      onChange={(e) => onChange?.(e.target.value)}
    />
  ),
}));

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

const ETAG = '"etag-1"';

function version(n: number, status: QueryVersion['status'], sql = `SELECT ${n}`): QueryVersion {
  return {
    version: n,
    status,
    uuid: `u${n}`,
    author: 'admin',
    description: `version ${n}`,
    tags: [],
    query_type: 'sql',
    sql,
    mongo: null,
    connection_name: 'lite',
    parameters: {},
    placeholders: [],
    cache_ttl: null,
    created_at: '2026-10-03 10:00:00',
    last_modified_at: '2026-10-03 10:00:00',
  };
}

function summary(name: string, published: number | null, latest: number): QuerySummary {
  return {
    name,
    description: `About ${name}`,
    query_type: 'sql',
    connection_name: 'lite',
    collection: null,
    tags: [],
    published_version: published,
    latest_version: latest,
    has_draft: latest > (published ?? 0),
    version_count: latest,
    example: false,
    endpoint: `/q/${name}`,
    updated_at: '2026-10-03 10:00:00',
  };
}

const FILM: Query = {
  ...summary('films', 1, 2),
  versions: [
    version(1, 'published'),
    {
      ...version(2, 'draft', 'SELECT title FROM film WHERE film_id = :film_id'),
      placeholders: ['film_id'],
      parameters: { film_id: { type: 'integer', required: true, min: 1 } },
    },
  ],
};

const common = {
  'GET /api/v1/connections': () => ({ items: [{ name: 'lite', db: 'sqlite', active: true }] }),
  'GET /api/v1/connections/lite/schema': () => ({
    truncated: false,
    tables: [{ name: 'film', type: 'table', columns: [{ name: 'film_id', type: 'INTEGER' }] }],
  }),
  'POST /api/v1/queries/validate': () => ({ valid: true, errors: [], placeholders: [], tables: ['film'] }),
  'GET /catalog': () => jsonResponse({ error: 'Unauthorized' }, 401),
};

describe('Queries list', () => {
  it('lists queries with their publish state and filters by status', async () => {
    const { calls } = fakeBackend({
      ...common,
      'GET /api/v1/queries': ({ search }) =>
        search.get('status') === 'unpublished'
          ? { items: [summary('drafty', null, 1)] }
          : { items: [summary('films', 1, 2), summary('drafty', null, 1)] },
    });
    renderAt('/queries');
    const table = await screen.findByRole('table', { name: 'Saved queries' });
    expect(within(table).getByRole('link', { name: 'films' })).toHaveAttribute('href', '/queries/films');
    expect(within(table).getByText('Published v1')).toBeInTheDocument();
    expect(within(table).getByText('Draft pending')).toBeInTheDocument();
    expect(within(table).getByText('Not published')).toBeInTheDocument();

    await userEvent.selectOptions(screen.getByLabelText('Filter by status'), 'unpublished');
    await waitFor(() => expect(calls.at(-1)?.search.get('status')).toBe('unpublished'));
    await waitFor(() => expect(within(table).queryByText('films')).not.toBeInTheDocument());
  });
});

describe('Query detail', () => {
  it('publishes the pending draft, conditional on the version it loaded', async () => {
    const { calls } = fakeBackend({
      ...common,
      'GET /api/v1/queries/films': () => jsonResponse(FILM, 200, { ETag: ETAG }),
      'POST /api/v1/queries/films/publish': () => ({ ...FILM, published_version: 2, has_draft: false }),
    });
    renderAt('/queries/films');
    await userEvent.click(await screen.findByRole('button', { name: 'Publish v2' }));
    await waitFor(() => expect(calls.some((c) => c.path === '/api/v1/queries/films/publish')).toBe(true));
    const publish = calls.find((c) => c.path === '/api/v1/queries/films/publish')!;
    expect(publish.body).toEqual({ version: 2 });
    expect(publish.headers.get('If-Match')).toBe(ETAG);
  });

  it('explains a change refused because someone else changed the query first', async () => {
    fakeBackend({
      ...common,
      'GET /api/v1/queries/films': () => jsonResponse(FILM, 200, { ETag: ETAG }),
      'POST /api/v1/queries/films/publish': () =>
        jsonResponse({ error: 'changed', code: 'precondition_failed', request_id: 'r' }, 412),
    });
    renderAt('/queries/films');
    await userEvent.click(await screen.findByRole('button', { name: 'Publish v2' }));
    expect(await screen.findByText(/Someone changed this query/)).toBeInTheDocument();
  });

  it('lists versions with roll-back for older ones and shows the parameter rules', async () => {
    fakeBackend({
      ...common,
      'GET /api/v1/queries/films': () =>
        jsonResponse(
          {
            ...FILM,
            published_version: 2,
            has_draft: false,
            versions: [version(1, 'previous'), { ...FILM.versions[1]!, status: 'published' }],
          },
          200,
          { ETag: ETAG },
        ),
    });
    renderAt('/queries/films');
    const params = await screen.findByRole('table', { name: 'Parameters' });
    expect(within(params).getByText('film_id')).toBeInTheDocument();
    expect(within(params).getByText('required · ≥ 1')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('tab', { name: 'Versions' }));
    const versions = screen.getByRole('table', { name: 'Versions' });
    expect(within(versions).getByRole('button', { name: 'Roll back to this' })).toBeInTheDocument();
  });

  it('says so when the query does not exist', async () => {
    fakeBackend({
      ...common,
      'GET /api/v1/queries/nope': () =>
        jsonResponse({ error: 'not found', code: 'query_not_found', request_id: 'r' }, 404),
    });
    renderAt('/queries/nope');
    expect(await screen.findByText('There is no saved query named "nope".')).toBeInTheDocument();
  });
});

describe('Query editor', () => {
  it('refuses to save an incomplete new query and says what is missing', async () => {
    const { calls } = fakeBackend(common);
    renderAt('/queries/new');
    await userEvent.click(await screen.findByRole('button', { name: 'Save as draft' }));
    expect(await screen.findByText('Give the query a name')).toBeInTheDocument();
    expect(screen.getByText('Describe what the query returns')).toBeInTheDocument();
    expect(screen.getByText('Write the SQL')).toBeInTheDocument();
    expect(calls.some((c) => c.method === 'POST' && c.path === '/api/v1/queries')).toBe(false);
  });

  it('creates a query as a draft, with parameter rules for the placeholders it uses', async () => {
    const { calls } = fakeBackend({
      ...common,
      'POST /api/v1/queries': () => jsonResponse({ ...FILM, name: 'by_id' }, 201),
      'GET /api/v1/queries/by_id': () => jsonResponse({ ...FILM, name: 'by_id' }, 200, { ETag: ETAG }),
    });
    renderAt('/queries/new');
    await userEvent.type(await screen.findByLabelText('Name'), 'by_id');
    await userEvent.type(screen.getByLabelText('Description'), 'One film');
    await waitFor(() => expect(screen.getByRole('option', { name: 'lite (sqlite)' })).toBeInTheDocument());
    await userEvent.selectOptions(screen.getByLabelText('Connection'), 'lite');
    await userEvent.type(screen.getByLabelText('SQL'), 'SELECT * FROM film WHERE film_id = :id');
    await userEvent.selectOptions(await screen.findByLabelText('Type of id'), 'integer');
    await userEvent.type(screen.getByLabelText('Description of id'), 'Film id');
    await userEvent.click(screen.getByRole('button', { name: 'Save as draft' }));

    await waitFor(() =>
      expect(calls.some((c) => c.method === 'POST' && c.path === '/api/v1/queries')).toBe(true),
    );
    const created = calls.find((c) => c.method === 'POST' && c.path === '/api/v1/queries')!;
    expect(created.body).toEqual({
      name: 'by_id',
      description: 'One film',
      sql: 'SELECT * FROM film WHERE film_id = :id',
      connection_name: 'lite',
      tags: [],
      parameters: { id: { type: 'integer', required: true, description: 'Film id' } },
      publish: false,
    });
    expect(await screen.findByRole('heading', { name: 'by_id' })).toBeInTheDocument(); // on the new query's page
  });

  it('adds a version from an existing one, keeping rules the form does not edit, and can publish at once', async () => {
    const { calls } = fakeBackend({
      ...common,
      'GET /api/v1/queries/films': () => jsonResponse(FILM, 200, { ETag: ETAG }),
      'POST /api/v1/queries/films/versions': () => jsonResponse(FILM, 201),
    });
    renderAt('/queries/films/edit?from=2');
    expect(await screen.findByRole('heading', { name: 'New version of films' })).toBeInTheDocument();
    expect(screen.getByLabelText('SQL')).toHaveValue('SELECT title FROM film WHERE film_id = :film_id');
    await waitFor(() => expect(screen.getByLabelText('Connection')).toHaveValue('lite'));
    await userEvent.click(screen.getByRole('button', { name: 'Save and publish' }));

    await waitFor(() => expect(calls.some((c) => c.path === '/api/v1/queries/films/versions')).toBe(true));
    const saved = calls.find((c) => c.path === '/api/v1/queries/films/versions')!;
    expect(saved.headers.get('If-Match')).toBe(ETAG);
    expect(saved.body).toMatchObject({
      publish: true,
      parameters: { film_id: { type: 'integer', required: true, min: 1 } }, // `min` survived the round trip
    });
  });

  it('tests the unsaved SQL with typed parameter values and shows the rows', async () => {
    const { calls } = fakeBackend({
      ...common,
      'GET /api/v1/queries/films': () => jsonResponse(FILM, 200, { ETag: ETAG }),
      'POST /execute_sql': () => jsonResponse([{ title: 'Alpha' }], 200, { 'X-Has-More': 'false' }),
    });
    renderAt('/queries/films/edit?from=2');
    await userEvent.type(await screen.findByLabelText('film_id'), '7');
    await userEvent.click(screen.getByRole('button', { name: /Run test/ }));
    const results = await screen.findByRole('table', { name: 'Test results' });
    expect(within(results).getByText('Alpha')).toBeInTheDocument();
    const run = calls.find((c) => c.path === '/execute_sql')!;
    expect(run.body).toEqual({
      sql: 'SELECT title FROM film WHERE film_id = :film_id',
      connection_name: 'lite',
      params: { film_id: 7 },
    });
  });
});

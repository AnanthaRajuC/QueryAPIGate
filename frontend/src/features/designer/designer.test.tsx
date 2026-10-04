import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { fakeBackend, jsonResponse } from '@/test/fakeBackend';
import { baseRoutes, FILMS } from '@/test/fixtures';
import { renderAt } from '@/test/renderApp';

import { candidateAt, literalToValue, mongoDocParams, resetDesignerState } from './state';

// CodeMirror needs layout APIs jsdom lacks; a textarea with the same contract stands in for it.
vi.mock('@/components/SqlEditor', () => ({
  SqlEditor: ({
    value,
    onChange,
    id,
    onRun,
  }: {
    value: string;
    onChange?: (v: string) => void;
    id?: string;
    onRun?: () => void;
  }) => (
    <textarea
      id={id}
      aria-label="SQL"
      value={value}
      onChange={(e) => onChange?.(e.target.value)}
      onKeyDown={(e) => {
        if (e.key === 'Enter' && e.ctrlKey) onRun?.();
      }}
    />
  ),
}));

const routes = () => ({
  ...baseRoutes(),
  'GET /api/v1/connections': () => ({
    items: [
      { name: 'lite', db: 'sqlite', active: true, host: null, port: null, database: '/data/films.db' },
      { name: 'warehouse', db: 'postgres', active: true, host: 'pg.internal', port: 5432, database: 'dw' },
    ],
  }),
  'GET /connections': () => ({
    connections: { lite: { usage: { queries: 3, errors: 1, avg_duration_ms: 4.5 } } },
  }),
  'POST /connections/databases': () => ({ databases: ['dw', 'staging'] }),
  'POST /execute_sql': () =>
    jsonResponse([{ id: 1, title: 'Alpha' }], 200, {
      'X-Page': '1',
      'X-Page-Size': '10',
      'X-Has-More': 'false',
    }),
});

beforeEach(() => resetDesignerState());
afterEach(() => vi.unstubAllGlobals());

describe('API Designer', () => {
  it('picks connections through Type → Host → Connection, showing the picked one in context', async () => {
    fakeBackend(routes());
    renderAt('/designer');
    const connection = await screen.findByLabelText('Connection');
    await waitFor(() => expect(within(connection).getAllByRole('option')).toHaveLength(2));
    expect(connection).toHaveValue('lite');
    expect(await screen.findByText('3 queries · 1 failed · 4.5ms avg')).toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText('Database type'), 'postgres');
    expect(screen.getByLabelText('Connection')).toHaveValue('warehouse');
    await waitFor(() => expect(screen.getByLabelText('Database')).not.toBeDisabled());
    expect(
      within(screen.getByLabelText('Database'))
        .getAllByRole('option')
        .map((o) => o.textContent),
    ).toEqual(['dw', 'staging']);
  });

  it('runs the SQL with its bound parameters and records it in Recent Queries', async () => {
    const { calls } = fakeBackend(routes());
    renderAt('/designer');
    await waitFor(() => expect(screen.getByLabelText('Connection')).toHaveValue('lite'));
    await userEvent.type(screen.getByLabelText('SQL'), 'SELECT id, title FROM film WHERE id = :id');
    await userEvent.click(document.querySelector('.side-tab[data-side-tab="settings"]')!);
    await userEvent.type(screen.getByLabelText(/Bound parameters/), '{{"id": 1}');
    expect(document.getElementById('run-refs')).toHaveTextContent(':id');
    await userEvent.click(screen.getByRole('button', { name: 'Run' }));
    expect(await screen.findByText('Alpha')).toBeInTheDocument();
    const run = calls.find((c) => c.path === '/execute_sql')!;
    expect(run.body).toEqual({
      sql: 'SELECT id, title FROM film WHERE id = :id',
      connection_name: 'lite',
      params: { id: 1 },
    });
    expect(run.search.get('page_size')).toBe('10');
    expect(document.getElementById('run-quick-stats')).toHaveTextContent('200 OK');
    await userEvent.click(document.querySelector('.side-tab[data-side-tab="recent"]')!);
    expect(screen.getByRole('button', { name: /SELECT id, title FROM film/ })).toHaveTextContent('1 row');
  });

  it('Explain prefixes EXPLAIN and Ctrl+Enter runs', async () => {
    const { calls } = fakeBackend(routes());
    renderAt('/designer');
    await waitFor(() => expect(screen.getByLabelText('Connection')).toHaveValue('lite'));
    await userEvent.type(screen.getByLabelText('SQL'), 'SELECT 1');
    await userEvent.click(screen.getByRole('button', { name: 'Explain' }));
    await waitFor(() => expect(calls.filter((c) => c.path === '/execute_sql')).toHaveLength(1));
    expect(calls.find((c) => c.path === '/execute_sql')!.body).toMatchObject({ sql: 'EXPLAIN SELECT 1' });
    await userEvent.type(screen.getByLabelText('SQL'), '{Control>}{Enter}{/Control}');
    await waitFor(() => expect(calls.filter((c) => c.path === '/execute_sql')).toHaveLength(2));
    expect(calls.filter((c) => c.path === '/execute_sql')[1]!.body).toMatchObject({ sql: 'SELECT 1' });
  });

  it('refuses bound parameters that are not JSON, before sending anything', async () => {
    const { calls } = fakeBackend(routes());
    renderAt('/designer');
    await waitFor(() => expect(screen.getByLabelText('Connection')).toHaveValue('lite'));
    await userEvent.type(screen.getByLabelText('SQL'), 'SELECT 1');
    await userEvent.click(document.querySelector('.side-tab[data-side-tab="settings"]')!);
    await userEvent.type(screen.getByLabelText(/Bound parameters/), '{{oops');
    await userEvent.click(screen.getByRole('button', { name: 'Run' }));
    expect(document.getElementById('error-banner')).toHaveTextContent('Bound parameters must be valid JSON');
    expect(calls.some((c) => c.path === '/execute_sql')).toBe(false);
  });

  it('Save as New API saves the editor query, published at once, and opens it', async () => {
    const { calls } = fakeBackend({
      ...routes(),
      'POST /api/v1/queries': () => jsonResponse({ ...FILMS, name: 'by_id', latest_version: 1 }, 201),
      'GET /api/v1/queries/by_id': () => jsonResponse({ ...FILMS, name: 'by_id' }),
    });
    renderAt('/designer');
    await waitFor(() => expect(screen.getByLabelText('Connection')).toHaveValue('lite'));
    await userEvent.type(screen.getByLabelText('SQL'), 'SELECT * FROM film WHERE id = :id');
    await userEvent.click(screen.getByRole('button', { name: 'Save as New API' }));
    const panel = document.getElementById('run-save-panel')!;
    expect(within(panel).getByLabelText('query_parameters')).toHaveValue('{\n  "id": {}\n}');
    await userEvent.type(within(panel).getByLabelText('Filename'), 'by_id');
    await userEvent.type(within(panel).getByLabelText('Author'), 'bob');
    await userEvent.type(within(panel).getByLabelText('Description'), 'One film');
    await userEvent.click(within(panel).getByRole('button', { name: 'Save' }));
    await waitFor(() =>
      expect(calls.some((c) => c.method === 'POST' && c.path === '/api/v1/queries')).toBe(true),
    );
    expect(calls.find((c) => c.method === 'POST' && c.path === '/api/v1/queries')!.body).toMatchObject({
      name: 'by_id',
      sql: 'SELECT * FROM film WHERE id = :id',
      connection_name: 'lite',
      parameters: { id: {} },
      publish: true,
    });
    expect(await screen.findByText('Saved by_id v1')).toBeInTheDocument();
  });
});

describe('Parameterize', () => {
  it('finds the `column op literal` under a double-click, on the column or the value', () => {
    const sql = "SELECT * FROM t WHERE rating = 'PG' AND length > 90";
    const onValue = candidateAt(sql, sql.indexOf('PG'), sql.indexOf('PG') + 2)!;
    expect(onValue).toMatchObject({ name: 'rating', valueText: "'PG'" });
    expect(sql.slice(onValue.absStart, onValue.absEnd)).toBe("'PG'");
    const onColumn = candidateAt(sql, sql.indexOf('length'), sql.indexOf('length') + 6)!;
    expect(onColumn).toMatchObject({ name: 'length', valueText: '90' });
    expect(candidateAt(sql, 0, 6)).toBeNull(); // SELECT
  });

  it('turns the literal into a JSON value', () => {
    expect(literalToValue("'it''s'")).toBe("it's");
    expect(literalToValue('42')).toBe(42);
    expect(literalToValue('NULL')).toBeNull();
    expect(literalToValue('true')).toBe(true);
  });

  it('finds :name parameters in a Mongo find() document', () => {
    expect(
      mongoDocParams('{"collection": "o", "filter": {"status": ":status", "n": {"$gt": ":min"}}}'),
    ).toEqual(['status', 'min']);
    expect(mongoDocParams('not json')).toEqual([]);
  });
});

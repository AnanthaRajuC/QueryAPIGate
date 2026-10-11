import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useState, type FormEvent } from 'react';

import { api, unwrap } from '@/api/client';
import { Field, useFeedback } from '@/app/feedback';

// What tables and columns mean (BACKLOG #86): written here, or imported from the database's own comments, and
// served with the schema - to the people browsing it and to AI agents over MCP (list_tables, the data dictionary).

const IMPORTABLE = ['postgres', 'mysql', 'clickhouse', 'duckdb'];

type Item = { table: string | null; column?: string | null; description: string | null };

export interface DescribedTable {
  name: string;
  description?: string;
  columns: { name: string; type?: string | null; description?: string }[];
}

function useSave(connection: string, onSaved: (message: string) => void) {
  const client = useQueryClient();
  const { showError } = useFeedback();
  return useMutation({
    mutationFn: async (items: Item[]) =>
      unwrap(
        await api.PATCH('/api/v1/connections/{name}/descriptions', {
          params: { path: { name: connection } },
          body: { items },
        }),
      ),
    onSuccess: () => {
      showError('');
      void client.invalidateQueries({ queryKey: ['connections', connection, 'schema'] });
      onSaved('Saved descriptions');
    },
    onError: (error: Error) => showError(error.message),
  });
}

/** The drawer for one table: its description and each column's. Only what changed is sent; an emptied box removes. */
export function DescribeTable({ connection, table }: { connection: string; table: DescribedTable }) {
  const { closeDrawer, toast } = useFeedback();
  const [own, setOwn] = useState(table.description ?? '');
  const [columns, setColumns] = useState<Record<string, string>>(
    Object.fromEntries(table.columns.map((c) => [c.name, c.description ?? ''])),
  );
  const save = useSave(connection, (message) => {
    closeDrawer();
    toast(message);
  });

  const submit = (e: FormEvent) => {
    e.preventDefault();
    const items: Item[] = [];
    if (own.trim() !== (table.description ?? ''))
      items.push({ table: table.name, description: own.trim() || null });
    table.columns.forEach((c) => {
      const next = (columns[c.name] ?? '').trim();
      if (next !== (c.description ?? ''))
        items.push({ table: table.name, column: c.name, description: next || null });
    });
    if (!items.length) closeDrawer();
    else save.mutate(items);
  };

  return (
    <form className="form" noValidate onSubmit={submit}>
      <p className="hint">
        What this table and its columns mean - shown here, and told to AI agents over MCP so they can write
        the right SQL. Only for the tables each caller can reach.
      </p>
      <Field id="d-table" label={table.name}>
        <textarea
          id="d-table"
          rows={2}
          value={own}
          placeholder="What one row is, e.g. One row per paying customer"
          onChange={(e) => setOwn(e.target.value)}
        />
      </Field>
      {table.columns.map((c) => (
        <Field key={c.name} id={'d-col-' + c.name} label={c.name} hint={c.type ?? undefined}>
          <input
            id={'d-col-' + c.name}
            value={columns[c.name] ?? ''}
            onChange={(e) => setColumns((all) => ({ ...all, [c.name]: e.target.value }))}
          />
        </Field>
      ))}
      <div className="form-actions">
        <button type="submit" className="btn primary" disabled={save.isPending}>
          Save
        </button>
        <button type="button" className="btn" onClick={closeDrawer}>
          Cancel
        </button>
      </div>
    </form>
  );
}

/** The connection's own notes (joins to use, metric definitions), and importing the database's comments. */
export function DescribeConnection({
  connection,
  dialect,
  notes,
}: {
  connection: string;
  dialect?: string | null;
  notes?: string;
}) {
  const { closeDrawer, showError, toast } = useFeedback();
  const client = useQueryClient();
  const [text, setText] = useState(notes ?? '');
  const [overwrite, setOverwrite] = useState(false);
  const save = useSave(connection, (message) => {
    closeDrawer();
    toast(message);
  });
  const importing = useMutation({
    mutationFn: async () =>
      unwrap(
        await api.POST('/api/v1/connections/{name}/descriptions/import', {
          params: { path: { name: connection } },
          body: { overwrite },
        }),
      ),
    onSuccess: (result) => {
      showError('');
      void client.invalidateQueries({ queryKey: ['connections', connection, 'schema'] });
      toast(
        `Found ${result.found} comment${result.found === 1 ? '' : 's'}: ${result.imported} imported, ` +
          `${result.kept} kept as they were`,
      );
    },
    onError: (error: Error) => showError(error.message),
  });

  return (
    <form
      className="form"
      noValidate
      onSubmit={(e) => {
        e.preventDefault();
        if (text.trim() === (notes ?? '')) closeDrawer();
        else save.mutate([{ table: null, description: text.trim() || null }]);
      }}
    >
      <Field
        id="d-notes"
        label="Notes"
        hint="For everyone writing SQL here, agents included: which joins to use, how a metric is worked out."
      >
        <textarea
          id="d-notes"
          rows={8}
          value={text}
          placeholder={'Join orders to customer on customer_id.\nRevenue is SUM(amt) over paid orders only.'}
          onChange={(e) => setText(e.target.value)}
        />
      </Field>
      {dialect && IMPORTABLE.includes(dialect) ? (
        <Field
          label="From the database"
          hint="Use the table and column comments already in the database. Descriptions written here are kept unless you replace them."
        >
          <label className="switch">
            <input type="checkbox" checked={overwrite} onChange={(e) => setOverwrite(e.target.checked)} />
            <span>Replace descriptions written here too</span>
          </label>
          <div>
            <button
              type="button"
              className="btn"
              disabled={importing.isPending}
              onClick={() => importing.mutate()}
            >
              Import comments
            </button>
          </div>
        </Field>
      ) : null}
      <div className="form-actions">
        <button type="submit" className="btn primary" disabled={save.isPending}>
          Save
        </button>
        <button type="button" className="btn" onClick={closeDrawer}>
          Cancel
        </button>
      </div>
    </form>
  );
}

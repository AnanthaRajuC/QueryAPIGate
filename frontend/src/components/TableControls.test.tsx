import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import { NoMatches, SortHeader, TableFilter } from './TableControls';
import { useTableControls, type Column } from './useTableControls';

type Row = { name: string; calls: number | null; seen: string | null };

const ROWS: Row[] = [
  { name: 'key-10', calls: 5, seen: '2026-11-02' },
  { name: 'Beta', calls: null, seen: null },
  { name: 'key-2', calls: 40, seen: '2026-11-20' },
  { name: 'alpha', calls: 12, seen: '2026-10-01' },
];

const COLUMNS: Column<Row>[] = [
  { key: 'name', label: 'Name', sort: (r) => r.name },
  { key: 'calls', label: 'Calls', sort: (r) => r.calls },
  { key: 'seen', label: 'Last seen', sort: (r) => r.seen },
  { key: 'actions', label: '' },
];

function Table({ rows = ROWS }: { rows?: Row[] }) {
  const t = useTableControls(rows, { columns: COLUMNS, text: (r) => [r.name, r.seen] });
  return (
    <>
      <TableFilter controls={t} placeholder="Filter…" label="Filter rows" />
      <table>
        <thead>
          <tr>
            {t.columns.map((c) => (
              <SortHeader key={c.key} controls={t} column={c} />
            ))}
          </tr>
        </thead>
        <tbody>
          {t.rows.map((r) => (
            <tr key={r.name}>
              <td>{r.name}</td>
            </tr>
          ))}
          {!t.rows.length && <NoMatches controls={t} span={COLUMNS.length} />}
        </tbody>
      </table>
    </>
  );
}

const names = () =>
  within(screen.getAllByRole('rowgroup')[1]!)
    .queryAllByRole('row')
    .map((r) => r.textContent);

describe('table controls', () => {
  it('sorts ascending, then descending, then back to the table’s own order', async () => {
    render(<Table />);
    const header = screen.getByRole('columnheader', { name: 'Name' });
    expect(header).toHaveAttribute('aria-sort', 'none');

    await userEvent.click(within(header).getByRole('button'));
    // case-insensitive, and numbers inside names as numbers
    expect(names()).toEqual(['alpha', 'Beta', 'key-2', 'key-10']);
    expect(header).toHaveAttribute('aria-sort', 'ascending');

    await userEvent.click(within(header).getByRole('button'));
    expect(names()).toEqual(['key-10', 'key-2', 'Beta', 'alpha']);
    expect(header).toHaveAttribute('aria-sort', 'descending');

    await userEvent.click(within(header).getByRole('button'));
    expect(names()).toEqual(['key-10', 'Beta', 'key-2', 'alpha']);
    expect(header).toHaveAttribute('aria-sort', 'none');
  });

  it('keeps blanks last whichever way a column sorts', async () => {
    render(<Table />);
    const calls = within(screen.getByRole('columnheader', { name: 'Calls' })).getByRole('button');
    await userEvent.click(calls);
    expect(names()).toEqual(['key-10', 'alpha', 'key-2', 'Beta']);
    await userEvent.click(calls);
    expect(names()).toEqual(['key-2', 'alpha', 'key-10', 'Beta']);
  });

  it('moves the sort to the column last clicked', async () => {
    render(<Table />);
    await userEvent.click(within(screen.getByRole('columnheader', { name: 'Name' })).getByRole('button'));
    await userEvent.click(
      within(screen.getByRole('columnheader', { name: 'Last seen' })).getByRole('button'),
    );
    expect(names()).toEqual(['alpha', 'key-10', 'key-2', 'Beta']);
    expect(screen.getByRole('columnheader', { name: 'Name' })).toHaveAttribute('aria-sort', 'none');
  });

  it('leaves a column without a sort as a plain header', () => {
    render(<Table />);
    const headers = screen.getAllByRole('columnheader');
    expect(within(headers[3]!).queryByRole('button')).toBeNull();
    expect(headers[3]).not.toHaveAttribute('aria-sort');
  });

  it('filters on every value a row gives it, and counts what is left', async () => {
    render(<Table />);
    expect(screen.queryByText(/of 4/)).toBeNull();
    await userEvent.type(screen.getByRole('searchbox', { name: 'Filter rows' }), 'KEY');
    expect(names()).toEqual(['key-10', 'key-2']);
    expect(screen.getByText('2 of 4')).toBeInTheDocument();

    await userEvent.clear(screen.getByRole('searchbox', { name: 'Filter rows' }));
    await userEvent.type(screen.getByRole('searchbox', { name: 'Filter rows' }), '2026-10');
    expect(names()).toEqual(['alpha']);
  });

  it('sorts what the filter leaves', async () => {
    render(<Table />);
    await userEvent.type(screen.getByRole('searchbox', { name: 'Filter rows' }), 'key');
    await userEvent.click(within(screen.getByRole('columnheader', { name: 'Calls' })).getByRole('button'));
    expect(names()).toEqual(['key-10', 'key-2']);
  });

  it('says when nothing matches, and clears the filter from there', async () => {
    render(<Table />);
    await userEvent.type(screen.getByRole('searchbox', { name: 'Filter rows' }), 'nope');
    expect(names()).toEqual(['Nothing matches “nope”. Clear the filter']);
    await userEvent.click(screen.getByRole('button', { name: 'Clear the filter' }));
    expect(names()).toHaveLength(4);
    expect(screen.getByRole('searchbox', { name: 'Filter rows' })).toHaveValue('');
  });
});

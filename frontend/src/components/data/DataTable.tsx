import { useTable, type RowData } from '@tanstack/react-table';
import { ArrowDown, ArrowUp } from 'lucide-react';

import { cn } from '@/lib/utils';

import { features, type DataColumn } from './columns';

/** A sortable table (TanStack Table): click a header to sort. Columns come from the caller. */
export function DataTable<T extends RowData>({
  columns,
  data,
  label,
  empty,
}: {
  columns: DataColumn<T>[];
  data: T[];
  label: string;
  empty?: React.ReactNode;
}) {
  const table = useTable({ features, columns, data });
  return (
    <div className="overflow-x-auto rounded-lg border border-line bg-surface">
      <table aria-label={label} className="w-full text-left text-sm">
        <thead className="border-b border-line bg-surface-2 text-xs text-ink-2">
          {table.getHeaderGroups().map((group) => (
            <tr key={group.id}>
              {group.headers.map((header) => {
                const sorted = header.column.getIsSorted();
                const sortable = header.column.getCanSort();
                return (
                  <th
                    key={header.id}
                    scope="col"
                    aria-sort={sorted === 'asc' ? 'ascending' : sorted === 'desc' ? 'descending' : undefined}
                    className="px-3 py-2 font-medium whitespace-nowrap"
                  >
                    {header.isPlaceholder ? null : sortable ? (
                      <button
                        type="button"
                        onClick={header.column.getToggleSortingHandler()}
                        className="inline-flex items-center gap-1 hover:text-ink"
                      >
                        <table.FlexRender header={header} />
                        {sorted === 'asc' && <ArrowUp size={12} aria-hidden />}
                        {sorted === 'desc' && <ArrowDown size={12} aria-hidden />}
                      </button>
                    ) : (
                      <table.FlexRender header={header} />
                    )}
                  </th>
                );
              })}
            </tr>
          ))}
        </thead>
        <tbody>
          {table.getRowModel().rows.map((row) => (
            <tr key={row.id} className="border-b border-line last:border-0 hover:bg-surface-2/60">
              {row.getAllCells().map((cell) => (
                <td key={cell.id} className={cn('px-3 py-2 align-top')}>
                  <table.FlexRender cell={cell} />
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {data.length === 0 && empty && <div className="px-3 py-8 text-center text-sm text-ink-3">{empty}</div>}
    </div>
  );
}

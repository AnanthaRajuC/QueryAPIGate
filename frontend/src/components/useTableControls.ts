import { useState, type ReactNode } from 'react';

// The state behind a sortable, filterable list table: the filter text, the column it sorts by, and the rows that
// leaves. TableControls.tsx draws the headers, the filter box and the no-matches row from it.

export type SortValue = string | number | boolean | null | undefined;

export interface Column<T> {
  key: string;
  label: ReactNode;
  /** The value a click on the header sorts by; a column without one isn't sortable (an activity chart, actions). */
  sort?: (row: T) => SortValue;
  /** Shown on hover, for a header whose label is short. */
  title?: string;
  /** The header's own class, such as `num` for a right-aligned number column. */
  className?: string;
}

export type SortState = { key: string; dir: 'asc' | 'desc' } | null;

/** Text order that reads numbers as numbers ("key-2" before "key-10"), case-insensitively. */
const collator = new Intl.Collator(undefined, { numeric: true, sensitivity: 'base' });

function compare(a: SortValue, b: SortValue): number {
  const blank = (v: SortValue) => v === null || v === undefined || v === '';
  if (blank(a) || blank(b)) return blank(a) === blank(b) ? 0 : blank(a) ? 1 : -1; // blanks last, either way
  if (typeof a === 'number' && typeof b === 'number') return a - b;
  if (typeof a === 'boolean' && typeof b === 'boolean') return Number(b) - Number(a); // true first
  return collator.compare(String(a), String(b));
}

export function useTableControls<T>(
  rows: readonly T[],
  {
    columns,
    text,
    initial = null,
  }: {
    columns: Column<T>[];
    /** Everything a row shows that a filter should find, joined. */
    text: (row: T) => (string | number | null | undefined)[];
    initial?: SortState;
  },
) {
  const [query, setQuery] = useState('');
  const [sort, setSort] = useState<SortState>(initial);
  // worked out each render: these lists are tens or hundreds of rows, and a memo keyed on the caller's inline
  // columns and text functions would miss every time anyway
  const shown = (() => {
    const needle = query.trim().toLowerCase();
    const found = needle
      ? rows.filter((row) =>
          text(row)
            .filter((v) => v !== null && v !== undefined)
            .join(' ')
            .toLowerCase()
            .includes(needle),
        )
      : [...rows];
    const by = sort && columns.find((c) => c.key === sort.key)?.sort;
    if (!sort || !by) return found;
    const sign = sort.dir === 'asc' ? 1 : -1;
    // blanks stay last whichever way, so only the comparison of two values flips
    return found
      .map((row, i) => ({ row, i, v: by(row) }))
      .sort((x, y) => {
        const blankX = x.v === null || x.v === undefined || x.v === '';
        const blankY = y.v === null || y.v === undefined || y.v === '';
        if (blankX || blankY) return compare(x.v, y.v) || x.i - y.i;
        return sign * compare(x.v, y.v) || x.i - y.i;
      })
      .map((x) => x.row);
  })();

  /** Ascending, then descending, then back to the table's own order. */
  const toggle = (key: string) =>
    setSort((s) => (s?.key !== key ? { key, dir: 'asc' } : s.dir === 'asc' ? { key, dir: 'desc' } : null));

  return { rows: shown, total: rows.length, query, setQuery, sort, toggle, columns };
}

export type TableControls<T> = ReturnType<typeof useTableControls<T>>;

import type { Column, TableControls } from './useTableControls';

// Sorting and filtering for the Console's list tables - the ones that hold their whole list in the browser (API keys,
// roles, MCP tools, connections, ...). Not for the ones fetched a page at a time (the audit log, run history, query
// results): sorting one page in the browser would look right and be wrong.
//
//   const t = useTableControls(rows, { columns: COLUMNS, text: (r) => [r.name, r.description] });
//   <TableFilter controls={t} placeholder="Filter keys…" />
//   <thead><tr>{t.columns.map((c) => <SortHeader key={c.key} controls={t} column={c} />)}</tr></thead>
//   <tbody>{t.rows.map(...)}</tbody>   {!t.rows.length && <NoMatches controls={t} span={n} />}

function SortIcon({ dir }: { dir: 'asc' | 'desc' | null }) {
  return (
    <svg className="sort-icon" viewBox="0 0 12 16" aria-hidden="true">
      <path d="M6 2.5 9.5 6.5h-7Z" className={dir === 'asc' ? 'on' : ''} />
      <path d="M6 13.5 2.5 9.5h7Z" className={dir === 'desc' ? 'on' : ''} />
    </svg>
  );
}

/** A column header: a button that sorts by it, with ▲▼ showing how; a plain header for a column that doesn't sort. */
export function SortHeader<T>({ controls, column }: { controls: TableControls<T>; column: Column<T> }) {
  if (!column.sort)
    return (
      <th className={column.className} title={column.title}>
        {column.label}
      </th>
    );
  const dir = controls.sort?.key === column.key ? controls.sort.dir : null;
  return (
    <th
      className={[column.className, 'sortable', dir ? 'sorted' : ''].filter(Boolean).join(' ')}
      aria-sort={dir === 'asc' ? 'ascending' : dir === 'desc' ? 'descending' : 'none'}
      title={column.title}
    >
      <button type="button" className="th-sort" onClick={() => controls.toggle(column.key)}>
        {column.label}
        <SortIcon dir={dir} />
      </button>
    </th>
  );
}

/** The filter box, and how many rows it leaves, for a table's .panel-bar. */
export function TableFilter<T>({
  controls,
  placeholder,
  label,
  id,
}: {
  controls: TableControls<T>;
  placeholder: string;
  /** What the box filters, for a screen reader: "Filter API keys". */
  label: string;
  id?: string;
}) {
  const filtered = controls.query.trim() !== '';
  return (
    <>
      {filtered && (
        <span className="table-count" aria-live="polite">
          {controls.rows.length} of {controls.total}
        </span>
      )}
      <input
        id={id}
        className="search table-filter"
        type="search"
        placeholder={placeholder}
        aria-label={label}
        value={controls.query}
        onChange={(e) => controls.setQuery(e.target.value)}
      />
    </>
  );
}

/** The row a filtered table shows when nothing is left. */
export function NoMatches<T>({ controls, span }: { controls: TableControls<T>; span: number }) {
  return (
    <tr className="no-matches">
      <td colSpan={span}>
        Nothing matches “{controls.query.trim()}”.{' '}
        <button type="button" className="btn ghost sm" onClick={() => controls.setQuery('')}>
          Clear the filter
        </button>
      </td>
    </tr>
  );
}

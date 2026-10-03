import {
  createColumnHelper,
  createSortedRowModel,
  rowSortingFeature,
  tableFeatures,
  type ColumnDef,
  type RowData,
} from '@tanstack/react-table';

/** The table features every DataTable has: sorting by clicking a header. */
export const features = tableFeatures({ rowSortingFeature, sortedRowModel: createSortedRowModel() });

// Columns of one table hold values of different types, so the cell-value parameter is left open here.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export type DataColumn<T extends RowData> = ColumnDef<typeof features, T, any>;

/** Typed column builder for DataTable: `const col = columnHelper<Row>(); col.accessor('name', {...})`. */
export function columnHelper<T extends RowData>() {
  return createColumnHelper<typeof features, T>();
}

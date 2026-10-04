// The Access map's filters and sort, kept for the life of the page - as the classic tab, which only hides itself,
// kept them - and settable from elsewhere: "View in Access map" opens it on one query, or on one table.

export interface AmapSort {
  field: string | null;
  colType: 'key' | 'role' | null;
  colName: string | null;
  dir: 1 | -1;
}

export interface AmapState {
  q: string;
  conn: string;
  table: string;
  db: string;
  reach: '' | 'reachable' | 'unreachable';
  sort: AmapSort;
  /** Queries already known to touch `table` (the schema browser computed them), so the map needn't ask again. */
  tableMatches: string[] | null;
}

const initial = (): AmapState => ({
  q: '',
  conn: '',
  table: '',
  db: '',
  reach: '',
  sort: { field: null, colType: null, colName: null, dir: 1 },
  tableMatches: null,
});

let state = initial();

export const amapState = () => state;

export function setAmapState(next: AmapState) {
  state = next;
}

/** Open on one query: the search box set to its name, every other filter cleared. */
export function presetQuery(name: string) {
  state = { ...initial(), sort: state.sort, q: name };
}

/** Open on one table of one connection, with the queries already known to touch it. */
export function presetTable(connection: string, table: string, matches: string[]) {
  state = { ...initial(), sort: state.sort, conn: connection, table, tableMatches: matches };
}

/** Back to no filters - for tests. */
export function resetAmapState() {
  state = initial();
}

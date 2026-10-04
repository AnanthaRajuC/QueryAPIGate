import { preferredPageSize, readPrefs } from '@/app/prefs';

// The API Designer's working state, kept for the life of the page so leaving the screen and coming back finds the
// query where it was - as the classic UI, which only hides its tab, always did. Not persisted beyond the page.

export interface DesignerState {
  type: string;
  host: string;
  connection: string;
  database: string;
  format: string;
  sql: string;
  params: string;
  page: number;
  pageSize: number;
  timeout: string;
}

const preferredFormat = () => readPrefs().format;

let saved: DesignerState | null = null;

export function loadDesignerState(): DesignerState {
  saved ??= {
    type: '',
    host: '',
    connection: '',
    database: '',
    format: preferredFormat(),
    sql: '',
    params: '',
    page: 1,
    pageSize: preferredPageSize(),
    timeout: '',
  };
  return saved;
}

export function saveDesignerState(state: DesignerState) {
  saved = state;
}

/** For tests: start from a clean slate. */
export function resetDesignerState() {
  saved = null;
}

// ---- Recent queries: this browser tab only, shared with the classic UI (ui.py RUN_HISTORY_KEY) ----

export interface HistoryEntry {
  sql: string;
  connection: string;
  format: string;
  status: number | null;
  rows: number | null;
  duration_ms: number | null;
}

const RUN_HISTORY_KEY = 'queryapigate-run-history';
const RUN_HISTORY_MAX = 20;

export function loadRunHistory(): HistoryEntry[] {
  try {
    return JSON.parse(sessionStorage.getItem(RUN_HISTORY_KEY) || '[]') as HistoryEntry[];
  } catch {
    return [];
  }
}

function store(list: HistoryEntry[]) {
  try {
    sessionStorage.setItem(RUN_HISTORY_KEY, JSON.stringify(list));
  } catch {
    // history is a convenience - nothing breaks without it
  }
}

/** A run starts: newest first, one entry per SQL + connection, outcome filled in by recordResult(). */
export function recordRun(sql: string, connection: string, format: string): HistoryEntry[] {
  const trimmed = sql.trim();
  if (!trimmed) return loadRunHistory();
  const list = loadRunHistory().filter((e) => !(e.sql === trimmed && e.connection === connection));
  list.unshift({ sql: trimmed, connection, format, status: null, rows: null, duration_ms: null });
  const capped = list.slice(0, RUN_HISTORY_MAX);
  store(capped);
  return capped;
}

export function recordResult(status: number, rows: number | null, durationMs: number | null): HistoryEntry[] {
  const list = loadRunHistory();
  if (!list.length) return list;
  list[0] = { ...list[0]!, status, rows, duration_ms: durationMs };
  store(list);
  return list;
}

// ---- "Parameterize": turn `col = literal` under a double-click into `col = :col` (ui.py paramizeCandidateAt) ----

const CANDIDATE_RE =
  /([A-Za-z_]\w*)\s*(=|!=|<>|<=|>=|<|>)\s*('(?:[^']|'')*'|(?:-?\d+(?:\.\d+)?|NULL|TRUE|FALSE)\b)/dgi;

export interface Candidate {
  name: string;
  valueText: string;
  absStart: number;
  absEnd: number;
}

export function candidateAt(text: string, clickStart: number, clickEnd: number): Candidate | null {
  const lineStart = text.lastIndexOf('\n', clickStart - 1) + 1;
  let lineEnd = text.indexOf('\n', clickEnd);
  if (lineEnd === -1) lineEnd = text.length;
  const line = text.slice(lineStart, lineEnd);
  const offset = clickStart - lineStart;
  const endOffset = clickEnd - lineStart;
  CANDIDATE_RE.lastIndex = 0;
  for (let m = CANDIDATE_RE.exec(line); m; m = CANDIDATE_RE.exec(line)) {
    const ident = m.indices![1]!;
    const value = m.indices![3]!;
    const hit = (offset < ident[1] && endOffset > ident[0]) || (offset < value[1] && endOffset > value[0]);
    if (hit)
      return { name: m[1]!, valueText: m[3]!, absStart: lineStart + value[0], absEnd: lineStart + value[1] };
  }
  return null;
}

export function literalToValue(text: string): unknown {
  if (/^null$/i.test(text)) return null;
  if (/^true$/i.test(text)) return true;
  if (/^false$/i.test(text)) return false;
  if (/^-?\d+(\.\d+)?$/.test(text)) return Number(text);
  if (text.startsWith("'") && text.endsWith("'")) return text.slice(1, -1).replace(/''/g, "'");
  return text;
}

/** :name parameters in a Mongo find() document - string leaves that are exactly ":name" (ui.py mongoDocParams). */
export function mongoDocParams(text: string): string[] {
  let doc: unknown;
  try {
    doc = JSON.parse(text);
  } catch {
    return [];
  }
  const out: string[] = [];
  const walk = (v: unknown) => {
    if (typeof v === 'string') {
      const m = /^:([A-Za-z_]\w*)$/.exec(v);
      if (m && !out.includes(m[1]!)) out.push(m[1]!);
    } else if (Array.isArray(v)) v.forEach(walk);
    else if (v && typeof v === 'object') Object.values(v).forEach(walk);
  };
  walk(doc);
  return out;
}

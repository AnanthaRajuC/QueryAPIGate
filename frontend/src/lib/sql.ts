// The classic UI's SQL tokenizer (ui.py highlightInto/sqlParams), ported as-is so read-only SQL is coloured exactly
// as it always was: .k keywords, .s strings, .n numbers, .p :params, .c comments (classic.css).

const SQL_KW = new Set(
  (
    'select from where and or not in is null as join left right inner outer full cross on using group by order having limit offset union ' +
    'all distinct insert into values update set delete create table view index drop alter with recursive case when then else end asc desc ' +
    'like ilike between exists returning primary key default top fetch next rows only over partition window count sum avg min max ' +
    'coalesce cast true false interval date timestamp extract filter lateral intersect except nulls first last'
  ).split(' '),
);

const SQL_TOKEN =
  /(--[^\n]*|\/\*[\s\S]*?(?:\*\/|$))|('(?:[^']|'')*'?)|("(?:[^"]|"")*"?|`[^`]*`?)|(::)|(:[A-Za-z_]\w*)|(\b\d+(?:\.\d+)?\b)|([A-Za-z_]\w*)/g;

export type Token = { cls: 'k' | 's' | 'n' | 'p' | 'c' | null; text: string };

export function tokenize(sql: string): Token[] {
  const out: Token[] = [];
  let last = 0;
  SQL_TOKEN.lastIndex = 0;
  for (let m = SQL_TOKEN.exec(sql); m; m = SQL_TOKEN.exec(sql)) {
    if (m.index > last) out.push({ cls: null, text: sql.slice(last, m.index) });
    let cls: Token['cls'] = null;
    if (m[1]) cls = 'c';
    else if (m[2]) cls = 's';
    else if (m[5]) cls = 'p';
    else if (m[6]) cls = 'n';
    else if (m[7] && SQL_KW.has(m[7].toLowerCase())) cls = 'k';
    out.push({ cls, text: m[0] });
    last = SQL_TOKEN.lastIndex;
  }
  if (last < sql.length) out.push({ cls: null, text: sql.slice(last) });
  return out;
}

/** :name parameters the SQL binds, in order of first use (ui.py sqlParams). */
export function sqlParams(sql: string): string[] {
  const seen = new Set<string>();
  SQL_TOKEN.lastIndex = 0;
  for (let m = SQL_TOKEN.exec(sql); m; m = SQL_TOKEN.exec(sql)) {
    if (m[5]) seen.add(m[5].slice(1));
  }
  return [...seen];
}

/** Quote for a POSIX shell (ui.py shQuote). */
export function shQuote(s: string): string {
  return "'" + s.replace(/'/g, "'\\''") + "'";
}

const SQL_CLAUSE_KW = new Set(
  'select from where having limit offset union with insert update set values join on group order delete left right inner outer full cross'.split(
    ' ',
  ),
);
const SQL_JOIN_PREFIX = new Set(['left', 'right', 'inner', 'outer', 'full', 'cross']);

/** Best-effort pretty-printing for SQL with no line breaks of its own: each clause on its own line (ui.py
 * formatSql) - used only when the server can't format it (dialects sqlglot doesn't parse). */
export function formatSql(sql: string): string {
  const pieces: string[] = [];
  let pos = 0;
  let lastWord: string | null = null;
  SQL_TOKEN.lastIndex = 0;
  for (let m = SQL_TOKEN.exec(sql); m; m = SQL_TOKEN.exec(sql)) {
    const gap = sql.slice(pos, m.index).replace(/[ \t]{2,}/g, ' ');
    const word = m[7] ? m[7].toLowerCase() : null;
    const glued =
      (word === 'join' && lastWord !== null && SQL_JOIN_PREFIX.has(lastWord)) ||
      (word === 'from' && lastWord === 'delete');
    if (word && SQL_CLAUSE_KW.has(word) && !glued) pieces.push(gap, '\n', m[0]);
    else pieces.push(gap, m[0]);
    lastWord = word;
    pos = SQL_TOKEN.lastIndex;
  }
  pieces.push(sql.slice(pos));
  return pieces
    .join('')
    .split('\n')
    .map((line) => line.trim())
    .filter((line) => line !== '')
    .join('\n');
}

export const PARSEABLE_DIALECTS = ['mysql', 'postgres', 'clickhouse', 'sqlite', 'duckdb'];

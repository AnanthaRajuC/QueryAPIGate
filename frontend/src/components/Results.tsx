import { useState } from 'react';

import { useFeedback, copyText } from '@/app/feedback';

import { ChartPanel, numericColumns } from './ChartPanel';

// A query response as the classic UI shows it (ui.py execute()/renderResponse()/renderTable()/pager()): the result
// bar (status, rows, range, format, time), the pager, Headers/Copy/Download/Copy as TSV, and the rows table.

const EXT: Record<string, string> = {
  json: 'json',
  ndjson: 'ndjson',
  csv: 'csv',
  tsv: 'tsv',
  xml: 'xml',
  yaml: 'yaml',
  xlsx: 'xlsx',
};

export interface ResultData {
  ok: boolean;
  status: number;
  statusText: string;
  contentType: string;
  headers: [string, string][];
  page: number;
  size: number;
  hasMore: boolean;
  elapsed: number;
  format: string;
  rows?: Record<string, unknown>[];
  text?: string;
  blob?: Blob;
  error?: string;
}

/** Read a fetch Response into what <Results> shows; a binary format (xlsx) is downloaded, as in the classic UI. */
export async function readResult(
  response: Response,
  o: { page: number; size: number; format: string; elapsed: number; filename: string },
): Promise<ResultData> {
  const headers: [string, string][] = [];
  response.headers.forEach((v, k) => headers.push([k, v]));
  headers.sort((a, b) => (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0));
  const contentType = (response.headers.get('content-type') || '').split(';')[0]!.trim();
  const base: ResultData = {
    ok: response.ok,
    status: response.status,
    statusText: response.statusText,
    contentType,
    headers,
    page: Number(response.headers.get('x-page')) || o.page,
    size: Number(response.headers.get('x-page-size')) || o.size,
    hasMore: response.headers.get('x-has-more') === 'true',
    elapsed: o.elapsed,
    format: o.format,
  };
  if (!response.ok) {
    const text = await response.text();
    let message = text || `HTTP ${response.status}`;
    try {
      const body = JSON.parse(text) as { error?: string };
      if (body?.error) message = body.error;
    } catch {
      // not JSON
    }
    return { ...base, error: message };
  }
  if (contentType === 'application/json') {
    const data = (await response.json()) as unknown;
    return Array.isArray(data)
      ? { ...base, rows: data as Record<string, unknown>[] }
      : { ...base, text: JSON.stringify(data, null, 2) };
  }
  if (
    contentType.startsWith('text/') ||
    ['application/xml', 'application/x-yaml', 'application/x-ndjson'].includes(contentType)
  ) {
    return { ...base, text: await response.text() };
  }
  const blob = await response.blob();
  download(blob, `${o.filename}${base.page > 1 ? '-p' + base.page : ''}.${EXT[o.format] || 'bin'}`);
  return { ...base, blob };
}

function download(blob: Blob, name: string) {
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 5000);
}

function columnsOf(rows: Record<string, unknown>[]) {
  const cols: string[] = [];
  rows.forEach((r) => Object.keys(r ?? {}).forEach((c) => (cols.includes(c) ? null : cols.push(c))));
  return cols;
}

function toTsv(rows: Record<string, unknown>[]) {
  const cols = columnsOf(rows);
  const cell = (v: unknown) =>
    (v === null || v === undefined ? '' : typeof v === 'object' ? JSON.stringify(v) : String(v)).replace(
      /[\t\n]/g,
      ' ',
    );
  return [cols.join('\t'), ...rows.map((r) => cols.map((c) => cell(r[c])).join('\t'))].join('\n');
}

export function Results({
  result,
  running,
  filename,
  onPage,
  onPageSize,
}: {
  result: ResultData | null;
  running: boolean;
  filename: string;
  onPage: (page: number) => void;
  onPageSize: (size: number) => void;
}) {
  const { toast } = useFeedback();
  const [showHeaders, setShowHeaders] = useState(false);
  const [showChart, setShowChart] = useState(false);
  if (running) {
    return (
      <div className="panel">
        <div className="resbar" />
        <div className="res-body">
          <div className="res-note">
            <span className="spin" />
            Running…
          </div>
        </div>
      </div>
    );
  }
  if (!result) return null;
  if (result.error) {
    return (
      <div className="panel">
        <div className="resbar" />
        <div className="res-body">
          <div className="res-note err">
            <span className="eb-code">{result.status}</span>
            {result.error}
          </div>
        </div>
      </div>
    );
  }
  const rows = result.rows;
  const payload = rows ? JSON.stringify(rows, null, 2) : (result.text ?? null);
  const offset = (result.page - 1) * result.size;
  const base = `${filename}${result.page > 1 ? '-p' + result.page : ''}`;
  return (
    <div className="panel">
      <div className="resbar">
        <span className="stat">
          <span>
            <b className="stat-ok">{result.status}</b>
            {result.statusText ? ' ' + result.statusText : ''}
          </span>
          {rows ? (
            <span>
              <b>{rows.length}</b>
              {rows.length === 1 ? ' row' : ' rows'}
            </span>
          ) : null}
          {rows && rows.length ? <span>{`rows ${offset + 1}–${offset + rows.length}`}</span> : null}
          <span>{result.format}</span>
          <span>{result.elapsed} ms</span>
        </span>
        <span className="spacer" />
        <Pager
          page={result.page}
          size={result.size}
          hasMore={result.hasMore}
          onPage={onPage}
          onPageSize={onPageSize}
        />
        <button type="button" className="btn sm ghost" onClick={() => setShowHeaders((s) => !s)}>
          Headers
        </button>
        {payload !== null && (
          <>
            <button type="button" className="btn sm ghost" onClick={() => copyText(payload, toast)}>
              Copy
            </button>
            <button
              type="button"
              className="btn sm ghost"
              onClick={() =>
                download(
                  new Blob([payload], { type: result.contentType || 'text/plain' }),
                  `${base}.${EXT[result.format] || 'txt'}`,
                )
              }
            >
              Download
            </button>
          </>
        )}
        {rows && (
          <button type="button" className="btn sm ghost" onClick={() => copyText(toTsv(rows), toast)}>
            Copy as TSV
          </button>
        )}
        {rows && rows.length > 0 && numericColumns(rows, columnsOf(rows)).length > 0 && (
          <button type="button" className="btn sm ghost" onClick={() => setShowChart((s) => !s)}>
            Chart
          </button>
        )}
      </div>
      <div className="res-body">
        {rows ? (
          <RowsTable rows={rows} offset={offset} />
        ) : result.text !== undefined ? (
          <pre className="res-pre">{result.text}</pre>
        ) : result.blob ? (
          <div className="res-note">
            Downloaded <code>{`${base}.${EXT[result.format] || 'bin'}`}</code> (
            {result.blob.size < 1024 ? `${result.blob.size} B` : `${(result.blob.size / 1024).toFixed(1)} KB`}
            )
            <button
              type="button"
              className="btn sm"
              onClick={() => download(result.blob!, `${base}.${EXT[result.format] || 'bin'}`)}
            >
              Download again
            </button>
          </div>
        ) : null}
        {showChart && rows && rows.length > 0 && <ChartPanel rows={rows} cols={columnsOf(rows)} />}
        <div className="panel headers-panel" hidden={!showHeaders}>
          <table className="grid">
            <tbody>
              {result.headers.map(([k, v]) => (
                <tr key={k}>
                  <td className="mono dim">{k}</td>
                  <td className="mono">{v}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

function Pager({
  page,
  size,
  hasMore,
  onPage,
  onPageSize,
}: {
  page: number;
  size: number;
  hasMore: boolean;
  onPage: (page: number) => void;
  onPageSize: (size: number) => void;
}) {
  const presets = [10, 25, 50, 100, 500];
  if (!presets.includes(size)) presets.push(size);
  presets.sort((a, b) => a - b);
  return (
    <div className="pager">
      <button type="button" className="btn sm" disabled={page <= 1} onClick={() => onPage(page - 1)}>
        ‹ Prev
      </button>
      <span className="hint">Page</span>
      <input
        type="number"
        min="1"
        defaultValue={page}
        key={page}
        aria-label="Page"
        onChange={(e) => onPage(Math.max(1, Number(e.target.value) || 1))}
      />
      <button
        type="button"
        className="btn sm"
        disabled={!hasMore}
        title={hasMore ? 'X-Has-More: true' : 'No more rows'}
        onClick={() => onPage(page + 1)}
      >
        {hasMore ? 'Next page ›' : 'Next ›'}
      </button>
      <select
        aria-label="Page size"
        value={String(size)}
        onChange={(e) => onPageSize(Number(e.target.value))}
      >
        {presets.map((n) => (
          <option key={n} value={String(n)}>
            {n} / page
          </option>
        ))}
      </select>
    </div>
  );
}

export function RowsTable({ rows, offset = 0 }: { rows: Record<string, unknown>[]; offset?: number }) {
  if (!rows.length) return <div className="res-note">No rows returned.</div>;
  const cols = columnsOf(rows);
  const numeric = Object.fromEntries(
    cols.map((c) => [
      c,
      rows.every((r) => r[c] === null || r[c] === undefined || typeof r[c] === 'number') &&
        rows.some((r) => typeof r[c] === 'number'),
    ]),
  );
  return (
    <div className="table-wrap">
      <table className="rs">
        <thead>
          <tr>
            <th className="rn">#</th>
            {cols.map((c) => (
              <th key={c} className={numeric[c] ? 'num' : undefined}>
                {c}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={i}>
              <td className="rn">{offset + i + 1}</td>
              {cols.map((c) => {
                const v = row[c];
                if (v === null || v === undefined) {
                  return (
                    <td key={c}>
                      <span className="null">NULL</span>
                    </td>
                  );
                }
                const s = typeof v === 'object' ? JSON.stringify(v) : String(v);
                return (
                  <td
                    key={c}
                    className={numeric[c] ? 'num' : undefined}
                    title={s.length > 40 ? s : undefined}
                  >
                    {s}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

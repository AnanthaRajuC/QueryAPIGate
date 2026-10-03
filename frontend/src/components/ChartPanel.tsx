import { useState } from 'react';

// The results bar's "Chart": a bar chart of this page's rows, label and value columns selectable (ui.py
// buildChartPanel/barChartSvg). Styled by classic.css (.chart-panel, .chart-bar, .chart-axis, .chart-label).

const CHART_MAX_BARS = 50;

export function numericColumns(rows: Record<string, unknown>[], cols: string[]) {
  return cols.filter((c) => {
    let any = false;
    const ok = rows.every((r) => {
      const v = r[c];
      if (v === null || v === undefined) return true;
      if (typeof v === 'number' && Number.isFinite(v)) {
        any = true;
        return true;
      }
      return false;
    });
    return any && ok;
  });
}

export function ChartPanel({ rows, cols }: { rows: Record<string, unknown>[]; cols: string[] }) {
  const numCols = numericColumns(rows, cols);
  const [labelCol, setLabelCol] = useState(() => cols.find((c) => !numCols.includes(c)) ?? cols[0] ?? '');
  const [valueCol, setValueCol] = useState(numCols[0] ?? '');
  const shown = rows.slice(0, CHART_MAX_BARS);
  return (
    <div className="panel chart-panel">
      <div className="chart-toolbar">
        <span className="hint">
          {`Chart of this page only (${shown.length}${shown.length < rows.length ? ' of ' + rows.length : ''}${shown.length === 1 ? ' row' : ' rows'}) — not the full result.`}
        </span>
        <span className="spacer" />
        <label className="switch" style={{ fontWeight: 400 }}>
          Label
          <select aria-label="Label column" value={labelCol} onChange={(e) => setLabelCol(e.target.value)}>
            {cols.map((c) => (
              <option key={c} value={c}>
                {c}
              </option>
            ))}
          </select>
        </label>
        <label className="switch" style={{ fontWeight: 400 }}>
          Value
          <select aria-label="Value column" value={valueCol} onChange={(e) => setValueCol(e.target.value)}>
            {numCols.map((c) => (
              <option key={c} value={c}>
                {c}
              </option>
            ))}
          </select>
        </label>
      </div>
      <div className="chart-svg-slot">
        <BarChart rows={shown} labelCol={labelCol} valueCol={valueCol} />
      </div>
    </div>
  );
}

function BarChart({
  rows,
  labelCol,
  valueCol,
}: {
  rows: Record<string, unknown>[];
  labelCol: string;
  valueCol: string;
}) {
  const data = rows.map((r) => {
    const v = r[valueCol];
    return {
      label: r[labelCol] === null || r[labelCol] === undefined ? '' : String(r[labelCol]),
      value: typeof v === 'number' && Number.isFinite(v) ? v : 0,
    };
  });
  const width = 680;
  const height = 220;
  const pad = { top: 10, right: 10, bottom: 30, left: 46 };
  const innerW = width - pad.left - pad.right;
  const innerH = height - pad.top - pad.bottom;
  const maxVal = Math.max(...data.map((d) => d.value), 0);
  const minVal = Math.min(...data.map((d) => d.value), 0);
  const range = maxVal - minVal || 1;
  const zeroY = pad.top + innerH * (maxVal / range);
  const gap = 4;
  const barW = data.length ? Math.max(2, (innerW - gap * (data.length - 1)) / data.length) : 0;
  return (
    <svg viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="xMinYMin meet">
      <line x1={pad.left} x2={width - pad.right} y1={zeroY} y2={zeroY} className="chart-axis" />
      {data.map((d, i) => {
        const barH = innerH * (Math.abs(d.value) / range);
        const x = pad.left + i * (barW + gap);
        return (
          <g key={i}>
            <rect
              x={x}
              y={d.value >= 0 ? zeroY - barH : zeroY}
              width={barW}
              height={Math.max(0, barH)}
              className="chart-bar"
            >
              <title>{`${d.label}: ${d.value}`}</title>
            </rect>
            {barW > 16 && (
              <text x={x + barW / 2} y={height - pad.bottom + 13} className="chart-label chart-label-x">
                {d.label.length > 9 ? d.label.slice(0, 8) + '…' : d.label}
              </text>
            )}
          </g>
        );
      })}
      <text x={2} y={pad.top + 8} className="chart-label chart-label-y">
        {String(maxVal)}
      </text>
      <text x={2} y={height - pad.bottom + 3} className="chart-label chart-label-y">
        {String(minVal)}
      </text>
    </svg>
  );
}

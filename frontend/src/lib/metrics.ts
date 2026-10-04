// /metrics (Prometheus text), parsed in the browser as the classic Metrics screen does (ui.py parseMetricsText,
// metricSum, metricGauge, metricGroupSum): this process's own counters and gauges since it started, no history.

export interface Series {
  name: string;
  labels: Record<string, string>;
  value: number;
}

export function parseMetricsText(text: string): Series[] {
  const series: Series[] = [];
  text.split('\n').forEach((line) => {
    if (!line || line.startsWith('#')) return;
    const m = line.match(/^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{([^}]*)\})?\s+([0-9eE+\-.]+)\s*$/);
    if (!m) return;
    const labels: Record<string, string> = {};
    for (const [, k, v] of (m[3] ?? '').matchAll(/([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"/g)) {
      labels[k!] = v!.replace(/\\"/g, '"').replace(/\\\\/g, '\\');
    }
    series.push({ name: m[1]!, labels, value: Number(m[4]) });
  });
  return series;
}

export function metricSum(
  series: Series[],
  name: string,
  filter?: (labels: Record<string, string>) => boolean,
) {
  return series
    .filter((s) => s.name === name && (!filter || filter(s.labels)))
    .reduce((a, s) => a + s.value, 0);
}

export function metricGauge(series: Series[], name: string) {
  return series.find((s) => s.name === name)?.value ?? 0;
}

export function metricGroupSum(series: Series[], name: string, labelKey: string) {
  const totals: Record<string, number> = {};
  series
    .filter((s) => s.name === name)
    .forEach((s) => {
      const key = s.labels[labelKey] || '(none)';
      totals[key] = (totals[key] ?? 0) + s.value;
    });
  return totals;
}

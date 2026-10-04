/** One tile of a .stat-tiles strip (ui.py statTile): a label and a value; `live` marks one polled every 2s. */
export function StatTile({
  label,
  value,
  warn,
  id,
  live,
}: {
  label: string;
  value: string | number;
  warn?: boolean;
  id?: string;
  live?: boolean;
}) {
  return (
    <div className="stat-tile">
      <div className="label">
        {label}
        {live ? <span className="live-dot" title="Updates every 2s" /> : null}
      </div>
      <div className={'value' + (warn ? ' warn' : '')} id={id}>
        {String(value)}
      </div>
    </div>
  );
}

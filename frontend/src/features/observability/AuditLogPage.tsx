import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';

import { api, unwrap, type Schemas } from '@/api/client';
import { Loading, useFeedback } from '@/app/feedback';
import { Time } from '@/components/Time';

// The classic Audit log screen (ui.py #tab-auditlog, loadAuditLog, renderAuditLog, renderAuditChanges), on
// /api/v1/audit: newest first, filtered by action and by actor / target / time, exported as JSON.

type Entry = Schemas['AuditEntry'];

/** create-ish actions read green, delete-ish red, everything else neutral. */
function tone(action: string) {
  if (/^(create|load|save)/.test(action)) return 'ok';
  if (/^(delete|unload|revoke)/.test(action)) return 'bad';
  return '';
}

function valueText(v: unknown) {
  if (v === null || v === undefined || v === '') return 'none';
  // an object (e.g. a queries grant's {name, allow_writes}) as JSON, not "[object Object]"
  const one = (x: unknown) => (x && typeof x === 'object' ? JSON.stringify(x) : String(x));
  if (Array.isArray(v)) return v.length ? v.map(one).join(', ') : 'none';
  return one(v);
}

const isEmpty = (v: unknown) => v === null || v === undefined || v === '' || (Array.isArray(v) && !v.length);

const isDiff = (v: unknown): v is { from?: unknown; to?: unknown } =>
  Boolean(v) && typeof v === 'object' && !Array.isArray(v) && ('from' in v! || 'to' in v!);

/** A change's fields: `field: from → to` for an update, `field: value` for a create or delete snapshot (its empty
 * fields left out). */
function Changes({ changes }: { changes: unknown }) {
  const record = changes && typeof changes === 'object' ? (changes as Record<string, unknown>) : null;
  const keys = record ? Object.keys(record).sort() : [];
  const shown = keys.filter((k) => isDiff(record![k]) || !isEmpty(record![k]));
  if (!shown.length) return <span className="dim">{keys.length ? 'no other fields set' : '—'}</span>;
  return (
    <div>
      {shown.map((k) => {
        const v = record![k];
        return (
          <div key={k} className="mono" style={{ fontSize: 12, color: 'var(--ink-2)' }}>
            {isDiff(v) ? `${k}: ${valueText(v.from)} → ${valueText(v.to)}` : `${k}: ${valueText(v)}`}
          </div>
        );
      })}
    </div>
  );
}

function download(entries: Entry[]) {
  const blob = new Blob([JSON.stringify(entries, null, 2) + '\n'], { type: 'application/json' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'queryapigate-audit-log.json';
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 0);
}

export function AuditLogPage() {
  const { toast } = useFeedback();
  const log = useQuery({
    queryKey: ['audit'],
    queryFn: async () => unwrap(await api.GET('/api/v1/audit')),
    retry: false,
  });
  const [action, setAction] = useState('');
  const [q, setQ] = useState('');
  const all = log.data?.items ?? [];
  const needle = q.trim().toLowerCase();
  const filtered = all.filter(
    (e) =>
      (!action || e.action === action) &&
      (!needle || [e.timestamp, e.actor, e.target].join(' ').toLowerCase().includes(needle)),
  );
  const actions = log.data?.actions ?? [];

  let body: React.ReactNode;
  let count = '';
  if (log.isPending) {
    body = <Loading text="Loading audit log…" />;
  } else if (log.isError) {
    body = (
      <div className="empty">
        <strong>Couldn’t load the audit log</strong>
        <span>{log.error.message}</span>
        <button type="button" className="btn sm" onClick={() => log.refetch()}>
          Retry
        </button>
      </div>
    );
  } else if (!all.length) {
    body = (
      <div className="empty">
        <strong>No administrative changes recorded yet</strong>
        <span>Creating or changing a connection, saved query or API key will appear here.</span>
      </div>
    );
  } else {
    count =
      filtered.length === all.length
        ? all.length + (all.length === 1 ? ' entry' : ' entries')
        : `${filtered.length} of ${all.length} entries`;
    body = !filtered.length ? (
      <div className="empty">
        <span>No entries match this filter.</span>
      </div>
    ) : (
      <div style={{ overflowX: 'auto' }}>
        <table className="grid">
          <thead>
            <tr>
              {['Time', 'Actor', 'Action', 'Target', 'Changes'].map((t) => (
                <th key={t}>{t}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {filtered.map((e, i) => (
              <tr key={i}>
                <td className="mono dim" style={{ whiteSpace: 'nowrap' }}>
                  <Time value={e.timestamp} fallback="" />
                </td>
                <td className="mono">{e.actor || '-'}</td>
                <td>
                  <span className={'tag act ' + tone(e.action)}>{e.action || ''}</span>
                </td>
                <td>
                  <span className="name">{e.target || ''}</span>
                </td>
                <td>
                  <Changes changes={e.changes} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    );
  }

  return (
    <>
      <div className="page-head">
        <div className="titles">
          <h1>Audit log</h1>
          <span className="sub" id="auditlog-sub">
            {'Administrative actions, newest first.' +
              (log.data ? ` Keeps the last ${log.data.retention} entries.` : '')}
          </span>
        </div>
        <span className="spacer" />
        <button id="refresh-auditlog" type="button" className="btn" onClick={() => void log.refetch()}>
          Refresh
        </button>
        <button
          id="export-auditlog"
          type="button"
          className="btn"
          onClick={() => {
            download(filtered);
            toast('Exported ' + filtered.length + (filtered.length === 1 ? ' entry' : ' entries'));
          }}
        >
          Export
        </button>
      </div>
      <div className="panel" style={{ overflow: 'hidden' }}>
        <div className="panel-bar">
          <select
            id="auditlog-action-filter"
            aria-label="Action"
            value={actions.includes(action) ? action : ''}
            onChange={(e) => setAction(e.target.value)}
          >
            <option value="">All actions</option>
            {actions.map((a) => (
              <option key={a} value={a}>
                {a}
              </option>
            ))}
          </select>
          <input
            id="auditlog-filter"
            className="search"
            style={{ width: 260 }}
            type="search"
            placeholder="Filter by actor, target, time…"
            value={q}
            onChange={(e) => setQ(e.target.value)}
          />
          <span className="spacer" />
          <span className="panel-count" id="auditlog-count">
            {count}
          </span>
        </div>
        <div id="auditlog-table">{body}</div>
      </div>
    </>
  );
}

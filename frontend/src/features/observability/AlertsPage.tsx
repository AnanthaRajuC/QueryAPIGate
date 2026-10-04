import { useState } from 'react';
import { useNavigate } from 'react-router';

import { dismissAlert, restoreAlert, useAlertFeed, type Alert } from '@/app/alerts';
import { Loading } from '@/app/feedback';
import { Time } from '@/components/Time';
import { rememberSection } from '@/features/settings/state';

// What needs attention now (GET /api/v1/alerts): expiring keys, failing connections, failing or slow queries, rate
// limits being hit, an open server. Live conditions, checked every minute - each clears by itself once its cause
// does. Dismissing hides one in this browser until it clears (see app/alerts.ts).

const SEVERITIES: { id: Alert['severity']; label: string }[] = [
  { id: 'critical', label: 'Critical' },
  { id: 'warning', label: 'Warning' },
  { id: 'info', label: 'For your information' },
];

const OPEN_LABEL: Record<string, string> = {
  key: 'Open API keys',
  query: 'Open query',
  connection: 'Open connections',
  settings: 'Open settings',
};

function useOpen() {
  const navigate = useNavigate();
  return (target: NonNullable<Alert['target']>) => {
    if (target.type === 'query') navigate(`/queries/${encodeURIComponent(target.name)}`);
    else if (target.type === 'key') navigate('/api-keys');
    else if (target.type === 'connection') navigate('/connections');
    else {
      rememberSection(target.name);
      navigate('/settings');
    }
  };
}

function AlertRow({ alert, dismissed }: { alert: Alert; dismissed?: boolean }) {
  const open = useOpen();
  return (
    <div className={'alert-row ' + alert.severity} data-alert={alert.id}>
      <span className={'sev ' + alert.severity}>{alert.severity}</span>
      <div className="alert-what">
        <strong>{alert.title}</strong>
        <span className="alert-detail">{alert.detail}</span>
        {alert.since && !alert.detail.includes(alert.since) && (
          <span className="alert-since">
            {alert.kind === 'key_expiring' ? 'Expires ' : 'Since '}
            <Time value={alert.since} />
          </span>
        )}
      </div>
      <div className="actions">
        {alert.target && (
          <button type="button" className="btn sm outlined" onClick={() => open(alert.target!)}>
            {OPEN_LABEL[alert.target.type] ?? 'Open'}
          </button>
        )}
        {dismissed ? (
          <button type="button" className="btn ghost sm" onClick={() => restoreAlert(alert.id)}>
            Restore
          </button>
        ) : (
          <button
            type="button"
            className="btn ghost sm"
            title="Hide it in this browser until it clears"
            onClick={() => dismissAlert(alert.id)}
          >
            Dismiss
          </button>
        )}
      </div>
    </div>
  );
}

export function AlertsPage() {
  const { query, active, dismissed } = useAlertFeed();
  const [showDismissed, setShowDismissed] = useState(false);

  let body: React.ReactNode;
  if (query.isPending) {
    body = <Loading text="Checking…" />;
  } else if (query.isError) {
    body = (
      <div className="panel">
        <div className="empty">
          <strong>Couldn’t check for alerts</strong>
          <span>Only the admin key (QUERYAPIGATE_API_KEY) can see alerts.</span>
          <button type="button" className="btn sm" onClick={() => void query.refetch()}>
            Retry
          </button>
        </div>
      </div>
    );
  } else {
    body = (
      <>
        {!active.length ? (
          <div className="panel" id="alerts-clear">
            <div className="empty">
              <strong>All clear</strong>
              <span>Nothing needs your attention right now.</span>
            </div>
          </div>
        ) : (
          SEVERITIES.map(({ id, label }) => {
            const items = active.filter((a) => a.severity === id);
            if (!items.length) return null;
            return (
              <div key={id} className="panel alerts-group" id={'alerts-' + id}>
                <h2>
                  {label} <span className="count">{items.length}</span>
                </h2>
                {items.map((a) => (
                  <AlertRow key={a.id} alert={a} />
                ))}
              </div>
            );
          })
        )}
        {dismissed.length > 0 && (
          <div className="alerts-dismissed">
            <button type="button" className="btn ghost sm" onClick={() => setShowDismissed((s) => !s)}>
              {`${dismissed.length} dismissed in this browser · ${showDismissed ? 'Hide' : 'Show'}`}
            </button>
            {showDismissed && (
              <div className="panel alerts-group" id="alerts-dismissed">
                {dismissed.map((a) => (
                  <AlertRow key={a.id} alert={a} dismissed />
                ))}
              </div>
            )}
          </div>
        )}
      </>
    );
  }

  return (
    <>
      <div className="page-head">
        <div className="titles">
          <h1>Alerts</h1>
          <span className="sub">
            What needs attention now, checked every minute. Each clears by itself once its cause does.
            {query.data ? (
              <>
                {' Last checked '}
                <Time value={query.data.checked_at} />.
              </>
            ) : null}
          </span>
        </div>
        <span className="spacer" />
        <button id="refresh-alerts" type="button" className="btn" onClick={() => void query.refetch()}>
          Check now
        </button>
      </div>
      {body}
    </>
  );
}

import { useState } from 'react';
import { useNavigate, useParams } from 'react-router';

import { ALERT_TABS, dismissAlert, restoreAlert, useAlertFeed, type Alert } from '@/app/alerts';
import { Loading } from '@/app/feedback';
import { Time } from '@/components/Time';
import { rememberSection } from '@/features/settings/state';
import { ExperimentalTag } from '@/components/Experimental';

// What needs attention now (GET /api/v1/alerts): expiring keys, failing connections, failing or slow queries, rate
// limits being hit, an open server. Live conditions, checked every minute - each clears by itself once its cause
// does. Dismissing hides one in this browser until it clears (see app/alerts.ts). All, then a tab per check, each at
// its own address (/alerts/slow-queries) so Home and the bell can open the right one.

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

function Groups({ alerts }: { alerts: Alert[] }) {
  return (
    <>
      {SEVERITIES.map(({ id, label }) => {
        const items = alerts.filter((a) => a.severity === id);
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
      })}
    </>
  );
}

export function AlertsPage() {
  const navigate = useNavigate();
  const params = useParams();
  const tab = ALERT_TABS.find((t) => t.id === params.tab);
  const { query, active: everything, dismissed: allDismissed } = useAlertFeed();
  const [showDismissed, setShowDismissed] = useState(false);
  const inTab = (a: Alert) => !tab || (tab.kinds as string[]).includes(a.kind);
  const active = everything.filter(inTab);
  const dismissed = allDismissed.filter(inTab);
  const of = (kinds?: string[]) => everything.filter((a) => !kinds || kinds.includes(a.kind));

  let body: React.ReactNode;
  if (query.isPending) {
    body = <Loading text="Checking…" />;
  } else if (query.isError) {
    body = (
      <div className="panel">
        <div className="empty">
          <strong>Couldn’t check for alerts</strong>
          <span>{query.error.message}</span>
          <button type="button" className="btn sm" onClick={() => void query.refetch()}>
            Retry
          </button>
        </div>
      </div>
    );
  } else {
    body = (
      <>
        {tab && <p className="alerts-about">{tab.about}</p>}
        {!active.length ? (
          <div className="panel" id="alerts-clear">
            <div className="empty">
              <strong>All clear</strong>
              <span>
                {tab
                  ? `Nothing to report from this check right now.`
                  : 'Nothing needs your attention right now.'}
              </span>
            </div>
          </div>
        ) : (
          <Groups alerts={active} />
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

  const tabButton = (id: string, label: string, alerts: Alert[]) => (
    <button
      key={id}
      type="button"
      role="tab"
      aria-selected={(tab?.id ?? 'all') === id}
      className={'minitab' + ((tab?.id ?? 'all') === id ? ' active' : '')}
      onClick={() => navigate(id === 'all' ? '/alerts' : '/alerts/' + id)}
    >
      {label}
      {query.data ? (
        <span
          className={
            'n' +
            (alerts.length ? ' on' : '') +
            (alerts.some((a) => a.severity === 'critical') ? ' critical' : '')
          }
        >
          {alerts.length}
        </span>
      ) : null}
    </button>
  );

  return (
    <>
      <div className="page-head">
        <div className="titles">
          <h1>
            Alerts <ExperimentalTag />
          </h1>
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
      <div className="minitabs alerts-tabs" role="tablist" aria-label="Checks">
        {tabButton('all', 'All', of())}
        {ALERT_TABS.map((t) => tabButton(t.id, t.label, of(t.kinds)))}
      </div>
      {body}
    </>
  );
}

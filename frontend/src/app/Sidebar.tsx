import { useQueryClient } from '@tanstack/react-query';
import { useState, type FormEvent } from 'react';
import { useLocation, useNavigate } from 'react-router';

import { getApiKey, setApiKey } from '@/auth/apiKey';

import { useAllQueries, useApiKeys, useConnections, useHealth, useRoles } from './data';
import { FOOT_ITEMS, NAV_GROUPS, type NavItem } from './navigation';
import { useAlertFeed } from './alerts';

// ui.py <aside class="side">, element for element.

export function activeItem(pathname: string): NavItem | undefined {
  return [...NAV_GROUPS.flatMap((g) => g.items), ...FOOT_ITEMS].find((i) =>
    i.path === '/' ? pathname === '/' : pathname.startsWith(i.path),
  );
}

function useCounts() {
  const connections = useConnections();
  const queries = useAllQueries();
  const keys = useApiKeys();
  const roles = useRoles();
  const alerts = useAlertFeed();
  const n = (value: number | undefined) => (value ? String(value) : '');
  return {
    connections: n(connections.data?.length),
    queries: n(queries.data?.length),
    apikeys: n(keys.data ? Object.keys(keys.data).length : undefined),
    roles: n(roles.data ? Object.keys(roles.data).length : undefined),
    alerts: n(alerts.urgent),
  };
}

export function Sidebar() {
  const navigate = useNavigate();
  const { pathname } = useLocation();
  const active = activeItem(pathname);
  const counts = useCounts();
  const health = useHealth();

  const go = (item: NavItem) => navigate(item.path);
  const button = (item: NavItem, extraClass?: string) => (
    <button
      key={item.tab}
      type="button"
      role="tab"
      data-tab={item.tab}
      data-group={item.group}
      data-label={item.label}
      title={item.label}
      className={
        [extraClass, active?.tab === item.tab ? 'active' : ''].filter(Boolean).join(' ') || undefined
      }
      aria-selected={active?.tab === item.tab}
      onClick={() => go(item)}
    >
      <span className="nav-abbr">{item.icon}</span>
      <span className="nav-text">{item.label}</span>
      {item.count ? <span className="count">{counts[item.count]}</span> : null}
    </button>
  );

  return (
    <aside className="side" id="side">
      <div className="side-head">
        <span className="wordmark">
          Query<b>API</b>Gate
        </span>
        <span className="wordmark-short" title="QueryAPIGate">
          Q<b>A</b>
          <i />
        </span>
        <span className="health" id="health" title="GET /health">
          <span className={`dot ${health.isError ? 'bad' : health.isSuccess ? 'ok' : ''}`} id="health-dot" />
          <span id="version">{health.data ? `v${health.data.version}` : '…'}</span>
        </span>
      </div>
      <nav id="tabs" role="tablist" aria-label="Sections">
        {NAV_GROUPS.map((group) => (
          <div className="nav-group" key={group.label}>
            <div className="nav-label">{group.label}</div>
            <div className="nav-rule" />
            {group.items.map((item) => button(item))}
          </div>
        ))}
      </nav>
      <StarCard />
      <div className="side-foot">
        {FOOT_ITEMS.map((item) => button(item, 'nav'))}
        <KeyDotNarrow />
        {/* A new tab, so the Console stays open. rel="opener" because target="_blank" is noopener by default, and a
            noopener tab starts with empty sessionStorage: /docs would lose this tab's API key. Same origin, so safe. */}
        <a className="side-link" href="/docs" target="_blank" rel="opener">
          API docs<span>/docs</span>
        </a>
        <a className="side-link" href="/openapi.json" target="_blank" rel="opener">
          OpenAPI<span>.json</span>
        </a>
        <KeyPanel />
      </div>
    </aside>
  );
}

function KeyDotNarrow() {
  return (
    <div className="key-dot-only" title="API key applied to this tab">
      <span className={`dot ${getApiKey() ? 'ok' : 'off'}`} id="key-dot-narrow" />
    </div>
  );
}

/** The API key for this tab - the same sessionStorage entry /ui and /docs use (ui.py #key-panel). */
function KeyPanel() {
  const queryClient = useQueryClient();
  const [key, setKey] = useState(getApiKey);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState('');
  const showView = Boolean(key) && !editing;

  function submit(event: FormEvent) {
    event.preventDefault();
    const value = draft.trim();
    setApiKey(value);
    setKey(value);
    setDraft('');
    setEditing(false);
    queryClient.invalidateQueries();
  }

  return (
    <div id="key-panel">
      <div className="kp-state">
        <span className={`dot ${key ? 'ok' : 'off'}`} id="key-dot" />
        <span id="key-state">{key ? 'API key applied' : 'No API key'}</span>
        <span className="scope">this tab</span>
      </div>
      <div className="kp-row" id="key-view" hidden={!showView}>
        <span className="kp-mask" id="key-mask">
          {'••••••••' + (key.length >= 16 ? key.slice(-4) : '')}
        </span>
        <button type="button" id="key-change" onClick={() => setEditing(true)}>
          Change
        </button>
      </div>
      <form id="key-bar" autoComplete="off" hidden={showView} onSubmit={submit}>
        <input
          id="key"
          type="password"
          autoComplete="off"
          placeholder="X-API-Key"
          aria-label="API key"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
        />
        <button id="save-key" type="submit">
          Apply
        </button>
      </form>
    </div>
  );
}

const STAR_DISMISS_KEY = 'queryapigate-ui-star-cta-dismissed';

function StarCard() {
  const [hidden, setHidden] = useState(() => {
    try {
      return localStorage.getItem(STAR_DISMISS_KEY) === '1';
    } catch {
      return false;
    }
  });
  if (hidden) return null;
  return (
    <div className="star-cta" id="star-cta">
      <button
        type="button"
        className="star-cta-close"
        id="star-cta-close"
        aria-label="Dismiss"
        onClick={() => {
          setHidden(true);
          try {
            localStorage.setItem(STAR_DISMISS_KEY, '1');
          } catch {
            // remembered for this page only
          }
        }}
      >
        ×
      </button>
      <div className="star-cta-title">Star QueryAPIGate</div>
      <div className="star-cta-desc">See the latest releases and help grow the community on GitHub.</div>
      <a
        className="star-cta-btn"
        href="https://github.com/AnanthaRajuC/QueryAPIGate"
        target="_blank"
        rel="noopener"
      >
        <img
          src="https://img.shields.io/github/stars/AnanthaRajuC/QueryAPIGate?style=flat-square&logo=github&label=Stars&labelColor=181717&color=2ea44f"
          height="20"
          alt="GitHub stars"
          loading="lazy"
        />
      </a>
    </div>
  );
}

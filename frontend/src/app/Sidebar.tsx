import { useQueryClient } from '@tanstack/react-query';
import { useEffect, useRef, useState, type FormEvent, type ReactNode } from 'react';
import { useLocation, useNavigate } from 'react-router';

import { getApiKey, setApiKey } from '@/auth/apiKey';
import { signInUrl, signOut, useSession } from '@/auth/session';

import { useCan, useHealth, useMe } from './data';
import { AttentionBadge } from './navCounts';
import {
  ATTENTION,
  FOOT_ITEMS,
  NAV_GROUPS,
  type CountKind,
  type NavGroup,
  type NavItem,
  type NavSection,
} from './navigation';
import { WorkspaceSwitcher } from './WorkspaceSwitcher';

// ui.py <aside class="side">, element for element - but one entry per group: a group of several screens is a section
// (navigation.tsx), whose page has them as tabs (SectionLayout); a group of one is that screen. A section holding what
// asks for someone's action (open alerts, writes waiting for approval) carries its badge. The menu fades out at the
// bottom while more of it is below.

export function activeItem(pathname: string): NavItem | undefined {
  return [...NAV_GROUPS.flatMap((g) => g.items), ...FOOT_ITEMS].find((i) =>
    i.path === '/' ? pathname === '/' : pathname.startsWith(i.path),
  );
}

export function Sidebar() {
  const navigate = useNavigate();
  const { pathname } = useLocation();
  const active = activeItem(pathname);
  const health = useHealth();
  const can = useCan();
  const shown = (item: NavItem) => !item.needs || can(item.needs);

  // The fade at the bottom of the menu: on while some of it is below the visible part.
  const navRef = useRef<HTMLElement>(null);
  const [moreBelow, setMoreBelow] = useState(false);
  useEffect(() => {
    const nav = navRef.current;
    if (!nav) return;
    const update = () => setMoreBelow(nav.scrollTop + nav.clientHeight < nav.scrollHeight - 4);
    update();
    nav.addEventListener('scroll', update, { passive: true });
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(update);
    observer?.observe(nav);
    for (const child of nav.children) observer?.observe(child);
    return () => {
      nav.removeEventListener('scroll', update);
      observer?.disconnect();
    };
  }, []);

  const entry = (
    key: string,
    label: string,
    icon: ReactNode,
    isActive: boolean,
    onClick: () => void,
    badge?: ReactNode,
    extraClass?: string,
  ) => (
    <button
      key={key}
      type="button"
      role="tab"
      data-tab={key}
      data-label={label}
      title={label}
      className={[extraClass, isActive ? 'active' : ''].filter(Boolean).join(' ') || undefined}
      aria-selected={isActive}
      onClick={onClick}
    >
      <span className="nav-abbr">{icon}</span>
      <span className="nav-text">{label}</span>
      {badge}
    </button>
  );
  const button = (item: NavItem, extraClass?: string) =>
    entry(
      item.tab,
      item.label,
      item.icon,
      active?.tab === item.tab,
      () => navigate(item.path),
      null,
      extraClass,
    );
  // A section: it opens the section's page, and is the active entry while any of its screens is open.
  const sectionButton = (group: NavGroup, section: NavSection, items: NavItem[]) => {
    const asking = items.map((i) => i.count).filter((k): k is CountKind => Boolean(k && ATTENTION[k]));
    return entry(
      section.id,
      section.label,
      section.icon,
      active?.group === group.label,
      () => navigate(section.path),
      asking.length ? <AttentionBadge kinds={asking} /> : null,
    );
  };

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
      <WorkspaceSwitcher />
      <nav
        id="tabs"
        role="tablist"
        aria-label="Sections"
        ref={navRef}
        className={moreBelow ? 'more-below' : undefined}
      >
        {NAV_GROUPS.map((group) => {
          const items = group.items.filter(shown);
          if (!items.length) return null;
          return group.section
            ? sectionButton(group, group.section, items)
            : items.map((item) => button(item));
        })}
      </nav>
      <StarCard />
      <div className="side-foot">
        {FOOT_ITEMS.filter(shown).map((item) => button(item, 'nav'))}
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
const ROLE_LABEL: Record<string, string> = {
  owner: 'Owner',
  admin: 'Admin',
  developer: 'Developer',
  auditor: 'Auditor',
};

/** Who the key in this tab signs in as: an administrator by name, the shared break-glass key, or an open server. */
function identity(me: ReturnType<typeof useMe>['data'], key: string) {
  if (!me) return key ? 'API key applied' : 'No API key';
  // no role here: only in workspaces (ADR 0023)
  const role = me.role === null ? 'Workspaces only' : (ROLE_LABEL[me.role] ?? me.role);
  if (me.via === 'token') return `Signed in as ${me.name} · ${role}`;
  if (me.via === 'sso') return `Signed in as ${me.name} · ${role} via SSO`;
  if (me.via === 'break-glass') return 'Shared admin key (owner)';
  return 'No API key needed';
}

function KeyPanel() {
  const queryClient = useQueryClient();
  const me = useMe();
  const session = useSession();
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
    // Who this key is, asked afresh - its readers see it emptied at once and refetched - and only then everything
    // else, so nothing is fetched (or shown) on the previous key's role.
    void queryClient.resetQueries({ queryKey: ['me'] }).finally(() => queryClient.invalidateQueries());
  }

  const sso = session.data?.sso ?? false;
  const viaSso = !key && Boolean(session.data?.signed_in);

  /** Forget this tab's key: a server that needs one shows its sign-in page (AppShell), an open one carries on. */
  function forget() {
    setApiKey('');
    setKey('');
    setEditing(false);
    void queryClient.resetQueries();
  }

  async function leave() {
    const providerSignOut = await signOut();
    // Out of the provider too, where it has a sign-out page; otherwise just back to a signed-out Console.
    if (providerSignOut) globalThis.location.assign(providerSignOut);
    else globalThis.location.reload();
  }

  return (
    <div id="key-panel">
      <div className="kp-state">
        <span className={`dot ${key || viaSso ? 'ok' : 'off'}`} id="key-dot" />
        <span id="key-state">{identity(me.data, key)}</span>
        <span className="scope">this tab</span>
      </div>
      {sso && viaSso && (
        <div className="kp-row">
          <span className="kp-mask">{session.data?.email ?? session.data?.admin}</span>
          <button type="button" id="sso-sign-out" onClick={() => void leave()}>
            Sign out
          </button>
        </div>
      )}
      {sso && !viaSso && !key && (
        <div className="kp-row">
          <a
            className="btn primary sm"
            id="sso-sign-in"
            href={signInUrl()}
            onClick={() => setApiKey('')} // the session, not a key, from here on
          >
            Sign in with SSO
          </a>
          <span className="dim kp-or">or a token below</span>
        </div>
      )}
      <div className="kp-row" id="key-view" hidden={!showView}>
        <span className="kp-mask" id="key-mask">
          {'••••••••' + (key.length >= 16 ? key.slice(-4) : '')}
        </span>
        <button type="button" id="key-change" onClick={() => setEditing(true)}>
          Change
        </button>
        <button type="button" id="key-sign-out" onClick={forget}>
          Sign out
        </button>
      </div>
      <form id="key-bar" autoComplete="off" hidden={showView || viaSso} onSubmit={submit}>
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

import { useQueryClient } from '@tanstack/react-query';
import { useCallback, useEffect, useRef, useState, type FormEvent, type ReactNode } from 'react';
import { useLocation, useNavigate } from 'react-router';

import { getApiKey, setApiKey } from '@/auth/apiKey';
import { signInUrl, signOut, useSession } from '@/auth/session';
import { ChevronRightIcon, ChevronsUpDownIcon, KeyIcon, SignInIcon, SignOutIcon } from '@/components/icons';
import { MenuButton, type MenuEntry } from '@/components/Menu';
import { useDismiss } from '@/components/useDismiss';

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
    hint?: string,
  ) => (
    <button
      key={key}
      type="button"
      role="tab"
      data-tab={key}
      data-label={label}
      title={hint ? `${label}: ${hint}` : label}
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
      undefined,
      section.description,
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
        {FOOT_ITEMS.filter((item) => item.tab !== 'help' && shown(item)).map((item) => button(item, 'nav'))}
        <HelpMenu active={active?.tab === 'help'} />
        <AccountMenu />
      </div>
    </aside>
  );
}

/** Help and docs: one entry that opens a menu over it - the Help screen, and the API's docs and OpenAPI description in
 * a new tab, so the Console stays open (with this tab's API key: MenuEntry's newTab). In the icon rail the menu opens
 * beside the icon. */
function HelpMenu({ active }: { active: boolean }) {
  const navigate = useNavigate();
  const help = FOOT_ITEMS.find((item) => item.tab === 'help')!;
  return (
    <MenuButton
      label="Help and docs"
      placement="above"
      className={active ? 'nav help-menu active' : 'nav help-menu'}
      entries={[
        { label: 'Help', onSelect: () => navigate(help.path) },
        { label: 'API docs', hint: '/docs', href: '/docs', newTab: true },
        { label: 'OpenAPI', hint: '.json', href: '/openapi.json', newTab: true },
      ]}
    >
      <span className="nav-abbr">{help.icon}</span>
      <span className="nav-text">Help and docs</span>
      <span className="nav-chevron">
        <ChevronRightIcon />
      </span>
    </MenuButton>
  );
}

const ROLE_LABEL: Record<string, string> = {
  owner: 'Owner',
  admin: 'Admin',
  developer: 'Developer',
  auditor: 'Auditor',
};

/** Who this tab is, as the account button says it: a name, and a line under it. */
function who(me: ReturnType<typeof useMe>['data'], key: string): { name: string; detail: string } {
  if (!me) return { name: key ? 'API key applied' : 'No API key', detail: 'this tab' };
  // no role here: only in workspaces (ADR 0023)
  const role = me.role === null ? 'Workspaces only' : (ROLE_LABEL[me.role] ?? me.role);
  if (me.via === 'token') return { name: me.name ?? 'Admin token', detail: `${role} · this tab` };
  if (me.via === 'sso') return { name: me.name ?? 'Signed in', detail: `${role} via SSO` };
  if (me.via === 'break-glass') return { name: 'Shared admin key', detail: 'Owner · this tab' };
  return { name: 'No API key needed', detail: 'an open server' };
}

/** The account, at the foot of the sidebar: who this tab is, as a button that opens a menu over it - Change key and
 * Sign out, or Sign in with SSO. The key is this tab's: the same sessionStorage entry /docs uses. Change key opens a
 * small form where the menu was; in the icon rail the button is the dot alone, and both open beside it. */
function AccountMenu() {
  const queryClient = useQueryClient();
  const me = useMe();
  const session = useSession();
  const [key, setKey] = useState(getApiKey);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState('');
  const wrap = useRef<HTMLDivElement>(null);
  const form = useRef<HTMLFormElement>(null);
  const closeForm = useCallback((refocus: boolean) => {
    setEditing(false);
    setDraft('');
    if (refocus) wrap.current?.querySelector<HTMLElement>('.account')?.focus();
  }, []);
  useDismiss(editing, [form], closeForm);
  useEffect(() => {
    if (editing) form.current?.querySelector('input')?.focus();
  }, [editing]);

  function submit(event: FormEvent) {
    event.preventDefault();
    const value = draft.trim();
    setApiKey(value);
    setKey(value);
    closeForm(true);
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

  const { name, detail } = who(me.data, key);
  const email = viaSso ? (session.data?.email ?? session.data?.admin) : undefined;
  const entries: MenuEntry[] = viaSso
    ? [{ label: 'Sign out', icon: <SignOutIcon />, id: 'sso-sign-out', onSelect: () => void leave() }]
    : key
      ? [
          { label: 'Change key', icon: <KeyIcon />, id: 'key-change', onSelect: () => setEditing(true) },
          { label: 'Sign out', icon: <SignOutIcon />, id: 'key-sign-out', onSelect: forget },
        ]
      : [
          {
            label: 'Sign in with SSO',
            icon: <SignInIcon />,
            id: 'sso-sign-in',
            href: signInUrl(),
            hidden: !sso,
            onSelect: () => setApiKey(''), // the session, not a key, from here on
          },
          { label: 'Use an API key', icon: <KeyIcon />, id: 'key-change', onSelect: () => setEditing(true) },
        ];

  return (
    <div id="key-panel" className="account-wrap" ref={wrap}>
      <MenuButton
        label={`Account: ${name}, ${detail}`}
        placement="above"
        className="account"
        header={
          <div className="account-head">
            <div>
              <div className="account-name">{name}</div>
              <div className="account-detail">{detail}</div>
              {email && email !== name && <div className="account-detail">{email}</div>}
            </div>
            {key && (
              <span
                className="account-mask"
                id="key-mask"
                title="This tab's API key, its last four characters"
              >
                {'••••' + (key.length >= 16 ? key.slice(-4) : '')}
              </span>
            )}
          </div>
        }
        entries={entries}
      >
        <span className={`dot ${key || viaSso ? 'ok' : 'off'}`} id="key-dot" />
        <span className="account-text">
          <span className="account-name" id="key-state">
            {name}
          </span>
          <span className="account-detail">{detail}</span>
        </span>
        <span className="account-chevron">
          <ChevronsUpDownIcon />
        </span>
      </MenuButton>
      {editing && (
        <form
          ref={form}
          id="key-bar"
          className="popover above"
          role="dialog"
          aria-label="API key for this tab"
          autoComplete="off"
          onSubmit={submit}
        >
          <label className="account-form-label" htmlFor="key">
            Admin token or API key
          </label>
          <input
            id="key"
            type="password"
            autoComplete="off"
            placeholder="X-API-Key"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
          />
          <div className="account-form-actions">
            <button type="button" className="btn sm" onClick={() => closeForm(true)}>
              Cancel
            </button>
            <button id="save-key" className="btn primary sm" type="submit" disabled={!draft.trim()}>
              Apply
            </button>
          </div>
        </form>
      )}
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

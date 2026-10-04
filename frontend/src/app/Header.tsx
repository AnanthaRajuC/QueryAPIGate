import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useLocation, useNavigate } from 'react-router';

import { onRateLimit, type RateLimit } from '@/api/client';

import { useAlertFeed } from './alerts';
import { useAllQueries, useApiKeys, useConnections, useRoles } from './data';
import { setPref, useIsDark, usePrefValues } from './prefs';
import { activeItem } from './Sidebar';

// ui.py <header class="top">: sidebar toggle, breadcrumbs, global search (Ctrl K), the rate-limit chip, and the font
// size and light/dark switches.

const COLLAPSE_KEY = 'queryapigate-ui-side-collapsed';

function readCollapsed() {
  try {
    return localStorage.getItem(COLLAPSE_KEY) === '1';
  } catch {
    return false;
  }
}

export function Header() {
  const { pathname } = useLocation();
  const active = activeItem(pathname);
  const [saved, setSaved] = useState(readCollapsed); // the sidebar as the viewer left it, remembered per browser
  // Help collapses the sidebar to give the docs room, each time it's opened, without changing the saved choice:
  // toggling there lasts until you leave, and other screens keep the sidebar as it was.
  const onHelp = pathname.startsWith('/help');
  const [helpCollapsed, setHelpCollapsed] = useState(true);
  const [wasOnHelp, setWasOnHelp] = useState(onHelp);
  if (onHelp !== wasOnHelp) {
    setWasOnHelp(onHelp);
    if (onHelp) setHelpCollapsed(true);
  }
  const collapsed = onHelp ? helpCollapsed : saved;
  const toggle = useCallback(() => (onHelp ? setHelpCollapsed((c) => !c) : setSaved((c) => !c)), [onHelp]);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [rate, setRate] = useState<RateLimit | null>(null);

  useEffect(() => onRateLimit(setRate), []);
  useEffect(() => {
    document.body.classList.toggle('side-collapsed', collapsed);
  }, [collapsed]);
  useEffect(() => {
    try {
      localStorage.setItem(COLLAPSE_KEY, saved ? '1' : '0');
    } catch {
      // remembered for this page only
    }
  }, [saved]);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (!(e.ctrlKey || e.metaKey) || e.shiftKey || e.altKey) return;
      const k = e.key.toLowerCase();
      if (k === 'b') {
        e.preventDefault();
        toggle();
      } else if (k === 'k') {
        e.preventDefault();
        setPaletteOpen((o) => !o);
      }
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [toggle]);

  const low = rate ? rate.remaining <= Math.max(1, rate.limit * 0.1) : false;
  return (
    <>
      <header className="top">
        <button
          type="button"
          id="side-toggle"
          title={collapsed ? 'Expand sidebar (Ctrl B)' : 'Collapse sidebar (Ctrl B)'}
          aria-label={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
          aria-expanded={!collapsed}
          onClick={toggle}
        >
          <span>
            <i />
          </span>
        </button>
        <div className="crumbs" id="crumbs">
          <span className="g" id="crumb-group">
            {active?.group ?? 'Overview'}
          </span>
          <span className="sl">/</span>
          <b id="crumb-page">{active?.label ?? 'Home'}</b>
        </div>
        <div className="search-wrap">
          <button
            type="button"
            id="global-search"
            aria-label="Search queries, connections, keys"
            onClick={() => setPaletteOpen(true)}
          >
            <span className="ph">Search queries, connections, keys…</span>
            <kbd>Ctrl K</kbd>
          </button>
        </div>
        <span
          className={low ? 'chip low' : 'chip'}
          id="rate"
          hidden={!rate}
          title="X-RateLimit-Remaining / X-RateLimit-Limit"
        >
          {rate ? `rate ${rate.remaining}/${rate.limit}` : ''}
        </span>
        <div className="top-tools">
          <AlertsBell />
          <FontSizeSwitch />
          <ThemeToggle />
        </div>
      </header>
      {paletteOpen && <Palette onClose={() => setPaletteOpen(false)} />}
    </>
  );
}

/** The alerts bell: how many critical and warning alerts need attention, red while any is critical. Shown only to
 *  callers who can read alerts (the admin key). */
function AlertsBell() {
  const navigate = useNavigate();
  const { query, urgent, critical } = useAlertFeed();
  if (!query.isSuccess) return null;
  const label = urgent ? `Alerts: ${urgent} need${urgent === 1 ? 's' : ''} attention` : 'Alerts: all clear';
  return (
    <button
      type="button"
      id="alerts-bell"
      title={label}
      aria-label={label}
      onClick={() => navigate('/alerts')}
    >
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9" />
        <path d="M10.3 21a1.94 1.94 0 0 0 3.4 0" />
      </svg>
      {urgent > 0 && (
        <span className={'badge' + (critical ? ' critical' : '')}>{urgent > 99 ? '99+' : urgent}</span>
      )}
    </button>
  );
}

const FONT_SIZES = ['Small', 'Medium', 'Large'] as const;

/** Small / Medium / Large as three A's of growing size - the same preference as Settings > Appearance > Font size. */
function FontSizeSwitch() {
  const { fontSize } = usePrefValues();
  return (
    <div className="seg" id="font-size" role="group" aria-label="Font size">
      {FONT_SIZES.map((size) => (
        <button
          key={size}
          type="button"
          className={fontSize === size ? 'on' : ''}
          aria-pressed={fontSize === size}
          aria-label={`${size} text`}
          title={`${size} text`}
          onClick={() => setPref('fontSize', size)}
        >
          A
        </button>
      ))}
    </div>
  );
}

/** Flips between light and dark from whatever is showing - the same preference as Settings > Appearance > Theme,
 *  which is where to go back to following the operating system. */
function ThemeToggle() {
  const dark = useIsDark();
  const label = dark ? 'Switch to light mode' : 'Switch to dark mode';
  return (
    <button
      type="button"
      id="theme-toggle"
      title={label}
      aria-label={label}
      onClick={() => setPref('theme', dark ? 'Light' : 'Dark')}
    >
      {dark ? (
        <svg viewBox="0 0 24 24" aria-hidden="true">
          <circle cx="12" cy="12" r="4" />
          <path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41" />
        </svg>
      ) : (
        <svg viewBox="0 0 24 24" aria-hidden="true">
          <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />
        </svg>
      )}
    </button>
  );
}

interface Entry {
  group: string;
  name: string;
  desc: string;
  extra?: string;
  go: () => void;
}

/** Ctrl K: jump to a connection, saved query, API key or role (ui.py #palette). */
function Palette({ onClose }: { onClose: () => void }) {
  const navigate = useNavigate();
  const connections = useConnections();
  const queries = useAllQueries();
  const keys = useApiKeys();
  const roles = useRoles();
  const [q, setQ] = useState('');
  const [sel, setSel] = useState(0);
  const input = useRef<HTMLInputElement>(null);
  useEffect(() => input.current?.focus(), []);

  const entries = useMemo<Entry[]>(() => {
    const out: Entry[] = [];
    (connections.data ?? []).forEach((c) =>
      out.push({ group: 'Connections', name: c.name, desc: c.db, go: () => navigate('/connections') }),
    );
    (queries.data ?? []).forEach((s) =>
      out.push({
        group: 'API Repository',
        name: s.name,
        desc: s.description,
        extra: [s.collection ?? '', s.tags.join(' ')].join(' '),
        go: () => navigate(`/queries/${encodeURIComponent(s.name)}`),
      }),
    );
    Object.keys(keys.data ?? {})
      .sort()
      .forEach((n) =>
        out.push({
          group: 'API keys',
          name: n,
          desc: String(keys.data?.[n]?.rate_limit ?? ''),
          go: () => navigate('/api-keys'),
        }),
      );
    Object.keys(roles.data ?? {})
      .sort()
      .forEach((n) => out.push({ group: 'Roles', name: n, desc: '', go: () => navigate('/roles') }));
    return out;
  }, [connections.data, queries.data, keys.data, roles.data, navigate]);

  const needle = q.trim().toLowerCase();
  const items = entries
    .filter(
      (e) => !needle || [e.name, e.desc, e.extra ?? '', e.group].join(' ').toLowerCase().includes(needle),
    )
    .slice(0, 40);
  const current = Math.min(sel, Math.max(0, items.length - 1));
  const pick = (i: number) => {
    const entry = items[i];
    if (!entry) return;
    onClose();
    entry.go();
  };

  let lastGroup: string | null = null;
  return (
    <div
      id="palette"
      role="dialog"
      aria-label="Search"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="pal-box">
        <input
          ref={input}
          id="pal-input"
          type="search"
          autoComplete="off"
          spellCheck={false}
          placeholder="Search queries, connections, keys…"
          aria-label="Search"
          value={q}
          onChange={(e) => {
            setQ(e.target.value);
            setSel(0);
          }}
          onKeyDown={(e) => {
            if (e.key === 'ArrowDown') {
              e.preventDefault();
              setSel(Math.min(items.length - 1, current + 1));
            } else if (e.key === 'ArrowUp') {
              e.preventDefault();
              setSel(Math.max(0, current - 1));
            } else if (e.key === 'Enter') {
              e.preventDefault();
              pick(current);
            } else if (e.key === 'Escape') {
              e.preventDefault();
              onClose();
            }
          }}
        />
        <div className="pal-list" id="pal-list">
          {items.length === 0 ? (
            <div className="pal-empty">
              {needle ? `Nothing matches “${needle}”.` : 'Nothing to search yet.'}
            </div>
          ) : (
            items.map((e, i) => {
              const heading = e.group !== lastGroup ? <div className="pal-group">{e.group}</div> : null;
              lastGroup = e.group;
              return (
                <div key={`${e.group}:${e.name}`} style={{ display: 'contents' }}>
                  {heading}
                  <button
                    type="button"
                    className={i === current ? 'pal-item on' : 'pal-item'}
                    onClick={() => pick(i)}
                    onMouseMove={() => current !== i && setSel(i)}
                  >
                    <span className="nm">{e.name}</span>
                    <span className="ds">{e.desc}</span>
                  </button>
                </div>
              );
            })
          )}
        </div>
      </div>
    </div>
  );
}

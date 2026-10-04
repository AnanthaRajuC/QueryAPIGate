import { useEffect, useMemo, useRef, useState } from 'react';
import { useLocation, useNavigate } from 'react-router';

import { onRateLimit, type RateLimit } from '@/api/client';

import { useAllQueries, useApiKeys, useConnections, useRoles } from './data';
import { setPref, useIsDark } from './prefs';
import { activeItem } from './Sidebar';

// ui.py <header class="top">: sidebar toggle, breadcrumbs, global search (Ctrl K), the rate-limit chip and the
// light/dark switch.

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
  const [collapsed, setCollapsed] = useState(readCollapsed);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [rate, setRate] = useState<RateLimit | null>(null);

  useEffect(() => onRateLimit(setRate), []);
  useEffect(() => {
    document.body.classList.toggle('side-collapsed', collapsed);
    try {
      localStorage.setItem(COLLAPSE_KEY, collapsed ? '1' : '0');
    } catch {
      // remembered for this page only
    }
  }, [collapsed]);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (!(e.ctrlKey || e.metaKey) || e.shiftKey || e.altKey) return;
      const k = e.key.toLowerCase();
      if (k === 'b') {
        e.preventDefault();
        setCollapsed((c) => !c);
      } else if (k === 'k') {
        e.preventDefault();
        setPaletteOpen((o) => !o);
      }
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, []);

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
          onClick={() => setCollapsed((c) => !c)}
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
        <ThemeToggle />
      </header>
      {paletteOpen && <Palette onClose={() => setPaletteOpen(false)} />}
    </>
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

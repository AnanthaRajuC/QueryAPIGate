import { useEffect, useRef, useState, type ReactNode } from 'react';
import { useNavigate } from 'react-router';

import { ApiError } from '@/api/client';
import { useApiKeys, useRoles } from '@/app/data';
import { Empty, Loading, useFeedback } from '@/app/feedback';

import {
  useQueryActions,
  useQueryDetail,
  useVersionHistory,
  type HistoryEntry,
  type Query,
  type QueryVersion,
} from './api';
import { MoveForm, QueryForm } from './forms';
import { queryReach } from './reach';
import {
  AccessTab,
  CacheTab,
  CliTab,
  CurlTab,
  HistoryTab,
  KeysTab,
  MetricsTab,
  RolesTab,
  RunTab,
  SqlTab,
  StatTiles,
} from './tabs';

// The classic saved-query detail panel (ui.py renderDetail): title row with the version switcher and actions,
// description, meta, stat tiles, requests-per-day chart and the subtabs. Publishing (drafts, ADR 0001) is shown
// with the classic pill and buttons: Published / Draft / Previous, and Publish, Roll back or Unpublish.

type TabId = 'run' | 'sql' | 'history' | 'curl' | 'keys' | 'roles' | 'access' | 'cache' | 'metrics' | 'cli';

const STATUS_LABEL: Record<QueryVersion['status'], string> = {
  published: 'Published',
  draft: 'Draft',
  previous: 'Previous',
};

export function QueryDetail({ name }: { name: string | undefined }) {
  const detail = useQueryDetail(name);
  if (!name) {
    return (
      <div id="query-detail" className="panel detail">
        <Empty title="No query selected">
          Pick a saved query to run it, read its SQL or see its history.
        </Empty>
      </div>
    );
  }
  if (detail.isPending) {
    return (
      <div id="query-detail" className="panel detail">
        <Loading />
      </div>
    );
  }
  if (detail.isError) {
    const missing = detail.error instanceof ApiError && detail.error.code === 'query_not_found';
    return (
      <div id="query-detail" className="panel detail">
        <Empty title={missing ? 'No such query' : 'Couldn’t load this query'}>
          {missing ? `There is no saved query named “${name}”.` : detail.error.message}
        </Empty>
      </div>
    );
  }
  return <Detail key={name} query={detail.data.data} etag={detail.data.etag} />;
}

function Detail({ query, etag }: { query: Query; etag: string | null }) {
  const latest = query.versions[query.versions.length - 1]!;
  const [versionNumber, setVersionNumber] = useState(latest.version);
  const [tab, setTab] = useState<TabId>('run');
  // A new version saved from the drawer becomes the latest: follow it, as the classic view does.
  const [seenLatest, setSeenLatest] = useState(latest.version);
  if (seenLatest !== latest.version) {
    setSeenLatest(latest.version);
    setVersionNumber(latest.version);
  }
  const v = query.versions.find((x) => x.version === versionNumber) ?? latest;
  const isLatest = v.version === latest.version;
  const history = useVersionHistory(query.name, v.version);
  const runs = history.data ?? [];
  const keys = useApiKeys();
  const roles = useRoles();
  const reach = queryReach(
    keys.data ?? {},
    roles.data ?? {},
    query.name,
    query.collection,
    v.connection_name,
  );

  const tabButton = (id: TabId, label: string, count?: number) => (
    <button
      type="button"
      role="tab"
      data-subtab={id}
      className={tab === id ? 'on' : ''}
      onClick={() => setTab(id)}
    >
      {label}
      {count !== undefined ? <span className="count">{count ? String(count) : ''}</span> : null}
    </button>
  );

  return (
    <div id="query-detail" className="panel detail">
      <div className="d-head">
        <div className="d-title">
          <h3>{query.name}</h3>
          {query.versions.length > 1 ? (
            <div className="versions" role="group" aria-label="Version">
              {query.versions.map((x) => (
                <button
                  key={x.version}
                  type="button"
                  className={x.version === v.version ? 'on' : ''}
                  title={x.version === latest.version ? 'Latest version' : 'Version ' + x.version}
                  onClick={() => setVersionNumber(x.version)}
                >
                  v{x.version}
                </button>
              ))}
            </div>
          ) : (
            <span className="vbadge">v{v.version}</span>
          )}
          <span className="hint">{isLatest ? 'latest' : 'older version'}</span>
          <span className={`pill ${v.status === 'published' ? 'ok' : 'off'}`} title={statusTitle(v, query)}>
            <i />
            {STATUS_LABEL[v.status]}
          </span>
          {v.cache_ttl ? (
            <span
              className="tag"
              title={`Served from cache for ${v.cache_ttl}s after each run - see the Caching tab`}
            >
              Cached · {v.cache_ttl}s
            </span>
          ) : null}
          <span className="spacer" />
          {/* One group, so the buttons wrap onto the next line together, as the classic three always did. */}
          <span style={{ display: 'flex', gap: 10, flexWrap: 'wrap' }}>
            <HeadActions query={query} v={v} etag={etag} />
          </span>
        </div>
        {v.description ? <p className="d-desc">{v.description}</p> : null}
        <dl className="meta">
          <MetaItem k="connection" v={v.connection_name || '—'} />
          <MetaItem k="collection" v={query.collection || '—'} />
          <MetaItem k="author" v={v.author || '—'} />
          <MetaItem k="modified" v={v.last_modified_at || v.created_at || '—'} />
          {v.tags.length ? <MetaItem k="tags" v={v.tags.join(', ')} /> : null}
        </dl>
        <StatTiles runs={runs} />
        <RequestsPerDay runs={runs} />
        <div className="subtabs" role="tablist">
          {tabButton('run', 'Run')}
          {tabButton('sql', 'SQL')}
          {tabButton('history', 'History', runs.length)}
          {tabButton('curl', 'Curl')}
          {tabButton('keys', 'API Keys', reach.keys.length)}
          {tabButton('roles', 'Roles', reach.roles.length)}
          {tabButton('access', 'Access')}
          {tabButton('cache', 'Cache')}
          {tabButton('metrics', 'Metrics')}
          {tabButton('cli', 'CLI')}
        </div>
      </div>
      <div className="d-body">
        {tab === 'run' && <RunTab query={query} v={v} />}
        {tab === 'sql' && <SqlTab query={query} v={v} />}
        {tab === 'history' && <HistoryTab v={v} runs={runs} />}
        {tab === 'curl' && <CurlTab query={query} v={v} />}
        {tab === 'keys' && <KeysTab reach={reach} keys={keys.data ?? {}} />}
        {tab === 'roles' && <RolesTab reach={reach} keys={keys.data ?? {}} roles={roles.data ?? {}} />}
        {tab === 'access' && <AccessTab query={query} v={v} reach={reach} />}
        {tab === 'cache' && <CacheTab query={query} v={v} etag={etag} />}
        {tab === 'metrics' && <MetricsTab v={v} runs={runs} />}
        {tab === 'cli' && <CliTab query={query} v={v} />}
      </div>
    </div>
  );
}

function statusTitle(v: QueryVersion, query: Query) {
  if (v.status === 'published') return `Served at ${query.endpoint}`;
  if (v.status === 'draft') return `Not served - only the admin key can run it, with ?version=${v.version}`;
  return `Served before; still runnable with ?version=${v.version}`;
}

function MetaItem({ k, v }: { k: string; v: string }) {
  return (
    <div>
      <dt>{k}</dt>
      <dd title={v}>{v}</dd>
    </div>
  );
}

/** Publish / Roll back / Unpublish, New version, Move… and Delete… (a menu, as in the classic view). */
function HeadActions({ query, v, etag }: { query: Query; v: QueryVersion; etag: string | null }) {
  const navigate = useNavigate();
  const { openDrawer, showError, toast } = useFeedback();
  const actions = useQueryActions(query.name);
  const [menu, setMenu] = useState<DOMRect | null>(null);
  const deleteBtn = useRef<HTMLButtonElement>(null);

  const report = (error: unknown) => {
    if (error instanceof ApiError && error.code === 'precondition_failed') {
      showError(
        'This query changed while you were looking at it - it has been reloaded. Check it and try again.',
        { status: 412 },
      );
    } else {
      showError((error as Error).message, error instanceof ApiError ? { status: error.status } : {});
    }
  };
  const publish = () =>
    actions.publish.mutate(
      { version: v.version, etag },
      {
        onSuccess: () => {
          showError('');
          toast(
            v.status === 'draft'
              ? `Published v${v.version} at ${query.endpoint}`
              : `Rolled back to v${v.version}`,
          );
        },
        onError: report,
      },
    );
  const unpublish = () => {
    if (
      !window.confirm(
        `Stop serving ${query.endpoint}? Callers get 404 until a version is published again. Every version is kept.`,
      )
    )
      return;
    actions.unpublish.mutate(
      { etag },
      { onSuccess: () => toast(`${query.name} unpublished`), onError: report },
    );
  };

  const items: { label: string; title?: string; run: () => void }[] = [];
  if (query.versions.length > 1) {
    items.push({
      label: `Delete v${v.version}`,
      title: `DELETE /api/v1/queries/${query.name}/versions/${v.version}`,
      run: () => {
        if (!window.confirm(`Delete version ${v.version} of ${query.name}?`)) return;
        actions.deleteVersion.mutate(
          { version: v.version, etag },
          { onSuccess: () => toast(`Deleted v${v.version} of ${query.name}`), onError: report },
        );
      },
    });
  }
  items.push({
    label:
      query.versions.length > 1 ? `Delete query (all ${query.versions.length} versions)` : 'Delete query',
    run: () => {
      if (
        !window.confirm(
          `Delete ${query.name}${query.versions.length > 1 ? ` and all ${query.versions.length} versions` : ''}?`,
        )
      )
        return;
      actions.deleteQuery.mutate(
        { etag },
        {
          onSuccess: () => {
            toast(`Deleted ${query.name}`);
            navigate('/queries');
          },
          onError: report,
        },
      );
    },
  });

  const busy = actions.publish.isPending || actions.unpublish.isPending;
  return (
    <>
      <button
        type="button"
        className="btn md"
        onClick={() =>
          openDrawer({
            title: 'New version',
            kicker: `${query.name} · from v${v.version}`,
            content: <QueryForm base={{ query, version: v, etag }} />,
          })
        }
      >
        New version
      </button>
      <button
        type="button"
        className="btn md"
        title={`File this query under a collection (PATCH /api/v1/queries/${query.name})`}
        onClick={() =>
          openDrawer({
            title: 'Move to a collection',
            kicker: query.name,
            content: <MoveForm query={query} etag={etag} />,
          })
        }
      >
        Move…
      </button>
      <button
        ref={deleteBtn}
        type="button"
        className="btn md danger"
        title="Delete this version or the whole query"
        onClick={() => setMenu(deleteBtn.current?.getBoundingClientRect() ?? null)}
      >
        Delete…
      </button>
      {v.status !== 'published' && (
        <button
          type="button"
          className={v.status === 'draft' ? 'btn md primary' : 'btn md'}
          disabled={busy}
          title={`POST /api/v1/queries/${query.name}/publish`}
          onClick={publish}
        >
          {v.status === 'draft' ? `Publish v${v.version}` : `Roll back to v${v.version}`}
        </button>
      )}
      {v.status === 'published' && (
        <button
          type="button"
          className="btn md"
          disabled={busy}
          title={`POST /api/v1/queries/${query.name}/unpublish`}
          onClick={unpublish}
        >
          Unpublish
        </button>
      )}
      {menu && <Menu anchor={menu} items={items} onClose={() => setMenu(null)} />}
    </>
  );
}

/** A small popover menu under its anchor; closes on an outside click or Escape (ui.py openMenu). */
function Menu({
  anchor,
  items,
  onClose,
}: {
  anchor: DOMRect;
  items: { label: string; title?: string; run: () => void }[];
  onClose: () => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const outside = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose();
    };
    const esc = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    const t = setTimeout(() => {
      document.addEventListener('mousedown', outside, true);
      document.addEventListener('keydown', esc, true);
    }, 0);
    return () => {
      clearTimeout(t);
      document.removeEventListener('mousedown', outside, true);
      document.removeEventListener('keydown', esc, true);
    };
  }, [onClose]);
  return (
    <div
      ref={ref}
      id="popmenu"
      className="menu"
      role="menu"
      style={{ top: anchor.bottom + 4, right: Math.max(8, window.innerWidth - anchor.right) }}
    >
      {items.map((it) => (
        <button
          key={it.label}
          type="button"
          role="menuitem"
          title={it.title}
          onClick={() => {
            onClose();
            it.run();
          }}
        >
          {it.label}
        </button>
      ))}
    </div>
  );
}

/** Daily request counts, zero-filled from the first to the last day recorded (ui.py dailyRequestCounts). */
function dailyCounts(runs: HistoryEntry[]) {
  const counts: Record<string, number> = {};
  runs.forEach((e) => {
    const day = String(e.executed_at || '').slice(0, 10);
    if (day) counts[day] = (counts[day] ?? 0) + 1;
  });
  const days = Object.keys(counts).sort();
  if (!days.length) return [];
  const out: { date: string; count: number }[] = [];
  const cursor = new Date(days[0] + 'T00:00:00Z');
  const end = new Date(days[days.length - 1] + 'T00:00:00Z');
  while (cursor <= end) {
    const iso = cursor.toISOString().slice(0, 10);
    out.push({ date: iso, count: counts[iso] ?? 0 });
    cursor.setUTCDate(cursor.getUTCDate() + 1);
  }
  return out;
}

function RequestsPerDay({ runs }: { runs: HistoryEntry[] }): ReactNode {
  const days = dailyCounts(runs);
  if (!days.length) return null;
  const max = days.reduce((m, d) => Math.max(m, d.count), 0);
  return (
    <div className="req-day-chart">
      <div className="req-day-head">
        <h3>Requests per day</h3>
        <span className="hint">{days.length === 1 ? '1 day' : `${days.length} days`}</span>
      </div>
      <div className="req-day-cols">
        {days.map((d) => (
          <div
            key={d.date}
            className="req-day-col"
            title={`${d.date} — ${d.count}${d.count === 1 ? ' request' : ' requests'}`}
          >
            {d.count === max && max > 0 ? <div className="req-day-label">{d.count}</div> : null}
            <div
              className={d.count ? 'req-day-bar' : 'req-day-bar zero'}
              style={{ height: `${max ? (100 * d.count) / max : 0}%` }}
            />
          </div>
        ))}
      </div>
      <div className="req-day-axis">
        <span>{days[0]!.date}</span>
        {days.length > 1 ? <span>{days[days.length - 1]!.date}</span> : null}
      </div>
    </div>
  );
}

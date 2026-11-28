import { useState, type ReactElement } from 'react';
import { useNavigate, useLocation } from 'react-router';

import { ApiError } from '@/api/client';
import { useApiKeys, useCan, useRoles } from '@/app/data';
import { Empty, Loading, useFeedback } from '@/app/feedback';

import { useQueryActions, useQueryDetail, useVersionHistory, type Query, type QueryVersion } from './api';
import { MoveForm, QueryForm } from './forms';
import { queryReach } from './reach';
import {
  AccessTab,
  CacheTab,
  CliTab,
  McpTab,
  CurlTab,
  HistoryTab,
  KeysTab,
  MetricsTab,
  RolesTab,
  RunTab,
  SqlTab,
} from './tabs';
import { StatStrip } from './stats';
import { Time } from '@/components/Time';
import { ManagedTag } from '@/components/Managed';
import { MoreIcon } from '@/components/icons';
import { MenuButton, type MenuEntry } from '@/components/Menu';

// The classic saved-query detail panel (ui.py renderDetail): title row with the version switcher and actions,
// description, meta, stat tiles, requests-per-day chart and the subtabs. Publishing (drafts, ADR 0001) is shown
// with the classic pill and buttons: Published / Draft / Previous, and Publish, Roll back or Unpublish.

type TabId =
  'run' | 'sql' | 'history' | 'curl' | 'keys' | 'roles' | 'access' | 'cache' | 'metrics' | 'cli' | 'mcp';

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
  // Home's request rows open a query on its History tab, at the version that ran
  const opened = useLocation().state as { tab?: TabId; version?: number } | null;
  const [versionNumber, setVersionNumber] = useState(opened?.version ?? latest.version);
  const [tab, setTab] = useState<TabId>(opened?.tab ?? 'run');
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
          <h3>
            {query.name} <ManagedTag kind="queries" name={query.name} />
          </h3>
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
          {isLatest ? null : (
            <span className="hint" title={`The latest is v${latest.version}`}>
              older version
            </span>
          )}
          <span
            className={`d-status ${v.status === 'published' ? 'ok' : 'off'}`}
            title={statusTitle(v, query)}
          >
            <i aria-hidden="true" />
            {STATUS_LABEL[v.status]}
          </span>
          {v.cache_ttl ? (
            <span
              className="d-cache"
              title={`Served from cache for ${v.cache_ttl}s after each run - see the Cache tab`}
            >
              Cached {v.cache_ttl}s
            </span>
          ) : null}
          <span className="spacer" />
          <span className="d-actions">
            <HeadActions query={query} v={v} etag={etag} />
          </span>
        </div>
        {v.description ? <p className="d-desc">{v.description}</p> : null}
        <dl className="d-meta">
          <MetaItem k="Connection" v={v.connection_name || '—'} />
          <MetaItem k="Author" v={v.author || '—'} />
          <MetaItem k="Modified" v={<Time value={v.last_modified_at || v.created_at} seconds={false} />} />
          {v.tags.length ? (
            <div className="d-tags">
              <dt className="visually-hidden">Tags</dt>
              {v.tags.map((t) => (
                <dd key={t} className="tag-chip">
                  {t}
                </dd>
              ))}
            </div>
          ) : null}
        </dl>
        <StatStrip runs={runs} />
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
          {tabButton('mcp', 'MCP')}
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
        {tab === 'mcp' && <McpTab query={query} v={v} etag={etag} />}
      </div>
    </div>
  );
}

function statusTitle(v: QueryVersion, query: Query) {
  if (v.status === 'published') return `Served at ${query.endpoint}`;
  if (v.status === 'draft') return `Not served - only the admin key can run it, with ?version=${v.version}`;
  return `Served before; still runnable with ?version=${v.version}`;
}

function MetaItem({ k, v }: { k: string; v: string | ReactElement }) {
  return (
    <div>
      <dt>{k}</dt>
      <dd title={typeof v === 'string' ? v : undefined}>{v}</dd>
    </div>
  );
}

/** Publish or Roll back (for a version that isn't the one served), New version, and a ⋯ menu: Move…, Unpublish
 * and the Delete items, each with its confirmation. */
function HeadActions({ query, v, etag }: { query: Query; v: QueryVersion; etag: string | null }) {
  const can = useCan();
  const navigate = useNavigate();
  const { openDrawer, showError, toast } = useFeedback();
  const actions = useQueryActions(query.name);
  const writer = can('queries.write');

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
  const deleteVersion = () => {
    if (!window.confirm(`Delete version ${v.version} of ${query.name}?`)) return;
    actions.deleteVersion.mutate(
      { version: v.version, etag },
      { onSuccess: () => toast(`Deleted v${v.version} of ${query.name}`), onError: report },
    );
  };
  const deleteQuery = () => {
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
  };

  const busy = actions.publish.isPending || actions.unpublish.isPending;
  const several = query.versions.length > 1;
  const entries: MenuEntry[] = [
    {
      label: 'Move…',
      title: `File this query under a collection (PATCH /api/v1/queries/${query.name})`,
      hidden: !writer,
      onSelect: () =>
        openDrawer({
          title: 'Move to a collection',
          kicker: query.name,
          content: <MoveForm query={query} etag={etag} />,
        }),
    },
    {
      label: 'Unpublish',
      title: `Stop serving ${query.endpoint} (POST /api/v1/queries/${query.name}/unpublish)`,
      hidden: !writer || v.status !== 'published',
      onSelect: unpublish,
    },
    'separator',
    {
      label: `Delete v${v.version}…`,
      title: `DELETE /api/v1/queries/${query.name}/versions/${v.version}`,
      danger: true,
      hidden: !writer || !several,
      onSelect: deleteVersion,
    },
    {
      label: several ? `Delete query (all ${query.versions.length} versions)…` : 'Delete query…',
      title: `DELETE /api/v1/queries/${query.name}`,
      danger: true,
      hidden: !writer,
      onSelect: deleteQuery,
    },
  ];
  return (
    <>
      {v.status !== 'published' && (
        <button
          hidden={!writer}
          type="button"
          className={v.status === 'draft' ? 'btn md primary' : 'btn md'}
          disabled={busy}
          title={`POST /api/v1/queries/${query.name}/publish`}
          onClick={publish}
        >
          {v.status === 'draft' ? `Publish v${v.version}` : `Roll back to v${v.version}`}
        </button>
      )}
      <button
        hidden={!writer}
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
      <MenuButton label="More actions: move, unpublish, delete" className="btn md icon" entries={entries}>
        <MoreIcon />
      </MenuButton>
    </>
  );
}

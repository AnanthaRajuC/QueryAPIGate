import { Link } from 'react-router';

import type { Schemas } from '@/api/client';
import { useApiKeys, useCan, useCollections } from '@/app/data';
import { Loading } from '@/app/feedback';
import { AccessCell } from '@/features/repository/reach';

import { useAccessDrawers, useRevokeKey } from './forms';
import { ScopeNode } from './grants';
import { grantBreadth, grantWords, ratePerSecond, writeLevel } from './grantSort';
import { ActivitySpark } from './KeyActivity';
import { useKeyActivityMap } from './activity';
import { Time } from '@/components/Time';
import { PencilIcon } from '@/components/icons';
import { ManagedTag } from '@/components/Managed';
import { NoMatches, SortHeader, TableFilter } from '@/components/TableControls';
import { useTableControls, type Column } from '@/components/useTableControls';

// The classic API keys screen (ui.py #tab-apikeys, renderApiKeys), on /api/v1/api-keys.

function isExpiringSoon(expires: string | null, today: string) {
  const soon = new Date(Date.now() + 7 * 24 * 60 * 60 * 1000).toISOString().slice(0, 10);
  return Boolean(expires && expires >= today && expires <= soon);
}

type QuotaUsage = NonNullable<Schemas['ApiKey']['usage_this_period']>;

/** A key's use of its quota this period - the fuller of calls and rows - as a line and a bar (ADR 0011). */
function QuotaUsageLine({ usage }: { usage: QuotaUsage }) {
  const parts = (['calls', 'rows'] as const)
    .map((what) => ({ what, used: usage[what], limit: usage[`${what}_limit`] }))
    .filter((p): p is { what: 'calls' | 'rows'; used: number; limit: number } => p.limit != null);
  const fullest = parts.reduce((a, b) => (b.used / b.limit > a.used / a.limit ? b : a), parts[0]!);
  const share = Math.min(1, fullest.used / fullest.limit);
  const per = usage.per === 'day' ? 'today' : 'this month';
  return (
    <div
      className="quota-usage"
      title={
        parts.map((p) => `${p.used.toLocaleString()} of ${p.limit.toLocaleString()} ${p.what}`).join(' · ') +
        ` ${per} · resets ${usage.resets_at}`
      }
    >
      <span className="dim">
        {fullest.used.toLocaleString()} / {fullest.limit.toLocaleString()} {fullest.what} {per}
      </span>
      <div className={`quota-bar${share >= 1 ? ' full' : share >= 0.8 ? ' near' : ''}`}>
        <span style={{ width: `${Math.round(share * 100)}%` }} />
      </div>
    </div>
  );
}

function usageText(usage: { queries: number; errors: number }) {
  if (!usage.queries) return null;
  const parts = [usage.queries + (usage.queries === 1 ? ' query' : ' queries')];
  if (usage.errors) parts.push(usage.errors + ' failed');
  return parts.join(' · ');
}

type ApiKey = Schemas['ApiKey'];
type KeyRow = { name: string; k: ApiKey };

const KEY_COLUMNS: Column<KeyRow>[] = [
  { key: 'name', label: 'Name', sort: (r) => r.name },
  {
    key: 'scope',
    label: 'Scope',
    sort: (r) => grantBreadth(r.k),
    title: 'Sorts by how much a key can reach',
  },
  { key: 'access', label: 'Access', sort: (r) => writeLevel(r.k) },
  { key: 'rate', label: 'Rate limit', sort: (r) => ratePerSecond(r.k.rate_limit) },
  { key: 'ips', label: 'IPs', sort: (r) => r.k.allowed_ips?.length || null },
  { key: 'expires', label: 'Expires', sort: (r) => r.k.expires_at },
  { key: 'last_used', label: 'Last used', sort: (r) => r.k.last_used_at },
  { key: 'usage', label: 'Usage', sort: (r) => r.k.usage.queries || null },
  { key: 'activity', label: 'Activity (30d)' },
  { key: 'actions', label: '' },
];

/** What the filter box finds a key by: its name, grants, role, rate limit, IPs, tags and state. */
function keyText({ name, k }: KeyRow) {
  return [
    name,
    ...grantWords(k),
    k.created_from_role,
    k.rate_limit,
    ...(k.allowed_ips ?? []),
    k.active ? null : 'revoked',
    k.previous_expires_at ? 'rotating' : null,
    k.mask?.length ? 'masked' : null,
    k.allow_writes ? 'read/write' : 'read-only',
  ];
}

export function ApiKeysPage() {
  const can = useCan();
  const keys = useApiKeys();
  const collections = useCollections();
  const activity = useKeyActivityMap();
  const drawers = useAccessDrawers();
  const revoke = useRevokeKey();
  const names = Object.keys(keys.data ?? {}).sort();
  const today = new Date().toISOString().slice(0, 10);
  const table = useTableControls(
    names.map((name) => ({ name, k: keys.data![name]! })),
    { columns: KEY_COLUMNS, text: keyText },
  );

  let body: React.ReactNode;
  if (keys.isPending) {
    body = <Loading text="Loading API keys…" />;
  } else if (keys.isError) {
    body = (
      <div className="empty">
        <strong>Couldn’t load API keys</strong>
        <span>{keys.error.message}</span>
        <button type="button" className="btn sm" onClick={() => keys.refetch()}>
          Retry
        </button>
      </div>
    );
  } else if (!names.length) {
    body = (
      <div className="empty">
        <strong>No scoped API keys yet</strong>
        <span>
          QUERYAPIGATE_API_KEY is a full-access admin key. Create a scoped one to limit a caller to specific
          connections, read-only.
        </span>
        <button
          hidden={!can('access.write')}
          type="button"
          className="btn primary"
          onClick={() => drawers.newKey()}
        >
          New API key
        </button>
      </div>
    );
  } else {
    body = (
      <>
        <div className="panel-bar">
          <span className="spacer" />
          <TableFilter
            controls={table}
            id="apikeys-filter"
            placeholder="Filter keys…"
            label="Filter API keys"
          />
        </div>
        <div style={{ overflowX: 'auto' }}>
          <table className="grid">
            <thead>
              <tr>
                {table.columns.map((c) => (
                  <SortHeader key={c.key} controls={table} column={c} />
                ))}
              </tr>
            </thead>
            <tbody>
              {table.rows.map(({ name, k }) => {
                const expired = Boolean(k.expires_at && k.expires_at < today);
                const soon = !expired && isExpiringSoon(k.expires_at, today);
                const ips = k.allowed_ips ?? [];
                const usage = usageText(k.usage);
                return (
                  <tr key={name} data-name={name}>
                    <td>
                      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                        <span
                          className={`dot ${k.active && !expired ? 'ok' : 'off'}`}
                          title={!k.active ? 'Revoked' : expired ? 'Expired' : 'Active'}
                        />
                        <span
                          className="name"
                          title={
                            k.created_at
                              ? 'Created ' +
                                k.created_at +
                                (k.created_from_role ? ' from role ' + k.created_from_role : '')
                              : undefined
                          }
                        >
                          {name}
                        </span>
                        <ManagedTag kind="api_keys" name={name} />
                        {k.previous_expires_at ? (
                          <span className="tag" title="Rotated: the old secret works until then too">
                            rotating
                          </span>
                        ) : null}
                        {k.mask?.length ? (
                          <span
                            className="tag"
                            title={k.mask.map((r) => `${r.column}: ${r.action}`).join(', ')}
                          >
                            masked
                          </span>
                        ) : null}
                      </div>
                    </td>
                    <td>
                      <ScopeNode entry={k} collections={collections.data?.collections} />
                    </td>
                    <AccessCell entry={k} />
                    <td className={k.rate_limit ? 'mono' : 'mono dim'} style={{ whiteSpace: 'nowrap' }}>
                      {k.rate_limit || 'server default'}
                    </td>
                    <td className="dim">
                      {ips.length ? (
                        <span title={ips.join(', ')}>{ips.length + (ips.length === 1 ? ' IP' : ' IPs')}</span>
                      ) : (
                        <span className="dim">any</span>
                      )}
                    </td>
                    <td style={{ whiteSpace: 'nowrap' }}>
                      {k.expires_at ? (
                        <span
                          style={
                            expired ? { color: 'var(--danger)' } : soon ? { color: 'var(--warn)' } : undefined
                          }
                        >
                          {k.expires_at + (expired ? ' (expired)' : soon ? ' (expires soon)' : '')}
                        </span>
                      ) : (
                        <span className="dim">never</span>
                      )}
                    </td>
                    <td className="mono dim" style={{ whiteSpace: 'nowrap' }}>
                      <Time value={k.last_used_at} fallback="never" />
                    </td>
                    {k.usage_this_period ? (
                      <td style={{ whiteSpace: 'nowrap' }}>
                        {usage}
                        <QuotaUsageLine usage={k.usage_this_period} />
                      </td>
                    ) : usage ? (
                      <td
                        title={`${k.usage.rows}${k.usage.rows === 1 ? ' row' : ' rows'} returned in total · since this process started`}
                        style={{ whiteSpace: 'nowrap' }}
                      >
                        {usage}
                      </td>
                    ) : (
                      <td className="dim" style={{ whiteSpace: 'nowrap' }}>
                        No activity yet
                      </td>
                    )}
                    <td className="spark-cell">
                      <ActivitySpark
                        label={name}
                        items={activity.by[name]}
                        isPending={activity.isPending}
                        isError={activity.isError}
                        to={`/api-keys/${encodeURIComponent(name)}`}
                      />
                    </td>
                    <td>
                      <div className="actions">
                        <Link
                          to={`/api-keys/${encodeURIComponent(name)}`}
                          className="btn ghost sm"
                          title={`Full details of ${name}`}
                        >
                          Details
                        </Link>
                        <button
                          hidden={!can('access.write')}
                          type="button"
                          className="btn ghost sm icon"
                          title={`Edit ${name}`}
                          aria-label="Edit"
                          onClick={() => drawers.editKey(name)}
                        >
                          <PencilIcon />
                        </button>
                        <button
                          hidden={!can('access.write')}
                          type="button"
                          className="btn ghost sm danger"
                          onClick={() => void revoke(name)}
                        >
                          Revoke
                        </button>
                      </div>
                    </td>
                  </tr>
                );
              })}
              {!table.rows.length && <NoMatches controls={table} span={KEY_COLUMNS.length} />}
            </tbody>
          </table>
        </div>
      </>
    );
  }

  return (
    <>
      <div className="page-head">
        <div className="titles">
          <h1>API keys</h1>
          <span className="sub" id="apikeys-sub">
            {(names.length ? names.length + (names.length === 1 ? ' key. ' : ' keys. ') : '') +
              'Scope each one to connections, collections or roles.'}
          </span>
        </div>
        <span className="spacer" />
        <button
          hidden={!can('access.write')}
          id="new-apikey"
          type="button"
          className="btn primary"
          onClick={() => drawers.newKey()}
        >
          New API key
        </button>
      </div>
      <div id="apikeys-table" className="panel">
        {body}
      </div>
    </>
  );
}

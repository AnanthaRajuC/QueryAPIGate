import { useApiKeys, useCan, useCollections } from '@/app/data';
import { Loading } from '@/app/feedback';
import { AccessCell } from '@/features/repository/reach';

import { useAccessDrawers, useRevokeKey } from './forms';
import { ScopeNode } from './grants';
import { Time } from '@/components/Time';
import { PencilIcon } from '@/components/icons';

// The classic API keys screen (ui.py #tab-apikeys, renderApiKeys), on /api/v1/api-keys.

function isExpiringSoon(expires: string | null, today: string) {
  const soon = new Date(Date.now() + 7 * 24 * 60 * 60 * 1000).toISOString().slice(0, 10);
  return Boolean(expires && expires >= today && expires <= soon);
}

function usageText(usage: { queries: number; errors: number }) {
  if (!usage.queries) return null;
  const parts = [usage.queries + (usage.queries === 1 ? ' query' : ' queries')];
  if (usage.errors) parts.push(usage.errors + ' failed');
  return parts.join(' · ');
}

export function ApiKeysPage() {
  const can = useCan();
  const keys = useApiKeys();
  const collections = useCollections();
  const drawers = useAccessDrawers();
  const revoke = useRevokeKey();
  const names = Object.keys(keys.data ?? {}).sort();
  const today = new Date().toISOString().slice(0, 10);

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
      <div style={{ overflowX: 'auto' }}>
        <table className="grid">
          <thead>
            <tr>
              {['Name', 'Scope', 'Access', 'Rate limit', 'IPs', 'Expires', 'Last used', 'Usage', ''].map(
                (t, i) => (
                  <th key={i}>{t}</th>
                ),
              )}
            </tr>
          </thead>
          <tbody>
            {names.map((name) => {
              const k = keys.data![name]!;
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
                  {usage ? (
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
                  <td>
                    <div className="actions">
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
          </tbody>
        </table>
      </div>
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

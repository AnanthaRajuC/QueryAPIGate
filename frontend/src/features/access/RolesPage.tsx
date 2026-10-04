import { useCollections, useRoles } from '@/app/data';
import { Loading } from '@/app/feedback';
import { PencilIcon, TrashIcon } from '@/components/icons';
import { AccessCell } from '@/features/repository/reach';

import { useAccessDrawers, useDeleteRole } from './forms';
import { ExampleBadge, queryGrantTags } from './grants';

// The classic Roles screen (ui.py #tab-roles, renderRoles), on /api/v1/roles.

export function RolesPage() {
  const roles = useRoles();
  const collections = useCollections();
  const drawers = useAccessDrawers();
  const remove = useDeleteRole();
  const names = Object.keys(roles.data ?? {}).sort();

  let body: React.ReactNode;
  if (roles.isPending) {
    body = <Loading text="Loading roles…" />;
  } else if (roles.isError) {
    body = (
      <div className="empty">
        <strong>Couldn’t load roles</strong>
        <span>Only the admin key (QUERYAPIGATE_API_KEY) can manage roles.</span>
        <button type="button" className="btn sm" onClick={() => roles.refetch()}>
          Retry
        </button>
      </div>
    );
  } else if (!names.length) {
    body = (
      <div className="empty">
        <strong>No roles yet</strong>
        <span>
          A role is a reusable template of connections, queries, write access, rate limit and allowed IPs —
          create an API key &quot;from&quot; a role instead of filling in every field by hand each time.
        </span>
        <button type="button" className="btn primary" onClick={drawers.newRole}>
          New role
        </button>
      </div>
    );
  } else {
    body = (
      <div style={{ overflowX: 'auto' }}>
        <table className="grid">
          <thead>
            <tr>
              {['Name', 'Connections', 'Queries / collections', 'Access', 'Rate limit', 'Keys', ''].map(
                (t, i) => (
                  <th key={i}>{t}</th>
                ),
              )}
            </tr>
          </thead>
          <tbody>
            {names.map((name) => {
              const r = roles.data![name]!;
              const conns =
                r.connections === '*'
                  ? 'all connections'
                  : r.connections.length
                    ? r.connections.join(', ')
                    : 'none';
              const tags = queryGrantTags(r, collections.data?.collections);
              return (
                <tr key={name} data-name={name}>
                  <td>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                      <span className="name">{name}</span>
                      {r.example && <ExampleBadge />}
                    </div>
                  </td>
                  <td className="dim">{conns}</td>
                  <td>
                    <div className="tags scope">
                      {tags.length ? tags : <span className="tag coll">—</span>}
                    </div>
                  </td>
                  <AccessCell entry={r} />
                  <td className={r.rate_limit ? 'mono' : 'mono dim'} style={{ whiteSpace: 'nowrap' }}>
                    {r.rate_limit || 'server default'}
                  </td>
                  <td className={r.keys_created ? 'mono' : 'mono dim'} title="Keys created from this role">
                    {r.keys_created}
                  </td>
                  <td>
                    <div className="actions">
                      <button type="button" className="btn ghost sm" onClick={() => drawers.newKey(name)}>
                        New key from this
                      </button>
                      <button
                        type="button"
                        className="btn ghost sm icon"
                        title={`Edit ${name}`}
                        aria-label="Edit"
                        onClick={() => drawers.editRole(name)}
                      >
                        <PencilIcon />
                      </button>
                      <button
                        type="button"
                        className="btn ghost sm icon danger"
                        title={`Delete ${name}`}
                        aria-label="Delete"
                        onClick={() => void remove(name)}
                      >
                        <TrashIcon />
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
          <h1>Roles</h1>
          <span className="sub" id="roles-sub">
            Reusable permission sets that API keys can inherit.
          </span>
        </div>
        <span className="spacer" />
        <button id="new-role" type="button" className="btn primary" onClick={drawers.newRole}>
          New role
        </button>
      </div>
      <div id="roles-table" className="panel">
        {body}
      </div>
    </>
  );
}

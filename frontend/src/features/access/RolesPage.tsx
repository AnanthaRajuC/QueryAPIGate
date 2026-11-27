import { useApiKeys, useCan, useCollections, useRoles } from '@/app/data';
import { Loading } from '@/app/feedback';
import { PencilIcon, TrashIcon } from '@/components/icons';
import { AccessCell } from '@/features/repository/reach';

import { ActivitySpark } from './KeyActivity';
import { useKeyActivityMap } from './activity';
import { useAccessDrawers, useDeleteRole } from './forms';
import { ExampleBadge, queryGrantTags } from './grants';
import { grantBreadth, grantWords, ratePerSecond, writeLevel } from './grantSort';
import { ManagedTag } from '@/components/Managed';
import { NoMatches, SortHeader, TableFilter } from '@/components/TableControls';
import { useTableControls, type Column } from '@/components/useTableControls';
import { PageTitle } from '@/components/PageTitle';

// The classic Roles screen (ui.py #tab-roles, renderRoles), on /api/v1/roles.

type Role = NonNullable<ReturnType<typeof useRoles>['data']>[string];
type RoleRow = { name: string; r: Role };

const ROLE_COLUMNS: Column<RoleRow>[] = [
  { key: 'name', label: 'Name', sort: (x) => x.name },
  {
    key: 'connections',
    label: 'Connections',
    sort: (x) => (x.r.connections === '*' ? 10_000 : x.r.connections.length),
    title: 'Sorts by how many connections a role reaches',
  },
  {
    key: 'queries',
    label: 'Queries / collections',
    sort: (x) => grantBreadth({ ...x.r, connections: [] }),
    title: 'Sorts by how many queries and collections a role grants',
  },
  { key: 'access', label: 'Access', sort: (x) => writeLevel(x.r) },
  { key: 'rate', label: 'Rate limit', sort: (x) => ratePerSecond(x.r.rate_limit) },
  { key: 'keys', label: 'Keys', sort: (x) => x.r.keys_created ?? 0 },
  { key: 'activity', label: 'Keys’ activity (30d)' },
  { key: 'actions', label: '' },
];

/** What the filter box finds a role by: its name, grants, rate limit and tags. */
function roleText({ name, r }: RoleRow) {
  return [
    name,
    ...grantWords(r),
    r.rate_limit,
    r.allow_writes ? 'read/write' : 'read-only',
    r.mask?.length ? 'masked' : null,
    r.require_claim_binding ? 'bound to user' : null,
    r.example ? 'example' : null,
  ];
}

export function RolesPage() {
  const can = useCan();
  const roles = useRoles();
  const collections = useCollections();
  const keys = useApiKeys();
  const activity = useKeyActivityMap();
  const drawers = useAccessDrawers();
  const remove = useDeleteRole();
  const names = Object.keys(roles.data ?? {}).sort();
  const table = useTableControls(
    names.map((name) => ({ name, r: roles.data![name]! })),
    { columns: ROLE_COLUMNS, text: roleText },
  );

  // A role doesn't run queries itself; its activity is the combined runs of the keys created from it.
  const roleRuns = (role: string) =>
    Object.values(keys.data ?? {})
      .filter((k) => k.created_from_role === role)
      .flatMap((k) => activity.by[k.name] ?? []);

  let body: React.ReactNode;
  if (roles.isPending) {
    body = <Loading text="Loading roles…" />;
  } else if (roles.isError) {
    body = (
      <div className="empty">
        <strong>Couldn’t load roles</strong>
        <span>{roles.error.message}</span>
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
        <button hidden={!can('access.write')} type="button" className="btn primary" onClick={drawers.newRole}>
          New role
        </button>
      </div>
    );
  } else {
    body = (
      <>
        <div className="panel-bar">
          <span className="spacer" />
          <TableFilter controls={table} id="roles-filter" placeholder="Filter roles…" label="Filter roles" />
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
              {table.rows.map(({ name, r }) => {
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
                        <ManagedTag kind="roles" name={name} />
                        {r.example && <ExampleBadge />}
                        {r.require_claim_binding ? (
                          <span
                            className="tag"
                            title="Its signed-in users run only saved queries that take a parameter from their token"
                          >
                            bound to user
                          </span>
                        ) : null}
                        {r.mask?.length ? (
                          <span
                            className="tag"
                            title={r.mask.map((m) => `${m.column}: ${m.action}`).join(', ')}
                          >
                            masked
                          </span>
                        ) : null}
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
                    <td className="spark-cell">
                      <ActivitySpark
                        label={name}
                        items={roleRuns(name)}
                        isPending={activity.isPending || keys.isPending}
                        isError={activity.isError || keys.isError}
                      />
                    </td>
                    <td>
                      <div className="actions">
                        <button
                          hidden={!can('access.write')}
                          type="button"
                          className="btn ghost sm"
                          onClick={() => drawers.newKey(name)}
                        >
                          New key from this
                        </button>
                        <button
                          hidden={!can('access.write')}
                          type="button"
                          className="btn ghost sm icon"
                          title={`Edit ${name}`}
                          aria-label="Edit"
                          onClick={() => drawers.editRole(name)}
                        >
                          <PencilIcon />
                        </button>
                        <button
                          hidden={!can('access.write')}
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
              {!table.rows.length && <NoMatches controls={table} span={ROLE_COLUMNS.length} />}
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
          <PageTitle tab>Roles</PageTitle>
          <span className="sub" id="roles-sub">
            Reusable permission sets that API keys can inherit.
          </span>
        </div>
        <span className="spacer" />
        <button
          hidden={!can('access.write')}
          id="new-role"
          type="button"
          className="btn primary"
          onClick={drawers.newRole}
        >
          New role
        </button>
      </div>
      <div id="roles-table" className="panel">
        {body}
      </div>
    </>
  );
}

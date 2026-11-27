import { usePendingWrites } from './approvals';
import { useAlertFeed } from './alerts';
import {
  useAdministrators,
  useAllQueries,
  useApiKeys,
  useCan,
  useConnections,
  useRoles,
  useWorkspaces,
} from './data';
import { ATTENTION, type CountKind } from './navigation';

// How many of each a screen lists, for its tab (SectionLayout) - and, for what asks for someone's action (open alerts,
// writes waiting for approval), a coloured badge on its section in the sidebar. Each count is its own component, so a
// tab asks only for what it shows: the Data page doesn't fetch API keys.

const size = (record: object | undefined) => (record ? Object.keys(record).length : undefined);

function useConnectionCount() {
  return useConnections().data?.length;
}
function useQueryCount() {
  return useAllQueries().data?.length;
}
function useApiKeyCount() {
  return size(useApiKeys().data);
}
function useRoleCount() {
  return size(useRoles().data);
}
function useWorkspaceCount() {
  return useWorkspaces().data?.length;
}
function useAdministratorCount() {
  const can = useCan();
  return useAdministrators(can('admins.read')).data?.length;
}
function useAlertCount() {
  return useAlertFeed().urgent;
}
function useApprovalCount() {
  return usePendingWrites().data?.length;
}

const COUNTERS: Record<CountKind, () => number | undefined> = {
  connections: useConnectionCount,
  queries: useQueryCount,
  apikeys: useApiKeyCount,
  roles: useRoleCount,
  workspaces: useWorkspaceCount,
  administrators: useAdministratorCount,
  alerts: useAlertCount,
  approvals: useApprovalCount,
};

/** A tab's count, once known: a coloured badge for one that asks for action (and isn't 0), a quiet total otherwise. */
export function NavCount({ kind }: { kind: CountKind }) {
  const useCount = COUNTERS[kind];
  const value = useCount();
  if (value === undefined) return null;
  const attention = ATTENTION[kind];
  return <span className={attention && value ? `count attention ${attention}` : 'count'}>{value}</span>;
}

/** The sidebar badge for a section holding screens that ask for action: the most urgent kind that has any - open
 * alerts before writes waiting for approval. */
export function AttentionBadge({ kinds }: { kinds: CountKind[] }) {
  const alerts = useAlertCount();
  const approvals = useApprovalCount();
  const values: Partial<Record<CountKind, number | undefined>> = { alerts, approvals };
  const kind = (['alerts', 'approvals'] as const).find((k) => kinds.includes(k) && values[k]);
  if (!kind) return null;
  return <span className={`count attention ${ATTENTION[kind]}`}>{values[kind]}</span>;
}

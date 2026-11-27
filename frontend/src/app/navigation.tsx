import type { ReactNode } from 'react';

// The sidebar, item for item as the classic UI had it (ui.py <nav id="tabs">): same groups, names, order and icons -
// each one a Console route. `tab` is the classic tab's id, which the shell uses as the screen's section id (#tab-<tab>), so the
// classic stylesheet's per-screen rules apply unchanged (ADR 0001).

export interface NavItem {
  tab: string;
  group: string;
  label: string;
  icon: ReactNode;
  /** Console route. */
  path: string;
  count?:
    | 'connections'
    | 'queries'
    | 'apikeys'
    | 'roles'
    | 'workspaces'
    | 'administrators'
    | 'alerts'
    | 'approvals';
  /** The capability (adminroles.py) a role needs for this screen; hidden without it. 'data': may run SQL. */
  needs?: string;
}

export type CountKind = NonNullable<NavItem['count']>;

/** Counts that ask for someone's action: a coloured badge (and a dot on the icon in the collapsed rail) - on the tab,
 * and on its section in the sidebar (navCounts.tsx). The others are totals, shown quietly on the tab. */
export const ATTENTION: Partial<Record<CountKind, 'danger' | 'accent'>> = {
  alerts: 'danger',
  approvals: 'accent',
};

const svg = (children: ReactNode) => (
  <svg
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth="2"
    strokeLinecap="round"
    strokeLinejoin="round"
  >
    {children}
  </svg>
);

/** A sidebar group shown as one item: it opens a page of its own whose tabs are the group's screens (SectionLayout).
 * Each screen keeps its own route, so links and bookmarks to it still work; the section's `path` opens the first one
 * the caller may see. */
export interface NavSection {
  id: string;
  label: string;
  path: string;
  description: string;
  icon: ReactNode;
}

export interface NavGroup {
  label: string;
  items: NavItem[];
  section?: NavSection;
}

export const NAV_GROUPS: NavGroup[] = [
  {
    label: 'Overview',
    items: [
      {
        tab: 'home',
        path: '/',
        group: 'Overview',
        label: 'Home',
        icon: svg(
          <>
            <path d="M3 11l9-8 9 8" />
            <path d="M5 10v10h14V10" />
          </>,
        ),
      },
    ],
  },
  {
    label: 'Data',
    section: {
      id: 'data',
      label: 'Data',
      path: '/data',
      description:
        'The databases this gateway reaches, the change streams it listens to, and the response cache.',
      icon: svg(
        <>
          <ellipse cx="12" cy="5.5" rx="7.5" ry="2.8" />
          <path d="M4.5 5.5v6c0 1.5 3.4 2.8 7.5 2.8s7.5-1.3 7.5-2.8v-6" />
          <path d="M4.5 11.5v6c0 1.5 3.4 2.8 7.5 2.8s7.5-1.3 7.5-2.8v-6" />
        </>,
      ),
    },
    items: [
      {
        tab: 'connections',
        needs: 'connections.read',
        group: 'Data',
        label: 'Connections',
        path: '/connections',
        count: 'connections',
        icon: svg(
          <>
            <ellipse cx="12" cy="5" rx="8" ry="3" />
            <path d="M4 5v6c0 1.7 3.6 3 8 3s8-1.3 8-3V5" />
            <path d="M4 11v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6" />
          </>,
        ),
      },
      {
        tab: 'cdcsources',
        needs: 'connections.read',
        path: '/cdc-sources',
        group: 'Data',
        label: 'CDC sources',
        icon: svg(
          <>
            <path d="M4 12h3l2-6 4 12 2-6h5" />
          </>,
        ),
      },
      {
        tab: 'caching',
        needs: 'cache.read',
        path: '/caching',
        group: 'Data',
        label: 'Caching',
        icon: svg(<path d="M13 2 4 14h6l-1 8 9-12h-6l1-8z" />),
      },
    ],
  },
  {
    label: 'API',
    section: {
      id: 'api',
      label: 'API',
      path: '/api',
      description: 'Saved queries published as REST endpoints, and the designer to write and try them first.',
      icon: svg(
        <>
          <path d="M8 6l-5 6 5 6" />
          <path d="M16 6l5 6-5 6" />
          <path d="M13.5 4l-3 16" />
        </>,
      ),
    },
    items: [
      {
        tab: 'queries',
        needs: 'queries.read',
        group: 'API',
        label: 'API Repository',
        path: '/queries',
        count: 'queries',
        icon: svg(
          <>
            <rect x="3" y="7" width="18" height="13" rx="1" />
            <path d="M3 7l2-4h14l2 4" />
            <path d="M10 12h4" />
          </>,
        ),
      },
      {
        tab: 'run',
        needs: 'data',
        group: 'API',
        label: 'API Designer',
        path: '/designer',
        icon: svg(
          <>
            <path d="M9 7 4 12l5 5" />
            <path d="M15 7l5 5-5 5" />
          </>,
        ),
      },
    ],
  },
  {
    label: 'MCP',
    items: [
      {
        tab: 'mcp',
        needs: 'observe',
        group: 'MCP',
        label: 'MCP server',
        path: '/mcp',
        icon: svg(
          <>
            <rect x="3" y="4" width="18" height="14" rx="2" />
            <path d="M8 20h8M7 9l2.5 2.5L7 14M12 14h4" />
          </>,
        ),
      },
    ],
  },
  {
    label: 'Delivery',
    section: {
      id: 'delivery',
      label: 'Delivery',
      path: '/delivery',
      description:
        'Saved queries written out as files - the exports, and the buckets and folders they go to.',
      icon: svg(
        <>
          <path d="M12 3v12" />
          <path d="M7 10l5 5 5-5" />
          <path d="M4 17v2.5A1.5 1.5 0 0 0 5.5 21h13a1.5 1.5 0 0 0 1.5-1.5V17" />
        </>,
      ),
    },
    items: [
      {
        tab: 'exports',
        path: '/exports',
        group: 'Delivery',
        label: 'Exports',
        needs: 'exports.read',
        icon: svg(
          <>
            <path d="M12 3v12" />
            <path d="M7 10l5 5 5-5" />
            <path d="M5 21h14" />
          </>,
        ),
      },
      {
        tab: 'destinations',
        path: '/destinations',
        group: 'Delivery',
        label: 'Destinations',
        needs: 'exports.read',
        icon: svg(
          <>
            <path d="M4 7h16v12H4z" />
            <path d="M4 7l2-3h12l2 3" />
            <path d="M9 12h6" />
          </>,
        ),
      },
    ],
  },
  {
    label: 'Access',
    section: {
      id: 'access',
      label: 'Access',
      path: '/access',
      description:
        'Who can reach what: API keys and the roles they come from, workspaces, and the administrators.',
      icon: svg(
        <>
          <rect x="4" y="10.5" width="16" height="10" rx="2" />
          <path d="M8 10.5V7a4 4 0 0 1 8 0v3.5" />
          <path d="M12 14.5v2.5" />
        </>,
      ),
    },
    items: [
      {
        tab: 'apikeys',
        needs: 'access.read',
        path: '/api-keys',
        group: 'Access',
        label: 'API keys',
        count: 'apikeys',
        icon: svg(
          <>
            <circle cx="7" cy="15" r="4" />
            <path d="M10 12l10-10" />
            <path d="M17 5l3 3" />
            <path d="M14 8l2 2" />
          </>,
        ),
      },
      {
        tab: 'roles',
        needs: 'access.read',
        path: '/roles',
        group: 'Access',
        label: 'Roles',
        count: 'roles',
        icon: svg(<path d="M12 3l7 3v6c0 4.5-3 7.5-7 9-4-1.5-7-4.5-7-9V6l7-3z" />),
      },
      {
        tab: 'accessmap',
        needs: 'access.read',
        path: '/access-map',
        group: 'Access',
        label: 'Access map',
        icon: svg(
          <>
            <circle cx="6" cy="6" r="2.5" />
            <circle cx="18" cy="6" r="2.5" />
            <circle cx="12" cy="18" r="2.5" />
            <path d="M8 7l3 9M16 7l-3 9" />
          </>,
        ),
      },
      {
        tab: 'workspaces',
        path: '/workspaces',
        group: 'Access',
        label: 'Workspaces',
        count: 'workspaces',
        needs: 'self',
        icon: svg(
          <>
            <rect x="3" y="4" width="8" height="7" rx="1.5" />
            <rect x="13" y="4" width="8" height="7" rx="1.5" />
            <rect x="8" y="13" width="8" height="7" rx="1.5" />
          </>,
        ),
      },
      {
        tab: 'administrators',
        path: '/administrators',
        group: 'Access',
        label: 'Administrators',
        count: 'administrators',
        needs: 'self',
        icon: svg(
          <>
            <circle cx="9" cy="8" r="3.5" />
            <path d="M2.5 20c.8-3.6 3.4-5.5 6.5-5.5s5.7 1.9 6.5 5.5" />
            <path d="M16 4.5a3.5 3.5 0 0 1 0 7" />
            <path d="M18 14.8c2 .7 3.2 2.5 3.6 5.2" />
          </>,
        ),
      },
    ],
  },
  {
    label: 'Observability',
    section: {
      id: 'observability',
      label: 'Observability',
      path: '/observability',
      description:
        'What is happening: alerts, live metrics, what AI agents did, writes waiting for approval, and the audit log.',
      icon: svg(
        <>
          <path d="M3 12h4l2.5-6 4 12 2.5-6H21" />
        </>,
      ),
    },
    items: [
      {
        tab: 'alerts',
        needs: 'observe',
        path: '/alerts',
        group: 'Observability',
        label: 'Alerts',
        count: 'alerts',
        icon: svg(
          <>
            <path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9" />
            <path d="M10.3 21a1.94 1.94 0 0 0 3.4 0" />
          </>,
        ),
      },
      {
        tab: 'metrics',
        needs: 'observe',
        path: '/metrics',
        group: 'Observability',
        label: 'Metrics',
        icon: svg(
          <>
            <path d="M4 20V10" />
            <path d="M12 20V4" />
            <path d="M20 20v-7" />
          </>,
        ),
      },
      {
        tab: 'agents',
        needs: 'observe',
        path: '/agents',
        group: 'Observability',
        label: 'Agents',
        icon: svg(
          <>
            <rect x="5" y="8" width="14" height="11" rx="2" />
            <path d="M12 4v4" />
            <circle cx="9.5" cy="13" r="1" />
            <circle cx="14.5" cy="13" r="1" />
          </>,
        ),
      },
      {
        tab: 'approvals',
        needs: 'writes.approve',
        path: '/approvals',
        group: 'Observability',
        label: 'Approvals',
        count: 'approvals',
        icon: svg(
          <>
            <path d="M9 12l2 2 4-4" />
            <rect x="4" y="4" width="16" height="16" rx="3" />
          </>,
        ),
      },
      {
        tab: 'auditlog',
        needs: 'observe',
        path: '/audit-log',
        group: 'Observability',
        label: 'Audit log',
        icon: svg(
          <>
            <rect x="5" y="4" width="14" height="17" rx="1.5" />
            <path d="M9 3h6v3H9z" />
            <path d="M8 11h8M8 15h8" />
          </>,
        ),
      },
    ],
  },
];

export const FOOT_ITEMS: NavItem[] = [
  {
    tab: 'settings',
    needs: 'settings.read',
    path: '/settings',
    group: 'System',
    label: 'Settings',
    icon: svg(
      <>
        <circle cx="12" cy="12" r="3.2" />
        <path d="M12 2v3M12 19v3M4.2 4.2l2.1 2.1M17.7 17.7l2.1 2.1M2 12h3M19 12h3M4.2 19.8l2.1-2.1M17.7 6.3l2.1-2.1" />
      </>,
    ),
  },
  {
    tab: 'help',
    path: '/help',
    group: 'System',
    label: 'Help',
    icon: svg(
      <>
        <circle cx="12" cy="12" r="9" />
        <path d="M9.5 9a2.5 2.5 0 015 0c0 2-2.5 2-2.5 4" />
        <path d="M12 17h.01" />
      </>,
    ),
  },
];

export const ALL_NAV = [...NAV_GROUPS.flatMap((g) => g.items), ...FOOT_ITEMS];

/** The sidebar groups shown as one item each, by id. */
export const SECTIONS: Record<string, NavGroup & { section: NavSection }> = Object.fromEntries(
  NAV_GROUPS.filter((g): g is NavGroup & { section: NavSection } => Boolean(g.section)).map((g) => [
    g.section.id,
    g,
  ]),
);

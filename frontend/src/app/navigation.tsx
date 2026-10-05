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
  count?: 'connections' | 'queries' | 'apikeys' | 'roles' | 'alerts';
  /** The capability (adminroles.py) a role needs for this screen; hidden without it. 'data': may run SQL. */
  needs?: string;
}

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

export const NAV_GROUPS: { label: string; items: NavItem[] }[] = [
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
    label: 'Access',
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
        tab: 'administrators',
        path: '/administrators',
        group: 'Access',
        label: 'Administrators',
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

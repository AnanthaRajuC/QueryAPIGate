// The Console's information architecture (ADR 0001): organised by workflow, not by backend table. Each screen is
// either built in the Console already, or still lives in the classic UI (/ui) - `classicTab` is the /ui tab that
// has it, opened directly by the "Open in the classic UI" link. Screens move over one vertical slice at a time.
export interface NavItem {
  label: string;
  path: string;
  /** Present while the screen still lives in the classic UI. */
  classicTab?: string;
  description: string;
}

export interface NavSection {
  title?: string;
  items: NavItem[];
}

export const NAVIGATION: NavSection[] = [
  {
    items: [{ label: 'Overview', path: '/', description: 'Server status and who you are signed in as.' }],
  },
  {
    title: 'Build',
    items: [
      {
        label: 'Queries',
        path: '/queries',
        classicTab: 'queries',
        description: 'Write, test, version and publish saved queries.',
      },
      {
        label: 'APIs',
        path: '/apis',
        classicTab: 'queries',
        description: 'The endpoints your saved queries publish.',
      },
      {
        label: 'Connections',
        path: '/connections',
        classicTab: 'connections',
        description: 'Databases QueryAPIGate can reach.',
      },
    ],
  },
  {
    title: 'Explore',
    items: [
      {
        label: 'Schema',
        path: '/schema',
        classicTab: 'run',
        description: 'Browse tables and run ad-hoc SQL.',
      },
      {
        label: 'API Explorer',
        path: '/explorer',
        classicTab: 'queries',
        description: 'Try published APIs with real parameters.',
      },
      {
        label: 'Query History',
        path: '/history',
        classicTab: 'queries',
        description: 'Every run: who, when, how long, what happened.',
      },
    ],
  },
  {
    title: 'Govern',
    items: [
      {
        label: 'API Keys',
        path: '/api-keys',
        classicTab: 'apikeys',
        description: 'Create, scope and revoke keys.',
      },
      {
        label: 'Roles & Access',
        path: '/roles',
        classicTab: 'roles',
        description: 'Reusable grants for keys and signed-in users.',
      },
      {
        label: 'Policies',
        path: '/policies',
        classicTab: 'accessmap',
        description: 'Who can reach what, at a glance.',
      },
    ],
  },
  {
    title: 'Observe',
    items: [
      {
        label: 'Metrics',
        path: '/metrics',
        classicTab: 'metrics',
        description: 'Traffic, latency and errors.',
      },
      {
        label: 'Audit',
        path: '/audit',
        classicTab: 'auditlog',
        description: 'Every configuration change and who made it.',
      },
      { label: 'Events', path: '/events', classicTab: 'home', description: 'Live activity as it happens.' },
    ],
  },
  {
    title: 'AI',
    items: [
      { label: 'MCP', path: '/mcp', classicTab: 'settings', description: 'Tools exposed to AI agents.' },
    ],
  },
  {
    title: 'Admin',
    items: [
      {
        label: 'Settings',
        path: '/settings',
        classicTab: 'settings',
        description: 'Effective server configuration.',
      },
      {
        label: 'System',
        path: '/system',
        classicTab: 'caching',
        description: 'Response cache and maintenance.',
      },
    ],
  },
];

export const ALL_ITEMS: NavItem[] = NAVIGATION.flatMap((section) => section.items);

/** Open the classic UI on a given tab: /ui restores the last tab it showed from sessionStorage, so set it first. */
export function classicUiHref(tab: string): string {
  try {
    sessionStorage.setItem('queryapigate-ui-tab', tab);
  } catch {
    // storage unavailable - /ui opens on its default tab instead
  }
  return '/ui';
}

import { useQuery } from '@tanstack/react-query';
import DOMPurify from 'dompurify';
import { marked } from 'marked';
import { useEffect, useRef, useState } from 'react';

import { useHealth } from '@/app/data';
import { withAnchors, type TocEntry } from '@/lib/toc';

// The classic Help screen (ui.py #tab-help, DOCS, loadDoc, initDocsBrowser): a quick reference, and a browser over
// this project's own markdown docs, read from GitHub at the tag of the version this server runs (main for a build
// with no tag) - a pip install or the Docker image doesn't ship documentation/. The markdown is untrusted content:
// its HTML always goes through DOMPurify before it reaches the page.

const REPO = 'https://github.com/AnanthaRajuC/QueryAPIGate';

const DOCS = [
  { id: 'readme', title: 'Overview', path: 'README.md', group: 'Reference' },
  {
    id: 'install',
    title: 'Installation & Setup',
    path: 'documentation/INSTALLATION_AND_SETUP.md',
    group: 'Reference',
  },
  { id: 'api', title: 'API Reference', path: 'documentation/API.md', group: 'Reference' },
  {
    id: 'deployment',
    title: 'Production Deployment',
    path: 'documentation/DEPLOYMENT.md',
    group: 'Reference',
  },
  { id: 'mcp', title: 'MCP Server', path: 'documentation/MCP.md', group: 'Reference' },
  { id: 'examples', title: 'Examples', path: 'documentation/EXAMPLES.md', group: 'Reference' },
  { id: 'security', title: 'Security', path: 'SECURITY.md', group: 'Reference' },
  { id: 'threatmodel', title: 'Threat Model', path: 'documentation/THREAT_MODEL.md', group: 'Reference' },
  { id: 'howto-index', title: 'All how-to guides', path: 'how-to/how-to.md', group: 'How-to guides' },
  {
    id: 'howto-01',
    title: 'Your first SQL-to-API',
    path: 'how-to/01-turn-your-first-sql-query-into-a-rest-api.md',
    group: 'How-to guides',
  },
  {
    id: 'howto-02',
    title: 'Connect a database',
    path: 'how-to/02-connect-a-database.md',
    group: 'How-to guides',
  },
  {
    id: 'howto-03',
    title: 'Connect via generic JDBC',
    path: 'how-to/03-connect-via-generic-jdbc.md',
    group: 'How-to guides',
  },
  {
    id: 'howto-04',
    title: 'Connect to MongoDB',
    path: 'how-to/04-connect-to-mongodb.md',
    group: 'How-to guides',
  },
  {
    id: 'howto-05',
    title: 'Try the example APIs',
    path: 'how-to/05-try-the-built-in-example-apis.md',
    group: 'How-to guides',
  },
  {
    id: 'howto-06',
    title: 'Use bound parameters safely',
    path: 'how-to/06-use-bound-parameters-safely.md',
    group: 'How-to guides',
  },
  {
    id: 'howto-07',
    title: 'Group queries into a collection',
    path: 'how-to/07-group-queries-into-a-collection.md',
    group: 'How-to guides',
  },
  {
    id: 'howto-08',
    title: 'Cache a saved query',
    path: 'how-to/08-cache-a-saved-query.md',
    group: 'How-to guides',
  },
  {
    id: 'howto-09',
    title: 'Stream a large export',
    path: 'how-to/09-stream-a-large-export.md',
    group: 'How-to guides',
  },
  {
    id: 'howto-10',
    title: 'Allow a query to write data',
    path: 'how-to/10-allow-a-saved-query-to-write-data.md',
    group: 'How-to guides',
  },
  {
    id: 'howto-11',
    title: "Browse a connection's schema",
    path: 'how-to/11-browse-a-connections-schema.md',
    group: 'How-to guides',
  },
  {
    id: 'howto-12',
    title: 'Export a Postman collection',
    path: 'how-to/12-export-a-postman-collection.md',
    group: 'How-to guides',
  },
  {
    id: 'howto-13',
    title: 'Set up a scoped API key',
    path: 'how-to/13-set-up-a-scoped-api-key.md',
    group: 'How-to guides',
  },
  {
    id: 'howto-14',
    title: 'Give a partner one query',
    path: 'how-to/14-give-a-partner-one-query-only.md',
    group: 'How-to guides',
  },
  {
    id: 'howto-15',
    title: 'Create a role',
    path: 'how-to/15-create-a-role.md',
    group: 'How-to guides',
  },
  {
    id: 'howto-16',
    title: 'Restrict a key to tables',
    path: 'how-to/16-restrict-a-key-to-tables.md',
    group: 'How-to guides',
  },
  {
    id: 'howto-17',
    title: 'Rate-limit a key',
    path: 'how-to/17-rate-limit-a-key.md',
    group: 'How-to guides',
  },
  {
    id: 'howto-18',
    title: 'Restrict a key to IP addresses',
    path: 'how-to/18-restrict-a-key-to-ips.md',
    group: 'How-to guides',
  },
  {
    id: 'howto-19',
    title: 'Give a key an expiry date',
    path: 'how-to/19-give-a-key-an-expiry-date.md',
    group: 'How-to guides',
  },
  {
    id: 'howto-20',
    title: 'Read the audit log',
    path: 'how-to/20-read-the-audit-log.md',
    group: 'How-to guides',
  },
  {
    id: 'howto-21',
    title: 'Verify who can reach what',
    path: 'how-to/21-verify-who-can-reach-what.md',
    group: 'How-to guides',
  },
  {
    id: 'howto-22',
    title: 'Keep passwords out of the store',
    path: 'how-to/22-encrypt-passwords-at-rest.md',
    group: 'How-to guides',
  },
  {
    id: 'howto-23',
    title: 'Put it behind a reverse proxy',
    path: 'how-to/23-put-it-behind-a-reverse-proxy.md',
    group: 'How-to guides',
  },
  {
    id: 'howto-24',
    title: 'Let an agent call queries via MCP',
    path: 'how-to/24-let-an-agent-call-your-queries-via-mcp.md',
    group: 'How-to guides',
  },
  {
    id: 'howto-25',
    title: 'Let an agent run ad-hoc SQL',
    path: 'how-to/25-mcp-ad-hoc-tools.md',
    group: 'How-to guides',
  },
  {
    id: 'howto-26',
    title: 'Read structured MCP results',
    path: 'how-to/26-mcp-structured-results.md',
    group: 'How-to guides',
  },
  {
    id: 'howto-27',
    title: 'Check the MCP server',
    path: 'how-to/27-check-the-mcp-server-from-the-console.md',
    group: 'How-to guides',
  },
  {
    id: 'howto-28',
    title: 'Build a client that watches queries run',
    path: 'how-to/28-build-a-client-that-watches-queries-run.md',
    group: 'How-to guides',
  },
  {
    id: 'howto-29',
    title: 'A private activity feed per user',
    path: 'how-to/29-per-key-live-feeds.md',
    group: 'How-to guides',
  },
  {
    id: 'howto-30',
    title: 'Schedule an export',
    path: 'how-to/30-schedule-an-export.md',
    group: 'How-to guides',
  },
  {
    id: 'howto-31',
    title: 'Get the CLI command for a query',
    path: 'how-to/31-get-the-cli-command-for-a-query.md',
    group: 'How-to guides',
  },
  {
    id: 'howto-32',
    title: 'Deploy with Docker',
    path: 'how-to/32-deploy-with-docker.md',
    group: 'How-to guides',
  },
  {
    id: 'howto-33',
    title: 'Back up and restore',
    path: 'how-to/33-back-up-and-restore.md',
    group: 'How-to guides',
  },
  {
    id: 'howto-34',
    title: 'Upgrade safely',
    path: 'how-to/34-upgrade-safely.md',
    group: 'How-to guides',
  },
  {
    id: 'howto-35',
    title: 'Prometheus and Grafana',
    path: 'how-to/35-wire-up-prometheus-and-grafana.md',
    group: 'How-to guides',
  },
  {
    id: 'howto-36',
    title: 'Trace a request in the logs',
    path: 'how-to/36-trace-a-request-in-the-logs.md',
    group: 'How-to guides',
  },
  {
    id: 'howto-37',
    title: 'Tune the connection pool',
    path: 'how-to/37-tune-the-connection-pool.md',
    group: 'How-to guides',
  },
  {
    id: 'howto-38',
    title: 'Allow a browser frontend (CORS)',
    path: 'how-to/38-allow-a-browser-frontend-cors.md',
    group: 'How-to guides',
  },
  {
    id: 'howto-39',
    title: 'Read the examples as a template',
    path: 'how-to/39-walk-through-the-example-apis.md',
    group: 'How-to guides',
  },
  {
    id: 'howto-40',
    title: 'Decide how to expose it',
    path: 'how-to/40-read-the-threat-model.md',
    group: 'How-to guides',
  },
  {
    id: 'howto-41',
    title: 'Publish files in S3 as an API',
    path: 'how-to/41-publish-files-in-s3-as-an-api.md',
    group: 'How-to guides',
  },
  {
    id: 'howto-42',
    title: 'Run several instances',
    path: 'how-to/42-run-several-instances.md',
    group: 'How-to guides',
  },
  {
    id: 'howto-43',
    title: 'Give your team their own admin access',
    path: 'how-to/43-give-your-team-their-own-admin-access.md',
    group: 'How-to guides',
  },
  {
    id: 'howto-44',
    title: "Deliver a daily file to a partner's bucket",
    path: 'how-to/44-deliver-a-daily-file-to-a-partners-bucket.md',
    group: 'How-to guides',
  },
];

type Doc = (typeof DOCS)[number];

const rawUrl = (path: string, ref: string) =>
  `https://raw.githubusercontent.com/AnanthaRajuC/QueryAPIGate/${encodeURIComponent(ref)}/${path}`;

async function fetchDoc(doc: Doc, version: string | null): Promise<{ html: string; toc: TocEntry[] }> {
  const ref = version ? 'v' + version : 'main';
  let response = await fetch(rawUrl(doc.path, ref));
  if (response.status === 404 && ref !== 'main') response = await fetch(rawUrl(doc.path, 'main'));
  if (!response.ok) throw new Error('GitHub returned ' + response.status);
  return withAnchors(DOMPurify.sanitize(await marked.parse(await response.text())));
}

type View = 'quickref' | 'docs' | 'howto';

// The Docs and How-to guides tabs: each lists one group of DOCS.
const GROUP_OF: Record<Exclude<View, 'quickref'>, Doc['group']> = {
  docs: 'Reference',
  howto: 'How-to guides',
};

// Which view is open, and which doc in each docs tab, kept while the page lives - as the classic tab did.
const memory: { view: View; docs: string; howto: string } = {
  view: 'docs',
  docs: DOCS.find((d) => d.group === 'Reference')!.id,
  howto: DOCS.find((d) => d.group === 'How-to guides')!.id,
};
const remember = (patch: Partial<typeof memory>) => Object.assign(memory, patch);

export function HelpPage() {
  const [view, setViewState] = useState(memory.view);
  const setView = (next: View) => {
    remember({ view: next });
    setViewState(next);
  };
  return (
    <>
      <div className="page-head">
        <div className="titles">
          <h1>Help</h1>
          <span className="sub">
            Quick reference for this admin UI - concepts, shortcuts and where to go for more.
          </span>
        </div>
      </div>
      <div className="minitabs" id="help-view-tabs" style={{ padding: '0 0 12px' }}>
        <button
          type="button"
          className={'minitab' + (view === 'quickref' ? ' active' : '')}
          data-help-view="quickref"
          onClick={() => setView('quickref')}
        >
          Quick reference
        </button>
        <button
          type="button"
          className={'minitab' + (view === 'docs' ? ' active' : '')}
          data-help-view="docs"
          onClick={() => setView('docs')}
        >
          Docs
        </button>
        <button
          type="button"
          className={'minitab' + (view === 'howto' ? ' active' : '')}
          data-help-view="howto"
          onClick={() => setView('howto')}
        >
          How-to guides
        </button>
      </div>
      <QuickReference hidden={view !== 'quickref'} />
      {view !== 'quickref' && <DocsBrowser key={view} tab={view} />}
    </>
  );
}

function DocsBrowser({ tab }: { tab: Exclude<View, 'quickref'> }) {
  const health = useHealth();
  const docs = DOCS.filter((d) => d.group === GROUP_OF[tab]);
  const [docId, setDocState] = useState(memory[tab]);
  const setDoc = (id: string) => {
    remember({ [tab]: id });
    setDocState(id);
  };
  const doc = docs.find((d) => d.id === docId) ?? docs[0]!;
  const version = health.data?.version ?? null;
  const html = useQuery({
    queryKey: ['help', 'doc', doc.id, version],
    queryFn: () => fetchDoc(doc, version),
    enabled: !health.isPending,
    staleTime: Infinity,
    retry: false,
  });

  // "On this page": jump to a section, and mark the one being read as the page scrolls.
  const content = useRef<HTMLDivElement>(null);
  const toc = html.data?.toc ?? [];
  const [reading, setReading] = useState<{ doc: string; id: string } | null>(null);
  const current = reading?.doc === doc.id ? reading.id : null; // a new doc starts with nothing marked
  const setCurrent = (id: string) => setReading({ doc: doc.id, id });
  const goTo = (id: string) => {
    const target = content.current?.querySelector<HTMLElement>('#' + CSS.escape(id));
    if (!target || !content.current) return;
    content.current.scrollTo?.({ top: target.offsetTop - 16 });
    setCurrent(id);
  };
  const track = () => {
    const box = content.current;
    if (!box || !toc.length) return;
    const atEnd = box.scrollTop + box.clientHeight >= box.scrollHeight - 4;
    let passed = toc[0]!.id;
    for (const entry of toc) {
      const heading = box.querySelector<HTMLElement>('#' + CSS.escape(entry.id));
      if (heading && heading.offsetTop - 24 <= box.scrollTop) passed = entry.id;
    }
    setCurrent(atEnd ? toc[toc.length - 1]!.id : passed);
  };
  // The doc's own links to #a-section scroll within it, rather than changing the Console's address.
  const followAnchor = (e: React.MouseEvent) => {
    const link = (e.target as HTMLElement).closest('a');
    const href = link?.getAttribute('href') ?? '';
    if (!href.startsWith('#') || href.length < 2) return;
    e.preventDefault();
    goTo(decodeURIComponent(href.slice(1)));
  };
  useEffect(() => {
    content.current?.scrollTo?.({ top: 0 });
  }, [doc.id]);
  // A long contents list scrolls too: keep the section being read in view in it.
  const tocBox = useRef<HTMLElement>(null);
  useEffect(() => {
    const box = tocBox.current;
    const link = box?.querySelector<HTMLElement>('a.active');
    if (!box || !link) return;
    if (
      link.offsetTop < box.scrollTop ||
      link.offsetTop + link.offsetHeight > box.scrollTop + box.clientHeight
    ) {
      box.scrollTop = link.offsetTop - box.clientHeight / 3;
    }
  }, [current]);

  return (
    <div className="docs-browser" id="help-docs">
      <nav className="docs-nav" id="docs-nav">
        {docs.map((d) => (
          <button
            key={d.id}
            type="button"
            data-doc={d.id}
            className={d.id === doc.id ? 'active' : ''}
            onClick={() => setDoc(d.id)}
          >
            {d.title}
          </button>
        ))}
      </nav>
      <div className="docs-content" id="docs-content" ref={content} onScroll={track} onClick={followAnchor}>
        {html.isError ? (
          <div className="docs-error">
            {`Could not load this doc from GitHub (${html.error.message}). It needs a network connection to raw.githubusercontent.com - or read it directly at `}
            <a href={`${REPO}/blob/main/${doc.path}`} target="_blank" rel="noopener">
              github.com
            </a>
            .
          </div>
        ) : html.data === undefined ? (
          <div className="empty">Loading…</div>
        ) : (
          // sanitized by DOMPurify in fetchDoc()
          <div dangerouslySetInnerHTML={{ __html: html.data.html }} />
        )}
      </div>
      {toc.length > 1 && (
        <nav className="docs-toc" id="docs-toc" aria-label="On this page" ref={tocBox}>
          <div className="nav-label">On this page</div>
          {toc.map((entry) => (
            <a
              key={entry.id}
              href={'#' + entry.id}
              className={(entry.level === 3 ? 'sub' : '') + (entry.id === current ? ' active' : '')}
              aria-current={entry.id === current ? 'location' : undefined}
              onClick={(e) => {
                e.preventDefault();
                goTo(entry.id);
              }}
            >
              {entry.text}
            </a>
          ))}
        </nav>
      )}
    </div>
  );
}

function QuickReference({ hidden }: { hidden: boolean }) {
  return (
    <div className="help-grid" id="help-quickref" hidden={hidden}>
      <div className="panel help-card">
        <h3>Getting started</h3>
        <ol>
          <li>
            Add a <b>connection</b> to your database on the <b>Connections</b> tab.
          </li>
          <li>
            Try a query in <b>API Designer</b> - browse its schema, then Run.
          </li>
          <li>
            Click <b>Save as New API</b> to turn a working query into a saved endpoint.
          </li>
          <li>
            Create a scoped <b>API key</b> (or a <b>role</b> to create several from) with only the access it
            needs.
          </li>
          <li>
            Check the <b>Access map</b> any time to see exactly which keys can reach which queries.
          </li>
        </ol>
      </div>
      <div className="panel help-card">
        <h3>Keyboard shortcuts</h3>
        <table className="help-shortcuts">
          <tbody>
            <tr>
              <td>
                <kbd>Ctrl</kbd>/<kbd>Cmd</kbd> <kbd>K</kbd>
              </td>
              <td>Search queries, connections and keys</td>
            </tr>
            <tr>
              <td>
                <kbd>Ctrl</kbd>/<kbd>Cmd</kbd> <kbd>B</kbd>
              </td>
              <td>Collapse or expand the sidebar</td>
            </tr>
            <tr>
              <td>
                <kbd>Ctrl</kbd>/<kbd>Cmd</kbd> <kbd>Enter</kbd>
              </td>
              <td>Run the current query (API Designer)</td>
            </tr>
            <tr>
              <td>Double-click a column</td>
              <td>Turn it into a bound parameter (API Designer)</td>
            </tr>
          </tbody>
        </table>
      </div>
      <div className="panel help-card">
        <h3>Where to find things</h3>
        <dl className="help-defs">
          <dt>Data</dt>
          <dd>
            <b>Connections</b> - your databases. <b>Caching</b> - response cache settings and a live browser
            of what&apos;s cached right now.
          </dd>
          <dt>API</dt>
          <dd>
            <b>API Repository</b> - every saved query: its versions, history, curl/CLI snippets and metrics.{' '}
            <b>API Designer</b> - write and run a query, then save it as a new endpoint.
          </dd>
          <dt>Access</dt>
          <dd>
            <b>API keys</b> and <b>Roles</b> - credentials and the templates they&apos;re created from.{' '}
            <b>Access map</b> - which keys can reach which queries, at a glance.
          </dd>
          <dt>Observability</dt>
          <dd>
            <b>Metrics</b> - request volume, latency and error rate across the server. <b>Audit log</b> - who
            changed what, when.
          </dd>
        </dl>
      </div>
      <div className="panel help-card">
        <h3>Concepts</h3>
        <dl className="help-defs">
          <dt>Saved query</dt>
          <dd>
            A SQL (or Mongo find) query saved under a name, published as <code>/q/&lt;name&gt;</code>.
          </dd>
          <dt>Collection</dt>
          <dd>A named group of saved queries - the unit for granting access to many at once.</dd>
          <dt>API key vs. role</dt>
          <dd>
            A key is a real credential; a role is a template new keys can be created from - granting a role by
            itself grants nothing.
          </dd>
          <dt>Reach: Q · C · W</dt>
          <dd>
            How a key reaches a query - named <b>Q</b>uery, <b>C</b>ollection, or <b>W</b>hole connection.
          </dd>
          <dt>Bound parameters</dt>
          <dd>
            Write <code>:name</code> in a query; supply <code>name</code> as a request parameter at run time.
          </dd>
          <dt>Response cache</dt>
          <dd>
            A saved query can cache its result for a TTL, so repeat calls skip the database - see the{' '}
            <b>Caching</b> tab.
          </dd>
          <dt>CLI export</dt>
          <dd>
            Run <code>queryapigate export &lt;name&gt;</code> to pull a saved query straight to a file - no
            server or API key needed. Each query&apos;s <b>CLI</b> tab has the exact command.
          </dd>
        </dl>
      </div>
      <div className="panel help-card">
        <h3>Resources</h3>
        <ul className="help-links">
          <li>
            <a href="../docs" target="_blank" rel="noopener">
              API docs
            </a>{' '}
            - every endpoint this server exposes, generated from its own OpenAPI spec.
          </li>
          <li>
            <a href="../openapi.json" target="_blank" rel="noopener">
              OpenAPI spec
            </a>{' '}
            <span className="dim">(.json)</span>
          </li>
          <li>
            <a href="https://AnanthaRajuC.github.io/QueryAPIGate/" target="_blank" rel="noopener">
              Documentation site
            </a>
          </li>
          <li>
            <a href={`${REPO}/blob/main/documentation/DEPLOYMENT.md`} target="_blank" rel="noopener">
              Production Docker deployment guide
            </a>
          </li>
          <li>
            <a href={`${REPO}/blob/main/documentation/MCP.md`} target="_blank" rel="noopener">
              MCP server
            </a>{' '}
            <span className="dim">- let an AI assistant call your saved queries</span>
          </li>
          <li>
            <a href={REPO} target="_blank" rel="noopener">
              GitHub repository
            </a>
          </li>
          <li>
            <a href={`${REPO}/issues`} target="_blank" rel="noopener">
              Report an issue
            </a>
          </li>
          <li>
            <a href={`${REPO}/blob/main/CHANGELOG.md`} target="_blank" rel="noopener">
              Changelog
            </a>
          </li>
        </ul>
      </div>
    </div>
  );
}

import { useQuery } from '@tanstack/react-query';
import DOMPurify from 'dompurify';
import { marked } from 'marked';
import { useState } from 'react';

import { useHealth } from '@/app/data';

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
    id: 'howto-24',
    title: 'Let an agent call queries via MCP',
    path: 'how-to/24-let-an-agent-call-your-queries-via-mcp.md',
    group: 'How-to guides',
  },
  {
    id: 'howto-28',
    title: 'Build a client that watches queries run',
    path: 'how-to/28-build-a-client-that-watches-queries-run.md',
    group: 'How-to guides',
  },
];

type Doc = (typeof DOCS)[number];

const rawUrl = (path: string, ref: string) =>
  `https://raw.githubusercontent.com/AnanthaRajuC/QueryAPIGate/${encodeURIComponent(ref)}/${path}`;

async function fetchDoc(doc: Doc, version: string | null): Promise<string> {
  const ref = version ? 'v' + version : 'main';
  let response = await fetch(rawUrl(doc.path, ref));
  if (response.status === 404 && ref !== 'main') response = await fetch(rawUrl(doc.path, 'main'));
  if (!response.ok) throw new Error('GitHub returned ' + response.status);
  return DOMPurify.sanitize(await marked.parse(await response.text()));
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
      <div className="docs-content" id="docs-content">
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
          <div dangerouslySetInnerHTML={{ __html: html.data }} />
        )}
      </div>
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

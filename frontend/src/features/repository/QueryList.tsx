import { useMemo, useState } from 'react';
import { useNavigate } from 'react-router';

import {
  useAllQueries,
  useCollections,
  useConnections,
  type Connection,
  type QuerySummary,
} from '@/app/data';
import { Empty, Loading, useFeedback } from '@/app/feedback';
import { apiFetch } from '@/api/client';

import { QueryForm, RenameCollectionForm } from './forms';

// The classic API Repository list panel (ui.py #queries-panel): text filter, Type/Host/Database filters,
// Collections | Queries · <collection> mini-tabs, and the query items.

function hostLabel(c: Connection) {
  return c.host ? c.host + (c.port ? ':' + c.port : '') : '(local file)';
}

export function QueryList({ selected }: { selected: string | undefined }) {
  const navigate = useNavigate();
  const { openDrawer } = useFeedback();
  const queries = useAllQueries();
  const connections = useConnections();
  useCollections(); // loaded with the list, so collection headers' key counts are ready
  const [q, setQ] = useState('');
  const [type, setType] = useState('');
  const [host, setHost] = useState('');
  const [database, setDatabase] = useState('');
  const [view, setView] = useState<'collections' | 'queries'>('collections');
  const [activeCollection, setActiveCollection] = useState<string | null>(null);

  const conns = useMemo(() => connections.data ?? [], [connections.data]);
  const byName = useMemo(() => Object.fromEntries(conns.map((c) => [c.name, c])), [conns]);
  const list = useMemo(() => queries.data ?? [], [queries.data]);

  // Cascading filters: each narrows the next one's options (ui.py paintQueryFilters).
  const types = [...new Set(conns.map((c) => c.db))].sort();
  const hosts = [...new Set(conns.filter((c) => !type || c.db === type).map(hostLabel))].sort();
  const dbs = [
    ...new Set(
      conns
        .filter((c) => (!type || c.db === type) && (!host || hostLabel(c) === host))
        .map((c) => c.database ?? '')
        .filter(Boolean),
    ),
  ].sort();

  // Land on the selected query's own collection the first time it is known (ui.py loadQueries' first load).
  const selectedSummary = list.find((s) => s.name === selected);
  const [initialised, setInitialised] = useState(false);
  if (!initialised && selectedSummary) {
    // Adjusting state while rendering (not in an effect), once, when the selected query first becomes known.
    setInitialised(true);
    if (list.some((s) => s.collection)) {
      setView('queries');
      setActiveCollection(selectedSummary.collection ?? '');
    }
  }

  const matchesConn = (connectionName: string | null) => {
    if (!type && !host && !database) return true;
    const c = connectionName ? byName[connectionName] : undefined;
    if (!c) return false;
    return (
      (!type || c.db === type) &&
      (!host || hostLabel(c) === host) &&
      (!database || (c.database ?? '') === database)
    );
  };

  const select = (s: QuerySummary) => navigate(`/queries/${encodeURIComponent(s.name)}`);
  const item = (s: QuerySummary, showCollection = false) => (
    <button
      key={s.name}
      type="button"
      data-name={s.name}
      className={s.name === selected ? 'qitem sel' : 'qitem'}
      onClick={() => select(s)}
    >
      <div className="qi-top">
        <span className="name">{s.name}</span>
        {showCollection && s.collection ? <span className="tag">{s.collection}</span> : null}
        <span className="tag v">v{s.latest_version}</span>
      </div>
      {s.description ? <div className="qi-desc">{s.description}</div> : null}
    </button>
  );

  let body: React.ReactNode;
  let subtabs: React.ReactNode = null;
  if (queries.isPending) body = <Loading />;
  else if (queries.isError) {
    body = (
      <Empty title="Couldn’t load saved queries">
        <button type="button" className="btn sm" onClick={() => queries.refetch()}>
          Retry
        </button>
      </Empty>
    );
  } else if (!list.length) {
    body = (
      <div className="empty">
        <strong>No saved queries yet</strong>
        <span>Saved queries become GET /q/&lt;name&gt; endpoints with typed parameters.</span>
        <button
          type="button"
          className="btn primary"
          onClick={() =>
            openDrawer({ title: 'New API', kicker: 'POST /api/v1/queries', content: <QueryForm /> })
          }
        >
          New API
        </button>
      </div>
    );
  } else {
    const needle = q.trim().toLowerCase();
    const connFiltered = list.filter((s) => matchesConn(s.connection_name));
    const matched = connFiltered.filter(
      (s) =>
        !needle ||
        [s.name, s.collection, s.description, s.tags.join(' '), s.connection_name]
          .join(' ')
          .toLowerCase()
          .includes(needle),
    );
    if (!matched.length) {
      body = (
        <Empty>{needle ? `Nothing matches “${needle}”.` : 'No saved query matches these filters.'}</Empty>
      );
    } else if (needle) {
      body = matched.map((s) => item(s, true));
    } else if (!connFiltered.some((s) => s.collection)) {
      body = connFiltered.map((s) => item(s));
    } else {
      const groups: Record<string, QuerySummary[]> = {};
      connFiltered.forEach((s) => (groups[s.collection ?? ''] ??= []).push(s));
      const names = Object.keys(groups).filter(Boolean).sort();
      if (groups['']) names.push('');
      const current = activeCollection !== null && groups[activeCollection] ? activeCollection : null;
      const showQueries = view === 'queries' && current !== null;
      subtabs = (
        <div className="minitabs">
          <button
            type="button"
            className={!showQueries ? 'minitab active' : 'minitab'}
            onClick={() => setView('collections')}
          >
            Collections
          </button>
          <button
            type="button"
            className={showQueries ? 'minitab active' : 'minitab'}
            disabled={current === null}
            onClick={() => current !== null && setView('queries')}
          >
            {'Queries' + (current !== null ? ' · ' + (current || 'No collection') : '')}
          </button>
        </div>
      );
      body = showQueries ? (
        <>
          <CollectionHeader name={current!} />
          {groups[current!]!.map((s) => item(s))}
        </>
      ) : (
        names.map((c) => (
          <button
            key={c || '(none)'}
            type="button"
            className="qcoll-row"
            onClick={() => {
              setView('queries');
              setActiveCollection(c);
            }}
          >
            <span className="qg-name">{c || 'No collection'}</span>
            <span className="qg-n">{groups[c]!.length}</span>
          </button>
        ))
      );
    }
  }

  return (
    <div id="queries-panel" className="panel">
      <div className="qsearch">
        <input
          id="query-filter"
          className="search"
          type="search"
          placeholder="Filter by name, description, tag…"
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
      </div>
      <div className="qfilters">
        <select
          id="qf-type"
          aria-label="Filter by database type"
          value={type}
          onChange={(e) => setType(e.target.value)}
        >
          <option value="">All types</option>
          {types.map((t) => (
            <option key={t} value={t}>
              {t}
            </option>
          ))}
        </select>
        <select
          id="qf-host"
          aria-label="Filter by host"
          value={hosts.includes(host) ? host : ''}
          onChange={(e) => setHost(e.target.value)}
        >
          <option value="">All hosts</option>
          {hosts.map((h) => (
            <option key={h} value={h}>
              {h}
            </option>
          ))}
        </select>
        <select
          id="qf-database"
          aria-label="Filter by database"
          value={dbs.includes(database) ? database : ''}
          onChange={(e) => setDatabase(e.target.value)}
        >
          <option value="">All databases</option>
          {dbs.map((d) => (
            <option key={d} value={d}>
              {d}
            </option>
          ))}
        </select>
      </div>
      <div id="queries-subtabs">{subtabs}</div>
      <div id="queries-table">{body}</div>
    </div>
  );
}

/** Keys granted the collection, its Postman export and Rename (ui.py collectionHeader). */
function CollectionHeader({ name }: { name: string }) {
  const { openDrawer, showError, toast } = useFeedback();
  const info = useCollections();
  if (!name) return null;
  const known = info.data?.collections[name];
  const reach = known ? known.keys.length : 0;
  const postman = async () => {
    const res = await apiFetch(`/api/v1/collections/${encodeURIComponent(name)}/postman`);
    if (!res.ok) {
      showError(`Couldn't export ${name}: HTTP ${res.status}`);
      return;
    }
    const blob = await res.blob();
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `${name}.postman_collection.json`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    showError('');
    toast(`Downloaded ${name}.postman_collection.json — set its apiKey variable after importing`);
  };
  return (
    <div className="qg-foot">
      <span
        title={
          reach
            ? 'Keys granted this collection: ' + known!.keys.join(', ')
            : 'No key is granted this collection'
        }
      >
        {reach + (reach === 1 ? ' key' : ' keys')}
      </span>
      <a
        href="#"
        title="Download this collection as a Postman Collection file (one request per query; holds no API key)"
        onClick={(e) => {
          e.preventDefault();
          void postman();
        }}
      >
        Postman
      </a>
      <a
        href="#"
        title="Rename this collection, carrying every key and role grant with it"
        onClick={(e) => {
          e.preventDefault();
          openDrawer({
            title: 'Rename collection',
            kicker: name,
            content: <RenameCollectionForm name={name} />,
          });
        }}
      >
        Rename
      </a>
    </div>
  );
}

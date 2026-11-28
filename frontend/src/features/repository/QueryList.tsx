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

import { ChevronLeftIcon, FilterIcon, MoreIcon } from '@/components/icons';
import { MenuButton } from '@/components/Menu';
import { PopoverButton } from '@/components/Popover';

import { QueryForm, RenameCollectionForm } from './forms';

// The API Repository list: a text filter with a filters button (type, host, database, in a popover), then either the
// collections or one collection's queries - headed by a row to go back, the collection's name and count, and a ⋯ menu
// of what can be done with the collection (its keys, Postman export, Rename).

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

  // What each filter shows - a host or database the type filter has since ruled out shows as "All" - and how many
  // are set, for the filters button.
  const hostShown = hosts.includes(host) ? host : '';
  const databaseShown = dbs.includes(database) ? database : '';
  const active = [type, hostShown, databaseShown].filter(Boolean).length;

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
      aria-current={s.name === selected ? 'true' : undefined}
      onClick={() => select(s)}
    >
      <div className="qi-top">
        <span className="name">{s.name}</span>
        {showCollection && s.collection ? <span className="tag">{s.collection}</span> : null}
        {s.version_count > 1 ? <span className="tag v">v{s.latest_version}</span> : null}
      </div>
      {s.description ? <div className="qi-desc">{s.description}</div> : null}
    </button>
  );

  let body: React.ReactNode;
  // above the scrolling rows, so its ⋯ menu isn't clipped by them
  let head: React.ReactNode = null;
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
      if (showQueries)
        head = (
          <CollectionHeader
            name={current!}
            count={groups[current!]!.length}
            onBack={() => setView('collections')}
          />
        );
      body = showQueries
        ? groups[current!]!.map((s) => item(s))
        : names.map((c) => (
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
              <span className="qg-n">{plural(groups[c]!.length)}</span>
            </button>
          ));
    }
  }

  return (
    <div id="queries-panel">
      <div className="qsearch">
        <input
          id="query-filter"
          className="search"
          type="search"
          aria-label="Filter by name, description or tag"
          placeholder="Filter by name, description, tag…"
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
        <PopoverButton
          label="Filters: type, host, database"
          className={active ? 'btn icon qf-button on' : 'btn icon qf-button'}
          button={
            <>
              <FilterIcon />
              {active ? <span className="qf-count">{active}</span> : null}
            </>
          }
        >
          <div className="qfilters">
            <label>
              Type
              <select id="qf-type" value={type} onChange={(e) => setType(e.target.value)}>
                <option value="">All types</option>
                {types.map((t) => (
                  <option key={t} value={t}>
                    {t}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Host
              <select id="qf-host" value={hostShown} onChange={(e) => setHost(e.target.value)}>
                <option value="">All hosts</option>
                {hosts.map((h) => (
                  <option key={h} value={h}>
                    {h}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Database
              <select id="qf-database" value={databaseShown} onChange={(e) => setDatabase(e.target.value)}>
                <option value="">All databases</option>
                {dbs.map((d) => (
                  <option key={d} value={d}>
                    {d}
                  </option>
                ))}
              </select>
            </label>
            {active ? (
              <button
                type="button"
                className="btn-text"
                onClick={() => {
                  setType('');
                  setHost('');
                  setDatabase('');
                }}
              >
                Clear filters
              </button>
            ) : null}
          </div>
        </PopoverButton>
      </div>
      {head}
      <div id="queries-table">{body}</div>
    </div>
  );
}

const plural = (n: number) => `${n} ${n === 1 ? 'query' : 'queries'}`;

/** One collection's head: back to the collections, its name and how many queries it holds, and a ⋯ menu - the keys
 * granted it, its Postman export and Rename. */
function CollectionHeader({ name, count, onBack }: { name: string; count: number; onBack: () => void }) {
  const { openDrawer, showError, toast } = useFeedback();
  const info = useCollections();
  const known = name ? info.data?.collections[name] : undefined;
  const keys = known ? known.keys : [];
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
    <div className="qg-head">
      <button
        type="button"
        className="btn icon ghost"
        aria-label="Back to collections"
        title="Back to collections"
        onClick={onBack}
      >
        <ChevronLeftIcon />
      </button>
      <span className="qg-name" title={name || 'No collection'}>
        {name || 'No collection'}
      </span>
      <span className="qg-n">{plural(count)}</span>
      {name ? (
        <MenuButton
          label="Collection actions"
          className="btn icon ghost"
          entries={[
            {
              note: keys.length
                ? `Granted to ${keys.length === 1 ? '1 key' : `${keys.length} keys`}`
                : 'No key is granted this collection',
              title: keys.length ? 'Keys granted this collection: ' + keys.join(', ') : undefined,
            },
            'separator',
            {
              label: 'Download Postman collection',
              title:
                'Download this collection as a Postman Collection file (one request per query; holds no API key)',
              onSelect: () => void postman(),
            },
            {
              label: 'Rename…',
              title: 'Rename this collection, carrying every key and role grant with it',
              onSelect: () =>
                openDrawer({
                  title: 'Rename collection',
                  kicker: name,
                  content: <RenameCollectionForm name={name} />,
                }),
            },
          ]}
        >
          <MoreIcon />
        </MenuButton>
      ) : null}
    </div>
  );
}

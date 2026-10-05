import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useMemo } from 'react';
import { useNavigate, useParams } from 'react-router';

import { api, unwrap } from '@/api/client';
import { useAllQueries, useCan, useCollections, useConnections } from '@/app/data';
import { useFeedback } from '@/app/feedback';

import { NewCollectionForm, QueryForm } from './forms';
import { QueryDetail } from './QueryDetail';
import { QueryList } from './QueryList';

// The classic "API Repository" screen (ui.py #tab-queries): examples strip, page head, and the split list | detail.

export function RepositoryPage() {
  const can = useCan();
  const { name } = useParams();
  const navigate = useNavigate();
  const queries = useAllQueries();
  const { openDrawer } = useFeedback();
  const list = useMemo(() => queries.data ?? [], [queries.data]);

  // First visit: land on the first query of the first collection, as the classic screen does.
  useEffect(() => {
    if (name || !list.length) return;
    const ordered = [...list].sort((a, b) =>
      (a.collection ?? '￿') + a.name < (b.collection ?? '￿') + b.name ? -1 : 1,
    );
    navigate(`/queries/${encodeURIComponent(ordered[0]!.name)}`, { replace: true });
  }, [name, list, navigate]);

  const versions = list.reduce((n, q) => n + q.version_count, 0);
  const collections = new Set(list.map((q) => q.collection).filter(Boolean)).size;

  return (
    <>
      <ExamplesStrip />
      <div className="page-head">
        <div className="titles">
          <h1>API Repository</h1>
          <span className="sub" id="queries-sub">
            {list.length
              ? `${versions}${versions === 1 ? ' version' : ' versions'}${
                  collections
                    ? ` across ${collections}${collections === 1 ? ' collection' : ' collections'}`
                    : ''
                }. `
              : ''}
            Each one is published at <code>/q/&lt;name&gt;</code>.
          </span>
        </div>
        <span className="spacer" />
        <button
          hidden={!can('queries.write')}
          id="new-collection"
          type="button"
          className="btn"
          onClick={() =>
            openDrawer({
              title: 'New collection',
              kicker: 'file queries under a new group',
              content: <NewCollectionForm />,
            })
          }
        >
          New collection
        </button>
        <button
          hidden={!can('queries.write')}
          id="new-query"
          type="button"
          className="btn primary"
          onClick={() =>
            openDrawer({ title: 'New API', kicker: 'POST /api/v1/queries', content: <QueryForm /> })
          }
        >
          New API
        </button>
      </div>
      <div className="split">
        <QueryList selected={name} />
        <QueryDetail name={name} />
      </div>
    </>
  );
}

function ExamplesStrip() {
  const can = useCan();
  const client = useQueryClient();
  const { toast, showError } = useFeedback();
  const examples = useQuery({
    queryKey: ['examples'],
    queryFn: async () => unwrap(await api.GET('/api/v1/examples')),
    retry: false,
  });
  const collections = useCollections();
  const connections = useConnections();
  const st = examples.data;
  if (!st || (!st.loaded && !st.partial) || !can('examples.write'))
    return <div id="examples-strip" className="panel" hidden />;

  const refreshAll = () => {
    client.invalidateQueries();
    void collections.refetch();
    void connections.refetch();
  };
  const load = async () => {
    try {
      unwrap(await api.POST('/api/v1/examples'));
      showError('');
      toast('Example APIs loaded');
      refreshAll();
    } catch (e) {
      showError((e as Error).message);
    }
  };
  const remove = async () => {
    if (
      !window.confirm(
        'Remove the example APIs? This deletes exactly the queries, roles and connection marked “example” — nothing else.',
      )
    )
      return;
    try {
      const res = unwrap(await api.DELETE('/api/v1/examples'));
      toast('Example APIs removed');
      if (res.keys_still_granted.length) {
        showError(
          'These keys were granted an example collection, which no longer exists, so that grant now reaches nothing: ' +
            res.keys_still_granted.join(', '),
        );
      }
      refreshAll();
    } catch (e) {
      showError((e as Error).message);
    }
  };
  return (
    <div id="examples-strip" className="panel">
      <span className="tag example" title="Installed by the example APIs — removing the examples removes it">
        example
      </span>
      <span style={{ flex: 1 }}>
        {st.partial
          ? 'The example APIs are only partly loaded (an interrupted load).'
          : `Example APIs are loaded: ${st.queries.length} queries in ${st.collections.length} collections, ${st.roles.length}${
              st.roles.length === 1 ? ' role' : ' roles'
            } and an “examples” connection. Removing them touches nothing else.`}
      </span>
      {st.partial && (
        <button type="button" className="btn sm outlined" onClick={load}>
          Finish loading
        </button>
      )}
      <button type="button" className="btn sm outlined danger" onClick={remove}>
        Remove examples
      </button>
    </div>
  );
}

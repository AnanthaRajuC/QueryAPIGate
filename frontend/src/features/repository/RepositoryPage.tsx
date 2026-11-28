import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useId, useMemo, useState } from 'react';
import { useNavigate, useParams } from 'react-router';

import { api, unwrap } from '@/api/client';
import { useAllQueries, useCan, useCollections, useConnections } from '@/app/data';
import { useFeedback } from '@/app/feedback';

import { NewCollectionForm, QueryForm } from './forms';
import { QueryDetail } from './QueryDetail';
import { QueryList } from './QueryList';
import { PageTitle } from '@/components/PageTitle';
import { SectionActions } from '@/components/SectionActions';

// The "API Repository" screen: its actions on the section's tab row, a one-line note when the example APIs are loaded,
// and the split list | detail.

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

  return (
    <>
      <PageTitle tab>API Repository</PageTitle>
      <SectionActions>
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
      </SectionActions>
      <ExamplesStrip />
      <div className="split">
        <QueryList selected={name} />
        <QueryDetail name={name} />
      </div>
    </>
  );
}

const plural = (n: number, one: string, many = one + 's') => `${n} ${n === 1 ? one : many}`;

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
  const [open, setOpen] = useState(false);
  const detailsId = useId();
  const st = examples.data;
  if (!st || (!st.loaded && !st.partial) || !can('examples.write')) return <div id="examples-strip" hidden />;

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
        'Remove the example APIs? This deletes exactly the queries, roles, keys and connection marked “example”, the example workspace’s members, and the workspace itself if nothing else is in it — nothing else.',
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
  const included = `Example APIs are loaded: ${plural(st.queries.length, 'query', 'queries')} in ${plural(
    st.collections.length,
    'collection',
  )}, ${plural(st.roles.length, 'role')} and an “examples” connection${
    st.workspace
      ? `, and the ${st.workspace.name} workspace with ${plural(st.workspace.members.length, 'member')}`
      : ''
  }. Removing them touches nothing else.`;
  return (
    <div id="examples-strip">
      <div className="examples-line">
        <span
          className="tag example"
          title="Installed by the example APIs — removing the examples removes it"
        >
          example
        </span>
        <span>
          {st.partial
            ? 'The example APIs are only partly loaded (an interrupted load).'
            : `Example data is loaded: ${plural(st.queries.length, 'query', 'queries')} in ${plural(
                st.collections.length,
                'collection',
              )}.`}
        </span>
        {!st.partial && (
          <button
            type="button"
            className="btn-text"
            aria-expanded={open}
            aria-controls={detailsId}
            onClick={() => setOpen((o) => !o)}
          >
            What’s included
          </button>
        )}
        {st.partial && (
          <button type="button" className="btn-text" onClick={load}>
            Finish loading
          </button>
        )}
        <button type="button" className="btn-text danger" onClick={remove}>
          Remove examples
        </button>
      </div>
      {!st.partial && (
        <p id={detailsId} className="examples-included" hidden={!open}>
          {included}
        </p>
      )}
    </div>
  );
}

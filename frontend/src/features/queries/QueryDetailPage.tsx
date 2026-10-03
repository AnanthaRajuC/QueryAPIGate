import { ArrowLeft, Pencil } from 'lucide-react';
import { useMemo, useState } from 'react';
import { Link, useParams } from 'react-router';

import { ApiError } from '@/api/client';
import { columnHelper } from '@/components/data/columns';
import { DataTable } from '@/components/data/DataTable';
import { SqlEditor } from '@/components/sql/SqlEditor';
import { Alert } from '@/components/ui/alert';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardTitle } from '@/components/ui/card';
import { Tabs } from '@/components/ui/tabs';

import {
  useQueryActions,
  useQueryDetail,
  useQueryHistory,
  type HistoryEntry,
  type Query,
  type QueryVersion,
} from './api';
import { QueryStatusBadges, VersionStatusBadge } from './StatusBadges';

type TabId = 'overview' | 'versions' | 'history';

const TABS: { id: TabId; label: string }[] = [
  { id: 'overview', label: 'Overview' },
  { id: 'versions', label: 'Versions' },
  { id: 'history', label: 'Run history' },
];

/** A mutation error as the user should read it - a stale copy gets its own, actionable, message. */
function actionError(error: Error | null) {
  if (!error) return null;
  if (error instanceof ApiError && error.code === 'precondition_failed') {
    return 'Someone changed this query while you were looking at it. It has been reloaded - check it and try again.';
  }
  return error.message;
}

export function QueryDetailPage() {
  const name = useParams().name ?? '';
  const detail = useQueryDetail(name);
  const [tab, setTab] = useState<TabId>('overview');

  if (detail.isPending) return <p className="text-sm text-ink-3">Loading…</p>;
  if (detail.isError) {
    return (
      <div className="flex flex-col gap-3">
        <BackLink />
        <Alert variant="danger">
          {detail.error instanceof ApiError && detail.error.code === 'query_not_found'
            ? `There is no saved query named "${name}".`
            : `Couldn't load this query: ${detail.error.message}`}
        </Alert>
      </div>
    );
  }
  const { data: query, etag } = detail.data;

  return (
    <div className="flex max-w-5xl flex-col gap-4">
      <BackLink />
      <Header query={query} etag={etag} />
      <Tabs label="Query sections" tabs={TABS} value={tab} onChange={setTab} />
      <div role="tabpanel" id={`panel-${tab}`} aria-labelledby={`tab-${tab}`}>
        {tab === 'overview' && <Overview query={query} />}
        {tab === 'versions' && <Versions query={query} etag={etag} />}
        {tab === 'history' && <History name={query.name} />}
      </div>
    </div>
  );
}

function BackLink() {
  return (
    <Link to="/queries" className="inline-flex w-fit items-center gap-1 text-sm text-ink-2 hover:text-accent">
      <ArrowLeft size={14} aria-hidden /> Queries
    </Link>
  );
}

function Header({ query, etag }: { query: Query; etag: string | null }) {
  const actions = useQueryActions(query.name);
  const latest = query.versions[query.versions.length - 1];
  const error = actionError(actions.publish.error ?? actions.unpublish.error);

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex flex-col gap-1">
          <h1 className="font-mono text-lg font-semibold">{query.name}</h1>
          <p className="text-sm text-ink-2">{query.description}</p>
          <QueryStatusBadges query={query} />
        </div>
        <div className="flex flex-wrap gap-2">
          {query.has_draft && latest && (
            <Button
              disabled={actions.publish.isPending}
              onClick={() => actions.publish.mutate({ version: latest.version, etag })}
            >
              Publish v{latest.version}
            </Button>
          )}
          <Button variant="outline" asChild>
            <Link to="edit">
              <Pencil size={14} aria-hidden /> New version
            </Link>
          </Button>
          {query.published_version !== null && (
            <Button
              variant="outline"
              disabled={actions.unpublish.isPending}
              onClick={() => {
                if (
                  window.confirm(
                    `Stop serving ${query.endpoint}? Callers will get 404 until you publish again.`,
                  )
                ) {
                  actions.unpublish.mutate({ etag });
                }
              }}
            >
              Unpublish
            </Button>
          )}
        </div>
      </div>
      {error && <Alert variant="danger">{error}</Alert>}
    </div>
  );
}

function Overview({ query }: { query: Query }) {
  const shown =
    query.versions.find((v) => v.status === 'published') ?? query.versions[query.versions.length - 1];
  if (!shown) return null;
  const params = shown.placeholders.filter((p) => !shown.parameters[p]?.from_claim);
  const example = `curl -H 'X-API-Key: <your key>' '${globalThis.location?.origin ?? ''}${query.endpoint}${
    params.length ? '?' + params.map((p) => `${p}=…`).join('&') : ''
  }'`;

  return (
    <div className="flex flex-col gap-4 pt-4">
      {query.published_version === null && (
        <Alert variant="warn">
          Not published: {query.endpoint} answers 404. Publish a version to make it available.
        </Alert>
      )}
      <Card className="flex flex-col gap-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <CardTitle>
            {shown.status === 'published' ? 'Published version' : 'Newest draft'} (v{shown.version})
          </CardTitle>
          <span className="text-xs text-ink-3">
            {shown.connection_name ? (
              <>
                on <span className="font-mono">{shown.connection_name}</span>
              </>
            ) : (
              'no default connection'
            )}
          </span>
        </div>
        {shown.query_type === 'sql' ? (
          <SqlEditor
            value={shown.sql ?? ''}
            readOnly
            label={`SQL of version ${shown.version}`}
            minHeight="4rem"
          />
        ) : (
          <pre className="overflow-x-auto rounded-md bg-surface-2 p-3 font-mono text-xs">
            {JSON.stringify(shown.mongo, null, 2)}
          </pre>
        )}
        <ParametersTable version={shown} />
      </Card>
      <Card className="flex flex-col gap-2">
        <CardTitle>Call it</CardTitle>
        <p className="text-sm text-ink-2">
          <span className="font-mono">GET {query.endpoint}</span> - also as CSV, NDJSON, XML or XLSX with{' '}
          <span className="font-mono">?format=</span>.
        </p>
        <pre className="overflow-x-auto rounded-md bg-surface-2 p-3 font-mono text-xs">{example}</pre>
      </Card>
    </div>
  );
}

function ParametersTable({ version }: { version: QueryVersion }) {
  if (version.placeholders.length === 0) return <p className="text-sm text-ink-3">No parameters.</p>;
  return (
    <table className="w-full text-left text-sm" aria-label="Parameters">
      <thead className="text-xs text-ink-2">
        <tr>
          <th className="py-1 pr-3 font-medium">Parameter</th>
          <th className="py-1 pr-3 font-medium">Type</th>
          <th className="py-1 pr-3 font-medium">Rules</th>
        </tr>
      </thead>
      <tbody>
        {version.placeholders.map((name) => {
          const rule = version.parameters[name];
          const rules = [
            rule?.from_claim
              ? `from token claim "${rule.from_claim}"`
              : rule?.required === false
                ? 'optional'
                : 'required',
            rule && 'default' in rule ? `default ${JSON.stringify(rule.default)}` : null,
            rule?.enum ? `one of ${rule.enum.map((v) => JSON.stringify(v)).join(', ')}` : null,
            rule?.min != null ? `≥ ${rule.min}` : null,
            rule?.max != null ? `≤ ${rule.max}` : null,
            rule?.pattern ? `matches ${rule.pattern}` : null,
          ].filter(Boolean);
          return (
            <tr key={name} className="border-t border-line">
              <td className="py-1 pr-3 font-mono">{name}</td>
              <td className="py-1 pr-3">{rule?.type ?? 'any'}</td>
              <td className="py-1 pr-3 text-ink-2">{rules.join(' · ')}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

const versionCol = columnHelper<QueryVersion>();

function Versions({ query, etag }: { query: Query; etag: string | null }) {
  const actions = useQueryActions(query.name);
  const error = actionError(actions.publish.error ?? actions.deleteVersion.error);
  const busy = actions.publish.isPending || actions.deleteVersion.isPending;
  const columns = useMemo(
    () =>
      versionCol.columns([
        versionCol.accessor('version', { header: 'Version', cell: (info) => `v${info.getValue()}` }),
        versionCol.accessor('status', {
          header: 'Status',
          cell: (info) => <VersionStatusBadge status={info.getValue()} />,
        }),
        versionCol.accessor('description', { header: 'Description' }),
        versionCol.accessor('author', { header: 'Author', cell: (info) => info.getValue() ?? '—' }),
        versionCol.accessor('created_at', {
          header: 'Created',
          cell: (info) => <span className="text-xs whitespace-nowrap">{info.getValue() ?? '—'}</span>,
        }),
        versionCol.display({
          id: 'actions',
          header: 'Actions',
          cell: ({ row }) => {
            const v = row.original;
            return (
              <div className="flex flex-wrap gap-1">
                {v.status !== 'published' && (
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={busy}
                    onClick={() => actions.publish.mutate({ version: v.version, etag })}
                  >
                    {v.status === 'draft' ? 'Publish' : 'Roll back to this'}
                  </Button>
                )}
                <Button size="sm" variant="ghost" asChild>
                  <Link to={`edit?from=${v.version}`}>Edit from this</Link>
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  disabled={busy}
                  onClick={() => {
                    if (
                      window.confirm(`Delete version ${v.version} of ${query.name}? This can't be undone.`)
                    ) {
                      actions.deleteVersion.mutate({ version: v.version, etag });
                    }
                  }}
                >
                  Delete
                </Button>
              </div>
            );
          },
        }),
      ]),
    [actions.publish, actions.deleteVersion, busy, etag, query.name],
  );
  const data = useMemo(() => [...query.versions].reverse(), [query.versions]);
  return (
    <div className="flex flex-col gap-3 pt-4">
      {error && <Alert variant="danger">{error}</Alert>}
      <DataTable label="Versions" columns={columns} data={data} />
    </div>
  );
}

const historyCol = columnHelper<HistoryEntry>();
const historyColumns = historyCol.columns([
  historyCol.accessor('executed_at', {
    header: 'Time',
    cell: (info) => <span className="text-xs whitespace-nowrap">{info.getValue()}</span>,
  }),
  historyCol.accessor('version', { header: 'Version', cell: (info) => `v${info.getValue()}` }),
  historyCol.accessor('status', {
    header: 'Result',
    cell: (info) =>
      info.getValue() === 'error' ? (
        <Badge variant="danger" title={info.row.original.error ?? undefined}>
          Error
        </Badge>
      ) : (
        <Badge variant="accent">OK</Badge>
      ),
  }),
  historyCol.accessor('key_name', { header: 'Caller', cell: (info) => info.getValue() ?? '—' }),
  historyCol.accessor('rows', { header: 'Rows', cell: (info) => info.getValue() ?? '—' }),
  historyCol.accessor('duration_ms', {
    header: 'Duration',
    cell: (info) => (info.getValue() != null ? `${Math.round(info.getValue()!)} ms` : '—'),
  }),
  historyCol.accessor('request_id', {
    header: 'Request ID',
    enableSorting: false,
    cell: (info) => <span className="font-mono text-xs">{info.getValue() ?? '—'}</span>,
  }),
]);

function History({ name }: { name: string }) {
  const history = useQueryHistory(name);
  const entries = useMemo(() => history.data?.pages.flatMap((page) => page.items) ?? [], [history.data]);
  if (history.isPending) return <p className="pt-4 text-sm text-ink-3">Loading…</p>;
  if (history.isError) return <Alert variant="danger">{history.error.message}</Alert>;
  return (
    <div className="flex flex-col gap-3 pt-4">
      <DataTable label="Run history" columns={historyColumns} data={entries} empty="No runs recorded yet." />
      {history.hasNextPage && (
        <Button
          variant="outline"
          className="self-center"
          disabled={history.isFetchingNextPage}
          onClick={() => history.fetchNextPage()}
        >
          Load older runs
        </Button>
      )}
    </div>
  );
}

import { Plus, Search } from 'lucide-react';
import { useDeferredValue, useMemo, useState } from 'react';
import { Link } from 'react-router';

import { columnHelper } from '@/components/data/columns';
import { DataTable } from '@/components/data/DataTable';
import { Alert } from '@/components/ui/alert';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Select } from '@/components/ui/form';
import { Input } from '@/components/ui/input';

import { useQueryList, type QuerySummary, type StatusFilter } from './api';
import { QueryStatusBadges } from './StatusBadges';

const col = columnHelper<QuerySummary>();

const columns = col.columns([
  col.accessor('name', {
    header: 'Name',
    cell: (info) => (
      <div className="flex flex-col">
        <Link to={encodeURIComponent(info.getValue())} className="font-medium text-accent hover:underline">
          {info.getValue()}
        </Link>
        <span className="line-clamp-1 text-xs text-ink-3">{info.row.original.description}</span>
      </div>
    ),
  }),
  col.accessor('published_version', {
    header: 'Status',
    cell: (info) => <QueryStatusBadges query={info.row.original} />,
    enableSorting: false,
  }),
  col.accessor('connection_name', {
    header: 'Connection',
    cell: (info) => <span className="font-mono text-xs">{info.getValue() ?? '—'}</span>,
  }),
  col.accessor('collection', {
    header: 'Collection',
    cell: (info) =>
      info.getValue() ? <Badge>{info.getValue()}</Badge> : <span className="text-ink-3">—</span>,
  }),
  col.accessor('version_count', { header: 'Versions' }),
  col.accessor('updated_at', {
    header: 'Updated',
    cell: (info) => <span className="text-xs whitespace-nowrap text-ink-2">{info.getValue() ?? '—'}</span>,
  }),
]);

const STATUS_OPTIONS: { value: StatusFilter | ''; label: string }[] = [
  { value: '', label: 'All statuses' },
  { value: 'published', label: 'Published' },
  { value: 'draft', label: 'Has a draft' },
  { value: 'unpublished', label: 'Not published' },
];

export function QueriesListPage() {
  const [search, setSearch] = useState('');
  const [status, setStatus] = useState<StatusFilter | ''>('');
  const deferredSearch = useDeferredValue(search);
  const list = useQueryList(deferredSearch.trim(), status);
  const data = useMemo(() => list.data ?? [], [list.data]);

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold">Queries</h1>
          <p className="text-sm text-ink-2">Saved SQL, published as API endpoints.</p>
        </div>
        <Button asChild>
          <Link to="new">
            <Plus size={16} aria-hidden /> New query
          </Link>
        </Button>
      </div>

      <div className="flex flex-wrap gap-2">
        <div className="relative min-w-56 flex-1">
          <Search size={14} aria-hidden className="absolute top-1/2 left-3 -translate-y-1/2 text-ink-3" />
          <Input
            type="search"
            aria-label="Search queries"
            placeholder="Search by name, description or tag"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            className="pl-8"
          />
        </div>
        <Select
          aria-label="Filter by status"
          value={status}
          onChange={(event) => setStatus(event.target.value as StatusFilter | '')}
          className="w-44"
        >
          {STATUS_OPTIONS.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </Select>
      </div>

      {list.isError ? (
        <Alert variant="danger">Couldn&apos;t load queries: {list.error.message}</Alert>
      ) : list.isPending ? (
        <p className="text-sm text-ink-3">Loading…</p>
      ) : (
        <DataTable
          label="Saved queries"
          columns={columns}
          data={data}
          empty={search || status ? 'No queries match these filters.' : 'No saved queries yet.'}
        />
      )}
    </div>
  );
}

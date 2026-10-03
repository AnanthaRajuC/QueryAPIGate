import { Badge } from '@/components/ui/badge';

import type { QuerySummary, QueryVersion } from './api';

/** Whether callers can reach a query, and whether unpublished changes are waiting. */
export function QueryStatusBadges({
  query,
}: {
  query: Pick<QuerySummary, 'published_version' | 'has_draft'>;
}) {
  return (
    <span className="inline-flex flex-wrap gap-1">
      {query.published_version !== null ? (
        <Badge variant="accent">Published v{query.published_version}</Badge>
      ) : (
        <Badge>Not published</Badge>
      )}
      {query.has_draft && query.published_version !== null && <Badge variant="warn">Draft pending</Badge>}
    </span>
  );
}

const VERSION_LABEL: Record<QueryVersion['status'], string> = {
  published: 'Published',
  draft: 'Draft',
  previous: 'Previous',
};

export function VersionStatusBadge({ status }: { status: QueryVersion['status'] }) {
  return (
    <Badge variant={status === 'published' ? 'accent' : status === 'draft' ? 'warn' : 'default'}>
      {VERSION_LABEL[status]}
    </Badge>
  );
}

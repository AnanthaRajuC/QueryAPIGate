import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useState } from 'react';
import { useNavigate, useParams } from 'react-router';

import { ApiError, api, unwrap, type Schemas } from '@/api/client';
import { useGate } from '@/app/data';
import { Field, Loading, useFeedback } from '@/app/feedback';
import { CodeBox } from '@/components/CodeBox';
import { Time } from '@/components/Time';

// Writes AI agents asked to make over MCP (ADR 0008): a person approves or rejects each one before it runs. The link an
// agent is given - /console/approvals/wr_... - opens that request here.

type WriteRequest = Schemas['WriteRequest'];

const STATUS_LABEL: Record<string, string> = {
  pending: 'Waiting',
  approved: 'Approved - not run yet',
  running: 'Running',
  done: 'Done',
  failed: 'Failed',
  rejected: 'Rejected',
  expired: 'Expired',
};

function useRequests(status: string) {
  const gate = useGate('writes.approve', 'write requests');
  return useQuery({
    queryKey: ['write-requests', status || 'all'],
    queryFn: async () => {
      await gate.check();
      return unwrap(await api.GET('/api/v1/write-requests', { params: { query: status ? { status } : {} } }))
        .items;
    },
    enabled: gate.ready,
    retry: false,
  });
}

const compact = (value: unknown) => JSON.stringify(value);

function Arguments({ args }: { args: Record<string, unknown> }) {
  const entries = Object.entries(args);
  if (!entries.length) return <span className="dim">none</span>;
  return (
    <table className="grid approval-args">
      <tbody>
        {entries.map(([name, value]) => (
          <tr key={name}>
            <td className="mono">{name}</td>
            <td className="mono">{compact(value)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function Detail({ id }: { id: string }) {
  const { closeDrawer, showError, toast } = useFeedback();
  const client = useQueryClient();
  const [reason, setReason] = useState('');
  const request = useQuery({
    queryKey: ['write-requests', 'one', id],
    queryFn: async () => unwrap(await api.GET('/api/v1/write-requests/{id}', { params: { path: { id } } })),
    retry: false,
  });
  const decide = useMutation({
    mutationFn: async (approve: boolean) =>
      approve
        ? unwrap(await api.POST('/api/v1/write-requests/{id}/approve', { params: { path: { id } } }))
        : unwrap(
            await api.POST('/api/v1/write-requests/{id}/reject', {
              params: { path: { id } },
              body: reason.trim() ? { reason: reason.trim() } : {},
            }),
          ),
    onSuccess: (decided) => {
      showError('');
      void client.invalidateQueries({ queryKey: ['write-requests'] });
      void client.invalidateQueries({ queryKey: ['alerts'] });
      closeDrawer();
      toast(decided.status === 'approved' ? `Approved - ${decided.caller} may run it now` : 'Rejected');
    },
    onError: (error: Error) =>
      showError(error.message, error instanceof ApiError ? { status: error.status } : {}),
  });

  if (request.isPending) return <Loading />;
  if (request.isError) return <div className="hint">{request.error.message}</div>;
  const r = request.data;
  const pending = r.status === 'pending';
  return (
    <div className="form">
      <p className="hint">
        <strong>{r.caller}</strong> asked to run <strong>{r.query}</strong> (version {r.version}) on{' '}
        <span className="mono">{r.connection}</span>. It changes at most {r.max_affected_rows} row
        {r.max_affected_rows === 1 ? '' : 's'}; past that it is rolled back.
      </p>
      <Field label="SQL">
        <CodeBox sql={r.sql} />
      </Field>
      <Field label="With exactly these arguments" hint="An approval is for these alone - no other write.">
        <Arguments args={r.arguments as Record<string, unknown>} />
      </Field>
      <div className="hint">
        Asked <Time value={r.created_at} /> · {STATUS_LABEL[r.status] ?? r.status}
        {pending || r.status === 'approved' ? (
          <>
            {' '}
            · expires <Time value={r.expires_at} />
          </>
        ) : null}
        {r.decided_by ? ` · ${r.status === 'rejected' ? 'rejected' : 'approved'} by ${r.decided_by}` : ''}
        {r.reason ? ` - ${r.reason}` : ''}
      </div>
      {r.result ? (
        <Field label="Result">
          <span className="mono">{compact(r.result)}</span>
        </Field>
      ) : null}
      {pending ? (
        <>
          <Field id="approval-reason" label="Reason" hint="Optional for a rejection - the agent is told.">
            <textarea
              id="approval-reason"
              rows={2}
              value={reason}
              onChange={(e) => setReason(e.target.value)}
            />
          </Field>
          <div className="form-actions">
            <button
              type="button"
              className="btn primary"
              disabled={decide.isPending}
              onClick={() => decide.mutate(true)}
            >
              Approve
            </button>
            <button
              type="button"
              className="btn danger"
              disabled={decide.isPending}
              onClick={() => decide.mutate(false)}
            >
              Reject
            </button>
            <button type="button" className="btn" onClick={closeDrawer}>
              Close
            </button>
          </div>
        </>
      ) : (
        <div className="form-actions">
          <button type="button" className="btn" onClick={closeDrawer}>
            Close
          </button>
        </div>
      )}
    </div>
  );
}

export function ApprovalsPage() {
  const { id } = useParams();
  const navigate = useNavigate();
  const { openDrawer } = useFeedback();
  const [status, setStatus] = useState('pending');
  const requests = useRequests(status);
  const items: WriteRequest[] = requests.data ?? [];

  const open = (requestId: string) =>
    openDrawer({
      title: 'Approve a write',
      kicker: requestId,
      wide: true,
      content: <Detail id={requestId} />,
    });

  useEffect(() => {
    if (id) {
      open(id);
      navigate('/approvals', { replace: true });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- the link opens its request once
  }, [id]);

  let body: React.ReactNode;
  if (requests.isPending) body = <Loading text="Loading write requests…" />;
  else if (requests.isError)
    body = (
      <div className="empty">
        <strong>Couldn’t load write requests</strong>
        <span>{requests.error.message}</span>
      </div>
    );
  else if (!items.length)
    body = (
      <div className="empty">
        <strong>
          {status === 'pending' ? 'No writes are waiting for approval' : 'No write requests yet'}
        </strong>
        <span>
          When an AI agent calls a write tool, its write waits here for a person to approve it - the agent is
          given a link to this screen.
        </span>
      </div>
    );
  else
    body = (
      <div style={{ overflowX: 'auto' }}>
        <table className="grid">
          <thead>
            <tr>
              {['Asked', 'Agent', 'Tool', 'Arguments', status === 'pending' ? 'Expires' : 'Status'].map(
                (t) => (
                  <th key={t}>{t}</th>
                ),
              )}
            </tr>
          </thead>
          <tbody>
            {items.map((r) => (
              <tr key={r.id} data-request={r.id}>
                <td className="mono dim" style={{ whiteSpace: 'nowrap' }}>
                  <Time value={r.created_at} fallback="" />
                </td>
                <td className="mono">{r.caller}</td>
                <td>
                  <button type="button" className="agent-name" onClick={() => open(r.id)}>
                    {r.query}
                  </button>
                </td>
                <td className="mono dim approval-args-cell">{compact(r.arguments)}</td>
                <td className="dim" style={{ whiteSpace: 'nowrap' }}>
                  {status === 'pending' ? (
                    <Time value={r.expires_at} />
                  ) : (
                    (STATUS_LABEL[r.status] ?? r.status)
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    );

  return (
    <>
      <div className="page-head">
        <div className="titles">
          <h1>Approvals</h1>
          <span className="sub">
            Writes AI agents asked to make over MCP. Each runs only once a person approves it - with exactly
            the arguments shown.
          </span>
        </div>
        <span className="spacer" />
        <button type="button" className="btn" onClick={() => void requests.refetch()}>
          Refresh
        </button>
      </div>
      <div className="panel" style={{ overflow: 'hidden' }}>
        <div className="panel-bar">
          <select aria-label="Show" value={status} onChange={(e) => setStatus(e.target.value)}>
            <option value="pending">Waiting</option>
            <option value="">All</option>
            <option value="done">Done</option>
            <option value="rejected">Rejected</option>
            <option value="expired">Expired</option>
            <option value="failed">Failed</option>
          </select>
          <span className="spacer" />
          <span className="panel-count">
            {requests.data ? `${items.length} request${items.length === 1 ? '' : 's'}` : ''}
          </span>
        </div>
        {body}
      </div>
    </>
  );
}

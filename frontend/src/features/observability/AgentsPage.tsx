import { useQuery } from '@tanstack/react-query';
import { Fragment, useState } from 'react';

import { api, unwrap, type Schemas } from '@/api/client';
import { Loading } from '@/app/feedback';
import { CodeBox } from '@/components/CodeBox';
import { Time } from '@/components/Time';

// What each AI agent did over MCP (GET /api/v1/usage/agents, BACKLOG #87): calls, rows read, refusals by code, how
// often masking applied, and - opened - its saved queries and most frequent ad-hoc statements. From run history.

type Agent = Schemas['AgentUsage'];

const WINDOWS = [
  { id: '1h', label: 'Last hour', hours: 1 },
  { id: '24h', label: 'Last 24 hours', hours: 24 },
  { id: '7d', label: 'Last 7 days', hours: 24 * 7 },
  { id: '30d', label: 'Last 30 days', hours: 24 * 30 },
];

const n = (value: number) => value.toLocaleString();

function Refusals({ refusals }: { refusals: Agent['refusals'] }) {
  const entries = Object.entries(refusals);
  if (!entries.length) return <span className="dim">none</span>;
  return (
    <span style={{ display: 'inline-flex', flexWrap: 'wrap', gap: 4 }}>
      {entries.map(([code, count]) => (
        <span key={code} className="tag" title={`${count} call${count === 1 ? '' : 's'} failed with ${code}`}>
          {`${code} × ${count}`}
        </span>
      ))}
    </span>
  );
}

function Writes({ writes }: { writes: Agent['writes'] }) {
  const parts = Object.entries(writes ?? {})
    .filter(([, count]) => count)
    .map(([outcome, count]) => `${count} ${outcome}`);
  return <div className="hint">{parts.length ? parts.join(' · ') : 'None.'}</div>;
}

function Detail({ agent }: { agent: Agent }) {
  const queries = Object.entries(agent.queries);
  return (
    <div className="agent-detail">
      <p className="sub-h">Writes asked for</p>
      <Writes writes={agent.writes} />
      <p className="sub-h" style={{ marginTop: 12 }}>
        Saved queries
      </p>
      {queries.length ? (
        <div className="hint">{queries.map(([name, count]) => `${name} × ${count}`).join(' · ')}</div>
      ) : (
        <div className="hint">None - only ad-hoc SQL.</div>
      )}
      <p className="sub-h" style={{ marginTop: 12 }}>
        Most frequent ad-hoc statements
      </p>
      {agent.top_statements.length ? (
        agent.top_statements.map((s, i) => (
          <div key={i} className="agent-statement">
            <div className="hint">
              {`${s.count} call${s.count === 1 ? '' : 's'} · ${n(s.rows)} rows` +
                (s.errors ? ` · ${s.errors} failed` : '')}
            </div>
            {s.sql ? <CodeBox sql={s.sql} /> : <span className="mono dim">{`sha256 ${s.sql_sha256}`}</span>}
          </div>
        ))
      ) : (
        <div className="hint">
          {agent.adhoc_calls
            ? 'Run history keeps no SQL text here (QUERYAPIGATE_HISTORY_ADHOC_SQL is none).'
            : 'No ad-hoc SQL.'}
        </div>
      )}
    </div>
  );
}

export function AgentsPage() {
  const [window, setWindow] = useState('24h');
  const [open, setOpen] = useState<string | null>(null);
  const hours = WINDOWS.find((w) => w.id === window)!.hours;
  const usage = useQuery({
    queryKey: ['usage', 'agents', window],
    queryFn: async () => unwrap(await api.GET('/api/v1/usage/agents', { params: { query: { hours } } })), // the server's clock
    retry: false,
  });
  const items = usage.data?.items ?? [];

  let body: React.ReactNode;
  if (usage.isPending) body = <Loading text="Loading agents…" />;
  else if (usage.isError)
    body = (
      <div className="empty">
        <strong>Couldn’t load agent usage</strong>
        <span>{usage.error.message}</span>
      </div>
    );
  else if (!items.length)
    body = (
      <div className="empty">
        <strong>No agent called the MCP server in this window</strong>
        <span>
          Runs over MCP - saved-query tools and execute_sql - appear here, per API key or signed-in user.
        </span>
      </div>
    );
  else
    body = (
      <div style={{ overflowX: 'auto' }}>
        <table className="grid">
          <thead>
            <tr>
              {['Agent', 'Calls', 'Rows', 'Failed', 'Masked', 'Refused or failed with', 'Last seen'].map(
                (t) => (
                  <th key={t}>{t}</th>
                ),
              )}
            </tr>
          </thead>
          <tbody>
            {items.map((a) => (
              <Fragment key={a.agent}>
                <tr>
                  <td>
                    <button
                      type="button"
                      className="agent-name"
                      aria-expanded={open === a.agent}
                      onClick={() => setOpen(open === a.agent ? null : a.agent)}
                    >
                      {a.agent}
                    </button>
                    <div className="dim" style={{ fontSize: 12 }}>
                      {a.connections.join(', ')}
                    </div>
                  </td>
                  <td className="num">{n(a.calls)}</td>
                  <td className="num">{n(a.rows)}</td>
                  <td className="num">{n(a.errors)}</td>
                  <td className="num">{n(a.masked_calls)}</td>
                  <td>
                    <Refusals refusals={a.refusals} />
                  </td>
                  <td className="mono dim" style={{ whiteSpace: 'nowrap' }}>
                    <Time value={a.last_seen} fallback="" />
                  </td>
                </tr>
                {open === a.agent && (
                  <tr>
                    <td colSpan={7}>
                      <Detail agent={a} />
                    </td>
                  </tr>
                )}
              </Fragment>
            ))}
          </tbody>
        </table>
      </div>
    );

  const notes: string[] = [];
  if (usage.data && usage.data.sample_rate < 1)
    notes.push(
      `Run history keeps ${Math.round(usage.data.sample_rate * 100)}% of successful runs, so successful calls and ` +
        'rows are a sample; failures are all here.',
    );
  if (usage.data?.truncated) notes.push('More runs than one summary reads (100,000) - narrow the window.');

  return (
    <>
      <div className="page-head">
        <div className="titles">
          <h1>Agents</h1>
          <span className="sub">
            What each AI agent did over MCP - per API key or signed-in user, from run history.
          </span>
        </div>
        <span className="spacer" />
        <button type="button" className="btn" onClick={() => void usage.refetch()}>
          Refresh
        </button>
      </div>
      <div className="panel" style={{ overflow: 'hidden' }}>
        <div className="panel-bar">
          <select aria-label="Window" value={window} onChange={(e) => setWindow(e.target.value)}>
            {WINDOWS.map((w) => (
              <option key={w.id} value={w.id}>
                {w.label}
              </option>
            ))}
          </select>
          <span className="spacer" />
          <span className="panel-count">
            {usage.data ? `${items.length} agent${items.length === 1 ? '' : 's'}` : ''}
          </span>
        </div>
        {notes.map((note) => (
          <div key={note} className="hint" style={{ padding: '6px 12px' }}>
            {note}
          </div>
        ))}
        {body}
      </div>
    </>
  );
}

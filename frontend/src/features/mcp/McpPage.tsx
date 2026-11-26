import { useMutation, useQuery } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import { useSearchParams } from 'react-router';

import { api, unwrap, type Schemas } from '@/api/client';
import { Loading } from '@/app/feedback';
import { NoMatches, SortHeader, TableFilter } from '@/components/TableControls';
import { useTableControls, type Column } from '@/components/useTableControls';
import { ActivitySpark } from '@/features/access/KeyActivity';
import { useQueryActivityMap } from '@/features/access/activity';

// The MCP server screen: your saved queries, exposed as tools an AI agent can call over MCP (ADR 0006). Three tabs:
// how to connect a client, a reachability check (run only when asked), and the tools a client would see in this
// workspace. The tab is in the URL (?tab=), so a link can open one.
// The MCP server's own settings (QUERYAPIGATE_MCP_*) stay under Settings; this screen is the operational view.
// GET /api/v1/mcp/tools and /api/v1/mcp/status both need the `observe` capability, like Agents and the audit log.

type Tool = Schemas['McpToolList']['items'][number];
type TabId = 'connect' | 'reachability' | 'tools';
const TABS: TabId[] = ['connect', 'reachability', 'tools'];

const TOOL_COLUMNS: Column<Tool>[] = [
  { key: 'name', label: 'Name', sort: (t) => t.name },
  { key: 'kind', label: 'Kind', sort: (t) => t.kind },
  { key: 'params', label: 'Parameters', sort: (t) => t.params.length || null },
  { key: 'description', label: 'Description', sort: (t) => t.description },
  { key: 'activity', label: 'MCP activity (30d)' },
];

const toolText = (t: Tool) => [t.name, t.kind, t.read_only ? 'read-only' : null, ...t.params, t.description];

const CONFIG = `{
  "mcpServers": {
    "queryapigate": {
      "url": "http://localhost:5001/mcp",
      "headers": { "X-API-Key": "<a scoped API key>" }
    }
  }
}`;

export function McpPage() {
  const [params, setParams] = useSearchParams();
  const asked = params.get('tab') as TabId | null;
  const tab: TabId = asked && TABS.includes(asked) ? asked : 'connect';
  const tools = useQuery({
    queryKey: ['mcp', 'tools'],
    queryFn: async () => unwrap(await api.GET('/api/v1/mcp/tools')).items,
    retry: false,
  });
  const check = useMutation({
    mutationFn: async () => unwrap(await api.GET('/api/v1/mcp/status')),
  });
  const activity = useQueryActivityMap();
  const status = check.data;
  const dot = check.isPending ? 'checking' : !status ? 'unknown' : status.reachable ? 'ok' : 'bad';
  const dotText = check.isPending
    ? 'Checking…'
    : !status
      ? 'Not checked yet'
      : (status.reachable ? 'Reachable on port ' : 'Not reachable on port ') + status.port;
  const list: Tool[] = tools.data ?? [];
  const table = useTableControls(list, { columns: TOOL_COLUMNS, text: toolText });

  const tabButton = (id: TabId, label: string, extra?: ReactNode) => (
    <button
      type="button"
      role="tab"
      id={'mcp-tab-' + id}
      data-subtab={id}
      aria-selected={tab === id}
      aria-controls={'mcp-' + id}
      className={tab === id ? 'on' : ''}
      onClick={() => setParams(id === 'connect' ? {} : { tab: id }, { replace: true })}
    >
      {label}
      {extra}
    </button>
  );

  return (
    <>
      <div className="page-head">
        <div className="titles">
          <h1>MCP server</h1>
          <span className="sub">
            Your saved queries, as tools an AI agent can call over MCP. Each key sees only its own
            workspace&apos;s tools.
          </span>
        </div>
        <span className="spacer" />
        <button type="button" className="btn" onClick={() => void tools.refetch()}>
          Refresh
        </button>
      </div>

      <div className="panel mcp-panel">
        <div className="subtabs" role="tablist" aria-label="MCP server">
          {tabButton('connect', 'Connect a client')}
          {tabButton(
            'reachability',
            'Reachability',
            status || check.isPending ? <span className={'dot ' + dot} /> : null,
          )}
          {tabButton('tools', 'Tools', tools.data ? <span className="count">{list.length}</span> : null)}
        </div>

        {tab === 'connect' && (
          <section className="mcp-tab" id="mcp-connect" role="tabpanel" aria-labelledby="mcp-tab-connect">
            <p className="mcp-intro">Any MCP client that speaks streamable HTTP.</p>
            <p className="hint">
              Start the MCP server with <code>queryapigate mcp</code>, then point a client at its URL with a
              scoped API key as the <code>X-API-Key</code> header. In Claude Desktop, that is one entry in its
              MCP-servers config:
            </p>
            <pre className="curlbox">{CONFIG}</pre>
          </section>
        )}

        {tab === 'reachability' && (
          <section
            className="mcp-tab"
            id="mcp-reachability"
            role="tabpanel"
            aria-labelledby="mcp-tab-reachability"
          >
            <p className="mcp-intro">A plain TCP connect to the configured MCP port, only when you ask.</p>
            <div className="set-row">
              <div className="set-what">
                <span className={'dot ' + dot} />
                <span className="set-label">{dotText}</span>
              </div>
              <button type="button" className="btn" disabled={check.isPending} onClick={() => check.mutate()}>
                Check now
              </button>
            </div>
          </section>
        )}

        {tab === 'tools' && (
          <section className="mcp-tab" id="mcp-tools" role="tabpanel" aria-labelledby="mcp-tab-tools">
            <div className="mcp-intro-row">
              <p className="mcp-intro">
                What tools/list returns for an unrestricted caller in this workspace.
              </p>
              {list.length > 0 && (
                <TableFilter
                  controls={table}
                  id="mcp-tools-filter"
                  placeholder="Filter tools…"
                  label="Filter tools"
                />
              )}
            </div>
            {tools.isPending ? (
              <Loading text="Loading tools…" />
            ) : tools.isError ? (
              <div className="hint">{tools.error.message}</div>
            ) : !list.length ? (
              <div className="empty">
                <strong>No tools yet</strong>
                <span>
                  Publish a saved query, or grant a key a connection, and it appears here as a tool.
                </span>
              </div>
            ) : (
              <table className="grid">
                <thead>
                  <tr>
                    {table.columns.map((c) => (
                      <SortHeader key={c.key} controls={table} column={c} />
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {table.rows.map((t) => (
                    <tr key={t.name}>
                      <td className="mono">{t.name}</td>
                      <td>
                        <span className="tag">{t.kind}</span>
                        {t.read_only && <span className="tag">read-only</span>}
                      </td>
                      <td className="mono">{t.params.length ? t.params.join(', ') : '—'}</td>
                      <td>{t.description}</td>
                      <td className="spark-cell">
                        {t.kind === 'saved query' ? (
                          <ActivitySpark
                            label={t.name}
                            items={(activity.by[t.name] ?? []).filter((r) => r.transport === 'mcp')}
                            isPending={activity.isPending}
                            isError={activity.isError}
                          />
                        ) : (
                          <span className="dim" title="Built-in tool - its calls aren't tracked per tool">
                            —
                          </span>
                        )}
                      </td>
                    </tr>
                  ))}
                  {!table.rows.length && <NoMatches controls={table} span={TOOL_COLUMNS.length} />}
                </tbody>
              </table>
            )}
          </section>
        )}
      </div>
    </>
  );
}

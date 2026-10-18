import { useMutation, useQuery } from '@tanstack/react-query';

import { api, unwrap, type Schemas } from '@/api/client';
import { Loading } from '@/app/feedback';

// The MCP server screen: your saved queries, exposed as tools an AI agent can call over MCP (ADR 0006). A
// reachability check (run only when asked), the tools a client would see in this workspace, and how to connect one.
// The MCP server's own settings (QUERYAPIGATE_MCP_*) stay under Settings; this screen is the operational view.
// GET /api/v1/mcp/tools and /api/v1/mcp/status both need the `observe` capability, like Agents and the audit log.

type Tool = Schemas['McpToolList']['items'][number];

const CONFIG = `{
  "mcpServers": {
    "queryapigate": {
      "url": "http://localhost:5001/mcp",
      "headers": { "X-API-Key": "<a scoped API key>" }
    }
  }
}`;

export function McpPage() {
  const tools = useQuery({
    queryKey: ['mcp', 'tools'],
    queryFn: async () => unwrap(await api.GET('/api/v1/mcp/tools')).items,
    retry: false,
  });
  const check = useMutation({
    mutationFn: async () => unwrap(await api.GET('/api/v1/mcp/status')),
  });
  const status = check.data;
  const dot = check.isPending ? 'checking' : !status ? 'unknown' : status.reachable ? 'ok' : 'bad';
  const dotText = check.isPending
    ? 'Checking…'
    : !status
      ? 'Not checked yet'
      : (status.reachable ? 'Reachable on port ' : 'Not reachable on port ') + status.port;
  const list: Tool[] = tools.data ?? [];

  return (
    <>
      <div className="page-head">
        <div className="titles">
          <h1>MCP server</h1>
          <span className="sub">
            Your saved queries, as tools an AI agent can call over MCP. Each key sees only its own workspace&apos;s
            tools.
          </span>
        </div>
        <span className="spacer" />
        <button type="button" className="btn" onClick={() => void tools.refetch()}>
          Refresh
        </button>
      </div>

      <div className="panel" id="mcp-connect">
        <div className="panel-head">
          <strong>Connect a client</strong>
          <span className="dim"> — any MCP client that speaks streamable HTTP</span>
        </div>
        <p className="hint" style={{ padding: '0 14px' }}>
          Start the MCP server with <code>queryapigate mcp</code>, then point a client at its URL with a scoped API
          key as the <code>X-API-Key</code> header. In Claude Desktop, that is one entry in its MCP-servers config:
        </p>
        <pre className="curlbox" style={{ margin: '0 14px 14px' }}>
          {CONFIG}
        </pre>
      </div>

      <div className="panel" id="mcp-reachability">
        <div className="panel-head">
          <strong>Reachability</strong>
          <span className="dim"> — a plain TCP connect to the configured MCP port, only when you ask</span>
        </div>
        <div className="set-row">
          <div className="set-what">
            <span className={'dot ' + dot} />
            <span className="set-label">{dotText}</span>
          </div>
          <button type="button" className="btn" disabled={check.isPending} onClick={() => check.mutate()}>
            Check now
          </button>
        </div>
      </div>

      <div className="panel" id="mcp-tools">
        <div className="panel-head">
          <strong>Tools</strong>
          <span className="dim"> — what tools/list returns for an unrestricted caller in this workspace</span>
        </div>
        {tools.isPending ? (
          <Loading text="Loading tools…" />
        ) : tools.isError ? (
          <div className="hint">{tools.error.message}</div>
        ) : !list.length ? (
          <div className="empty">
            <strong>No tools yet</strong>
            <span>Publish a saved query, or grant a key a connection, and it appears here as a tool.</span>
          </div>
        ) : (
          <table className="grid">
            <thead>
              <tr>
                <th>Name</th>
                <th>Kind</th>
                <th>Parameters</th>
                <th>Description</th>
              </tr>
            </thead>
            <tbody>
              {list.map((t) => (
                <tr key={t.name}>
                  <td className="mono">{t.name}</td>
                  <td>
                    <span className="tag">{t.kind}</span>
                    {t.read_only && <span className="tag">read-only</span>}
                  </td>
                  <td className="mono">{t.params.length ? t.params.join(', ') : '—'}</td>
                  <td>{t.description}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </>
  );
}

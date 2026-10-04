import { useMutation, useQuery } from '@tanstack/react-query';
import { useState } from 'react';

import { api, unwrap, type Schemas } from '@/api/client';
import { useSettings } from '@/app/data';
import { copyText, Loading, useFeedback } from '@/app/feedback';
import { setPref, usePrefValues, type Prefs } from '@/app/prefs';

import { lastSection, rememberSection } from './state';

// The classic Settings screen (ui.py #tab-settings, renderSettings, renderMcpExtras, the copy-env button): the
// server's configuration by section from /api/v1/settings, read-only, plus this browser's appearance preferences.
// The MCP section adds a reachability check, run only when asked, and the tools an MCP client would see.

type Section = Schemas['SettingsSection'];

type PrefRow = [keyof Prefs, string, string, string[]];

const APPEARANCE_ROWS: PrefRow[] = [
  ['theme', 'Theme', 'Follows your operating system unless set.', ['System', 'Light', 'Dark']],
  [
    'fontSize',
    'Font size',
    'Text, and the spacing around it, across the Console.',
    ['Small', 'Medium', 'Large'],
  ],
  [
    'timeZone',
    'Time zone',
    "Timestamps in this computer's time zone, UTC, or the server's (the one it records them in).",
    ['Local', 'UTC', 'Server'],
  ],
  [
    'timeFormat',
    'Time format',
    'Relative shows "5 min ago"; hover any time for the exact one.',
    ['Absolute', 'Relative'],
  ],
  ['density', 'Table density', 'Row height in lists and result grids.', ['Compact', 'Comfortable']],
  ['motion', 'Reduce motion', 'Turns off sliding drawers and pulsing indicators.', ['System', 'On']],
];

const EDITOR_ROWS: PrefRow[] = [
  [
    'format',
    'Default result format',
    'Pre-selected format in API Designer and when trying a query.',
    ['json', 'csv', 'ndjson', 'tsv', 'xml', 'yaml', 'xlsx'],
  ],
  [
    'pageSize',
    'Rows per page',
    'How many rows a query returns per page, to start with.',
    ['10', '25', '50', '100', '500'],
  ],
  ['nullDisplay', 'NULL values', 'How a NULL looks in result grids.', ['NULL', 'Blank']],
  [
    'numberFormat',
    'Numbers',
    'Thousands separators in result grids (1,234,567). Copies and exports keep the raw value.',
    ['Plain', 'Grouped'],
  ],
  ['lineWrap', 'Wrap long lines', 'In the SQL editor.', ['Off', 'On']],
  ['lineNumbers', 'Line numbers', 'In the SQL editor.', ['Off', 'On']],
];

export function SettingsPage() {
  const { toast } = useFeedback();
  const settings = useSettings();
  const [section, setSectionState] = useState(lastSection());
  const setSection = (id: string) => {
    rememberSection(id);
    setSectionState(id);
  };
  const sections = settings.data ?? [];

  const copyEnv = () => {
    const lines = sections.flatMap((sec) =>
      sec.rows.filter((row) => row.env_value != null).map((row) => `${row.env}=${row.env_value}`),
    );
    if (!lines.length) {
      toast('Nothing to copy: every setting is at its default (secrets are never copied)');
      return;
    }
    copyText(lines.join('\n') + '\n', toast);
  };

  const navButton = (id: string, label: string, n: number | null) => (
    <button key={id} type="button" className={section === id ? 'on' : ''} onClick={() => setSection(id)}>
      <span style={{ flex: 1 }}>{label}</span>
      {n === null ? null : <span className="n">{String(n)}</span>}
    </button>
  );

  let body: React.ReactNode;
  const current = sections.find((s) => s.id === section) ?? sections[0];
  if (section === 'ui') {
    body = <PrefsPanel title="Appearance" rows={APPEARANCE_ROWS} />;
  } else if (section === 'editor') {
    body = <PrefsPanel title="Editor & results" rows={EDITOR_ROWS} />;
  } else if (settings.isPending) {
    body = <Loading text="Loading settings…" />;
  } else if (!settings.data) {
    body = (
      <div className="panel">
        <div className="empty">
          <strong>Settings unavailable</strong>
          <span>
            Settings are visible to the admin key only. Apply it in the sidebar, or check the message at the
            top of the page.
          </span>
        </div>
      </div>
    );
  } else if (current) {
    body = (
      <>
        <div className="set-note">
          <i />
          <span>
            Read-only. These values come from <code>QUERYAPIGATE_*</code> environment variables and are
            validated at startup. Change them in your deployment and restart the server.
          </span>
        </div>
        <SectionPanel section={current} />
        {current.id === 'mcp' && <McpExtras />}
      </>
    );
  }

  return (
    <>
      <div className="page-head">
        <div className="titles">
          <h1>Settings</h1>
          <span className="sub">
            Effective configuration of this server, plus preferences for this browser.
          </span>
        </div>
        <span className="spacer" />
        <button id="copy-env" type="button" className="btn" onClick={copyEnv}>
          Copy as .env
        </button>
      </div>
      <div className="settings">
        <nav id="settings-nav" aria-label="Settings sections">
          {sections.map((sec) => navButton(sec.id, sec.title, sec.rows.length))}
          {navButton('ui', 'Appearance', null)}
          {navButton('editor', 'Editor & results', null)}
        </nav>
        <div id="settings-body">{body}</div>
      </div>
    </>
  );
}

function SectionPanel({ section }: { section: Section }) {
  return (
    <div className="panel">
      <div className="set-head">
        <h2>{section.title}</h2>
        <span>{section.description}</span>
      </div>
      {section.rows.map((row) => (
        <div key={row.env + row.label} className="set-row">
          <div className="set-what">
            <span className="set-label">{row.label}</span>
            <span className="set-desc">{row.description}</span>
            <code className="set-env">{row.env}</code>
          </div>
          <div className="set-val">
            <span className="v" title={row.value}>
              {row.value}
            </span>
            <span className={'badge ' + row.source}>{row.source}</span>
          </div>
        </div>
      ))}
    </div>
  );
}

/** Preferences kept in this browser: Appearance, and Editor & results. */
function PrefsPanel({ title, rows }: { title: string; rows: PrefRow[] }) {
  const prefs = usePrefValues();
  return (
    <div className="panel">
      <div className="set-head">
        <h2>{title}</h2>
        <span>Stored in this browser only. Nothing is sent to the server.</span>
      </div>
      {rows.map(([key, label, description, options]) => (
        <div key={key} className="set-row pref">
          <div className="set-what">
            <span className="set-label">{label}</span>
            <span className="set-desc">{description}</span>
          </div>
          <div className="seg" role="group" aria-label={label}>
            {options.map((option) => (
              <button
                key={option}
                type="button"
                className={prefs[key] === option ? 'on' : ''}
                onClick={() => setPref(key, option)}
              >
                {option}
              </button>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}

/** The MCP section's extras: a reachability check, only when asked, and what tools/list returns now. */
function McpExtras() {
  const tools = useQuery({
    queryKey: ['mcp', 'tools'],
    queryFn: async () => unwrap(await api.GET('/api/v1/mcp/tools')).items,
    retry: false,
    gcTime: 0, // loaded once per visit to the section, as in /ui
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
  const list = tools.data ?? [];
  return (
    <div className="panel" style={{ marginTop: 12 }}>
      <div className="set-head">
        <h2>Reachability</h2>
        <span>A plain TCP connect attempt against the port above - not started or probed automatically.</span>
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
      <div className="set-head" style={{ marginTop: 14 }}>
        <h2>Tools</h2>
        <span>What tools/list currently returns for an unrestricted MCP caller.</span>
      </div>
      {tools.isPending ? (
        <div className="hint">Loading…</div>
      ) : !list.length ? (
        <div className="hint">No saved queries or connections are reachable yet.</div>
      ) : (
        <table className="grid">
          <thead>
            <tr>
              <th>Name</th>
              <th>Kind</th>
              <th>Params</th>
              <th>Description</th>
            </tr>
          </thead>
          <tbody>
            {list.map((t) => (
              <tr key={t.name}>
                <td className="mono">{t.name}</td>
                <td>
                  <span className="tag">{t.kind}</span>
                </td>
                <td className="mono">{t.params.length ? t.params.join(', ') : '—'}</td>
                <td>{t.description}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

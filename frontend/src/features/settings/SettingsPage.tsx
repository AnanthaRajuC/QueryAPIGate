import { useEffect, type ReactNode } from 'react';
import { useNavigate, useParams } from 'react-router';

import { type Schemas } from '@/api/client';
import { useSettings } from '@/app/data';
import { copyText, Loading, useFeedback } from '@/app/feedback';
import { setPref, usePrefValues, type Prefs } from '@/app/prefs';
import { ExperimentalTag } from '@/components/Experimental';
import { CopyIcon, LockIcon } from '@/components/icons';

import { lastTab, rememberTab } from './state';

// The Settings screen: the server's configuration from /api/v1/settings, read-only, plus this browser's preferences.
// A tab per area - a section's `area`, several small sections sharing one - and in it a heading per group: a row's
// `group` where its section has them (General, Security), else the section itself. A row is its name, variable and
// description, beside its value - "Set by env" only when something set it. The MCP server's tools and reachability
// live on their own screen (features/mcp/McpPage); the MCP *settings* stay here.

type Section = Schemas['SettingsSection'];
type Row = Section['rows'][number];

const AREAS: { id: string; label: string }[] = [
  { id: 'general', label: 'General' },
  { id: 'security', label: 'Security' },
  { id: 'sign-in', label: 'Sign-in' },
  { id: 'performance', label: 'Performance' },
  { id: 'observability', label: 'Observability' },
  { id: 'mcp', label: 'MCP server' },
];
/** The tab of this browser's preferences - the Console's own, not the server's. */
const BROWSER = 'browser';

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
    ['json', 'csv', 'ndjson', 'tsv', 'xml', 'yaml', 'xlsx', 'parquet'],
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
  const navigate = useNavigate();
  const sections = settings.data ?? [];
  const asked = useParams().tab;
  // a section's own id (/settings/jwt) opens the tab it is under
  const tab =
    asked === BROWSER || AREAS.some((a) => a.id === asked)
      ? asked!
      : (sections.find((sec) => sec.id === asked)?.area ?? lastTab());
  useEffect(() => rememberTab(tab), [tab]);

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

  const count = (area: string) =>
    sections.filter((sec) => sec.area === area).reduce((n, sec) => n + sec.rows.length, 0);
  const tabs = [
    ...AREAS.filter((a) => !settings.data || count(a.id)).map((a) => ({
      ...a,
      n: settings.data ? count(a.id) : null,
    })),
    { id: BROWSER, label: 'This browser', n: null },
  ];

  let body: ReactNode;
  if (tab === BROWSER) {
    body = (
      <>
        <div className="set-note">
          <span>Stored in this browser only. Nothing is sent to the server.</span>
        </div>
        <PrefsGroup title="Appearance" rows={APPEARANCE_ROWS} />
        <PrefsGroup title="Editor & results" rows={EDITOR_ROWS} />
      </>
    );
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
  } else {
    const noConfigFile = sections.some((sec) =>
      sec.rows.some((row) => row.env === 'QUERYAPIGATE_CONFIG_FILE' && row.source === 'default'),
    );
    body = (
      <>
        <div className="set-note">
          <LockIcon />
          <span>
            <strong>Read-only.</strong> These values come from <code>QUERYAPIGATE_*</code> environment
            variables and are validated at startup. Change them in your deployment and restart the server.
          </span>
        </div>
        {groupsOf(sections, tab).map((group) => (
          <SettingsGroup key={group.key} title={group.title} description={group.description}>
            {group.rows.map((row) => (
              <SettingRow
                key={row.env + row.label}
                row={row}
                // it applies only with a configuration file (the server refuses it without one)
                dim={noConfigFile && row.env === 'QUERYAPIGATE_CONFIG_PRUNE'}
              />
            ))}
          </SettingsGroup>
        ))}
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
          <CopyIcon /> Copy as .env
        </button>
      </div>
      <div className="settings-tabbar">
        <div className="subtabs" id="settings-tabs" role="tablist" aria-label="Settings">
          {tabs.map((t) => (
            <button
              key={t.id}
              type="button"
              role="tab"
              aria-selected={tab === t.id}
              className={tab === t.id ? 'on' : undefined}
              onClick={() => navigate(`/settings/${t.id}`)}
            >
              {t.label}
              {t.n === null ? null : <span className="count">{t.n}</span>}
            </button>
          ))}
        </div>
      </div>
      <div id="settings-body" role="tabpanel" aria-label={tabs.find((t) => t.id === tab)?.label}>
        {body}
      </div>
    </>
  );
}

type Group = { key: string; title: string; description?: string; rows: Row[] };

/** An area's headings, in order: a section's own groups where its rows have them, else the section. */
function groupsOf(sections: Section[], area: string): Group[] {
  const groups: Group[] = [];
  for (const sec of sections.filter((s) => s.area === area)) {
    if (!sec.rows.some((row) => row.group)) {
      groups.push({ key: sec.id, title: sec.title, description: sec.description, rows: sec.rows });
      continue;
    }
    for (const row of sec.rows) {
      const title = row.group ?? sec.title;
      const key = `${sec.id}/${title}`;
      let group = groups.find((g) => g.key === key);
      if (!group) groups.push((group = { key, title, rows: [] }));
      group.rows.push(row);
    }
  }
  return groups;
}

function SettingsGroup({
  title,
  description,
  children,
}: {
  title: string;
  description?: string;
  children: ReactNode;
}) {
  return (
    <section className="set-group" aria-label={title}>
      <h2>{title}</h2>
      {description && <p className="set-group-desc">{description}</p>}
      {children}
    </section>
  );
}

/** What a value says, for reading: on/off as a dot, an unset one as "Not set" - display only; the API's value (and
 * the .env copy) stay as they are. */
function shown(value: string): { on: boolean } | { unset: string | null } | { text: string } {
  if (value === 'on' || value === 'off') return { on: value === 'on' };
  const unset = /^(?:none|not set)(?: - (.+))?$/.exec(value);
  if (unset) return { unset: unset[1] ?? null };
  if (value === 'the metadata store') return { text: 'Metadata store' };
  return { text: value };
}

/** Long enough to wrap in a box of its own, with a copy button - a path, a list. */
const LONG = 36;

function SettingRow({ row, dim }: { row: Row; dim: boolean }) {
  const { toast } = useFeedback();
  const value = shown(row.value);
  return (
    <div
      className={dim ? 'set-item dim' : 'set-item'}
      title={dim ? 'Applies only with a configuration file' : undefined}
    >
      <div className="set-item-what">
        <div className="set-item-title">
          <h3>{row.label}</h3>
          {row.experimental && <ExperimentalTag />}
          {row.env && (
            <button
              type="button"
              className="set-env"
              aria-label={`Copy the variable name ${row.env}`}
              title="Copy the variable name"
              onClick={() => copyText(row.env, toast)}
            >
              {row.env}
              <CopyIcon />
            </button>
          )}
        </div>
        <p className="set-item-desc">{row.description}</p>
      </div>
      <div className="set-item-value">
        {'on' in value ? (
          <span className="set-bool">
            <span className={value.on ? 'set-dot on' : 'set-dot'} aria-hidden="true" />
            {value.on ? 'On' : 'Off'}
          </span>
        ) : 'unset' in value ? (
          <span className="set-unset">
            Not set{value.unset ? <span className="set-unset-why"> · {value.unset}</span> : null}
          </span>
        ) : value.text.length > LONG ? (
          <span className="set-long">{value.text}</span>
        ) : (
          <span className="set-text">{value.text}</span>
        )}
        {(row.source !== 'default' || ('text' in value && value.text.length > LONG && !row.secret)) && (
          <div className="set-item-meta">
            {row.source !== 'default' && <span className="tag set-by">Set by {row.source}</span>}
            {'text' in value && value.text.length > LONG && !row.secret && (
              <button
                type="button"
                className="btn-text"
                aria-label={`Copy the value of ${row.label}`}
                onClick={() => copyText(row.value, toast)}
              >
                <CopyIcon /> Copy
              </button>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

/** Preferences kept in this browser: Appearance, and Editor & results - in the same rows, a choice for the value. */
function PrefsGroup({ title, rows }: { title: string; rows: PrefRow[] }) {
  const prefs = usePrefValues();
  return (
    <SettingsGroup title={title}>
      {rows.map(([key, label, description, options]) => (
        <div key={key} className="set-item">
          <div className="set-item-what">
            <div className="set-item-title">
              <h3>{label}</h3>
            </div>
            <p className="set-item-desc">{description}</p>
          </div>
          <div className="set-item-value">
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
        </div>
      ))}
    </SettingsGroup>
  );
}

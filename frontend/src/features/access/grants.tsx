import type { ReactNode } from 'react';

import { useAllQueries, useCollections, useConnections, type RoleEntry } from '@/app/data';
import { Field } from '@/app/feedback';

// What a key and a role share (ui.py scopeNode, queryGrantTags, collectionGrantField and the grant fields of
// openApiKeyForm / openRoleForm): their tags in a table row, and the form fields that edit them.

type CollectionCounts = Record<string, { queries: string[] }> | undefined;

/** Query names plus collection grants, as tags - one renderer for both tables (ui.py queryGrantTags). */
export function queryGrantTags(entry: RoleEntry, collections: CollectionCounts): ReactNode[] {
  const tags: ReactNode[] = [];
  if (entry.queries === '*') {
    tags.push(
      <span key="all-queries" className="tag">
        all queries
      </span>,
    );
  } else {
    (entry.queries ?? []).forEach((q) => {
      const writable = typeof q === 'object' && q.allow_writes;
      const name = typeof q === 'object' ? q.name : q;
      tags.push(
        <span key={'q:' + name} className="tag" title={writable ? name + ' — write access' : undefined}>
          {name + (writable ? ' (write)' : '')}
        </span>,
      );
    });
  }
  (entry.collections ?? []).forEach((c) => {
    const n = collections?.[c]?.queries.length ?? 0;
    tags.push(
      <span
        key={'c:' + c}
        className="tag coll"
        title={`Every query in collection ${c} (${n} now, including any added later) — read-only`}
      >
        {c + '/'}
      </span>,
    );
  });
  return tags;
}

/** A key's "Scope" cell: its connections, then its query and collection grants (ui.py scopeNode). */
export function ScopeNode({ entry, collections }: { entry: RoleEntry; collections: CollectionCounts }) {
  const tags: ReactNode[] = [];
  if (entry.connections === '*') {
    tags.push(
      <span key="all-connections" className="tag coll">
        all connections
      </span>,
    );
  } else {
    (entry.connections ?? []).forEach((c) =>
      tags.push(
        <span key={'conn:' + c} className="tag coll">
          {'conn: ' + c}
        </span>,
      ),
    );
  }
  tags.push(...queryGrantTags(entry, collections));
  return tags.length ? <div className="tags scope">{tags}</div> : <span className="dim">—</span>;
}

export function ExampleBadge() {
  return (
    <span className="tag example" title="Installed by the example APIs — removing the examples removes it">
      example
    </span>
  );
}

// ---- the form ----

export interface GrantState {
  allConnections: boolean;
  connections: string[];
  allQueries: boolean;
  /** query name -> write through it */
  queries: Record<string, boolean>;
  collections: string[];
  allowWrites: boolean;
  writeOps: string;
  tables: string;
  rateLimit: string;
  ips: string;
}

export function initialGrants(existing?: RoleEntry): GrantState {
  const queries: Record<string, boolean> = {};
  if (existing && existing.queries !== '*') {
    existing.queries.forEach((q) => {
      if (typeof q === 'object') queries[q.name] = Boolean(q.allow_writes);
      else queries[q] = false;
    });
  }
  return {
    allConnections: !existing || existing.connections === '*',
    connections: existing && existing.connections !== '*' ? existing.connections : [],
    allQueries: existing?.queries === '*',
    queries,
    collections: existing?.collections ?? [],
    allowWrites: Boolean(existing?.allow_writes),
    writeOps: (existing?.allowed_write_ops ?? []).join(', '),
    tables: (existing?.allowed_tables ?? []).join(', '),
    rateLimit: existing?.rate_limit ?? '',
    ips: (existing?.allowed_ips ?? []).join(', '),
  };
}

const list = (text: string, lower = false) => {
  const items = text
    .split(',')
    .map((v) => (lower ? v.trim().toLowerCase() : v.trim()))
    .filter(Boolean);
  return items.length ? items : null;
};

/** The request fields for these grants - only what exists now can be granted (as in /ui). */
export function grantsPayload(state: GrantState, connectionNames: string[], queryNames: string[]) {
  return {
    connections: state.allConnections ? '*' : connectionNames.filter((c) => state.connections.includes(c)),
    allow_writes: state.allowWrites,
    queries: state.allQueries
      ? '*'
      : queryNames
          .filter((q) => q in state.queries)
          .map((q) => (state.queries[q] ? { name: q, allow_writes: true } : q)),
    collections: state.collections,
    rate_limit: state.rateLimit.trim() || null,
    allowed_ips: list(state.ips),
    allowed_write_ops: list(state.writeOps, true),
    allowed_tables: list(state.tables, true),
  };
}

const TABLES_HINT =
  'Optional, comma-separated table names — e.g. "orders, customers". Restricts every statement (read or write) to only these tables, on mysql/postgres/clickhouse/sqlite/duckdb connections — a query against an unsupported connection type (h2, jdbc, mongo) is always rejected while this is set, not silently unrestricted. Leave blank to allow any table.';

const HINTS = {
  k: {
    queries:
      '— independent of connections above: runnable by name even without connection access, for an external client that should only see its own approved queries. Check "write" on a query to let this key write through it specifically, even with no blanket write access — "All saved queries" above can never carry write access, only a specific enumerated list can.',
    queryWrite: (q: string) => `Allow this key to write through ${q} specifically`,
    writeOps:
      'Optional, comma-separated SQL keywords — e.g. "insert, update". Only takes effect when Allow writes is on; narrows which write statements this key may perform. Leave blank to allow any write.',
    rateLimit:
      'Optional, this key only — e.g. "100/minute". Checked in addition to QUERYAPIGATE_RATE_LIMIT, not instead of it. Leave blank for no limit of its own.',
    ips: 'Optional, comma-separated IP addresses or CIDR ranges — e.g. "203.0.113.5, 10.0.0.0/8". Leave blank to allow any address.',
  },
  r: {
    queries: null,
    queryWrite: (q: string) => `Allow a key created from this role to write through ${q} specifically`,
    writeOps:
      'Optional, comma-separated SQL keywords — e.g. "insert, update". Only takes effect when Allow writes is on.',
    rateLimit: 'Optional — e.g. "100/minute". Leave blank for no limit of its own.',
    ips: 'Optional, comma-separated IP addresses or CIDR ranges. Leave blank to allow any address.',
  },
};

/** The names a grant form offers: existing connections, saved queries and collections. */
export function useGrantChoices() {
  const connections = useConnections();
  const queries = useAllQueries();
  const collections = useCollections();
  return {
    connectionNames: (connections.data ?? []).map((c) => c.name).sort(),
    queryNames: (queries.data ?? []).map((q) => q.name).sort(),
    collections: collections.data?.collections,
  };
}

const dimmed = (off: boolean) => (off ? { opacity: 0.4, pointerEvents: 'none' as const } : { opacity: 1 });

/** The grant fields of the key form (prefix "k") and the role form (prefix "r"), in the classic order. */
export function GrantFields({
  prefix,
  state,
  onChange,
}: {
  prefix: 'k' | 'r';
  state: GrantState;
  onChange: (next: GrantState) => void;
}) {
  const { connectionNames, queryNames, collections } = useGrantChoices();
  const hints = HINTS[prefix];
  const set = (patch: Partial<GrantState>) => onChange({ ...state, ...patch });
  const toggle = (items: string[], item: string, on: boolean) =>
    on ? [...items, item] : items.filter((i) => i !== item);
  const collectionNames = Object.keys(collections ?? {}).sort();
  return (
    <>
      <label className="switch">
        <input
          id={`${prefix}-all`}
          type="checkbox"
          checked={state.allConnections}
          onChange={(e) => set({ allConnections: e.target.checked })}
        />
        All connections
      </label>
      <div className="field">
        <label>Allowed connections</label>
        <div className="tags" style={dimmed(state.allConnections)}>
          {connectionNames.length ? (
            connectionNames.map((c) => (
              <label key={c} className="switch" style={{ fontWeight: 400 }}>
                <input
                  type="checkbox"
                  checked={!state.allConnections && state.connections.includes(c)}
                  onChange={(e) => set({ connections: toggle(state.connections, c, e.target.checked) })}
                />
                {c}
              </label>
            ))
          ) : (
            <span className="hint">No connections exist yet — add one on the Connections tab first.</span>
          )}
        </div>
      </div>
      <label className="switch">
        <input
          id={`${prefix}-all-queries`}
          type="checkbox"
          checked={state.allQueries}
          onChange={(e) => set({ allQueries: e.target.checked })}
        />
        All saved queries
      </label>
      <div className="field">
        <label>Additional saved-query access</label>
        <div className="tags" style={dimmed(state.allQueries)}>
          {queryNames.length ? (
            queryNames.map((q) => {
              const checked = !state.allQueries && q in state.queries;
              return (
                <label key={q} className="switch" style={{ fontWeight: 400 }}>
                  <input
                    type="checkbox"
                    checked={checked}
                    onChange={(e) => {
                      const next = { ...state.queries };
                      if (e.target.checked) next[q] = false;
                      else delete next[q];
                      set({ queries: next });
                    }}
                  />
                  {q}
                  <span className="switch" style={{ fontWeight: 400, marginLeft: 6 }}>
                    <input
                      type="checkbox"
                      title={hints.queryWrite(q)}
                      disabled={!checked}
                      checked={checked && Boolean(state.queries[q])}
                      onChange={(e) => set({ queries: { ...state.queries, [q]: e.target.checked } })}
                    />
                    write
                  </span>
                </label>
              );
            })
          ) : (
            <span className="hint">
              No saved queries exist yet — add one on the API Repository tab first.
            </span>
          )}
        </div>
        {hints.queries ? <span className="hint">{hints.queries}</span> : null}
      </div>
      <div className="field">
        <label>Collections</label>
        <div className="tags">
          {collectionNames.length ? (
            collectionNames.map((c) => (
              <label key={c} className="switch" style={{ fontWeight: 400 }}>
                <input
                  type="checkbox"
                  checked={state.collections.includes(c)}
                  onChange={(e) => set({ collections: toggle(state.collections, c, e.target.checked) })}
                />
                {c}
                <span className="dim">{` (${collections![c]!.queries.length})`}</span>
              </label>
            ))
          ) : (
            <span className="hint">No collections yet — file a saved query under one first.</span>
          )}
        </div>
        <span className="hint">
          — reaches every query in the collection, including ones filed there later; read-only, and never
          ad-hoc SQL. Moving a query into or out of a collection changes what this key can run.
        </span>
      </div>
      <label className="switch">
        <input
          id={`${prefix}-writes`}
          type="checkbox"
          checked={state.allowWrites}
          onChange={(e) => set({ allowWrites: e.target.checked })}
        />
        Allow writes<span className="hint">— still capped by QUERYAPIGATE_ALLOW_WRITES</span>
      </label>
      <Field id={`${prefix}-allowed-write-ops`} label="Allowed write operations" hint={hints.writeOps}>
        <input
          id={`${prefix}-allowed-write-ops`}
          value={state.writeOps}
          placeholder="e.g. insert, update"
          autoComplete="off"
          spellCheck={false}
          onChange={(e) => set({ writeOps: e.target.value })}
        />
      </Field>
      <Field id={`${prefix}-allowed-tables`} label="Allowed tables" hint={TABLES_HINT}>
        <input
          id={`${prefix}-allowed-tables`}
          value={state.tables}
          placeholder="e.g. orders, customers"
          autoComplete="off"
          spellCheck={false}
          onChange={(e) => set({ tables: e.target.value })}
        />
      </Field>
      <Field id={`${prefix}-rate-limit`} label="Rate limit" hint={hints.rateLimit}>
        <input
          id={`${prefix}-rate-limit`}
          value={state.rateLimit}
          placeholder="e.g. 100/minute"
          autoComplete="off"
          spellCheck={false}
          onChange={(e) => set({ rateLimit: e.target.value })}
        />
      </Field>
      <Field id={`${prefix}-allowed-ips`} label="Allowed IPs" hint={hints.ips}>
        <input
          id={`${prefix}-allowed-ips`}
          value={state.ips}
          placeholder="e.g. 203.0.113.5, 10.0.0.0/8"
          autoComplete="off"
          spellCheck={false}
          onChange={(e) => set({ ips: e.target.value })}
        />
      </Field>
    </>
  );
}

import { useQueryClient } from '@tanstack/react-query';
import { useMemo, useRef, useState, type FormEvent } from 'react';
import { useNavigate } from 'react-router';

import { ApiError, apiJson } from '@/api/client';
import { useAllQueries, useCollections, useConnections, type CollectionsInfo } from '@/app/data';
import { Field, FormActions, useFeedback } from '@/app/feedback';
import { SchemaField, useSchema } from '@/components/SchemaBrowser';
import { SqlEditor, type SqlEditorHandle } from '@/components/SqlEditor';
import { sqlParams } from '@/lib/sql';

import {
  useQueryActions,
  useSaveQuery,
  type Query,
  type QueryCreateInput,
  type QueryVersion,
  type QueryVersionInput,
} from './api';

// The classic API Repository drawers (ui.py openQueryForm, openMoveForm, openNewCollectionForm,
// openRenameCollectionForm) - same fields, labels and hints. One addition: "Save as draft", because a version is
// now a draft until it is published (ADR 0001). The primary Save publishes at once, exactly as Save always did.

const COLLECTION_RE = /^[a-z0-9][a-z0-9._-]{0,62}$/;

function reportError(showError: ReturnType<typeof useFeedback>['showError'], error: unknown) {
  if (error instanceof ApiError) {
    if (error.code === 'precondition_failed') {
      showError(
        'This query changed since you opened the form - close it, check the latest version and try again.',
        { status: 412 },
      );
      return;
    }
    showError(error.message, { status: error.status, errors: error.details });
    return;
  }
  showError((error as Error).message);
}

// ---- New API / New version ----

/** New API, or (with `base`) a new version of one. `starter` pre-fills a new one's connection and SQL - the Access
 * map's "New API on <table>". */
export function QueryForm({
  base,
  starter,
}: {
  base?: { query: Query; version: QueryVersion; etag: string | null };
  starter?: { connection: string; sql?: string };
}) {
  const navigate = useNavigate();
  const { closeDrawer, showError, toast } = useFeedback();
  const connections = useConnections();
  const queries = useAllQueries();
  const collectionsInfo = useCollections();
  const save = useSaveQuery();
  const editor = useRef<SqlEditorHandle>(null);
  const prefill = base?.version;
  const isMongo0 = prefill?.query_type === 'mongo';

  const [filename, setFilename] = useState(base?.query.name ?? '');
  const [author, setAuthor] = useState(prefill?.author ?? '');
  const [connection, setConnection] = useState(prefill?.connection_name ?? starter?.connection ?? '');
  const [sql, setSql] = useState(
    isMongo0 && prefill?.mongo
      ? JSON.stringify(
          {
            collection: prefill.mongo.mongo_collection,
            filter: prefill.mongo.mongo_filter ?? {},
            ...(prefill.mongo.mongo_projection ? { projection: prefill.mongo.mongo_projection } : {}),
            ...(prefill.mongo.mongo_sort ? { sort: prefill.mongo.mongo_sort } : {}),
          },
          null,
          2,
        )
      : (prefill?.sql ?? starter?.sql ?? ''),
  );
  const [description, setDescription] = useState(prefill?.description ?? '');
  const [collection, setCollection] = useState('');
  const [tags, setTags] = useState(prefill?.tags.join(', ') ?? '');
  const [params, setParams] = useState(
    prefill && Object.keys(prefill.parameters).length ? JSON.stringify(prefill.parameters, null, 2) : '',
  );

  const dialect = connections.data?.find((c) => c.name === connection)?.db;
  const isMongo = dialect === 'mongo';
  const schema = useSchema(connection || undefined);
  const completion = useMemo(
    () => Object.fromEntries((schema.data?.tables ?? []).map((t) => [t.name, t.columns.map((c) => c.name)])),
    [schema.data],
  );
  const refs = isMongo ? [] : sqlParams(sql);
  const nextVersion = base ? base.query.latest_version + 1 : null;

  function submit(publish: boolean) {
    return (event?: FormEvent) => {
      event?.preventDefault();
      let parameters: Record<string, unknown> = {};
      if (params.trim()) {
        try {
          parameters = JSON.parse(params) as Record<string, unknown>;
        } catch (e) {
          showError('query_parameters is not valid JSON: ' + (e as Error).message, {
            errors: { query_parameters: (e as Error).message },
          });
          return;
        }
      }
      const name = filename.trim();
      const body: QueryVersionInput = {
        author,
        description,
        tags: tags
          .split(',')
          .map((t) => t.trim())
          .filter(Boolean),
        connection_name: connection || undefined,
        parameters: parameters as QueryVersionInput['parameters'],
        publish,
      };
      if (isMongo) {
        let doc: { collection?: string; filter?: object; projection?: object; sort?: object };
        try {
          doc = sql.trim() ? (JSON.parse(sql) as typeof doc) : {};
        } catch (e) {
          showError('The query must be valid JSON: ' + (e as Error).message, {
            errors: { sql_query: (e as Error).message },
          });
          return;
        }
        if (!doc || typeof doc !== 'object' || Array.isArray(doc) || !doc.collection) {
          showError(
            'The query must be a JSON object with a "collection" field, e.g. {"collection": "users", "filter": {}}.',
            {
              errors: { sql_query: 'collection is required' },
            },
          );
          return;
        }
        Object.assign(body, {
          query_type: 'mongo',
          mongo_collection: doc.collection,
          mongo_filter: doc.filter ?? {},
        });
        if (doc.projection) body.mongo_projection = doc.projection as Record<string, never>;
        if (doc.sort) body.mongo_sort = doc.sort as Record<string, never>;
      } else {
        body.sql = sql;
      }
      // As before: saving under a name that already exists adds a version to it.
      const existing = queries.data?.find((q) => q.name === name);
      const chosen = collection.trim();
      if (!base && chosen && existing && (existing.collection ?? '') !== chosen) {
        showError(
          `“${name}” already exists${existing.collection ? ' in collection ' + existing.collection : ' with no collection'}. Save it without a collection here, then use “Move…” — that shows which keys gain or lose access first.`,
        );
        return;
      }
      const createBody: QueryCreateInput = { ...body, name, ...(chosen ? { collection: chosen } : {}) };
      const input =
        base || existing
          ? ({ kind: 'version', name: base?.query.name ?? name, etag: base?.etag ?? null, body } as const)
          : ({ kind: 'create', body: createBody } as const);
      save.mutate(input, {
        onSuccess: (saved) => {
          showError('');
          closeDrawer();
          const version = saved.latest_version;
          toast(`Saved ${saved.name} v${version}${publish ? '' : ' as a draft'}`);
          navigate(`/queries/${encodeURIComponent(saved.name)}`);
        },
        onError: (e) => reportError(showError, e),
      });
    };
  }

  return (
    <form className="form" noValidate onSubmit={submit(true)}>
      <Field
        id="q-filename"
        label="Filename"
        hint="Letters, digits, spaces, “.”, “_” and “-”. Saving an existing name adds a version."
      >
        <input
          id="q-filename"
          value={filename}
          placeholder="films_by_rating"
          required
          autoComplete="off"
          spellCheck={false}
          readOnly={Boolean(base)}
          autoFocus={!base}
          onChange={(e) => setFilename(e.target.value)}
        />
      </Field>
      <div className="grid2">
        <Field id="q-author" label="Author">
          <input
            id="q-author"
            value={author}
            required
            autoComplete="off"
            spellCheck={false}
            onChange={(e) => setAuthor(e.target.value)}
          />
        </Field>
        <Field id="q-connection" label="Default connection">
          <select id="q-connection" value={connection} onChange={(e) => setConnection(e.target.value)}>
            <option value="">(none — must be given at run time)</option>
            {connection && !connections.data?.some((c) => c.name === connection) ? (
              <option value={connection}>{connection}</option>
            ) : null}
            {(connections.data ?? []).map((c) => (
              <option key={c.name} value={c.name}>
                {c.name + (c.active ? '' : ' (inactive)')}
              </option>
            ))}
          </select>
        </Field>
      </div>
      <div className="field">
        <label htmlFor="q-sql">
          {isMongo ? 'Query' : 'SQL'}
          <span className="type">
            {isMongo
              ? 'a find() filter document - use ":name" for bound parameters'
              : 'use :name for bound parameters'}
          </span>
        </label>
        <SqlEditor
          handle={editor}
          id="q-sql"
          value={sql}
          onChange={setSql}
          dialect={dialect}
          schema={completion}
          label={isMongo ? 'Query' : 'SQL'}
          placeholder={
            isMongo
              ? '{"collection": "orders", "filter": {"status": ":status"}}'
              : 'SELECT * FROM t WHERE id = :id'
          }
        />
        <div className="refs">
          {refs.length > 0 && <span className="hint">Bound in the query:</span>}
          {refs.map((n) => (
            <span key={n} className="tag">
              :{n}
            </span>
          ))}
        </div>
      </div>
      <SchemaField
        connection={connection}
        dialect={dialect}
        editor={editor}
        isMongo={isMongo}
        onPreview={(table) => {
          closeDrawer();
          navigate('/designer', { state: { preview: { connection, table } } });
        }}
      />
      <Field id="q-description" label="Description">
        <input
          id="q-description"
          value={description}
          required
          autoComplete="off"
          spellCheck={false}
          onChange={(e) => setDescription(e.target.value)}
        />
      </Field>
      {!base && (
        <div className="field">
          <label htmlFor="q-collection">Collection</label>
          <input
            id="q-collection"
            list="q-collection-list"
            placeholder="optional — e.g. reporting"
            autoComplete="off"
            spellCheck={false}
            value={collection}
            onChange={(e) => setCollection(e.target.value)}
          />
          <datalist id="q-collection-list">
            {Object.keys(collectionsInfo.data?.collections ?? {})
              .sort()
              .map((c) => (
                <option key={c} value={c} />
              ))}
          </datalist>
          <div className="hint">
            Optional. Lowercase letters, digits, “.”, “_” and “-”. Later, use “Move…” to change it.
          </div>
          {collection.trim() && <Impact moves={[[null, collection.trim()]]} info={collectionsInfo.data} />}
        </div>
      )}
      <Field id="q-tags" label="Tags">
        <input
          id="q-tags"
          value={tags}
          placeholder="comma-separated"
          autoComplete="off"
          spellCheck={false}
          onChange={(e) => setTags(e.target.value)}
        />
      </Field>
      <Field
        id="q-params"
        label="query_parameters"
        hint="JSON object: name → type, or {type, min, max, default, description}."
      >
        <textarea
          id="q-params"
          spellCheck={false}
          placeholder='{"id": {"type": "int", "min": 1}}'
          value={params}
          onChange={(e) => setParams(e.target.value)}
        />
      </Field>
      <FormActions onCancel={closeDrawer}>
        <button
          type="button"
          className="btn"
          disabled={save.isPending}
          title="Save without publishing: callers keep getting the published version"
          onClick={() => submit(false)()}
        >
          Save as draft
        </button>
        <button type="submit" className="btn primary" disabled={save.isPending}>
          {nextVersion ? `Save as v${nextVersion}` : 'Save'}
        </button>
      </FormActions>
    </form>
  );
}

// ---- Who gains or loses access when queries change collection (ui.py impactNodeMany) ----

export function Impact({
  moves,
  info,
}: {
  moves: [string | null, string | null][];
  info: CollectionsInfo | undefined;
}) {
  const reaching = (c: string | null, kind: 'keys' | 'roles') =>
    c && info?.collections[c] ? info.collections[c]![kind] : [];
  const minus = (a: string[], b: string[]) => a.filter((x) => !b.includes(x));
  const acc = {
    keysGain: new Set<string>(),
    keysLose: new Set<string>(),
    rolesGain: new Set<string>(),
    rolesLose: new Set<string>(),
  };
  moves.forEach(([from, to]) => {
    minus(reaching(to, 'keys'), reaching(from, 'keys')).forEach((x) => acc.keysGain.add(x));
    minus(reaching(from, 'keys'), reaching(to, 'keys')).forEach((x) => acc.keysLose.add(x));
    minus(reaching(to, 'roles'), reaching(from, 'roles')).forEach((x) => acc.rolesGain.add(x));
    minus(reaching(from, 'roles'), reaching(to, 'roles')).forEach((x) => acc.rolesLose.add(x));
  });
  const one = moves.length === 1;
  const lines: [string, string[], boolean][] = [];
  if (acc.keysGain.size)
    lines.push([
      `Keys that will gain access to ${one ? 'this query' : 'some of these queries'}: `,
      [...acc.keysGain],
      true,
    ]);
  if (acc.keysLose.size)
    lines.push([`Keys that will lose access to ${one ? 'it' : 'some of them'}: `, [...acc.keysLose], true]);
  if (acc.rolesGain.size) {
    lines.push([
      `Roles that will include ${one ? 'it' : 'them'} (affects only keys created from them later): `,
      [...acc.rolesGain],
      false,
    ]);
  }
  if (acc.rolesLose.size) {
    lines.push([
      `Roles that will no longer include ${one ? 'it' : 'some of them'} (keys already created from them are unaffected): `,
      [...acc.rolesLose],
      false,
    ]);
  }
  return (
    <div className="impact">
      {!lines.length && <span className="hint">No key’s access changes.</span>}
      {lines.map(([text, names, warn]) => (
        <div key={text} className="hint" style={warn ? { color: 'var(--warn)' } : undefined}>
          {text}
          <strong>{names.join(', ')}</strong>
        </div>
      ))}
    </div>
  );
}

// ---- Move… ----

export function MoveForm({ query, etag }: { query: Query; etag: string | null }) {
  const { closeDrawer, showError, toast } = useFeedback();
  const info = useCollections();
  const actions = useQueryActions(query.name);
  const current = query.collection ?? '';
  const [choice, setChoice] = useState(current);
  const [newName, setNewName] = useState('');
  const target = choice === '__new__' ? newName.trim() : choice;
  const names = Object.keys(info.data?.collections ?? {}).sort();

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (choice === '__new__' && !target) {
      showError('Enter a name for the new collection.');
      return;
    }
    if (target === current) {
      closeDrawer();
      return;
    }
    actions.move.mutate(
      { collection: target || null, etag },
      {
        onSuccess: () => {
          showError('');
          closeDrawer();
          toast(query.name + (target ? ' → ' + target : ' removed from its collection'));
        },
        onError: (err) => reportError(showError, err),
      },
    );
  };
  return (
    <form className="form" noValidate onSubmit={submit}>
      <div className="hint">
        Moving a query is not a new version. A key granted a collection can run every query in it — so this
        changes what other keys can reach:
      </div>
      <Field id="mv-select" label="Collection">
        <select id="mv-select" value={choice} onChange={(e) => setChoice(e.target.value)}>
          <option value="">No collection</option>
          {names.map((c) => (
            <option key={c} value={c}>
              {`${c} (${info.data!.collections[c]!.queries.length})`}
            </option>
          ))}
          <option value="__new__">New collection…</option>
        </select>
      </Field>
      {choice === '__new__' && (
        <Field id="mv-new" label="New collection name" hint="Lowercase letters, digits, “.”, “_” and “-”.">
          <input
            id="mv-new"
            placeholder="e.g. reporting"
            autoComplete="off"
            spellCheck={false}
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
          />
        </Field>
      )}
      <div>
        {target === current ? (
          <span className="hint">No change.</span>
        ) : (
          <Impact moves={[[current || null, target || null]]} info={info.data} />
        )}
      </div>
      <FormActions onCancel={closeDrawer}>
        <button type="submit" className="btn primary" disabled={actions.move.isPending}>
          Move
        </button>
      </FormActions>
    </form>
  );
}

// ---- New collection ----

export function NewCollectionForm() {
  const client = useQueryClient();
  const { closeDrawer, showError, toast } = useFeedback();
  const queries = useAllQueries();
  const info = useCollections();
  const [name, setName] = useState('');
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const rows = [...(queries.data ?? [])].sort(
    (a, b) => (a.collection ? 1 : 0) - (b.collection ? 1 : 0) || (a.name < b.name ? -1 : 1),
  );
  const trimmed = name.trim();
  const existing = Boolean(info.data?.collections[trimmed]);
  const hint = !trimmed
    ? 'Lowercase letters, digits, “.”, “_” and “-”.'
    : !COLLECTION_RE.test(trimmed)
      ? 'Not a valid name — use lowercase letters, digits, “.”, “_” and “-”, starting with a letter or digit.'
      : existing
        ? `“${trimmed}” already exists — the ticked queries will be added to it.`
        : '';
  const chosen = [...picked];

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!COLLECTION_RE.test(trimmed) || !chosen.length) return;
    setBusy(true);
    let done = 0;
    for (const q of chosen) {
      try {
        await apiJson(`/api/v1/queries/${encodeURIComponent(q)}`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ collection: trimmed }),
        });
        done++;
      } catch (err) {
        reportError(showError, err);
        break;
      }
    }
    setBusy(false);
    if (done === chosen.length) {
      showError('');
      closeDrawer();
      toast(`Created ${trimmed} with ${done}${done === 1 ? ' query' : ' queries'}`);
    } else if (done) {
      showError(
        `Moved ${done} of ${chosen.length} queries into ${trimmed} before it stopped — the rest are unchanged.`,
      );
    }
    if (done) {
      void client.invalidateQueries({ queryKey: ['queries'] });
      void client.invalidateQueries({ queryKey: ['collections'] });
    }
  };

  return (
    <form className="form" noValidate onSubmit={submit}>
      <div className="hint">
        A collection exists only while a query is in it, so choose its first queries now. Moving them is not a
        new version.
      </div>
      <Field id="nc-name" label="Name">
        <input
          id="nc-name"
          placeholder="e.g. reporting"
          autoComplete="off"
          spellCheck={false}
          autoFocus
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
      </Field>
      <div className="hint">{hint}</div>
      <div className="field">
        <label>Queries to file under it</label>
        <div className="tags" style={{ flexDirection: 'column', gap: 6 }}>
          {rows.length ? (
            rows.map((q) => (
              <label key={q.name} className="switch" style={{ fontWeight: 400 }}>
                <input
                  type="checkbox"
                  checked={picked.has(q.name)}
                  onChange={(e) =>
                    setPicked((all) => {
                      const next = new Set(all);
                      if (e.target.checked) next.add(q.name);
                      else next.delete(q.name);
                      return next;
                    })
                  }
                />
                {q.name}
                {q.collection ? <span className="dim">{` (now in ${q.collection})`}</span> : null}
              </label>
            ))
          ) : (
            <span className="hint">
              No saved queries yet — create one first; a collection exists only while a query is in it.
            </span>
          )}
        </div>
      </div>
      {trimmed && COLLECTION_RE.test(trimmed) && chosen.length > 0 && (
        <Impact
          moves={chosen.map(
            (q) =>
              [queries.data?.find((x) => x.name === q)?.collection ?? null, trimmed] as [
                string | null,
                string,
              ],
          )}
          info={info.data}
        />
      )}
      <FormActions onCancel={closeDrawer}>
        <button
          type="submit"
          className="btn primary"
          disabled={busy || !(COLLECTION_RE.test(trimmed) && chosen.length)}
        >
          Create collection
        </button>
      </FormActions>
    </form>
  );
}

// ---- Rename collection ----

export function RenameCollectionForm({ name }: { name: string }) {
  const client = useQueryClient();
  const { closeDrawer, showError, toast } = useFeedback();
  const info = useCollections();
  const known = info.data?.collections[name] ?? { queries: [], keys: [], roles: [] };
  const [to, setTo] = useState(name);
  const [merge, setMerge] = useState(false);
  const [busy, setBusy] = useState(false);
  const target = to.trim();
  const clash = target !== name && Boolean(info.data?.collections[target]);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!target || target === name) {
      showError('Enter a different name.');
      return;
    }
    if (clash && !merge) {
      showError(`“${target}” already exists — tick “Merge” to combine them.`);
      return;
    }
    setBusy(true);
    try {
      await apiJson(`/collections/${encodeURIComponent(name)}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: target, merge }),
      });
      showError('');
      closeDrawer();
      toast(`Renamed ${name} → ${target}`);
      void client.invalidateQueries();
    } catch (err) {
      reportError(showError, err);
    } finally {
      setBusy(false);
    }
  };
  return (
    <form className="form" noValidate onSubmit={submit}>
      <div className="hint">
        {`Renames ${known.queries.length}${known.queries.length === 1 ? ' query' : ' queries'} and updates ${known.keys.length}${
          known.keys.length === 1 ? ' key' : ' keys'
        } and ${known.roles.length}${known.roles.length === 1 ? ' role' : ' roles'} that are granted it. No key loses access at any point.`}
      </div>
      <Field id="rn-name" label="New name">
        <input
          id="rn-name"
          autoComplete="off"
          spellCheck={false}
          autoFocus
          value={to}
          onChange={(e) => setTo(e.target.value)}
        />
      </Field>
      {clash && (
        <label className="switch">
          <input id="rn-merge" type="checkbox" checked={merge} onChange={(e) => setMerge(e.target.checked)} />
          Merge into the existing collection
          <span className="hint">— also how to finish a rename that was interrupted part-way</span>
        </label>
      )}
      <FormActions onCancel={closeDrawer}>
        <button type="submit" className="btn primary" disabled={busy}>
          Rename
        </button>
      </FormActions>
    </form>
  );
}

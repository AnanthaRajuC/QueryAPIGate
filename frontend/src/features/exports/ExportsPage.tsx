import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState, type FormEvent } from 'react';

import { api, unwrap, unwrapEmpty, unwrapWithEtag, type Schemas } from '@/api/client';
import { useAllQueries, useCan } from '@/app/data';
import { Field, FormActions, Loading, useFeedback } from '@/app/feedback';
import { PencilIcon, TrashIcon } from '@/components/icons';
import { Time } from '@/components/Time';

import { ExperimentalNote } from './ExperimentalNote';
import { useDestinations, useExports, useReport } from './shared';

// Saved exports (ADR 0004), on /api/v1/exports: a saved query delivered to a destination, run by a scheduler or here.

type Export = Schemas['Export'];
type Format = Export['format'];
const FORMATS: Format[] = ['parquet', 'csv', 'ndjson'];

export function ExportsPage() {
  const list = useExports();
  const can = useCan();
  const { openDrawer, toast } = useFeedback();
  const report = useReport();
  const client = useQueryClient();
  const write = can('exports.write');
  const newExport = () => openDrawer({ title: 'New export', wide: true, content: <ExportForm /> });

  const run = useMutation({
    mutationFn: async (name: string) =>
      unwrap(await api.POST('/api/v1/exports/{name}/runs', { params: { path: { name } } })),
    onSuccess: (r) => {
      toast(
        r.object
          ? `✓ ${r.rows} ${r.rows === 1 ? 'row' : 'rows'} written to ${r.object}`
          : '✓ No new rows - nothing written',
      );
      void client.invalidateQueries({ queryKey: ['exports'] });
    },
    onError: (e) => {
      report(e);
      void client.invalidateQueries({ queryKey: ['exports'] });
    },
  });
  const remove = async (name: string) => {
    if (!window.confirm(`Delete export '${name}'? Files it already wrote stay where they are.`)) return;
    try {
      unwrapEmpty(await api.DELETE('/api/v1/exports/{name}', { params: { path: { name } } }));
      toast('Deleted ' + name);
      void client.invalidateQueries({ queryKey: ['exports'] });
    } catch (error) {
      report(error);
    }
  };

  let body: React.ReactNode;
  if (list.isPending) {
    body = <Loading text="Loading exports…" />;
  } else if (list.isError) {
    body = <div className="hint">{list.error.message}</div>;
  } else if (!list.data.length) {
    body = (
      <div className="empty">
        <strong>No exports yet</strong>
        <span>
          An export delivers a saved query&apos;s result as a file to a destination - Parquet, CSV or NDJSON -
          and, if incremental, only what changed since the last run. Run it here, or from cron, a Kubernetes
          CronJob or Airflow.
        </span>
        {write && (
          <button type="button" className="btn primary" onClick={newExport}>
            New export
          </button>
        )}
      </div>
    );
  } else {
    body = (
      <div style={{ overflowX: 'auto' }}>
        <table className="grid">
          <thead>
            <tr>
              {['Name', 'Query', 'Writes to', 'Format', 'Watermark', ''].map((t, i) => (
                <th key={i}>{t}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {list.data.map((x) => (
              <tr key={x.name} data-name={x.name}>
                <td>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                    <span className="name">{x.name}</span>
                    {x.running && <span className="tag">running</span>}
                  </div>
                </td>
                <td className="mono">{x.query}</td>
                <td className="mono">
                  {x.destination}:{x.path}
                </td>
                <td>{x.format}</td>
                <td className={x.incremental ? 'mono' : 'dim'}>
                  {x.incremental
                    ? `${x.incremental.column} > ${x.watermark ?? x.incremental.start ?? '(start)'}`
                    : 'everything, each run'}
                </td>
                <td>
                  <div className="actions">
                    {can('exports.run') && (
                      <button
                        type="button"
                        className="btn sm outlined"
                        disabled={run.isPending || x.running}
                        onClick={() => run.mutate(x.name)}
                      >
                        {run.isPending && run.variables === x.name ? 'Running…' : 'Run now'}
                      </button>
                    )}
                    <button
                      type="button"
                      className="btn ghost sm"
                      onClick={() =>
                        openDrawer({
                          title: 'Runs',
                          kicker: x.name,
                          wide: true,
                          content: <Runs name={x.name} />,
                        })
                      }
                    >
                      Runs
                    </button>
                    {write && (
                      <>
                        <button
                          type="button"
                          className="btn ghost sm icon"
                          title={`Edit ${x.name}`}
                          aria-label="Edit"
                          onClick={() =>
                            openDrawer({
                              title: 'Edit export',
                              kicker: x.name,
                              wide: true,
                              content: <ExportForm name={x.name} />,
                            })
                          }
                        >
                          <PencilIcon />
                        </button>
                        <button
                          type="button"
                          className="btn ghost sm icon danger"
                          title={`Delete ${x.name}`}
                          aria-label="Delete"
                          onClick={() => void remove(x.name)}
                        >
                          <TrashIcon />
                        </button>
                      </>
                    )}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    );
  }

  return (
    <>
      <div className="page-head">
        <div className="titles">
          <h1>Exports</h1>
          <span className="sub">Saved queries delivered as files to a bucket or a folder.</span>
        </div>
        <span className="spacer" />
        {write && (
          <button id="new-export" type="button" className="btn primary" onClick={newExport}>
            New export
          </button>
        )}
      </div>
      <ExperimentalNote />
      <div id="exports-table" className="panel">
        {body}
      </div>
    </>
  );
}

function Runs({ name }: { name: string }) {
  const runs = useQuery({
    queryKey: ['exports', name, 'runs'],
    queryFn: async () =>
      unwrap(await api.GET('/api/v1/exports/{name}/runs', { params: { path: { name } } })).items,
    retry: false,
  });
  if (runs.isPending) return <Loading text="Loading runs…" />;
  if (runs.isError) return <div className="hint">{runs.error.message}</div>;
  if (!runs.data.length) return <div className="hint">No runs yet.</div>;
  return (
    <table className="grid" id="export-runs">
      <thead>
        <tr>
          {['When', 'Status', 'Rows', 'Written', 'By'].map((t) => (
            <th key={t}>{t}</th>
          ))}
        </tr>
      </thead>
      <tbody>
        {runs.data.map((r) => (
          <tr key={String(r.run_id ?? r.executed_at)}>
            <td className="mono" style={{ whiteSpace: 'nowrap' }}>
              <Time value={r.executed_at} />
            </td>
            <td>
              <span className={`dot ${r.status === 'success' ? 'ok' : 'bad'}`} /> {r.status}
            </td>
            <td className="mono">{r.rows ?? '—'}</td>
            <td className={r.object ? 'mono' : 'dim'}>
              {r.status === 'error' ? r.error : (r.object ?? 'nothing - no new rows')}
            </td>
            <td className="dim">{r.key_name ?? '—'}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function ExportForm({ name }: { name?: string }) {
  const detail = useQuery({
    queryKey: ['exports', 'detail', name],
    queryFn: async () =>
      unwrapWithEtag(await api.GET('/api/v1/exports/{name}', { params: { path: { name: name! } } })),
    enabled: Boolean(name),
    retry: false,
    gcTime: 0,
  });
  if (name && detail.isPending) return <Loading />;
  if (name && detail.isError) return <div className="hint">{detail.error.message}</div>;
  return (
    <ExportFields
      key={name ?? ''}
      name={name}
      existing={detail.data?.data}
      etag={detail.data?.etag ?? null}
    />
  );
}

function ExportFields({ name, existing, etag }: { name?: string; existing?: Export; etag: string | null }) {
  const isEdit = Boolean(name);
  const { closeDrawer, showError, toast } = useFeedback();
  const report = useReport();
  const client = useQueryClient();
  const queries = useAllQueries();
  const destinations = useDestinations();
  const [exportName, setExportName] = useState(name ?? '');
  const [query, setQuery] = useState(existing?.query ?? '');
  const [destination, setDestination] = useState(existing?.destination ?? '');
  const [format, setFormat] = useState<Format>(existing?.format ?? 'parquet');
  const [path, setPath] = useState(existing?.path ?? '{name}/{date}/{name}_{run}.parquet');
  const [params, setParams] = useState(
    existing?.params && Object.keys(existing.params).length ? JSON.stringify(existing.params) : '',
  );
  const [incremental, setIncremental] = useState(Boolean(existing?.incremental));
  const [column, setColumn] = useState(existing?.incremental?.column ?? '');
  const [parameter, setParameter] = useState(existing?.incremental?.parameter ?? '');
  const [start, setStart] = useState(String(existing?.incremental?.start ?? ''));
  const [skipEmpty, setSkipEmpty] = useState(existing ? existing.skip_empty : true);
  const [description, setDescription] = useState(existing?.description ?? '');

  const changeFormat = (next: Format) => {
    setFormat(next);
    setPath((p) => p.replace(/\.(parquet|csv|ndjson)$/, '.' + next)); // keep the extension in step
  };

  const save = useMutation({
    mutationFn: async () => {
      let parsed: Record<string, unknown> = {};
      if (params.trim()) {
        try {
          parsed = JSON.parse(params);
        } catch {
          throw new Error('Parameters must be a JSON object, e.g. {"region": "EU"}.');
        }
      }
      const body = {
        query,
        destination,
        format,
        path: path.trim(),
        params: parsed,
        skip_empty: skipEmpty,
        description: description.trim() || (isEdit ? null : undefined),
        incremental: incremental
          ? {
              column: column.trim(),
              parameter: parameter.trim(),
              ...(start.trim() ? { start: start.trim() } : {}),
            }
          : isEdit
            ? null
            : undefined,
      };
      if (isEdit) {
        return unwrap(
          await api.PATCH('/api/v1/exports/{name}', {
            params: { path: { name: name! }, header: etag ? { 'If-Match': etag } : {} },
            body,
          }),
        );
      }
      return unwrap(await api.POST('/api/v1/exports', { body: { name: exportName.trim(), ...body } }));
    },
    onSuccess: (saved) => {
      showError('');
      void client.invalidateQueries({ queryKey: ['exports'] });
      closeDrawer();
      toast((isEdit ? 'Updated ' : 'Created ') + saved.name);
    },
    onError: report,
  });

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!isEdit && !exportName.trim()) {
      showError('A name is required.', { errors: { name: 'This field is required' } });
      return;
    }
    save.mutate();
  };

  const queryNames = (queries.data ?? []).map((q) => q.name).sort();
  const destinationNames = (destinations.data ?? []).map((d) => d.name);
  return (
    <form className="form" noValidate onSubmit={submit}>
      <div className="grid2">
        <Field id="x-name" label="Name">
          <input
            id="x-name"
            value={exportName}
            placeholder="acme-daily-orders"
            autoComplete="off"
            spellCheck={false}
            disabled={isEdit}
            autoFocus={!isEdit}
            onChange={(e) => setExportName(e.target.value)}
          />
        </Field>
        <Field id="x-description" label="Description" hint="Optional.">
          <input id="x-description" value={description} onChange={(e) => setDescription(e.target.value)} />
        </Field>
      </div>
      <div className="grid2">
        <Field id="x-query" label="Saved query" hint="Its published version runs.">
          <select id="x-query" value={query} onChange={(e) => setQuery(e.target.value)}>
            <option value="">Choose a query…</option>
            {queryNames.map((q) => (
              <option key={q} value={q}>
                {q}
              </option>
            ))}
          </select>
        </Field>
        <Field id="x-destination" label="Destination">
          <select id="x-destination" value={destination} onChange={(e) => setDestination(e.target.value)}>
            <option value="">Choose a destination…</option>
            {destinationNames.map((d) => (
              <option key={d} value={d}>
                {d}
              </option>
            ))}
          </select>
        </Field>
      </div>
      <div className="grid2">
        <Field
          id="x-path"
          label="File path"
          hint="Under the destination. {name}, {date}, {time}, {run} and the query's parameters ({region}) are filled in."
        >
          <input id="x-path" value={path} spellCheck={false} onChange={(e) => setPath(e.target.value)} />
        </Field>
        <Field id="x-format" label="Format">
          <select id="x-format" value={format} onChange={(e) => changeFormat(e.target.value as Format)}>
            {FORMATS.map((f) => (
              <option key={f} value={f}>
                {f}
              </option>
            ))}
          </select>
        </Field>
      </div>
      <Field id="x-params" label="Parameters" hint='JSON, e.g. {"region": "EU"}. Optional.'>
        <input
          id="x-params"
          className="mono"
          value={params}
          spellCheck={false}
          onChange={(e) => setParams(e.target.value)}
        />
      </Field>
      <label className="switch">
        <input
          id="x-incremental"
          type="checkbox"
          checked={incremental}
          onChange={(e) => setIncremental(e.target.checked)}
        />
        Incremental<span className="hint">— each run delivers only what changed since the last one</span>
      </label>
      {incremental && (
        <div className="grid2" style={{ gridTemplateColumns: '1fr 1fr 1fr' }}>
          <Field
            id="x-column"
            label="Column"
            hint="Its largest value delivered is remembered, e.g. updated_at."
          >
            <input
              id="x-column"
              value={column}
              spellCheck={false}
              onChange={(e) => setColumn(e.target.value)}
            />
          </Field>
          <Field id="x-parameter" label="Parameter" hint="Bound to it next run: WHERE updated_at > :since.">
            <input
              id="x-parameter"
              value={parameter}
              spellCheck={false}
              onChange={(e) => setParameter(e.target.value)}
            />
          </Field>
          <Field id="x-start" label="Start" hint="Bound on the first run.">
            <input id="x-start" value={start} spellCheck={false} onChange={(e) => setStart(e.target.value)} />
          </Field>
        </div>
      )}
      <label className="switch">
        <input
          id="x-skip-empty"
          type="checkbox"
          checked={skipEmpty}
          onChange={(e) => setSkipEmpty(e.target.checked)}
        />
        Skip empty runs<span className="hint">— a run with no rows writes no file</span>
      </label>
      <FormActions onCancel={closeDrawer}>
        <button type="submit" className="btn primary" disabled={save.isPending}>
          {isEdit ? 'Save' : 'Create'}
        </button>
      </FormActions>
    </form>
  );
}

import { zodResolver } from '@hookform/resolvers/zod';
import { ArrowLeft, CheckCircle2, Play } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';
import { Controller, useForm, useWatch } from 'react-hook-form';
import { Link, useNavigate, useParams, useSearchParams } from 'react-router';
import { z } from 'zod';

import { ApiError } from '@/api/client';
import { SqlEditor } from '@/components/sql/SqlEditor';
import { Alert } from '@/components/ui/alert';
import { Button } from '@/components/ui/button';
import { Card, CardTitle } from '@/components/ui/card';
import { Field, Select, Textarea } from '@/components/ui/form';
import { Input } from '@/components/ui/input';

import {
  useConnectionSchema,
  useConnections,
  useQueryDetail,
  useSaveQuery,
  useTestRun,
  useValidate,
  type Query,
  type QueryVersionInput,
} from './api';
import { ParametersEditor } from './ParametersEditor';
import { localPlaceholders, parseValue, toDraft, toRules, type ParamDraft } from './parameters';

// The flagship workflow (ADR 0001): write SQL with schema-aware completion, define parameters, validate, test against
// the real database, then save as a draft or publish.

const NAME_PATTERN = /^[A-Za-z0-9 _.-]+$/;

const formSchema = z.object({
  name: z
    .string()
    .trim()
    .min(1, 'Give the query a name')
    .regex(NAME_PATTERN, "Use letters, digits, spaces, '.', '_' and '-'")
    .refine((value) => !value.startsWith('.'), "A name can't start with '.'"),
  description: z.string().trim().min(1, 'Describe what the query returns'),
  connection_name: z.string(),
  tags: z.string(),
  sql: z.string().trim().min(1, 'Write the SQL'),
});

type FormValues = z.infer<typeof formSchema>;

export function QueryEditorPage() {
  const name = useParams().name;
  const [searchParams] = useSearchParams();
  const from = Number(searchParams.get('from')) || undefined;
  if (!name) return <Editor key="new" />;
  return <ExistingQueryEditor name={name} from={from} />;
}

function ExistingQueryEditor({ name, from }: { name: string; from?: number }) {
  const detail = useQueryDetail(name);
  if (detail.isPending) return <p className="text-sm text-ink-3">Loading…</p>;
  if (detail.isError)
    return (
      <Alert variant="danger">
        Couldn&apos;t load {name}: {detail.error.message}
      </Alert>
    );
  return (
    <Editor
      key={`${name}:${from ?? 'latest'}`}
      query={detail.data.data}
      etag={detail.data.etag}
      from={from}
    />
  );
}

function Editor({ query, etag, from }: { query?: Query; etag?: string | null; from?: number }) {
  const navigate = useNavigate();
  const source = query
    ? (query.versions.find((v) => v.version === from) ?? query.versions[query.versions.length - 1])
    : undefined;
  const form = useForm<FormValues>({
    resolver: zodResolver(formSchema),
    defaultValues: {
      name: query?.name ?? '',
      description: source?.description ?? '',
      connection_name: source?.connection_name ?? '',
      tags: source?.tags.join(', ') ?? '',
      sql: source?.sql ?? '',
    },
  });
  const [drafts, setDrafts] = useState<Record<string, ParamDraft>>(() =>
    Object.fromEntries(Object.entries(source?.parameters ?? {}).map(([n, rule]) => [n, toDraft(rule)])),
  );
  const sql = useWatch({ control: form.control, name: 'sql' });
  const connectionName = useWatch({ control: form.control, name: 'connection_name' });
  const placeholders = useMemo(() => localPlaceholders(sql), [sql]);

  const connections = useConnections();
  const schema = useConnectionSchema(connectionName || undefined);
  const dialect = connections.data?.find((c) => c.name === connectionName)?.db;
  const completion = useMemo(
    () => Object.fromEntries((schema.data?.tables ?? []).map((t) => [t.name, t.columns.map((c) => c.name)])),
    [schema.data],
  );

  const body = (values: FormValues, publish: boolean): QueryVersionInput => ({
    description: values.description.trim(),
    sql: values.sql,
    connection_name: values.connection_name || undefined,
    tags: values.tags
      .split(',')
      .map((t) => t.trim())
      .filter(Boolean),
    parameters: toRules(placeholders, drafts),
    publish,
  });

  // Validate as you type (debounced): the server's own saving rules, plus the tables the SQL reads.
  const validate = useValidate();
  const { mutate: runValidate } = validate;
  const rulesKey = JSON.stringify(toRules(placeholders, drafts));
  useEffect(() => {
    if (!sql.trim()) return;
    const timer = setTimeout(() => {
      runValidate({
        description: form.getValues('description') || 'draft',
        sql,
        connection_name: connectionName || undefined,
        parameters: JSON.parse(rulesKey),
      });
    }, 600);
    return () => clearTimeout(timer);
  }, [sql, connectionName, rulesKey, runValidate, form]);

  const save = useSaveQuery();
  const submit = (publish: boolean) =>
    form.handleSubmit((values) => {
      const input = query
        ? ({ kind: 'version', name: query.name, etag: etag ?? null, body: body(values, publish) } as const)
        : ({ kind: 'create', body: { ...body(values, publish), name: values.name.trim() } } as const);
      save.mutate(input, {
        onSuccess: (saved) => navigate(`/queries/${encodeURIComponent(saved.name)}`),
        onError: (error) => {
          if (error instanceof ApiError && error.code === 'query_exists') {
            form.setError('name', { message: 'A query with this name already exists' });
          }
        },
      });
    });

  const saveError =
    save.error instanceof ApiError && save.error.code === 'precondition_failed'
      ? 'This query changed since you opened the editor (a new version or a publish elsewhere). Reload to see it - your SQL is still here to copy.'
      : save.error && !(save.error instanceof ApiError && save.error.code === 'query_exists')
        ? save.error.message
        : null;

  const errors = form.formState.errors;
  return (
    <div className="flex max-w-5xl flex-col gap-4">
      <Link
        to={query ? `/queries/${encodeURIComponent(query.name)}` : '/queries'}
        className="inline-flex w-fit items-center gap-1 text-sm text-ink-2 hover:text-accent"
      >
        <ArrowLeft size={14} aria-hidden /> {query ? query.name : 'Queries'}
      </Link>
      <div>
        <h1 className="text-lg font-semibold">{query ? `New version of ${query.name}` : 'New query'}</h1>
        <p className="text-sm text-ink-2">
          {query
            ? `Starting from v${source?.version}. Saving adds v${query.latest_version + 1}; the published version keeps serving until you publish.`
            : 'Saved as a draft until you publish it.'}
        </p>
      </div>

      <form className="flex flex-col gap-4" onSubmit={(e) => e.preventDefault()} noValidate>
        <Card className="grid gap-4 sm:grid-cols-2">
          {!query && (
            <Field
              label="Name"
              htmlFor="name"
              error={errors.name?.message}
              hint="Its endpoint will be /q/<name>."
            >
              <Input id="name" aria-invalid={Boolean(errors.name)} {...form.register('name')} />
            </Field>
          )}
          <Field label="Connection" htmlFor="connection_name" hint="The database it runs against by default.">
            <Controller
              control={form.control}
              name="connection_name"
              render={({ field }) => (
                // Controlled: the options arrive after the form's value is set, and must still show it.
                <Select id="connection_name" {...field}>
                  <option value="">No default connection</option>
                  {field.value && !connections.data?.some((c) => c.name === field.value) && (
                    <option value={field.value}>{field.value}</option>
                  )}
                  {connections.data?.map((c) => (
                    <option key={c.name} value={c.name} disabled={!c.active}>
                      {c.name} ({c.db}){c.active ? '' : ' - inactive'}
                    </option>
                  ))}
                </Select>
              )}
            />
          </Field>
          <Field
            label="Description"
            htmlFor="description"
            error={errors.description?.message}
            className="sm:col-span-2"
          >
            <Textarea
              id="description"
              rows={2}
              aria-invalid={Boolean(errors.description)}
              {...form.register('description')}
            />
          </Field>
          <Field label="Tags" htmlFor="tags" hint="Comma-separated." className="sm:col-span-2">
            <Input id="tags" {...form.register('tags')} />
          </Field>
        </Card>

        <Card className="flex flex-col gap-2">
          <div className="flex items-center justify-between gap-2">
            <CardTitle>SQL</CardTitle>
            <span className="text-xs text-ink-3">
              {schema.isSuccess
                ? `Completion knows ${schema.data.tables.length} tables on ${connectionName}`
                : connectionName
                  ? 'Loading tables for completion…'
                  : 'Pick a connection for table and column completion'}
            </span>
          </div>
          <Controller
            control={form.control}
            name="sql"
            render={({ field }) => (
              <SqlEditor
                value={field.value}
                onChange={field.onChange}
                dialect={dialect}
                schema={completion}
                label="SQL"
                placeholder="SELECT … FROM … WHERE id = :id"
              />
            )}
          />
          {errors.sql && (
            <p role="alert" className="text-xs text-danger">
              {errors.sql.message}
            </p>
          )}
          <ValidationPanel result={validate.data} pending={validate.isPending} />
        </Card>

        <Card className="flex flex-col gap-3">
          <CardTitle>Parameters</CardTitle>
          <ParametersEditor
            placeholders={placeholders}
            drafts={drafts}
            onChange={(name, draft) => setDrafts((all) => ({ ...all, [name]: draft }))}
          />
        </Card>

        <TestPanel sql={sql} connectionName={connectionName} placeholders={placeholders} drafts={drafts} />

        {saveError && <Alert variant="danger">{saveError}</Alert>}
        <div className="flex flex-wrap justify-end gap-2">
          <Button type="button" variant="outline" disabled={save.isPending} onClick={submit(false)}>
            Save as draft
          </Button>
          <Button type="button" disabled={save.isPending} onClick={submit(true)}>
            Save and publish
          </Button>
        </div>
      </form>
    </div>
  );
}

function ValidationPanel({
  result,
  pending,
}: {
  result: { valid: boolean; errors: { message: string; field?: string }[]; tables: string[] } | undefined;
  pending: boolean;
}) {
  if (!result) return pending ? <p className="text-xs text-ink-3">Checking…</p> : null;
  return (
    <div aria-live="polite" className="flex flex-col gap-1 text-xs">
      {result.valid ? (
        <p className="inline-flex items-center gap-1 text-accent">
          <CheckCircle2 size={14} aria-hidden /> Valid
          {result.tables.length > 0 && (
            <span className="text-ink-3"> · reads {result.tables.join(', ')}</span>
          )}
        </p>
      ) : (
        <ul className="flex flex-col gap-1 text-danger">
          {result.errors.map((error, index) => (
            <li key={index}>{error.message}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

function TestPanel({
  sql,
  connectionName,
  placeholders,
  drafts,
}: {
  sql: string;
  connectionName: string;
  placeholders: string[];
  drafts: Record<string, ParamDraft>;
}) {
  const [values, setValues] = useState<Record<string, string>>({});
  const run = useTestRun();
  const canRun = Boolean(sql.trim() && connectionName);

  function test() {
    const params: Record<string, unknown> = {};
    for (const name of placeholders) {
      const draft = drafts[name];
      const value = parseValue(draft?.type ?? '', values[name] ?? draft?.default ?? '');
      if (value !== undefined) params[name] = value;
    }
    run.mutate({ sql, connection_name: connectionName, params });
  }

  return (
    <Card className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <CardTitle>Test</CardTitle>
        <span className="text-xs text-ink-3">
          Runs the SQL above, unsaved, against the connection - up to 50 rows.
        </span>
      </div>
      {placeholders.length > 0 && (
        <div className="flex flex-wrap gap-3">
          {placeholders.map((name) => (
            <Field key={name} label={name} htmlFor={`test-${name}`}>
              <Input
                id={`test-${name}`}
                className="h-8 w-40"
                placeholder={drafts[name]?.default || ''}
                value={values[name] ?? ''}
                onChange={(e) => setValues((all) => ({ ...all, [name]: e.target.value }))}
              />
            </Field>
          ))}
        </div>
      )}
      <Button
        type="button"
        variant="outline"
        className="w-fit"
        disabled={!canRun || run.isPending}
        onClick={test}
      >
        <Play size={14} aria-hidden /> {run.isPending ? 'Running…' : 'Run test'}
      </Button>
      {!connectionName && <p className="text-xs text-ink-3">Pick a connection to test against.</p>}
      {run.isError && <Alert variant="danger">{run.error.message}</Alert>}
      {run.isSuccess && <ResultsTable rows={run.data.rows} hasMore={run.data.hasMore} />}
    </Card>
  );
}

function ResultsTable({ rows, hasMore }: { rows: Record<string, unknown>[]; hasMore: boolean }) {
  if (rows.length === 0) return <p className="text-sm text-ink-2">No rows.</p>;
  const columns = Object.keys(rows[0] ?? {});
  return (
    <div className="flex flex-col gap-1">
      <div className="max-h-80 overflow-auto rounded-md border border-line">
        <table className="w-full text-left font-mono text-xs" aria-label="Test results">
          <thead className="sticky top-0 bg-surface-2 text-ink-2">
            <tr>
              {columns.map((c) => (
                <th key={c} className="px-2 py-1 font-medium">
                  {c}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, i) => (
              <tr key={i} className="border-t border-line">
                {columns.map((c) => (
                  <td key={c} className="px-2 py-1 whitespace-nowrap">
                    {row[c] === null ? <span className="text-ink-3">null</span> : String(row[c])}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="text-xs text-ink-3">
        {rows.length} row{rows.length === 1 ? '' : 's'}
        {hasMore ? ' (more not shown)' : ''}
      </p>
    </div>
  );
}

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState, type FormEvent } from 'react';

import { ApiError, api, unwrap, unwrapEmpty, unwrapWithEtag, type Schemas } from '@/api/client';
import { useCan } from '@/app/data';
import { Field, FormActions, Loading, useFeedback } from '@/app/feedback';
import { PencilIcon, TrashIcon } from '@/components/icons';

import { ExperimentalNote } from './ExperimentalNote';
import { useDestinations, useReport } from './shared';

// Where exports may write (ADR 0004), on /api/v1/destinations: a bucket prefix or a folder, and its credentials.

type Destination = Schemas['Destination'];

export function DestinationsPage() {
  const list = useDestinations();
  const can = useCan();
  const { openDrawer, toast } = useFeedback();
  const report = useReport();
  const client = useQueryClient();
  const write = can('destinations.write');
  const newDestination = () => openDrawer({ title: 'New destination', content: <DestinationForm /> });

  const test = async (name: string) => {
    try {
      const result = unwrap(
        await api.POST('/api/v1/destinations/{name}/test', { params: { path: { name } } }),
      );
      toast(`✓ Wrote ${result.object} (${result.elapsed_ms} ms)`);
    } catch (error) {
      report(error);
    }
  };
  const remove = async (name: string) => {
    if (
      !window.confirm(
        `Delete destination '${name}'? Its credentials are removed; files already written stay.`,
      )
    )
      return;
    try {
      unwrapEmpty(await api.DELETE('/api/v1/destinations/{name}', { params: { path: { name } } }));
      toast('Deleted ' + name);
      void client.invalidateQueries({ queryKey: ['destinations'] });
    } catch (error) {
      report(error);
    }
  };

  let body: React.ReactNode;
  if (list.isPending) {
    body = <Loading text="Loading destinations…" />;
  } else if (list.isError) {
    body = <div className="hint">{list.error.message}</div>;
  } else if (!list.data.length) {
    body = (
      <div className="empty">
        <strong>No destinations yet</strong>
        <span>
          A destination is where exports may write - an S3, Google Cloud Storage or R2 bucket prefix, or a
          folder - and the credentials to write there. Nothing is ever written outside it.
        </span>
        {write && (
          <button type="button" className="btn primary" onClick={newDestination}>
            New destination
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
              {['Name', 'Writes under', 'Storage', 'Credentials', ''].map((t, i) => (
                <th key={i}>{t}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {list.data.map((d) => (
              <tr key={d.name} data-name={d.name}>
                <td className="name">{d.name}</td>
                <td className="mono">{d.url}</td>
                <td className="dim">
                  {[d.storage ?? 'local folder', d.region, d.endpoint].filter(Boolean).join(' · ')}
                </td>
                <td className="dim">{d.user ? `${d.user} · ${d.password ?? 'no secret'}` : 'none'}</td>
                <td>
                  <div className="actions" hidden={!write}>
                    <button type="button" className="btn ghost sm" onClick={() => void test(d.name)}>
                      Test
                    </button>
                    <button
                      type="button"
                      className="btn ghost sm icon"
                      title={`Edit ${d.name}`}
                      aria-label="Edit"
                      onClick={() =>
                        openDrawer({
                          title: 'Edit destination',
                          kicker: d.name,
                          content: <DestinationForm name={d.name} />,
                        })
                      }
                    >
                      <PencilIcon />
                    </button>
                    <button
                      type="button"
                      className="btn ghost sm icon danger"
                      title={`Delete ${d.name}`}
                      aria-label="Delete"
                      onClick={() => void remove(d.name)}
                    >
                      <TrashIcon />
                    </button>
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
          <h1>Destinations</h1>
          <span className="sub">
            Buckets and folders exports may write to, and the credentials to write there.
          </span>
        </div>
        <span className="spacer" />
        {write && (
          <button id="new-destination" type="button" className="btn primary" onClick={newDestination}>
            New destination
          </button>
        )}
      </div>
      <ExperimentalNote />
      <div id="destinations-table" className="panel">
        {body}
      </div>
    </>
  );
}

function DestinationForm({ name }: { name?: string }) {
  const detail = useQuery({
    queryKey: ['destinations', 'detail', name],
    queryFn: async () =>
      unwrapWithEtag(await api.GET('/api/v1/destinations/{name}', { params: { path: { name: name! } } })),
    enabled: Boolean(name),
    retry: false,
    gcTime: 0,
  });
  if (name && detail.isPending) return <Loading />;
  if (name && detail.isError) return <div className="hint">{detail.error.message}</div>;
  return (
    <DestinationFields
      key={name ?? ''}
      name={name}
      existing={detail.data?.data}
      etag={detail.data?.etag ?? null}
    />
  );
}

function DestinationFields({
  name,
  existing,
  etag,
}: {
  name?: string;
  existing?: Destination;
  etag: string | null;
}) {
  const isEdit = Boolean(name);
  const { closeDrawer, showError, toast } = useFeedback();
  const report = useReport();
  const client = useQueryClient();
  const [destName, setDestName] = useState(name ?? '');
  const [url, setUrl] = useState(existing?.url ?? '');
  const [region, setRegion] = useState(existing?.region ?? '');
  const [endpoint, setEndpoint] = useState(existing?.endpoint ?? '');
  const [urlStyle, setUrlStyle] = useState<string>(existing?.url_style ?? '');
  const [useSsl, setUseSsl] = useState(existing?.use_ssl !== false);
  const [accountId, setAccountId] = useState(existing?.account_id ?? '');
  const [user, setUser] = useState(existing?.user ?? '');
  const [password, setPassword] = useState(existing?.password ?? '');
  const [tested, setTested] = useState<{ ok: boolean; text: string } | null>(null);
  const remote = /^(s3|gs|gcs|r2):\/\//.test(url);

  /** The form's fields - an emptied one as `empty` (undefined on create, null on edit, which removes it). */
  const fields = (empty: null | undefined) => ({
    url: url.trim(),
    region: (remote && region.trim()) || empty,
    endpoint: (remote && endpoint.trim()) || empty,
    url_style: (remote && (urlStyle as 'vhost' | 'path')) || empty,
    use_ssl: remote && !useSsl ? false : empty,
    account_id: (remote && accountId.trim()) || empty,
    user: (remote && user.trim()) || empty,
    password: (remote && password) || empty,
  });

  const save = useMutation({
    mutationFn: async () => {
      if (isEdit) {
        return unwrap(
          await api.PATCH('/api/v1/destinations/{name}', {
            params: { path: { name: name! }, header: etag ? { 'If-Match': etag } : {} },
            body: fields(null),
          }),
        );
      }
      return unwrap(
        await api.POST('/api/v1/destinations', { body: { name: destName.trim(), ...fields(undefined) } }),
      );
    },
    onSuccess: (saved) => {
      showError('');
      void client.invalidateQueries({ queryKey: ['destinations'] });
      closeDrawer();
      toast((isEdit ? 'Updated ' : 'Created ') + saved.name);
    },
    onError: report,
  });

  const probe = useMutation({
    mutationFn: async () =>
      isEdit && password === existing?.password
        ? unwrap(await api.POST('/api/v1/destinations/{name}/test', { params: { path: { name: name! } } }))
        : unwrap(await api.POST('/api/v1/destinations/test', { body: fields(undefined) })),
    onSuccess: (r) => setTested({ ok: true, text: `✓ Wrote ${r.object} (${r.elapsed_ms} ms)` }),
    onError: (e) =>
      setTested({ ok: false, text: '✗ ' + (e instanceof ApiError ? e.message : (e as Error).message) }),
  });

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!isEdit && !destName.trim()) {
      showError('A name is required.', { errors: { name: 'This field is required' } });
      return;
    }
    save.mutate();
  };

  return (
    <form className="form" noValidate onSubmit={submit}>
      <Field
        id="d-name"
        label="Name"
        hint={isEdit ? undefined : 'How exports refer to it, e.g. partner-acme.'}
      >
        <input
          id="d-name"
          value={destName}
          placeholder="partner-acme"
          autoComplete="off"
          spellCheck={false}
          disabled={isEdit}
          autoFocus={!isEdit}
          onChange={(e) => setDestName(e.target.value)}
        />
      </Field>
      <Field
        id="d-url"
        label="Writes under"
        hint="An s3://, gs:// or r2:// prefix, or a folder (/exports/ or file:///exports/), ending in /. Exports write here and nowhere else."
      >
        <input
          id="d-url"
          value={url}
          placeholder="s3://acme-exchange/from-us/"
          autoComplete="off"
          spellCheck={false}
          onChange={(e) => setUrl(e.target.value)}
        />
      </Field>
      {remote && (
        <fieldset className="duck-files">
          <legend>Object storage</legend>
          <div className="grid2">
            <Field id="d-user" label="Access key ID">
              <input
                id="d-user"
                value={user}
                autoComplete="off"
                spellCheck={false}
                onChange={(e) => setUser(e.target.value)}
              />
            </Field>
            <Field
              id="d-password"
              label="Secret access key"
              hint={
                isEdit
                  ? 'Leave the mask to keep the stored secret.'
                  : '${ENV_VAR} references are resolved on the server.'
              }
            >
              <input
                id="d-password"
                type="password"
                value={password}
                autoComplete="off"
                onChange={(e) => setPassword(e.target.value)}
              />
            </Field>
          </div>
          <div className="grid2">
            <Field id="d-region" label="Region">
              <input
                id="d-region"
                value={region}
                placeholder="eu-west-1"
                onChange={(e) => setRegion(e.target.value)}
              />
            </Field>
            <Field
              id="d-endpoint"
              label="Endpoint"
              hint="Only for an S3-compatible service, e.g. minio.internal:9000."
            >
              <input id="d-endpoint" value={endpoint} onChange={(e) => setEndpoint(e.target.value)} />
            </Field>
          </div>
          <div className="grid2">
            <Field id="d-url-style" label="URL style">
              <select id="d-url-style" value={urlStyle} onChange={(e) => setUrlStyle(e.target.value)}>
                <option value="">Default</option>
                <option value="vhost">Virtual-hosted (bucket.host)</option>
                <option value="path">Path (host/bucket)</option>
              </select>
            </Field>
            <Field id="d-account" label="Account ID" hint="Cloudflare R2 only.">
              <input id="d-account" value={accountId} onChange={(e) => setAccountId(e.target.value)} />
            </Field>
          </div>
          <label className="switch">
            <input
              id="d-ssl"
              type="checkbox"
              checked={useSsl}
              onChange={(e) => setUseSsl(e.target.checked)}
            />
            Use HTTPS
          </label>
        </fieldset>
      )}
      <div className="test-row">
        <button
          type="button"
          className="btn"
          disabled={probe.isPending || !url.trim()}
          onClick={() => probe.mutate()}
        >
          {probe.isPending ? 'Testing…' : 'Test'}
        </button>
        <span className="test-result">
          {tested && <span className={tested.ok ? 'test-ok' : 'test-fail'}>{tested.text}</span>}
        </span>
      </div>
      <div className="hint">
        Test writes one small file, {'_queryapigate_probe.csv'}, under the prefix - overwritten each time.
      </div>
      <FormActions onCancel={closeDrawer}>
        <button type="submit" className="btn primary" disabled={save.isPending}>
          {isEdit ? 'Save' : 'Create'}
        </button>
      </FormActions>
    </form>
  );
}

import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router';

import { api, apiFetch, unwrap, unwrapEmpty, type Schemas } from '@/api/client';
import { useAllQueries, useSettings } from '@/app/data';
import { useFeedback } from '@/app/feedback';
import { useMetricsSeries } from '@/app/metrics';
import { readResult, Results, type ResultData } from '@/components/Results';
import { StatTile } from '@/components/StatTile';
import { metricGauge } from '@/lib/metrics';

// The classic Caching screen (ui.py #tab-caching, renderCaching, renderCacheEntriesPanel, showCacheEntry): how the
// cache_ttl response cache performs, which saved queries declare a cache_ttl, and what is cached right now -
// each entry's body viewable exactly as a caller receives it on a hit.

type Entry = Schemas['CacheEntryList']['items'][number];

function useCacheEntries() {
  return useQuery({
    queryKey: ['cache', 'entries'],
    queryFn: async () => unwrap(await api.GET('/api/v1/cache/entries')).items,
    retry: false,
  });
}

export function CachingPage() {
  const navigate = useNavigate();
  const client = useQueryClient();
  const settings = useSettings();
  const metrics = useMetricsSeries();
  const queries = useAllQueries();
  const series = metrics.data ?? [];
  const backend = settings.data?.find((s) => s.id === 'cache')?.rows[0]?.value ?? '…';
  const hits = metricGauge(series, 'queryapigate_cache_hits_total');
  const misses = metricGauge(series, 'queryapigate_cache_misses_total');
  const total = hits + misses;
  const cached = (queries.data ?? []).filter((q) => (q.cache_ttl ?? 0) > 0);

  const refresh = () => {
    void metrics.refetch();
    void client.invalidateQueries({ queryKey: ['cache', 'entries'] });
  };

  return (
    <>
      <div className="page-head">
        <div className="titles">
          <h1>Caching</h1>
          <span className="sub">
            The response cache backing saved queries&apos; <code>cache_ttl</code> - see the Response cache row
            in Settings for the backend itself.
          </span>
        </div>
        <span className="spacer" />
        <button id="refresh-caching" type="button" className="btn" onClick={refresh}>
          Refresh
        </button>
      </div>
      <div id="caching-body">
        <div className="stat-tiles">
          <StatTile label="Cache backend" value={backend} />
          <StatTile label="Entries" value={metricGauge(series, 'queryapigate_cache_entries')} />
          <StatTile label="Hit rate" value={total ? ((100 * hits) / total).toFixed(1) + '%' : '—'} />
          <StatTile label="Hits" value={hits} />
          <StatTile label="Misses" value={misses} />
        </div>
        <p className="sub-h" style={{ marginTop: 14 }}>
          Saved queries with cache_ttl set
        </p>
        {!cached.length ? (
          <div className="hint">
            No saved query declares a cache_ttl yet - set one on a read-only query to start caching its
            responses.
          </div>
        ) : (
          <div className="panel" style={{ overflowX: 'auto' }}>
            <table className="grid">
              <thead>
                <tr>
                  {['Query', 'Collection', 'Connection', 'cache_ttl'].map((t, i) => (
                    <th key={t} className={i === 3 ? 'num' : ''}>
                      {t}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {cached.map((q) => (
                  <tr key={q.name}>
                    <td>
                      <button
                        type="button"
                        className="target"
                        onClick={() => navigate(`/queries/${encodeURIComponent(q.name)}`)}
                      >
                        {q.name}
                      </button>
                    </td>
                    <td>{q.collection || <span className="dim">—</span>}</td>
                    <td className="mono">{q.connection_name || '—'}</td>
                    <td className="mono num">{q.cache_ttl + 's'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
      <div id="cache-entries-panel">
        <Entries />
      </div>
    </>
  );
}

function size(bytes: number) {
  return bytes < 1024 ? bytes + ' B' : (bytes / 1024).toFixed(1) + ' KB';
}

function Entries() {
  const client = useQueryClient();
  const { openDrawer, showError, toast } = useFeedback();
  const entries = useCacheEntries();
  const list = entries.data ?? [];
  const reload = () => void client.invalidateQueries({ queryKey: ['cache', 'entries'] });
  const fail = (error: unknown) => showError((error as Error).message);

  const clearAll = async () => {
    if (
      !window.confirm(
        `Evict all ${list.length} cache entries? The next call to each query will re-run for real.`,
      )
    )
      return;
    try {
      unwrapEmpty(await api.DELETE('/api/v1/cache/entries'));
      toast('Cache cleared');
      reload();
    } catch (error) {
      fail(error);
    }
  };
  const remove = async (key: string) => {
    try {
      unwrapEmpty(await api.DELETE('/api/v1/cache/entries/{key}', { params: { path: { key } } }));
      toast('Entry deleted');
      reload();
    } catch (error) {
      fail(error);
    }
  };

  return (
    <>
      <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
        <div className="set-head" style={{ marginTop: 14 }}>
          <h2>Cache entries</h2>
          <span>{`${list.length}${list.length === 1 ? ' live entry' : ' live entries'} right now.`}</span>
        </div>
        <span className="spacer" />
        <button type="button" className="btn sm" disabled={!list.length} onClick={() => void clearAll()}>
          Clear cache
        </button>
      </div>
      {!list.length ? (
        <div className="hint">
          Nothing cached right now - call a query with a cache_ttl set, then check back.
        </div>
      ) : (
        <div className="panel" style={{ overflowX: 'auto', marginTop: 8 }}>
          <table className="grid">
            <thead>
              <tr>
                {['Query', 'Version', 'Connection', 'Format', 'Content type', 'Size', 'TTL left', ''].map(
                  (t, i) => (
                    <th key={i} className={i >= 4 && i <= 6 ? 'num' : ''}>
                      {t}
                    </th>
                  ),
                )}
              </tr>
            </thead>
            <tbody>
              {list.map((e) => {
                const m = e.meta ?? {};
                return (
                  <tr key={e.key}>
                    <td>
                      <button
                        type="button"
                        className="target"
                        onClick={() =>
                          openDrawer({
                            title: m.name || 'Cache entry',
                            kicker: e.key,
                            content: <EntryBody entry={e} />,
                          })
                        }
                      >
                        {m.name || e.key.slice(0, 12) + '…'}
                      </button>
                    </td>
                    <td className="mono">{m.version !== undefined ? 'v' + m.version : '—'}</td>
                    <td className="mono">{m.connection || '—'}</td>
                    <td className="mono">{m.format || '—'}</td>
                    <td className="mono num">{e.content_type}</td>
                    <td className="mono num">{size(e.size_bytes)}</td>
                    <td className="mono num">{Math.max(0, Math.round(e.ttl_remaining_s)) + 's'}</td>
                    <td>
                      <button type="button" className="btn sm ghost" onClick={() => void remove(e.key)}>
                        Delete
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}

const failed = (message: string): ResultData => ({
  ok: false,
  status: 0,
  statusText: '',
  contentType: '',
  headers: [],
  page: 1,
  size: 0,
  hasMore: false,
  elapsed: 0,
  format: '',
  error: message,
});

/** The exact cached body, through the same result renderer the API Designer uses. */
function EntryBody({ entry }: { entry: Entry }) {
  const { showError } = useFeedback();
  const name = entry.meta?.name || 'cache-entry';
  const format = entry.meta?.format || 'json';
  const [result, setResult] = useState<ResultData | null>(null);
  useEffect(() => {
    let live = true;
    const t0 = performance.now();
    apiFetch(`/api/v1/cache/entries/${encodeURIComponent(entry.key)}`)
      .then((response) =>
        readResult(response, {
          page: 1,
          size: 0,
          format,
          elapsed: Math.round(performance.now() - t0),
          filename: name,
        }),
      )
      .then(
        (read) => {
          if (!live) return;
          if (read.error) showError(read.error, { status: read.status }); // as /ui reports a failed response
          setResult(read);
        },
        (error: Error) => live && setResult(failed('Network error: ' + error.message)),
      );
    return () => {
      live = false;
    };
  }, [entry.key, format, name, showError]);
  return (
    <>
      <div className="hint">
        The exact cached body, served with its real Content-Type - what a caller receives on a hit.
      </div>
      <Results
        result={result}
        running={!result}
        filename={name}
        onPage={() => {}}
        onPageSize={() => {}}
        layout="drawer"
      />
    </>
  );
}

import { useQuery } from '@tanstack/react-query';

import { apiFetch } from '@/api/client';
import { parseMetricsText, type Series } from '@/lib/metrics';

// /metrics, parsed - shared by Home, Caching and Metrics, as the classic UI's one metricsSeries was. Loaded once
// and kept until a Refresh; the two connection-pool gauges have their own 2s poll while a screen shows them.

async function fetchSeries(): Promise<Series[]> {
  const response = await apiFetch('/metrics');
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return parseMetricsText(await response.text());
}

export function useMetricsSeries() {
  return useQuery({ queryKey: ['metrics'], queryFn: fetchSeries, retry: false, staleTime: Infinity });
}

export function usePoolPoll(enabled: boolean) {
  return useQuery({
    queryKey: ['metrics', 'pool'],
    queryFn: fetchSeries,
    retry: false,
    refetchInterval: 2000,
    enabled,
  });
}

import { useQuery } from '@tanstack/react-query';

import { api, unwrap } from '@/api/client';
import { useGate } from '@/app/data';

// What the CDC sources screen shares with its tests (ADR 0033 §11).

export function useCdcSources() {
  const gate = useGate('connections.read', 'CDC sources');
  return useQuery({
    queryKey: ['cdc-sources'],
    queryFn: async () => {
      await gate.check();
      return unwrap(await api.GET('/api/v1/cdc-sources')).items;
    },
    enabled: gate.ready,
    retry: false,
  });
}

/** Debezium Server's sink lines for a source: where to post, and how to sign. The secret goes in as an environment
 * variable, which Debezium Server expands in its properties - so the file never holds it. */
export function sinkProperties(ingestPath: string, origin = window.location.origin) {
  return [
    'debezium.sink.type=http',
    `debezium.sink.http.url=${origin}${ingestPath}`,
    'debezium.sink.http.batch.enabled=true',
    'debezium.sink.http.authentication.type=standard-webhooks',
    'debezium.sink.http.authentication.webhook.secret=${WEBHOOK_SECRET}',
    'debezium.format.value=json',
  ].join('\n');
}

import { useQuery } from '@tanstack/react-query';

import { api, unwrap } from '@/api/client';

import { useGate } from './data';

/** Pending write requests, for the sidebar count - only for a role that may decide them. Asked when the request is
 * about to go out (useGate), so a tab that just switched to a key without `writes.approve` never sends one. */
export function usePendingWrites() {
  const gate = useGate('writes.approve', 'write requests');
  return useQuery({
    queryKey: ['write-requests', 'pending'],
    queryFn: async () => {
      await gate.check();
      return unwrap(await api.GET('/api/v1/write-requests', { params: { query: { status: 'pending' } } })).items;
    },
    enabled: gate.ready,
    refetchInterval: 30_000,
    retry: false,
  });
}

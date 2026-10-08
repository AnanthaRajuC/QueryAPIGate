import { useQuery } from '@tanstack/react-query';

import { ApiError, api, unwrap } from '@/api/client';
import { useGate } from '@/app/data';
import { useFeedback } from '@/app/feedback';

// What the Exports and Destinations screens share (ADR 0004).

export function useReport() {
  const { showError } = useFeedback();
  return (error: unknown) =>
    error instanceof ApiError
      ? showError(error.message, { status: error.status })
      : showError((error as Error).message);
}

export function useDestinations() {
  const gate = useGate('exports.read', 'destinations');
  return useQuery({
    queryKey: ['destinations'],
    queryFn: async () => {
      await gate.check();
      return unwrap(await api.GET('/api/v1/destinations')).items;
    },
    enabled: gate.ready,
    retry: false,
  });
}

export function useExports() {
  const gate = useGate('exports.read', 'exports');
  return useQuery({
    queryKey: ['exports'],
    queryFn: async () => {
      await gate.check();
      return unwrap(await api.GET('/api/v1/exports')).items;
    },
    enabled: gate.ready,
    retry: false,
  });
}

import { useQuery } from '@tanstack/react-query';

import { api, unwrap } from './client';

export function useHealth() {
  return useQuery({
    queryKey: ['health'],
    queryFn: async () => unwrap(await api.GET('/health')),
  });
}

/** Who the current key is and what it can reach - also how the Console checks that a key is valid. */
export function useCatalog(apiKey: string) {
  return useQuery({
    queryKey: ['catalog', apiKey],
    queryFn: async () => unwrap(await api.GET('/catalog')),
    retry: false,
  });
}

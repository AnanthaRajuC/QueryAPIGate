import { useQueryClient } from '@tanstack/react-query';
import { useCallback, useEffect } from 'react';
import { useNavigate } from 'react-router';

import { setWorkspace, workspaceToSettle } from '@/auth/workspace';

import { useMe } from './data';

// The tab's workspace (ADR 0023): moving between them, and where a tab starts.

/** Move this tab to `name`: everything shown came from the workspace it leaves, so all of it is asked again - from
 * Home, since the screen it was on may name something the new workspace hasn't got. */
export function useSwitchWorkspace() {
  const client = useQueryClient();
  const navigate = useNavigate();
  return useCallback(
    (name: string) => {
      setWorkspace(name);
      void client.resetQueries();
      navigate('/');
    },
    [client, navigate],
  );
}

/** Where a tab starts: an administrator whose roles are all in workspaces starts in their first one - in `default`
 * they'd be refused everything - and a tab left in a workspace that's gone (or was never theirs) goes back to
 * `default` (workspaceToSettle()). Until it has moved, the API client holds every request but /api/v1/me. */
export function useSettleWorkspace() {
  const me = useMe();
  const switchTo = useSwitchWorkspace();
  const target = workspaceToSettle(me);
  useEffect(() => {
    if (target !== null) switchTo(target);
  }, [target, switchTo]);
}

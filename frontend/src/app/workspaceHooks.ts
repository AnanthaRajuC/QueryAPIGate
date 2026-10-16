import { useQueryClient } from '@tanstack/react-query';
import { useCallback, useEffect } from 'react';
import { useNavigate } from 'react-router';

import { ApiError } from '@/api/client';
import { DEFAULT_WORKSPACE, getWorkspace, setWorkspace } from '@/auth/workspace';

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
 * `default`. */
export function useSettleWorkspace() {
  const me = useMe();
  const switchTo = useSwitchWorkspace();
  useEffect(() => {
    const current = getWorkspace();
    const gone =
      me.error instanceof ApiError && me.error.status === 404 && me.error.code === 'workspace_not_found';
    if (gone && current !== DEFAULT_WORKSPACE) {
      switchTo(DEFAULT_WORKSPACE);
      return;
    }
    const memberships = Object.keys(me.data?.workspaces ?? {});
    const first = memberships[0];
    if (me.data && me.data.role === null && first !== undefined && !memberships.includes(current)) {
      switchTo(first);
    }
  }, [me.data, me.error, switchTo]);
}

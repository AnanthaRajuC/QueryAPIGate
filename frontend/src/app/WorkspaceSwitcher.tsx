import { getWorkspace } from '@/auth/workspace';

import { useMe, useWorkspaces } from './data';
import { useSwitchWorkspace } from './workspaceHooks';

/** The workspace picker under the wordmark - only once there is a choice to make: a second workspace, or a tab
 * that is in one the caller can't see. A server with only `default` never shows it. */
export function WorkspaceSwitcher() {
  const me = useMe();
  const list = useWorkspaces(Boolean(me.data));
  const switchTo = useSwitchWorkspace();
  const current = getWorkspace();
  const names = (list.data ?? []).map((w) => w.name);
  if (!names.includes(current)) names.unshift(current);
  if (names.length < 2) return null;
  return (
    <label className="ws-switch" title="The workspace this tab works in (ADR 0023)">
      <span className="dim">Workspace</span>
      <select id="workspace" value={current} onChange={(e) => switchTo(e.target.value)}>
        {names.map((name) => (
          <option key={name} value={name}>
            {name}
          </option>
        ))}
      </select>
    </label>
  );
}

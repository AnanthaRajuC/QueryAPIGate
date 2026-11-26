import { DEFAULT_WORKSPACE, getWorkspace } from '@/auth/workspace';

import { useMe, useWorkspaces } from './data';
import { useSwitchWorkspace } from './workspaceHooks';

/** The workspaces this tab can move to, the one it's in (listed even when the caller can't see it), and the move. */
function useWorkspaceChoice() {
  const me = useMe();
  const list = useWorkspaces(Boolean(me.data));
  const switchTo = useSwitchWorkspace();
  const current = getWorkspace();
  const names = (list.data ?? []).map((w) => w.name);
  if (!names.includes(current)) names.unshift(current);
  return { names, current, switchTo };
}

/** The workspace picker under the wordmark - only once there is a choice to make: a second workspace, or a tab
 * that is in one the caller can't see. A server with only `default` never shows it. */
export function WorkspaceSwitcher() {
  const { names, current, switchTo } = useWorkspaceChoice();
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

/** The same choice in the top bar, while this tab is outside `default` - so whichever screen is open, it says which
 * workspace the connections, queries and keys on it belong to. In `default` there's nothing to say, except in the
 * icon rail (collapsed, or a narrow screen), which has no room for the switcher: there the chip stands in for it
 * whenever there is a choice (`rail-only`, shown by console.css). */
export function WorkspaceChip() {
  const { names, current, switchTo } = useWorkspaceChoice();
  const inDefault = current === DEFAULT_WORKSPACE;
  if (inDefault && names.length < 2) return null;
  return (
    <label
      className={inDefault ? 'ws-chip rail-only' : 'ws-chip'}
      id="workspace-chip"
      title={`This tab works in the ${current} workspace (ADR 0023)`}
    >
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <path d="m12 3 9 4.5-9 4.5-9-4.5Z" />
        <path d="m3 12 9 4.5 9-4.5" />
        <path d="m3 16.5 9 4.5 9-4.5" />
      </svg>
      <select aria-label="Workspace" value={current} onChange={(e) => switchTo(e.target.value)}>
        {names.map((name) => (
          <option key={name} value={name}>
            {name}
          </option>
        ))}
      </select>
    </label>
  );
}

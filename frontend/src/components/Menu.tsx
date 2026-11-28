import { useCallback, useEffect, useId, useRef, useState, type KeyboardEvent, type ReactNode } from 'react';

import { useDismiss } from './useDismiss';

// A button that opens a menu of actions under it (the ⋯ menus). The menu follows the WAI-ARIA menu button pattern:
// Enter, Space or ArrowDown on the button opens it with the first item focused; ArrowUp/ArrowDown move through the
// items (wrapping), Home and End jump to the ends; Escape closes it and puts focus back on the button, and Tab closes
// it and moves on. A pressed item closes the menu and then runs.

export type MenuEntry =
  | { label: string; title?: string; danger?: boolean; hidden?: boolean; onSelect: () => void }
  /** A line that says something rather than doing something ("Granted to 2 keys"): reachable, not pressable. */
  | { note: string; title?: string }
  | 'separator';

export function MenuButton({
  label,
  entries,
  className = 'btn icon',
  children,
}: {
  /** The button's accessible name, and the menu's. */
  label: string;
  entries: MenuEntry[];
  className?: string;
  /** What the button shows: an icon, or text. */
  children: ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const button = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  const id = useId();

  const close = useCallback((refocus: boolean) => {
    setOpen(false);
    if (refocus) button.current?.focus();
  }, []);
  useDismiss(open, [button, menu], close);
  useEffect(() => {
    if (open) items(menu.current)[0]?.focus();
  }, [open]);

  const shown = entries.filter((e) => e === 'separator' || !('hidden' in e) || !e.hidden);
  if (!shown.some((e) => e !== 'separator' && 'onSelect' in e)) return null;

  const onMenuKey = (e: KeyboardEvent) => {
    const list = items(menu.current);
    const at = list.indexOf(document.activeElement as HTMLElement);
    const go = (i: number) => {
      e.preventDefault();
      list[(i + list.length) % list.length]?.focus();
    };
    if (e.key === 'ArrowDown') go(at + 1);
    else if (e.key === 'ArrowUp') go(at - 1);
    else if (e.key === 'Home') go(0);
    else if (e.key === 'End') go(list.length - 1);
    else if (e.key === 'Tab') close(false);
  };

  return (
    <span className="menu-anchor">
      <button
        ref={button}
        type="button"
        className={className}
        aria-label={typeof children === 'string' ? undefined : label}
        title={label}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? id : undefined}
        onClick={() => setOpen((o) => !o)}
        onKeyDown={(e) => {
          if (e.key === 'ArrowDown' && !open) {
            e.preventDefault();
            setOpen(true);
          }
        }}
      >
        {children}
      </button>
      {open && (
        <div ref={menu} id={id} className="menu-pop" role="menu" aria-label={label} onKeyDown={onMenuKey}>
          {shown.map((entry, i) => {
            if (entry === 'separator') return <div key={i} role="separator" className="menu-sep" />;
            if ('note' in entry)
              return (
                <div
                  key={i}
                  role="menuitem"
                  aria-disabled="true"
                  tabIndex={-1}
                  className="menu-note"
                  title={entry.title}
                >
                  {entry.note}
                </div>
              );
            return (
              <button
                key={i}
                type="button"
                role="menuitem"
                tabIndex={-1}
                className={entry.danger ? 'danger' : undefined}
                title={entry.title}
                onClick={() => {
                  close(true);
                  entry.onSelect();
                }}
              >
                {entry.label}
              </button>
            );
          })}
        </div>
      )}
    </span>
  );
}

function items(menu: HTMLElement | null): HTMLElement[] {
  return menu ? [...menu.querySelectorAll<HTMLElement>('[role="menuitem"]')] : [];
}

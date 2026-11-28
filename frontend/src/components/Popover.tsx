import { useCallback, useEffect, useId, useRef, useState, type ReactNode } from 'react';

import { useDismiss } from './useDismiss';

// A button that opens a small panel of controls under it (the query list's filters). The panel is a non-modal
// dialog: opening it focuses its first control, Escape closes it and puts focus back on the button, and a press
// outside it closes it.

export function PopoverButton({
  label,
  className = 'btn icon',
  button,
  children,
}: {
  /** The button's accessible name, and the panel's. */
  label: string;
  className?: string;
  /** What the button shows. */
  button: ReactNode;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null);
  const panel = useRef<HTMLDivElement>(null);
  const id = useId();

  const close = useCallback((refocus: boolean) => {
    setOpen(false);
    if (refocus) trigger.current?.focus();
  }, []);
  useDismiss(open, [trigger, panel], close);
  useEffect(() => {
    if (open) panel.current?.querySelector<HTMLElement>('select, input, button, [tabindex]')?.focus();
  }, [open]);

  return (
    <span className="menu-anchor">
      <button
        ref={trigger}
        type="button"
        className={className}
        aria-label={label}
        title={label}
        aria-expanded={open}
        aria-controls={open ? id : undefined}
        onClick={() => setOpen((o) => !o)}
      >
        {button}
      </button>
      {open && (
        <div ref={panel} id={id} className="popover" role="dialog" aria-label={label}>
          {children}
        </div>
      )}
    </span>
  );
}

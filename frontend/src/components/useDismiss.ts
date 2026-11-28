import { useEffect, type RefObject } from 'react';

/** While `open`, a press outside every one of `inside` (the button and what it opened) calls `onDismiss`, and so does
 * Escape - with `escape` true when it was Escape, so the caller can hand focus back to its button. */
export function useDismiss(
  open: boolean,
  inside: RefObject<HTMLElement | null>[],
  onDismiss: (escape: boolean) => void,
) {
  useEffect(() => {
    if (!open) return;
    const outside = (e: MouseEvent) => {
      if (!inside.some((r) => r.current?.contains(e.target as Node))) onDismiss(false);
    };
    const esc = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return;
      e.stopPropagation();
      onDismiss(true);
    };
    document.addEventListener('mousedown', outside, true);
    document.addEventListener('keydown', esc, true);
    return () => {
      document.removeEventListener('mousedown', outside, true);
      document.removeEventListener('keydown', esc, true);
    };
    // `inside` is a fresh array of the same refs on each render
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, onDismiss]);
}

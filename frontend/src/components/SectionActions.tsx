import { useContext, type ReactNode } from 'react';
import { createPortal } from 'react-dom';

import { InSection, SectionActionsSlot } from './inSection';

// A screen's main actions ("New API"). Inside a section they sit at the right end of the section's tab row, so the
// screen doesn't need a header band of its own; on their own they are a row at the top of the screen.

export function SectionActions({ children }: { children: ReactNode }) {
  const inSection = useContext(InSection);
  const slot = useContext(SectionActionsSlot);
  if (!inSection) return <div className="page-actions">{children}</div>;
  return slot ? createPortal(children, slot) : null;
}

import { createContext } from 'react';

/** Whether a screen is shown as a tab of a section (SectionLayout) - its title then a level down (PageTitle). */
export const InSection = createContext(false);

/** Where a screen's own actions go inside a section: the right end of the section's tab row (SectionActions). */
export const SectionActionsSlot = createContext<HTMLElement | null>(null);

import { useContext, type HTMLAttributes } from 'react';

import { InSection } from './inSection';

// A screen's title. On its own it is the page's heading (h1). Inside a section (SectionLayout: one sidebar item whose
// screens are tabs), the section's name is the page's heading and the tab already names the screen - so the screen's
// title is a level down, and hidden on a screen the tab names (`tab`); a screen the tab doesn't name, such as one API
// key's details, still shows its own.

export function PageTitle({
  tab = false,
  className,
  ...rest
}: HTMLAttributes<HTMLHeadingElement> & { tab?: boolean }) {
  if (!useContext(InSection)) return <h1 className={className} {...rest} />;
  const classes = ['page-title', tab ? 'visually-hidden' : '', className ?? ''].filter(Boolean).join(' ');
  return <h2 className={classes} {...rest} />;
}

import { useState } from 'react';
import { Navigate, NavLink, Outlet } from 'react-router';

import { InSection, SectionActionsSlot } from '@/components/inSection';

import { useCan, useMe } from './data';
import { Loading } from './feedback';
import { NavCount } from './navCounts';
import { SECTIONS, type NavItem } from './navigation';

// A sidebar group shown as one item (navigation.tsx, `section`): its page - a tab for each of its screens the caller
// may see, with how many there are where that's known, and the open screen's main actions at the row's right end
// (SectionActions) - and below them the screen itself, at its own route. The group's name is the page's heading, kept
// for a screen reader but not shown (the sidebar and the breadcrumb already say it), and what the group is for is the
// sidebar item's tooltip. The screen's title is the tab's (PageTitle), so it isn't said twice.

function useVisibleItems(id: string): NavItem[] {
  const can = useCan();
  return SECTIONS[id]!.items.filter((item) => !item.needs || can(item.needs));
}

export function SectionLayout({ id }: { id: string }) {
  const { section } = SECTIONS[id]!;
  const items = useVisibleItems(id);
  const [actions, setActions] = useState<HTMLElement | null>(null);
  return (
    <InSection.Provider value>
      <SectionActionsSlot.Provider value={actions}>
        <h1 className="visually-hidden">{section.label}</h1>
        <div className="section-tabbar">
          <nav className="section-tabs" aria-label={section.label}>
            {items.map((item) => (
              <NavLink key={item.tab} to={item.path} data-tab={item.tab}>
                <span className="section-tab-icon">{item.icon}</span>
                {item.label}
                {item.count && <NavCount kind={item.count} />}
              </NavLink>
            ))}
          </nav>
          <div className="section-actions" ref={setActions} />
        </div>
        <div className="section-body">
          <Outlet />
        </div>
      </SectionActionsSlot.Provider>
    </InSection.Provider>
  );
}

/** The section's own address (/access): its first screen the caller may see - once /api/v1/me has said what that
 * is (until then every screen counts as allowed, and a developer would land on API keys). */
export function SectionIndex({ id }: { id: string }) {
  const me = useMe();
  const first = useVisibleItems(id)[0];
  if (me.isPending) return <Loading />;
  return <Navigate to={first ? first.path : '/'} replace />;
}

import { Navigate, NavLink, Outlet } from 'react-router';

import { InSection } from '@/components/inSection';

import { useCan, useMe } from './data';
import { Loading } from './feedback';
import { NavCount } from './navCounts';
import { SECTIONS, type NavItem } from './navigation';

// A sidebar group shown as one item (navigation.tsx, `section`): its page - the group's name and what it is for, then
// a tab for each of its screens the caller may see, with how many there are where that's known - and below them the
// screen itself, at its own route. The screen's title is the tab's (PageTitle), so it isn't said twice.

function useVisibleItems(id: string): NavItem[] {
  const can = useCan();
  return SECTIONS[id]!.items.filter((item) => !item.needs || can(item.needs));
}

export function SectionLayout({ id }: { id: string }) {
  const { section } = SECTIONS[id]!;
  const items = useVisibleItems(id);
  return (
    <InSection.Provider value>
      <div className="section-head">
        <div className="titles">
          <h1>{section.label}</h1>
          <span className="sub">{section.description}</span>
        </div>
      </div>
      <nav className="section-tabs" aria-label={section.label}>
        {items.map((item) => (
          <NavLink key={item.tab} to={item.path} data-tab={item.tab}>
            <span className="section-tab-icon">{item.icon}</span>
            {item.label}
            {item.count && <NavCount kind={item.count} />}
          </NavLink>
        ))}
      </nav>
      <div className="section-body">
        <Outlet />
      </div>
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

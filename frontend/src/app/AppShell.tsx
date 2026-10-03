import { Menu, X } from 'lucide-react';
import { useState } from 'react';
import { NavLink, Outlet } from 'react-router';

import { Badge } from '@/components/ui/badge';
import { cn } from '@/lib/utils';

import { NAVIGATION } from './navigation';
import { SignIn } from './SignIn';

export function AppShell() {
  // Below the md breakpoint the navigation is a toggled menu, so the page content stays on screen.
  const [menuOpen, setMenuOpen] = useState(false);
  return (
    <div className="grid min-h-screen grid-cols-1 grid-rows-[auto_1fr] md:grid-cols-[232px_minmax(0,1fr)] md:grid-rows-none">
      <aside className="border-b border-line bg-surface md:sticky md:top-0 md:h-screen md:overflow-y-auto md:border-r md:border-b-0">
        <div className="flex h-13 items-center gap-2 border-b border-line px-4">
          <span className="font-mono text-[13px] font-bold tracking-wide">QueryAPIGate</span>
          <Badge variant="accent">Console</Badge>
          <button
            type="button"
            className="ml-auto rounded-md p-1.5 text-ink-2 hover:bg-surface-2 md:hidden"
            aria-label={menuOpen ? 'Close menu' : 'Open menu'}
            aria-expanded={menuOpen}
            onClick={() => setMenuOpen((open) => !open)}
          >
            {menuOpen ? <X size={18} /> : <Menu size={18} />}
          </button>
        </div>
        <nav aria-label="Console" className={cn('flex-col gap-4 p-3 md:flex', menuOpen ? 'flex' : 'hidden')}>
          {NAVIGATION.map((section, index) => (
            <div key={section.title ?? index}>
              {section.title && (
                <div className="px-2 pb-1 text-[11px] font-semibold tracking-wider text-ink-3 uppercase">
                  {section.title}
                </div>
              )}
              <ul className="flex flex-col">
                {section.items.map((item) => (
                  <li key={item.path}>
                    <NavLink
                      to={item.path}
                      onClick={() => setMenuOpen(false)}
                      end={item.path === '/'}
                      className={({ isActive }) =>
                        cn(
                          'flex items-center justify-between rounded-md px-2 py-1.5 text-[13px] text-ink-2 hover:bg-surface-2 hover:text-ink',
                          isActive && 'bg-accent-soft text-accent hover:bg-accent-soft hover:text-accent',
                        )
                      }
                    >
                      {item.label}
                      {item.classicTab && <span className="text-[10px] text-ink-3">classic</span>}
                    </NavLink>
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </nav>
      </aside>
      <div className="min-w-0">
        <header className="flex h-13 items-center justify-between gap-4 border-b border-line bg-surface px-4 md:px-6">
          <span className="hidden text-xs text-ink-3 sm:inline">
            Experimental - screens move here from the{' '}
            <a href="/ui" className="underline hover:text-accent">
              classic UI
            </a>{' '}
            one at a time.
          </span>
          <SignIn />
        </header>
        <main className="p-4 md:p-6">
          <Outlet />
        </main>
      </div>
    </div>
  );
}

import type { NavItem } from '@/app/navigation';
import { classicUiHref } from '@/app/navigation';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';

/** Placeholder for a screen that still lives in the classic UI. */
export function ClassicScreen({ item }: { item: NavItem }) {
  return (
    <div className="flex max-w-2xl flex-col gap-4">
      <h1 className="text-lg font-semibold">{item.label}</h1>
      <Card>
        <p className="text-sm text-ink-2">{item.description}</p>
        <p className="mt-3 text-sm">
          This screen hasn&apos;t moved to the Console yet. It works in the classic UI.
        </p>
        <Button asChild className="mt-4">
          <a href="/ui" onClick={() => classicUiHref(item.classicTab ?? 'home')}>
            Open in the classic UI
          </a>
        </Button>
      </Card>
    </div>
  );
}

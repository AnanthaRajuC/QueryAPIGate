import type { ReactNode } from 'react';

import { useFormatTime } from '@/lib/useFormatTime';

// A server timestamp, shown as Settings > Appearance says: in this computer's time zone, UTC or the server's, as a date
// and time or as "5 min ago". The tooltip always has the exact time and its zone.

/** A server timestamp in a <time>; `fallback` when there is none. A value that isn't a date and time is shown as is. */
export function Time({ value, fallback = '—' }: { value: string | null | undefined; fallback?: ReactNode }) {
  const format = useFormatTime();
  if (!value) return <>{fallback}</>;
  const t = format(value);
  return t ? (
    <time dateTime={t.iso} title={t.title}>
      {t.text}
    </time>
  ) : (
    <time>{value}</time>
  );
}

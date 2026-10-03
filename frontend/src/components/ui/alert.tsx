import * as React from 'react';

import { cn } from '@/lib/utils';

export function Alert({
  variant = 'info',
  className,
  ...props
}: React.HTMLAttributes<HTMLDivElement> & { variant?: 'info' | 'danger' | 'warn' }) {
  return (
    <div
      role={variant === 'danger' ? 'alert' : 'status'}
      className={cn(
        'rounded-md border px-3 py-2 text-sm',
        variant === 'danger' && 'border-danger/40 bg-danger-soft text-danger',
        variant === 'warn' && 'border-warn/40 bg-warn/10 text-ink',
        variant === 'info' && 'border-line bg-surface-2 text-ink-2',
        className,
      )}
      {...props}
    />
  );
}

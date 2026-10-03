import * as React from 'react';

import { cn } from '@/lib/utils';

const control =
  'w-full rounded-md border border-line-strong bg-surface px-3 text-sm placeholder:text-ink-3 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-60';

export function Label({ className, ...props }: React.LabelHTMLAttributes<HTMLLabelElement>) {
  return <label className={cn('text-xs font-medium text-ink-2', className)} {...props} />;
}

export function Select({ className, ...props }: React.SelectHTMLAttributes<HTMLSelectElement>) {
  return <select className={cn(control, 'h-9', className)} {...props} />;
}

export function Textarea({ className, ...props }: React.TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return <textarea className={cn(control, 'min-h-16 py-2', className)} {...props} />;
}

/** A labelled form field with an optional hint and error, wired up for screen readers. */
export function Field({
  label,
  htmlFor,
  hint,
  error,
  children,
  className,
}: {
  label: string;
  htmlFor: string;
  hint?: string;
  error?: string;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <div className={cn('flex flex-col gap-1', className)}>
      <Label htmlFor={htmlFor}>{label}</Label>
      {children}
      {error ? (
        <p id={`${htmlFor}-error`} role="alert" className="text-xs text-danger">
          {error}
        </p>
      ) : (
        hint && <p className="text-xs text-ink-3">{hint}</p>
      )}
    </div>
  );
}

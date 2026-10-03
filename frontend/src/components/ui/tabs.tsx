import { cn } from '@/lib/utils';

/** Simple accessible tabs: a tablist of buttons; the caller renders the active panel. */
export function Tabs<T extends string>({
  tabs,
  value,
  onChange,
  label,
}: {
  tabs: { id: T; label: string }[];
  value: T;
  onChange: (id: T) => void;
  label: string;
}) {
  return (
    <div role="tablist" aria-label={label} className="flex gap-1 border-b border-line">
      {tabs.map((tab) => (
        <button
          key={tab.id}
          type="button"
          role="tab"
          id={`tab-${tab.id}`}
          aria-selected={value === tab.id}
          aria-controls={`panel-${tab.id}`}
          onClick={() => onChange(tab.id)}
          className={cn(
            '-mb-px border-b-2 px-3 py-2 text-sm text-ink-2 hover:text-ink',
            value === tab.id ? 'border-accent font-medium text-ink' : 'border-transparent',
          )}
        >
          {tab.label}
        </button>
      ))}
    </div>
  );
}

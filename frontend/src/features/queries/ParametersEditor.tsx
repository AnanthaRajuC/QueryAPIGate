import { Select } from '@/components/ui/form';
import { Input } from '@/components/ui/input';

import { EMPTY_PARAM, type ParamDraft, type ParamType } from './parameters';

const TYPES: { value: ParamType; label: string }[] = [
  { value: '', label: 'any' },
  { value: 'integer', label: 'integer' },
  { value: 'number', label: 'number' },
  { value: 'string', label: 'string' },
  { value: 'boolean', label: 'boolean' },
];

/** One row per :placeholder the SQL uses: type, required, default and description. */
export function ParametersEditor({
  placeholders,
  drafts,
  onChange,
}: {
  placeholders: string[];
  drafts: Record<string, ParamDraft>;
  onChange: (name: string, draft: ParamDraft) => void;
}) {
  if (placeholders.length === 0) {
    return (
      <p className="text-sm text-ink-3">
        No parameters. Write <span className="font-mono">:name</span> in the SQL to add one.
      </p>
    );
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-sm" aria-label="Parameter rules">
        <thead className="text-xs text-ink-2">
          <tr>
            <th className="py-1 pr-2 font-medium">Parameter</th>
            <th className="py-1 pr-2 font-medium">Type</th>
            <th className="py-1 pr-2 font-medium">Required</th>
            <th className="py-1 pr-2 font-medium">Default</th>
            <th className="py-1 pr-2 font-medium">Description</th>
          </tr>
        </thead>
        <tbody>
          {placeholders.map((name) => {
            const draft = drafts[name] ?? EMPTY_PARAM;
            const set = (patch: Partial<ParamDraft>) => onChange(name, { ...draft, ...patch });
            const fromClaim = typeof draft.extra.from_claim === 'string';
            return (
              <tr key={name} className="border-t border-line">
                <td className="py-1.5 pr-2 font-mono whitespace-nowrap">
                  {name}
                  {fromClaim && (
                    <span className="block font-sans text-xs text-ink-3">
                      from token claim {draft.extra.from_claim}
                    </span>
                  )}
                </td>
                <td className="py-1.5 pr-2">
                  <Select
                    aria-label={`Type of ${name}`}
                    value={draft.type}
                    onChange={(e) => set({ type: e.target.value as ParamType })}
                    className="h-8 w-28"
                  >
                    {TYPES.map((t) => (
                      <option key={t.value} value={t.value}>
                        {t.label}
                      </option>
                    ))}
                  </Select>
                </td>
                <td className="py-1.5 pr-2">
                  <input
                    type="checkbox"
                    aria-label={`${name} is required`}
                    checked={draft.required}
                    disabled={fromClaim}
                    onChange={(e) => set({ required: e.target.checked })}
                    className="size-4 accent-[var(--color-accent)]"
                  />
                </td>
                <td className="py-1.5 pr-2">
                  <Input
                    aria-label={`Default for ${name}`}
                    value={draft.default}
                    disabled={fromClaim}
                    onChange={(e) =>
                      set({ default: e.target.value, required: e.target.value ? false : draft.required })
                    }
                    className="h-8 w-32"
                  />
                </td>
                <td className="py-1.5 pr-2">
                  <Input
                    aria-label={`Description of ${name}`}
                    value={draft.description}
                    onChange={(e) => set({ description: e.target.value })}
                    className="h-8 min-w-40"
                  />
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

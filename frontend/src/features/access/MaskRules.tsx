// Column-masking rules (ADR 0005) as a form field: one row per rule - a source column and what to do with its values.

import type { MaskRule } from './maskRules';

const ACTIONS: { value: MaskRule['action']; label: string }[] = [
  { value: 'hash', label: 'hash - same value, same code' },
  { value: 'mask', label: 'mask - j***@example.com' },
  { value: 'null', label: 'null' },
  { value: 'drop', label: 'drop the column' },
  { value: 'keep', label: 'keep - fine as it is' },
];

export function MaskRulesField({
  id,
  rules,
  onChange,
  who,
}: {
  id: string;
  rules: MaskRule[];
  onChange: (rules: MaskRule[]) => void;
  /** Whose results the rules apply to, for the hint: "this key", "keys from this role", "this export". */
  who: string;
}) {
  const update = (i: number, change: Partial<MaskRule>) =>
    onChange(rules.map((rule, j) => (j === i ? { ...rule, ...change } : rule)));
  return (
    <fieldset className="duck-files" id={id}>
      <legend>
        Column masking <span className="tag">experimental</span>
      </legend>
      <div className="hint">
        Values of these source columns are hidden in everything {who} receives - whatever the SQL calls them.
        A query masking can&apos;t trace is refused, never served unmasked. Table and column, with * as a
        wildcard: customers.email, *.phone.
      </div>
      {rules.map((rule, i) => (
        <div className="grid2" key={i} style={{ gridTemplateColumns: '2fr 2fr auto', alignItems: 'end' }}>
          <input
            aria-label={`Masked column ${i + 1}`}
            className="mono"
            value={rule.column}
            placeholder="customers.email"
            spellCheck={false}
            onChange={(e) => update(i, { column: e.target.value })}
          />
          <select
            aria-label={`Masking action ${i + 1}`}
            value={rule.action}
            onChange={(e) => update(i, { action: e.target.value as MaskRule['action'] })}
          >
            {ACTIONS.map((a) => (
              <option key={a.value} value={a.value}>
                {a.label}
              </option>
            ))}
          </select>
          <button
            type="button"
            className="btn ghost sm"
            aria-label={`Remove rule ${i + 1}`}
            onClick={() => onChange(rules.filter((_, j) => j !== i))}
          >
            Remove
          </button>
        </div>
      ))}
      <div>
        <button
          type="button"
          className="btn sm"
          onClick={() => onChange([...rules, { column: '', action: 'hash' }])}
        >
          Add a masking rule
        </button>
      </div>
    </fieldset>
  );
}

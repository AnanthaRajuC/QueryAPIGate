// Column-masking rules (ADR 0005): their shape, and what a form sends.

export interface MaskRule {
  column: string;
  action: 'hash' | 'mask' | 'null' | 'drop' | 'keep';
}

/** The rules to send: complete rows only, or `empty` (undefined to leave out, null to clear) when there are none. */
export function maskPayload(rules: MaskRule[], empty: null | undefined) {
  const complete = rules.map((r) => ({ column: r.column.trim(), action: r.action })).filter((r) => r.column);
  return complete.length ? complete : empty;
}

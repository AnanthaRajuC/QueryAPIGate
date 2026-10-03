import type { ParameterRule } from './api';

export type ParamType = '' | 'integer' | 'number' | 'string' | 'boolean';

/** One parameter as the editor's form holds it. `extra` keeps every rule the form doesn't edit (enum, min, max,
 * pattern, from_claim, ...) so saving a new version never silently drops them. */
export interface ParamDraft {
  type: ParamType;
  required: boolean;
  default: string;
  description: string;
  extra: Omit<ParameterRule, 'type' | 'required' | 'default' | 'description'>;
}

export const EMPTY_PARAM: ParamDraft = { type: '', required: true, default: '', description: '', extra: {} };

export function toDraft(rule: ParameterRule | undefined): ParamDraft {
  if (!rule) return EMPTY_PARAM;
  const { type, required, default: value, description, ...extra } = rule;
  return {
    type: (type ?? '') as ParamType,
    required,
    default: 'default' in rule && value !== undefined && value !== null ? String(value) : '',
    description: description ?? '',
    extra,
  };
}

/** A typed value from the text the user typed, for a default or a test value; undefined when blank. */
export function parseValue(type: ParamType, text: string): unknown {
  if (text === '') return undefined;
  if (type === 'integer' || type === 'number') {
    const n = Number(text);
    return Number.isFinite(n) ? n : text; // left as text: the server reports it with its own message
  }
  if (type === 'boolean') return ['true', '1', 'yes'].includes(text.trim().toLowerCase());
  return text;
}

/** The parameter rules to save: only placeholders the SQL still uses, and only those with any rule set - an
 * undeclared placeholder is a required, untyped parameter, which needs no declaration. */
export function toRules(
  placeholders: string[],
  drafts: Record<string, ParamDraft>,
): Record<string, ParameterRule> {
  const rules: Record<string, ParameterRule> = {};
  for (const name of placeholders) {
    const draft = drafts[name];
    if (!draft) continue;
    const value = parseValue(draft.type, draft.default);
    const declared =
      draft.type !== '' ||
      !draft.required ||
      value !== undefined ||
      draft.description !== '' ||
      Object.keys(draft.extra).length > 0;
    if (!declared) continue;
    const rule: ParameterRule = { ...draft.extra, type: draft.type || null, required: draft.required };
    if (value !== undefined) rule.default = value;
    if (draft.description) rule.description = draft.description;
    rules[name] = rule;
  }
  return rules;
}

/** Placeholders (:name) in SQL, in order of first use - an instant local approximation, so the parameters list
 * updates while typing; the server's validate call is authoritative (it also skips strings and comments). */
export function localPlaceholders(sql: string): string[] {
  const names: string[] = [];
  const stripped = sql
    .replace(/'(?:''|[^'])*'/g, "''")
    .replace(/--[^\n]*/g, '')
    .replace(/\/\*[\s\S]*?\*\//g, '');
  for (const match of stripped.matchAll(/(?<![:\w]):([A-Za-z_]\w*)/g)) {
    const name = match[1];
    if (name && !names.includes(name)) names.push(name);
  }
  return names;
}

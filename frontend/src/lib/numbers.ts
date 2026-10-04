/** 1234567.5 -> '1,234,567.5': separators in the integer part only, so a float shows exactly as it would plain. */
export function grouped(n: number): string {
  const text = String(n);
  if (!Number.isFinite(n) || /e/i.test(text)) return text;
  const [whole, fraction] = text.split('.');
  return whole!.replace(/\B(?=(\d{3})+(?!\d))/g, ',') + (fraction === undefined ? '' : '.' + fraction);
}

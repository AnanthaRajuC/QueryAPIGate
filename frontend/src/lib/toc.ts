// "On this page" for a rendered doc: ids on its h2/h3 headings - GitHub's own slugs, so the docs' links to
// #a-section keep working - and the list of them.

export interface TocEntry {
  id: string;
  text: string;
  level: 2 | 3;
}

/** GitHub's heading anchor: lower case, punctuation dropped, spaces to hyphens. */
export function slug(text: string): string {
  return text
    .trim()
    .toLowerCase()
    .replace(/[^\p{L}\p{N}\s_-]/gu, '')
    .replace(/\s/g, '-');
}

/** Anchors for already-sanitized HTML. The ids come from heading text only, never from the markup itself. */
export function withAnchors(html: string): { html: string; toc: TocEntry[] } {
  const root = new DOMParser().parseFromString(`<div>${html}</div>`, 'text/html').body.firstElementChild!;
  const seen = new Map<string, number>();
  const toc: TocEntry[] = [];
  root.querySelectorAll('h1, h2, h3').forEach((h) => {
    const text = (h.textContent ?? '').trim();
    const base = slug(text) || 'section';
    const n = seen.get(base) ?? 0; // a repeated heading gets -1, -2, ... as on GitHub
    seen.set(base, n + 1);
    h.id = n ? `${base}-${n}` : base;
    if (h.tagName !== 'H1' && text) toc.push({ id: h.id, text, level: h.tagName === 'H2' ? 2 : 3 });
  });
  return { html: root.innerHTML, toc };
}

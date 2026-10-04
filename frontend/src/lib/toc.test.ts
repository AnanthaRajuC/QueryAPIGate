import { describe, expect, it } from 'vitest';

import { slug, withAnchors } from './toc';

describe('On this page', () => {
  it("makes GitHub's anchors, so a doc's own #links land", () => {
    expect(slug('Drafts and publishing')).toBe('drafts-and-publishing');
    expect(slug('`GET /api/v1/history`')).toBe('get-apiv1history');
    expect(slug('Rate limiting and CORS')).toBe('rate-limiting-and-cors');
  });

  it('lists h2 and h3 with ids, numbering repeats, and leaves the title out', () => {
    const { html, toc } = withAnchors(
      '<h1>Title</h1><h2>Setup</h2><h3>Notes</h3><h2>Usage</h2><h3>Notes</h3>',
    );
    expect(toc).toEqual([
      { id: 'setup', text: 'Setup', level: 2 },
      { id: 'notes', text: 'Notes', level: 3 },
      { id: 'usage', text: 'Usage', level: 2 },
      { id: 'notes-1', text: 'Notes', level: 3 },
    ]);
    expect(html).toContain('<h2 id="usage">Usage</h2>');
    expect(html).toContain('<h1 id="title">Title</h1>');
  });

  it('takes ids from heading text only, not from markup', () => {
    const { toc } = withAnchors('<h2 id="evil" onclick="x()">Hello <b>there</b></h2>');
    expect(toc).toEqual([{ id: 'hello-there', text: 'Hello there', level: 2 }]);
  });
});

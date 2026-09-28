"""A small admin UI at /ui: connections, saved queries and an ad-hoc SQL runner.

Self-contained (no build step, no external script or stylesheet - unlike /docs, which needs the real
Swagger UI library, this page is simple enough to write by hand). It is a client of the existing JSON API
only; there is no server-side logic here beyond serving this one static page. Query results are always
rendered through DOM APIs (createElement/textContent), never innerHTML, so a value coming back from a
database can never execute as markup.
"""

UI_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>QueryAPIGate</title>
<style>
  :root {
    color-scheme: light dark;
    --bg: light-dark(#f5f5f3, #131416);
    --surface: light-dark(#ffffff, #1a1b1e);
    --surface-2: light-dark(#f0f0ed, #212226);
    --surface-3: light-dark(#e8e8e4, #2a2b30);
    --line: light-dark(#e2e2de, #2b2c31);
    --line-strong: light-dark(#cdcdc8, #3a3b41);
    --ink: light-dark(#1b1c1f, #e7e7e4);
    --ink-2: light-dark(#56585e, #a6a7ac);
    --ink-3: light-dark(#85878d, #74767c);
    --accent: light-dark(oklch(0.52 0.11 170), oklch(0.78 0.11 170));
    --accent-ink: light-dark(#ffffff, #0c1714);
    --accent-soft: light-dark(oklch(0.52 0.11 170 / 0.1), oklch(0.78 0.11 170 / 0.13));
    --danger: light-dark(oklch(0.53 0.17 27), oklch(0.74 0.14 27));
    --danger-soft: light-dark(oklch(0.53 0.17 27 / 0.08), oklch(0.74 0.14 27 / 0.12));
    --warn: light-dark(oklch(0.6 0.13 75), oklch(0.8 0.12 80));
    --syn-kw: light-dark(oklch(0.48 0.14 265), oklch(0.78 0.1 265));
    --syn-str: light-dark(oklch(0.5 0.12 145), oklch(0.8 0.11 145));
    --syn-num: light-dark(oklch(0.55 0.13 55), oklch(0.8 0.11 65));
    --syn-param: light-dark(oklch(0.5 0.15 330), oklch(0.78 0.12 330));
    --syn-cmt: var(--ink-3);
    --shadow: light-dark(0 12px 40px rgb(0 0 0 / 0.12), 0 12px 40px rgb(0 0 0 / 0.5));
    --sans: system-ui, -apple-system, "Segoe UI", Helvetica, Arial, sans-serif;
    --mono: ui-monospace, "SF Mono", SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
  }
  * { box-sizing: border-box; }
  [hidden] { display: none !important; }
  html, body { min-height: 100%; }
  body { margin: 0; background: var(--bg); color: var(--ink); font: 13px/1.45 var(--sans); -webkit-font-smoothing: antialiased;
    display: grid; grid-template-columns: var(--side-w, 232px) minmax(0, 1fr); transition: grid-template-columns 0.18s ease; }
  body.side-collapsed { --side-w: 60px; }
  .content { min-width: 0; }
  a { color: var(--ink-2); text-decoration: none; }
  a:hover { color: var(--accent); }
  code, .mono { font-family: var(--mono); font-size: 12px; }
  h2, h3 { margin: 0; font-weight: 600; letter-spacing: -0.005em; }
  ::selection { background: var(--accent-soft); }

  /* ---- sidebar ---- */
  aside.side { position: sticky; top: 0; height: 100vh; display: flex; flex-direction: column; background: var(--surface);
    border-right: 1px solid var(--line); z-index: 21; min-width: 0; }
  .side-head { height: 52px; flex: none; display: flex; align-items: center; gap: 10px; padding: 0 16px; border-bottom: 1px solid var(--line); }
  .wordmark { font: 700 13px/1 var(--mono); letter-spacing: 0.02em; white-space: nowrap; }
  .wordmark b, .wordmark-short b { color: var(--accent); font-weight: 700; }
  .wordmark-short { display: none; position: relative; font: 700 14px/1 var(--mono); }
  .wordmark-short i { position: absolute; right: -7px; top: -3px; width: 6px; height: 6px; border-radius: 50%; background: var(--accent); }
  .health { margin-left: auto; display: inline-flex; align-items: center; gap: 6px; font: 11px/1 var(--mono); color: var(--ink-3);
    padding: 4px 7px; border: 1px solid var(--line); border-radius: 20px; white-space: nowrap; }
  .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--ink-3); flex: none; display: inline-block; }
  .dot.ok { background: var(--accent); box-shadow: 0 0 0 3px var(--accent-soft); }
  .dot.bad { background: var(--danger); box-shadow: 0 0 0 3px var(--danger-soft); }
  .dot.off { background: transparent; border: 1.5px solid var(--ink-3); }
  #tabs { flex: 1; overflow-y: auto; overflow-x: hidden; padding: 10px 10px 12px; display: flex; flex-direction: column; gap: 10px; scrollbar-width: thin; }
  .nav-group { display: flex; flex-direction: column; gap: 1px; }
  .nav-label { font: 600 10.5px var(--sans); letter-spacing: 0.06em; text-transform: uppercase; color: var(--ink-3); padding: 4px 10px 6px; }
  .nav-rule { display: none; height: 1px; background: var(--line); margin: 4px 6px 6px; }
  #tabs button, .side-foot button.nav { position: relative; display: flex; align-items: center; gap: 8px; width: 100%; height: 32px; border: 0;
    background: none; padding: 0 10px; border-radius: 6px; font: 500 13px var(--sans); color: var(--ink-2); cursor: pointer; white-space: nowrap; text-align: left; }
  #tabs button:hover, .side-foot button.nav:hover { color: var(--ink); background: var(--surface-2); }
  #tabs button.active, .side-foot button.nav.active { color: var(--ink); background: var(--surface-3); }
  #tabs button.active::before, .side-foot button.nav.active::before { content: ""; position: absolute; left: -10px; top: 7px; bottom: 7px; width: 2px; background: var(--accent); border-radius: 0 2px 2px 0; }
  .nav-text { flex: 1; }
  .nav-abbr { display: none; font: 600 11.5px var(--mono); letter-spacing: 0.02em; }
  #tabs .count { margin-left: auto; }
  .side-foot { flex: none; padding: 10px; border-top: 1px solid var(--line); display: flex; flex-direction: column; gap: 1px; }
  .side-link { display: flex; align-items: center; justify-content: space-between; height: 30px; padding: 0 10px; border-radius: 6px; font-size: 12.5px; }
  .side-link:hover { background: var(--surface-2); }
  .side-link span { font: 11px var(--mono); color: var(--ink-3); }
  .side-foot .key-dot-only { display: none; justify-content: center; padding: 10px 0 4px; }
  #key-panel { margin-top: 8px; padding: 10px; border: 1px solid var(--line); border-radius: 8px; background: var(--bg); display: flex; flex-direction: column; gap: 8px; }
  .kp-state { display: flex; align-items: center; gap: 7px; font-size: 11.5px; color: var(--ink-2); }
  .kp-state .scope { margin-left: auto; color: var(--ink-3); }
  .kp-row { display: flex; align-items: center; gap: 6px; }
  .kp-mask { flex: 1; font: 12px var(--mono); color: var(--ink); letter-spacing: 0.08em; min-width: 0; overflow: hidden; text-overflow: ellipsis; }
  #key-change, #save-key { height: 24px; padding: 0 8px; border: 1px solid var(--line-strong); border-radius: 5px; background: var(--surface); color: var(--ink); font: 500 12px var(--sans); cursor: pointer; }
  #key-change:hover, #save-key:hover { background: var(--surface-2); }
  #key-bar { display: flex; align-items: center; gap: 6px; margin: 0; }
  #key-bar input { height: 24px; font: 12px var(--mono); padding: 0 7px; }
  body.side-collapsed .side-head { justify-content: center; padding: 0; }
  body.side-collapsed .wordmark, body.side-collapsed .health, body.side-collapsed .nav-label, body.side-collapsed .nav-text,
  body.side-collapsed #tabs .count, body.side-collapsed .side-link, body.side-collapsed #key-panel { display: none; }
  body.side-collapsed .wordmark-short, body.side-collapsed .nav-rule { display: block; }
  body.side-collapsed .nav-abbr { display: inline; }
  body.side-collapsed .side-foot .key-dot-only { display: flex; }
  body.side-collapsed #tabs button, body.side-collapsed .side-foot button.nav { justify-content: center; padding: 0; }

  /* ---- top bar ---- */
  header.top { position: sticky; top: 0; z-index: 20; display: flex; align-items: center; gap: 16px; height: 52px;
    padding: 0 28px 0 16px; background: var(--bg); border-bottom: 1px solid var(--line); }
  #side-toggle { flex: none; width: 30px; height: 30px; display: flex; align-items: center; justify-content: center; border: 1px solid transparent;
    border-radius: 6px; background: transparent; cursor: pointer; }
  #side-toggle:hover { background: var(--surface); border-color: var(--line); }
  #side-toggle span { position: relative; width: 16px; height: 12px; border: 1.5px solid var(--ink-2); border-radius: 3px; }
  #side-toggle span i { position: absolute; left: 0; top: 0; bottom: 0; width: 5px; background: var(--ink-2); }
  body.side-collapsed #side-toggle span i { width: 2px; }
  .crumbs { display: flex; align-items: center; gap: 8px; font-size: 12.5px; white-space: nowrap; }
  .crumbs .g { color: var(--ink-3); }
  .crumbs .sl { color: var(--line-strong); }
  .crumbs b { color: var(--ink); font-weight: 500; }
  .count { font: 11px/1 var(--mono); color: var(--ink-3); background: var(--surface-2); padding: 3px 5px; border-radius: 4px; min-width: 18px; text-align: center; }
  .count:empty { display: none; }
  .search-wrap { flex: 1; display: flex; justify-content: center; min-width: 0; }
  #global-search { width: 100%; max-width: 440px; height: 32px; display: flex; align-items: center; gap: 8px; padding: 0 8px 0 11px; border: 1px solid var(--line);
    border-radius: 7px; background: var(--surface); color: var(--ink-3); font: 12.5px var(--sans); cursor: text; text-align: left; }
  #global-search:hover { border-color: var(--line-strong); }
  #global-search .ph { flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  #global-search kbd, .kbd { font: 10.5px/1 var(--mono); padding: 3px 5px; border-radius: 4px; border: 1px solid var(--line-strong); color: var(--ink-2); opacity: 1; }
  .chip { font: 11px/1 var(--mono); color: var(--ink-2); padding: 6px 8px; border-radius: 5px; background: var(--surface-2); white-space: nowrap; }
  .chip.low { color: var(--danger); background: var(--danger-soft); }

  /* ---- command palette (Ctrl K) ---- */
  #palette { position: fixed; inset: 0; z-index: 70; background: rgb(0 0 0 / 0.4); display: flex; justify-content: center; align-items: flex-start; padding-top: 12vh; }
  #palette[hidden] { display: none; }
  .pal-box { width: min(560px, calc(100vw - 32px)); max-height: 60vh; display: flex; flex-direction: column; background: var(--surface); border: 1px solid var(--line-strong);
    border-radius: 10px; box-shadow: var(--shadow); overflow: hidden; }
  .pal-box input { height: 44px; border: 0; border-bottom: 1px solid var(--line); border-radius: 0; padding: 0 14px; font-size: 14px; background: transparent; }
  .pal-box input:focus { box-shadow: none; }
  .pal-list { overflow: auto; padding: 6px; }
  .pal-group { font: 600 10.5px var(--sans); letter-spacing: 0.06em; text-transform: uppercase; color: var(--ink-3); padding: 8px 10px 4px; }
  .pal-item { display: flex; align-items: center; gap: 10px; width: 100%; border: 0; background: none; color: var(--ink); padding: 7px 10px; border-radius: 6px; cursor: pointer; text-align: left; font: 12.5px var(--sans); }
  .pal-item.on, .pal-item:hover { background: var(--surface-3); }
  .pal-item .nm { font: 600 12px var(--mono); }
  .pal-item .ds { color: var(--ink-3); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1; min-width: 0; }
  .pal-empty { padding: 22px 12px; text-align: center; color: var(--ink-3); }
  tr.flash td, .qitem.flash { animation: flash 1.6s ease-out; }
  @keyframes flash { from { background: var(--accent-soft); } to { background: transparent; } }

  /* ---- error banner ---- */
  #error-banner { position: sticky; top: 52px; z-index: 19; background: var(--danger-soft); border-bottom: 1px solid var(--danger);
    color: var(--ink); padding: 9px 28px; backdrop-filter: blur(8px); background-color: light-dark(#fbeeed, #2a1a1a); }
  .eb-main { display: flex; align-items: baseline; gap: 10px; }
  .eb-code { font: 600 11px/1 var(--mono); color: var(--accent-ink); background: var(--danger); padding: 3px 6px; border-radius: 4px; }
  .eb-msg { flex: 1; font-weight: 500; overflow-wrap: anywhere; }
  .eb-close { border: 0; background: none; color: var(--ink-2); font-size: 16px; line-height: 1; cursor: pointer; padding: 0 2px; }
  .eb-fields { margin: 6px 0 0; padding: 0 0 0 0; list-style: none; display: flex; flex-direction: column; gap: 2px; font-size: 12.5px; color: var(--ink-2); }
  .eb-fields code { color: var(--danger); }

  /* ---- layout ---- */
  main { padding: 24px 28px 48px; max-width: 1480px; }
  main:has(> #tab-run.active) { max-width: none; }
  main > section { display: none; }
  main > section.active { display: block; }
  .toolbar { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; flex-wrap: wrap; }
  .toolbar h2 { font-size: 15px; }
  .toolbar .sub { color: var(--ink-3); font-size: 12.5px; }
  .spacer { flex: 1; }
  .page-head { display: flex; align-items: flex-end; gap: 12px; margin-bottom: 16px; flex-wrap: wrap; }
  .page-head .titles { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
  .page-head h1 { margin: 0; font-size: 19px; font-weight: 600; letter-spacing: -0.01em; }
  .page-head .sub { color: var(--ink-3); font-size: 12.5px; }
  .page-head .sub code { font: 12px var(--mono); color: var(--ink-2); }
  .page-head > .btn { height: 32px; padding: 0 14px; }
  .page-head .updated { font-size: 11.5px; color: var(--ink-3); }
  .panel-bar { display: flex; align-items: center; gap: 10px; padding: 10px 12px; border-bottom: 1px solid var(--line); flex-wrap: wrap; }
  .panel-foot { display: flex; align-items: center; padding: 9px 14px; color: var(--ink-3); font-size: 12px; }
  .panel-count { font-size: 12px; color: var(--ink-3); }
  .seg { display: inline-flex; padding: 2px; border: 1px solid var(--line); border-radius: 7px; background: var(--bg); gap: 2px; }
  .seg button { display: flex; align-items: center; gap: 6px; height: 26px; padding: 0 10px; border: 0; border-radius: 5px; background: transparent;
    color: var(--ink-2); font: 500 12px var(--sans); cursor: pointer; white-space: nowrap; }
  .seg button.on { background: var(--surface-3); color: var(--ink); }
  .seg .n { font: 11px var(--mono); color: var(--ink-3); }
  .pill { display: inline-flex; align-items: center; gap: 6px; height: 22px; padding: 0 8px; border-radius: 11px; font-size: 12px; font-weight: 500; }
  .pill i { width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
  .pill.ok { background: var(--accent-soft); color: var(--accent); }
  .pill.off { background: var(--surface-2); color: var(--ink-2); }
  .pill.off i { background: transparent; border: 1.5px solid var(--ink-3); width: 4px; height: 4px; }
  .panel { background: var(--surface); border: 1px solid var(--line); border-radius: 8px; }
  .search { height: 30px; width: 240px; padding: 0 10px; background: var(--bg); border-color: var(--line-strong); }
  .toolbar select { width: auto; }
  #connections-table, #apikeys-table, #roles-table, #auditlog-table { overflow-x: auto; }

  /* ---- controls ---- */
  .btn { display: inline-flex; align-items: center; justify-content: center; gap: 6px; height: 30px; padding: 0 12px;
    border: 1px solid var(--line-strong); border-radius: 6px; background: var(--surface); color: var(--ink);
    font: 500 12.5px var(--sans); cursor: pointer; white-space: nowrap; }
  .btn:hover { background: var(--surface-2); }
  .btn:disabled { opacity: 0.45; cursor: default; }
  .btn.primary { background: var(--accent); border-color: var(--accent); color: var(--accent-ink); }
  .btn.primary:hover { filter: brightness(1.06); }
  .btn.danger { color: var(--danger); }
  .btn.danger:hover { background: var(--danger-soft); border-color: var(--danger); }
  .btn.ghost { border-color: transparent; background: transparent; }
  .btn.ghost:hover { background: var(--surface-2); }
  .btn.sm { height: 26px; padding: 0 10px; font-size: 12px; border-radius: 5px; }
  .btn.md { height: 28px; padding: 0 11px; font-size: 12px; }
  .btn.icon { width: 30px; padding: 0; font-size: 17px; }
  kbd { font: 10.5px/1 var(--mono); padding: 2px 4px; border-radius: 3px; border: 1px solid currentColor; opacity: 0.7; }
  input, select, textarea { font: 13px var(--sans); color: var(--ink); background: var(--surface); border: 1px solid var(--line-strong);
    border-radius: 6px; padding: 0 9px; height: 30px; width: 100%; min-width: 0; }
  textarea { height: auto; padding: 8px 9px; font: 12.5px/1.5 var(--mono); resize: vertical; min-height: 84px; }
  input:focus, select:focus, textarea:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-soft); }
  input:disabled { background: var(--surface-2); color: var(--ink-2); }
  select:disabled { background: var(--surface-2); color: var(--ink-3); cursor: not-allowed; }
  input[type=checkbox] { width: 15px; height: 15px; accent-color: var(--accent); margin: 0; }
  input[type=number] { font-family: var(--mono); font-size: 12.5px; }
  input::placeholder, textarea::placeholder { color: var(--ink-3); }
  .field { display: flex; flex-direction: column; gap: 5px; min-width: 0; }
  .field > label { font-size: 12px; font-weight: 500; color: var(--ink-2); display: flex; gap: 6px; align-items: baseline; }
  .field > label .req { color: var(--danger); }
  .field > label .type { font: 11px var(--mono); color: var(--ink-3); font-weight: 400; }
  .hint { font-size: 11.5px; color: var(--ink-3); }
  .grid2 { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 12px; }
  .grid-host { display: grid; grid-template-columns: minmax(0, 1fr) 96px; gap: 12px; }
  .password-field { display: flex; align-items: center; gap: 6px; min-width: 0; }
  .password-field input { min-width: 0; }

  /* ---- tables ---- */
  table.grid { width: 100%; border-collapse: separate; border-spacing: 0; }
  table.grid th { position: sticky; top: 0; z-index: 1; text-align: left; font: 600 11px var(--sans); letter-spacing: 0.03em; text-transform: uppercase;
    color: var(--ink-3); background: var(--surface); padding: 9px 12px; border-bottom: 1px solid var(--line); white-space: nowrap; }
  table.grid td { padding: 11px 12px; border-bottom: 1px solid var(--line); vertical-align: middle; }
  table.grid td.num, table.grid th.num { text-align: right; }
  table.grid tbody tr:last-child td { border-bottom: 0; }
  table.grid tbody tr:hover td { background: var(--surface-2); }
  .actions { display: flex; gap: 4px; justify-content: flex-end; white-space: nowrap; }
  .actions .btn.outlined { border-color: var(--line-strong); background: var(--surface); }
  .actions .btn.outlined:disabled { color: var(--ink-3); }
  .tag { display: inline-block; font: 11px/1.5 var(--mono); padding: 0 6px; border-radius: 4px; background: var(--surface-2); border: 1px solid var(--line); color: var(--ink-2); white-space: nowrap; }
  .tags { display: flex; flex-wrap: wrap; gap: 4px; }
  .name { font: 600 12.5px var(--mono); }
  .dim { color: var(--ink-3); }
  .empty { padding: 44px 20px; text-align: center; display: flex; flex-direction: column; align-items: center; gap: 8px; color: var(--ink-2); }
  .empty strong { color: var(--ink); font-size: 14px; font-weight: 600; }
  .loading { padding: 28px 16px; color: var(--ink-3); display: flex; align-items: center; gap: 10px; justify-content: center; }
  .spin { width: 14px; height: 14px; border-radius: 50%; border: 2px solid var(--line-strong); border-top-color: var(--accent); animation: spin 0.7s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }

  /* ---- saved queries ---- */
  .split { display: grid; grid-template-columns: minmax(260px, 340px) minmax(0, 1fr); gap: 16px; align-items: start; }
  .tag.coll { border-style: dashed; }
  #examples-strip { display: flex; align-items: center; gap: 10px; padding: 8px 12px; margin-bottom: 12px; font-size: 12.5px; color: var(--ink-2); }
  #examples-strip[hidden] { display: none; }
  .detail { min-height: 360px; }
  .run-row { display: flex; gap: 10px; align-items: flex-end; flex-wrap: wrap; }
  .run-row .field { width: 150px; }
  .sub-h { font-size: 11px; font-weight: 600; letter-spacing: 0.03em; text-transform: uppercase; color: var(--ink-3); margin: 0; }
  .history-list { display: flex; flex-direction: column; gap: 2px; margin-top: 6px; max-height: 320px; overflow: auto; }
  .history-item { border: 0; background: none; color: var(--ink-2); font: 11.5px var(--mono); text-align: left; padding: 4px 6px; border-radius: 4px;
    cursor: pointer; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .history-item:hover { background: var(--surface-2); color: var(--ink); }

  /* ---- SQL editor (hand-rolled highlighting: a <pre> painted behind a transparent <textarea>) ---- */
  .editor { position: relative; min-height: 110px; height: 150px; resize: vertical; overflow: hidden; background: var(--surface); }
  .editor-gutter { position: absolute; left: 0; top: 0; bottom: 0; width: 42px; padding: 12px 8px 12px 0; overflow: hidden;
    text-align: right; font: 13px/1.6 var(--mono); color: var(--ink-3); background: var(--surface-2); border-right: 1px solid var(--line);
    user-select: none; pointer-events: none; }
  .editor .hl, .editor textarea { position: absolute; top: 0; right: 0; bottom: 0; left: 42px; margin: 0; padding: 12px 14px; border: 0; border-radius: 0;
    font: 13px/1.6 var(--mono); tab-size: 2; white-space: pre; overflow: auto; letter-spacing: 0; }
  .editor .hl { pointer-events: none; color: var(--ink); overflow: hidden; }
  .editor textarea { color: transparent; caret-color: var(--ink); background: transparent; resize: none; min-height: 0; height: 100%; }
  .editor textarea:focus { box-shadow: none; }
  .editor textarea::selection { background: light-dark(oklch(0.52 0.11 170 / 0.22), oklch(0.78 0.11 170 / 0.25)); color: transparent; }
  .editor.boxed { border: 1px solid var(--line-strong); border-radius: 6px; height: 170px; min-height: 120px; }
  .editor.boxed:focus-within { border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-soft); }
  .code { margin: 0; padding: 12px 14px; font: 12.5px/1.6 var(--mono); white-space: pre-wrap; overflow-wrap: anywhere; background: var(--surface-2);
    border: 1px solid var(--line); border-radius: 6px; max-height: 420px; overflow: auto; }
  .k { color: var(--syn-kw); font-weight: 600; }
  .s { color: var(--syn-str); }
  .n { color: var(--syn-num); }
  .p { color: var(--syn-param); font-weight: 600; }
  .c { color: var(--syn-cmt); font-style: italic; }

  /* ---- run SQL tab ---- */
  .runner { display: grid; grid-template-columns: minmax(0, 1fr) 7px var(--runner-side-w, 280px); overflow: hidden; }
  .runner-main { display: flex; flex-direction: column; min-width: 0; }
  .runner-bar { display: flex; align-items: center; gap: 8px; padding: 8px 10px; border-bottom: 1px solid var(--line); background: var(--surface); flex-wrap: wrap; }
  .runner-bar select { width: auto; height: 28px; font-size: 12.5px; }
  .runner-bar .lbl { font-size: 11.5px; color: var(--ink-3); }
  .runner-splitter { position: relative; cursor: col-resize; background: var(--surface); touch-action: none; }
  .runner-splitter::after { content: ''; position: absolute; left: 50%; top: 0; bottom: 0; width: 1px; background: var(--line); transform: translateX(-50%); }
  .runner-splitter:hover::after, .runner-splitter.dragging::after { width: 3px; background: var(--accent); }
  /* A grip - a short stack of dots - so the splitter reads as draggable rather than as a stray divider line. */
  .runner-splitter::before { content: ''; position: absolute; left: 50%; top: 50%; width: 3px; height: 3px; border-radius: 50%;
    background: var(--ink-3); transform: translate(-50%, -50%);
    box-shadow: 0 -7px 0 var(--ink-3), 0 7px 0 var(--ink-3), 0 -14px 0 var(--ink-3), 0 14px 0 var(--ink-3); }
  .runner-splitter:hover::before, .runner-splitter.dragging::before { background: var(--accent);
    box-shadow: 0 -7px 0 var(--accent), 0 7px 0 var(--accent), 0 -14px 0 var(--accent), 0 14px 0 var(--accent); }
  .runner-side { padding: 12px; display: flex; flex-direction: column; gap: 12px; background: var(--surface); overflow: hidden; }
  .runner-side textarea { min-height: 96px; }
  .runner-side .grid2 input { height: 28px; }
  /* Recent queries, query settings and the schema browser are three tabs rather than one long scroll - the
     sidebar is narrow, and only one of the three needs to be visible at once. */
  .side-tabs { display: flex; gap: 2px; border-bottom: 1px solid var(--line); margin: 0 0 2px; flex: none; }
  .side-tab { flex: 1; min-width: 0; border: 0; background: none; padding: 0 2px 8px; font: 600 11.5px var(--sans); color: var(--ink-3);
    cursor: pointer; border-bottom: 2px solid transparent; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .side-tab:hover { color: var(--ink); }
  .side-tab.active { color: var(--ink); border-bottom-color: var(--accent); }
  .side-tab-panel { display: flex; flex-direction: column; gap: 12px; flex: 1; min-height: 0; }
  .side-tab-panel[hidden] { display: none; }
  .refs { display: flex; gap: 4px; flex-wrap: wrap; align-items: center; min-height: 18px; }

  /* A small "browse a list, drill into one" tab pair - two levels shown one at a time instead of an
     expand/collapse tree. Used by the schema browser (Tables/Columns) and the Saved Queries list
     (Collections/Queries). */
  .minitabs { display: flex; gap: 2px; padding: 4px 4px 0; flex: none; }
  .minitab { border: 0; background: none; padding: 4px 8px 6px; font: 600 11px var(--sans); color: var(--ink-3);
    cursor: pointer; border-bottom: 2px solid transparent; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 50%; }
  .minitab:hover:not(:disabled) { color: var(--ink); }
  .minitab.active { color: var(--ink); border-bottom-color: var(--accent); }
  .minitab:disabled { color: var(--ink-3); opacity: 0.5; cursor: default; }
  /* ---- schema browser (click a table for its columns, a column to insert it into the nearest SQL editor) ---- */
  .schema-browser { border: 1px solid var(--line); border-radius: 6px; background: var(--surface); display: flex; flex-direction: column; }
  /* The Tables/Columns switch stays put (never scrolls out of reach); only the list below it does - see
     .schema-content. */
  /* A fixed cap with its own scrollbar - not unbounded - so a connection with a lot of tables, or a table
     with a lot of columns, scrolls inside its own box instead of growing the whole page. */
  .schema-content { max-height: 220px; overflow: auto; }
  #run-schema-slot .schema-content { max-height: 460px; }
  .schema-browser .hint, .schema-browser .loading { padding: 9px 10px; }
  .schema-row { display: flex; align-items: center; gap: 0; }
  .schema-select { flex: none; width: 20px; height: 26px; padding: 0; border: 0; background: none; color: var(--ink-3); font-size: 11px; cursor: pointer; }
  .schema-select:hover { color: var(--accent); }
  .schema-table { flex: 1; display: flex; align-items: center; gap: 6px; min-width: 0; border: 0; background: none; color: inherit; font: inherit;
    text-align: left; padding: 4px 6px 4px 0; cursor: pointer; }
  .schema-table:hover { background: var(--surface-2); }
  .schema-preview { flex: none; width: 22px; height: 26px; padding: 0; border: 0; background: none; color: var(--ink-3); font-size: 9px; cursor: pointer; }
  .schema-preview:hover { color: var(--accent); }
  .schema-table .name { font: 600 12px var(--mono); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .schema-col { display: flex; justify-content: space-between; gap: 8px; width: 100%; border: 0; background: none; color: inherit; font: 12px var(--mono);
    text-align: left; padding: 6px 10px; cursor: pointer; }
  .schema-col:hover { background: var(--surface-2); }

  /* ---- results ---- */
  .results { margin-top: 14px; }
  .results:empty { display: none; }
  .resbar { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; padding: 7px 10px; border-bottom: 1px solid var(--line); font-size: 12px; color: var(--ink-2); }
  .resbar:empty { display: none; }
  .resbar .stat { font: 11.5px var(--mono); color: var(--ink-2); display: flex; gap: 12px; align-items: center; }
  .resbar .stat b { color: var(--ink); font-weight: 600; }
  .resbar .stat b.stat-ok { color: var(--accent); }
  .pager { display: flex; align-items: center; gap: 4px; }
  .pager input { width: 52px; height: 24px; text-align: center; padding: 0 4px; }
  .pager select { width: auto; height: 24px; font-size: 12px; padding: 0 4px; }
  .res-body:empty { display: none; }
  .table-wrap { max-height: 62vh; overflow: auto; }
  table.rs { border-collapse: separate; border-spacing: 0; font: 12.5px/1.4 var(--mono); min-width: 100%; }
  table.rs th { position: sticky; top: 0; z-index: 1; background: var(--surface-2); text-align: left; font-weight: 600; color: var(--ink);
    padding: 6px 12px; border-bottom: 1px solid var(--line-strong); border-right: 1px solid var(--line); white-space: nowrap; }
  table.rs td { padding: 5px 12px; border-bottom: 1px solid var(--line); border-right: 1px solid var(--line); white-space: nowrap;
    max-width: 380px; overflow: hidden; text-overflow: ellipsis; }
  table.rs tr:hover td { background: var(--accent-soft); }
  table.rs .rn { color: var(--ink-3); text-align: right; background: var(--surface); position: sticky; left: 0; font-size: 11px; padding: 5px 8px; }
  table.rs th.rn { z-index: 2; background: var(--surface-2); }
  table.rs .num { text-align: right; font-variant-numeric: tabular-nums; }
  table.rs .null { color: var(--ink-3); font-style: italic; font-size: 11px; }
  .res-pre { margin: 0; padding: 12px 14px; font: 12.5px/1.55 var(--mono); white-space: pre-wrap; overflow-wrap: anywhere; max-height: 62vh; overflow: auto; }
  .res-note { padding: 22px 16px; color: var(--ink-2); display: flex; align-items: center; gap: 10px; justify-content: center; }
  .res-note.err { color: var(--danger); }

  /* ---- response headers panel ---- */
  .headers-panel { margin: 0 14px 12px; overflow: auto; max-height: 200px; }
  .headers-panel td { font-size: 11.5px; padding: 5px 10px; }
  .headers-panel td:first-child { color: var(--ink-3); white-space: nowrap; }
  .headers-panel td:last-child { overflow-wrap: anywhere; }

  /* ---- quick chart (current page only) ---- */
  .chart-panel { margin: 0 14px 12px; padding: 10px 12px; }
  .chart-toolbar { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 8px; }
  .chart-toolbar select { font: 11.5px var(--mono); background: var(--surface); color: var(--ink); border: 1px solid var(--line); border-radius: 6px; padding: 3px 6px; }
  .chart-svg-slot svg { width: 100%; height: auto; display: block; }
  .chart-bar { fill: var(--accent); }
  .chart-bar:hover { opacity: 0.75; }
  .chart-axis { stroke: var(--line); stroke-width: 1; }
  .chart-label { font: 10px var(--mono); fill: var(--ink-3); }
  .chart-label-x { text-anchor: middle; }
  .chart-label-y { text-anchor: start; }

  /* ---- metrics tab ---- */

  /* ---- collapsible JSON tree (non-tabular responses) ---- */
  .jt-root { padding: 12px 14px; max-height: 62vh; overflow: auto; }
  .jt-head { display: inline-flex; align-items: center; gap: 2px; }
  .jt-toggle { border: 0; background: none; color: var(--ink-3); font-size: 9px; cursor: pointer; padding: 0 3px; width: 16px; flex: none; }
  .jt-toggle:hover { color: var(--ink); }
  .jt-children { padding-left: 18px; border-left: 1px solid var(--line); margin-left: 6px; }
  .jt-item { display: flex; gap: 4px; align-items: flex-start; }
  .jt-key { color: var(--syn-param); flex: none; }
  .jt-punct { color: var(--ink-3); }
  .jt-str { color: var(--syn-str); overflow-wrap: anywhere; }
  .jt-num { color: var(--syn-num); }
  .jt-null { color: var(--ink-3); font-style: italic; }

  /* ---- drawer ---- */
  .backdrop { position: fixed; inset: 0; background: rgb(0 0 0 / 0.28); z-index: 40; }
  .drawer { position: fixed; top: 0; right: 0; bottom: 0; width: min(500px, 100%); z-index: 41; background: var(--surface); border-left: 1px solid var(--line);
    box-shadow: var(--shadow); display: flex; flex-direction: column; transform: translateX(102%); transition: transform 0.18s ease; visibility: hidden; }
  .drawer.open { transform: none; visibility: visible; }
  .drawer-head { display: flex; align-items: flex-start; gap: 10px; padding: 16px 18px 14px; border-bottom: 1px solid var(--line); }
  .drawer-head h3 { font-size: 15px; }
  .kicker { font: 11px var(--mono); color: var(--ink-3); margin-bottom: 3px; }
  .kicker:empty { display: none; }
  .drawer-body { flex: 1; overflow: auto; }
  .form { display: flex; flex-direction: column; gap: 14px; padding: 18px; min-height: 100%; }
  .form-actions { position: sticky; bottom: 0; margin: auto -18px -18px; padding: 12px 18px; background: var(--surface); border-top: 1px solid var(--line); display: flex; gap: 8px; justify-content: flex-end; }
  .switch { display: flex; align-items: center; gap: 8px; font-weight: 500; font-size: 12.5px; cursor: pointer; }
  .test-row { display: flex; align-items: center; gap: 10px; }
  .test-result { font-size: 12.5px; overflow-wrap: anywhere; }
  .test-ok { color: var(--accent); }
  .test-fail { color: var(--danger); }

  /* ---- toast ---- */
  #toasts { position: fixed; right: 20px; bottom: 20px; z-index: 60; display: flex; flex-direction: column; gap: 8px; align-items: flex-end; }
  .toast { background: var(--ink); color: var(--surface); padding: 8px 12px; border-radius: 6px; font-size: 12.5px; box-shadow: var(--shadow); transition: opacity 0.3s; }

  footer { padding: 0 28px 24px; color: var(--ink-3); font-size: 11.5px; max-width: 1480px; }

  @media (max-width: 1100px) {
    header.top { padding-right: 16px; gap: 10px; }
    #error-banner { padding: 9px 16px; }
  }
  @media (max-width: 980px) {
    body { --side-w: 60px; }
    .side-head { justify-content: center; padding: 0; }
    .wordmark, .health, .nav-label, .nav-text, #tabs .count, .side-link, #key-panel { display: none; }
    .wordmark-short, .nav-rule { display: block; }
    .nav-abbr { display: inline; }
    .side-foot .key-dot-only { display: flex; }
    #tabs button, .side-foot button.nav { justify-content: center; padding: 0; }
    #side-toggle { display: none; }
    .search-wrap { display: none; }
    main { padding: 16px; }
    .split, .runner { grid-template-columns: minmax(0, 1fr); }
    #queries-panel { position: static; max-height: 320px; }
    .runner-main { border-right: 0; border-bottom: 1px solid var(--line); }
    .runner-splitter { display: none; }
    .hide-sm { display: none; }
  }

  /* ---- settings ---- */

  /* ---- saved queries: list panel, collections, detail ---- */
  .split { display: grid; grid-template-columns: minmax(260px, 320px) minmax(0, 1fr); gap: 16px; align-items: start; }
  #queries-panel { position: sticky; top: 76px; max-height: calc(100vh - 110px); display: flex; flex-direction: column; overflow: hidden; }
  .qsearch { padding: 10px; border-bottom: 1px solid var(--line); }
  .qsearch .search { width: 100%; }
  #queries-subtabs:not(:empty) { border-bottom: 1px solid var(--line); }
  #queries-table { overflow: auto; }
  .qcoll-row { display: flex; align-items: center; gap: 8px; width: 100%; padding: 9px 12px; border: 0; border-bottom: 1px solid var(--line);
    background: none; color: var(--ink-2); cursor: pointer; text-align: left; }
  .qcoll-row:hover { color: var(--ink); background: var(--surface-2); }
  .qcoll-row .qg-name { flex: 1; font: 600 11.5px var(--sans); letter-spacing: 0.04em; text-transform: uppercase; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .qcoll-row .qg-n { font: 11px var(--mono); color: var(--ink-3); }
  .qitem { display: flex; flex-direction: column; gap: 3px; width: 100%; padding: 9px 14px 9px 30px; border: 0; border-top: 1px solid var(--line);
    border-left: 2px solid transparent; background: none; color: inherit; font: inherit; cursor: pointer; text-align: left; }
  #queries-table > .qitem:first-child, .qg-foot + .qitem { border-top: 0; }
  .qitem:hover { background: var(--surface-2); }
  .qitem.sel { background: var(--accent-soft); border-left-color: var(--accent); }
  #queries-table > .qitem { padding-left: 14px; }
  .qi-top { display: flex; align-items: center; gap: 7px; min-width: 0; }
  .qi-top .name { font: 600 12px var(--mono); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .tag.v { font-size: 10.5px; padding: 0 5px; background: var(--bg); }
  .qi-desc { color: var(--ink-2); font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .qg-foot { display: flex; align-items: center; gap: 12px; padding: 8px 14px 7px; border-bottom: 1px solid var(--line); font-size: 11.5px; color: var(--ink-3); }
  .qg-foot a { font-size: 11.5px; }
  .tag.example { border-color: var(--accent); color: var(--accent); background: transparent; }
  #examples-strip { display: flex; align-items: center; gap: 12px; padding: 10px 14px; margin-bottom: 16px; font-size: 12.5px; color: var(--ink-2); }
  #examples-strip[hidden] { display: none; }
  .impact { display: flex; flex-direction: column; gap: 3px; padding: 8px 10px; border: 1px solid var(--line); border-radius: 6px; background: var(--surface-2); }
  .detail { min-height: 200px; min-width: 0; }
  .d-head { padding: 18px 20px 0; display: flex; flex-direction: column; gap: 12px; }
  .d-title { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  .d-title h3 { font: 600 16px var(--mono); }
  .d-desc { color: var(--ink-2); margin: 0; text-wrap: pretty; }
  .vbadge { font: 500 11.5px/22px var(--mono); padding: 0 8px; border-radius: 5px; background: var(--ink); color: var(--surface); }
  .versions { display: inline-flex; border-radius: 5px; overflow: hidden; border: 1px solid var(--line-strong); }
  .versions button { border: 0; border-right: 1px solid var(--line-strong); background: var(--surface); color: var(--ink-2); font: 500 11.5px var(--mono); padding: 0 8px; height: 22px; cursor: pointer; }
  .versions button:last-child { border-right: 0; }
  .versions button:hover { background: var(--surface-2); }
  .versions button.on { background: var(--ink); color: var(--surface); }
  .meta { margin: 0; display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 1px; background: var(--line); border: 1px solid var(--line); border-radius: 6px; overflow: hidden; }
  .meta div { background: var(--bg); padding: 8px 10px; display: flex; flex-direction: column; gap: 2px; min-width: 0; }
  .meta dt { font-size: 11px; color: var(--ink-3); }
  .meta dd { margin: 0; font: 12px var(--mono); color: var(--ink); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .subtabs { display: flex; gap: 2px; border-bottom: 1px solid var(--line); margin: 0 -20px; padding: 0 14px; }
  .subtabs button { border: 0; background: none; font: 500 12.5px var(--sans); color: var(--ink-2); padding: 10px 8px; cursor: pointer; position: relative; display: flex; gap: 6px; align-items: center; }
  .subtabs button:hover { color: var(--ink); }
  .subtabs button.on { color: var(--ink); }
  .subtabs button.on::after { content: ""; position: absolute; left: 6px; right: 6px; bottom: -1px; height: 2px; background: var(--ink); }
  .d-body { padding: 18px 20px 20px; display: flex; flex-direction: column; gap: 16px; }
  .param-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 14px; }
  .param-grid .field > label { font: 500 12px var(--mono); color: var(--ink); }
  .param-grid input { font-family: var(--mono); font-size: 12.5px; background: var(--bg); border-color: var(--line-strong); }
  .get-row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; padding: 10px; border: 1px solid var(--line); border-radius: 8px; background: var(--bg); }
  .get-row .method { font: 600 11px var(--mono); padding: 3px 6px; border-radius: 4px; background: var(--accent-soft); color: var(--accent); }
  .get-row .endpoint { flex: 1; min-width: 160px; font: 12.5px var(--mono); color: var(--ink); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .get-row select { width: auto; height: 28px; padding: 0 6px; font-size: 12px; background: var(--surface); }
  .codebox { border: 1px solid var(--line); border-radius: 8px; background: var(--bg); padding: 12px 0; font: 12.5px/1.65 var(--mono); overflow: auto; }
  .ln-r { display: flex; white-space: pre; }
  .ln-n { flex: none; width: 44px; padding-right: 14px; text-align: right; color: var(--line-strong); user-select: none; }
  .curlbox { margin: 0; padding: 14px 16px; font: 12.5px/1.65 var(--mono); white-space: pre-wrap; overflow-wrap: anywhere; background: var(--bg); border: 1px solid var(--line); border-radius: 8px; color: var(--ink); }
  .menu { position: fixed; z-index: 60; min-width: 190px; padding: 4px; background: var(--surface); border: 1px solid var(--line-strong); border-radius: 8px; box-shadow: var(--shadow); display: flex; flex-direction: column; }
  .menu button { border: 0; background: none; text-align: left; padding: 7px 10px; border-radius: 5px; font: 500 12.5px var(--sans); color: var(--danger); cursor: pointer; }
  .menu button:hover { background: var(--danger-soft); }
  /* ---- double-click a column/value in the SQL editor to turn it into a bound parameter ---- */
  .paramize-popover { position: fixed; z-index: 65; width: 260px; padding: 10px; background: var(--surface);
    border: 1px solid var(--line-strong); border-radius: 8px; box-shadow: var(--shadow); }
  .paramize-popover .preview { font: 12px var(--mono); color: var(--ink-2); margin-bottom: 8px; overflow-wrap: anywhere; }
  .paramize-popover .preview b { color: var(--ink); }
  .paramize-popover button { width: 100%; border: 1px solid var(--accent); background: var(--accent); color: var(--accent-ink);
    text-align: center; padding: 6px 10px; border-radius: 5px; font: 600 12.5px var(--sans); cursor: pointer; }
  .paramize-popover button:hover { filter: brightness(1.06); }
  .tags.scope .tag { border-style: dashed; }
  .panel-bar select { width: auto; height: 30px; padding: 0 8px; background: var(--bg); border-color: var(--line-strong); font-size: 12.5px; }
  .results:has(.resbar:empty):has(.res-body:empty) { display: none; }
  .tag.act { border: 0; padding: 1px 6px; background: var(--surface-2); color: var(--ink-2); }
  .tag.act.ok { background: var(--accent-soft); color: var(--accent); }
  .tag.act.bad { background: var(--danger-soft); color: var(--danger); }
  .access-box { display: flex; flex-direction: column; gap: 6px; padding: 10px 14px; border: 1px solid var(--line); border-radius: 8px; background: var(--surface-2); }
  .access-summary { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; font-size: 12.5px; }
  .access-summary b { font: 600 12.5px var(--mono); }
  .access-names { display: flex; align-items: center; gap: 4px; flex-wrap: wrap; margin-left: 4px; }
  .access-names .name { font: 600 12px var(--mono); }
  .access-roles { margin: 0; }
  #accessmap-body { overflow: auto; }
  table.amap { border-collapse: separate; border-spacing: 0; font-size: 12.5px; margin-bottom: 4px; }
  table.amap th, table.amap td { padding: 7px 10px; border-bottom: 1px solid var(--line); border-right: 1px solid var(--line); white-space: nowrap; }
  table.amap thead th { position: sticky; top: 0; z-index: 2; background: var(--surface-2); font: 600 11px var(--sans); letter-spacing: 0.03em; text-transform: uppercase; color: var(--ink-3); }
  table.amap .amap-query { position: sticky; left: 0; z-index: 1; background: var(--surface); text-align: left; font: 600 12px var(--mono); max-width: 220px; overflow: hidden; text-overflow: ellipsis; }
  table.amap thead th.amap-query { z-index: 3; }
  table.amap tfoot .amap-query { position: static; background: var(--surface-2); font: 600 11px var(--sans); text-transform: uppercase; letter-spacing: 0.03em; color: var(--ink-3); }
  table.amap .amap-coll { display: block; font: 11px var(--sans); color: var(--ink-3); font-weight: 400; overflow: hidden; text-overflow: ellipsis; }
  table.amap td.amap-cell { text-align: center; }
  table.amap td.num, table.amap th.num { text-align: right; font: 12px var(--mono); color: var(--ink-2); }
  table.amap tfoot td { border-bottom: 0; background: var(--surface-2); }
  table.amap .amap-key-head { text-align: center; vertical-align: top; }
  table.amap .amap-key-name { display: block; font: 600 12px var(--mono) !important; letter-spacing: 0 !important; text-transform: none !important; color: var(--ink) !important; }
  table.amap .amap-key-sub { display: block; margin-top: 2px; font: 10.5px var(--sans) !important; text-transform: none !important; letter-spacing: 0 !important; color: var(--ink-3) !important; font-weight: 400 !important; white-space: normal; }
  table.amap .amap-key-head.revoked .amap-key-name { color: var(--ink-3) !important; text-decoration: line-through; }
  .amap-dot { display: inline-flex; align-items: center; justify-content: center; min-width: 18px; height: 18px; padding: 0 3px; border-radius: 9px;
    background: var(--accent-soft); color: var(--accent); font: 600 10px var(--mono); }
  .amap-dot.conn { background: color-mix(in oklab, var(--warn) 18%, transparent); color: var(--warn); }
  .amap-dot.role { background: var(--surface-3); color: var(--ink-2); }
  .amap-dot.role.conn { background: color-mix(in oklab, var(--warn) 12%, var(--surface-3)); color: var(--warn); }
  table.amap th.amap-group { background: var(--bg); border-bottom: 1px solid var(--line); text-align: center; font: 600 10.5px var(--sans); letter-spacing: 0.06em; color: var(--ink-3); }
  table.amap th.amap-group.role { color: var(--ink-2); }
  table.amap th.amap-sortable { cursor: pointer; user-select: none; }
  table.amap th.amap-sortable:hover { color: var(--ink); }
  table.amap th.amap-key-head.amap-sortable:hover .amap-key-name { color: var(--accent); }
  .legend .amap-dot { min-width: 16px; height: 16px; font-size: 9px; vertical-align: middle; }
  .amap-q-row { display: flex; align-items: center; gap: 6px; }
  .amap-q-row span { overflow: hidden; text-overflow: ellipsis; }
  .amap-info { flex: none; width: 15px; height: 15px; border-radius: 50%; border: 1px solid var(--ink-3); color: var(--ink-3);
    font: 600 10px/1 var(--mono); background: none; cursor: pointer; display: inline-flex; align-items: center; justify-content: center; padding: 0; }
  .amap-info:hover { border-color: var(--accent); color: var(--accent); }
  .qi-versions td.mono { font-size: 11.5px; }
  .legend { display: inline-flex; align-items: center; gap: 6px; margin-left: 10px; font-size: 11.5px; }
  .legend .amap-dot { margin-left: 10px; }

  /* ---- metrics ---- */
  .stat-tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 1px; background: var(--line); border: 1px solid var(--line); border-radius: 8px; overflow: hidden; margin-bottom: 16px; }
  .stat-tile { background: var(--surface); padding: 14px 16px; display: flex; flex-direction: column; gap: 6px; }
  .stat-tile .label { font-size: 11px; color: var(--ink-3); text-transform: uppercase; letter-spacing: 0.04em; }
  .stat-tile .value { font: 600 22px var(--mono); color: var(--ink); }
  .stat-tile .value.warn { color: var(--danger); }
  .metrics-charts { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 16px; margin-bottom: 16px; }
  .chart-card { background: var(--surface); border: 1px solid var(--line); border-radius: 8px; padding: 14px 16px; display: flex; flex-direction: column; gap: 14px; }
  .chart-card h3 { font-size: 13px; font-weight: 600; }
  .bar-row { display: grid; grid-template-columns: 80px minmax(0, 1fr) 48px; align-items: center; gap: 10px; }
  .bar-row .bl { font: 11.5px var(--mono); color: var(--ink-2); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .bar-track { height: 10px; border-radius: 3px; background: var(--surface-2); overflow: hidden; }
  .bar-fill { height: 100%; border-radius: 3px; }
  .bar-row .bv { font: 12px var(--mono); text-align: right; }

  /* ---- settings ---- */
  .settings { display: flex; flex-wrap: wrap; gap: 20px; align-items: flex-start; }
  #settings-nav { flex: 1 1 160px; max-width: 200px; position: sticky; top: 76px; display: flex; flex-direction: column; gap: 1px; }
  #settings-nav button { display: flex; align-items: center; height: 32px; padding: 0 10px; border: 0; border-radius: 6px; background: none; color: var(--ink-2); font: 500 13px var(--sans); cursor: pointer; text-align: left; }
  #settings-nav button:hover { color: var(--ink); }
  #settings-nav button.on { background: var(--surface-3); color: var(--ink); }
  #settings-nav .n { font: 10.5px var(--mono); color: var(--ink-3); }
  #settings-body { flex: 999 1 480px; display: flex; flex-direction: column; gap: 14px; min-width: 0; }
  .set-note { display: flex; align-items: flex-start; gap: 10px; padding: 10px 14px; border: 1px solid var(--line); border-radius: 8px; background: var(--surface); font-size: 12.5px; color: var(--ink-2); }
  .set-note i { flex: none; margin-top: 5px; width: 7px; height: 7px; border-radius: 50%; background: var(--warn); }
  .set-note code { font: 12px var(--mono); color: var(--ink); }
  .set-head { padding: 14px 18px; border-bottom: 1px solid var(--line); display: flex; flex-direction: column; gap: 3px; }
  .set-head h2 { font-size: 14px; font-weight: 600; }
  .set-head span { font-size: 12.5px; color: var(--ink-3); }
  .set-row { display: flex; flex-wrap: wrap; gap: 10px 20px; padding: 14px 18px; border-bottom: 1px solid var(--line); align-items: center; }
  .set-row:last-child { border-bottom: 0; }
  .set-what { flex: 1 1 280px; display: flex; flex-direction: column; gap: 4px; min-width: 0; }
  .set-label { font-weight: 500; color: var(--ink); }
  .set-desc { font-size: 12.5px; color: var(--ink-2); text-wrap: pretty; }
  .set-env { font: 11px var(--mono); color: var(--ink-3); }
  .set-val { flex: 0 1 auto; display: flex; align-items: center; justify-content: flex-end; gap: 8px; min-width: 0; max-width: 100%; }
  .set-val .v { font: 12.5px var(--mono); padding: 5px 9px; border-radius: 5px; background: var(--bg); border: 1px solid var(--line); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 100%; }
  .badge { flex: none; font: 10.5px/1.6 var(--mono); padding: 0 6px; border-radius: 4px; border: 1px solid var(--line-strong); color: var(--ink-3); }
  .badge.env { border-color: var(--accent); color: var(--accent); }
  .set-row.pref { justify-content: space-between; }
  .set-row.pref .set-what { flex: 0 1 auto; }
  body.compact table.grid td { padding: 5px 14px; }
  body.compact table.rs td { padding: 2px 12px; }
  @media (max-width: 980px) { .split, .runner { grid-template-columns: minmax(0, 1fr); } #queries-panel { position: static; max-height: 320px; } .runner-splitter { display: none; } }
</style></head>
<body>
<aside class="side" id="side">
  <div class="side-head">
    <span class="wordmark">Query<b>API</b>Gate</span>
    <span class="wordmark-short" title="QueryAPIGate">Q<b>A</b><i></i></span>
    <span class="health" id="health" title="GET /health"><span class="dot" id="health-dot"></span><span id="version">…</span></span>
  </div>
  <nav id="tabs" role="tablist" aria-label="Sections">
    <div class="nav-group"><div class="nav-label">Data</div><div class="nav-rule"></div>
      <button type="button" role="tab" data-tab="connections" data-group="Data" data-label="Connections" title="Connections" class="active"><span class="nav-abbr">Cn</span><span class="nav-text">Connections</span><span class="count" id="count-connections"></span></button>
      <button type="button" role="tab" data-tab="queries" data-group="Data" data-label="Saved queries" title="Saved queries"><span class="nav-abbr">Sq</span><span class="nav-text">Saved queries</span><span class="count" id="count-queries"></span></button>
      <button type="button" role="tab" data-tab="run" data-group="Data" data-label="Run SQL" title="Run SQL"><span class="nav-abbr">Rn</span><span class="nav-text">Run SQL</span></button>
    </div>
    <div class="nav-group"><div class="nav-label">Access</div><div class="nav-rule"></div>
      <button type="button" role="tab" data-tab="apikeys" data-group="Access" data-label="API keys" title="API keys"><span class="nav-abbr">Ky</span><span class="nav-text">API keys</span><span class="count" id="count-apikeys"></span></button>
      <button type="button" role="tab" data-tab="roles" data-group="Access" data-label="Roles" title="Roles"><span class="nav-abbr">Ro</span><span class="nav-text">Roles</span><span class="count" id="count-roles"></span></button>
      <button type="button" role="tab" data-tab="accessmap" data-group="Access" data-label="Access map" title="Access map"><span class="nav-abbr">Am</span><span class="nav-text">Access map</span></button>
    </div>
    <div class="nav-group"><div class="nav-label">Observability</div><div class="nav-rule"></div>
      <button type="button" role="tab" data-tab="metrics" data-group="Observability" data-label="Metrics" title="Metrics"><span class="nav-abbr">Mt</span><span class="nav-text">Metrics</span></button>
      <button type="button" role="tab" data-tab="auditlog" data-group="Observability" data-label="Audit log" title="Audit log"><span class="nav-abbr">Au</span><span class="nav-text">Audit log</span></button>
    </div>
  </nav>
  <div class="side-foot">
    <button type="button" role="tab" class="nav" data-tab="settings" data-group="System" data-label="Settings" id="nav-settings" title="Settings"><span class="nav-abbr">St</span><span class="nav-text">Settings</span></button>
    <div class="key-dot-only" title="API key applied to this tab"><span class="dot off" id="key-dot-narrow"></span></div>
    <a class="side-link" href="docs">API docs<span>/docs</span></a>
    <a class="side-link" href="openapi.json">OpenAPI<span>.json</span></a>
    <div id="key-panel">
      <div class="kp-state"><span class="dot off" id="key-dot"></span><span id="key-state">No API key</span><span class="scope">this tab</span></div>
      <div class="kp-row" id="key-view" hidden><span class="kp-mask" id="key-mask"></span><button type="button" id="key-change">Change</button></div>
      <form id="key-bar" autocomplete="off">
        <input id="key" type="password" autocomplete="off" placeholder="X-API-Key" aria-label="API key">
        <button id="save-key" type="submit">Apply</button>
      </form>
    </div>
  </div>
</aside>
<div class="content">
<header class="top">
  <button type="button" id="side-toggle" title="Collapse sidebar (Ctrl B)" aria-label="Collapse sidebar" aria-expanded="true"><span><i></i></span></button>
  <div class="crumbs" id="crumbs"><span class="g" id="crumb-group">Data</span><span class="sl">/</span><b id="crumb-page">Connections</b></div>
  <div class="search-wrap"><button type="button" id="global-search" aria-label="Search queries, connections, keys"><span class="ph">Search queries, connections, keys…</span><kbd>Ctrl K</kbd></button></div>
  <span class="chip" id="rate" hidden title="X-RateLimit-Remaining / X-RateLimit-Limit"></span>
</header>
<div id="error-banner" role="alert" hidden></div>

<main>
  <section id="tab-connections" class="active">
    <div class="page-head">
      <div class="titles"><h1>Connections</h1><span class="sub">Databases this gateway can run saved queries against.</span></div>
      <span class="spacer"></span>
      <button id="new-connection" type="button" class="btn primary">New connection</button>
    </div>
    <div class="panel" style="overflow:hidden">
      <div class="panel-bar">
        <div class="seg" id="conn-tabs" role="group" aria-label="Connection status"></div>
        <span class="spacer"></span>
        <input id="conn-filter" class="search" type="search" placeholder="Filter by name, type, host…">
      </div>
      <div id="connections-table"><div class="loading"><span class="spin"></span>Loading connections…</div></div>
      <div class="panel-foot" id="connections-foot"></div>
    </div>
  </section>

  <section id="tab-queries">
    <div id="examples-strip" class="panel" hidden></div>
    <div class="page-head">
      <div class="titles"><h1>Saved queries</h1><span class="sub" id="queries-sub">Each one is published at <code>/q/&lt;name&gt;</code>.</span></div>
      <span class="spacer"></span>
      <button id="new-collection" type="button" class="btn">New collection</button>
      <button id="new-query" type="button" class="btn primary">New saved query</button>
    </div>
    <div class="split">
      <div id="queries-panel" class="panel">
        <div class="qsearch"><input id="query-filter" class="search" type="search" placeholder="Filter by name, description, tag…"></div>
        <div id="queries-subtabs"></div>
        <div id="queries-table"><div class="loading"><span class="spin"></span>Loading…</div></div>
      </div>
      <div id="query-detail" class="panel detail"><div class="empty"><strong>No query selected</strong><span>Pick a saved query to run it, read its SQL or see its history.</span></div></div>
    </div>
  </section>

  <section id="tab-apikeys">
    <div class="page-head">
      <div class="titles"><h1>API keys</h1><span class="sub" id="apikeys-sub">Scope each one to connections, collections or roles.</span></div>
      <span class="spacer"></span>
      <button id="new-apikey" type="button" class="btn primary">New API key</button>
    </div>
    <div id="apikeys-table" class="panel"><div class="loading"><span class="spin"></span>Loading API keys…</div></div>
  </section>

  <section id="tab-roles">
    <div class="page-head">
      <div class="titles"><h1>Roles</h1><span class="sub" id="roles-sub">Reusable permission sets that API keys can inherit.</span></div>
      <span class="spacer"></span>
      <button id="new-role" type="button" class="btn primary">New role</button>
    </div>
    <div id="roles-table" class="panel"><div class="loading"><span class="spin"></span>Loading roles…</div></div>
  </section>

  <section id="tab-auditlog">
    <div class="page-head">
      <div class="titles"><h1>Audit log</h1><span class="sub" id="auditlog-sub">Administrative actions, newest first.</span></div>
      <span class="spacer"></span>
      <button id="refresh-auditlog" type="button" class="btn">Refresh</button>
      <button id="export-auditlog" type="button" class="btn">Export</button>
    </div>
    <div class="panel" style="overflow:hidden">
      <div class="panel-bar">
        <select id="auditlog-action-filter"><option value="">All actions</option></select>
        <input id="auditlog-filter" class="search" style="width:260px" type="search" placeholder="Filter by actor, target, time…">
        <span class="spacer"></span><span class="panel-count" id="auditlog-count"></span>
      </div>
      <div id="auditlog-table"><div class="loading"><span class="spin"></span>Loading audit log…</div></div>
    </div>
  </section>

  <section id="tab-metrics">
    <div class="page-head">
      <div class="titles"><h1>Metrics</h1><span class="sub">Live totals since this process started. For trends, scrape <code>/metrics</code> with Prometheus and import the bundled Grafana dashboard.</span></div>
      <span class="spacer"></span>
      <span class="updated" id="metrics-updated"></span>
      <button id="refresh-metrics" type="button" class="btn">Refresh</button>
    </div>
    <div id="metrics-body"><div class="loading"><span class="spin"></span>Loading metrics…</div></div>
  </section>

  <section id="tab-accessmap">
    <div class="page-head">
      <div class="titles"><h1>Access map</h1><span class="sub">Which API keys can reach which saved queries - the whole point of collections and connection grants, in one place.
        <span class="legend"><span class="amap-dot">Q</span> named query &nbsp;<span class="amap-dot">C</span> collection &nbsp;<span class="amap-dot conn">W</span> whole connection</span></span></div>
      <span class="spacer"></span>
      <select id="accessmap-conn-filter"><option value="">All connections</option></select>
      <select id="accessmap-table-filter" disabled><option value="">Select a connection first</option></select>
      <select id="accessmap-db-filter"><option value="">All databases</option></select>
      <select id="accessmap-reach-filter">
        <option value="">Any reach</option>
        <option value="reachable">Reachable by a key</option>
        <option value="unreachable">Reachable by no key</option>
      </select>
      <input id="accessmap-filter" class="search" type="search" placeholder="Filter by query or key…">
    </div>
    <div id="accessmap-body" class="panel"><div class="loading"><span class="spin"></span>Loading…</div></div>
  </section>

  <section id="tab-run">
    <div class="page-head">
      <div class="titles"><h1>Run SQL</h1><span class="sub">Ad-hoc statements against any active connection. Nothing here is saved.</span></div>
    </div>
    <form id="run-form" class="panel runner" novalidate>
      <div class="runner-main">
        <div class="runner-bar">
          <span class="lbl">Type</span>
          <select id="run-conn-type" aria-label="Database type"></select>
          <span class="lbl">Host</span>
          <select id="run-conn-host" aria-label="Host"></select>
          <span class="lbl">Connection</span>
          <select id="run-connection" required aria-label="Connection"></select>
          <span class="lbl">Database</span>
          <select id="run-database" aria-label="Database" disabled></select>
          <span class="lbl">Format</span>
          <select id="run-format" aria-label="Format">
            <option value="json">json</option><option value="ndjson">ndjson</option>
            <option value="csv">csv</option><option value="tsv">tsv</option>
            <option value="xml">xml</option><option value="yaml">yaml</option><option value="xlsx">xlsx</option>
          </select>
          <span class="spacer"></span>
          <span class="hint hide-sm"><kbd>Ctrl</kbd> <kbd>Enter</kbd></span>
          <button type="button" class="btn ghost md" id="run-save-as-api-button" title="Save this query as a new saved query">Save as New API</button>
          <button type="button" class="btn ghost md" id="run-explain-button" title="Run EXPLAIN on this query">Explain</button>
          <button type="submit" class="btn primary md" id="run-button" style="padding:0 16px">Run</button>
        </div>
        <div class="editor" id="run-editor">
          <div class="editor-gutter" id="run-sql-gutter" aria-hidden="true"></div>
          <pre class="hl" id="run-sql-hl" aria-hidden="true"></pre>
          <textarea id="run-sql" required spellcheck="false" wrap="off" autocomplete="off" aria-label="SQL" placeholder="SELECT * FROM t WHERE id = :id"></textarea>
        </div>
      </div>
      <div class="runner-splitter" id="run-splitter" role="separator" aria-orientation="vertical" aria-label="Resize sidebar" tabindex="0"></div>
      <div class="runner-side">
        <div class="side-tabs" role="tablist">
          <button type="button" class="side-tab" data-side-tab="recent" role="tab" aria-selected="false">Recent</button>
          <button type="button" class="side-tab" data-side-tab="settings" role="tab" aria-selected="false">Settings</button>
          <button type="button" class="side-tab" data-side-tab="schema" role="tab" aria-selected="false">Schema</button>
        </div>
        <div class="side-tab-panel" data-side-panel="recent" hidden>
          <div id="run-history-slot"></div>
        </div>
        <div class="side-tab-panel" data-side-panel="settings" hidden>
          <div class="field">
            <label for="run-params">Bound parameters <span class="type">JSON</span></label>
            <textarea id="run-params" spellcheck="false" placeholder='{"id": 1}'></textarea>
            <div class="refs" id="run-refs"></div>
          </div>
          <div class="grid2">
            <div class="field"><label for="run-page">Page</label><input id="run-page" type="number" min="1" value="1"></div>
            <div class="field"><label for="run-page-size">Page size</label><input id="run-page-size" type="number" min="1" value="10"></div>
          </div>
          <div class="field"><label for="run-timeout">Timeout <span class="type">seconds, optional</span></label><input id="run-timeout" type="number" min="0" step="any" placeholder="server default"></div>
        </div>
        <div class="side-tab-panel" data-side-panel="schema" hidden>
          <div id="run-schema-slot"></div>
        </div>
      </div>
    </form>
    <div class="panel results" id="run-results-panel">
      <div class="resbar" id="run-status"></div>
      <div class="res-body" id="run-results"></div>
    </div>
  </section>

  <section id="tab-settings">
    <div class="page-head">
      <div class="titles"><h1>Settings</h1><span class="sub">Effective configuration of this server, plus preferences for this browser.</span></div>
      <span class="spacer"></span>
      <button id="copy-env" type="button" class="btn">Copy as .env</button>
    </div>
    <div class="settings">
      <nav id="settings-nav" aria-label="Settings sections"></nav>
      <div id="settings-body"><div class="loading"><span class="spin"></span>Loading settings…</div></div>
    </div>
  </section>
</main>
</div>

<div class="backdrop" id="drawer-backdrop" hidden></div>
<aside class="drawer" id="drawer" aria-hidden="true" role="dialog" aria-labelledby="drawer-title">
  <div class="drawer-head">
    <div><div class="kicker" id="drawer-kicker"></div><h3 id="drawer-title"></h3></div>
    <span class="spacer"></span>
    <button type="button" class="btn ghost icon" id="drawer-close" aria-label="Close">×</button>
  </div>
  <div class="drawer-body">
    <div id="connection-form-slot"></div>
    <div id="delete-connection-slot"></div>
    <div id="query-form-slot"></div>
    <div id="apikey-form-slot"></div>
    <div id="role-form-slot"></div>
    <div id="query-info-slot"></div>
  </div>
</aside>
<div id="palette" hidden role="dialog" aria-label="Search">
  <div class="pal-box"><input id="pal-input" type="search" autocomplete="off" spellcheck="false" placeholder="Search queries, connections, keys…" aria-label="Search"><div class="pal-list" id="pal-list"></div></div>
</div>
<div id="toasts" aria-live="polite"></div>

<script>
'use strict';

// ---- DOM builder: every value goes through textContent / createTextNode / setAttribute, never as markup. ----
function h(tag, attrs) {
  var node = document.createElement(tag);
  attrs = attrs || {};
  Object.keys(attrs).forEach(function (k) {
    var v = attrs[k];
    if (v === null || v === undefined || v === false) return;
    if (k === 'text') node.textContent = v;
    else if (k.slice(0, 2) === 'on') node[k] = v;
    else if (k === 'className') node.className = v;
    else if (k === 'value') node.value = v;
    else node.setAttribute(k, v === true ? '' : v);
  });
  function append(child) {
    if (child === null || child === undefined || child === false) return;
    if (Array.isArray(child)) { child.forEach(append); return; }
    node.appendChild(typeof child === 'string' || typeof child === 'number' ? document.createTextNode(String(child)) : child);
  }
  for (var i = 2; i < arguments.length; i++) append(arguments[i]);
  return node;
}
function clear(node) { node.textContent = ''; return node; }
function $(id) { return document.getElementById(id); }
function enc(s) { return encodeURIComponent(s); }
function loadingNode(text) { return h('div', { className: 'loading' }, h('span', { className: 'spin' }), text || 'Loading…'); }

// ---- API key: per-tab, same sessionStorage key /docs uses ----
function getKey() { try { return sessionStorage.getItem('queryapigate-key') || ''; } catch (e) { return ''; } }
function setKey(v) { try { sessionStorage.setItem('queryapigate-key', v); } catch (e) {} }
var keyEditing = false;
function paintKeyState() {
  var key = getKey();
  $('key-dot').className = 'dot ' + (key ? 'ok' : 'off');
  $('key-dot-narrow').className = 'dot ' + (key ? 'ok' : 'off');
  $('key-state').textContent = key ? 'API key applied' : 'No API key';
  var showView = !!key && !keyEditing;
  $('key-view').hidden = !showView;
  $('key-bar').hidden = showView;
  // the last four characters only when the key is long enough that they reveal nothing useful
  $('key-mask').textContent = '••••••••' + (key.length >= 16 ? key.slice(-4) : '');
}

// ---- feedback ----
function showError(message, detail) {
  var banner = clear($('error-banner'));
  if (!message) { banner.hidden = true; return; }
  detail = detail || {};
  var fields = detail.errors && typeof detail.errors === 'object' ? Object.keys(detail.errors) : [];
  banner.appendChild(h('div', { className: 'eb-main' },
    detail.status ? h('span', { className: 'eb-code', text: String(detail.status) }) : null,
    h('span', { className: 'eb-msg', text: String(message) }),
    h('button', { type: 'button', className: 'eb-close', 'aria-label': 'Dismiss', text: '×', onclick: function () { showError(''); } })));
  if (fields.length) {
    banner.appendChild(h('ul', { className: 'eb-fields' }, fields.map(function (f) {
      return h('li', {}, h('code', { text: f }), ' — ', String(detail.errors[f]));
    })));
  }
  banner.hidden = false;
}
function errorMessage(status, body, text) {
  var msg = (body && body.error) || text || ('HTTP ' + status);
  if (status === 401 && !getKey()) msg += ' — enter an API key in the top bar.';
  return msg;
}
function toast(msg) {
  var t = h('div', { className: 'toast', text: msg });
  $('toasts').appendChild(t);
  setTimeout(function () { t.style.opacity = '0'; }, 2200);
  setTimeout(function () { t.remove(); }, 2600);
}
function noteRate(res) {
  var limit = res.headers.get('x-ratelimit-limit'), left = res.headers.get('x-ratelimit-remaining');
  if (limit === null || left === null) return;
  var chip = $('rate');
  chip.textContent = 'rate ' + left + '/' + limit;
  chip.className = 'chip' + (Number(left) <= Math.max(1, Number(limit) * 0.1) ? ' low' : '');
  chip.hidden = false;
}

// ---- API ----
async function apiFetch(path, opts) {
  opts = opts || {};
  var headers = Object.assign({}, opts.headers || {});
  var key = getKey();
  if (key) headers['X-API-Key'] = key;
  var body = opts.body;
  if (opts.json !== undefined) { headers['Content-Type'] = 'application/json'; body = JSON.stringify(opts.json); }
  var res = await fetch(path, { method: opts.method || 'GET', headers: headers, body: body });
  noteRate(res);
  return res;
}
/** Call a JSON endpoint; on failure show the server's error and return null. */
async function apiJson(path, opts) {
  var res;
  try { res = await apiFetch(path, opts); }
  catch (e) { showError('Network error: ' + e.message); return null; }
  var text = await res.text(), body = null;
  try { body = text ? JSON.parse(text) : null; } catch (e) { /* not JSON */ }
  if (!res.ok) { showError(errorMessage(res.status, body, ''), { status: res.status, errors: body && body.errors }); return null; }
  return body;
}

// ---- tabs (the sidebar) ----
var NAV_BUTTONS = document.querySelectorAll('#tabs button, #nav-settings');
function showTab(name) {
  NAV_BUTTONS.forEach(function (b) {
    var on = b.dataset.tab === name;
    b.classList.toggle('active', on);
    b.setAttribute('aria-selected', on ? 'true' : 'false');
    if (on) {
      $('crumb-group').textContent = b.dataset.group;
      $('crumb-page').textContent = b.dataset.label;
    }
  });
  document.querySelectorAll('main > section').forEach(function (s) { s.classList.toggle('active', s.id === 'tab-' + name); });
  try { sessionStorage.setItem('queryapigate-ui-tab', name); } catch (e) {}
}
NAV_BUTTONS.forEach(function (btn) { btn.onclick = function () { showTab(btn.dataset.tab); }; });

// ---- sidebar: collapsible, remembered per browser, Ctrl/Cmd+B ----
function setSideCollapsed(collapsed) {
  document.body.classList.toggle('side-collapsed', collapsed);
  var toggle = $('side-toggle');
  toggle.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
  toggle.title = collapsed ? 'Expand sidebar (Ctrl B)' : 'Collapse sidebar (Ctrl B)';
  toggle.setAttribute('aria-label', collapsed ? 'Expand sidebar' : 'Collapse sidebar');
  try { localStorage.setItem('queryapigate-ui-side-collapsed', collapsed ? '1' : '0'); } catch (e) {}
}
$('side-toggle').onclick = function () { setSideCollapsed(!document.body.classList.contains('side-collapsed')); };
document.addEventListener('keydown', function (e) {
  if ((e.ctrlKey || e.metaKey) && !e.shiftKey && !e.altKey && e.key.toLowerCase() === 'b') {
    e.preventDefault();
    $('side-toggle').onclick();
  }
});
try { if (localStorage.getItem('queryapigate-ui-side-collapsed') === '1') setSideCollapsed(true); } catch (e) {}

// ---- global search (Ctrl/Cmd+K): jump to a connection, saved query, API key or role ----
var palSel = 0, palItems = [];
function flashRow(boxId, name) {
  var row = Array.prototype.filter.call(document.querySelectorAll('#' + boxId + ' [data-name]'), function (el) { return el.getAttribute('data-name') === name; })[0];
  if (!row) return;
  row.scrollIntoView({ block: 'center' });
  row.classList.remove('flash'); void row.offsetWidth; row.classList.add('flash');
}
function paletteEntries() {
  var out = [];
  Object.keys(connectionsCache).sort().forEach(function (n) {
    out.push({ group: 'Connections', name: n, desc: connectionsCache[n].db || '', go: function () { showTab('connections'); connFilter = 'all'; renderConnections(); flashRow('connections-table', n); } });
  });
  filesCache.forEach(function (f) {
    var l = latestOf(f);
    out.push({ group: 'Saved queries', name: f.filename, desc: l.description || '', extra: [f.collection, (l.tags || []).join(' ')].join(' '), go: function () {
      showTab('queries'); $('query-filter').value = ''; selected.name = f.filename; selected.version = l.version; selected.tab = 'run';
      queriesView = 'queries'; queriesActiveCollection = f.collection || '';
      renderQueryList(); renderDetail(); flashRow('queries-table', f.filename); } });
  });
  Object.keys(apiKeysCache).sort().forEach(function (n) {
    out.push({ group: 'API keys', name: n, desc: apiKeysCache[n].rate_limit || '', go: function () { showTab('apikeys'); flashRow('apikeys-table', n); } });
  });
  Object.keys(rolesCache).sort().forEach(function (n) {
    out.push({ group: 'Roles', name: n, desc: '', go: function () { showTab('roles'); flashRow('roles-table', n); } });
  });
  return out;
}
function paintPalette() {
  var q = $('pal-input').value.trim().toLowerCase();
  palItems = paletteEntries().filter(function (e) { return !q || [e.name, e.desc, e.extra || '', e.group].join(' ').toLowerCase().indexOf(q) !== -1; }).slice(0, 40);
  if (palSel >= palItems.length) palSel = Math.max(0, palItems.length - 1);
  var list = clear($('pal-list'));
  if (!palItems.length) { list.appendChild(h('div', { className: 'pal-empty', text: q ? 'Nothing matches “' + q + '”.' : 'Nothing to search yet.' })); return; }
  var lastGroup = null;
  palItems.forEach(function (e, i) {
    if (e.group !== lastGroup) { list.appendChild(h('div', { className: 'pal-group', text: e.group })); lastGroup = e.group; }
    list.appendChild(h('button', { type: 'button', className: 'pal-item' + (i === palSel ? ' on' : ''), onclick: function () { palGo(i); },
      onmousemove: function () { if (palSel !== i) { palSel = i; paintPalette(); } } },
      h('span', { className: 'nm', text: e.name }), h('span', { className: 'ds', text: e.desc })));
  });
  var on = list.querySelector('.pal-item.on');
  if (on) on.scrollIntoView({ block: 'nearest' });
}
function palGo(i) { var e = palItems[i]; if (!e) return; closePalette(); e.go(); }
function openPalette() { $('palette').hidden = false; $('pal-input').value = ''; palSel = 0; paintPalette(); $('pal-input').focus(); }
function closePalette() { $('palette').hidden = true; }
$('global-search').onclick = openPalette;
$('palette').onmousedown = function (e) { if (e.target === $('palette')) closePalette(); };
$('pal-input').oninput = function () { palSel = 0; paintPalette(); };
$('pal-input').onkeydown = function (e) {
  if (e.key === 'ArrowDown') { e.preventDefault(); palSel = Math.min(palItems.length - 1, palSel + 1); paintPalette(); }
  else if (e.key === 'ArrowUp') { e.preventDefault(); palSel = Math.max(0, palSel - 1); paintPalette(); }
  else if (e.key === 'Enter') { e.preventDefault(); palGo(palSel); }
  else if (e.key === 'Escape') { e.preventDefault(); closePalette(); }
};
document.addEventListener('keydown', function (e) {
  if ((e.ctrlKey || e.metaKey) && !e.shiftKey && !e.altKey && e.key.toLowerCase() === 'k') { e.preventDefault(); if ($('palette').hidden) openPalette(); else closePalette(); }
});

// ---- interface preferences: this browser only (localStorage), never sent to the server ----
var PREFS_KEY = 'queryapigate-ui-prefs';
var PREF_DEFAULTS = { theme: 'System', density: 'Comfortable', format: 'json' };
var prefs = (function () {
  var saved = {};
  try { saved = JSON.parse(localStorage.getItem(PREFS_KEY) || '{}') || {}; } catch (e) {}
  return Object.assign({}, PREF_DEFAULTS, saved);
})();
function applyPrefs() {
  document.documentElement.style.colorScheme = { System: 'light dark', Light: 'light', Dark: 'dark' }[prefs.theme] || 'light dark';
  document.body.classList.toggle('compact', prefs.density === 'Compact');
  $('run-format').value = prefs.format;
}
function setPref(key, value) {
  prefs[key] = value;
  try { localStorage.setItem(PREFS_KEY, JSON.stringify(prefs)); } catch (e) {}
  applyPrefs();
  renderSettings();
}
var PREF_ROWS = [
  ['theme', 'Theme', 'Follows your operating system unless set.', ['System', 'Light', 'Dark']],
  ['density', 'Table density', 'Row height in lists and result grids.', ['Compact', 'Comfortable']],
  ['format', 'Default result format', 'Pre-selected format in Run SQL.', ['json', 'csv', 'ndjson', 'tsv', 'xml', 'yaml', 'xlsx']]
];

// ---- settings: a read-only view of GET /settings (environment variables), plus the interface preferences ----
var settingsData = null, settingsSection = 'general';
async function loadSettings() {
  var body = await apiJson('settings');
  if (!body) { settingsData = null; renderSettings(); return; }
  settingsData = body.sections;
  renderSettings();
  paintAuditSub();
}
function renderSettings() {
  var nav = clear($('settings-nav')), body = clear($('settings-body'));
  var sections = settingsData || [];
  function navButton(id, label, n) {
    return h('button', { type: 'button', className: settingsSection === id ? 'on' : '', onclick: function () { settingsSection = id; renderSettings(); } },
      h('span', { style: 'flex:1', text: label }), n === '' ? null : h('span', { className: 'n', text: String(n) }));
  }
  sections.forEach(function (sec) { nav.appendChild(navButton(sec.id, sec.title, sec.rows.length)); });
  nav.appendChild(navButton('ui', 'Interface', ''));
  if (settingsSection === 'ui') {
    body.appendChild(h('div', { className: 'panel' },
      h('div', { className: 'set-head' }, h('h2', { text: 'Interface' }), h('span', { text: 'Stored in this browser only. Nothing is sent to the server.' })),
      PREF_ROWS.map(function (def) {
        return h('div', { className: 'set-row pref' },
          h('div', { className: 'set-what' }, h('span', { className: 'set-label', text: def[1] }), h('span', { className: 'set-desc', text: def[2] })),
          h('div', { className: 'seg' }, def[3].map(function (opt) {
            return h('button', { type: 'button', className: prefs[def[0]] === opt ? 'on' : '', text: opt, onclick: function () { setPref(def[0], opt); } });
          })));
      })));
    return;
  }
  if (!settingsData) {
    body.appendChild(h('div', { className: 'panel' }, h('div', { className: 'empty' }, h('strong', { text: 'Settings unavailable' }),
      h('span', { text: 'Settings are visible to the admin key only. Apply it in the sidebar, or check the message at the top of the page.' }))));
    return;
  }
  var sec = sections.filter(function (x) { return x.id === settingsSection; })[0] || sections[0];
  if (!sec) return;
  settingsSection = sec.id;
  body.appendChild(h('div', { className: 'set-note' }, h('i'),
    h('span', {}, 'Read-only. These values come from ', h('code', { text: 'QUERYAPIGATE_*' }),
      ' environment variables and are validated at startup. Change them in your deployment and restart the server.')));
  body.appendChild(h('div', { className: 'panel' },
    h('div', { className: 'set-head' }, h('h2', { text: sec.title }), h('span', { text: sec.description })),
    sec.rows.map(function (row) {
      return h('div', { className: 'set-row' },
        h('div', { className: 'set-what' }, h('span', { className: 'set-label', text: row.label }), h('span', { className: 'set-desc', text: row.description }),
          h('code', { className: 'set-env', text: row.env })),
        h('div', { className: 'set-val' }, h('span', { className: 'v', title: row.value, text: row.value }),
          h('span', { className: 'badge ' + row.source, text: row.source })));
    })));
}
$('copy-env').onclick = function () {
  var lines = [];
  (settingsData || []).forEach(function (sec) {
    sec.rows.forEach(function (row) { if (row.env_value !== null && row.env_value !== undefined) lines.push(row.env + '=' + row.env_value); });
  });
  if (!lines.length) { toast('Nothing to copy: every setting is at its default (secrets are never copied)'); return; }
  copyText(lines.join('\n') + '\n');
};

paintKeyState();
$('key-change').onclick = function () { keyEditing = true; paintKeyState(); $('key').value = ''; $('key').focus(); };
$('key-bar').onsubmit = function (e) {
  e.preventDefault();
  setKey($('key').value);
  keyEditing = false;
  $('key').value = '';
  paintKeyState();
  showError('');
  toast($('key').value ? 'API key applied to this tab' : 'API key cleared');
  refreshAll();
};

// ---- drawer (hosts the connection and saved-query forms) ----
function openDrawer(slotId, title, kicker) {
  clear($('connection-form-slot')); clear($('query-form-slot')); clear($('apikey-form-slot')); clear($('role-form-slot')); clear($('query-info-slot')); clear($('delete-connection-slot'));
  $('drawer-title').textContent = title;
  $('drawer-kicker').textContent = kicker || '';
  $('drawer').classList.add('open');
  $('drawer').setAttribute('aria-hidden', 'false');
  $('drawer-backdrop').hidden = false;
  return $(slotId);
}
function closeDrawer() {
  $('drawer').classList.remove('open');
  $('drawer').setAttribute('aria-hidden', 'true');
  $('drawer-backdrop').hidden = true;
  clear($('connection-form-slot')); clear($('query-form-slot')); clear($('apikey-form-slot')); clear($('role-form-slot')); clear($('query-info-slot')); clear($('delete-connection-slot'));
}
$('drawer-close').onclick = closeDrawer;
$('drawer-backdrop').onclick = closeDrawer;
document.addEventListener('keydown', function (e) { if (e.key === 'Escape' && $('drawer').classList.contains('open')) closeDrawer(); });
function field(id, label, input, hint, extra) {
  return h('div', { className: 'field' }, h('label', { for: id }, label, extra || null), input,
    hint ? h('div', { className: 'hint', text: hint }) : null);
}
function formActions(submitText, onCancel) {
  var submit = h('button', { type: 'submit', className: 'btn primary', text: submitText });
  return { node: h('div', { className: 'form-actions' }, h('button', { type: 'button', className: 'btn', text: 'Cancel', onclick: onCancel }), submit), submit: submit };
}

// ---- SQL highlighting (tokens become spans via textContent) ----
var SQL_KW = {};
('select from where and or not in is null as join left right inner outer full cross on using group by order having limit offset union ' +
 'all distinct insert into values update set delete create table view index drop alter with recursive case when then else end asc desc ' +
 'like ilike between exists returning primary key default top fetch next rows only over partition window count sum avg min max ' +
 'coalesce cast true false interval date timestamp extract filter lateral intersect except nulls first last')
  .split(' ').forEach(function (w) { SQL_KW[w] = true; });
var SQL_TOKEN = /(--[^\n]*|\/\*[\s\S]*?(?:\*\/|$))|('(?:[^']|'')*'?)|("(?:[^"]|"")*"?|`[^`]*`?)|(::)|(:[A-Za-z_]\w*)|(\b\d+(?:\.\d+)?\b)|([A-Za-z_]\w*)/g;
function highlightInto(target, sql) {
  var frag = document.createDocumentFragment(), last = 0, m;
  SQL_TOKEN.lastIndex = 0;
  function span(cls, text) { var s = document.createElement('span'); s.className = cls; s.textContent = text; frag.appendChild(s); }
  while ((m = SQL_TOKEN.exec(sql))) {
    if (m.index > last) frag.appendChild(document.createTextNode(sql.slice(last, m.index)));
    if (m[1]) span('c', m[1]);
    else if (m[2]) span('s', m[2]);
    else if (m[5]) span('p', m[5]);
    else if (m[6]) span('n', m[6]);
    else if (m[7] && SQL_KW[m[7].toLowerCase()]) span('k', m[7]);
    else frag.appendChild(document.createTextNode(m[0]));
    last = SQL_TOKEN.lastIndex;
  }
  if (last < sql.length) frag.appendChild(document.createTextNode(sql.slice(last)));
  clear(target).appendChild(frag);
  return target;
}
function sqlParams(sql) {
  var seen = {}, out = [], m;
  SQL_TOKEN.lastIndex = 0;
  while ((m = SQL_TOKEN.exec(sql))) if (m[5] && !seen[m[5]]) { seen[m[5]] = true; out.push(m[5].slice(1)); }
  return out;
}
/** Mirrors mongotools.placeholder_names() client-side, for a Mongo query's "detected params" preview - a
 * string leaf that is exactly ":name" (not sqlParams()'s SQL tokenizer, which treats a JSON-quoted string
 * as opaque and would miss every one of these). Returns [] for text that isn't valid JSON. */
function mongoDocParams(text) {
  var doc;
  try { doc = text.trim() ? JSON.parse(text) : {}; } catch (e) { return []; }
  var seen = {}, out = [];
  (function walk(node) {
    if (typeof node === 'string') { var m = /^:([A-Za-z_]\w*)$/.exec(node); if (m && !seen[m[1]]) { seen[m[1]] = true; out.push(m[1]); } }
    else if (Array.isArray(node)) node.forEach(walk);
    else if (node && typeof node === 'object') Object.keys(node).forEach(function (k) { walk(node[k]); });
  })(doc);
  return out;
}
/** `gutter`, if given, gets one line number per line, kept in sync with the textarea's own scroll position -
 * "what line is that part of the query on" for anything longer than a couple of lines, the same question a
 * code editor's own gutter answers. Rebuilt on every keystroke like the highlight overlay already was;
 * SQL is never long enough for that to be a real cost. */
function bindEditor(textarea, pre, onChange, gutter) {
  var gutterLines = 0;
  function paintGutter() {
    var count = (textarea.value.match(/\n/g) || []).length + 1;
    if (count === gutterLines) return;
    gutterLines = count;
    var lines = [];
    for (var i = 1; i <= count; i++) lines.push(i);
    clear(gutter).appendChild(h('div', { style: 'white-space:pre' }, lines.join('\n')));
  }
  function paint() {
    highlightInto(pre, textarea.value + '\n');
    pre.scrollTop = textarea.scrollTop; pre.scrollLeft = textarea.scrollLeft;
    if (gutter) { paintGutter(); gutter.scrollTop = textarea.scrollTop; }
    if (onChange) onChange(textarea.value);
  }
  textarea.addEventListener('input', paint);
  textarea.addEventListener('scroll', function () {
    pre.scrollTop = textarea.scrollTop; pre.scrollLeft = textarea.scrollLeft;
    if (gutter) gutter.scrollTop = textarea.scrollTop;
  });
  textarea.addEventListener('keydown', function (e) {
    if (e.key === 'Tab' && !e.shiftKey && !e.ctrlKey && !e.metaKey) {
      e.preventDefault();
      var s = textarea.selectionStart, en = textarea.selectionEnd;
      textarea.value = textarea.value.slice(0, s) + '  ' + textarea.value.slice(en);
      textarea.selectionStart = textarea.selectionEnd = s + 2;
      paint();
    }
  });
  textarea.repaint = paint;
  paint();
}
function makeEditor(attrs, value) {
  var ta = h('textarea', Object.assign({ spellcheck: 'false', wrap: 'off', autocomplete: 'off' }, attrs));
  ta.value = value || '';
  var pre = h('pre', { className: 'hl', 'aria-hidden': 'true' });
  var gutter = h('div', { className: 'editor-gutter', 'aria-hidden': 'true' });
  var wrap = h('div', { className: 'editor boxed' }, gutter, pre, ta);
  bindEditor(ta, pre, undefined, gutter);
  return wrap;
}

// ---- schema browser: click a table or column to insert its name into a SQL editor ----
function insertAtCursor(textarea, text) {
  var start = textarea.selectionStart, end = textarea.selectionEnd;
  textarea.value = textarea.value.slice(0, start) + text + textarea.value.slice(end);
  var pos = start + text.length;
  textarea.selectionStart = textarea.selectionEnd = pos;
  textarea.focus();
  if (textarea.repaint) textarea.repaint();
}
var schemaCache = {}; // "name" (or "name::database" - see below) -> {status: 'loading'|'ready'|'error', tables, truncated, message}
// Three independent consumers now share this one cache by connection (the tree-based schema browser below,
// the access map's table filter, and Run SQL's own table dropdown) - schemaWaiters is what lets more than one
// of them wait on the *same* in-flight fetch instead of only whichever one happened to start it getting
// notified when it resolves. A caller asks for `key` by calling loadSchema() whenever schemaCache[key] is
// either absent or already 'loading' - the second case is exactly "someone else started this fetch," which
// loadSchema() below turns into "queue behind it" rather than firing a second, redundant request.
var schemaWaiters = {}; // key -> [onDone, ...] queued behind an in-flight fetch for that key
async function loadSchema(name, onDone, database) {
  var key = database ? name + '::' + database : name;
  if (schemaCache[key] && schemaCache[key].status === 'loading') {
    (schemaWaiters[key] = schemaWaiters[key] || []).push(onDone);
    return;
  }
  schemaCache[key] = { status: 'loading' };
  var url = 'connections/' + enc(name) + '/schema' + (database ? '?database=' + enc(database) : '');
  var res;
  try { res = await apiFetch(url); }
  catch (e) { schemaCache[key] = { status: 'error', message: 'Network error: ' + e.message }; return finishSchemaFetch(key, onDone); }
  if (!res.ok) {
    var body = null;
    try { body = await res.json(); } catch (e) {}
    schemaCache[key] = { status: 'error', message: (body && body.error) || ('HTTP ' + res.status) };
    return finishSchemaFetch(key, onDone);
  }
  var data = await res.json();
  schemaCache[key] = { status: 'ready', tables: data.tables || [], truncated: !!data.truncated };
  finishSchemaFetch(key, onDone);
}
function finishSchemaFetch(key, onDone) {
  var waiters = schemaWaiters[key];
  delete schemaWaiters[key];
  onDone();
  if (waiters) waiters.forEach(function (fn) { fn(); });
}
/** A self-contained, connection-aware schema browser: a Tables tab (click a table to see its Columns) and a
 * Columns tab (click a column to insert it - `insertFn(text)` - into the nearest SQL editor). Errors (an
 * unsupported connection type, no permission, ...) render inline rather than through the page-wide error
 * banner, since browsing the schema is optional, not the action the user took. */
/** `setDatabase()` lets a caller with its own "browse a different database on this server" picker (Run SQL's
 * own Database dropdown) point this same tree at that database instead of the connection's configured
 * default - the exact schema-cache key loadSchema() and the access map's table filter also use, so they
 * all agree on the same fetch and never disagree with each other about what's in a given database. */
function schemaBrowser(insertFn, previewFn, selectFn) {
  var box = h('div', { className: 'schema-browser' });
  var current = null, currentDb = null;
  var view = 'tables', activeTableName = null; // 'tables' or 'columns' - which one activeTableName is showing
  function key() { return currentDb ? current + '::' + currentDb : current; }
  function showTables() { view = 'tables'; paint(); }
  function showColumns(name) { view = 'columns'; activeTableName = name; paint(); }
  function paintTables(content, entry) {
    entry.tables.forEach(function (t) {
      content.appendChild(h('div', { className: 'schema-row' },
        h('button', { type: 'button', className: 'schema-table', title: 'Columns of ' + t.name, onclick: function () { showColumns(t.name); } },
          h('span', { className: 'name', text: t.name }), h('span', { className: 'tag', text: t.type })),
        selectFn ? h('button', { type: 'button', className: 'schema-select', title: 'Copy a starter query for ' + t.name + ' into the editor', 'aria-label': 'Copy a starter query for ' + t.name,
          onclick: function () { selectFn(t); } }, '⧉') : null,
        previewFn ? h('button', { type: 'button', className: 'schema-preview', title: 'Preview ' + t.name + ' in Run SQL', 'aria-label': 'Preview ' + t.name,
          onclick: function () { previewFn(t.name); } }, '👁') : null));
    });
    if (entry.truncated) content.appendChild(h('div', { className: 'hint', text: 'Showing the first 5000 columns.' }));
  }
  function paintColumns(content, entry) {
    var t = entry.tables.filter(function (x) { return x.name === activeTableName; })[0];
    if (!t) { showTables(); return; } // the table it was showing is gone (e.g. a refresh) - nothing sane to show
    if (!t.columns.length) { content.appendChild(h('div', { className: 'hint', text: 'No columns to show.' })); return; }
    t.columns.forEach(function (c) {
      content.appendChild(h('button', {
        type: 'button', className: 'schema-col', title: c.type + (c.nullable ? ' · nullable' : ' · not null'),
        onclick: function () { insertFn(c.name); }
      }, h('span', { text: c.name }), h('span', { className: 'dim', text: c.type })));
    });
  }
  function paint() {
    clear(box);
    if (!current) { box.appendChild(h('div', { className: 'hint', text: 'Pick a connection to browse its schema.' })); return; }
    var entry = schemaCache[key()];
    if (!entry || entry.status === 'loading') { loadSchema(current, paint, currentDb); entry = { status: 'loading' }; }
    if (entry.status === 'loading') { box.appendChild(loadingNode('Loading schema…')); return; }
    if (entry.status === 'error') { box.appendChild(h('div', { className: 'hint', text: entry.message })); return; }
    if (!entry.tables.length) { box.appendChild(h('div', { className: 'hint', text: 'No tables found.' })); return; }
    box.appendChild(h('div', { className: 'minitabs' },
      h('button', { type: 'button', className: 'minitab' + (view === 'tables' ? ' active' : ''), onclick: showTables }, 'Tables'),
      h('button', { type: 'button', className: 'minitab' + (view === 'columns' ? ' active' : ''), disabled: !activeTableName,
        onclick: function () { if (activeTableName) showColumns(activeTableName); } },
        'Columns' + (activeTableName ? ' · ' + activeTableName : ''))));
    var content = h('div', { className: 'schema-content' });
    box.appendChild(content);
    if (view === 'columns' && activeTableName) paintColumns(content, entry);
    else paintTables(content, entry);
  }
  paint();
  return {
    node: box,
    setConnection: function (name) { current = name || null; currentDb = null; view = 'tables'; activeTableName = null; paint(); },
    setDatabase: function (database) { currentDb = database || null; view = 'tables'; activeTableName = null; paint(); },
    refresh: function () { delete schemaCache[key()]; paint(); }
  };
}
function schemaField(browser) {
  return field(null, 'Schema', browser.node, null,
    h('button', { type: 'button', className: 'btn ghost sm', text: '↻', title: 'Refresh', onclick: function () { browser.refresh(); } }));
}

// ---- connections ----
var DB_TYPES = ['mysql', 'postgres', 'clickhouse', 'sqlite', 'h2', 'duckdb', 'mongo']; // mirrors config.SUPPORTED_DB_TYPES
// Dialects that support browsing/switching to a different database on the same server - the Load-databases
// button on the connection form and the Database dropdown/tree override in Run SQL.
var DB_SWITCHABLE_TYPES = ['mysql', 'postgres', 'clickhouse', 'mongo'];
var PASSWORD_MASK = '********'; // mirrors config.PASSWORD_MASK; sending it back unchanged keeps the stored password
var connectionsCache = {};

function openConnectionForm(name, existing) {
  existing = existing || {};
  var isEdit = !!name;
  var slot = openDrawer('connection-form-slot', isEdit ? 'Edit connection' : 'New connection', isEdit ? name : 'PATCH /connections');
  function inp(id, type, value, ph) {
    return h('input', { id: id, type: type || 'text', value: value === undefined || value === null ? '' : String(value), placeholder: ph || null, autocomplete: 'off', spellcheck: 'false' });
  }
  function withRevealToggle(input) {
    var btn = h('button', { type: 'button', className: 'btn ghost sm', 'aria-label': 'Show password', text: 'Show' });
    btn.onclick = function () {
      var showing = input.type === 'text';
      input.type = showing ? 'password' : 'text';
      btn.textContent = showing ? 'Show' : 'Hide';
      btn.setAttribute('aria-label', (showing ? 'Show' : 'Hide') + ' password');
    };
    return h('div', { className: 'password-field' }, input, btn);
  }
  var nameInput = inp('c-name', 'text', name || '', 'reporting-db');
  if (isEdit) nameInput.disabled = true;
  var dbSelect = h('select', { id: 'c-db' }, DB_TYPES.map(function (t) { return h('option', { value: t, text: t }); }));
  if (existing.db) dbSelect.value = existing.db;
  var active = h('input', { id: 'c-active', type: 'checkbox' });
  active.checked = existing.active !== false;
  function collectDetails() {
    var port = $('c-port').value;
    return {
      db: dbSelect.value,
      host: $('c-host').value || undefined,
      port: port ? Number(port) : undefined,
      user: $('c-user').value || undefined,
      password: $('c-password').value,
      database: $('c-database').value || undefined
    };
  }
  // The Default database field starts as a plain text input (the only option for sqlite/duckdb/h2/jdbc, and
  // the fallback for mysql/postgres/clickhouse until "Load databases…" is clicked) and can turn into a
  // dropdown of the server's real databases, in place, without disturbing anything else on the form.
  var dbFieldSlot = h('div', {});
  function paintDatabaseField(options, selected) {
    clear(dbFieldSlot);
    if (!options) { dbFieldSlot.appendChild(inp('c-database', 'text', selected, 'SQLite, DuckDB and H2 take a file path')); return; }
    var sel = h('select', { id: 'c-database' }, options.map(function (name) { return h('option', { value: name, text: name }); }));
    if (options.indexOf(selected) !== -1) sel.value = selected;
    dbFieldSlot.appendChild(sel);
  }
  paintDatabaseField(null, existing.database);
  var dbLoadResult = h('span', { className: 'test-result' });
  async function runDbLoad() {
    var current = $('c-database').value;
    dbLoadBtn.disabled = true; dbLoadBtn.textContent = 'Loading…'; clear(dbLoadResult);
    var details = collectDetails();
    if (isEdit) details.name = name;
    var res, body = null;
    try { res = await apiFetch('connections/databases', { method: 'POST', json: details }); body = await res.json().catch(function () { return null; }); }
    catch (e) { res = null; }
    dbLoadBtn.disabled = false; dbLoadBtn.textContent = 'Load databases…';
    if (res && res.ok && body && body.databases) {
      paintDatabaseField(body.databases, current);
      dbLoadResult.appendChild(h('span', { className: 'test-ok', text: '✓ ' + body.databases.length + (body.databases.length === 1 ? ' database' : ' databases') }));
    } else {
      var msg = (body && body.error) || 'Network error';
      if (body && body.detail) msg += ': ' + body.detail;
      dbLoadResult.appendChild(h('span', { className: 'test-fail', text: '✗ ' + msg }));
    }
  }
  var dbLoadBtn = h('button', { type: 'button', className: 'btn sm ghost', text: 'Load databases…', hidden: true, onclick: runDbLoad });
  // Editing an existing connection already has real, saved credentials, so there is no need to make the user
  // click "Load databases…" themselves - do it once, automatically, the first time the field becomes relevant.
  var dbAutoLoaded = false;
  function paintDbLoadVisibility() {
    dbLoadBtn.hidden = DB_SWITCHABLE_TYPES.indexOf(dbSelect.value) === -1;
    // Deferred: the rest of the form (host/port/user/password fields collectDetails() reads) is still being
    // built when this first runs on initial paint, and only exists in the DOM once this synchronous function
    // returns.
    if (!dbLoadBtn.hidden && isEdit && !dbAutoLoaded) { dbAutoLoaded = true; setTimeout(runDbLoad, 0); }
  }
  dbSelect.addEventListener('change', paintDbLoadVisibility);
  paintDbLoadVisibility();
  var testResult = h('span', { className: 'test-result' });
  var testBtn = h('button', { type: 'button', className: 'btn', text: 'Test connection', onclick: async function () {
    testBtn.disabled = true; testBtn.textContent = 'Testing…'; clear(testResult);
    var details = collectDetails();
    if (isEdit) details.name = name; // lets a still-masked password resolve to the real stored one, server-side
    var res, body = null;
    try { res = await apiFetch('connections/test', { method: 'POST', json: details }); body = await res.json().catch(function () { return null; }); }
    catch (e) { res = null; }
    testBtn.disabled = false; testBtn.textContent = 'Test connection';
    if (res && res.ok) {
      testResult.appendChild(h('span', { className: 'test-ok', text: '✓ Connected' + (body && body.elapsed_ms !== undefined ? ' (' + body.elapsed_ms + ' ms)' : '') }));
    } else {
      var msg = (body && body.error) || 'Network error';
      if (body && body.detail) msg += ': ' + body.detail;
      testResult.appendChild(h('span', { className: 'test-fail', text: '✗ ' + msg }));
    }
  } });
  var actions = formActions(isEdit ? 'Save' : 'Create', closeDrawer);
  var form = h('form', { className: 'form', novalidate: true, onsubmit: function (e) {
    e.preventDefault();
    saveConnection(name, Object.assign(collectDetails(), { active: active.checked }), nameInput.value.trim(), actions.submit);
  } },
    field('c-name', 'Name', nameInput, isEdit ? 'Renaming isn’t supported — create a new connection instead.' : 'Referenced as connection_name by queries and the API.'),
    field('c-db', 'Database type', dbSelect),
    h('div', { className: 'grid-host' }, field('c-host', 'Host', inp('c-host', 'text', existing.host, 'localhost')),
      field('c-port', 'Port', inp('c-port', 'number', existing.port, ''))),
    h('div', { className: 'grid2' }, field('c-user', 'User', inp('c-user', 'text', existing.user, '')),
      field('c-password', 'Password', withRevealToggle(inp('c-password', 'password', isEdit ? (existing.password === undefined ? '' : existing.password) : '', '')),
        isEdit ? 'Leave the mask to keep the stored password.' : '${ENV_VAR} references are resolved on the server.')),
    field('c-database', 'Default database', dbFieldSlot, 'SQLite, DuckDB and H2 take a file path here instead.'),
    h('div', { className: 'test-row' }, dbLoadBtn, dbLoadResult),
    h('label', { className: 'switch' }, active, 'Active', h('span', { className: 'hint', text: '— inactive connections refuse queries' })),
    h('div', { className: 'test-row' }, testBtn, testResult),
    isEdit && existing.created_at ? h('div', { className: 'hint' },
      'Created ' + existing.created_at + (existing.updated_at && existing.updated_at !== existing.created_at ? ' · last edited ' + existing.updated_at : '')) : null,
    actions.node);
  slot.appendChild(form);
  (isEdit ? $('c-host') : nameInput).focus();
}
$('new-connection').onclick = function () { openConnectionForm(null, {}); };

async function saveConnection(originalName, details, newName, btn) {
  var name = originalName || newName;
  if (!name) { showError('A connection name is required.', { errors: { name: 'This field is required' } }); $('c-name').focus(); return; }
  var body = {};
  body[name] = details;
  btn.disabled = true;
  var res = await apiJson('connections', { method: 'PATCH', json: { connections: body } });
  btn.disabled = false;
  if (res) { showError(''); closeDrawer(); toast((originalName ? 'Saved ' : 'Created ') + name); loadConnections(); }
}
/** Deleting a connection is destructive and breaks every saved query that used it, so this asks for a
 * reason (kept nowhere but the audit log - see app.py's delete_connection()) and makes the admin type the
 * connection's own name back, exactly, before the Delete button will even enable - the same "you cannot
 * click this by reflex" friction a terminal's "type the resource name to confirm" prompt gives. */
function openDeleteConnectionForm(name) {
  var slot = openDrawer('delete-connection-slot', 'Delete connection', name);
  var reasonInput = h('textarea', { id: 'del-reason', placeholder: 'Why is this connection being deleted?' });
  var confirmInput = h('input', { id: 'del-confirm', type: 'text', autocomplete: 'off', spellcheck: 'false', placeholder: name });
  var actions = formActions('Delete', closeDrawer);
  actions.submit.classList.remove('primary'); actions.submit.classList.add('danger');
  actions.submit.disabled = true;
  function checkReady() { actions.submit.disabled = !(reasonInput.value.trim() && confirmInput.value === name); }
  reasonInput.addEventListener('input', checkReady);
  confirmInput.addEventListener('input', checkReady);
  var form = h('form', { className: 'form', novalidate: true, onsubmit: async function (e) {
    e.preventDefault();
    actions.submit.disabled = true;
    var res = await apiJson('connections/' + enc(name), { method: 'DELETE', json: { reason: reasonInput.value.trim() } });
    if (res) { closeDrawer(); toast('Deleted ' + name); loadConnections(); loadAuditLog(); }
    else checkReady();
  } },
    h('p', { className: 'd-desc' }, 'This deletes ', h('span', { className: 'name' }, name),
      ' permanently. Every saved query that uses it will stop working. It moves to the Deleted tab, with the reason and who deleted it.'),
    field('del-reason', 'Reason', reasonInput, 'Recorded in the audit log - required.'),
    field('del-confirm', 'Type "' + name + '" to confirm', confirmInput),
    actions.node);
  slot.appendChild(form);
  reasonInput.focus();
}
async function loadConnections() {
  var data = await apiJson('connections');
  var box = $('connections-table');
  if (!data) {
    clear(box).appendChild(h('div', { className: 'empty' }, h('strong', { text: 'Couldn’t load connections' }),
      h('button', { type: 'button', className: 'btn sm', text: 'Retry', onclick: loadConnections })));
    return;
  }
  connectionsCache = data.connections || {};
  populateConnectionSelect();
  renderConnections();
}
// A key's usage has no avg_duration_ms (request/query latency isn't split by key - see metrics.py); a
// connection's does, since a connection has exactly one dialect and its latency histogram is keyed by it.
function usageCell(usage) {
  if (!usage || !usage.queries) return h('td', { className: 'dim', style: 'white-space:nowrap', text: 'No activity yet' });
  var parts = [usage.queries + (usage.queries === 1 ? ' query' : ' queries')];
  if (usage.errors) parts.push(usage.errors + ' failed');
  if (usage.avg_duration_ms !== undefined && usage.avg_duration_ms !== null) parts.push(usage.avg_duration_ms + 'ms avg');
  var title = usage.rows + (usage.rows === 1 ? ' row' : ' rows') + ' returned in total · since this process started';
  return h('td', { title: title, style: 'white-space:nowrap', text: parts.join(' · ') });
}
var connFilter = 'all';
/** Every 'delete_connection' audit entry, newest first - the Deleted tab's whole data source. There is no
 * separate deleted-connections store; a delete is destructive (the connection is gone, and store.py never
 * kept a tombstone for it), so the audit log - which already recorded a full snapshot plus the reason - is
 * the only place this can come from, exactly like #35's "everything auditable" design elsewhere. */
function deletedConnections() {
  return auditLogCache.filter(function (e) { return e.action === 'delete_connection'; });
}
function renderConnections() {
  var box = clear($('connections-table'));
  var all = Object.keys(connectionsCache).sort();
  var activeCount = all.filter(function (n) { return connectionsCache[n].active; }).length;
  var deleted = deletedConnections();
  $('count-connections').textContent = all.length ? String(all.length) : '';
  var tabs = clear($('conn-tabs'));
  [['all', 'All', all.length], ['active', 'Active', activeCount], ['inactive', 'Inactive', all.length - activeCount],
   ['deleted', 'Deleted', deleted.length]].forEach(function (t) {
    tabs.appendChild(h('button', { type: 'button', className: connFilter === t[0] ? 'on' : '', onclick: function () { connFilter = t[0]; renderConnections(); } },
      t[1], h('span', { className: 'n', text: String(t[2]) })));
  });
  var foot = clear($('connections-foot'));
  $('conn-filter').placeholder = connFilter === 'deleted' ? 'Filter by name, actor, reason…' : 'Filter by name, type, host…';
  $('new-connection').hidden = connFilter === 'deleted';

  if (connFilter === 'deleted') {
    if (!deleted.length) { box.appendChild(h('div', { className: 'empty' }, h('strong', { text: 'No deleted connections' }),
      h('span', { text: 'Every connection deletion is recorded here, with who did it, when, and why.' }))); return; }
    var qd = $('conn-filter').value.trim().toLowerCase();
    var shown = deleted.filter(function (e) {
      return !qd || [e.target, e.actor, e.changes.deleted_reason, e.changes.db, e.changes.host].join(' ').toLowerCase().indexOf(qd) !== -1;
    });
    foot.appendChild(h('span', { text: 'Showing ' + shown.length + ' of ' + deleted.length + (deleted.length === 1 ? ' deleted connection' : ' deleted connections') }));
    if (!shown.length) { box.appendChild(h('div', { className: 'empty' }, h('span', { text: 'No deleted connections match “' + qd + '”.' }))); return; }
    var drows = shown.map(function (e) {
      var c = e.changes || {};
      var endpoint = c.host ? c.host + (c.port ? ':' + c.port : '') : '';
      return h('tr', {},
        h('td', {}, h('span', { className: 'name', text: e.target })),
        h('td', {}, h('span', { className: 'tag', text: c.db || '?' })),
        h('td', { className: 'mono', style: 'white-space:nowrap' }, endpoint || h('span', { className: 'dim', text: '—' })),
        h('td', { className: 'mono', text: c.database || '' }),
        h('td', { className: 'mono dim', style: 'white-space:nowrap', text: e.timestamp || '—' }),
        h('td', { className: 'mono', text: e.actor || '—' }),
        h('td', { text: c.deleted_reason || '—' }));
    });
    box.appendChild(h('div', { style: 'overflow-x:auto' }, h('table', { className: 'grid' },
      h('thead', {}, h('tr', {}, ['Name', 'Type', 'Host', 'Database', 'Deleted at', 'Deleted by', 'Reason'].map(function (t) { return h('th', { text: t }); }))),
      h('tbody', {}, drows))));
    return;
  }

  if (!all.length) {
    box.appendChild(h('div', { className: 'empty' }, h('strong', { text: 'No connections yet' }),
      h('span', { text: 'Add a MySQL, PostgreSQL, ClickHouse, SQLite or H2 database to start running SQL.' }),
      h('button', { type: 'button', className: 'btn primary', text: 'New connection', onclick: function () { openConnectionForm(null, {}); } })));
    return;
  }
  var q = $('conn-filter').value.trim().toLowerCase();
  var names = all.filter(function (n) {
    var c = connectionsCache[n];
    if (connFilter === 'active' && !c.active) return false;
    if (connFilter === 'inactive' && c.active) return false;
    return !q || [n, c.db, c.host, c.database, c.user].join(' ').toLowerCase().indexOf(q) !== -1;
  });
  foot.appendChild(h('span', { text: 'Showing ' + names.length + ' of ' + all.length + (all.length === 1 ? ' connection' : ' connections') }));
  if (!names.length) { box.appendChild(h('div', { className: 'empty' }, h('span', { text: q ? 'No connections match “' + q + '”.' : 'No ' + connFilter + ' connections.' }))); return; }
  var rows = names.map(function (name) {
    var c = connectionsCache[name];
    var endpoint = c.host ? c.host + (c.port ? ':' + c.port : '') : '';
    return h('tr', { 'data-name': name },
      h('td', {}, h('div', { style: 'display:flex;align-items:center;gap:8px' }, h('span', { className: 'name', text: name }), c.example ? exampleBadge() : null)),
      h('td', {}, h('span', { className: 'tag', text: c.db || '?' })),
      h('td', { className: 'mono', style: 'white-space:nowrap' }, endpoint || h('span', { className: 'dim', text: '—' })),
      h('td', { className: 'mono', text: c.database || '' }),
      h('td', { className: 'mono dim' }, c.user || '—'),
      h('td', {}, h('span', { className: 'pill ' + (c.active ? 'ok' : 'off') }, h('i'), c.active ? 'Active' : 'Inactive')),
      usageCell(c.usage),
      h('td', { className: 'mono dim', style: 'white-space:nowrap', text: c.created_at || '—' }),
      h('td', { className: 'mono dim', style: 'white-space:nowrap', text: c.updated_at || '—' }),
      h('td', {}, h('div', { className: 'actions' },
        h('button', { type: 'button', className: 'btn sm outlined', text: 'Query', disabled: !c.active, title: 'Open in Run SQL', onclick: function () {
          showTab('run'); selectRunConnection(name); $('run-sql').focus(); } }),
        h('button', { type: 'button', className: 'btn ghost sm', text: 'Edit', onclick: function () { openConnectionForm(name, c); } }),
        h('button', { type: 'button', className: 'btn ghost sm danger', text: 'Delete', onclick: function () { openDeleteConnectionForm(name); } }))));
  });
  box.appendChild(h('div', { style: 'overflow-x:auto' }, h('table', { className: 'grid' },
    h('thead', {}, h('tr', {}, ['Name', 'Type', 'Host', 'Database', 'User', 'Status', 'Usage', 'Created', 'Last modified', ''].map(function (t) { return h('th', { text: t }); }))),
    h('tbody', {}, rows))));
}
$('conn-filter').oninput = renderConnections;
function connectionOptions(select, includeBlank, blankText) {
  var current = select.value;
  clear(select);
  if (includeBlank) select.appendChild(h('option', { value: '', text: blankText }));
  Object.keys(connectionsCache).sort().forEach(function (name) {
    var c = connectionsCache[name];
    select.appendChild(h('option', { value: name, text: name + (c.active ? '' : ' (inactive)') }));
  });
  if (current && connectionsCache[current]) select.value = current;
}
// ---- Run SQL's connection picker: Type -> Host -> Connection (database), rather than one flat name list -
// with tens of connections sharing a host, "which of these is the one I want" is a type+host question first. ----
function runConnHostLabel(c) { return c.host ? c.host + (c.port ? ':' + c.port : '') : '(local file)'; }
function paintRunConnType() {
  var sel = $('run-conn-type');
  var current = sel.value;
  var types = Array.from(new Set(Object.keys(connectionsCache).map(function (n) { return connectionsCache[n].db; }))).sort();
  clear(sel).appendChild(h('option', { value: '', text: 'All types' }));
  types.forEach(function (t) { sel.appendChild(h('option', { value: t, text: t })); });
  sel.value = types.indexOf(current) !== -1 ? current : '';
}
function paintRunConnHost() {
  var sel = $('run-conn-host');
  var current = sel.value;
  var type = $('run-conn-type').value;
  var hosts = Array.from(new Set(Object.keys(connectionsCache)
    .filter(function (n) { return !type || connectionsCache[n].db === type; })
    .map(function (n) { return runConnHostLabel(connectionsCache[n]); }))).sort();
  clear(sel).appendChild(h('option', { value: '', text: 'All hosts' }));
  hosts.forEach(function (host) { sel.appendChild(h('option', { value: host, text: host })); });
  sel.value = hosts.indexOf(current) !== -1 ? current : '';
}
function paintRunConnection() {
  var select = $('run-connection');
  var current = select.value;
  var type = $('run-conn-type').value, host = $('run-conn-host').value;
  var names = Object.keys(connectionsCache).sort().filter(function (n) {
    var c = connectionsCache[n];
    return (!type || c.db === type) && (!host || runConnHostLabel(c) === host);
  });
  clear(select);
  names.forEach(function (name) {
    var c = connectionsCache[name];
    select.appendChild(h('option', { value: name, text: name + ' · ' + (c.database || '?') + (c.active ? '' : ' (inactive)') }));
  });
  if (names.indexOf(current) !== -1) select.value = current;
  else { var firstActive = names.filter(function (n) { return connectionsCache[n].active; })[0]; if (firstActive) select.value = firstActive; }
  runSchema.setConnection(select.value || '');
  paintRunDatabase();
  paintRunEditorMode();
}
// ---- Run SQL's own "browse a different database on this same server" picker - separate from the tree-based
// schema browser above, which always shows the connection's own configured database and is unaffected by it. ----
var dbListCache = {}; // connection name -> {status: 'loading'|'ready'|'error', databases, message}
async function loadDatabases(name, onDone) {
  dbListCache[name] = { status: 'loading' };
  var res, body = null;
  try { res = await apiFetch('connections/databases', { method: 'POST', json: { name: name } }); }
  catch (e) { dbListCache[name] = { status: 'error', message: 'Network error: ' + e.message }; onDone(); return; }
  try { body = await res.json(); } catch (e) {}
  dbListCache[name] = res.ok && body ? { status: 'ready', databases: body.databases }
    : { status: 'error', message: (body && body.error) || ('HTTP ' + res.status) };
  onDone();
}
function paintRunDatabase() {
  var sel = $('run-database');
  var connName = $('run-connection').value;
  clear(sel);
  if (!connName || !connectionsCache[connName]) {
    sel.appendChild(h('option', { value: '', text: '—' })); sel.disabled = true;
    runSchema.setDatabase(null); return;
  }
  var c = connectionsCache[connName];
  if (DB_SWITCHABLE_TYPES.indexOf(c.db) === -1) {
    // Nothing to switch to - sqlite/duckdb are a single file, h2/jdbc have no supported "list databases" query.
    sel.disabled = true;
    sel.appendChild(h('option', { value: c.database || '', text: c.database || '(default)' }));
    runSchema.setDatabase(null); return;
  }
  var entry = dbListCache[connName];
  if (!entry) { loadDatabases(connName, function () { if ($('run-connection').value === connName) paintRunDatabase(); }); entry = { status: 'loading' }; }
  if (entry.status === 'loading') { sel.disabled = true; sel.appendChild(h('option', { value: '', text: 'Loading…' })); return; }
  if (entry.status === 'error') { sel.disabled = true; sel.appendChild(h('option', { value: '', text: 'Unavailable' })); return; }
  sel.disabled = false;
  entry.databases.forEach(function (name) { sel.appendChild(h('option', { value: name, text: name })); });
  if (entry.databases.indexOf(c.database) !== -1) sel.value = c.database;
  // The tree gets an override only when it actually differs from the connection's own default.
  runSchema.setDatabase(sel.value !== c.database ? sel.value : null);
}
$('run-database').onchange = function () {
  var connName = $('run-connection').value, c = connectionsCache[connName];
  runSchema.setDatabase(c && $('run-database').value !== c.database ? $('run-database').value : null);
};
function populateConnectionSelect() {
  paintRunConnType();
  paintRunConnHost();
  paintRunConnection();
}
/** Jumps the Type/Host filters to wherever `name` actually lives, then selects it - so a shortcut into Run SQL
 * from elsewhere (the Connections table's "Query" button, run history, a schema preview) always lands on the
 * right connection instead of silently failing because the cascade happened to be filtered to something else. */
/** The editor holds SQL for every dialect except mongo (BACKLOG #36: find-only, one JSON textarea - see
 * runMongo()) - so its placeholder/label and the Explain button (no EXPLAIN equivalent yet) follow whichever
 * connection is currently selected. */
function paintRunEditorMode() {
  var c = connectionsCache[$('run-connection').value];
  var isMongo = !!c && c.db === 'mongo';
  var ta = $('run-sql');
  ta.placeholder = isMongo ? '{"collection": "orders", "filter": {"status": ":status"}}'
                            : 'SELECT * FROM t WHERE id = :id';
  ta.setAttribute('aria-label', isMongo ? 'Mongo find query (JSON)' : 'SQL');
  $('run-explain-button').hidden = isMongo;
}
function selectRunConnection(name) {
  var c = connectionsCache[name];
  if (!c) return;
  $('run-conn-type').value = c.db; paintRunConnHost();
  $('run-conn-host').value = runConnHostLabel(c); paintRunConnection();
  $('run-connection').value = name;
  runSchema.setConnection(name);
  paintRunDatabase();
  paintRunEditorMode();
}
$('run-conn-type').onchange = function () { paintRunConnHost(); paintRunConnection(); };
$('run-conn-host').onchange = function () { paintRunConnection(); };

// ---- API keys ----
// QUERYAPIGATE_API_KEY is a full-access admin key, unaffected by anything here. A scoped key created below is
// limited to the connections it's given (or every connection) and can be denied write access even when the
// server otherwise allows it - but only the admin key can reach this tab's endpoints at all, so a scoped
// key visiting /ui sees the same "couldn't load" state here as it does on Connections and Saved Queries.
var apiKeysCache = {};

async function loadApiKeys() {
  await loadCollections();
  var data = await apiJson('api_keys');
  var box = $('apikeys-table');
  if (!data) {
    apiKeysCache = {};
    $('count-apikeys').textContent = '';
    clear(box).appendChild(h('div', { className: 'empty' }, h('strong', { text: 'Couldn’t load API keys' }),
      h('span', { text: 'Only the admin key (QUERYAPIGATE_API_KEY) can manage API keys.' }),
      h('button', { type: 'button', className: 'btn sm', text: 'Retry', onclick: loadApiKeys })));
    return;
  }
  apiKeysCache = data.keys || {};
  renderApiKeys();
  if (Object.keys(rolesCache).length) renderRoles(); // the Keys column counts keys created from each role
  renderAccessMap();
}
function renderApiKeys() {
  var box = clear($('apikeys-table'));
  var names = Object.keys(apiKeysCache).sort();
  $('count-apikeys').textContent = names.length ? String(names.length) : '';
  $('apikeys-sub').textContent = (names.length ? names.length + (names.length === 1 ? ' key. ' : ' keys. ') : '') + 'Scope each one to connections, collections or roles.';
  if (!names.length) {
    box.appendChild(h('div', { className: 'empty' }, h('strong', { text: 'No scoped API keys yet' }),
      h('span', { text: 'QUERYAPIGATE_API_KEY is a full-access admin key. Create a scoped one to limit a caller to specific connections, read-only.' }),
      h('button', { type: 'button', className: 'btn primary', text: 'New API key', onclick: function () { openApiKeyForm(); } })));
    return;
  }
  var today = new Date().toISOString().slice(0, 10);
  var rows = names.map(function (name) {
    var k = apiKeysCache[name];
    var expired = k.expires_at && k.expires_at < today;
    var expiry = k.expires_at
      ? h('span', { style: expired ? 'color:var(--danger)' : '', text: k.expires_at + (expired ? ' (expired)' : '') })
      : h('span', { className: 'dim', text: 'never' });
    var ips = (k.allowed_ips || []).length
      ? h('span', { title: k.allowed_ips.join(', '), text: k.allowed_ips.length + (k.allowed_ips.length === 1 ? ' IP' : ' IPs') })
      : h('span', { className: 'dim', text: 'any' });
    return h('tr', { 'data-name': name },
      h('td', {}, h('div', { style: 'display:flex;align-items:center;gap:8px' },
        h('span', { className: 'dot ' + (k.active && !expired ? 'ok' : 'off'), title: !k.active ? 'Revoked' : (expired ? 'Expired' : 'Active') }),
        h('span', { className: 'name', title: k.created_at ? 'Created ' + k.created_at + (k.created_from_role ? ' from role ' + k.created_from_role : '') : null, text: name }))),
      h('td', {}, scopeNode(k)),
      accessCell(k),
      h('td', { className: k.rate_limit ? 'mono' : 'mono dim', style: 'white-space:nowrap', text: k.rate_limit || 'server default' }),
      h('td', { className: 'dim' }, ips),
      h('td', { style: 'white-space:nowrap' }, expiry),
      h('td', { className: 'mono dim', style: 'white-space:nowrap', text: k.last_used_at || 'never' }),
      usageCell(k.usage),
      h('td', {}, h('div', { className: 'actions' },
        h('button', { type: 'button', className: 'btn ghost sm', text: 'Edit', onclick: function () { openApiKeyForm(name, k); } }),
        h('button', { type: 'button', className: 'btn ghost sm danger', text: 'Revoke', onclick: function () { deleteApiKey(name); } }))));
  });
  box.appendChild(h('div', { style: 'overflow-x:auto' }, h('table', { className: 'grid' },
    h('thead', {}, h('tr', {}, ['Name', 'Scope', 'Access', 'Rate limit', 'IPs', 'Expires', 'Last used', 'Usage', ''].map(function (t) { return h('th', { text: t }); }))),
    h('tbody', {}, rows))));
}
/** One "Scope" cell for a key or role: its connections and its query / collection grants as quiet dashed chips. */
function scopeNode(entry) {
  var tags = [];
  if (entry.connections === '*') tags.push(h('span', { className: 'tag coll', text: 'all connections' }));
  else (entry.connections || []).forEach(function (c) { tags.push(h('span', { className: 'tag coll', text: 'conn: ' + c })); });
  queryGrantTags(entry).forEach(function (t) { tags.push(t); });
  return tags.length ? h('div', { className: 'tags scope' }, tags) : h('span', { className: 'dim', text: '—' });
}
function accessCell(entry) {
  var writeOps = entry.allow_writes && (entry.allowed_write_ops || []).length;
  if (!entry.allow_writes) return h('td', { className: 'dim', style: 'white-space:nowrap', text: 'Read-only' });
  if (writeOps) return h('td', { style: 'white-space:nowrap', title: entry.allowed_write_ops.join(', '),
    text: 'Read/write (' + entry.allowed_write_ops.length + (entry.allowed_write_ops.length === 1 ? ' op' : ' ops') + ')' });
  return h('td', { style: 'white-space:nowrap', text: 'Read/write' });
}

// ---- roles ----
// A role is a reusable *template* for the grant fields below - copied onto a key once, at
// "create key from role" time. Editing or deleting a role afterward never touches a key already
// created from it; see the "Create from" picker in openApiKeyForm.
var rolesCache = {};

async function loadRoles() {
  await loadCollections();
  var data = await apiJson('roles');
  var box = $('roles-table');
  if (!data) {
    rolesCache = {};
    $('count-roles').textContent = '';
    clear(box).appendChild(h('div', { className: 'empty' }, h('strong', { text: 'Couldn’t load roles' }),
      h('span', { text: 'Only the admin key (QUERYAPIGATE_API_KEY) can manage roles.' }),
      h('button', { type: 'button', className: 'btn sm', text: 'Retry', onclick: loadRoles })));
    return;
  }
  rolesCache = data.roles || {};
  renderRoles();
  if (selected.name) renderDetail(); // the accessBox's "also granted to role X" line needs rolesCache too
}
function renderRoles() {
  var box = clear($('roles-table'));
  var names = Object.keys(rolesCache).sort();
  $('count-roles').textContent = names.length ? String(names.length) : '';
  if (!names.length) {
    box.appendChild(h('div', { className: 'empty' }, h('strong', { text: 'No roles yet' }),
      h('span', { text: 'A role is a reusable template of connections, queries, write access, rate limit and allowed IPs — create an API key "from" a role instead of filling in every field by hand each time.' }),
      h('button', { type: 'button', className: 'btn primary', text: 'New role', onclick: function () { openRoleForm(); } })));
    return;
  }
  var keysFrom = {};
  Object.keys(apiKeysCache).forEach(function (kn) { var from = apiKeysCache[kn].created_from_role; if (from) keysFrom[from] = (keysFrom[from] || 0) + 1; });
  var rows = names.map(function (name) {
    var r = rolesCache[name];
    var conns = r.connections === '*' ? 'all connections' : ((r.connections || []).length ? r.connections.join(', ') : 'none');
    return h('tr', { 'data-name': name },
      h('td', {}, h('div', { style: 'display:flex;align-items:center;gap:8px' }, h('span', { className: 'name', text: name }), r.example ? exampleBadge() : null)),
      h('td', { className: 'dim', text: conns }),
      h('td', {}, h('div', { className: 'tags scope' }, queryGrantTags(r).length ? queryGrantTags(r) : [h('span', { className: 'tag coll', text: '—' })])),
      accessCell(r),
      h('td', { className: r.rate_limit ? 'mono' : 'mono dim', style: 'white-space:nowrap', text: r.rate_limit || 'server default' }),
      h('td', { className: 'mono', text: String(keysFrom[name] || 0), title: 'Keys created from this role' }),
      h('td', {}, h('div', { className: 'actions' },
        h('button', { type: 'button', className: 'btn ghost sm', text: 'New key from this', onclick: function () { openApiKeyForm(null, {}, name); } }),
        h('button', { type: 'button', className: 'btn ghost sm', text: 'Edit', onclick: function () { openRoleForm(name, r); } }),
        h('button', { type: 'button', className: 'btn ghost sm danger', text: 'Delete', onclick: function () { deleteRole(name); } }))));
  });
  box.appendChild(h('div', { style: 'overflow-x:auto' }, h('table', { className: 'grid' },
    h('thead', {}, h('tr', {}, ['Name', 'Connections', 'Queries / collections', 'Access', 'Rate limit', 'Keys', ''].map(function (t) { return h('th', { text: t }); }))),
    h('tbody', {}, rows))));
}

function openRoleForm(name, existing) {
  existing = existing || {};
  var isEdit = !!name;
  var slot = openDrawer('role-form-slot', isEdit ? 'Edit role' : 'New role', isEdit ? name : 'POST /roles');
  var nameInput = h('input', { id: 'r-name', value: name || '', placeholder: 'reporting', autocomplete: 'off', spellcheck: 'false', required: isEdit ? null : true });
  if (isEdit) nameInput.disabled = true;
  var connNames = Object.keys(connectionsCache).sort();
  var wildcard = existing.connections === undefined || existing.connections === '*';
  var allCheckbox = h('input', { id: 'r-all', type: 'checkbox' });
  allCheckbox.checked = wildcard;
  var connChecks = {};
  var connBox = h('div', { className: 'tags' }, connNames.length ? connNames.map(function (cname) {
    var cb = h('input', { type: 'checkbox' });
    cb.checked = !wildcard && (existing.connections || []).indexOf(cname) !== -1;
    connChecks[cname] = cb;
    return h('label', { className: 'switch', style: 'font-weight:400' }, cb, cname);
  }) : h('span', { className: 'hint', text: 'No connections exist yet — add one on the Connections tab first.' }));
  function paintConnMode() { connBox.style.opacity = allCheckbox.checked ? '0.4' : '1'; connBox.style.pointerEvents = allCheckbox.checked ? 'none' : 'auto'; }
  allCheckbox.onchange = paintConnMode;
  paintConnMode();
  var queryNames = filesCache.map(function (f) { return f.filename; }).sort();
  var queryWildcard = existing.queries === '*';
  var existingQueryEntries = queryWildcard ? [] : (existing.queries || []);
  var existingQueryNames = existingQueryEntries.map(function (e) { return typeof e === 'string' ? e : e.name; });
  var existingWriteQueries = {};
  existingQueryEntries.forEach(function (e) { if (e && typeof e === 'object' && e.allow_writes) existingWriteQueries[e.name] = true; });
  var allQueriesCheckbox = h('input', { id: 'r-all-queries', type: 'checkbox' });
  allQueriesCheckbox.checked = queryWildcard;
  var queryChecks = {}, queryWriteChecks = {};
  var queryBox = h('div', { className: 'tags' }, queryNames.length ? queryNames.map(function (qname) {
    var cb = h('input', { type: 'checkbox' });
    cb.checked = !queryWildcard && existingQueryNames.indexOf(qname) !== -1;
    queryChecks[qname] = cb;
    var writeCb = h('input', { type: 'checkbox', title: 'Allow a key created from this role to write through ' + qname + ' specifically' });
    writeCb.checked = !!existingWriteQueries[qname];
    writeCb.disabled = !cb.checked;
    cb.onchange = function () { writeCb.disabled = !cb.checked; if (!cb.checked) writeCb.checked = false; };
    queryWriteChecks[qname] = writeCb;
    return h('label', { className: 'switch', style: 'font-weight:400' }, cb, qname,
      h('span', { className: 'switch', style: 'font-weight:400;margin-left:6px' }, writeCb, 'write'));
  }) : h('span', { className: 'hint', text: 'No saved queries exist yet — add one on the Saved Queries tab first.' }));
  function paintQueryMode() { queryBox.style.opacity = allQueriesCheckbox.checked ? '0.4' : '1'; queryBox.style.pointerEvents = allQueriesCheckbox.checked ? 'none' : 'auto'; }
  allQueriesCheckbox.onchange = paintQueryMode;
  paintQueryMode();
  var collectionField = collectionGrantField(existing.collections);
  var writesCheckbox = h('input', { id: 'r-writes', type: 'checkbox' });
  writesCheckbox.checked = !!existing.allow_writes;
  var rateLimitInput = h('input', { id: 'r-rate-limit', value: existing.rate_limit || '', placeholder: 'e.g. 100/minute', autocomplete: 'off', spellcheck: 'false' });
  var allowedIpsInput = h('input', { id: 'r-allowed-ips', value: (existing.allowed_ips || []).join(', '), placeholder: 'e.g. 203.0.113.5, 10.0.0.0/8', autocomplete: 'off', spellcheck: 'false' });
  var allowedWriteOpsInput = h('input', { id: 'r-allowed-write-ops', value: (existing.allowed_write_ops || []).join(', '), placeholder: 'e.g. insert, update', autocomplete: 'off', spellcheck: 'false' });
  var actions = formActions(isEdit ? 'Save' : 'Create role', closeDrawer);
  var form = h('form', { className: 'form', novalidate: true, onsubmit: function (e) {
    e.preventDefault();
    var connections = allCheckbox.checked ? '*' : Object.keys(connChecks).filter(function (c) { return connChecks[c].checked; });
    var queries = allQueriesCheckbox.checked ? '*' : Object.keys(queryChecks).filter(function (q) { return queryChecks[q].checked; })
      .map(function (q) { return queryWriteChecks[q].checked ? { name: q, allow_writes: true } : q; });
    var rateLimit = rateLimitInput.value.trim() || null;
    var allowedIps = allowedIpsInput.value.split(',').map(function (v) { return v.trim(); }).filter(Boolean);
    if (!allowedIps.length) allowedIps = null;
    var allowedWriteOps = allowedWriteOpsInput.value.split(',').map(function (v) { return v.trim().toLowerCase(); }).filter(Boolean);
    if (!allowedWriteOps.length) allowedWriteOps = null;
    var payload = { connections: connections, allow_writes: writesCheckbox.checked, queries: queries, collections: collectionField.value(), rate_limit: rateLimit, allowed_ips: allowedIps, allowed_write_ops: allowedWriteOps };
    if (isEdit) updateRole(name, payload, actions.submit);
    else createRole(nameInput.value.trim(), payload, actions.submit);
  } },
    field('r-name', 'Name', nameInput, isEdit ? null : 'Letters, digits, spaces, “.”, “_” and “-”.'),
    h('label', { className: 'switch' }, allCheckbox, 'All connections'),
    h('div', { className: 'field' }, h('label', {}, 'Allowed connections'), connBox),
    h('label', { className: 'switch' }, allQueriesCheckbox, 'All saved queries'),
    h('div', { className: 'field' }, h('label', {}, 'Additional saved-query access'), queryBox),
    collectionField.node,
    h('label', { className: 'switch' }, writesCheckbox, 'Allow writes', h('span', { className: 'hint', text: '— still capped by QUERYAPIGATE_ALLOW_WRITES' })),
    field('r-allowed-write-ops', 'Allowed write operations', allowedWriteOpsInput, 'Optional, comma-separated SQL keywords — e.g. "insert, update". Only takes effect when Allow writes is on.'),
    field('r-rate-limit', 'Rate limit', rateLimitInput, 'Optional — e.g. "100/minute". Leave blank for no limit of its own.'),
    field('r-allowed-ips', 'Allowed IPs', allowedIpsInput, 'Optional, comma-separated IP addresses or CIDR ranges. Leave blank to allow any address.'),
    h('div', { className: 'hint', text: 'A role is a template: it’s copied onto a key once, when the key is created "from" it. Editing or deleting this role afterward never changes a key already created from it.' }),
    actions.node);
  slot.appendChild(form);
  nameInput.focus();
}
$('new-role').onclick = function () { openRoleForm(); };

async function createRole(name, payload, btn) {
  if (!name) { showError('A role name is required.', { errors: { name: 'This field is required' } }); $('r-name').focus(); return; }
  btn.disabled = true;
  var res = await apiJson('roles', { method: 'POST', json: Object.assign({ name: name }, payload) });
  btn.disabled = false;
  if (res) { showError(''); closeDrawer(); toast('Created role ' + name); loadRoles(); }
}
async function updateRole(name, payload, btn) {
  btn.disabled = true;
  var res = await apiJson('roles/' + enc(name), { method: 'PATCH', json: payload });
  btn.disabled = false;
  if (res) { showError(''); closeDrawer(); toast('Updated ' + name); loadRoles(); }
}
async function deleteRole(name) {
  if (!confirm("Delete role '" + name + "'? Keys already created from it are unaffected — this only removes the template.")) return;
  var res = await apiJson('roles/' + enc(name), { method: 'DELETE' });
  if (res) { toast('Deleted role ' + name); loadRoles(); }
}

// ---- audit log ----
var auditLogCache = [];

async function loadAuditLog() {
  var box = $('auditlog-table');
  var data = await apiJson('audit_log');
  if (!data) {
    $('auditlog-count').textContent = '';
    clear(box).appendChild(h('div', { className: 'empty' }, h('strong', { text: 'Couldn’t load the audit log' }),
      h('span', { text: 'Only the admin key (QUERYAPIGATE_API_KEY) can view the audit log.' }),
      h('button', { type: 'button', className: 'btn sm', text: 'Retry', onclick: loadAuditLog })));
    return;
  }
  auditLogCache = data.entries || [];
  var actionSelect = $('auditlog-action-filter');
  var currentAction = actionSelect.value;
  var actions = Array.from(new Set(auditLogCache.map(function (e) { return e.action; }).filter(Boolean))).sort();
  clear(actionSelect);
  actionSelect.appendChild(h('option', { value: '', text: 'All actions' }));
  actions.forEach(function (a) { actionSelect.appendChild(h('option', { value: a, text: a })); });
  if (actions.indexOf(currentAction) !== -1) actionSelect.value = currentAction;
  renderAuditLog();
  renderConnections(); // the Connections screen's Deleted tab reads auditLogCache too
}
$('refresh-auditlog').onclick = loadAuditLog;
$('auditlog-action-filter').onchange = renderAuditLog;
$('auditlog-filter').oninput = renderAuditLog;

function auditLimit() {
  var rows = [];
  (settingsData || []).forEach(function (sec) { rows = rows.concat(sec.rows); });
  var row = rows.filter(function (r) { return r.env === 'QUERYAPIGATE_AUDIT_LOG_LIMIT'; })[0];
  return row ? parseInt(row.value, 10) || 500 : 500;
}
function paintAuditSub() {
  $('auditlog-sub').textContent = 'Administrative actions, newest first. Keeps the last ' + auditLimit() + ' entries.';
}
function auditFiltered() {
  var action = $('auditlog-action-filter').value;
  var q = $('auditlog-filter').value.trim().toLowerCase();
  return auditLogCache.filter(function (e) {
    if (action && e.action !== action) return false;
    if (!q) return true;
    return [e.timestamp, e.actor, e.target].join(' ').toLowerCase().indexOf(q) !== -1;
  });
}
/** create-ish actions read green, delete-ish red, everything else neutral - the same three tones as the design. */
function auditTone(action) {
  if (/^(create|load|save)/.test(action || '')) return 'ok';
  if (/^(delete|unload|revoke)/.test(action || '')) return 'bad';
  return '';
}
$('export-auditlog').onclick = function () {
  var entries = auditFiltered();
  downloadBlob(new Blob([JSON.stringify(entries, null, 2) + '\n'], { type: 'application/json' }), 'queryapigate-audit-log.json');
  toast('Exported ' + entries.length + (entries.length === 1 ? ' entry' : ' entries'));
};
function renderAuditLog() {
  var box = clear($('auditlog-table'));
  paintAuditSub();
  if (!auditLogCache.length) {
    $('auditlog-count').textContent = '';
    box.appendChild(h('div', { className: 'empty' }, h('strong', { text: 'No administrative changes recorded yet' }),
      h('span', { text: 'Creating or changing a connection, saved query or API key will appear here.' })));
    return;
  }
  var entries = auditFiltered();
  $('auditlog-count').textContent = entries.length === auditLogCache.length
    ? auditLogCache.length + (auditLogCache.length === 1 ? ' entry' : ' entries')
    : entries.length + ' of ' + auditLogCache.length + ' entries';
  if (!entries.length) {
    box.appendChild(h('div', { className: 'empty' }, h('span', { text: 'No entries match this filter.' })));
    return;
  }
  var rows = entries.map(function (e) {
    return h('tr', {},
      h('td', { className: 'mono dim', style: 'white-space:nowrap', text: e.timestamp || '' }),
      h('td', { className: 'mono', text: e.actor || '-' }),
      h('td', {}, h('span', { className: 'tag act ' + auditTone(e.action), text: e.action || '' })),
      h('td', {}, h('span', { className: 'name', text: e.target || '' })),
      h('td', {}, renderAuditChanges(e.changes)));
  });
  box.appendChild(h('div', { style: 'overflow-x:auto' }, h('table', { className: 'grid' },
    h('thead', {}, h('tr', {}, ['Time', 'Actor', 'Action', 'Target', 'Changes'].map(function (t) { return h('th', { text: t }); }))),
    h('tbody', {}, rows))));
}

function auditValueText(v) {
  if (v === null || v === undefined || v === '') return 'none';
  if (Array.isArray(v)) return v.length ? v.join(', ') : 'none';
  return String(v);
}

/** true for a value renderAuditChanges() should skip in a full snapshot (create/delete) - unset/empty, not
 * a real value like false or 0. A diff entry (an update) never has these in the first place - _dict_diff()
 * only ever includes a field that actually changed - so this only thins out snapshots. */
function auditValueIsEmpty(v) {
  return v === null || v === undefined || v === '' || (Array.isArray(v) && !v.length);
}

function renderAuditChanges(changes) {
  var keys = changes && typeof changes === 'object' ? Object.keys(changes).sort() : [];
  var shown = keys.filter(function (key) {
    var v = changes[key];
    var isDiff = v && typeof v === 'object' && !Array.isArray(v) && ('from' in v || 'to' in v);
    return isDiff || !auditValueIsEmpty(v);
  });
  if (!shown.length) return h('span', { className: 'dim', text: keys.length ? 'no other fields set' : '—' });
  return h('div', {}, shown.map(function (key) {
    var v = changes[key];
    var text = (v && typeof v === 'object' && !Array.isArray(v) && ('from' in v || 'to' in v))
      ? key + ': ' + auditValueText(v.from) + ' → ' + auditValueText(v.to)
      : key + ': ' + auditValueText(v);
    return h('div', { className: 'mono', style: 'font-size:12px;color:var(--ink-2)', text: text });
  }));
}

// ---- metrics (a live snapshot of /metrics, parsed client-side - counters and gauges only, this process's
// own in-memory numbers since it started, deliberately no history or trends; see the tab's own subtitle
// and documentation/API.md#observability for why that stays Prometheus/Grafana's job, not this page's) ----
function parseMetricsText(text) {
  var series = [];
  text.split('\n').forEach(function (line) {
    if (!line || line.charAt(0) === '#') return;
    var m = line.match(/^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{([^}]*)\})?\s+([0-9eE+\-.]+)\s*$/);
    if (!m) return;
    var labels = {};
    (m[3] || '').replace(/([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"/g, function (_, k, v) {
      labels[k] = v.replace(/\\"/g, '"').replace(/\\\\/g, '\\');
      return '';
    });
    series.push({ name: m[1], labels: labels, value: Number(m[4]) });
  });
  return series;
}
function metricSum(series, name, filter) {
  return series.filter(function (s) { return s.name === name && (!filter || filter(s.labels)); })
    .reduce(function (a, s) { return a + s.value; }, 0);
}
function metricGauge(series, name) {
  var s = series.filter(function (s2) { return s2.name === name; })[0];
  return s ? s.value : 0;
}
function metricGroupSum(series, name, labelKey) {
  var totals = {};
  series.filter(function (s) { return s.name === name; }).forEach(function (s) {
    var key = s.labels[labelKey] || '(none)';
    totals[key] = (totals[key] || 0) + s.value;
  });
  return totals;
}

async function loadMetrics() {
  var box = $('metrics-body');
  var res;
  try { res = await apiFetch('metrics'); } catch (e) { res = null; }
  if (!res || !res.ok) {
    clear(box).appendChild(h('div', { className: 'empty' }, h('strong', { text: 'Couldn’t load metrics' }),
      h('button', { type: 'button', className: 'btn sm', text: 'Retry', onclick: loadMetrics })));
    return;
  }
  renderMetrics(parseMetricsText(await res.text()));
  metricsAt = Date.now();
  paintMetricsAge();
}
$('refresh-metrics').onclick = loadMetrics;

function statTile(label, value, warn) {
  return h('div', { className: 'stat-tile' },
    h('div', { className: 'label', text: label }),
    h('div', { className: 'value' + (warn ? ' warn' : ''), text: String(value) }));
}
/** One card of horizontal bars: label, a track filled in proportion to the largest value, and the number. */
function barCard(title, rows, colorOf) {
  var max = rows.reduce(function (m, r) { return Math.max(m, r.value); }, 0);
  return h('div', { className: 'chart-card' }, h('h3', { text: title }), rows.map(function (r) {
    var width = r.value ? Math.max(1, 100 * r.value / max) : 0;
    return h('div', { className: 'bar-row' }, h('span', { className: 'bl', text: r.label }),
      h('div', { className: 'bar-track' }, h('div', { className: 'bar-fill', style: 'width:' + width + '%;background:' + colorOf(r) })),
      h('span', { className: 'bv', text: String(r.value) }));
  }));
}
var metricsAt = null;
function paintMetricsAge() {
  if (metricsAt === null) { $('metrics-updated').textContent = ''; return; }
  var secs = Math.round((Date.now() - metricsAt) / 1000);
  $('metrics-updated').textContent = secs < 5 ? 'Updated just now' : 'Updated ' + (secs < 90 ? secs + 's' : Math.round(secs / 60) + ' min') + ' ago';
}
setInterval(paintMetricsAge, 5000);

function renderMetrics(series) {
  var box = clear($('metrics-body'));
  var totalRequests = metricSum(series, 'queryapigate_requests_total');
  var byStatusClass = {};
  series.filter(function (s) { return s.name === 'queryapigate_requests_total'; }).forEach(function (s) {
    var cls = (s.labels.status || '?').charAt(0) + 'xx';
    byStatusClass[cls] = (byStatusClass[cls] || 0) + s.value;
  });
  var errorCount = Object.keys(byStatusClass).filter(function (c) { return c === '4xx' || c === '5xx'; })
    .reduce(function (a, c) { return a + byStatusClass[c]; }, 0);
  var errorRate = totalRequests ? (100 * errorCount / totalRequests) : 0;

  box.appendChild(h('div', { className: 'stat-tiles' },
    statTile('Requests', totalRequests),
    statTile('Error rate', errorRate.toFixed(1) + '%', errorRate >= 5),
    statTile('Active queries', metricGauge(series, 'queryapigate_active_queries')),
    statTile('Pool idle connections', metricGauge(series, 'queryapigate_pool_idle_connections')),
    statTile('Rate limit rejections', metricGauge(series, 'queryapigate_rate_limit_rejections_total')),
    statTile('Rows returned', metricSum(series, 'queryapigate_rows_returned_total'))));

  var statusRows = ['2xx', '3xx', '4xx', '5xx'].map(function (c) { return { label: c, value: byStatusClass[c] || 0 }; })
    .filter(function (r) { return r.value > 0; });
  var byConnection = metricGroupSum(series, 'queryapigate_queries_total', 'connection');
  var connNamesAll = Array.from(new Set(Object.keys(connectionsCache).concat(Object.keys(byConnection)))).sort();
  var connRows = connNamesAll.map(function (c) { return { label: c, value: byConnection[c] || 0 }; });
  var statusColor = { '2xx': 'var(--accent)', '3xx': 'var(--accent)', '4xx': 'var(--warn)', '5xx': 'var(--danger)' };

  box.appendChild(h('div', { className: 'metrics-charts' },
    statusRows.length ? barCard('Requests by status', statusRows, function (r) { return statusColor[r.label]; }) : null,
    connRows.length ? barCard('Queries by connection', connRows, function () { return 'var(--accent)'; }) : null));

  var errorsByConnection = {};
  series.filter(function (s) { return s.name === 'queryapigate_queries_total' && s.labels.status === 'error'; })
    .forEach(function (s) { var c = s.labels.connection || '?'; errorsByConnection[c] = (errorsByConnection[c] || 0) + s.value; });
  var rowsByConnection = metricGroupSum(series, 'queryapigate_rows_returned_total', 'connection');
  var sumByConn = {}, countByConn = {};
  series.filter(function (s) { return s.name === 'queryapigate_query_duration_seconds_sum'; })
    .forEach(function (s) { sumByConn[s.labels.connection] = (sumByConn[s.labels.connection] || 0) + s.value; });
  series.filter(function (s) { return s.name === 'queryapigate_query_duration_seconds_count'; })
    .forEach(function (s) { countByConn[s.labels.connection] = (countByConn[s.labels.connection] || 0) + s.value; });
  var connNames = Object.keys(byConnection).sort();
  if (connNames.length) {
    var tRows = connNames.map(function (c) {
      var count = countByConn[c] || 0;
      var avgMs = count ? Math.round(1000 * (sumByConn[c] || 0) / count) : null;
      return h('tr', {},
        h('td', {}, h('span', { className: 'name', text: c })),
        h('td', { className: 'mono num', text: String(byConnection[c] || 0) }),
        h('td', { className: 'mono num', text: String(errorsByConnection[c] || 0) }),
        h('td', { className: 'mono num', text: avgMs === null ? '—' : avgMs + ' ms' }),
        h('td', { className: 'mono num', text: String(rowsByConnection[c] || 0) }));
    });
    box.appendChild(h('div', { className: 'panel', style: 'overflow-x:auto' }, h('table', { className: 'grid' },
      h('thead', {}, h('tr', {}, ['Connection', 'Queries', 'Errors', 'Avg latency', 'Rows returned'].map(function (t, i) { return h('th', { className: i ? 'num' : '', text: t }); }))),
      h('tbody', {}, tRows))));
  }
}

function openApiKeyForm(name, existing, fromRole) {
  existing = existing || {};
  var isEdit = !!name;
  var slot = openDrawer('apikey-form-slot', isEdit ? 'Edit API key' : 'New API key', isEdit ? name : 'POST /api_keys');
  var nameInput = h('input', { id: 'k-name', value: name || '', placeholder: 'reporting', autocomplete: 'off', spellcheck: 'false', required: isEdit ? null : true });
  if (isEdit) nameInput.disabled = true;
  var roleNames = Object.keys(rolesCache).sort();
  var roleSelect = null, grantFields = null;
  if (!isEdit && roleNames.length) {
    roleSelect = h('select', { id: 'k-role' }, h('option', { value: '', text: 'Custom (choose grants below)' }),
      roleNames.map(function (r) { return h('option', { value: r, text: r }); }));
    if (fromRole) roleSelect.value = fromRole;
  }
  var connNames = Object.keys(connectionsCache).sort();
  var wildcard = existing.connections === undefined || existing.connections === '*';
  var allCheckbox = h('input', { id: 'k-all', type: 'checkbox' });
  allCheckbox.checked = wildcard;
  var connChecks = {};
  var connBox = h('div', { className: 'tags' }, connNames.length ? connNames.map(function (cname) {
    var cb = h('input', { type: 'checkbox' });
    cb.checked = !wildcard && (existing.connections || []).indexOf(cname) !== -1;
    connChecks[cname] = cb;
    return h('label', { className: 'switch', style: 'font-weight:400' }, cb, cname);
  }) : h('span', { className: 'hint', text: 'No connections exist yet — add one on the Connections tab first.' }));
  function paintConnMode() { connBox.style.opacity = allCheckbox.checked ? '0.4' : '1'; connBox.style.pointerEvents = allCheckbox.checked ? 'none' : 'auto'; }
  allCheckbox.onchange = paintConnMode;
  paintConnMode();
  var queryNames = filesCache.map(function (f) { return f.filename; }).sort();
  var queryWildcard = existing.queries === '*';
  var existingQueryEntries = queryWildcard ? [] : (existing.queries || []);
  var existingQueryNames = existingQueryEntries.map(function (e) { return typeof e === 'string' ? e : e.name; });
  var existingWriteQueries = {};
  existingQueryEntries.forEach(function (e) { if (e && typeof e === 'object' && e.allow_writes) existingWriteQueries[e.name] = true; });
  var allQueriesCheckbox = h('input', { id: 'k-all-queries', type: 'checkbox' });
  allQueriesCheckbox.checked = queryWildcard;
  var queryChecks = {}, queryWriteChecks = {};
  var queryBox = h('div', { className: 'tags' }, queryNames.length ? queryNames.map(function (qname) {
    var cb = h('input', { type: 'checkbox' });
    cb.checked = !queryWildcard && existingQueryNames.indexOf(qname) !== -1;
    queryChecks[qname] = cb;
    var writeCb = h('input', { type: 'checkbox', title: 'Allow this key to write through ' + qname + ' specifically' });
    writeCb.checked = !!existingWriteQueries[qname];
    writeCb.disabled = !cb.checked;
    cb.onchange = function () { writeCb.disabled = !cb.checked; if (!cb.checked) writeCb.checked = false; };
    queryWriteChecks[qname] = writeCb;
    return h('label', { className: 'switch', style: 'font-weight:400' }, cb, qname,
      h('span', { className: 'switch', style: 'font-weight:400;margin-left:6px' }, writeCb, 'write'));
  }) : h('span', { className: 'hint', text: 'No saved queries exist yet — add one on the Saved Queries tab first.' }));
  function paintQueryMode() { queryBox.style.opacity = allQueriesCheckbox.checked ? '0.4' : '1'; queryBox.style.pointerEvents = allQueriesCheckbox.checked ? 'none' : 'auto'; }
  allQueriesCheckbox.onchange = paintQueryMode;
  paintQueryMode();
  var collectionField = collectionGrantField(existing.collections);
  var writesCheckbox = h('input', { id: 'k-writes', type: 'checkbox' });
  writesCheckbox.checked = !!existing.allow_writes;
  var expiresInput = h('input', { id: 'k-expires', type: 'date', value: existing.expires_at || '' });
  var rateLimitInput = h('input', { id: 'k-rate-limit', value: existing.rate_limit || '', placeholder: 'e.g. 100/minute', autocomplete: 'off', spellcheck: 'false' });
  var allowedIpsInput = h('input', { id: 'k-allowed-ips', value: (existing.allowed_ips || []).join(', '), placeholder: 'e.g. 203.0.113.5, 10.0.0.0/8', autocomplete: 'off', spellcheck: 'false' });
  var allowedWriteOpsInput = h('input', { id: 'k-allowed-write-ops', value: (existing.allowed_write_ops || []).join(', '), placeholder: 'e.g. insert, update', autocomplete: 'off', spellcheck: 'false' });
  var activeCheckbox = isEdit ? h('input', { id: 'k-active', type: 'checkbox' }) : null;
  if (activeCheckbox) activeCheckbox.checked = existing.active !== false;
  grantFields = h('div', {},
    h('label', { className: 'switch' }, allCheckbox, 'All connections'),
    h('div', { className: 'field' }, h('label', {}, 'Allowed connections'), connBox),
    h('label', { className: 'switch' }, allQueriesCheckbox, 'All saved queries'),
    h('div', { className: 'field' }, h('label', {}, 'Additional saved-query access'), queryBox,
      h('span', { className: 'hint', text: '— independent of connections above: runnable by name even without connection access, for an external client that should only see its own approved queries. Check "write" on a query to let this key write through it specifically, even with no blanket write access — "All saved queries" above can never carry write access, only a specific enumerated list can.' })),
    collectionField.node,
    h('label', { className: 'switch' }, writesCheckbox, 'Allow writes', h('span', { className: 'hint', text: '— still capped by QUERYAPIGATE_ALLOW_WRITES' })),
    field('k-allowed-write-ops', 'Allowed write operations', allowedWriteOpsInput, 'Optional, comma-separated SQL keywords — e.g. "insert, update". Only takes effect when Allow writes is on; narrows which write statements this key may perform. Leave blank to allow any write.'),
    field('k-rate-limit', 'Rate limit', rateLimitInput, 'Optional, this key only — e.g. "100/minute". Checked in addition to QUERYAPIGATE_RATE_LIMIT, not instead of it. Leave blank for no limit of its own.'),
    field('k-allowed-ips', 'Allowed IPs', allowedIpsInput, 'Optional, comma-separated IP addresses or CIDR ranges — e.g. "203.0.113.5, 10.0.0.0/8". Leave blank to allow any address.'));
  function paintRoleMode() {
    var picked = roleSelect && roleSelect.value;
    grantFields.style.opacity = picked ? '0.4' : '1';
    grantFields.style.pointerEvents = picked ? 'none' : 'auto';
  }
  if (roleSelect) { roleSelect.onchange = paintRoleMode; paintRoleMode(); }
  var actions = formActions(isEdit ? 'Save' : 'Create key', closeDrawer);
  var form = h('form', { className: 'form', novalidate: true, onsubmit: function (e) {
    e.preventDefault();
    var expiresAt = expiresInput.value || null;
    var pickedRole = roleSelect && roleSelect.value;
    if (!isEdit && pickedRole) { createApiKey({ name: nameInput.value.trim(), role: pickedRole, expires_at: expiresAt }, actions.submit); return; }
    var connections = allCheckbox.checked ? '*' : Object.keys(connChecks).filter(function (c) { return connChecks[c].checked; });
    var queries = allQueriesCheckbox.checked ? '*' : Object.keys(queryChecks).filter(function (q) { return queryChecks[q].checked; })
      .map(function (q) { return queryWriteChecks[q].checked ? { name: q, allow_writes: true } : q; });
    var rateLimit = rateLimitInput.value.trim() || null;
    var allowedIps = allowedIpsInput.value.split(',').map(function (v) { return v.trim(); }).filter(Boolean);
    if (!allowedIps.length) allowedIps = null;
    var allowedWriteOps = allowedWriteOpsInput.value.split(',').map(function (v) { return v.trim().toLowerCase(); }).filter(Boolean);
    if (!allowedWriteOps.length) allowedWriteOps = null;
    if (isEdit) updateApiKey(name, { connections: connections, allow_writes: writesCheckbox.checked, active: activeCheckbox.checked, queries: queries, collections: collectionField.value(), expires_at: expiresAt, rate_limit: rateLimit, allowed_ips: allowedIps, allowed_write_ops: allowedWriteOps }, actions.submit);
    else createApiKey({ name: nameInput.value.trim(), connections: connections, allow_writes: writesCheckbox.checked, queries: queries, collections: collectionField.value(), expires_at: expiresAt, rate_limit: rateLimit, allowed_ips: allowedIps, allowed_write_ops: allowedWriteOps }, actions.submit);
  } },
    field('k-name', 'Name', nameInput, isEdit ? null : 'Letters, digits, spaces, “.”, “_” and “-”.'),
    roleSelect ? field('k-role', 'Create from', roleSelect, 'Optional — copies that role’s connections, queries, collections, write access, rate limit and allowed IPs onto this key once, at creation. Editing or deleting the role afterward never changes this key.') : null,
    grantFields,
    field('k-expires', 'Expires', expiresInput, 'Optional — valid through the end of this date. Leave blank for no expiry.'),
    activeCheckbox ? h('label', { className: 'switch' }, activeCheckbox, 'Active', h('span', { className: 'hint', text: '— unchecking revokes it immediately' })) : null,
    actions.node);
  slot.appendChild(form);
  nameInput.focus();
}
$('new-apikey').onclick = function () { openApiKeyForm(); };

async function createApiKey(payload, btn) {
  if (!payload.name) { showError('An API key name is required.', { errors: { name: 'This field is required' } }); $('k-name').focus(); return; }
  btn.disabled = true;
  var res = await apiJson('api_keys', { method: 'POST', json: payload });
  btn.disabled = false;
  if (res) { showError(''); revealApiKey(res.name, res.key); loadApiKeys(); }
}
async function updateApiKey(name, patch, btn) {
  btn.disabled = true;
  var res = await apiJson('api_keys/' + enc(name), { method: 'PATCH', json: patch });
  btn.disabled = false;
  if (res) { showError(''); closeDrawer(); toast('Updated ' + name); loadApiKeys(); }
}
async function deleteApiKey(name) {
  if (!confirm("Revoke API key '" + name + "'? Anything still using it will stop working immediately.")) return;
  var res = await apiJson('api_keys/' + enc(name), { method: 'DELETE' });
  if (res) { toast('Revoked ' + name); loadApiKeys(); }
}
/** Shown once, right after creation - the secret is never retrievable again after this. */
function revealApiKey(name, secret) {
  clear($('apikey-form-slot'));
  $('drawer-title').textContent = 'API key created';
  $('drawer-kicker').textContent = name;
  var input = h('input', { readonly: true, className: 'mono', onclick: function () { input.select(); } });
  input.value = secret;
  $('apikey-form-slot').appendChild(h('div', { className: 'form' },
    h('div', { className: 'field' }, h('label', {}, 'Secret key'), input,
      h('div', { className: 'hint', text: 'Store this now — it cannot be shown again. To rotate it, revoke this key and create a new one.' })),
    h('div', { className: 'form-actions' },
      h('button', { type: 'button', className: 'btn ghost', text: 'Copy', onclick: function () { copyText(secret); } }),
      h('button', { type: 'button', className: 'btn primary', text: 'Done', onclick: closeDrawer }))));
}

// ---- collections ----
// A collection is one named group a saved query belongs to, stored on the query file itself (so it cannot
// disagree with it). A key's `collections` grant is LIVE - it reaches whatever is in the collection now and
// whatever is filed there later - which is why moving a query shows who gains and loses access first.
var collectionsCache = { collections: {}, uncollected: [] };
var collectionsLoading = null;
// Saved Queries' own Collections/Queries split, same idea as schemaBrowser()'s Tables/Columns: browse
// collections, drill into one to see its queries, rather than an expand/collapse tree per collection.
var queriesView = 'collections', queriesActiveCollection = null;
function loadCollections() {
  if (!collectionsLoading) {
    collectionsLoading = apiJson('collections').then(function (data) {
      collectionsCache = data && data.collections ? data : { collections: {}, uncollected: [] };
    }).then(function () { collectionsLoading = null; }, function () { collectionsLoading = null; });
  }
  return collectionsLoading;
}
function collectionNames() { return Object.keys(collectionsCache.collections).sort(); }
function accessImpact(from, to) {
  function reaching(c, kind) { var e = c ? collectionsCache.collections[c] : null; return e ? e[kind] : []; }
  function minus(a, b) { return a.filter(function (x) { return b.indexOf(x) === -1; }); }
  return { keysGain: minus(reaching(to, 'keys'), reaching(from, 'keys')), keysLose: minus(reaching(from, 'keys'), reaching(to, 'keys')),
           rolesGain: minus(reaching(to, 'roles'), reaching(from, 'roles')), rolesLose: minus(reaching(from, 'roles'), reaching(to, 'roles')) };
}
/** The who-gains-who-loses summary shown before queries move (or are first filed) - the same arithmetic the server
 * records in the audit log. `moves` is a list of [fromCollection, toCollection]; one entry for a single query. */
function impactNodeMany(moves) {
  var box = h('div', { className: 'impact' });
  var acc = { keysGain: [], keysLose: [], rolesGain: [], rolesLose: [] };
  moves.forEach(function (m) {
    var i = accessImpact(m[0] || null, m[1] || null);
    Object.keys(acc).forEach(function (k) { i[k].forEach(function (x) { if (acc[k].indexOf(x) === -1) acc[k].push(x); }); });
  });
  var what = moves.length === 1 ? 'this query' : 'some of these queries';
  var lines = [];
  if (acc.keysGain.length) lines.push(['Keys that will gain access to ' + what + ': ', acc.keysGain.join(', '), true]);
  if (acc.keysLose.length) lines.push(['Keys that will lose access to ' + (moves.length === 1 ? 'it' : 'some of them') + ': ', acc.keysLose.join(', '), true]);
  if (acc.rolesGain.length) lines.push(['Roles that will include ' + (moves.length === 1 ? 'it' : 'them') + ' (affects only keys created from them later): ', acc.rolesGain.join(', '), false]);
  if (acc.rolesLose.length) lines.push(['Roles that will no longer include ' + (moves.length === 1 ? 'it' : 'some of them') + ' (keys already created from them are unaffected): ', acc.rolesLose.join(', '), false]);
  if (!lines.length) box.appendChild(h('span', { className: 'hint', text: 'No key’s access changes.' }));
  lines.forEach(function (l) {
    box.appendChild(h('div', { className: 'hint', style: l[2] ? 'color:var(--warn)' : null }, l[0], h('strong', { text: l[1] })));
  });
  return box;
}
function impactNode(from, to) { return impactNodeMany([[from, to]]); }
/** Query names plus collection grants for a key/role row - one renderer for both tables, so they cannot drift apart. */
function queryGrantTags(entry) {
  var tags = [];
  if (entry.queries === '*') tags.push(h('span', { className: 'tag', text: 'all queries' }));
  else (entry.queries || []).forEach(function (q) {
    var writable = q && typeof q === 'object' && q.allow_writes;
    var qname = writable ? q.name : q;
    tags.push(h('span', { className: 'tag', title: writable ? qname + ' — write access' : null, text: qname + (writable ? ' (write)' : '') }));
  });
  (entry.collections || []).forEach(function (c) {
    var known = collectionsCache.collections[c];
    var n = known ? known.queries.length : 0;
    tags.push(h('span', { className: 'tag coll', title: 'Every query in collection ' + c + ' (' + n + ' now, including any added later) — read-only', text: c + '/' }));
  });
  return tags;
}
function queryGrantNode(entry) {
  var tags = queryGrantTags(entry);
  return tags.length ? h('div', { className: 'tags' }, tags) : h('span', { className: 'dim', text: '—' });
}
/** Every way `entry` (an API key or role) can reach a specific saved query - additive, so more than one can
 * apply at once (e.g. both a collection grant and a connection grant). Mirrors apikeys.can_run_saved(): a
 * named `queries` grant, a `collections` grant covering the query's collection, or a `connections` grant
 * covering the whole connection it runs on (which reaches every query on that connection, this one included). */
function reachVia(entry, queryName, collection, connectionName) {
  var via = [];
  if (entry.queries === '*') via.push({ label: 'all queries', kind: 'query' });
  else if ((entry.queries || []).some(function (q) { return (q && typeof q === 'object' ? q.name : q) === queryName; })) {
    via.push({ label: 'named', kind: 'query' });
  }
  if (collection && (entry.collections || []).indexOf(collection) !== -1) via.push({ label: 'collection ' + collection, kind: 'collection' });
  if (entry.connections === '*') via.push({ label: 'all connections', kind: 'connection' });
  else if (connectionName && (entry.connections || []).indexOf(connectionName) !== -1) via.push({ label: 'connection ' + connectionName, kind: 'connection' });
  return via;
}
/** Every API key and role that can reach a saved query, and why - the reverse of queryGrantNode() (which
 * answers "what can this key reach"), used for the per-query Access panel and the Access map screen. The
 * admin key is never listed here: it is not one of apiKeysCache's entries, and it can always run everything. */
function queryReach(queryName, collection, connectionName) {
  var keys = Object.keys(apiKeysCache).sort().map(function (name) {
    return { name: name, active: apiKeysCache[name].active, via: reachVia(apiKeysCache[name], queryName, collection, connectionName) };
  }).filter(function (k) { return k.via.length; });
  var roles = Object.keys(rolesCache).sort().map(function (name) {
    return { name: name, via: reachVia(rolesCache[name], queryName, collection, connectionName) };
  }).filter(function (r) { return r.via.length; });
  return { keys: keys, roles: roles };
}

/** Checkboxes over the existing collections for a key/role form; a name the entry already holds stays listed even when it has emptied. */
function collectionGrantField(held) {
  var checks = {};
  var names = collectionNames();
  var box = h('div', { className: 'tags' }, names.length ? names.map(function (c) {
    var cb = h('input', { type: 'checkbox' });
    cb.checked = (held || []).indexOf(c) !== -1;
    checks[c] = cb;
    return h('label', { className: 'switch', style: 'font-weight:400' }, cb, c,
      h('span', { className: 'dim', text: ' (' + collectionsCache.collections[c].queries.length + ')' }));
  }) : h('span', { className: 'hint', text: 'No collections yet — file a saved query under one first.' }));
  return {
    node: h('div', { className: 'field' }, h('label', {}, 'Collections'), box,
      h('span', { className: 'hint', text: '— reaches every query in the collection, including ones filed there later; read-only, and never ad-hoc SQL. Moving a query into or out of a collection changes what this key can run.' })),
    value: function () { return Object.keys(checks).filter(function (c) { return checks[c].checked; }); }
  };
}

// ---- example APIs (installed by `queryapigate examples load`; everything they add is marked "example") ----
var examplesState = { loaded: false, partial: false, queries: [], collections: [] };
function exampleBadge() { return h('span', { className: 'tag example', title: 'Installed by the example APIs — removing the examples removes it', text: 'example' }); }
async function loadExamples() {
  var data = await apiJson('examples');
  examplesState = data || { loaded: false, partial: false, queries: [], collections: [] };
  renderExamplesStrip();
}
function renderExamplesStrip() {
  var box = clear($('examples-strip'));
  var st = examplesState;
  if (!st.loaded && !st.partial) { box.hidden = true; return; }
  box.hidden = false;
  box.appendChild(exampleBadge());
  box.appendChild(h('span', { style: 'flex:1', text: st.partial
    ? 'The example APIs are only partly loaded (an interrupted load).'
    : 'Example APIs are loaded: ' + st.queries.length + ' queries in ' + st.collections.length + ' collections, four roles and an “examples” connection. Removing them touches nothing else.' }));
  if (st.partial) box.appendChild(h('button', { type: 'button', className: 'btn sm outlined', text: 'Finish loading', onclick: loadExampleData }));
  box.appendChild(h('button', { type: 'button', className: 'btn sm outlined danger', text: 'Remove examples', onclick: removeExamples }));
}
async function loadExampleData() {
  var res = await apiJson('examples', { method: 'POST' });
  if (res) { showError(''); toast('Example APIs loaded'); refreshAll(); }
}
async function removeExamples() {
  if (!confirm('Remove the example APIs? This deletes exactly the queries, roles and connection marked “example” — nothing else.')) return;
  var res = await apiJson('examples', { method: 'DELETE' });
  if (!res) return;
  toast('Example APIs removed');
  if ((res.keys_still_granted || []).length) showError('These keys were granted an example collection, which no longer exists, so that grant now reaches nothing: ' + res.keys_still_granted.join(', '));
  refreshAll();
}

// ---- saved queries ----
var filesCache = [];
var selected = { name: null, version: null, tab: 'run' };
var contentCache = {};

function latestOf(f) { return f.versions[f.versions.length - 1] || {}; }
function findFile(name) { return filesCache.filter(function (f) { return f.filename === name; })[0] || null; }
function lastRun(v) { var hs = v.execution_history || []; return hs[hs.length - 1] || null; }

var queriesInitialised = false;
async function loadQueries(selectName) {
  await loadCollections();
  var data = await apiJson('list_files');
  if (!data) {
    clear($('queries-table')).appendChild(h('div', { className: 'empty' }, h('strong', { text: 'Couldn’t load saved queries' }),
      h('button', { type: 'button', className: 'btn sm', text: 'Retry', onclick: function () { loadQueries(); } })));
    return;
  }
  filesCache = (data.files || []).slice().sort(function (a, b) { return a.filename < b.filename ? -1 : 1; });
  contentCache = {};
  $('count-queries').textContent = filesCache.length ? String(filesCache.length) : '';
  paintQueriesSub();
  if (selectName !== undefined) selected.name = selectName;
  if (!queriesInitialised && filesCache.length) {
    // First load: land on the Queries tab of the first query's own collection (the rest are one click away).
    queriesInitialised = true;
    var ordered = filesCache.slice().sort(function (a, b) { return ((a.collection || '\uffff') + a.filename) < ((b.collection || '\uffff') + b.filename) ? -1 : 1; });
    if (!selected.name) { selected.name = ordered[0].filename; selected.version = latestOf(ordered[0]).version; }
    var chosen = findFile(selected.name);
    if (chosen) { queriesView = 'queries'; queriesActiveCollection = chosen.collection || ''; }
  }
  var f = selected.name && findFile(selected.name);
  if (!f) { selected.name = null; selected.version = null; }
  else if (!f.versions.some(function (v) { return v.version === selected.version; })) selected.version = latestOf(f).version;
  renderQueryList();
  renderDetail();
  renderAccessMap();
}
function paintQueriesSub() {
  var sub = clear($('queries-sub'));
  var versions = filesCache.reduce(function (n, f) { return n + f.versions.length; }, 0);
  var collections = Array.from(new Set(filesCache.map(function (f) { return f.collection; }).filter(Boolean))).length;
  sub.appendChild(document.createTextNode((filesCache.length ? versions + (versions === 1 ? ' version' : ' versions') +
    (collections ? ' across ' + collections + (collections === 1 ? ' collection' : ' collections') : '') + '. ' : '') + 'Each one is published at '));
  sub.appendChild(h('code', { text: '/q/<name>' }));
  sub.appendChild(document.createTextNode('.'));
}
function queryItem(f, showCollectionTag) {
  var l = latestOf(f);
  return h('button', { type: 'button', 'data-name': f.filename, className: 'qitem' + (f.filename === selected.name ? ' sel' : ''), onclick: function () {
    selected.name = f.filename; selected.version = l.version; renderQueryList(); renderDetail(); } },
    h('div', { className: 'qi-top' },
      h('span', { className: 'name', text: f.filename }),
      showCollectionTag && f.collection ? h('span', { className: 'tag', text: f.collection }) : null,
      h('span', { className: 'tag v', text: 'v' + l.version })),
    l.description ? h('div', { className: 'qi-desc', text: l.description }) : null);
}
function collectionRow(name, count) {
  return h('button', { type: 'button', className: 'qcoll-row', onclick: function () {
    queriesView = 'queries'; queriesActiveCollection = name; renderQueryList(); } },
    h('span', { className: 'qg-name', text: name || 'No collection' }), h('span', { className: 'qg-n', text: String(count) }));
}
/** The reach/Postman/Rename line the old expand/collapse group's footer showed for a collection - now a
 * header above its query list (queryItem() stays a direct #queries-table child either way, so it always
 * gets that selector's flat, unindented padding - there is no tree level left to indent under). null for
 * the "No collection" bucket, which has no collection identity to act on. */
function collectionHeader(name) {
  if (!name) return null;
  var known = collectionsCache.collections[name];
  var reach = known ? known.keys.length : 0;
  return h('div', { className: 'qg-foot' },
    h('span', { title: reach ? 'Keys granted this collection: ' + known.keys.join(', ') : 'No key is granted this collection', text: reach + (reach === 1 ? ' key' : ' keys') }),
    h('a', { href: '#', title: 'Download this collection as a Postman Collection file (one request per query; holds no API key)', text: 'Postman', onclick: function (e) { e.preventDefault(); downloadPostman(name); } }),
    h('a', { href: '#', title: 'Rename this collection, carrying every key and role grant with it', text: 'Rename', onclick: function (e) { e.preventDefault(); openRenameCollectionForm(name); } }));
}
function renderQueryList() {
  var subtabs = clear($('queries-subtabs'));
  var box = clear($('queries-table'));
  if (!filesCache.length) {
    box.appendChild(h('div', { className: 'empty' }, h('strong', { text: 'No saved queries yet' }),
      h('span', { text: 'Saved queries become GET /q/<name> endpoints with typed parameters.' }),
      h('button', { type: 'button', className: 'btn primary', text: 'New saved query', onclick: function () { openQueryForm(); } }),
      h('button', { type: 'button', className: 'btn', text: 'Load example APIs', title: 'Install four worked scenarios (reporting, dashboard, export, partner) you can try and remove again', onclick: loadExampleData })));
    return;
  }
  var q = $('query-filter').value.trim().toLowerCase();
  var list = filesCache.filter(function (f) {
    var l = latestOf(f);
    return !q || [f.filename, f.collection, l.description, (l.tags || []).join(' '), l.connection_name, l.author].join(' ').toLowerCase().indexOf(q) !== -1;
  });
  if (!list.length) { box.appendChild(h('div', { className: 'empty' }, h('span', { text: 'Nothing matches “' + q + '”.' }))); return; }
  // A search in progress overrides Collections/Queries entirely - matches can span any number of
  // collections, so a flat list (each item labelled with its own collection) beats a two-level drill-down.
  if (q) { list.forEach(function (f) { box.appendChild(queryItem(f, true)); }); return; }
  // Grouping is decided from the whole list, not the filtered one (moot here since q is empty, but keeps
  // the collection tab list stable rather than recomputed from a possibly-filtered set).
  if (!filesCache.some(function (f) { return f.collection; })) { list.forEach(function (f) { box.appendChild(queryItem(f)); }); return; }
  var groups = {};
  filesCache.forEach(function (f) { var c = f.collection || ''; (groups[c] = groups[c] || []).push(f); });
  var names = Object.keys(groups).filter(Boolean).sort();
  if (groups['']) names.push('');
  // The active collection can vanish out from under this view (renamed, or its last query moved/deleted
  // elsewhere) - fall back to Collections rather than an empty screen or a stale "Queries · <old name>" tab.
  if (queriesActiveCollection !== null && !groups[queriesActiveCollection]) { queriesView = 'collections'; queriesActiveCollection = null; }
  subtabs.appendChild(h('div', { className: 'minitabs' },
    h('button', { type: 'button', className: 'minitab' + (queriesView === 'collections' ? ' active' : ''),
      onclick: function () { queriesView = 'collections'; renderQueryList(); } }, 'Collections'),
    h('button', { type: 'button', className: 'minitab' + (queriesView === 'queries' ? ' active' : ''), disabled: queriesActiveCollection === null,
      onclick: function () { if (queriesActiveCollection !== null) { queriesView = 'queries'; renderQueryList(); } } },
      'Queries' + (queriesActiveCollection !== null ? ' · ' + (queriesActiveCollection || 'No collection') : ''))));
  if (queriesView === 'queries' && queriesActiveCollection !== null && groups[queriesActiveCollection]) {
    var header = collectionHeader(queriesActiveCollection);
    if (header) box.appendChild(header);
    groups[queriesActiveCollection].forEach(function (f) { box.appendChild(queryItem(f)); });
  } else {
    names.forEach(function (c) { box.appendChild(collectionRow(c, groups[c].length)); });
  }
}
$('query-filter').oninput = renderQueryList;

async function getContent(name) {
  if (contentCache[name]) return contentCache[name];
  var data = await apiJson('view_file_content?filename=' + enc(name));
  if (!data) return null;
  var parsed = null;
  try { parsed = JSON.parse(data.content); } catch (e) { parsed = null; }
  contentCache[name] = { raw: data.content, parsed: parsed };
  return contentCache[name];
}

/** The always-visible "who can reach this" panel on a saved query - the answer this app has and a plain
 * request client (Postman and friends) never will, so it sits beside the run panel, not behind a tab. */
function accessBox(queryName, collection, connectionName) {
  var reach = queryReach(queryName, collection, connectionName);
  var summary = h('div', { className: 'access-summary' },
    h('b', { text: String(reach.keys.length) }), ' ' + (reach.keys.length === 1 ? 'API key' : 'API keys') + (reach.keys.length ? ' reach' : ' reaches') + ' this query');
  if (reach.keys.length) {
    var names = h('div', { className: 'access-names' });
    reach.keys.forEach(function (k, i) {
      names.appendChild(h('span', { className: 'name', title: k.via.map(function (v) { return v.label; }).join(', '), text: k.name + (i < reach.keys.length - 1 ? ',' : '') }),
        k.active ? null : h('span', { className: 'dim', text: ' (revoked)' }));
    });
    summary.appendChild(names);
  } else {
    summary.appendChild(h('span', { className: 'hint', text: 'Only the admin key can run it.' }));
  }
  summary.appendChild(h('button', { type: 'button', className: 'btn sm ghost', style: 'margin-left:auto', text: 'View access map', onclick: function () {
    showTab('accessmap'); $('accessmap-filter').value = queryName; renderAccessMap();
  } }));
  var roleLine = reach.roles.length
    ? h('div', { className: 'access-roles hint' }, 'Also granted to role' + (reach.roles.length > 1 ? 's' : '') + ' ',
        h('span', { className: 'mono' }, reach.roles.map(function (r) { return r.name; }).join(', ')),
        ' - a key must be created from one of these to actually call it.')
    : null;
  return h('div', { className: 'access-box' }, summary, roleLine);
}
/** The Access map screen: every saved query against every API key, so "which keys can call which endpoints"
 * is answered at a glance instead of by opening each query or each key in turn. */
/** Everything about a saved query that isn't already on screen in the Saved queries detail view: when it was
 * first created (the earliest version, not just the one being looked at), when it was last changed, when it
 * was last actually called (the newest execution_history entry across *every* version, not just the latest),
 * and the dialect of the connection it runs on. Shared by the access map's info popup and, potentially, any
 * other place that wants the same "everything about this query" summary. */
function queryStats(f) {
  var earliest = f.versions[0];
  f.versions.forEach(function (v) { if ((v.created_at || '') < (earliest.created_at || '')) earliest = v; });
  var latest = latestOf(f);
  var lastUsed = null;
  f.versions.forEach(function (v) {
    (v.execution_history || []).forEach(function (run) {
      if (run.executed_at && (!lastUsed || run.executed_at > lastUsed)) lastUsed = run.executed_at;
    });
  });
  var conn = connectionsCache[latest.connection_name];
  return { created: earliest.created_at || null, modified: latest.last_modified_at || latest.created_at || null,
    lastUsed: lastUsed, dbType: conn ? conn.db : null, connectionName: latest.connection_name, latest: latest };
}
/** A read-only popup (the existing drawer, repurposed) with everything queryStats() knows about one saved
 * query, plus a per-version breakdown - the "tell me more about this one query" the access map's info icon
 * opens, so the matrix itself doesn't need three more columns per query to answer it. */
var queryInfoToken = 0; // bumped on every open, so a slow getContent() from a previous popup can never paint over a newer one
async function openQueryInfo(f) {
  var token = ++queryInfoToken;
  var stats = queryStats(f);
  var latest = stats.latest;
  var conn = connectionsCache[stats.connectionName];
  var slot = openDrawer('query-info-slot', f.filename, (f.collection || 'uncollected') + ' · ' +
    f.versions.length + (f.versions.length === 1 ? ' version' : ' versions'));
  var sqlBox = h('div', {}, loadingNode('Loading SQL…'));
  slot.appendChild(h('div', { className: 'form' },
    latest.description ? h('p', { className: 'd-desc', text: latest.description }) : null,
    h('dl', { className: 'meta' },
      metaItem('connection', stats.connectionName || '—'),
      h('div', {}, h('dt', { text: 'database' }), h('dd', {}, conn ? h('span', { className: 'tag', text: conn.db }) : h('span', { className: 'dim', text: '—' }))),
      metaItem('collection', f.collection || '—'),
      metaItem('status', latest.status || '—'),
      metaItem('author', latest.author || '—'),
      metaItem('created', stats.created || '—'),
      metaItem('last modified', stats.modified || '—'),
      metaItem('last used', stats.lastUsed || 'never'),
      (latest.tags || []).length ? metaItem('tags', latest.tags.join(', ')) : null),
    h('p', { className: 'sub-h', style: 'margin-top:6px', text: 'SQL · v' + latest.version + (f.versions.length > 1 ? ' (latest)' : '') }),
    sqlBox,
    h('p', { className: 'sub-h', style: 'margin-top:6px', text: 'Versions' }),
    h('div', { className: 'panel', style: 'overflow:auto' }, h('table', { className: 'grid qi-versions' },
      h('thead', {}, h('tr', {}, ['Version', 'Created', 'Last modified', 'Runs'].map(function (t) { return h('th', { text: t }); }))),
      h('tbody', {}, f.versions.slice().reverse().map(function (v) {
        return h('tr', {}, h('td', { className: 'mono', text: 'v' + v.version }),
          h('td', { className: 'mono dim', text: v.created_at || '—' }),
          h('td', { className: 'mono dim', text: v.last_modified_at || '—' }),
          h('td', { className: 'mono', style: 'text-align:right', text: String((v.execution_history || []).length) }));
      })))),
    h('div', { className: 'form-actions' },
      h('button', { type: 'button', className: 'btn', text: 'Close', onclick: closeDrawer }),
      h('button', { type: 'button', className: 'btn primary', text: 'Open in Saved queries', onclick: function () {
        closeDrawer(); showTab('queries');
        queriesView = 'queries'; queriesActiveCollection = f.collection || '';
        selected.name = f.filename; selected.version = latest.version; selected.tab = 'run';
        renderQueryList(); renderDetail();
      } }))));
  var c = await getContent(f.filename);
  if (token !== queryInfoToken) return; // the popup moved on (or closed) while this was in flight
  clear(sqlBox);
  if (!c) { sqlBox.appendChild(h('div', { className: 'hint', text: 'Could not load the SQL.' })); return; }
  var data = c.parsed && c.parsed[String(latest.version)];
  var sql = queryDisplayText(data, c.raw);
  // Several example queries (and anything else authored as one Python string) have no line breaks of their
  // own; showing that verbatim is an unreadable, horizontally-scrolling wall of text. Reformat only when the
  // author supplied no line breaks at all - a query someone hand-formatted keeps exactly the layout they gave it.
  var display = sql.indexOf('\n') === -1 ? formatSql(sql) : sql;
  sqlBox.appendChild(codeBox(display));
  sqlBox.appendChild(h('div', { style: 'margin-top:6px' },
    h('button', { type: 'button', className: 'btn sm ghost', text: 'Copy SQL', onclick: function () { copyText(display); } })));
}
/** True clause keywords only - "BY", "JOIN", "INTO", "ALL" are deliberately left out so they stay glued to
 * the word before them (GROUP BY, LEFT JOIN, INSERT INTO, UNION ALL land on one line, not split in two). */
var SQL_CLAUSE_KW = { select: 1, from: 1, where: 1, having: 1, limit: 1, offset: 1, union: 1, with: 1, insert: 1,
  update: 1, set: 1, values: 1, join: 1, on: 1, group: 1, order: 1, delete: 1,
  left: 1, right: 1, inner: 1, outer: 1, full: 1, cross: 1 };
var SQL_JOIN_PREFIX = { left: 1, right: 1, inner: 1, outer: 1, full: 1, cross: 1 };
/** A light, best-effort pretty-printer for a SQL string with no line breaks of its own: puts each clause on
 * its own line using the same tokenizer highlightInto() uses, so it can never break in the middle of a
 * string, a quoted identifier or a comment. It has no real parser behind it - depth and precedence are not
 * tracked - so a keyword inside a subquery or a window function's OVER (...) breaks onto its own line too;
 * for a read-only "let me glance at this query" popup that is a fair trade for staying simple and safe. */
function formatSql(sql) {
  SQL_TOKEN.lastIndex = 0;
  var pieces = [], pos = 0, lastWord = null, m;
  while ((m = SQL_TOKEN.exec(sql))) {
    var gap = sql.slice(pos, m.index).replace(/[ \t]{2,}/g, ' ');
    var word = m[7] ? m[7].toLowerCase() : null;
    // JOIN right after a LEFT/RIGHT/INNER/OUTER/FULL/CROSS prefix, and FROM right after DELETE, stay glued to
    // that prefix instead of starting their own line - the pair reads as one clause, not two.
    var glued = (word === 'join' && SQL_JOIN_PREFIX[lastWord]) || (word === 'from' && lastWord === 'delete');
    if (word && SQL_CLAUSE_KW[word] && !glued) { pieces.push(gap, '\n', m[0]); } else { pieces.push(gap, m[0]); }
    lastWord = word;
    pos = SQL_TOKEN.lastIndex;
  }
  pieces.push(sql.slice(pos));
  return pieces.join('').split('\n').map(function (line) { return line.trim(); })
    .filter(function (line, i) { return line !== '' || i === 0; }).join('\n');
}
/** true once a key's expires_at date has passed - the same check renderApiKeys() makes, factored out so the
 * access map can grey out an expired key exactly like the API keys screen does. */
function isKeyExpired(k) {
  var today = new Date().toISOString().slice(0, 10);
  return !!(k.expires_at && k.expires_at < today);
}
var AMAP_CODE = { query: 'Q', collection: 'C', connection: 'W' };
var AMAP_INFO_HEADERS = ['Database', 'Created', 'Last modified', 'Last used', 'Version'];
var AMAP_INFO_FIELDS = ['database', 'created', 'modified', 'lastUsed', 'version']; // same order, for sortable headers
/** The same five at-a-glance facts the info popup opens to show, as plain <td>s for the access map's rows -
 * so the common case (what dialect, how stale, which version) never needs a click to see. */
function amapInfoCells(f) {
  var stats = queryStats(f);
  var conn = connectionsCache[stats.connectionName];
  return [
    h('td', {}, conn ? h('span', { className: 'tag', text: conn.db }) : h('span', { className: 'dim', text: '—' })),
    h('td', { className: 'mono dim', text: stats.created || '—' }),
    h('td', { className: 'mono dim', text: stats.modified || '—' }),
    h('td', { className: stats.lastUsed ? 'mono' : 'mono dim', text: stats.lastUsed || 'never' }),
    h('td', { className: 'mono', text: 'v' + stats.latest.version })
  ];
}
// ---- access map sorting: click any header (a scalar column or a specific key/role's own column) to sort by
// it; click again to reverse; a third click returns to the default collection/name order. `field` is one of
// the scalar names ('query', 'database', ..., 'reach'), or 'col' for a specific key/role's own reach, in
// which case colType ('key'|'role') and colName pick out which one. ----
// ---- access map connection/table drill-down: pick a connection, see its actual tables (via the same
// /connections/<name>/schema the Run SQL schema browser already uses), then pick one to find out which
// saved queries - if any - already expose it. This is the "which tables are already exposed" discovery
// tool: at tens or hundreds of endpoints, nobody can hold that in their head from the query list alone. ----
var amapConnFilter = '';
var amapTableFilter = '';
var amapTableMatches = null; // null (no table filter), 'loading', or a Set of filenames that reference the table
/** Rebuilds the Table select from schemaCache[amapConnFilter] - "pick a connection first", a loading state,
 * an error state, or the real table list - fetching that connection's schema at most once (schemaBrowser()
 * follows the same already-cached-unless-absent rule). */
function amapPaintTableOptions() {
  var sel = $('accessmap-table-filter');
  clear(sel);
  if (!amapConnFilter) { sel.appendChild(h('option', { value: '', text: 'Select a connection first' })); sel.disabled = true; return; }
  var entry = schemaCache[amapConnFilter];
  if (!entry || entry.status === 'loading') { loadSchema(amapConnFilter, function () { amapPaintTableOptions(); renderAccessMap(); }); entry = { status: 'loading' }; }
  if (entry.status === 'loading') { sel.appendChild(h('option', { value: '', text: 'Loading tables…' })); sel.disabled = true; return; }
  if (entry.status === 'error') { sel.appendChild(h('option', { value: '', text: 'Schema unavailable' })); sel.disabled = true; return; }
  sel.disabled = false;
  sel.appendChild(h('option', { value: '', text: 'All tables (' + entry.tables.length + ')' }));
  entry.tables.forEach(function (t) { sel.appendChild(h('option', { value: t.name, text: t.name + ' · ' + t.columns.length + (t.columns.length === 1 ? ' col' : ' cols') })); });
  sel.value = entry.tables.some(function (t) { return t.name === amapTableFilter; }) ? amapTableFilter : '';
}
/** Which of `connName`'s saved queries actually mention `tableName` in their (latest version's) SQL - a
 * plain word-boundary text search, not real parsing, same trade-off as formatSql(). Scoped to one
 * connection's queries, not every query on the server, so this stays cheap even with hundreds of endpoints
 * overall. getContent() caches per file, so re-picking the same table later costs nothing further. */
async function computeTableMatches(connName, tableName) {
  var candidates = filesCache.filter(function (f) { return latestOf(f).connection_name === connName; });
  var needle = new RegExp('\\b' + tableName.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '\\b', 'i');
  var hits = await Promise.all(candidates.map(async function (f) {
    var c = await getContent(f.filename);
    if (!c) return null;
    var latest = latestOf(f);
    var data = c.parsed && c.parsed[String(latest.version)];
    var sql = queryDisplayText(data, c.raw);
    return needle.test(sql) ? f.filename : null;
  }));
  return new Set(hits.filter(Boolean));
}
$('accessmap-conn-filter').onchange = function () {
  amapConnFilter = this.value;
  amapTableFilter = ''; amapTableMatches = null;
  renderAccessMap();
};
$('accessmap-table-filter').onchange = function () {
  amapTableFilter = this.value;
  if (!amapTableFilter) { amapTableMatches = null; renderAccessMap(); return; }
  amapTableMatches = 'loading';
  var conn = amapConnFilter, table = amapTableFilter;
  renderAccessMap();
  computeTableMatches(conn, table).then(function (set) {
    if (amapConnFilter !== conn || amapTableFilter !== table) return; // selection moved on while this was in flight
    amapTableMatches = set;
    renderAccessMap();
  });
};
var amapSort = { field: null, colType: null, colName: null, dir: 1 };
function amapSortLabel(field, colType, colName) {
  var on = amapSort.field === field && amapSort.colType === (colType || null) && amapSort.colName === (colName || null);
  return on ? (amapSort.dir === 1 ? ' ▲' : ' ▼') : '';
}
function amapToggleSort(field, colType, colName) {
  var same = amapSort.field === field && amapSort.colType === (colType || null) && amapSort.colName === (colName || null);
  amapSort = same && amapSort.dir === 1 ? { field: field, colType: colType || null, colName: colName || null, dir: -1 }
    : same ? { field: null, colType: null, colName: null, dir: 1 }
    : { field: field, colType: colType || null, colName: colName || null, dir: 1 };
  renderAccessMap();
}
/** The value one row sorts by, for whichever column was clicked - `item` is one of the {f, l, stats, reach}
 * records renderAccessMap() builds per row. 'col' reads whether the given key or role reaches this row at
 * all (0 or 1), so sorting a key's own column groups every query it can reach at one end. */
function amapCompareValue(item, field, colType, colName) {
  switch (field) {
    case 'query': return item.f.filename.toLowerCase();
    case 'database': return (item.stats.dbType || '').toLowerCase();
    case 'created': return item.stats.created || '';
    case 'modified': return item.stats.modified || '';
    case 'lastUsed': return item.stats.lastUsed || '';
    case 'version': return Number(item.stats.latest.version) || 0;
    case 'connection': return (item.stats.connectionName || '').toLowerCase();
    case 'reach': return item.reach;
    case 'col':
      var cache = colType === 'key' ? apiKeysCache : rolesCache;
      return reachVia(cache[colName], item.f.filename, item.f.collection, item.l.connection_name).length ? 1 : 0;
    default: return '';
  }
}
/** A clickable, sortable header for one of the fixed scalar columns (Query, Database, ..., Reach). */
function amapTh(label, className, field) {
  return h('th', { className: className, onclick: function () { amapToggleSort(field, null, null); } }, label + amapSortLabel(field, null, null));
}
/** One column header, shared by an API key and a role - both have a name, connections/queries/collections
 * grants, allow_writes and rate_limit, so the same two-line header (name, then access + rate limit) fits
 * either. `revokedTitle` is set only for a key (roles have no active/expires_at state of their own);
 * `colType` ('key'|'role') is what sorting by this specific column records in amapSort. */
function amapColHead(name, entry, revokedTitle, colType) {
  var sub = (entry.allow_writes ? 'Read/write' : 'Read-only') + ' · ' + (entry.rate_limit || 'server default');
  return h('th', { className: 'amap-key-head amap-sortable' + (revokedTitle ? ' revoked' : ''),
    title: revokedTitle ? name + revokedTitle : name, onclick: function () { amapToggleSort('col', colType, name); } },
    h('span', { className: 'amap-key-name' }, name, amapSortLabel('col', colType, name),
      revokedTitle ? h('span', { className: 'dim', text: revokedTitle }) : null),
    h('span', { className: 'amap-key-sub', text: sub }));
}
/** One reach cell - a key's and a role's are identical apart from the `role` modifier class, which mutes
 * the dot so the two column groups stay visually distinct even without re-reading the group header. Returns
 * {node, reached} rather than just the <td> so callers can tally reach without re-deriving it from the DOM. */
function amapCell(entry, f, l, roleClass) {
  var via = reachVia(entry, f.filename, f.collection, l.connection_name);
  if (!via.length) return { node: h('td', { className: 'amap-cell' }), reached: false };
  var codes = Array.from(new Set(via.map(function (v) { return AMAP_CODE[v.kind]; })));
  var viaConnOnly = codes.length === 1 && codes[0] === 'W';
  var node = h('td', { className: 'amap-cell', title: via.map(function (v) { return v.label; }).join(', ') },
    h('span', { className: 'amap-dot' + (roleClass ? ' role' : '') + (viaConnOnly ? ' conn' : ''), text: codes.join('·') }));
  return { node: node, reached: true };
}
function renderAccessMap() {
  var box = clear($('accessmap-body'));
  if (!filesCache.length) { box.appendChild(h('div', { className: 'empty' }, h('strong', { text: 'No saved queries yet' }))); return; }

  // The Database filter's own options are rebuilt every render (which dialects exist can change), keeping
  // whatever was already picked if it is still one of them.
  var dbSelect = $('accessmap-db-filter');
  var dbCurrent = dbSelect.value;
  var dbTypes = Array.from(new Set(filesCache.map(function (f) {
    var conn = connectionsCache[latestOf(f).connection_name]; return conn ? conn.db : null;
  }).filter(Boolean))).sort();
  clear(dbSelect).appendChild(h('option', { value: '', text: 'All databases' }));
  dbTypes.forEach(function (t) { dbSelect.appendChild(h('option', { value: t, text: t })); });
  if (dbTypes.indexOf(dbCurrent) !== -1) dbSelect.value = dbCurrent;

  // Connection drill-down: every defined connection, whether or not a saved query uses it yet - listing an
  // unused one is exactly how this screen surfaces "nothing exposes this database at all".
  var connSelect = $('accessmap-conn-filter');
  var connNamesAll = Object.keys(connectionsCache).sort();
  clear(connSelect).appendChild(h('option', { value: '', text: 'All connections' }));
  connNamesAll.forEach(function (name) { connSelect.appendChild(h('option', { value: name, text: name })); });
  connSelect.value = connNamesAll.indexOf(amapConnFilter) !== -1 ? amapConnFilter : '';
  amapPaintTableOptions();

  var keyNames = Object.keys(apiKeysCache).sort();
  var roleNames = Object.keys(rolesCache).sort();
  if (!keyNames.length && !roleNames.length) {
    box.appendChild(h('div', { className: 'empty' }, h('strong', { text: 'No scoped API keys or roles yet' }),
      h('span', { text: 'Every query is reachable by the admin key only until a scoped key is created.' })));
    return;
  }
  var q = $('accessmap-filter').value.trim().toLowerCase();
  var allRows = filesCache.slice().sort(function (a, b) { return ((a.collection || '￿') + a.filename) < ((b.collection || '￿') + b.filename) ? -1 : 1; });
  var match = function (name) { return name.toLowerCase().indexOf(q) !== -1; };
  var rowsMatch = q ? allRows.filter(function (f) { return match(f.filename) || (f.collection && match(f.collection)); }) : allRows;
  var keyColsMatch = q ? keyNames.filter(match) : keyNames;
  var roleColsMatch = q ? roleNames.filter(match) : roleNames;
  // A search term narrows whichever axis (or axes) it actually matches; an axis it doesn't touch stays whole
  // rather than collapsing to nothing - "acme" shows every query under just that key's column, with every
  // role column still shown too, and "top_films" shows every key and role column for just that one row.
  var anyMatch = rowsMatch.length || keyColsMatch.length || roleColsMatch.length;
  var rows = rowsMatch.length ? rowsMatch : (anyMatch ? allRows : []);
  var keyCols = keyColsMatch.length ? keyColsMatch : (anyMatch ? keyNames : []);
  var roleCols = roleColsMatch.length ? roleColsMatch : (anyMatch ? roleNames : []);

  if (amapConnFilter) rows = rows.filter(function (f) { return latestOf(f).connection_name === amapConnFilter; });
  // The table filter needs each candidate query's actual SQL, fetched (and cached) on demand - amapTableMatches
  // is null until that finishes, and the sentinel string 'loading' while it's in flight.
  if (amapTableFilter && amapTableMatches === 'loading') { box.appendChild(loadingNode('Checking which queries reference ' + amapTableFilter + '…')); return; }
  if (amapTableFilter && amapTableMatches) rows = rows.filter(function (f) { return amapTableMatches.has(f.filename); });

  if (!rows.length) {
    var reason = q ? 'Nothing matches “' + q + '”.'
      : amapTableFilter ? '“' + amapTableFilter + '” on ' + amapConnFilter + ' is not exposed by any saved query yet.'
      : amapConnFilter ? 'No saved query uses “' + amapConnFilter + '” yet.'
      : 'Nothing matches these filters.';
    var empty = h('div', { className: 'empty' }, h('span', { text: reason }));
    if (!q && amapConnFilter) {
      empty.appendChild(h('button', { type: 'button', className: 'btn primary', text: 'New saved query' + (amapTableFilter ? ' on ' + amapTableFilter : ''), onclick: function () {
        var conn = amapConnFilter, table = amapTableFilter;
        var isMongo = (connectionsCache[conn] || {}).db === 'mongo';
        var starter = table && isMongo ? JSON.stringify({ collection: table, filter: {} }, null, 2)
                                       : table ? 'SELECT * FROM ' + table : null;
        showTab('queries');
        openQueryForm(null, starter ? { connection_name: conn, sql_query: starter } : { connection_name: conn });
      } }));
    }
    box.appendChild(empty); return;
  }

  // One record per row up front - {f, l, stats, reach} - so the Database and Reach filters and every sortable
  // column can all read off the same precomputed values instead of re-deriving them in three different places.
  var dbFilter = dbSelect.value;
  var reachFilter = $('accessmap-reach-filter').value;
  var enriched = rows.map(function (f) {
    var l = latestOf(f);
    var stats = queryStats(f);
    var reach = keyCols.reduce(function (n, name) {
      return n + (reachVia(apiKeysCache[name], f.filename, f.collection, l.connection_name).length ? 1 : 0);
    }, 0);
    return { f: f, l: l, stats: stats, reach: reach };
  });
  if (dbFilter) enriched = enriched.filter(function (item) { return item.stats.dbType === dbFilter; });
  if (reachFilter === 'reachable') enriched = enriched.filter(function (item) { return item.reach > 0; });
  else if (reachFilter === 'unreachable') enriched = enriched.filter(function (item) { return item.reach === 0; });
  if (amapSort.field) {
    enriched.sort(function (a, b) {
      var va = amapCompareValue(a, amapSort.field, amapSort.colType, amapSort.colName);
      var vb = amapCompareValue(b, amapSort.field, amapSort.colType, amapSort.colName);
      return va < vb ? -amapSort.dir : va > vb ? amapSort.dir : 0;
    });
  }
  if (!enriched.length) { box.appendChild(h('div', { className: 'empty' }, h('span', { text: 'No queries match these filters.' }))); return; }

  var leadCols = 2 + AMAP_INFO_HEADERS.length; // Query + Connection + the five at-a-glance columns
  var groupRow = h('tr', {},
    h('th', { colSpan: leadCols }),
    keyCols.length ? h('th', { colSpan: keyCols.length, className: 'amap-group', text: 'API KEYS' }) : null,
    roleCols.length ? h('th', { colSpan: roleCols.length, className: 'amap-group role',
      title: 'What a key created from each role would reach - a role grants nothing on its own', text: 'ROLES' }) : null,
    h('th'));
  var thead = h('thead', {}, groupRow, h('tr', {},
    amapTh('Query', 'amap-query amap-sortable', 'query'),
    AMAP_INFO_FIELDS.map(function (field, i) { return amapTh(AMAP_INFO_HEADERS[i], 'amap-sortable', field); }),
    amapTh('Connection', 'amap-sortable', 'connection'),
    keyCols.map(function (name) {
      var k = apiKeysCache[name], expired = isKeyExpired(k);
      return amapColHead(name, k, !k.active ? ' (revoked)' : expired ? ' (expired ' + k.expires_at + ')' : '', 'key');
    }),
    roleCols.map(function (name) { return amapColHead(name, rolesCache[name], '', 'role'); }),
    amapTh('Reach', 'num amap-sortable', 'reach')));

  var keyTotals = {}; keyCols.forEach(function (name) { keyTotals[name] = 0; });
  var roleTotals = {}; roleCols.forEach(function (name) { roleTotals[name] = 0; });
  var tbody = h('tbody', {}, enriched.map(function (item) {
    var f = item.f, l = item.l;
    var keyCells = keyCols.map(function (name) {
      var cell = amapCell(apiKeysCache[name], f, l, false);
      if (cell.reached) keyTotals[name]++;
      return cell.node;
    });
    var roleCells = roleCols.map(function (name) {
      var cell = amapCell(rolesCache[name], f, l, true);
      if (cell.reached) roleTotals[name]++;
      return cell.node;
    });
    return h('tr', {},
      h('td', { className: 'amap-query' },
        h('div', { className: 'amap-q-row' }, h('span', { text: f.filename }),
          h('button', { type: 'button', className: 'amap-info', title: 'Details for ' + f.filename, 'aria-label': 'Details for ' + f.filename,
            onclick: function () { openQueryInfo(f); } }, 'i')),
        f.collection ? h('span', { className: 'amap-coll', text: f.collection }) : null),
      amapInfoCells(f),
      h('td', { className: 'mono dim', text: l.connection_name || '—' }),
      keyCells, roleCells,
      h('td', { className: 'num', title: 'Reachable by an actual API key (roles alone reach nothing)', text: item.reach ? String(item.reach) : '—' }));
  }));

  var tfoot = h('tfoot', {}, h('tr', {},
    h('td', { className: 'amap-query', text: 'Reaches' }),
    AMAP_INFO_HEADERS.map(function () { return h('td'); }), h('td'),
    keyCols.map(function (name) { return h('td', { className: 'num', text: keyTotals[name] + ' / ' + enriched.length }); }),
    roleCols.map(function (name) { return h('td', { className: 'num', text: roleTotals[name] + ' / ' + enriched.length }); }),
    h('td')));

  box.appendChild(h('div', { style: 'overflow:auto' }, h('table', { className: 'amap' }, thead, tbody, tfoot)));
}
$('accessmap-filter').oninput = renderAccessMap;
$('accessmap-db-filter').onchange = renderAccessMap;
$('accessmap-reach-filter').onchange = renderAccessMap;


function renderDetail() {
  var box = clear($('query-detail'));
  var f = selected.name && findFile(selected.name);
  if (!f) {
    box.appendChild(h('div', { className: 'empty' }, h('strong', { text: 'No query selected' }),
      h('span', { text: 'Pick a saved query to run it, read its SQL or see its history.' })));
    return;
  }
  var latest = latestOf(f);
  var v = f.versions.filter(function (x) { return x.version === selected.version; })[0] || latest;
  var isLatest = v.version === latest.version;
  var history = v.execution_history || [];

  var versionNode = f.versions.length > 1
    ? h('div', { className: 'versions', role: 'group', 'aria-label': 'Version' }, f.versions.map(function (x) {
        return h('button', { type: 'button', className: x.version === v.version ? 'on' : '', text: 'v' + x.version,
          title: x.version === latest.version ? 'Latest version' : 'Version ' + x.version,
          onclick: function () { selected.version = x.version; renderDetail(); } });
      }))
    : h('span', { className: 'vbadge', text: 'v' + v.version });
  var status = String(v.status || 'active');
  var deleteBtn = h('button', { type: 'button', className: 'btn md danger', text: 'Delete…', title: 'Delete this version or the whole query', onclick: function () {
    var items = [];
    if (f.versions.length > 1) items.push({ label: 'Delete v' + v.version, title: 'DELETE /saved_sql/' + f.filename + '?version=' + v.version,
      run: function () { deleteQueryVersion(f.filename, v.version, f.versions.length); } });
    items.push({ label: f.versions.length > 1 ? 'Delete query (all ' + f.versions.length + ' versions)' : 'Delete query', run: function () { deleteQuery(f.filename); } });
    openMenu(deleteBtn, items);
  } });
  var tagsText = (v.tags || []).join(', ');
  var head = h('div', { className: 'd-head' },
    h('div', { className: 'd-title' },
      h('h3', { text: f.filename }),
      versionNode,
      h('span', { className: 'hint', text: isLatest ? 'latest' : 'older version' }),
      h('span', { className: 'pill ' + (status === 'active' ? 'ok' : 'off') }, h('i'), status.charAt(0).toUpperCase() + status.slice(1)),
      h('span', { className: 'spacer' }),
      h('button', { type: 'button', className: 'btn md', text: 'New version', onclick: function () { openQueryForm(f.filename, v); } }),
      h('button', { type: 'button', className: 'btn md', text: 'Move…', title: 'File this query under a collection (PUT /saved_sql/' + f.filename + '/collection)', onclick: function () { openMoveForm(f); } }),
      deleteBtn),
    v.description ? h('p', { className: 'd-desc', text: v.description }) : null,
    h('dl', { className: 'meta' },
      metaItem('connection', v.connection_name || '—'), metaItem('collection', f.collection || '—'), metaItem('author', v.author || '—'),
      metaItem('modified', v.last_modified_at || v.created_at || '—'), metaItem('status', v.status || '—'),
      tagsText ? metaItem('tags', tagsText) : null),
    accessBox(f.filename, f.collection, v.connection_name),
    h('div', { className: 'subtabs', role: 'tablist' },
      subtab('run', 'Run'), subtab('sql', 'SQL'),
      subtab('history', 'History', h('span', { className: 'count', text: history.length ? String(history.length) : '' })),
      subtab('curl', 'Curl')));
  var body = h('div', { className: 'd-body' });
  box.appendChild(head);
  box.appendChild(body);
  if (selected.tab === 'sql') renderSqlTab(body, f, v);
  else if (selected.tab === 'history') renderHistoryTab(body, v);
  else if (selected.tab === 'curl') renderCurlTab(body, f, v, isLatest);
  else renderRunTab(body, f, v, isLatest);
}
function metaItem(k, val) { return h('div', {}, h('dt', { text: k }), h('dd', { title: String(val), text: String(val) })); }
/** A small popover menu under `anchor`; closes on any outside click or Escape. items: [{label, title, run}]. */
function openMenu(anchor, items) {
  var old = document.getElementById('popmenu');
  if (old) old.remove();
  var box = anchor.getBoundingClientRect();
  var menu = h('div', { id: 'popmenu', className: 'menu', role: 'menu', style: 'top:' + (box.bottom + 4) + 'px;right:' + Math.max(8, window.innerWidth - box.right) + 'px' },
    items.map(function (it) {
      return h('button', { type: 'button', role: 'menuitem', title: it.title || null, text: it.label, onclick: function () { close(); it.run(); } });
    }));
  function close() { menu.remove(); document.removeEventListener('mousedown', outside, true); document.removeEventListener('keydown', esc, true); }
  function outside(e) { if (!menu.contains(e.target)) close(); }
  function esc(e) { if (e.key === 'Escape') close(); }
  document.body.appendChild(menu);
  setTimeout(function () { document.addEventListener('mousedown', outside, true); document.addEventListener('keydown', esc, true); }, 0);
}
/** Read-only code with line numbers, tokens coloured through the same highlighter as the editors. */
function codeBox(sql) {
  var lines = String(sql).split('\n');
  var box = h('div', { className: 'codebox' });
  var pre = h('div', { className: 'ln-rows' });
  lines.forEach(function (line, i) {
    var text = h('span', { className: 'ln-t' });
    highlightInto(text, line);
    if (!line) text.textContent = ' ';
    pre.appendChild(h('div', { className: 'ln-r' }, h('span', { className: 'ln-n', text: String(i + 1) }), text));
  });
  box.appendChild(pre);
  return box;
}
function subtab(key, label, extra) {
  return h('button', { type: 'button', role: 'tab', 'data-subtab': key, className: selected.tab === key ? 'on' : '', onclick: function () { selected.tab = key; renderDetail(); } }, label, extra || null);
}

async function renderSqlTab(body, f, v) {
  body.appendChild(loadingNode());
  var c = await getContent(f.filename);
  clear(body);
  if (!c) return;
  var data = c.parsed && c.parsed[String(v.version)];
  var shown = queryDisplayText(data, c.raw);
  var codeEl = codeBox(shown);
  var raw = h('pre', { className: 'code', text: c.raw });
  raw.hidden = true;
  var toggle = h('button', { type: 'button', className: 'btn sm ghost', text: 'Show raw file', onclick: function () {
    raw.hidden = !raw.hidden; toggle.textContent = raw.hidden ? 'Show raw file' : 'Hide raw file'; } });
  body.appendChild(codeEl);
  body.appendChild(h('div', { className: 'toolbar', style: 'margin:-6px 0 0' },
    h('button', { type: 'button', className: 'btn sm ghost', text: 'Copy SQL', onclick: function () { copyText(shown); } }), toggle));
  body.appendChild(raw);
  var qp = v.query_parameters || {};
  var keys = Object.keys(qp);
  body.appendChild(h('p', { className: 'sub-h', text: 'Parameters' }));
  if (!keys.length) { body.appendChild(h('div', { className: 'hint', text: 'This version declares no query_parameters.' })); return; }
  body.appendChild(h('div', { className: 'panel', style: 'overflow:auto' }, h('table', { className: 'grid' },
    h('thead', {}, h('tr', {}, ['Name', 'Type', 'Constraints', 'Description'].map(function (t) { return h('th', { text: t }); }))),
    h('tbody', {}, keys.map(function (k) {
      var s = typeof qp[k] === 'string' ? { type: qp[k] } : (qp[k] || {});
      var cons = Object.keys(s).filter(function (x) { return x !== 'type' && x !== 'description'; })
        .map(function (x) { return x + '=' + JSON.stringify(s[x]); }).join('  ');
      return h('tr', {}, h('td', {}, h('span', { className: 'name', text: k })), h('td', { className: 'mono', text: String(s.type || '') }),
        h('td', { className: 'mono dim', text: cons }), h('td', { text: s.description ? String(s.description) : '' }));
    })))));
}

function renderHistoryTab(body, v) {
  var hs = (v.execution_history || []).slice().reverse();
  if (!hs.length) { body.appendChild(h('div', { className: 'empty' }, h('strong', { text: 'No runs recorded' }), h('span', { text: 'Runs of v' + v.version + ' through /q/ appear here.' }))); return; }
  var statusSelect = h('select', {},
    h('option', { value: '', text: 'All statuses' }),
    h('option', { value: 'success', text: 'Success' }),
    h('option', { value: 'error', text: 'Failed' }));
  var searchInput = h('input', { className: 'search', type: 'search',
    placeholder: 'Filter by connection, caller, request id, error…' });
  body.appendChild(h('div', { className: 'toolbar', style: 'margin:0 0 10px' }, statusSelect, searchInput));
  var tableBox = h('div', { className: 'panel', style: 'overflow:auto;max-height:60vh' });
  body.appendChild(tableBox);

  function paint() {
    var status = statusSelect.value;
    var q = searchInput.value.trim().toLowerCase();
    var rows = hs.filter(function (x) {
      if (status && x.status !== status) return false;
      if (!q) return true;
      return [x.connection_name, x.key_name, x.request_id, x.error].join(' ').toLowerCase().indexOf(q) !== -1;
    });
    clear(tableBox);
    if (!rows.length) { tableBox.appendChild(h('div', { className: 'empty' }, h('span', { text: 'No runs match this filter.' }))); return; }
    tableBox.appendChild(h('table', { className: 'grid' },
      h('thead', {}, h('tr', {}, ['', 'Executed at', 'Caller', 'Rows', 'Duration', 'Request ID', 'Error'].map(function (t, i) { return h('th', { className: i === 3 || i === 4 ? 'num' : '', text: t }); }))),
      h('tbody', {}, rows.map(function (x) {
        var good = x.status === 'success';
        return h('tr', {},
          h('td', { style: 'width:20px;padding-right:0' }, h('span', { className: 'dot ' + (good ? 'ok' : 'bad'), title: String(x.status || '') })),
          h('td', { className: 'mono dim', style: 'white-space:nowrap', title: x.connection_name ? 'connection: ' + x.connection_name : null, text: String(x.executed_at || '') }),
          h('td', { className: 'mono', text: String(x.key_name || '') }),
          h('td', { className: 'mono num', text: x.rows === undefined || x.rows === null ? '—' : String(x.rows) }),
          h('td', { className: 'mono num', text: x.duration_ms === undefined ? '' : x.duration_ms + ' ms' }),
          h('td', { className: 'mono dim', title: String(x.request_id || ''), style: 'max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap', text: String(x.request_id || '') }),
          h('td', { style: 'color:var(--danger);white-space:normal;word-break:break-word;max-width:320px',
                   text: x.error ? String(x.error) : '—' }));
      }))));
  }
  statusSelect.onchange = paint;
  searchInput.oninput = paint;
  paint();
}

/** The latest version's declared parameters come from the OpenAPI catalogue (one source of truth with the
 * server). Older versions aren't in the catalogue, so their own query_parameters are used instead. */
var RESERVED = ['format', 'page', 'page_size', 'version', 'connection_name', 'timeout'];
async function fetchQueryParameters(name) {
  var spec = await apiJson('openapi.json');
  if (!spec) return null;
  var op = spec.paths && spec.paths['/q/' + enc(name)];
  return (op && op.get && op.get.parameters || []).filter(function (p) { return RESERVED.indexOf(p.name) === -1; });
}
function paramsFromVersion(v) {
  var qp = v.query_parameters || {};
  return Object.keys(qp).map(function (k) {
    var s = typeof qp[k] === 'string' ? { type: qp[k] } : (qp[k] || {});
    return { name: k, required: s.default === undefined && s.required !== false, description: s.description,
      schema: { type: s.type, default: s.default, minimum: s.min, maximum: s.max } };
  });
}

async function renderRunTab(body, f, v, isLatest) {
  body.appendChild(loadingNode('Loading parameters…'));
  var params = isLatest ? await fetchQueryParameters(f.filename) : null;
  if (!params) params = paramsFromVersion(v);
  if (selected.name !== f.filename || selected.tab !== 'run') return;
  clear(body);
  var inputs = {};
  var grid = h('div', { className: 'param-grid' }, params.map(function (p) {
    var sch = p.schema || {};
    var bounds = [sch.minimum !== undefined ? '≥ ' + sch.minimum : null, sch.maximum !== undefined ? '≤ ' + sch.maximum : null].filter(Boolean).join(' ');
    var input = h('input', { id: 'run-q-' + p.name, spellcheck: 'false', autocomplete: 'off',
      placeholder: sch.default !== undefined ? 'default: ' + sch.default : (p.required ? 'required' : ''),
      required: p.required ? true : null, type: sch.type === 'integer' || sch.type === 'int' ? 'number' : 'text' });
    inputs[p.name] = input;
    return field('run-q-' + p.name, h('span', { className: 'mono', text: p.name }), input,
      [p.description, bounds].filter(Boolean).join(' · ') || null,
      [sch.type ? h('span', { className: 'type', text: String(sch.type) }) : null, p.required ? h('span', { className: 'req', text: '*' }) : null]);
  }));
  var connSelect = h('select', { id: 'rq-connection', 'aria-label': 'Connection' });
  connectionOptions(connSelect, true, 'saved: ' + (v.connection_name || 'none'));
  var formatSelect = h('select', { id: 'rq-format', 'aria-label': 'Format' }, ['json', 'csv', 'tsv', 'xml', 'yaml', 'ndjson', 'xlsx'].map(function (x) { return h('option', { value: x, text: x }); }));
  formatSelect.value = prefs.format;
  var sizeSelect = h('select', { id: 'rq-page-size', 'aria-label': 'Page size' }, [10, 25, 50, 100, 500].map(function (n) { return h('option', { value: String(n), text: n + ' rows' }); }));
  var statusBox = h('div', { className: 'resbar' }), resultsBox = h('div', { className: 'res-body' });
  var resultsPanel = h('div', { className: 'panel' }, statusBox, resultsBox);
  resultsPanel.hidden = true;
  var page = 1;
  var runBtn = h('button', { type: 'submit', className: 'btn primary md', style: 'padding:0 16px', text: 'Run' });
  var url = 'q/' + enc(f.filename);
  async function run() {
    var query = params.map(function (p) {
      var val = inputs[p.name].value;
      return val !== '' ? enc(p.name) + '=' + enc(val) : null;
    }).filter(Boolean);
    query.push('format=' + formatSelect.value, 'page=' + page, 'page_size=' + sizeSelect.value);
    if (!isLatest) query.push('version=' + v.version);
    if (connSelect.value) query.push('connection_name=' + enc(connSelect.value));
    resultsPanel.hidden = false;
    await execute(function () { return apiFetch(url + '?' + query.join('&')); }, {
      results: resultsBox, status: statusBox, button: runBtn, page: page, pageSize: Number(sizeSelect.value),
      filename: f.filename, format: formatSelect.value,
      onPage: function (n) { page = n; run(); },
      onPageSize: function (n) { sizeSelect.value = String(n); page = 1; run(); }
    });
    refreshHistorySilently(f.filename);
  }
  var form = h('form', { novalidate: true, style: 'display:flex;flex-direction:column;gap:14px', onsubmit: function (e) { e.preventDefault(); page = 1; run(); } },
    params.length ? grid : h('div', { className: 'hint', text: 'No parameters — this query runs as-is.' }),
    h('div', { className: 'get-row' },
      h('span', { className: 'method', text: 'GET' }),
      h('span', { className: 'endpoint', title: '/' + url, text: '/' + url + (isLatest ? '' : '?version=' + v.version) }),
      connSelect, formatSelect, sizeSelect, runBtn));
  body.appendChild(form);
  body.appendChild(resultsPanel);
}
async function refreshHistorySilently(name) {
  var res;
  try { res = await apiFetch('list_files'); } catch (e) { return; }
  if (!res.ok) return;
  var data = await res.json();
  filesCache = (data.files || []).slice().sort(function (a, b) { return a.filename < b.filename ? -1 : 1; });
  renderQueryList();
  var f = findFile(name);
  if (!f || selected.name !== name) return;
  var v = f.versions.filter(function (x) { return x.version === selected.version; })[0];
  var badge = document.querySelector('#query-detail .subtabs button[data-subtab="history"] .count');
  if (v && badge) badge.textContent = String((v.execution_history || []).length || '');
}

async function renderCurlTab(body, f, v, isLatest) {
  body.appendChild(loadingNode('Loading parameters…'));
  var params = isLatest ? await fetchQueryParameters(f.filename) : null;
  if (!params) params = paramsFromVersion(v);
  if (selected.name !== f.filename || selected.tab !== 'curl') return;
  clear(body);
  var cmd = asSavedQueryCurl(f.filename, params, isLatest ? null : v.version);
  body.appendChild(h('pre', { className: 'curlbox', text: cmd }));
  body.appendChild(h('div', {}, h('button', { type: 'button', className: 'btn md', text: 'Copy command', onclick: function () { copyText(cmd); } })));
  if (params.length) body.appendChild(h('div', { className: 'hint', text: 'Replace the <placeholder> value(s) with real parameters before running it.' }));
}

async function openQueryForm(baseName, baseVersion) {
  var slot = openDrawer('query-form-slot', baseName ? 'New version' : 'New saved query',
    baseName ? baseName + ' · from v' + baseVersion.version : 'PATCH /save_sql_to_file');
  var prefill = baseVersion || {};
  var sql = '';
  if (baseName) {
    slot.appendChild(loadingNode());
    var c = await getContent(baseName);
    var d = c && c.parsed && c.parsed[String(baseVersion.version)];
    sql = mongoEditorText(d) !== null ? mongoEditorText(d) : (d && d.sql_query) || '';
    clear(slot);
  } else if (prefill.sql_query) {
    sql = prefill.sql_query; // e.g. a `SELECT * FROM <table>` starting point from the access map's "not yet exposed" prompt
  }
  function inp(id, value, ph, req) {
    return h('input', { id: id, value: value || '', placeholder: ph || null, required: req ? true : null, autocomplete: 'off', spellcheck: 'false' });
  }
  var connSelect = h('select', { id: 'q-connection' });
  connectionOptions(connSelect, true, '(none — must be given at run time)');
  if (prefill.connection_name) connSelect.value = prefill.connection_name;
  var qp = prefill.query_parameters && Object.keys(prefill.query_parameters).length ? JSON.stringify(prefill.query_parameters, null, 2) : '';
  var paramsTa = h('textarea', { id: 'q-params', spellcheck: 'false', placeholder: '{"id": {"type": "int", "min": 1}}' });
  paramsTa.value = qp;
  var detected = h('div', { className: 'refs' });
  var editor = makeEditor({ id: 'q-sql', required: true, placeholder: 'SELECT * FROM t WHERE id = :id' }, sql);
  var sqlTa = editor.querySelector('textarea');
  var sqlLabel = h('label', { for: 'q-sql' }, 'SQL', h('span', { className: 'type', text: 'use :name for bound parameters' }));
  function isFormMongo() { return (connectionsCache[connSelect.value] || {}).db === 'mongo'; }
  function paintFormMode() {
    var mongo = isFormMongo();
    clear(sqlLabel).appendChild(document.createTextNode(mongo ? 'Query' : 'SQL'));
    sqlLabel.appendChild(h('span', { className: 'type', text: mongo ? 'a find() filter document - use ":name" for bound parameters' : 'use :name for bound parameters' }));
    sqlTa.placeholder = mongo ? '{"collection": "orders", "filter": {"status": ":status"}}' : 'SELECT * FROM t WHERE id = :id';
  }
  function paintRefs() {
    clear(detected);
    var names = isFormMongo() ? mongoDocParams(sqlTa.value) : sqlParams(sqlTa.value);
    if (names.length) detected.appendChild(h('span', { className: 'hint', text: 'Bound in the query:' }));
    names.forEach(function (n) { detected.appendChild(h('span', { className: 'tag', text: ':' + n })); });
  }
  sqlTa.addEventListener('input', paintRefs);
  paintFormMode();
  paintRefs();
  var querySchema = schemaBrowser(function (text) { insertAtCursor(sqlTa, text); },
    function (tableName) { previewTable(connSelect.value, tableName); closeDrawer(); },
    function (table) { applySelectQuery(sqlTa, connSelect.value, table); paintRefs(); });
  querySchema.setConnection(connSelect.value);
  connSelect.onchange = function () { querySchema.setConnection(connSelect.value); paintFormMode(); paintRefs(); };
  var collectionInput = h('input', { id: 'q-collection', list: 'q-collection-list', placeholder: 'optional — e.g. reporting', autocomplete: 'off', spellcheck: 'false' });
  var collectionImpact = h('div', {});
  collectionInput.addEventListener('input', function () { clear(collectionImpact).appendChild(collectionInput.value.trim() ? impactNode(null, collectionInput.value.trim()) : h('span')); });
  var collectionField = h('div', { className: 'field' }, h('label', { for: 'q-collection' }, 'Collection'), collectionInput,
    h('datalist', { id: 'q-collection-list' }, collectionNames().map(function (c) { return h('option', { value: c }); })),
    h('div', { className: 'hint', text: 'Optional. Lowercase letters, digits, “.”, “_” and “-”. Later, use “Move…” to change it.' }), collectionImpact);
  var actions = formActions(baseName ? 'Save as v' + (latestOf(findFile(baseName) || { versions: [baseVersion] }).version + 1) : 'Save', closeDrawer);
  var form = h('form', { className: 'form', novalidate: true, onsubmit: function (e) { e.preventDefault(); saveQuery(actions.submit); } },
    field('q-filename', 'Filename', inp('q-filename', baseName, 'films_by_rating', true), 'Letters, digits, spaces, “.”, “_” and “-”. Saving an existing name adds a version.'),
    h('div', { className: 'grid2' }, field('q-author', 'Author', inp('q-author', prefill.author, '', true)), field('q-connection', 'Default connection', connSelect)),
    h('div', { className: 'field' }, sqlLabel, editor, detected),
    schemaField(querySchema),
    field('q-description', 'Description', inp('q-description', prefill.description, '', true)),
    baseName ? null : collectionField,
    field('q-tags', 'Tags', inp('q-tags', (prefill.tags || []).join(', '), 'comma-separated')),
    field('q-params', 'query_parameters', paramsTa, 'JSON object: name → type, or {type, min, max, default, description}.'),
    actions.node);
  slot.appendChild(form);
  (baseName ? sqlTa : $('q-filename')).focus();
}
/** A mongo saved version's editor text - the same {"collection", "filter", ...} JSON convention runMongo()
 * uses - or null when ``d`` isn't a mongo version (so the caller falls back to plain sql_query text). */
function mongoEditorText(d) {
  if (!d || d.query_type !== 'mongo') return null;
  var doc = { collection: d.mongo_collection, filter: d.mongo_filter || {} };
  if (d.mongo_projection) doc.projection = d.mongo_projection;
  if (d.mongo_sort) doc.sort = d.mongo_sort;
  return JSON.stringify(doc, null, 2);
}
/** What to show for a saved version, wherever the UI displays "the query": the mongo JSON convention, plain
 * sql_query text, or (a version so old/malformed neither applies) the raw file content as a last resort. */
function queryDisplayText(data, raw) {
  var mongo = mongoEditorText(data);
  if (mongo !== null) return mongo;
  return data && data.sql_query !== undefined && data.sql_query !== null ? String(data.sql_query) : raw;
}
$('new-query').onclick = function () { openQueryForm(); };

async function saveQuery(btn) {
  var paramsText = $('q-params').value.trim();
  var queryParameters = {};
  if (paramsText) {
    try { queryParameters = JSON.parse(paramsText); }
    catch (e) { showError('query_parameters is not valid JSON: ' + e.message, { errors: { query_parameters: e.message } }); return; }
  }
  var tags = $('q-tags').value.split(',').map(function (t) { return t.trim(); }).filter(Boolean);
  var filename = $('q-filename').value.trim();
  var isMongo = (connectionsCache[$('q-connection').value] || {}).db === 'mongo';
  var body = {
    filename: filename,
    author: $('q-author').value,
    description: $('q-description').value,
    tags: tags,
    connection_name: $('q-connection').value || undefined,
    query_parameters: queryParameters
  };
  if (isMongo) {
    var doc;
    try { doc = $('q-sql').value.trim() ? JSON.parse($('q-sql').value) : {}; }
    catch (e) { showError('The query must be valid JSON: ' + e.message, { errors: { sql_query: e.message } }); return; }
    if (!doc || typeof doc !== 'object' || Array.isArray(doc) || !doc.collection) {
      showError('The query must be a JSON object with a "collection" field, e.g. {"collection": "users", "filter": {}}.',
               { errors: { sql_query: 'collection is required' } });
      return;
    }
    body.query_type = 'mongo';
    body.mongo_collection = doc.collection;
    body.mongo_filter = doc.filter || {};
    if (doc.projection) body.mongo_projection = doc.projection;
    if (doc.sort) body.mongo_sort = doc.sort;
  } else {
    body.sql_query = $('q-sql').value;
  }
  var collectionEl = $('q-collection');
  var chosen = collectionEl ? collectionEl.value.trim() : '';
  if (chosen) {
    var already = findFile(filename);
    if (already && (already.collection || '') !== chosen) {
      showError('“' + filename + '” already exists' + (already.collection ? ' in collection ' + already.collection : ' with no collection') + '. Save it without a collection here, then use “Move…” — that shows which keys gain or lose access first.');
      return;
    }
    if (!already) body.collection = chosen;
  }
  btn.disabled = true;
  var res = await apiJson('save_sql_to_file', { method: 'PATCH', json: body });
  btn.disabled = false;
  if (res) {
    showError(''); closeDrawer();
    toast('Saved ' + filename + (res.version ? ' v' + res.version : ''));
    selected.version = res.version || null;
    showTab('queries');
    loadQueries(filename);
  }
}
async function downloadPostman(name) {
  var res;
  try { res = await apiFetch('collections/' + enc(name) + '/postman'); }
  catch (e) { showError('Network error: ' + e.message); return; }
  if (!res.ok) {
    var body = null; try { body = await res.json(); } catch (e) { /* not JSON */ }
    showError(errorMessage(res.status, body, ''), { status: res.status });
    return;
  }
  showError('');
  downloadBlob(await res.blob(), name + '.postman_collection.json');
  toast('Downloaded ' + name + '.postman_collection.json — set its apiKey variable after importing');
}
/** A collection exists only while a query is in it, so "new collection" means choosing its first queries. */
function openNewCollectionForm() {
  var slot = openDrawer('query-form-slot', 'New collection', 'file queries under a new group');
  var NAME_RE = /^[a-z0-9][a-z0-9._-]{0,62}$/;
  var nameInput = h('input', { id: 'nc-name', placeholder: 'e.g. reporting', autocomplete: 'off', spellcheck: 'false' });
  var nameHint = h('div', { className: 'hint', text: 'Lowercase letters, digits, “.”, “_” and “-”.' });
  var checks = {};
  var rows = filesCache.slice().sort(function (a, b) {
    return (a.collection ? 1 : 0) - (b.collection ? 1 : 0) || (a.filename < b.filename ? -1 : 1);
  }).map(function (f) {
    var cb = h('input', { type: 'checkbox' });
    checks[f.filename] = { box: cb, from: f.collection || null };
    cb.onchange = paint;
    return h('label', { className: 'switch', style: 'font-weight:400' }, cb, f.filename,
      f.collection ? h('span', { className: 'dim', text: ' (now in ' + f.collection + ')' }) : null);
  });
  var list = h('div', { className: 'tags', style: 'flex-direction:column;gap:6px' }, rows.length ? rows
    : h('span', { className: 'hint', text: 'No saved queries yet — create one first; a collection exists only while a query is in it.' }));
  var impact = h('div', {});
  var actions = formActions('Create collection', closeDrawer);
  function picked() { return Object.keys(checks).filter(function (n) { return checks[n].box.checked; }); }
  function paint() {
    var name = nameInput.value.trim();
    var existing = !!collectionsCache.collections[name];
    nameHint.textContent = !name ? 'Lowercase letters, digits, “.”, “_” and “-”.'
      : !NAME_RE.test(name) ? 'Not a valid name — use lowercase letters, digits, “.”, “_” and “-”, starting with a letter or digit.'
      : existing ? '“' + name + '” already exists — the ticked queries will be added to it.' : '';
    var chosen = picked();
    clear(impact);
    if (name && NAME_RE.test(name) && chosen.length) {
      impact.appendChild(impactNodeMany(chosen.map(function (n) { return [checks[n].from, name]; })));
    }
    actions.submit.disabled = !(NAME_RE.test(name) && chosen.length);
  }
  nameInput.oninput = paint;
  slot.appendChild(h('form', { className: 'form', novalidate: true, onsubmit: async function (e) {
    e.preventDefault();
    var name = nameInput.value.trim();
    var chosen = picked();
    if (!NAME_RE.test(name) || !chosen.length) return;
    actions.submit.disabled = true;
    var done = 0;
    for (var i = 0; i < chosen.length; i++) {
      var res = await apiJson('saved_sql/' + enc(chosen[i]) + '/collection', { method: 'PUT', json: { collection: name } });
      if (!res) break;  // apiJson has already shown why
      done++;
    }
    if (done === chosen.length) { showError(''); closeDrawer(); toast('Created ' + name + ' with ' + done + (done === 1 ? ' query' : ' queries')); }
    else { showError('Moved ' + done + ' of ' + chosen.length + ' queries into ' + name + ' before it stopped — the rest are unchanged.'); actions.submit.disabled = false; }
    if (done) loadQueries(selected.name);
  } },
    h('div', { className: 'hint', text: 'A collection exists only while a query is in it, so choose its first queries now. Moving them is not a new version.' }),
    field('nc-name', 'Name', nameInput, null), nameHint,
    h('div', { className: 'field' }, h('label', {}, 'Queries to file under it'), list),
    impact, actions.node));
  paint();
  nameInput.focus();
}
$('new-collection').onclick = openNewCollectionForm;
function openMoveForm(f) {
  var current = f.collection || '';
  var slot = openDrawer('query-form-slot', 'Move to a collection', f.filename);
  var select = h('select', { id: 'mv-select' }, h('option', { value: '', text: 'No collection' }),
    collectionNames().map(function (c) { return h('option', { value: c, text: c + ' (' + collectionsCache.collections[c].queries.length + ')' }); }),
    h('option', { value: '__new__', text: 'New collection…' }));
  select.value = current;
  var newInput = h('input', { id: 'mv-new', placeholder: 'e.g. reporting', autocomplete: 'off', spellcheck: 'false' });
  var newField = field('mv-new', 'New collection name', newInput, 'Lowercase letters, digits, “.”, “_” and “-”.');
  var impact = h('div', {});
  function target() { return select.value === '__new__' ? newInput.value.trim() : select.value; }
  function paint() {
    newField.style.display = select.value === '__new__' ? '' : 'none';
    clear(impact).appendChild(target() === current ? h('span', { className: 'hint', text: 'No change.' }) : impactNode(current, target()));
  }
  select.onchange = paint; newInput.oninput = paint;
  var actions = formActions('Move', closeDrawer);
  slot.appendChild(h('form', { className: 'form', novalidate: true, onsubmit: async function (e) {
    e.preventDefault();
    var to = target();
    if (select.value === '__new__' && !to) { showError('Enter a name for the new collection.'); newInput.focus(); return; }
    if (to === current) { closeDrawer(); return; }
    actions.submit.disabled = true;
    var res = await apiJson('saved_sql/' + enc(f.filename) + '/collection', { method: 'PUT', json: { collection: to || null } });
    actions.submit.disabled = false;
    if (res) { showError(''); closeDrawer(); toast(f.filename + (to ? ' → ' + to : ' removed from its collection')); loadQueries(f.filename); }
  } },
    h('div', { className: 'hint', text: 'Moving a query is not a new version. A key granted a collection can run every query in it — so this changes what other keys can reach:' }),
    field('mv-select', 'Collection', select), newField, impact, actions.node));
  paint();
}
function openRenameCollectionForm(name) {
  var known = collectionsCache.collections[name] || { queries: [], keys: [], roles: [] };
  var slot = openDrawer('query-form-slot', 'Rename collection', name);
  var input = h('input', { id: 'rn-name', value: name, autocomplete: 'off', spellcheck: 'false' });
  var mergeBox = h('input', { id: 'rn-merge', type: 'checkbox' });
  var mergeRow = h('label', { className: 'switch' }, mergeBox, 'Merge into the existing collection', h('span', { className: 'hint', text: '— also how to finish a rename that was interrupted part-way' }));
  function paint() { mergeRow.style.display = input.value.trim() !== name && collectionsCache.collections[input.value.trim()] ? '' : 'none'; }
  input.oninput = paint;
  var actions = formActions('Rename', closeDrawer);
  slot.appendChild(h('form', { className: 'form', novalidate: true, onsubmit: async function (e) {
    e.preventDefault();
    var to = input.value.trim();
    if (!to || to === name) { showError('Enter a different name.'); input.focus(); return; }
    if (collectionsCache.collections[to] && !mergeBox.checked) { showError('“' + to + '” already exists — tick “Merge” to combine them.'); return; }
    actions.submit.disabled = true;
    var res = await apiJson('collections/' + enc(name), { method: 'PATCH', json: { name: to, merge: mergeBox.checked } });
    actions.submit.disabled = false;
    if (res) { showError(''); closeDrawer(); toast('Renamed ' + name + ' → ' + to); loadQueries(selected.name); loadApiKeys(); loadRoles(); }
  } },
    h('div', { className: 'hint', text: 'Renames ' + known.queries.length + (known.queries.length === 1 ? ' query' : ' queries') + ' and updates ' + known.keys.length + (known.keys.length === 1 ? ' key' : ' keys') + ' and ' + known.roles.length + (known.roles.length === 1 ? ' role' : ' roles') + ' that are granted it. No key loses access at any point.' }),
    field('rn-name', 'New name', input), mergeRow, actions.node));
  paint();
  input.focus(); input.select();
}
async function deleteQuery(name) {
  if (!confirm("Delete saved query '" + name + "' (all versions)?")) return;
  var res = await apiJson('saved_sql/' + enc(name), { method: 'DELETE' });
  if (res) { toast('Deleted ' + name); loadQueries(null); }
}
async function deleteQueryVersion(name, version, count) {
  var msg = "Delete version " + version + " of '" + name + "'?" + (count === 1 ? ' It is the only version, so the query itself will be removed.' : '');
  if (!confirm(msg)) return;
  var res = await apiJson('saved_sql/' + enc(name) + '?version=' + enc(String(version)), { method: 'DELETE' });
  if (res) { toast('Deleted ' + name + ' v' + version); selected.version = null; loadQueries(count === 1 ? null : name); }
}

// ---- shared execution + result rendering (saved-query runs and Run SQL) ----
function copyText(text) {
  if (navigator.clipboard) navigator.clipboard.writeText(text).then(function () { toast('Copied'); }, function () { toast('Copy failed'); });
}
function downloadBlob(blob, name) {
  var a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(function () { URL.revokeObjectURL(a.href); }, 5000);
}
var EXT = { json: 'json', ndjson: 'ndjson', csv: 'csv', tsv: 'tsv', xml: 'xml', yaml: 'yaml', xlsx: 'xlsx' };

async function execute(doFetch, o) {
  showError('');
  clear(o.status);
  clear(o.results).appendChild(h('div', { className: 'res-note' }, h('span', { className: 'spin' }), 'Running…'));
  if (o.button) o.button.disabled = true;
  var t0 = performance.now(), res;
  try { res = await doFetch(); }
  catch (e) {
    if (o.button) o.button.disabled = false;
    clear(o.results).appendChild(h('div', { className: 'res-note err', text: 'Network error: ' + e.message }));
    return;
  }
  o.elapsed = Math.round(performance.now() - t0);
  await renderResponse(res, o);
  if (o.button) o.button.disabled = false;
}

async function renderResponse(res, o) {
  var box = clear(o.results), bar = clear(o.status);
  var contentType = (res.headers.get('content-type') || '').split(';')[0].trim();
  if (!res.ok) {
    var text = await res.text(), body = null;
    try { body = JSON.parse(text); } catch (e) {}
    var msg = errorMessage(res.status, body, text);
    showError(msg, { status: res.status, errors: body && body.errors });
    box.appendChild(h('div', { className: 'res-note err' }, h('span', { className: 'eb-code', text: String(res.status) }), msg));
    return;
  }
  var page = Number(res.headers.get('x-page')) || o.page || 1;
  var size = Number(res.headers.get('x-page-size')) || o.pageSize || 10;
  var hasMore = res.headers.get('x-has-more') === 'true';
  var base = (o.filename || 'result') + (page > 1 ? '-p' + page : '');
  var payload = null, rowCount = null, format = o.format || 'json', tableData = null;
  if (contentType === 'application/json') {
    var data = await res.json();
    payload = JSON.stringify(data, null, 2);
    if (Array.isArray(data)) { rowCount = data.length; tableData = data; renderTable(box, data, (page - 1) * size); }
    else box.appendChild(h('div', { className: 'res-pre jt-root' }, jsonTree(data)));
  } else if (contentType.indexOf('text/') === 0 || contentType === 'application/xml' || contentType === 'application/x-yaml' || contentType === 'application/x-ndjson') {
    payload = await res.text();
    box.appendChild(h('pre', { className: 'res-pre', text: payload }));
  } else {
    var blob = await res.blob();
    var fname = base + '.' + (EXT[format] || 'bin');
    downloadBlob(blob, fname);
    box.appendChild(h('div', { className: 'res-note' }, 'Downloaded ', h('code', { text: fname }), ' (' + (blob.size < 1024 ? blob.size + ' B' : (blob.size / 1024).toFixed(1) + ' KB') + ')',
      h('button', { type: 'button', className: 'btn sm', text: 'Download again', onclick: function () { downloadBlob(blob, fname); } })));
  }
  var stat = h('span', { className: 'stat' },
    h('span', {}, h('b', { className: 'stat-ok', text: String(res.status) }), res.statusText ? ' ' + res.statusText : ''),
    rowCount !== null ? h('span', {}, h('b', { text: String(rowCount) }), rowCount === 1 ? ' row' : ' rows') : null,
    rowCount ? h('span', { text: 'rows ' + ((page - 1) * size + 1) + '–' + ((page - 1) * size + rowCount) }) : null,
    h('span', { text: format }),
    o.elapsed !== undefined ? h('span', { text: o.elapsed + ' ms' }) : null);
  bar.appendChild(stat);
  bar.appendChild(h('span', { className: 'spacer' }));
  if (o.onPage) bar.appendChild(pager(page, size, hasMore, o));
  var headersBox = headersPanel(res);
  headersBox.hidden = true;
  bar.appendChild(h('button', { type: 'button', className: 'btn sm ghost', text: 'Headers', onclick: function () { headersBox.hidden = !headersBox.hidden; } }));
  if (payload !== null) {
    bar.appendChild(h('button', { type: 'button', className: 'btn sm ghost', text: 'Copy', onclick: function () { copyText(payload); } }));
    bar.appendChild(h('button', { type: 'button', className: 'btn sm ghost', text: 'Download', onclick: function () {
      downloadBlob(new Blob([payload], { type: contentType || 'text/plain' }), base + '.' + (EXT[format] || 'txt')); } }));
  }
  if (tableData) bar.appendChild(h('button', { type: 'button', className: 'btn sm ghost', text: 'Copy as TSV', onclick: function () { copyText(rowsToTsv(tableData)); } }));
  if (o.sql !== undefined || o.mongo) bar.appendChild(h('button', { type: 'button', className: 'btn sm ghost', text: 'Copy as curl', onclick: function () { copyText(asCurl(o)); } }));
  var chartPanel = null;
  if (tableData && tableData.length && numericColumns(tableData).length) {
    chartPanel = buildChartPanel(tableData);
    chartPanel.hidden = true;
    bar.appendChild(h('button', { type: 'button', className: 'btn sm ghost', text: 'Chart', onclick: function () { chartPanel.hidden = !chartPanel.hidden; } }));
  }
  box.appendChild(headersBox);
  if (chartPanel) box.appendChild(chartPanel);
}

/** A collapsible list of every header the response actually carries - X-Page, X-RateLimit-*, X-Request-Id,
 * X-Cache, ETag, and whatever else the server sends, not a hand-picked subset. */
function headersPanel(res) {
  var rows = [];
  res.headers.forEach(function (v, k) { rows.push([k, v]); });
  rows.sort(function (a, b) { return a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0; });
  return h('div', { className: 'panel headers-panel' }, h('table', { className: 'grid' },
    h('tbody', {}, rows.map(function (r) { return h('tr', {}, h('td', { className: 'mono dim' }, r[0]), h('td', { className: 'mono' }, r[1])); }))));
}

function columnsOf(rows) {
  var cols = [], seen = {};
  rows.forEach(function (r) { Object.keys(r || {}).forEach(function (c) { if (!seen[c]) { seen[c] = true; cols.push(c); } }); });
  return cols;
}

function rowsToTsv(rows) {
  var cols = columnsOf(rows);
  var lines = [cols.join('\t')];
  rows.forEach(function (row) {
    lines.push(cols.map(function (c) {
      var v = row[c];
      if (v === null || v === undefined) return '';
      var s = typeof v === 'object' ? JSON.stringify(v) : String(v);
      return s.replace(/[\t\n\r]/g, ' ');
    }).join('\t'));
  });
  return lines.join('\n');
}

// ---- quick chart: a bar chart of the current page only, never implying a full-result view ----
var CHART_MAX_BARS = 50;

function numericColumns(rows) {
  return columnsOf(rows).filter(function (c) {
    var any = false;
    var allNumericOrNull = rows.every(function (r) {
      var v = r[c];
      if (v === null || v === undefined) return true;
      if (typeof v === 'number' && isFinite(v)) { any = true; return true; }
      return false;
    });
    return any && allNumericOrNull;
  });
}

function buildChartPanel(tableData) {
  var cols = columnsOf(tableData);
  var numCols = numericColumns(tableData);
  var labelCol = cols.filter(function (c) { return numCols.indexOf(c) === -1; })[0] || cols[0];
  var valueCol = numCols[0];
  var shown = tableData.slice(0, CHART_MAX_BARS);
  var labelSel = h('select', { 'aria-label': 'Label column' },
    cols.map(function (c) { return h('option', { value: c, text: c, selected: c === labelCol ? true : null }); }));
  var valueSel = h('select', { 'aria-label': 'Value column' },
    numCols.map(function (c) { return h('option', { value: c, text: c, selected: c === valueCol ? true : null }); }));
  var svgSlot = h('div', { className: 'chart-svg-slot' });
  function repaint() { clear(svgSlot).appendChild(barChartSvg(shown, labelSel.value, valueSel.value)); }
  labelSel.onchange = repaint;
  valueSel.onchange = repaint;
  repaint();
  return h('div', { className: 'panel chart-panel' },
    h('div', { className: 'chart-toolbar' },
      h('span', { className: 'hint', text: 'Chart of this page only (' + shown.length +
        (shown.length < tableData.length ? ' of ' + tableData.length : '') +
        (shown.length === 1 ? ' row' : ' rows') + ') — not the full result.' }),
      h('span', { className: 'spacer' }),
      h('label', { className: 'switch', style: 'font-weight:400' }, 'Label', labelSel),
      h('label', { className: 'switch', style: 'font-weight:400' }, 'Value', valueSel)),
    svgSlot);
}

function barChartSvg(rows, labelCol, valueCol) {
  var NS = 'http://www.w3.org/2000/svg';
  var data = rows.map(function (r) {
    var v = r[valueCol];
    return { label: r[labelCol] === null || r[labelCol] === undefined ? '' : String(r[labelCol]),
            value: typeof v === 'number' && isFinite(v) ? v : 0 };
  });
  var width = 680, height = 220, pad = { top: 10, right: 10, bottom: 30, left: 46 };
  var innerW = width - pad.left - pad.right, innerH = height - pad.top - pad.bottom;
  var maxVal = Math.max.apply(null, data.map(function (d) { return d.value; }).concat([0]));
  var minVal = Math.min.apply(null, data.map(function (d) { return d.value; }).concat([0]));
  var range = (maxVal - minVal) || 1;
  var zeroY = pad.top + innerH * (maxVal / range);
  var gap = 4;
  var barW = data.length ? Math.max(2, (innerW - gap * (data.length - 1)) / data.length) : 0;

  var svg = document.createElementNS(NS, 'svg');
  svg.setAttribute('viewBox', '0 0 ' + width + ' ' + height);
  svg.setAttribute('preserveAspectRatio', 'xMinYMin meet');

  var axis = document.createElementNS(NS, 'line');
  axis.setAttribute('x1', pad.left); axis.setAttribute('x2', width - pad.right);
  axis.setAttribute('y1', zeroY); axis.setAttribute('y2', zeroY);
  axis.setAttribute('class', 'chart-axis');
  svg.appendChild(axis);

  data.forEach(function (d, i) {
    var barH = innerH * (Math.abs(d.value) / range);
    var x = pad.left + i * (barW + gap);
    var y = d.value >= 0 ? zeroY - barH : zeroY;
    var rect = document.createElementNS(NS, 'rect');
    rect.setAttribute('x', x); rect.setAttribute('y', y);
    rect.setAttribute('width', barW); rect.setAttribute('height', Math.max(0, barH));
    rect.setAttribute('class', 'chart-bar');
    var title = document.createElementNS(NS, 'title');
    title.textContent = d.label + ': ' + d.value;
    rect.appendChild(title);
    svg.appendChild(rect);
    if (barW > 16) {
      var label = document.createElementNS(NS, 'text');
      label.setAttribute('x', x + barW / 2);
      label.setAttribute('y', height - pad.bottom + 13);
      label.setAttribute('class', 'chart-label chart-label-x');
      label.textContent = d.label.length > 9 ? d.label.slice(0, 8) + '…' : d.label;
      svg.appendChild(label);
    }
  });

  [[maxVal, pad.top + 8], [minVal, height - pad.bottom + 3]].forEach(function (pair) {
    var label = document.createElementNS(NS, 'text');
    label.setAttribute('x', 2); label.setAttribute('y', pair[1]);
    label.setAttribute('class', 'chart-label chart-label-y');
    label.textContent = String(pair[0]);
    svg.appendChild(label);
  });

  return svg;
}

function shQuote(s) { return "'" + String(s).replace(/'/g, "'\\''") + "'"; }
/** o.sql/o.connection/o.params (or, for a mongo connection, o.mongoBody) come from runSql()/runMongo()'s
 * call to execute() - ad-hoc runs only, not saved-query ones. The API key, if any, is a placeholder rather
 * than the real value: this text is meant to be copied out of the browser (to a terminal, a ticket, a
 * chat), and the key typed into this page shouldn't ride along by default. */
function asCurl(o) {
  var qs = 'format=' + enc(o.format) + '&page=' + o.page + '&page_size=' + o.pageSize + (o.timeout ? '&timeout=' + enc(o.timeout) : '');
  var url = new URL((o.mongo ? 'execute_mongo' : 'execute_sql') + '?' + qs, location.href).href;
  var body = JSON.stringify(o.mongo ? o.mongoBody : { sql: o.sql, connection_name: o.connection, params: o.params || {} });
  var lines = ['curl -X POST ' + shQuote(url), "  -H 'Content-Type: application/json'"];
  if (getKey()) lines.push("  -H 'X-API-Key: YOUR_KEY_HERE'  # replace with your own key");
  lines.push('  -d ' + shQuote(body));
  return lines.join(' \\\n');
}
/** A saved query's own GET /q/<name> endpoint as a curl command - readable as a reference, not necessarily
 * runnable as-is: a parameter with no declared default becomes a <name> placeholder to fill in rather than
 * a real value, since none is on hand outside the Run tab's own form. Same API-key placeholder policy as
 * asCurl() above. */
function asSavedQueryCurl(name, params, version) {
  // Built as a plain string, not new URL(...).href, since a <placeholder> value would otherwise come back
  // percent-encoded (%3Cid%3E) - unreadable for a command that's meant to be read and edited, not run as-is.
  var query = params.map(function (p) {
    var sch = p.schema || {};
    return sch.default !== undefined ? enc(p.name) + '=' + enc(String(sch.default)) : enc(p.name) + '=<' + p.name + '>';
  });
  query.push('format=json');
  if (version) query.push('version=' + version);
  var base = new URL('q/' + enc(name), location.href).href;
  var url = query.length ? base + '?' + query.join('&') : base;
  var lines = ['curl ' + shQuote(url)];
  if (getKey()) lines.push("  -H 'X-API-Key: YOUR_KEY_HERE'  # replace with your own key");
  return lines.join(' \\\n');
}

/** A collapsible tree for a JSON value that isn't a row array - the same idea browser devtools or `jq`
 * give for free, instead of a flat stringified dump. */
function jsonNode(entries, bracketOpen, bracketClose) {
  var open = true;
  var toggle = h('button', { type: 'button', className: 'jt-toggle', text: '▾', 'aria-label': 'Collapse' });
  var items = h('div', { className: 'jt-children' }, entries.map(function (pair) {
    return h('div', { className: 'jt-item' }, h('span', { className: 'jt-key', text: pair[0] + ': ' }), jsonTree(pair[1]));
  }));
  toggle.onclick = function () {
    open = !open;
    items.hidden = !open;
    toggle.textContent = open ? '▾' : '▸';
    toggle.setAttribute('aria-label', open ? 'Collapse' : 'Expand');
  };
  return h('div', { className: 'jt-node' },
    h('div', { className: 'jt-head' }, toggle, h('span', { className: 'jt-punct', text: bracketOpen + entries.length + bracketClose })),
    items);
}
function jsonTree(value) {
  if (value === null || value === undefined) return h('span', { className: 'jt-null', text: 'null' });
  if (Array.isArray(value)) {
    if (!value.length) return h('span', { className: 'jt-punct', text: '[]' });
    return jsonNode(value.map(function (v, i) { return [i, v]; }), '[', ']');
  }
  if (typeof value === 'object') {
    var keys = Object.keys(value);
    if (!keys.length) return h('span', { className: 'jt-punct', text: '{}' });
    return jsonNode(keys.map(function (k) { return [k, value[k]]; }), '{', '}');
  }
  if (typeof value === 'string') return h('span', { className: 'jt-str', text: JSON.stringify(value) });
  return h('span', { className: 'jt-num', text: String(value) });
}

function pager(page, size, hasMore, o) {
  var pageInput = h('input', { type: 'number', min: '1', value: String(page), 'aria-label': 'Page', onchange: function () {
    var n = Math.max(1, Number(pageInput.value) || 1); o.onPage(n); } });
  var presets = [10, 25, 50, 100, 500];
  if (presets.indexOf(size) === -1) presets.push(size);
  presets.sort(function (a, b) { return a - b; });
  var sizeSelect = h('select', { 'aria-label': 'Page size', onchange: function () { o.onPageSize(Number(sizeSelect.value)); } },
    presets.map(function (n) { return h('option', { value: String(n), text: n + ' / page' }); }));
  sizeSelect.value = String(size);
  return h('div', { className: 'pager' },
    h('button', { type: 'button', className: 'btn sm', text: '‹ Prev', disabled: page <= 1, onclick: function () { o.onPage(page - 1); } }),
    h('span', { className: 'hint', text: 'Page' }), pageInput,
    h('button', { type: 'button', className: 'btn sm', text: hasMore ? 'Next page ›' : 'Next ›', disabled: !hasMore, title: hasMore ? 'X-Has-More: true' : 'No more rows', onclick: function () { o.onPage(page + 1); } }),
    sizeSelect);
}

function renderTable(box, rows, offset) {
  if (!rows.length) { box.appendChild(h('div', { className: 'res-note', text: 'No rows returned.' })); return; }
  var cols = [], seen = {};
  rows.forEach(function (r) { Object.keys(r || {}).forEach(function (c) { if (!seen[c]) { seen[c] = true; cols.push(c); } }); });
  var numeric = {};
  cols.forEach(function (c) {
    numeric[c] = rows.every(function (r) { var v = r[c]; return v === null || v === undefined || typeof v === 'number'; })
      && rows.some(function (r) { return typeof r[c] === 'number'; });
  });
  box.appendChild(h('div', { className: 'table-wrap' }, h('table', { className: 'rs' },
    h('thead', {}, h('tr', {}, h('th', { className: 'rn', text: '#' }), cols.map(function (c) { return h('th', { className: numeric[c] ? 'num' : null, text: c }); }))),
    h('tbody', {}, rows.map(function (row, i) {
      return h('tr', {}, h('td', { className: 'rn', text: String(offset + i + 1) }), cols.map(function (c) {
        var v = row[c];
        if (v === null || v === undefined) return h('td', {}, h('span', { className: 'null', text: 'NULL' }));
        var s = typeof v === 'object' ? JSON.stringify(v) : String(v);
        return h('td', { className: numeric[c] ? 'num' : null, title: s.length > 40 ? s : null, text: s });
      }));
    })))));
}

// ---- Run SQL tab ----
var runPage = 1;
function paintRunRefs(sql) {
  var box = clear($('run-refs'));
  var names = sqlParams(sql);
  if (!names.length) return;
  box.appendChild(h('span', { className: 'hint', text: 'In SQL:' }));
  names.forEach(function (n) { box.appendChild(h('span', { className: 'tag', text: ':' + n })); });
}
bindEditor($('run-sql'), $('run-sql-hl'), paintRunRefs, $('run-sql-gutter'));

// ---- Run SQL: double-click a column (or its value) in the SQL editor to turn it into a bound parameter ----
// col = 'literal', col = 123, col = NULL/TRUE/FALSE, and the comparison operators <>/<=/>=/!=/</> as well as
// =. A double-click's native word-selection is what tells us which column - textarea.selectionStart/End
// after that native selection, not any pixel-position guessing - so this only needs a plain <textarea>, the
// same trick sqlParams()/highlightInto() already lean on elsewhere. Scoped to a single line, both to keep
// the regex simple and because a real WHERE condition is realistically never split use across lines in a way
// this needs to follow. Mongo's find() filter is JSON, not "col = value" SQL, so the regex below simply never
// matches there - no separate dialect check needed, double-clicking inside a mongo query is just a no-op.
// The trailing \b only guards the number/NULL/TRUE/FALSE branch (so "5" never matches inside "50") - it
// cannot also sit after the quoted-string branch, since a closing quote is itself a non-word character and
// \b never matches non-word-to-non-word (e.g. quote-to-space, or quote-to-end-of-line), which would make
// every quoted value fail to match at all.
var PARAMIZE_CANDIDATE_RE = /([A-Za-z_]\w*)\s*(=|!=|<>|<=|>=|<|>)\s*('(?:[^']|'')*'|(?:-?\d+(?:\.\d+)?|NULL|TRUE|FALSE)\b)/gid;
function literalToJsValue(text) {
  if (/^null$/i.test(text)) return null;
  if (/^true$/i.test(text)) return true;
  if (/^false$/i.test(text)) return false;
  if (/^-?\d+(\.\d+)?$/.test(text)) return Number(text);
  if (text[0] === "'" && text[text.length - 1] === "'") return text.slice(1, -1).replace(/''/g, "'");
  return text;
}
/** Whichever `col OP literal` match (if any) on the double-clicked word's line covers the click - column or
 * value clicked, either way the whole thing is described the same way: `name` (the column) and the literal
 * value's own span to replace with `:name`. */
function paramizeCandidateAt(text, clickStart, clickEnd) {
  var lineStart = text.lastIndexOf('\n', clickStart - 1) + 1;
  var lineEnd = text.indexOf('\n', clickEnd);
  if (lineEnd === -1) lineEnd = text.length;
  var line = text.slice(lineStart, lineEnd);
  var offset = clickStart - lineStart, endOffset = clickEnd - lineStart;
  PARAMIZE_CANDIDATE_RE.lastIndex = 0;
  var m;
  while ((m = PARAMIZE_CANDIDATE_RE.exec(line))) {
    var identRange = m.indices[1], valueRange = m.indices[3];
    var hit = (offset < identRange[1] && endOffset > identRange[0]) || (offset < valueRange[1] && endOffset > valueRange[0]);
    if (hit) {
      return { name: m[1], valueText: m[3], absStart: lineStart + valueRange[0], absEnd: lineStart + valueRange[1] };
    }
  }
  return null;
}
function openParameterizePopover(x, y, candidate, textarea, paramsTa) {
  var old = document.getElementById('paramize-popover');
  if (old) old.remove();
  var box = h('div', { id: 'paramize-popover', className: 'paramize-popover' },
    h('div', { className: 'preview' }, candidate.name + ' = ' + candidate.valueText + '  →  ' + candidate.name + ' = ',
      h('b', { text: ':' + candidate.name })),
    h('button', { type: 'button', text: 'Parameterize', onclick: function () {
      close();
      var before = textarea.value.slice(0, candidate.absStart), after = textarea.value.slice(candidate.absEnd);
      textarea.value = before + ':' + candidate.name + after;
      var caret = candidate.absStart + 1 + candidate.name.length;
      textarea.selectionStart = textarea.selectionEnd = caret;
      if (textarea.repaint) textarea.repaint();
      textarea.focus();
      var params = {};
      try { params = paramsTa.value.trim() ? JSON.parse(paramsTa.value) : {}; } catch (e) { params = {}; }
      params[candidate.name] = literalToJsValue(candidate.valueText);
      paramsTa.value = JSON.stringify(params, null, 2);
    } }));
  function close() { box.remove(); document.removeEventListener('mousedown', outside, true); document.removeEventListener('keydown', esc, true); }
  function outside(e) { if (!box.contains(e.target)) close(); }
  function esc(e) { if (e.key === 'Escape') close(); }
  document.body.appendChild(box);
  var left = Math.min(x, window.innerWidth - box.offsetWidth - 12);
  box.style.left = Math.max(8, left) + 'px';
  box.style.top = (y + 12) + 'px';
  setTimeout(function () { document.addEventListener('mousedown', outside, true); document.addEventListener('keydown', esc, true); }, 0);
}
$('run-sql').addEventListener('dblclick', function (e) {
  var start = this.selectionStart, end = this.selectionEnd;
  if (start === end) return; // double-click landed on whitespace/punctuation - nothing was selected
  var candidate = paramizeCandidateAt(this.value, start, end);
  if (candidate) openParameterizePopover(e.clientX, e.clientY, candidate, this, $('run-params'));
});

// ---- Run SQL: sidebar tabs (Recent queries / Query settings) ----
(function () {
  var SIDE_TAB_KEY = 'queryapigate-ui-run-side-tab';
  var tabs = Array.prototype.slice.call(document.querySelectorAll('.side-tabs .side-tab'));
  var panels = {};
  document.querySelectorAll('.side-tab-panel').forEach(function (p) { panels[p.dataset.sidePanel] = p; });
  function selectSideTab(name) {
    if (!panels[name]) return;
    tabs.forEach(function (t) {
      var on = t.dataset.sideTab === name;
      t.classList.toggle('active', on);
      t.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    Object.keys(panels).forEach(function (k) { panels[k].hidden = k !== name; });
    try { localStorage.setItem(SIDE_TAB_KEY, name); } catch (e) {}
  }
  tabs.forEach(function (t) { t.onclick = function () { selectSideTab(t.dataset.sideTab); }; });
  var saved = null;
  try { saved = localStorage.getItem(SIDE_TAB_KEY); } catch (e) {}
  selectSideTab(saved && panels[saved] ? saved : 'recent');
})();

// ---- Run SQL: draggable splitter between the editor and the sidebar (recent queries, params, schema) ----
(function () {
  var RUNNER_SIDE_W_KEY = 'queryapigate-ui-runner-side-w';
  var MIN_SIDE_W = 220, MAX_SIDE_W = 560, MIN_MAIN_W = 320;
  var splitter = $('run-splitter'), runner = document.querySelector('.runner');
  function setSideWidth(px) { runner.style.setProperty('--runner-side-w', px + 'px'); }
  try {
    var saved = Number(localStorage.getItem(RUNNER_SIDE_W_KEY));
    if (saved) setSideWidth(Math.max(MIN_SIDE_W, Math.min(MAX_SIDE_W, saved)));
  } catch (e) {}
  var dragging = false, startX = 0, startW = 0;
  splitter.addEventListener('pointerdown', function (e) {
    dragging = true; startX = e.clientX; startW = runner.querySelector('.runner-side').getBoundingClientRect().width;
    splitter.classList.add('dragging');
    splitter.setPointerCapture(e.pointerId);
    document.body.style.userSelect = 'none';
  });
  splitter.addEventListener('pointermove', function (e) {
    if (!dragging) return;
    var proposed = startW - (e.clientX - startX);
    var maxAllowed = Math.min(MAX_SIDE_W, runner.getBoundingClientRect().width - MIN_MAIN_W - 7);
    setSideWidth(Math.max(MIN_SIDE_W, Math.min(maxAllowed, proposed)));
  });
  function endDrag(e) {
    if (!dragging) return;
    dragging = false; splitter.classList.remove('dragging'); document.body.style.userSelect = '';
    try { localStorage.setItem(RUNNER_SIDE_W_KEY, parseFloat(getComputedStyle(runner).getPropertyValue('--runner-side-w')) || ''); } catch (err) {}
  }
  splitter.addEventListener('pointerup', endDrag);
  splitter.addEventListener('pointercancel', endDrag);
  splitter.addEventListener('keydown', function (e) {
    var step = e.shiftKey ? 40 : 12;
    if (e.key === 'ArrowLeft') { e.preventDefault(); setSideWidth(Math.min(MAX_SIDE_W, (runner.querySelector('.runner-side').getBoundingClientRect().width) + step)); }
    else if (e.key === 'ArrowRight') { e.preventDefault(); setSideWidth(Math.max(MIN_SIDE_W, (runner.querySelector('.runner-side').getBoundingClientRect().width) - step)); }
    else return;
    try { localStorage.setItem(RUNNER_SIDE_W_KEY, parseFloat(getComputedStyle(runner).getPropertyValue('--runner-side-w')) || ''); } catch (err) {}
  });
})();
var runSchema = schemaBrowser(function (text) { insertAtCursor($('run-sql'), text); },
  function (tableName) { previewTable($('run-connection').value, tableName); },
  function (table) { applySelectQuery($('run-sql'), $('run-connection').value, table); });
$('run-schema-slot').appendChild(schemaField(runSchema));
$('run-connection').onchange = function () { runSchema.setConnection($('run-connection').value); paintRunDatabase(); paintRunEditorMode(); };
function keyRun(e) { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); $('run-form').requestSubmit ? $('run-form').requestSubmit() : $('run-form').onsubmit(e); } }
$('run-sql').addEventListener('keydown', keyRun);
$('run-params').addEventListener('keydown', keyRun);
$('run-form').onsubmit = function (e) {
  e.preventDefault();
  runPage = Number($('run-page').value) || 1;
  recordRunHistory();
  runSql({});
};
$('run-explain-button').onclick = function () {
  if (!$('run-sql').value.trim()) { showError('Write some SQL to run.', { errors: { sql: 'This field is required' } }); $('run-sql').focus(); return; }
  runPage = 1;
  runSql({ explain: true, button: $('run-explain-button') });
};
/** "Save as New API": by the time someone reaches for this, the assumption is they've already tried the
 * query here and it does what they want - so this opens the same "New saved query" form the Saved Queries
 * screen's own button does (openQueryForm()), just pre-filled with what Run SQL already has: the query text
 * itself (SQL, or - unchanged either way - the mongo JSON convention text), the connection, and an empty
 * {name: {}} skeleton entry per detected bound parameter (mongoDocParams() for mongo, sqlParams() for SQL -
 * the same two functions the form's own "Bound in the query" hint already uses) so query_parameters starts
 * with every name ready to have a type/default added, not blank. */
$('run-save-as-api-button').onclick = function () {
  var connection = $('run-connection').value;
  var text = $('run-sql').value;
  if (!connection) { showError('Pick a connection first — add one on the Connections tab.'); return; }
  if (!text.trim()) { showError('Write a query first.', { errors: { sql: 'This field is required' } }); $('run-sql').focus(); return; }
  var isMongo = (connectionsCache[connection] || {}).db === 'mongo';
  var names = isMongo ? mongoDocParams(text) : sqlParams(text);
  var queryParameters = {};
  names.forEach(function (n) { queryParameters[n] = {}; });
  showTab('queries');
  openQueryForm(null, { connection_name: connection, sql_query: text, query_parameters: queryParameters });
};
function runSql(opts) {
  var connection = $('run-connection').value;
  if (!connection) { showError('Pick a connection first — add one on the Connections tab.'); return; }
  var isMongo = (connectionsCache[connection] || {}).db === 'mongo';
  if (isMongo) { runMongo(opts); return; }
  var sql = $('run-sql').value;
  if (!sql.trim()) { showError('Write some SQL to run.', { errors: { sql: 'This field is required' } }); $('run-sql').focus(); return; }
  var paramsText = $('run-params').value.trim();
  var params = {};
  if (paramsText) {
    try { params = JSON.parse(paramsText); }
    catch (e) { showError('Bound parameters must be valid JSON: ' + e.message, { errors: { params: e.message } }); return; }
  }
  var format = $('run-format').value;
  var pageSize = Number($('run-page-size').value) || 10;
  var timeout = $('run-timeout').value;
  $('run-page').value = String(runPage);
  var qs = 'format=' + enc(format) + '&page=' + runPage + '&page_size=' + pageSize + (timeout ? '&timeout=' + enc(timeout) : '');
  var sqlToRun = opts.explain ? 'EXPLAIN ' + sql : sql;
  // Only sent when it actually differs from the connection's own configured database - the common case (left
  // at the default) behaves exactly as before this existed, for every key, admin or scoped.
  var connDb = (connectionsCache[connection] || {}).database;
  var chosenDb = $('run-database').value;
  var body = { sql: sqlToRun, connection_name: connection, params: params };
  if (chosenDb && chosenDb !== connDb) body.database = chosenDb;
  execute(function () {
    return apiFetch('execute_sql?' + qs, { method: 'POST', json: body });
  }, {
    results: $('run-results'), status: $('run-status'), button: opts.button || $('run-button'), page: runPage, pageSize: pageSize,
    filename: opts.explain ? 'explain' : 'query', format: format,
    sql: sqlToRun, connection: connection, params: params, timeout: timeout,
    onPage: function (n) { runPage = n; runSql(opts); },
    onPageSize: function (n) { $('run-page-size').value = String(n); runPage = 1; runSql(opts); }
  });
}

/** The mongo branch of runSql(): the editor holds one JSON object - {"collection", "filter", "projection",
 * "sort"} - rather than SQL text (see BACKLOG #36 - MongoDB support is find-only, so there is no EXPLAIN
 * and no dedicated Collection/Filter form fields yet, just this one-textarea convention). */
function runMongo(opts) {
  if (opts.explain) { showError('EXPLAIN is not supported for Mongo connections yet.'); return; }
  var raw = $('run-sql').value;
  var doc;
  try { doc = raw.trim() ? JSON.parse(raw) : {}; }
  catch (e) { showError('The query must be valid JSON: ' + e.message, { errors: { sql: e.message } }); $('run-sql').focus(); return; }
  if (!doc || typeof doc !== 'object' || Array.isArray(doc) || !doc.collection) {
    showError('The query must be a JSON object with a "collection" field, e.g. {"collection": "users", "filter": {}}.',
             { errors: { sql: 'collection is required' } });
    $('run-sql').focus();
    return;
  }
  var paramsText = $('run-params').value.trim();
  var params = {};
  if (paramsText) {
    try { params = JSON.parse(paramsText); }
    catch (e) { showError('Bound parameters must be valid JSON: ' + e.message, { errors: { params: e.message } }); return; }
  }
  var format = $('run-format').value;
  var pageSize = Number($('run-page-size').value) || 10;
  var timeout = $('run-timeout').value;
  $('run-page').value = String(runPage);
  var qs = 'format=' + enc(format) + '&page=' + runPage + '&page_size=' + pageSize + (timeout ? '&timeout=' + enc(timeout) : '');
  var connection = $('run-connection').value;
  var body = { collection: doc.collection, filter: doc.filter || {}, connection_name: connection, params: params };
  if (doc.projection) body.projection = doc.projection;
  if (doc.sort) body.sort = doc.sort;
  execute(function () {
    return apiFetch('execute_mongo?' + qs, { method: 'POST', json: body });
  }, {
    results: $('run-results'), status: $('run-status'), button: opts.button || $('run-button'), page: runPage, pageSize: pageSize,
    filename: 'query', format: format, connection: connection, params: params, timeout: timeout,
    mongo: true, mongoBody: body,
    onPage: function (n) { runPage = n; runMongo(opts); },
    onPageSize: function (n) { $('run-page-size').value = String(n); runPage = 1; runMongo(opts); }
  });
}

/** A starter SELECT for a SQL table - every column by name (not "*"; the schema browser already knows
 * them), one per line, ordered by the first one, capped at a sane default so a quick look never pulls an
 * entire table. The table name is schema-qualified (schema.fetch_schema()'s 'schema' field - Postgres's
 * 'public', MySQL/ClickHouse's own database name, SQLite/DuckDB's fixed 'main', ...) since the same table
 * name can exist in more than one schema on the same connection. */
function buildSqlSelect(table) {
  var cols = table.columns.length ? table.columns.map(function (c) { return '  ' + c.name; }).join(',\n') : '  *';
  var target = (table.schema ? table.schema + '.' : '') + table.name;
  var sql = 'SELECT\n' + cols + '\nFROM ' + target;
  if (table.columns.length) sql += '\nORDER BY ' + table.columns[0].name;
  return sql + '\nLIMIT 100';
}
/** Grows `textarea`'s editor box (its parent .editor/.editor.boxed - both make the textarea's own height
 * meaningless, see the "hand-rolled highlighting" CSS comment, so it's the box, not the textarea, that has
 * to change) tall enough to show every line of the current value without needing to drag-resize it first -
 * never shrinks (a later shorter query just leaves spare room, same as picking a smaller browser window
 * wouldn't retroactively shrink a manually-resized box), and caps at 60% of the viewport so one huge query
 * cannot push the rest of the page off-screen; still hand-resizable past that with the box's own drag handle
 * either way. */
function fitEditorToContent(textarea) {
  var editorEl = textarea.parentElement;
  var lines = (textarea.value.match(/\n/g) || []).length + 1;
  var cs = getComputedStyle(textarea);
  var lineHeight = parseFloat(cs.lineHeight) || 18;
  var padY = (parseFloat(cs.paddingTop) || 0) + (parseFloat(cs.paddingBottom) || 0);
  var needed = Math.ceil(lines * lineHeight + padY) + 2; // a couple of px of slack so the last line never clips
  var current = editorEl.getBoundingClientRect().height;
  var maxAllowed = Math.max(needed, Math.round(window.innerHeight * 0.6));
  editorEl.style.height = Math.min(Math.max(needed, current), maxAllowed) + 'px';
}
/** The schema browser's "select" icon: writes a starter query for `table` into `textarea` - a full SELECT
 * (SQL) or an equivalent find() document (mongo: filter is already "every field", so there is nothing to
 * list there the way SQL's SELECT does; sort is mongo's ORDER BY - "limit" is left out on purpose, since
 * paging is already the Page/Page size fields, not something this JSON convention's body reads). Replaces
 * the whole editor rather than inserting at the cursor, same as previewTable() below - this is a complete
 * statement, not a fragment to weave into an existing one. */
function applySelectQuery(textarea, connectionName, table) {
  var isMongo = (connectionsCache[connectionName] || {}).db === 'mongo';
  textarea.value = isMongo ? JSON.stringify({ collection: table.name, filter: {}, sort: { _id: 1 } }, null, 2)
                            : buildSqlSelect(table);
  if (textarea.repaint) textarea.repaint();
  fitEditorToContent(textarea);
}
/** Jump to the Run SQL tab pre-filled with a preview of one table - from either schema browser. */
function previewTable(connectionName, tableName) {
  showTab('run');
  if (connectionName && connectionsCache[connectionName]) selectRunConnection(connectionName);
  var isMongo = (connectionsCache[connectionName] || {}).db === 'mongo';
  $('run-sql').value = isMongo ? JSON.stringify({ collection: tableName, filter: {} }, null, 2)
                                : 'SELECT * FROM ' + tableName;
  if ($('run-sql').repaint) $('run-sql').repaint();
  runPage = 1;
  recordRunHistory();
  runSql({});
}

// ---- client-side ad-hoc query history (this browser tab only; not the saved-query execution_history) ----
var RUN_HISTORY_KEY = 'queryapigate-run-history', RUN_HISTORY_MAX = 20;
function loadRunHistory() {
  try { return JSON.parse(sessionStorage.getItem(RUN_HISTORY_KEY) || '[]'); } catch (e) { return []; }
}
function recordRunHistory() {
  var sql = $('run-sql').value.trim();
  if (!sql) return;
  var entry = { sql: sql, connection: $('run-connection').value, format: $('run-format').value };
  var list = loadRunHistory().filter(function (e) { return !(e.sql === entry.sql && e.connection === entry.connection); });
  list.unshift(entry);
  if (list.length > RUN_HISTORY_MAX) list = list.slice(0, RUN_HISTORY_MAX);
  try { sessionStorage.setItem(RUN_HISTORY_KEY, JSON.stringify(list)); } catch (e) {}
  renderRunHistory();
}
function renderRunHistory() {
  var box = clear($('run-history-slot'));
  var list = loadRunHistory();
  if (!list.length) { box.appendChild(h('div', { className: 'hint', text: 'Queries you run will show up here.' })); return; }
  box.appendChild(h('div', { className: 'history-list' }, list.map(function (entry) {
    return h('button', { type: 'button', className: 'history-item', title: entry.sql, onclick: function () {
      $('run-sql').value = entry.sql;
      if ($('run-sql').repaint) $('run-sql').repaint();
      if (entry.connection && connectionsCache[entry.connection]) selectRunConnection(entry.connection);
      if (entry.format) $('run-format').value = entry.format;
    } }, entry.sql.length > 64 ? entry.sql.slice(0, 64) + '…' : entry.sql);
  })));
}

// ---- startup ----
function refreshAll() { loadConnections(); loadQueries(); loadApiKeys(); loadRoles(); loadAuditLog(); loadMetrics(); loadExamples(); loadSettings(); }
apiFetch('health').then(function (res) { return res.ok ? res.json() : null; }).then(function (info) {
  $('health-dot').className = 'dot ' + (info && info.status === 'ok' ? 'ok' : 'bad');
  $('version').textContent = info ? 'v' + info.version : 'unreachable';
}, function () { $('health-dot').className = 'dot bad'; $('version').textContent = 'unreachable'; });
try { var lastTab = sessionStorage.getItem('queryapigate-ui-tab'); if (lastTab && $('tab-' + lastTab)) showTab(lastTab); } catch (e) {}
applyPrefs();
renderRunHistory();
refreshAll();
</script>
</body></html>
"""

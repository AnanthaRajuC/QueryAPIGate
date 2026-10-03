# ADR 0001: QueryAPIGate Console, a React/TypeScript frontend over a versioned Management API

- **Status:** Accepted (2026-10-03)
- **Date:** 2026-10-03
- **Backlog:** #72 (Management API v1), #73 (QueryAPIGate Console)

## Context

The admin UI (`/ui`) began as a small helper page and has become the main way people use QueryAPIGate:
- connections, schema browsing, ad-hoc SQL;
- saved queries with versions, collections, cache and history;
- API keys, roles, audit, metrics, live events, settings, and MCP status.

It is implemented as one static HTML/CSS/JavaScript page embedded in a Python string (`queryapigate/ui.py`,
about 6,200 lines), with no build step.

**What is already right.** The UI has no server-side logic. It is a pure client of the JSON API: every
screen calls endpoints such as `/connections`, `/api_keys`, `/roles`, `/history`, `/settings` and `/events`
through one `apiJson()`/`apiFetch()` helper. Results are rendered through DOM APIs, never `innerHTML`, so
database values can't execute as markup. Separating the UI from the backend's internals has therefore already
happened.

**What is not.**
- **The UI itself has hit its limits.** A single file with no components, types, tests or build tooling makes
  each new screen slower to build and riskier to change. Where the product is heading needs:
  - a SQL editor with completion;
  - a guided query → secure → publish → monitor workflow;
  - per-persona navigation;
  - consistent tables, forms, and empty, loading and error states.
- **The management API grew one feature at a time and shows it:**
  - names left over from the file-based store (`/list_files`, `/save_sql_to_file`, `/view_file_content`,
    `/saved_sql/<name>`, `/execute_sql_from_file`);
  - RPC-style and resource-style routes mixed together;
  - no version prefix;
  - management and runtime routes side by side in one `app.py`.
- **The OpenAPI document is written by hand** (`queryapigate/openapi.py`), and nothing checks it against the
  routes. A typed client can't safely be generated from it until something does.
- **REST endpoints are covered by the project's compatibility policy.** Whatever the management API looks like
  when 1.0 freezes is what 1.x has to keep.

## Decision

1. **Build a new frontend, the QueryAPIGate Console,** in React + TypeScript (Vite), in this repository under
   `frontend/`. It is a client of public HTTP APIs only, never of Python internals.
2. **Design a versioned, resource-oriented Management API under `/api/v1/`** (queries, connections, API keys,
   roles, collections, history, audit, settings, MCP). It's a product interface in its own right: usable by
   the Console, scripts, Terraform or GitOps tooling, and other organizations' internal platforms.
3. **Treat the OpenAPI document as the contract between the two.**
   - A conformance test keeps it true: every route and method is documented, and (as #72 adds response
     schemas) real responses validate against them.
   - A copy of the document is committed as `frontend/openapi.json`. Python regenerates it
     (`frontend/scripts/dump_openapi.py`), and a backend test fails while it is stale. The frontend build turns it
     into TypeScript types (`openapi-typescript`) and a typed client (`openapi-fetch`), never hand-copied. That
     way the frontend build needs no Python, and backend contributors need no Node.
4. **Ship one artifact.** CI builds the Console's static assets and includes them in the Python wheel and the
   Docker image. Flask serves them at `/console`. Users never need Node.js, and `docker run` or `docker compose
   up` still gives the complete product.
5. **Migrate in phases, never a rewrite-and-switch** (see Migration). The current UI keeps working until the
   Console reaches feature parity, and from now on receives bug fixes only.

**Stack (deliberately conservative):**

| Concern | Choice |
|---|---|
| Language and build | TypeScript + Vite (static output only; no server-side rendering, so no Node in production) |
| Framework and routing | React (single-page app), React Router |
| Server state | TanStack Query; URL state next; a global store (Zustand) only if a real need appears |
| API client and types | `openapi-typescript` + `openapi-fetch`, from `frontend/openapi.json` |
| Styling and components | **The classic UI's own stylesheet** (`frontend/src/styles/classic.css`, ported verbatim from `ui.py`) and its markup and class names - see "Visual parity" below |
| Forms and tables | Plain React forms and `<table class="grid">` / `<table class="rs">`, as the classic screens use |
| SQL editor | CodeMirror 6 (`@codemirror/lang-sql`), chosen by spike - see Findings - styled as the classic `.editor.boxed` |
| Charts | Inline SVG, as the classic UI draws them; a library only if monitoring grows beyond that |
| Live events | a `fetch()` stream reader (`EventSource` can't send `X-API-Key`), honouring `Last-Event-ID` |
| Tests | Vitest + React Testing Library; Playwright for end-to-end |
| Lint and format | ESLint + Prettier |
| Toolchain | npm, Node.js 22 (`frontend/.nvmrc`) |

Version constraints found when scaffolding (2026-10-03): TypeScript is pinned to 5.9 because `openapi-typescript`
and `typescript-eslint` don't yet support TypeScript 6/7, and React Router 7 is used because 8 needs a newer Node 22
patch release than some contributors have. Revisit both when those constraints lift.

**Visual parity (amended 2026-10-03).** Until a deliberate, separately decided redesign, the Console looks and feels
exactly like the classic UI:
- the same stylesheet, sidebar (groups, names, order, icons, counts), header, breadcrumbs, Ctrl K search, API key
  panel, drawers, toasts and error banner;
- each rebuilt screen uses the classic screen's markup and layout.

Screens not yet rebuilt open in the classic UI on the matching tab, so moving between the two is seamless. New
capabilities (drafts and publishing) are expressed with the classic components (pill, `btn md`, the drawer form),
not a new visual language. Every slice is checked with side-by-side screenshots of the classic and Console screens,
light and dark.

This replaces the first-pass stack (Tailwind CSS + shadcn/ui + Radix, React Hook Form + Zod, TanStack Table, lucide
icons) and the workflow-based navigation (Build / Explore / Govern / Observe / AI / Admin) that the first Queries
slice used. Both made the Console look and behave unlike the classic UI, which was not the intent. The
workflow-based information architecture and the flagship connect → query → secure → publish → monitor flow remain
the direction for the redesign, when one is decided.

## Alternatives considered

- **Keep extending `ui.py`.** No new dependencies and no build step, but components, types and tests stay out
  of reach, and every screen added makes the eventual migration bigger. Rejected for new work; kept for fixes.
- **Server-rendered UI (Jinja + htmx).** Simpler and Python-only, but a poor fit for the editor-heavy,
  highly interactive screens the Console needs. It would also couple the UI to Flask instead of to a public
  API, which loses the Management API's value to other clients.
- **Vue, Svelte or another framework.** Technically viable. React was chosen for the size of its ecosystem
  (editor, table, form and component libraries) and of its contributor pool, which matters for an open-source
  project hoping for outside contributors.
- **A separate repository for the frontend.** It would decouple release cadence, but every API change would
  need a coordinated pair of PRs, version skew between UI and server would become possible, and generating
  types from OpenAPI would cross repositories. One maintainer and one release train favour one repository.
  Revisit if the Console gets its own team or release cycle.
- **Reorganise the Python package (`api/`, `auth/`, `core/`, `services/` …) up front.** Rejected as a big-bang
  move: it would break every import and every open PR at once. The service layer instead grows one resource
  at a time as `/api/v1` is built.

## Migration

1. **Foundations.**
   - Accept this ADR and freeze `ui.py` (fixes only).
   - Add the OpenAPI conformance test.
   - Scaffold `frontend/` with lint, type-check and test CI.
   - Make the Docker image a multi-stage build (a Node stage builds the assets; the Python image serves them).
   - Serve an empty Console at `/console`.
2. **Vertical slices.** Build the Console one area at a time, starting with the flagship workflow (Queries),
   then Connections and Schema, then Keys and Roles, then History, Metrics and Audit, then MCP and Settings.
   For each slice:
   - design that resource's `/api/v1` endpoints, driven by what the screen actually needs, with the service
     layer underneath;
   - point both the slice and the matching part of `ui.py` at them;
   - deprecate the old routes it replaces (BACKLOG #67).
3. **Parity.** When every `ui.py` screen exists in the Console, `/ui` redirects to `/console`. The Console
   leaves "experimental" (BACKLOG #64).
4. **Removal.** Delete `ui.py` one minor release after the redirect. Remove the deprecated routes when the
   deprecation policy allows.

Until step 3, the Console ships marked experimental, alongside the existing UI.

## Consequences

- **Positive:**
  - the Management API becomes a documented, versioned product surface other tools can build on;
  - the frontend gets types, components, tests and a design system;
  - the flagship workflow becomes possible to build.
- **Negative:**
  - Node.js joins the toolchain (for frontend contributors and CI, not for users or backend-only
    contributors);
  - two UIs exist until parity;
  - the image build gets a second stage;
  - a source checkout without built assets serves a "Console not built" page at `/console`.
- **Compatibility:**
  - `/api/v1` should exist before the project freezes its REST contract, so 1.x doesn't have to carry the
    legacy management routes as first-class;
  - runtime routes (`/q/<name>`, `/execute_sql`, `/events`, `/openapi.json`, `/catalog`, `/health`,
    `/metrics`) are not renamed by this decision.

## Findings from the foundations step

- **36 of 47 API operations had no response schema** in the OpenAPI document, almost all of them management
  endpoints. Route and method coverage was already complete. Types generated for those responses are therefore
  empty, which confirms that #72 has to design every `/api/v1` resource with full response schemas. `GET /health`
  was documented as part of this step because the Console's shell needed it.
- **`/ui` restores its last tab from sessionStorage,** so the Console's placeholder screens open the matching
  classic tab directly. That makes running both UIs side by side seamless for users.
- **SQL editor spike (2026-10-03): CodeMirror 6 over Monaco.** Both were built with the same SQL text and a
  two-table schema for completion, and both were served with the Console's exact CSP.

  | | CodeMirror 6 | Monaco 0.57 |
  |---|---|---|
  | Size, gzipped | **135 KB** | **~778 KB** (672 KB main + 90 KB worker + 13 KB CSS) |
  | Runs under the Console's CSP | Yes, no violations | Yes, no violations (worker bundled and served from `'self'`) |
  | Schema-aware SQL completion | Built in (`sql({ schema, dialect })`), with PostgreSQL, MySQL, SQLite and other dialects | Custom completion provider needed |
  | Mobile | Supported | Not supported upstream |

  CSP turned out *not* to distinguish them, contrary to the original expectation. A logged deliberate violation
  confirmed the check was real. The deciding factors are size (Monaco would make the Console's first load about
  6 times heavier for one screen), built-in dialect- and schema-aware completion matching QueryAPIGate's
  databases, and mobile support. Monaco's advantages (a VS Code feel, multi-cursor and minimap) don't outweigh
  that. A diff view for query versions, its other strength, is available for CodeMirror as `@codemirror/merge`.
  Note for anyone revisiting this: `@monaco-editor/react` loads Monaco from a CDN by default, which the Console's
  CSP blocks by design. Monaco would have to be bundled directly, as the spike did.
- **The root `.gitignore` ignored every `lib/` directory,** which would have silently excluded
  `frontend/src/lib/`. It now has an explicit exception.

## Findings from the parity pass (2026-10-03)

- **The first Queries slice looked nothing like the classic UI.** Only the colour tokens had been carried over;
  type scale, spacing, components, navigation and layout came from Tailwind and shadcn defaults. The fix was to make
  `ui.py`'s stylesheet the Console's stylesheet, verbatim, and rebuild the screens from the classic markup.
- **The classic layout depends on page structure.** The body is a grid (sidebar | content), forms rely on their
  slot wrapper, and the CSS keys on ids (`#tabs`, `#queries-panel`, `#key-panel`, ...). The Console reproduces that
  structure exactly: `#root` is `display: contents`, drawer forms sit in a slot `<div>`, and components keep the
  classic ids.
- **Shared per-browser state carries over.** The API key (sessionStorage), the sidebar's collapsed state, the star
  card's dismissal and the theme and density preferences (localStorage) use the classic keys, so both UIs stay in
  step.
- **Bugs found in the browser that the unit tests could not see:**
  - Escape closing the completion popup also closed the drawer, so the drawer now ignores keys the editor
    already handled.
  - Header buttons wrapped differently with the extra Unpublish button, so they now wrap as one group.
  - CodeMirror's fold gutter made the editor wider than the classic one, so it now uses a leaner setup without it.

## Open questions

- Console authentication: keep the admin API key in session storage (as `/ui` does today), or add a proper
  sign-in (OIDC for administrators) as part of the Console?
- Should runtime routes also move under `/api/v1` (e.g. `/api/v1/q/<name>`), or stay at the root as stable,
  user-facing URLs?
- Is the OpenAPI spec generated from code (route annotations or schema classes) or kept hand-written with the
  conformance test as its guard?

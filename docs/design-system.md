# Workspace design system

Milestone 18 defines how the RunWeave workspace looks and behaves before the
developer-platform screens are built. The source of truth is code:
`frontend/src/design/tokens.css` for tokens and `frontend/src/components/` for
shared components. This document explains the decisions and the review scope.

## Principles

- **Dark-first developer tool.** Designed for long sessions next to a terminal
  and editor. Dense, scannable data; quiet chrome; color reserved for state and
  data. A light theme ships with the same structure.
- **State is never color alone.** Every status carries an icon shape and a text
  label. Status colors are fixed and never reused as chart series.
- **Untrusted text stays text.** Agent input/output, model text, tool output,
  and API errors render as escaped text (React text nodes), never as HTML.
- **Offline and self-contained.** The workspace runs under a strict Content
  Security Policy (`default-src 'none'; script-src 'self'; style-src 'self'`).
  No external fonts, scripts, images, or inline styles; system font stacks only.
- **Small dependency surface.** React, Vite, and our own components. New UI
  dependencies need a clear requirement (see charts and tables below).

## Information architecture

A persistent left sidebar on wide screens; a top bar with a menu button below
900 px.

| Section | Purpose | Built in milestone |
| --- | --- | --- |
| Agents | Registered agents, connection type, health, active mode | 19, 20 |
| Runs | Run list and run detail (timeline, usage, result) | 19, 21 |
| Load tests | Concurrency and fault scenarios, live charts, reports | 22 |
| Evals | Datasets, per-case results, baseline comparison | 22 |
| Shadow | Two agent versions side by side, promotion gates | 22 |
| Approvals | Pending decisions with exact arguments and expiry | 23 |
| Settings | Providers, keys, budgets, fault profiles, theme | 21 |

First run shows the Setup flow (connect an agent, run a smoke test) instead of
an empty Agents page. The production navigation lists only sections that exist:
Agents and Runs since milestone 19.

## Tokens

Defined as CSS custom properties in `tokens.css`. Dark is the default design;
the theme follows the operating system unless the user picks Dark or Light, and
the choice is remembered in browser storage when available.

| Group | Tokens |
| --- | --- |
| Surfaces | `--page`, `--surface`, `--surface-raised`, `--surface-sunken` |
| Text | `--text`, `--text-secondary`, `--text-muted`, `--text-on-accent` |
| Lines | `--hairline`, `--border` (form controls, 3:1), `--focus` |
| Accent | `--accent`, `--accent-hover`, `--accent-text` |
| Status (fixed) | `--status-good`, `--status-warning`, `--status-serious`, `--status-critical`, `--status-info`, `--status-neutral` |
| Charts | `--series-1` … `--series-8`, `--grid`, `--axis` |
| Type | `--font-sans`, `--font-mono`, `--text-xs` … `--text-2xl` |
| Space and shape | `--space-1` … `--space-8` (4 px base), `--radius-sm/md/lg` |

Chart series and status colors come from the validated data-visualization
reference palette (categorical order is fixed; colorblind separation checked per
mode). Status: good `#0ca30c`, warning `#fab219`, serious `#ec835a`, critical
`#d03b3b`. In light mode warning and serious are below 3:1 on the surface by
design; their icon and label carry the meaning.

## Accessibility baseline (WCAG 2.1 AA)

Enforced by `frontend/test/tokens.test.js` in both themes:

- Body, secondary, and muted text: at least 4.5:1 on every surface.
- Accent text and text on accent buttons: at least 4.5:1.
- Focus ring and form-control borders: at least 3:1 against adjacent surfaces.
- Status indicators: at least 3:1 in dark mode; light-mode warning/serious rely
  on icon + label (documented exception).

Component rules: full keyboard operation with visible focus; a skip link; one
`main` landmark and a labelled `nav`; `aria-current="page"` on the active
section; tables use `caption`, `th scope`, and `aria-sort`; every chart has an
accessible name and a data-table view; motion respects
`prefers-reduced-motion`; layouts work from 360 px to wide monitors without
horizontal page scroll.

### How the audit runs

`frontend/browser-smoke.mjs` (`npm run test:browser`, in CI with PostgreSQL) drives the
real workspace and audits every screen and state it reaches with axe-core
(`frontend/a11y.mjs`, WCAG 2.0/2.1 A and AA rules). That is about 50 audits, in both
themes, at desktop width and at 390 px. A screen must also not scroll sideways at 390 px.
Findings from the whole run are written to `frontend/test-results/a11y-report.json`, and
any violation fails the run.

Accepted exceptions are listed in `ACCEPTED` in `frontend/a11y.mjs`, each with a reason
and repeated here. There are none today; the light-mode status-colour exception above is a
token rule that axe does not flag, because status badges always pair colour with an icon
and a label.

The same run checks what axe cannot:

- **Keyboard paths:**
  - The skip link comes first and moves focus to the content.
  - The sidebar tabs in visual order, and Enter on a link changes the screen, marks it with
    `aria-current` and moves focus to `#main`.
  - The theme switch works with the arrow keys.
  - Charts respond to the arrow keys and Escape.
  - Table rows can be chosen with Enter.
- **Focus management:**
  - Form errors move focus to the first invalid field, whose description is the message.
  - A confirmation takes focus, and Escape cancels it and returns focus to the button that
    opened it.
  - After a decision, focus lands on the panel heading rather than the page body.
- **Reduced motion:** with `prefers-reduced-motion: reduce`, a live run has no animations
  or transitions.

Content that scrolls inside its own box (wide tables and code at narrow widths) becomes a
named, focusable region while it overflows (`useScrollRegion`), so arrow keys can scroll
it. When the content fits, it adds no tab stop.

Automated checks don't replace a person. Before a release, also check that each screen
reads sensibly in a screen reader (Narrator on Windows, VoiceOver on macOS): headings,
the live run state, form errors, the approval confirmation, and chart summaries.

### Keyboard map

| Where | Keys |
| --- | --- |
| Any screen | Tab and Shift+Tab move through controls; the first Tab reaches *Skip to content* |
| Sidebar | Enter opens a section; focus moves to the screen's content |
| Theme switch | Arrow keys choose System, Dark or Light |
| Tables | Tab to a row's link or button, then Enter; sortable headers toggle with Enter or Space |
| Scrolling tables and code | Tab to the region, then the arrow keys scroll it |
| Charts | Tab to the chart; Left and Right move between points; Escape hides the tooltip |
| Approve or Reject | Enter opens the confirmation, which takes focus; Enter confirms; Escape cancels |

## Components

| Component | Notes |
| --- | --- |
| `AppShell` | Sidebar navigation, top bar, connection state, theme switch, skip link |
| `PageHeader` | Eyebrow, title, description, actions |
| `Panel` | Titled surface with optional actions |
| `StatusBadge` | Run and check states; icon shape + label + color |
| `DataTable` | Semantic table, sortable headers, numeric alignment, empty state |
| `StatTile` | Label, value, optional delta and sparkline |
| `KeyValueList` | Timing, usage, configuration facts |
| `Timeline` | Ordered run steps with state, duration, and detail |
| `LineChart` | SVG lines with crosshair tooltip, legend, and table view |
| `EmptyState`, `ErrorState`, `LoadingState` | Consistent non-data states; errors use `role="alert"` |
| `ThemeSwitch` | System / Dark / Light |

## Charts and tables decision

- Charts: our own small SVG components (`LineChart`, `Sparkline`) cover the
  milestone 18–22 needs (latency, throughput, queue depth, error rate over time)
  without a dependency. Milestone 22 re-evaluates a canvas library (uPlot,
  MIT) only if live load-test streams measurably slow the page.
- Tables: our own semantic `DataTable`. Milestone 24 chose **pagination** over windowed
  rendering, because every row on a page stays in the DOM for screen readers and
  find-in-page.
  - `usePages` and `Pager` show 100 rows at a time. The pager is a labelled `nav` that
    announces "Rows 101–200 of 5,000" politely.
  - Eval reports start on failed cases and can be searched by case id; shadow reports
    start on changed pairs. Bench failure categories, the Approvals inbox and the loaded
    runs page the same way.
- Long chart series are downsampled to about 400 points. Each bucket keeps its lowest and
  highest point, so spikes stay visible. The chart's data table says when it shows fewer
  points than were recorded.
- `frontend/perf-smoke.mjs` (`npm run test:perf`, in CI) holds the budget. It renders
  3,000 runs, a 200-step timeline, 5,000-case eval and shadow reports and a 2,000-tick live
  chart, and requires:
  - a first render under 1.5 s;
  - paging and search under 250 ms;
  - no main-thread task of 200 ms or more;
  - at most 100 table rows in the DOM.

## Clickable prototype

`npm --prefix frontend run dev:prototype` serves every screen with synthetic
data at `/prototype.html`; `npm --prefix frontend run build:prototype` writes a
static copy to `frontend/dist-prototype/`. The prototype is never included in
the production build served by the workspace. Its data is fictional and makes no
API calls.

Review checklist for the owner: navigation and section names; density and
readability in both themes; status and severity presentation; Run detail
timeline; Load test charts; Evals diff layout; Approvals inbox; Setup flow; and
anything missing before screens are built in milestones 19–23.

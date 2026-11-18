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
an empty Agents page. The production navigation lists only sections that exist;
until milestone 19 that is the built-in incident-triage workspace under Runs.

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
- Tables: our own semantic `DataTable`. Milestone 24 adds windowed rendering if
  large-data checks need it.

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

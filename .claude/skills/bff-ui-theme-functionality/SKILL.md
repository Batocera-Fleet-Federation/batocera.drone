---
name: bff-ui-theme-functionality
description: Use this when adding or changing any Batocera Drone browser UI — admin pages, Integrations, Stream Deck, modals, tables, accordions, forms, cards, or CSS. Required whenever markup uses Bootstrap table/modal/accordion/form components or a screen looks like default Bootstrap light theme (white tables, white unreadable modals). Covers app/web/static/css/drone.css, drone.js, and integrations.js.
---

# Drone UI theme

This app is a dark arcade admin (`--admin-*` tokens in `drone.css`). It never
sets `data-bs-theme="dark"`. Bootstrap 5's light defaults therefore win unless
markup uses the project classes **and** `drone.css` keeps global overrides for
the components Bootstrap paints white.

Tokens (do not invent new palette values):

- `--admin-bg` `#101828`, `--admin-surface` `#151f32`, `--admin-surface-muted` `#1f2a44`
- `--admin-border` `#31405f`, `--admin-text` `#ecf6ff`, `--admin-muted` `#9fb0c9`
- accents: `--admin-sidebar-accent` cyan, `--admin-accent-hot` pink, `--admin-accent-coin` gold, `--admin-accent-green`

## Required classes

| Component | Markup | Notes |
|---|---|---|
| Table | `table table-sm align-middle themed-table` | Never a bare `.table`. `table-hover` optional. |
| Modal | outer `modal fade`; inner `modal-content themed-modal`; close `btn-close btn-close-white` | `sdShowModal` / `openXModal()` already do this. |
| Accordion | `accordion themed-accordion` | Bootstrap's open header is otherwise a light-blue strip. |
| Card | existing `.card` (globally themed) | Do not set a white `background` on cards or tables. |
| Form | `.form-control` / `.form-select` (globally themed) | |

`drone.css` also themes `.table`, `.modal-content`, and `.accordion-*`
globally so a missed class still stays dark. Keep those global rules. Still
put the explicit classes on new markup so a later CSS regression is obvious.

## Nested modals

Bootstrap stacks every `.modal` at z-index 1055. A help/confirm dialog opened
from an already-open editor must add `sd-modal-nested` (z-index 1080) and mark
its backdrop `sd-modal-nested-backdrop` (1070). See `sdShowModal` in
`integrations.js`. Do not open a second modal behind the first.

## Do not

- Rely on Bootstrap light defaults (white `.table`, white `.modal-content`,
  white `.accordion-item`).
- Set `data-bs-theme="dark"` on `<html>` without auditing every existing override.
- Put raw `#fff` / `background: white` on admin content panels.
- Copy Stream Deck (or any feature) tables/modals without `themed-*` classes.
- Name a top-level function `bootstrap()` in `drone.js` — it clobbers
  `window.bootstrap` (see `drone-admin-features`).

## Check before finishing UI work

Search the new markup for `<table`, `modal-content`, and `accordion`. Each
table must include `themed-table`, each modal-content `themed-modal`, each
accordion `themed-accordion`. If a screenshot shows a white grid or white
dialog with light text, the theme contract was skipped.

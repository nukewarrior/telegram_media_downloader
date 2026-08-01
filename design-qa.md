# Design QA

## Comparison target

- Source visual truth: `/root/.codex/attachments/dce9c290-2608-48ca-841e-724a36200290/codex-clipboard-1503fe52-2058-4da1-9219-4e1bf990aa2e.png` (Google Photos time-navigation reference), `/root/.codex/attachments/e22e0497-7762-436d-a9dc-58e4464f58b2/codex-clipboard-9a99f70c-6bc4-48f3-94cc-5d97c07dde09.png` (proximity magnification reference), and `/root/.codex/generated_images/019fb926-d268-78a1-b160-dea66adfe990/exec-2dc859cf-1c04-4ad5-8b45-ab3686a3994b.png` (chosen light-theme composition).
- Implementation: `/root/telegram_media_downloader/frontend/src/views/ArchivesView.vue` and `/root/telegram_media_downloader/frontend/src/styles.css`.
- Intended viewport and state: 1440 × 1024 desktop, archive page with the media-bound right-edge navigation hovered; 1024px and narrower with the compact day jump menu opened.

## Evidence status

- Source visual references were opened and inspected.
- A browser-rendered implementation screenshot could not be captured: this environment has no Node.js/npm runtime and no browser-control surface is available, so Vite cannot be started or inspected.
- `git diff --check` passed. Frontend type checking, unit tests, interaction checks, console checks, and side-by-side visual comparison remain unavailable until a Node/browser-enabled environment is used.

## Required fidelity surfaces

- Fonts and typography: existing Noto Sans SC / Plus Jakarta Sans hierarchy is preserved in code; rendered comparison is blocked.
- Spacing and layout rhythm: the desktop rail is a zero-width sticky overlay within the media stream, with padded first/last daily positions; narrow layouts replace it with a flow-layout menu of days grouped by month. Rendered overflow checks are blocked.
- Colors and visual tokens: the existing off-white canvas and teal `#087f99` emphasis are used; rendered opacity and contrast checks are blocked.
- Image quality and asset fidelity: existing controlled archive thumbnails and Lucide icons are retained; no mock imagery was added to production UI. Browser crop checks are blocked.
- Copy and app-specific text: month headings, day headings, full-day floating labels, date-jump controls, and accessible labels are implemented in Chinese; screen-reader verification is blocked.

## Findings

- [P1] Browser and interaction verification unavailable.
  Location: archive route at desktop and responsive breakpoints.
  Evidence: Node.js/npm and browser-control capabilities are absent in the current environment.
  Impact: hover activation, drag-to-scrub behavior, scroll alignment, responsive layout, focus handling, and visual fidelity cannot be validated from static code.
  Fix: run `npm run build` and `npm test`, then open `/archives` at 1440 × 1024, 1024px, 768px, and 375px; capture the hovered desktop rail and open mobile-menu states, compare them against the listed source images, and update this report.

## Implementation checklist

- [x] Group archive entries by month and by every media-bearing day using complete `message_date` values, ordered newest first.
- [x] Keep the desktop time navigator as a hidden edge target that does not reserve content width and is bounded by the media stream.
- [x] Add hover, focus, keyboard, click, and drag navigation behavior over daily nodes with proximity scaling.
- [x] Add first/last padding and suppress nearby static year labels to avoid collision with the full-day floating label.
- [x] Provide a compact responsive day jump menu grouped by month and reduced-motion styling.
- [ ] Run browser-rendered visual comparison and interaction verification in a Node/browser-enabled environment.

## Comparison history

- Initial pass: browser-rendered implementation evidence is unavailable, so no side-by-side comparison could be performed.

final result: blocked

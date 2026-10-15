# Paperweight design

A desktop utility for people with oversized PDFs. They want the file smaller; seeing where the space goes is how they get there. Technical details stay available but never lead.

## Structure

- **No document open:** one task — open a PDF. No navigation, no empty panels. Several files open the batch screen: one quality control, an optional limit, a file list with results, Save / Save all.
- **Header:** sticky; brand, document name and size, **Make it smaller**, Open PDF.
- **Space map:** tall, scrollable rows from largest to smallest; small parts get a readable minimum tile and the caption says so.
- **Document open:** a narrow view list (Overview, Pages, Parts, Make smaller), the document name and size, and one primary action, **Make it smaller**.
- **Organize:** a sticky bar (page count and changes, Save as new PDF; Turn left/right, Delete, Blank page, Insert PDF, Undo) over a thumbnail grid. Click, Shift-click and Ctrl-click select; drag moves the selection; Delete, L, R, Ctrl+Z and Ctrl+A work from the keyboard. Moved, inserted and blank pages say where they came from.
- **Details panel** appears only when something is selected and closes with ✕ or Escape. Below 860 px it sits under the content.
- **Make smaller:** four quality cards (Lossless, High, Balanced, Compact) that double as the page preview's readout, a collapsed **More options** (cleanup items that this PDF actually has, pages to keep), an optional size limit, a page preview with side-by-side zoom (Fit, 2×, 4×, scrolled together), then the one submit button. The result reuses the same comparison view for redrawn pages.

## Rules

- Say each thing once. Privacy is stated on the start screen and in the compression note, nowhere else.
- Plain words: “Text & drawings”, “PDF structure”, “Parts”, “Its own / Shared / Total”. Object numbers, codecs and dictionaries live under **Technical details**.
- Numbers use English formatting everywhere (`19,271`, `13.37 MB`), tabular figures.
- Numeric table columns are right-aligned; text columns left-aligned. No decorative bars or per-row icons; the space map is the visual.
- Progress shows a plain phase name and a percentage, never internal object counts.
- One accent (green) for the primary action, selection and focus. Category colors only identify categories.

## Tokens

All colors are OKLCH custom properties in `web/style.css` (`--color-*`, `--cat-*`); no raw color values elsewhere. Type is the native Windows face (Segoe UI Variable Display for headings, Text for UI). Spacing, radii, durations and easing are tokens too. Motion is limited to color transitions and a progress pulse, and is removed under `prefers-reduced-motion`.

## Checked widths

1366 px desktop and 390 px phone, with no horizontal page scroll and no wrapped button or navigation labels.

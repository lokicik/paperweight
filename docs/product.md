# Product direction

## Audience

People who want to shrink large PDFs, especially illustrated books. Technical PDF details remain available on demand.

## Current workflow

Open a PDF → understand its storage map → inspect large components and their pages → preview a page at each quality → create a smaller, verified copy (or parts that each fit a limit) → compare redrawn pages with the original.

The first desktop version uses Tauri 2 with a bundled Python/PyMuPDF engine. Windows is the first validated target. The same interface can run in a browser for development.

## Next experiments

- Measure the compression method on scanned books, slide decks and photo-heavy PDFs; adjust presets from those results.
- Let people exclude specific pages from redrawing, or compress one selected image, from the explorer views.
- Link the space map to compression: show which categories a copy actually reduced.

## Invariants

- Local processing and read-only inspection of originals.
- Global sizes must reconcile to the file length.
- Shared resources count once in document totals.
- Referenced page sizes are not additive and are not estimates of deletion savings.
- No guaranteed visual equivalence or universal compression result from one test book.
- A compressed copy is offered only after the whole-document page checks pass.
- Sizes shown for a copy are measured, not estimated.

## Release state

Private development repository; AGPL-3.0-only license selected for a future open-source release. Public distribution, signing and cross-platform support remain separate milestones.

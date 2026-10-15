# Compression method

The original PDF is never modified. Compression writes a separate copy and offers it only after it passes the page checks.

## Steps

1. **Lossless cleanup**, in parallel. Remove object-level XMP metadata (the document's own metadata stays). Re-encode 8-bit images with one or three components as level-9 Flate with PNG predictors, and other streams that use only general-purpose filters (Flate, ASCII85, ASCIIHex, LZW, RunLength) as level-9 Flate, whenever that is smaller; on small images plain Flate is tried too, and the predictor parameters count against the gain. Store 8-bit RGB or gray images with at most 256 colors as indexed images with 1, 2, 4 or 8 bits per pixel, pixel for pixel identical, keeping an ICC-based color space as the palette base; soft masks stay gray, since PDF does not allow them to be indexed. Unused objects are dropped and objects are packed into object streams.
2. **Find heavy pages.** For each page, count its drawing streams plus the images and reusable artwork that no other page uses. Pages under 4 KB are skipped.
3. **Prepare each page in isolation**, in parallel:
   - Keep every text operation and the graphics state it depends on, including clipping paths. Drop painted paths, shadings, images and reusable artwork.
   - Render the remaining artwork without text at the preset resolution, crop away the white margin and use grayscale when the artwork has no color.
   - Encode as JPEG at the preset quality, as lossy JPEG 2000, and, for color artwork, as palette PNG. The palette has as few colors as fit: octree palettes of 16 to 256 colors are tried first, then a 256-color median-cut palette. Lossy candidates are kept only when their measured error does not exceed that JPEG's. JPEG 2000 first aims at that JPEG's own PSNR, then 1 and 3 dB higher, because OpenJPEG usually lands above its target. Keep the smallest. Gray artwork also tries PNG; full-color PNG and lossless JPEG 2000 are tried only when no lossy candidate fits, because on 420 sampled pages they never won.
   - Keep the page as it was when the image would not be clearly smaller, when text lives inside reusable artwork, when text is used as a clipping shape, or when the extracted text would change.
   - Skip the redraw when it cannot pay off: on pages that are mostly JBIG2 or CCITT black-and-white scans (no redraw is smaller), and on pages whose re-encodable pictures hold nearly all their bytes and are all shown at or below the preset's resolution (a redraw would only scale them up; the pictures are re-encoded instead).
   - **Re-encode images instead** when the page is not redrawn, or when redrawing would scale one of its images up: each 8-bit gray or RGB image of at least 20 KB that only this page uses is encoded again at its own resolution with the same candidates and the same error limit, measured against its own pixels. JPEGs, plain or inside Flate, whose quantization tables show a quality at or below the preset's are left as they are: they cannot get smaller within that quality's error. A new encoding is used only when it is at least 15% smaller, and the page keeps whichever change saves more. The page's content, text and other objects stay untouched.
4. **Build the copy.** Redrawn pages keep their page object, annotations and links; only their drawing and resources are replaced. The resources become page-owned so other pages are unaffected, and entries the new text layer never names are dropped. Re-encoded images keep their color space; a palette is indexed over it.
5. **Check the whole copy** against the original: extracted text on every page (the report says how many pages have text; scans may have none), link properties, bookmarks, page boxes and rotation, and how every page looks: both are rendered at 36 DPI and no area of about 0.2 inch may differ by more than 150 gray levels on average. Sound copies of the measured books stayed under 100, so this catches gross errors such as a missing, blacked-out or recolored image, not compression noise. A changed page that fails is put back in its original form; any other failure stops the job.

## Options

- **Also remove** (lossless; listed only when present): editing data from design apps (`/PieceInfo`), page thumbnails, attached files and their annotations, and the document's XMP metadata. Object-level XMP metadata is always removed.
- **Keep these pages as they are:** nothing on the listed pages is redrawn or re-encoded.
- **Split when the limit can't be met:** the finished copy is cut into consecutive page ranges. Each range is the longest whose real saved size stays under the limit (binary search on measured parts); bookmarks move with their pages, and each part's page text is compared with the copy. A single page larger than the limit becomes its own part and is reported as over the limit.
- **Several files:** each file goes through the same steps with its own checks.

## Modes

- **Quality:** change every page that gets smaller at the chosen preset. Lossless changes none.
- **Size limit:** stop after the lossless cleanup if it already fits (or if Lossless is the lowest quality allowed). Otherwise try presets from High down to the lowest allowed one. Within the first preset that fits, change the fewest pages that keep the measured size under the limit. If none fits, the report says so and offers the smallest copy at the lowest allowed preset.

| Preset | Resolution | JPEG reference quality |
| --- | ---: | ---: |
| Lossless | — | — |
| High | 200 DPI | 85 |
| Balanced | 150 DPI | 80 |
| Compact | 110 DPI | 75 |

## Measured on one illustrated book — 2026-10-04

1,694 pages, 211,983,909 bytes, High preset, 16 worker processes:

| Stage | Bytes |
| --- | ---: |
| Original | 211,983,909 |
| Lossless cleanup | 164,322,847 |
| Finished copy, 1,136 pages changed | 51,697,497 |

End to end: 72 seconds (366 seconds before the speedups of 2026-10-04). Every page check passed: text on all 1,694 pages, links, 1,266 bookmarks, page boxes. An earlier one-off experiment on the same book reached 60,577,752 bytes with hand-tuned scripts.

## Measured on five documents — 2026-10-04

16 worker processes on one Windows machine with 24 hardware threads. Every run passed all page checks and left the source file unchanged.

| Document | Kind | Original | Lossless | High | Balanced | Compact |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| Principles of Neural Science, 1,694 p | Digital; heavy vector figures | 212.0 MB | 164.3 MB | 51.7 MB | 39.1 MB | 27.9 MB |
| Neuroscience: Exploring the Brain, 1,019 p | 300 DPI color scan, no text layer | 563.5 MB | 494.0 MB | 196.5 MB | 144.9 MB | 107.2 MB |
| Cognitive neuroscience textbook, 1,119 p | Ebook conversion; JPEG photos ≤220 DPI | 39.5 MB | 37.5 MB | 16.8 MB | 14.4 MB | 12.5 MB |
| Storybook, 5 p | Full-page 72 DPI illustrations, ASCII85 + Flate | 8.25 MB | 4.77 MB | 0.51 MB | 0.42 MB | 0.36 MB |
| Programming book, 352 p | Text only | 1.68 MB | 1.45 MB | 1.45 MB | 1.43 MB | 1.40 MB |

Times at High: 72 s, 140 s, 16 s, 1.4 s and 1.5 s (before the speedups: 366 s, 544 s, 118 s, 7 s and 2 s). At Balanced: 49 s, 74 s and 11 s for the first three.

Observations:

- Pages already stored efficiently stay as they are. The text-only book gets only lossless savings; redrawing would make it larger.
- In the photo ebook every photo is already at or below 220 DPI. Redrawing those pages at 200 DPI would scale the photos up, so their JPEGs are re-encoded at their own resolution instead, which is where most of its saving comes from. Lower presets trade photo detail for size.
- The storybook's illustrations are only 72 DPI; re-encoding them at that resolution is far smaller than redrawing them at 200 DPI.
- On redrawn pages, color spaces, fonts and graphics states that only the old drawing used are dropped; on the illustrated textbook that alone saved about 3.6 MB.
- About half of the scan's pages are flat-color text pages. Exact indexed images shrink them losslessly; redrawing them at 200 DPI would be larger than the 300 DPI originals. Scanned text is visibly softer at Compact but stays readable.
- At reading size, illustrations and photos looked the same at High; at high zoom, fine paper texture and microscopy detail soften.

## Limits

- Redrawn artwork is an image: it no longer scales like vector art at very high zoom. Text and labels stay vector text.
- The error budget is a pixel measurement against the JPEG reference, not proof of perceptual equality.
- Text comparison uses MuPDF. Other readers may extract text differently.
- Optional content (layers) on a redrawn page is flattened to its default visibility.
- Five documents are not a general guarantee. Slide decks, CMYK print files and black-and-white scans are not yet measured.
- Black-and-white scanned text would benefit from bitonal encodings such as JBIG2, which are not implemented.

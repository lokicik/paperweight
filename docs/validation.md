# Validation

The initial explorer was checked with a 1,694-page illustrated book and generated fixtures. The existing compressed book was an input to the explorer, not a new compression result.

| Category | Encoded bytes |
| --- | ---: |
| Images | 32,702,663 |
| Page content | 22,593,541 |
| Font files | 216,749 |
| Reusable artwork | 15,797 |
| Metadata | 9,690 |
| Object containers | 2,757,937 |
| Other streams | 732,446 |
| Structure and remainder | 1,548,929 |
| **Total** | **60,577,752** |

The inventory contains 10,335 streams plus the structural remainder. Source hashes were unchanged after inspection and rendering. The input documents and their previews are not included in this repository.

Generated tests cover shared images, compressed object containers, masks, reference-shaped strings, protected/invalid input, source preservation, HTTP upload/preview, access guards and worker cancellation.

Browser checks covered file selection, shared resources, page navigation, search and narrow-screen layout.

## Windows packaging checks — 2026-10-04

- Nine generated-fixture analysis and local HTTP tests passed on Python 3.12.
- The PyInstaller executable passed an end-to-end check covering PDF upload, exact byte accounting, a shared image on two pages, image/page PNG previews, cancellation and a clean exit after the desktop parent's shutdown signal.
- The frozen-engine test caught a Windows startup deadlock caused by a blocking read on inherited stdin. The engine now checks the pipe for data before shutdown instead of holding it in a blocking read; this executable test remains the regression check.
- The Tauri 2 Windows release and NSIS installer built with Rust 1.90.0. The build is unsigned; installation on a separate clean Windows machine has not been tested.
- Launching the release opened the application window and its loopback engine. The packaged engine's web interface also completed the 1,694-page, 60,577,752-byte book through the file picker, with the same category totals and 10,335 streams as the initial browser prototype.

## Compression checks — 2026-10-04

- Generated fixtures cover vector-heavy pages, text inside reusable artwork (kept), rotated pages, links and outlines, password-protected input, reachable and unreachable size limits, and redrawing only the pages a limit needs.
- HTTP tests cover starting, validating, cancelling, downloading, previewing and exploring a compressed copy.
- The 1,694-page book compressed from 211,983,909 to 51,697,497 bytes at the High preset in 72 seconds; all whole-document checks passed. Details in [compression.md](compression.md).
- Four more documents (a 563.5 MB color scan, a photo ebook, a text-only book and an illustrated storybook) were compressed at High, and the larger two also at Balanced and Compact; all page checks passed. Results in [compression.md](compression.md).
- An 80-page excerpt with a 6 MB limit produced a 4.89 MB copy at High quality, redrawing 2 pages. Browser checks covered the form, progress, result, page comparison, download headers and opening the copy in the explorer.

These checks do not establish support for every PDF or a general compression quality guarantee.

# Third-party components

Paperweight is licensed under AGPL-3.0-only. Preserve this notice and the accompanying license when distributing it.

## Runtime components

- **PyMuPDF 1.28.2 and MuPDF** — PDF parsing, encoded stream access and rendering. Artifex Software provides these under AGPL or a commercial agreement. This project uses the AGPL option. [Official license information](https://pymupdf.readthedocs.io/en/latest/about.html#license-and-copyright).
- **pypdf 6.19.0** — content stream parsing for text-preserving compression. BSD-3-Clause. [Repository and license](https://github.com/py-pdf/pypdf).
- **Pillow 12.3.0 and OpenJPEG** — PNG, JPEG and JPEG 2000 encoding of redrawn artwork. Pillow uses the MIT-CMU (HPND) license; its wheels bundle OpenJPEG, libjpeg-turbo and zlib under their own permissive licenses. [Pillow license](https://github.com/python-pillow/Pillow/blob/main/LICENSE).
- **Tauri 2 and official plugins** — desktop integration. [Tauri repository and licenses](https://github.com/tauri-apps/tauri).
- **Python** — the interpreter is included in the packaged engine. [Python license](https://docs.python.org/3/license.html).
- **Microsoft Edge WebView2** — Windows web runtime, managed by the operating system or the installer bootstrapper. [Distribution documentation](https://v2.tauri.app/distribute/windows-installer/).

PyInstaller is used to build the engine executable. Its [license and bootloader exception](https://pyinstaller.org/en/stable/license.html) apply to that build component. The AGPL requirements of PyMuPDF remain applicable.

Dependency versions are recorded in the Python requirements, `pnpm-lock.yaml` and `src-tauri/Cargo.lock`. Public binary releases should include the relevant dependency notices and corresponding source. No user PDF, book or rendered book page belongs in the source distribution.

The treemap icon in `assets/icon.svg` was created for this project. The frontend uses local code and system fonts, with no CDN dependencies.

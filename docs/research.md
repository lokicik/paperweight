# Comparable tools and product hypothesis

Checked 2026-10-04. This is a focused comparison, not proof of an unoccupied market.

| Tool | Existing capability | Relevance |
| --- | --- | --- |
| Adobe Acrobat | Audit space usage breaks PDF storage down by component type. | File-size analysis already exists in a major PDF product. |
| PDFSyntax | Open-source Python package generates an HTML browser with object links, reverse usage links, a page index and a physical minimap. | PDF internals navigation already exists in open source; its documented interface emphasizes PDF syntax. |
| pdfsizeopt | Command-line PDF size optimizer. | Optimization itself is also an established open-source capability. |

Primary references:

- [Adobe: Audit space usage](https://helpx.adobe.com/acrobat/desktop/create-documents/optimize-pdfs/audit-space.html)
- [PDFSyntax browser documentation](https://github.com/desgeeko/pdfsyntax/blob/main/docs/browse.md)
- [pdfsizeopt repository](https://github.com/pts/pdfsizeopt)

## The hypothesis worth testing

A visual explorer can make the path from “my PDF is too large” to a specific, understandable action easier: find the large parts, see exactly what they contain, then preview and measure an optimization before applying it. Shared-resource accounting should make the numbers understandable without requiring knowledge of PDF objects.

The current prototype implements the inspection step. It combines a size map with image/page previews and explains why page costs overlap. It has not established that this combination is unique, nor that users prefer it to existing products.

The next validation is a small set of different real documents: scanned books, digitally generated books, slide decks and mixed PDFs. Check whether people can locate the dominant cost and make a useful decision without opening an object dictionary.

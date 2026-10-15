# Accounting model

- Total bytes come from the actual file length. MB and KB use decimal units.
- Active encoded stream payloads are counted once by object number.
- Object streams compress several objects together. Their individual compressed sizes cannot be measured independently, so the container has its own category.
- File structure is file length minus active stream payloads. It includes dictionaries, delimiters, indexes and older revisions that are no longer active.
- Page costs follow references, including inherited resources and annotation appearances. Resources may be referenced without being visibly drawn. Shared page costs cannot be summed into the document total.
- Image transparency masks are separate resources. A raw image preview can differ from the composed page; the page preview shows the composition.
- Inline images are part of the page drawing stream. They do not receive an invented separate encoded size.
- A document-wide or unreferenced resource is not automatically removable.
- Repaired input is explicitly flagged. Category assignments for a repaired file are inspection estimates.

Stored bytes are not predicted compression savings. Compression will need its own measured before/after comparison.

The prototype accepts inputs up to 1 GB; this is a limit, not a performance guarantee. Password-protected documents require an unlocked copy. Individual image previews are limited to 50 megapixels.

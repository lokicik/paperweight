# Contributing

Start with the development and test commands in README.md. Keep PDF processing local and preserve the source document.

When changing accounting or resource traversal, add a generated fixture that captures the difference. Check global totals, shared resources and relevant page/image previews. A referenced resource is not necessarily visibly drawn; do not label it as waste without evidence.

Do not commit real user PDFs, screenshots of their pages, absolute personal paths, caches, virtual environments, secrets or build outputs. Tests should generate their own fixtures.

Compression changes need a generated fixture and a measured before/after result. Report measured sizes only; never offer a copy that has not passed the page checks.

UI contributions should preserve the ordinary-user audience: size and visual content first; object dictionaries as optional details.

# Changelog

All notable changes to `kaos-pdf` are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0a1] — 2026-05-07

First public alpha. Apache-2.0. Earlier internal versions were proprietary.

### Added

- **PDF extraction pipeline** built on `pypdfium2` (Apache-2.0): produces
  a `kaos-content` `ContentDocument` AST with provenance (source URI,
  1-based page, bounding box, extractor name, confidence) on every node.
  PDFium calls are serialised through a global lock so the library is
  safe under threaded executors. See `docs/THREAD_SAFETY.md`.
- **Public Python API** — `extract_pdf`, `extract_pdf_bytes`,
  `extract_pdf_with_tables`, `extract_page_text`, `render_page`,
  `get_page_count`, `get_pdf_metadata`, `get_pdf_outline`,
  `classify_document`, `classify_page`, plus the re-exported
  `search_document` / `SearchResult` / `SearchResults` from
  `kaos-content`. `kaos_pdf.__all__` enumerates the surface.
- **Typed result models** — `PdfMetadata` and `PdfOutlineEntry`
  `@dataclass(frozen=True, slots=True)` types. Sparse `to_dict()`
  (None fields omitted) preserves the wire format for JSON consumers.
  `page_count` is carried directly on `PdfMetadata`.
- **Seven MCP tools** registered via `register_pdf_tools(runtime)`
  (requires `kaos-mcp` from source until that package ships on PyPI):
  `kaos-pdf-extract-parse`, `kaos-pdf-extract-page-text`,
  `kaos-pdf-render-page`, `kaos-pdf-metadata`,
  `kaos-pdf-search-document`, `kaos-pdf-get-outline`,
  `kaos-pdf-classify-page`. All seven are read-only / idempotent /
  non-destructive / non-open-world. Every `ToolResult.create_error()`
  call site returns a three-part recovery hint (what / how to fix /
  alternative tool) for LLM self-correction.
- **Optional extras**:
  - `[ocr]` — `pytesseract` engine for scanned PDFs (requires the
    system `tesseract` binary). `OCRMode` is the `extract_pdf(ocr=...)`
    setting; `OCREngine` is the engine ABC; `TesseractEngine` is the
    Apache-2.0 default. OCR paragraphs carry `Provenance.confidence`.
  - `[tables]` — `pdfplumber` engine (MIT, pure Python — no Java, no
    GPU) for borderless and multi-line tables. Extracted tables become
    `TabularDocument` with typed columns; live in the body with
    `Provenance.extractor = "kaos-pdf/tables/{engine}"`.
  - `[nlp]` — `kaos-nlp-core` for BM25 sentence-level search via
    `search_document(..., level="sentence")`.
- **Two CLI entry points** — `kaos-pdf` (admin) and `kaos-pdf-serve`
  (MCP server, stdio + streamable HTTP). Every structured subcommand
  supports `--json` for machine-readable output. CLI uses 1-based page
  numbers; the Python API is 0-based.
- **Errors** — `KaosPdfError` base + `PdfNotFoundError`,
  `PdfExtractionError`, `PdfRenderError`. Tools translate these into
  `ToolResult.create_error()`.

### Notes

- VLM page programs (describe / classify / VLM-OCR) live in
  `kaos-llm-core[vision]` ≥ 0.1.0a3, not in `kaos-pdf`. The split
  keeps the extraction → LLM dependency direction one-directional.
- `kaos-pdf` does not and will not depend on AGPL or GPL libraries.
  This rules out Surya for OCR and camelot-lattice / Tabula for
  tables.
- The `[mcp]` extra is intentionally not declared at 0.1.0a1 because
  `kaos-mcp` is not yet on PyPI; `uv lock` refuses to resolve any
  declared extra whose package is unresolvable. The extra returns
  once `kaos-mcp` ships.

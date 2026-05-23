# kaos-pdf

> **Part of [Kelvin Agentic OS](https://kelvin.legal) (KAOS)** — open agentic
> infrastructure for legal work, built by
> [273 Ventures](https://273ventures.com).
> See the [full KAOS package map](https://github.com/273v) for the rest of the stack.

[![PyPI - Version](https://img.shields.io/pypi/v/kaos-pdf)](https://pypi.org/project/kaos-pdf/)
[![Python](https://img.shields.io/pypi/pyversions/kaos-pdf)](https://pypi.org/project/kaos-pdf/)
[![License](https://img.shields.io/pypi/l/kaos-pdf)](https://github.com/273v/kaos-pdf/blob/main/LICENSE)
[![CI](https://github.com/273v/kaos-pdf/actions/workflows/ci.yml/badge.svg)](https://github.com/273v/kaos-pdf/actions/workflows/ci.yml)

`kaos-pdf` is the PDF-extraction layer of KAOS — it turns a PDF byte stream
into a typed [`kaos-content`](https://github.com/273v/kaos-content)
`ContentDocument` AST with provenance (page numbers, bounding boxes,
extraction confidence) on every node, plus a small set of read-only MCP
tools for agentic workflows. The engine is
[`pypdfium2`](https://github.com/pypdfium2-team/pypdfium2) (Apache-2.0)
and all PDFium calls are serialised through a global lock so the library
is safe to call from threaded executors. The primary parse path
(`parse_pdf`, `extract_outline`, `extract_metadata`, render APIs) returns
AST nodes, typed dataclasses, or `KaosImage` values with provenance.
`extract_page_text(path, page_number)` and its MCP counterpart
`kaos-pdf-extract-page-text` are an intentional escape hatch that
returns raw page text for lightweight context-free page inspection;
use the AST path when provenance, structure, or downstream redaction
/ verification is required.

The base install is intentionally small: three runtime dependencies
(`kaos-content[images,layout,markdown]`, `kaos-core`, `pypdfium2`) and no
compiled native code beyond the PDFium wheel. Heavier capabilities are
opt-in extras: `[ocr]` adds `pytesseract` for scanned pages (and requires
a system `tesseract` binary), `[tables]` adds `pdfplumber` (MIT, pure
Python — no Java, no GPU) for borderless and multi-line tables, and
`[nlp]` adds `kaos-nlp-core` for BM25 sentence-level search. VLM page
programs (describe / classify / OCR-via-VLM) live in
`kaos-llm-core[vision]` — they were moved out of `kaos-pdf` to
keep the extraction → LLM dependency direction one-directional. We do not
and will not depend on AGPL or GPL libraries (this rules out Surya for
OCR and camelot-lattice / Tabula for tables).

## Install

```bash
uv add "kaos-pdf>=0.1.0"
# or
pip install "kaos-pdf>=0.1.0"

# OCR for scanned PDFs (requires system tesseract binary)
uv add 'kaos-pdf[ocr]>=0.1.0'

# Structured table extraction via pdfplumber
uv add 'kaos-pdf[tables]>=0.1.0'

# BM25 sentence-level search via kaos-nlp-core
uv add 'kaos-pdf[nlp]>=0.1.0'
```

`kaos-pdf` requires Python **3.13** or newer (3.14 is supported). The
package is pure Python — the only native code is the PDFium wheel shipped
by `pypdfium2`, which has prebuilt wheels for Linux, macOS, and Windows
on x86_64 and arm64.

## Quick start

Extract a PDF into the document AST, render a page, and search for a term:

```python
from kaos_pdf import (
    extract_pdf,
    get_pdf_metadata,
    get_pdf_outline,
    render_page,
    search_document,
)

# Parse the whole document into a kaos-content ContentDocument
doc = extract_pdf("contract.pdf")
print(len(doc.body), "top-level blocks")

# Typed metadata (PdfMetadata dataclass; sparse to_dict() for JSON)
meta = get_pdf_metadata("contract.pdf")
print(meta.page_count, meta.title, meta.author)

# Outline / bookmarks (list[PdfOutlineEntry], also typed)
for entry in get_pdf_outline("contract.pdf"):
    print("  " * entry.level, entry.title, "p", entry.page)

# Render the first page as a 300-DPI PIL image (returned as KaosImage)
image = render_page("contract.pdf", page_number=0, dpi=300)
image.pil.save("page-1.png")

# AST-grounded search — paragraph-level by default
hits = search_document(doc, "indemnification", top_k=5)
for hit in hits.results:
    print(f"score={hit.score:.2f} :: {hit.text[:80]}")
```

Every node in the returned `ContentDocument` carries a `Provenance`
(source path, 1-based page, bounding box, extractor name, confidence)
so downstream consumers — citation verifiers, redaction tooling,
labelers — can ground answers back to the original PDF.

## Concepts

The package is a thin, typed surface over `pypdfium2`. The most important
entries:

| Concept | What it is |
|---|---|
| **`extract_pdf(path, *, pages=None, ocr="never", tables="geometric", extract_images=False, image_src_builder=...)`** | Primary entry point. Returns a `ContentDocument`. `pages` selects 0-based indices; `ocr` is `"never"` / `"auto"` / `"always"`; `tables` is `"geometric"` / `"engine"` / `"disabled"`; `image_src_builder` lets callers control the image URI policy (default inlines as `data:` URLs). |
| **`extract_pdf_bytes(data, ...)` / `extract_pdf_with_tables(path, ...)`** | Bytes-input variant and the sidecar form that returns `(ContentDocument, TabularDocument)` when you want tables out of the body. |
| **`render_page(path, page_number, *, dpi=300, grayscale=False)`** | Renders a single page (0-based) to a `KaosImage` (PIL + DPI + provenance). |
| **`extract_page_text(path, page_number)` / `get_page_count(path)`** | Lightweight per-page text + page-count helpers that skip full AST construction. |
| **`PdfMetadata` / `PdfOutlineEntry`** | `@dataclass(frozen=True, slots=True)` result types returned by `get_pdf_metadata()` and `get_pdf_outline()`. Sparse `to_dict()` (None fields omitted) preserves the historical wire format. `page_count` lives on `PdfMetadata` directly — no extra `get_page_count()` call needed. |
| **`classify_document(path)` / `classify_page(path, page_number)`** | Lightweight document/page-type heuristics (e.g. `text`, `scanned`, `mixed`). |
| **`search_document(doc, query, *, top_k=10, level="paragraph")`** | Re-exported from `kaos-content`. AST-grounded ranked search returning `SearchResults` with `total_matches` / `has_more` for pagination. `level="sentence"` requires the `[nlp]` extra. |
| **`OCRMode` / `OCREngine` / `TesseractEngine`** | OCR pluggability. `OCRMode` is the `extract_pdf(ocr=...)` setting; `OCREngine` is the engine ABC; `TesseractEngine` is the Apache-2.0 default (install with `[ocr]` + system tesseract). OCR paragraphs carry `Provenance.confidence` so verifiers can weight them. |
| **`TableMode` / `TableEngine` / `ExtractedTable` / `TableResult`** | Table pluggability. `pdfplumber` is the MIT default behind `[tables]`. Extracted tables become `TabularDocument` with typed columns and live in the body with `Provenance.extractor = "kaos-pdf/tables/{engine}"`. |
| **`ParsePDFTool`, `GetPageTextTool`, `RenderPageTool`, `PDFMetadataTool`, `SearchDocumentTool`, `GetOutlineTool`, `ClassifyPageTool`** | The seven `KaosTool` subclasses exposed over MCP as `kaos-pdf-extract-parse`, `-extract-page-text`, `-render-page`, `-metadata`, `-search-document`, `-get-outline`, `-classify-page`. All seven are `readOnly`, `idempotent`, non-destructive, non-open-world. Register with `register_pdf_tools(runtime)`. |
| **Errors (`KaosPdfError`, `PdfNotFoundError`, `PdfExtractionError`, `PdfRenderError`)** | Dedicated exception hierarchy. MCP tools translate these into `ToolResult.create_error()` with the documented three-part recovery hint (what / how to fix / alternative tool). |

## CLI

`kaos-pdf` ships two entry-point scripts. Every structured command on
the admin CLI supports `--json` for machine-readable output piped to
other agents:

```bash
kaos-pdf --help                                     # admin CLI
kaos-pdf-serve --help                               # MCP server

kaos-pdf info contract.pdf --json                   # metadata + page count + classification
kaos-pdf outline contract.pdf --json                # PDF bookmarks (falls back to detected headings)
kaos-pdf page contract.pdf 3 --json                 # plain text from a single page (1-based)
kaos-pdf extract contract.pdf -f markdown -p 1-5    # full AST → markdown / text / json / html
kaos-pdf render contract.pdf 1 --dpi 300 -o p1.png  # render a page as PNG
kaos-pdf classify contract.pdf --page 1 --json      # document- or page-level type
kaos-pdf search contract.pdf "indemnification" -k 5 # AST-grounded ranked search

kaos-pdf-serve                                      # stdio (Claude Code / Desktop)
kaos-pdf-serve --http --port 8000                   # streamable HTTP
```

The admin CLI uses 1-based page numbers (consistent with how the file
opens in any PDF viewer) and translates internally to the 0-based indices
the Python API uses. `kaos-pdf-serve` exposes the seven MCP tools listed
in **Concepts** above.

## Compatibility & status

| Aspect | |
|---|---|
| **Python** | 3.13, 3.14 |
| **OS** | Linux, macOS, Windows (pure-Python wheel; the only native code is the PDFium wheel shipped by `pypdfium2`) |
| **Maturity** | 0.1.0 GA. The public API is documented in `kaos_pdf.__all__`. |
| **Stability policy** | Pre-1.0: minor bumps may change behaviour. Every change is documented in [`CHANGELOG.md`](CHANGELOG.md). The MCP tool surface (`kaos-pdf-*` names) and the `KAOS_PDF_*` environment-variable namespace are public API and follow the same policy. |
| **Test coverage** | 340 unit tests plus a small integration tier hitting the MCP wire end-to-end. Bounded unit gate (`pytest tests/unit -q --no-cov`) finishes in ~35s. |
| **Type checker** | Validated with [`ty`](https://docs.astral.sh/ty/), Astral's Python type checker. |

## Documentation

Per-package reference: see in-tree docstrings and
[CHANGELOG.md](CHANGELOG.md).

Cross-cutting KAOS guides (agentic patterns, persona presets, settings
policy, citations, MCP data flow, migration to 0.1.0 GA) live in
[`kaos-modules/docs/guides/`](https://github.com/273v/kaos-modules/tree/main/docs/guides).

## Companion packages

`kaos-pdf` is one of the packages in the
[Kelvin Agentic OS](https://kelvin.legal). The broader stack:

| Package | Layer | What it does |
|---|---|---|
| [`kaos-core`](https://github.com/273v/kaos-core) | Core | Foundational runtime, MCP-native types, registries, execution engine, VFS |
| [`kaos-content`](https://github.com/273v/kaos-content) | Core | Typed document AST: Block/Inline, provenance, views |
| [`kaos-mcp`](https://github.com/273v/kaos-mcp) | Bridge | FastMCP server, `kaos` management CLI, MCP resource templates |
| [`kaos-pdf`](https://github.com/273v/kaos-pdf) | Extraction | PDF → AST with provenance |
| [`kaos-web`](https://github.com/273v/kaos-web) | Extraction | Web extraction, browser automation, search, domain intelligence |
| [`kaos-office`](https://github.com/273v/kaos-office) | Extraction | DOCX / PPTX / XLSX readers + writers to AST |
| [`kaos-tabular`](https://github.com/273v/kaos-tabular) | Extraction | DuckDB-powered SQL analytics |
| [`kaos-source`](https://github.com/273v/kaos-source) | Data | Government + financial data connectors (Federal Register, eCFR, EDGAR, GovInfo, PACER, GLEIF) |
| [`kaos-llm-client`](https://github.com/273v/kaos-llm-client) | LLM | Multi-provider LLM transport |
| [`kaos-llm-core`](https://github.com/273v/kaos-llm-core) | LLM | Typed LLM programming (Signatures, Programs, Optimizers) |
| [`kaos-nlp-core`](https://github.com/273v/kaos-nlp-core) | Primitives (Rust) | High-performance NLP primitives |
| [`kaos-nlp-transformers`](https://github.com/273v/kaos-nlp-transformers) | ML | Dense embeddings + retrieval |
| [`kaos-graph`](https://github.com/273v/kaos-graph) | Primitives (Rust) | Graph algorithms + RDF/SPARQL |
| [`kaos-ml-core`](https://github.com/273v/kaos-ml-core) | Primitives (Rust) | Classical ML on the document AST |
| [`kaos-citations`](https://github.com/273v/kaos-citations) | Legal | Legal citation extraction, resolution, verification |
| [`kaos-agents`](https://github.com/273v/kaos-agents) | Agentic | Agent runtime, memory, recipes |
| [`kaos-reference`](https://github.com/273v/kaos-reference) | Sample | Reference module for module authors |

Packages depend on `kaos-core`; everything else is opt-in. Mix and match the
ones you need.

## Development

```bash
git clone https://github.com/273v/kaos-pdf
cd kaos-pdf
uv sync --group dev
```

Install pre-commit hooks (recommended — they run the same checks as CI on
every commit, scoped to staged files):

```bash
uvx pre-commit install
uvx pre-commit run --all-files     # one-time full sweep
```

Manual QA commands (the same set CI runs):

```bash
uv run ruff format --check kaos_pdf tests
uv run ruff check kaos_pdf tests
uv run ty check kaos_pdf tests
uv run pytest tests/unit -q --no-cov
```

## Build from source

```bash
uv build
uv pip install dist/*.whl
python -c "import kaos_pdf; print(kaos_pdf.__version__)"  # smoke import
```

## Contributing

Issues and pull requests are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md)
for setup, quality gates, pull request expectations, and engineering
standards. By contributing you agree to follow the
[project conduct expectations](CODE_OF_CONDUCT.md) and certify the
[Developer Certificate of Origin v1.1](https://developercertificate.org/) —
sign every commit with `git commit -s`. Please open an issue before starting
on a non-trivial change so we can align on scope.

## Security

For security issues, **please do not file a public issue**. Report privately
via [GitHub Private Vulnerability Reporting](https://github.com/273v/kaos-pdf/security/advisories/new)
or email **security@273ventures.com**. See [SECURITY.md](SECURITY.md) for the
full disclosure policy.

## License

Apache License 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).

Copyright 2026 [273 Ventures LLC](https://273ventures.com).
Built for [kelvin.legal](https://kelvin.legal).

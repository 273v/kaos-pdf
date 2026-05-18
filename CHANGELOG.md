# Changelog

All notable changes to `kaos-pdf` are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0a7] — 2026-05-18

### Added

- **`kaos-pdf-search-document` result dicts** now include
  `path: list[str]` per hit — the structural breadcrumb
  (root-first, INCLUDING the immediate section). For PDFs with an
  embedded outline the path carries the chain of enclosing heading
  texts; for outline-less PDFs (the common case) the path is empty,
  which is the explicit "no structural identifier available"
  contract. Downstream agents MUST NOT invent section identifiers
  for hits with empty `path`. See
  `kaos-modules/docs/plans/persona-matrix-followups.md` §4.

### Changed

- **kaos-content floor raised to `>=0.1.0a11`** to pick up the
  structural-breadcrumb contract on `SearchResult.path` and
  `DocumentView.block_path()`. Pass-through for kaos-pdf internals
  (the search tool just forwards `r.path` through to its result
  dict).

## [0.1.0a6] — 2026-05-17

### Changed

- **kaos-core floor raised to `>=0.1.0a10`** to pick up the URI
  contract redesign (bare names route through
  `context.default_vfs_namespace`; `file://` and `vfs://` schemes).
  See `kaos-modules/docs/plans/uri-contract-redesign.md`. The 6
  file-input PDF tools route through `resolve_input_path` as
  pass-throughs.

## [0.1.0a5] — 2026-05-17

### Changed

- **All six file-input MCP tools now resolve their `path` argument
  through `kaos_core.path_resolver.resolve_input_path()`** instead of
  raw `Path(p).exists()` against the process CWD. The affected tools
  are `kaos-pdf-extract-parse`, `kaos-pdf-extract-page-text`,
  `kaos-pdf-render-page`, `kaos-pdf-metadata`, `kaos-pdf-get-outline`,
  and `kaos-pdf-classify-page`. Each tool's `path` parameter now
  accepts three input shapes — an absolute filesystem path
  (unchanged), a `kaos://artifacts/<id>` URI returned by a previous
  extract/materialise tool in the same session, or a relative path
  that resolves through the session VFS (e.g. files uploaded through
  a host UI's chat panel). The new
  `kaos_pdf._path_resolver.resolve_pdf_input()` async context manager
  centralises the call and constrains accepted mime types to
  `application/pdf`. Existing absolute-path callers — every CLI
  invocation, every fixture-based test — continue to work as a
  passthrough; only the previously-broken VFS / artifact paths are
  affected. `kaos-pdf-search-document` was already artifact-first via
  `artifact_id` and is unchanged. (Stage 2 of
  `kaos-modules/docs/plans/vfs-blind-tools-audit-and-fix-plan.md`,
  filed after the production incident in single-user-chat session
  `01KRVYAEA3B1HG95DBAG6H0DJ3` where five uploaded NDA `.docx` files
  routed through the SPA returned `File not found` from every tool
  call and the agent then fabricated a legal-analysis table citing
  those files — the same failure class affected kaos-pdf for any
  PDF uploaded into the session VFS.)

### Added

- `kaos_pdf._path_resolver.resolve_pdf_input()` — internal async
  context manager wrapping `kaos_core.path_resolver.resolve_input_path`
  with the PDF mime allow-list and a stub `KaosContext` for CLI
  callers. Tools call it instead of `Path(...).exists()`.
- `ParsePDFTool` now threads the source-artifact id (when the input
  was a `kaos://artifacts/<id>` URI) into both the parsed-document
  manifest's metadata (`source_artifact_id` / `source_body_uri`) and
  the response `structured_content` so downstream tools can trace the
  parsed `ContentDocument` back to its source PDF artifact without
  having to re-resolve the original chat scrollback.
- Every refactored tool's `path` parameter schema now describes the
  three accepted input shapes (absolute path, `kaos://artifacts/<id>`,
  session-VFS relative path) so an LLM inspecting the schema can
  discover that bare artifact URIs are accepted without reading the
  source.

### Dependencies

- Bumped `kaos-core` requirement from `>=0.1.0a3,<0.2` to
  `>=0.1.0a9,<0.2` to pick up `kaos_core.path_resolver`.

## [0.1.0a4] — 2026-05-15

### Added — tool-group registration entry points (PRD PR 1)

- **`register_pdf_documents_tools(runtime)`** — registers the 7
  read-only PDF tools (parsers, extractors, renderers, metadata
  inspectors, outline + classification). This is the entry point a
  consumer of the SessionToolSet `documents` group should call.
- **`register_pdf_authoring_tools(runtime)`** — public surface for
  future `kaos-pdf-write-*` writers, mutators, redactors. Currently
  registers 0 tools; the stable function exists so the
  SessionToolSet `authoring` group has a registration entry point
  before writers ship.
- **`register_pdf_tools(runtime)`** is now a backward-compatible
  union of the two — callers that previously got 7 tools continue
  to get 7. The wrapper composes the new entry points so a future
  authoring writer registered into one half flows automatically
  into the union.

The split is motivated by
`kaos-modules/docs/internal/dynamic-tool-planning-prd.md` §4
("PR 1 — catalog expansion"). It is purely additive: no existing
function signature, tool name, schema, or behavior changes.

## [0.1.0a3] — 2026-05-15

### Fixed

- **`kaos-pdf-extract-parse`'s `pages` parameter now declares its
  element type.** Previously the schema was `type=array` with no
  `items` declaration, which OpenAI's strict JSON Schema validator
  rejected with HTTP 400 `invalid_function_parameters`. The entire
  tool catalog for the turn was lost, leaving the agent to
  hallucinate answers. Now `items: {type: "integer", minimum: 0}` so
  the LLM gets a precise contract (0-based page indices, no
  negative values). kaos-core 0.1.0a7's defensive `items: {}` floor
  is also in play as belt + suspenders.

### Security

- **vulture (dead-code scan) now runs in pre-commit + CI alongside
  the existing bandit job.** New `vulture` hook in
  ``.pre-commit-config.yaml`` mirrored by a new ``vulture (dead-code
  scan)`` job in ``security.yml``. `--min-confidence 100` with the
  shared `--ignore-names` list for names vulture can't infer from
  the import graph (framework callbacks, OAuth/OIDC field names,
  signal handlers, MCP `_meta` keys). Also lands the existing
  bandit hook in pre-commit (it was only in CI before). Both pass
  clean. Mirrors the rollout from kaos-core.
### Changed

- **uv.lock is now tracked in git.** Previously gitignored at v0.1.0a1
  because the ``[mcp]`` optional extra (and the ``kaos-mcp`` dev
  dependency) referenced a sibling not yet on PyPI; ``uv lock``
  couldn't resolve them. ``kaos-mcp`` shipped (0.1.0a2), so the
  original gating reason no longer applies. Tracking the lockfile
  gives reproducible local dev environments, lets Dependabot surface
  sibling-version bumps as PRs, and makes the supply-chain pin set
  publicly auditable. Mirrors the org-wide convention being adopted
  across all 16 kaos-* repos.

## [0.1.0a2] — 2026-05-08

CI supply-chain hardening (audit-02 F7) and documentation accuracy
(audit-02 F8). No source code or public API changes.

### Security

- **F7: CI supply-chain hardening.** `.github/workflows/security.yml`
  pins the gitleaks Docker image to `v8.21.2` (no longer tracking
  `:latest`), adds a Bandit static-analysis job (medium severity /
  medium confidence, AST-level — `B101,B404,B603,B607` skipped because
  pytest assertions, subprocess use, and known-safe partial-path
  invocations are intentional), and runs the integration suite on
  `schedule` and `workflow_dispatch` so cross-package regressions
  surface against `main` even though the unit gate stays the PR fast
  path. SHA-pinning of GitHub Actions themselves remains a follow-up;
  the existing `.github/dependabot.yml` `github-actions` ecosystem PRs
  continue to keep tag-pinned actions current.

### Changed

- **F8: `SECURITY.md` rewritten to match the actual surface.** The
  previous file was the cross-package template — it referenced
  `ProgramOfThought`, `batch_run`, the semantic cache, and Program v3
  envelope JSON, all of which live in `kaos-llm-core` / `kaos-agents`,
  not `kaos-pdf`. The new file documents the real boundaries: PDF
  input handling (malformed / encrypted / oversize / metadata),
  Tool-layer validation, the global PDFium lock, OCR / table-extraction
  subprocess wrappers, and the OIDC release pipeline. Out-of-scope
  items now correctly list third-party dependencies (`pypdfium2`,
  `pillow`, `pytesseract`, `camelot-py`, `tabula-py`) and the bundled
  PDFium binary.

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

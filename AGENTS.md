# AGENTS.md

Repository-local guidance for coding agents working on `kaos-pdf`.
This file is the canonical cross-tool instruction file for this
repository.

## Scope

- Follow this file for all automated coding-agent work in this
  repository.
- Keep changes focused and public-repository appropriate.
- Preserve user changes already present in the worktree.
- For contributor process, use [CONTRIBUTING.md](CONTRIBUTING.md).
- For detailed engineering rules, use:
  - [Python design and architecture](docs/standards/python-design-and-architecture.md)
  - [Code quality standards](docs/standards/code-quality-standards.md)
  - [Engineering process](docs/standards/engineering-process.md)
  - [Tests, fixtures, and CI](docs/standards/tests-fixtures-ci.md)

## Project Identity

- Distribution: `kaos-pdf`.
- Import package: `kaos_pdf`.
- Runtime: Python 3.13+.
- Package type: pure Python, typed, Apache-2.0 licensed.
- CLI entry points: `kaos-pdf` and `kaos-pdf-serve`.
- Core purpose: extract PDF content into the `kaos-content`
  `ContentDocument` AST with provenance, page, bounding-box, and
  confidence data suitable for downstream verification.
- Public contracts include `kaos_pdf.__all__`, CLI commands and
  `--json` output, MCP tool names and schemas, dataclass/Pydantic wire
  shapes, documented errors, and environment-variable names.

## Setup

Use `uv` for local environments, dependency resolution, builds, and
tool execution.

```bash
uv sync --group dev
uvx pre-commit install
```

Public extras are optional and must stay lazy:

- `ocr` for Tesseract-backed OCR.
- `onnx` for local ONNX OCR via RapidOCR (PP-OCRv5 on ONNX Runtime;
  Apache-2.0 code + models; no PyTorch / no transformers runtime).
- `tables` for `pdfplumber` table extraction.
- `nlp` for BM25 sentence-level search through `kaos-nlp-core`.

## Local Checks

Run the focused quality gate before handing off code changes:

```bash
uv run ruff format --check kaos_pdf tests
uv run ruff check kaos_pdf tests
uv run ty check kaos_pdf tests
uv run pytest tests/unit -q --no-cov
```

Use `ty`, not mypy. Inline type suppressions use `# ty: ignore[...]`
with the narrowest practical rule.

When packaging, release metadata, README rendering, or build behavior
changes, also run:

```bash
uv build
uvx --from twine twine check --strict dist/*
```

For docs-only changes, run at least `git diff --check` and a practical
Markdown/link sanity check.

## Architecture Rules

- Keep `kaos_pdf` import-time work minimal: no filesystem scans,
  network calls, provider setup, logging setup, OCR engine loads, or
  PDF parsing at import time.
- Keep the top-level API small and explicit through `kaos_pdf.__all__`.
- Use typed dataclasses or Pydantic models for external shapes rather
  than loosely structured dictionaries.
- Keep optional dependencies behind extras and lazy imports.
- Centralize parser, OCR, table, and renderer adapters so dependency
  details do not leak through public contracts.
- Treat CLI, MCP, JSON/schema, error, and environment-variable behavior
  as stable public surfaces once released.
- Keep blocking PDF rendering, parsing, OCR, and table work off async
  event loops. Use bounded offloading and respect cancellation cleanup
  when async callers are involved.
- Preserve PDFium thread-safety protections. `pypdfium2` calls must
  remain serialized through the repository's locking strategy unless a
  documented upstream guarantee and tests justify a change.

## PDF Principles

- `pypdfium2` is the PDF engine and is acceptable because it is
  Apache-2.0 compatible.
- Do not add GPL, AGPL, unknown-license, non-commercial, or
  no-derivatives PDF, OCR, table, or vision dependencies.
- Keep the base install small. Heavy OCR, table, NLP, and vision
  capabilities belong behind explicit extras or companion integrations.
- Extracted content should become `kaos-content` AST nodes, typed
  records, or `KaosImage` values. Do not introduce public APIs that
  return ungrounded raw text as the primary result.
- Preserve provenance on extracted content: source, page number,
  bounding box, extractor identity, and confidence when available.
- Use realistic PDF fixtures for parser, layout, OCR, table, rendering,
  and MCP behavior. Fixtures must be redistributable, documented, and
  free of secrets, sensitive data, and restricted content.
- Bound untrusted PDF handling with practical file-size, page-count,
  rendering-DPI, image-size, recursion, and wall-time limits.
- Handle paths and temporary files safely. Do not traverse directories,
  follow unexpected symlinks, leak internal paths in user-facing errors,
  or leave temporary rendered pages behind after failure.
- Keep OCR, table, and vision behavior optional. Missing optional
  dependencies should produce actionable errors, not import failures for
  the base package.

## Testing

- Bug fixes need regression tests.
- New public behavior needs tests through the real public entry point.
- Security-sensitive behavior needs accepted and rejected cases.
- Unit tests must not require network, credentials, large downloads, or
  local services.
- Use realistic PDFs for extraction behavior; use small synthetic PDFs
  only when they directly isolate a condition.
- MCP and CLI changes must cover stable names, JSON/schema shapes, exit
  behavior, and actionable error messages.

## Security

- Never commit secrets, tokens, secret keys, credentials, `.env`
  files, customer documents, privileged content, or unknown-license PDF
  fixtures.
- Redact sensitive data in logs, CLI output, JSON output, MCP errors,
  and exceptions.
- Validate untrusted PDF inputs early and fail with bounded,
  predictable errors.
- Do not discuss suspected vulnerabilities in public issues; follow
  [SECURITY.md](SECURITY.md).
- Do not weaken dependency license posture to add PDF functionality.

## Commits, PRs, And Releases

- Use conventional commit style and sign commits with `git commit -s`.
- Keep docs-only, code, tests, packaging, and release changes separated
  when possible.
- PRs should state what changed, why, how it was tested, and whether
  public API, CLI, MCP schema, package metadata, fixtures, or release
  artifacts changed.
- User-visible behavior changes need a `CHANGELOG.md` entry.
- Releases require green formatting, linting, typing, tests, build,
  strict metadata check, and a fresh install smoke test as described in
  the standards.
- Do not move public tags or force-push shared branches.
